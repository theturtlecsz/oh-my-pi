from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .config import OperationsConfig

OWNER_SCOPES = (
    "work.read",
    "work.mutate",
    "work.approve",
    "work.close",
    "work.execute",
    "work.stop",
)
# OMP-402: the automation principal keeps the owner's original five scopes —
# flood writes items and /execute appends evidence and runs execution
# commands. Intake publication stays owner-only through the existing
# actor_kind check in the store; finer per-command limits belong to OMP-403
# (finding E0481). The stop scope is held only by the owner and the client
# principal (OMP-405), so this tuple is not an alias of OWNER_SCOPES.
AUTOMATION_SCOPES = (
    "work.read",
    "work.mutate",
    "work.approve",
    "work.close",
    "work.execute",
)
DEFAULT_BASE_URL = "http://127.0.0.1:54322"
# OMP-416: the client principal — a monitoring caller that reads the ledger and
# engages the agent stop. With --stop-only it is narrowed to work.stop alone.
CLIENT_SCOPES = ("work.read", "work.client", "work.stop")
CLIENT_STOP_ONLY_SCOPES = ("work.stop",)
# OMP-415: the push runner. work.events.admin is not an owner scope.
EVENT_PUSH_SCOPES = ("work.read", "work.events.admin")


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o777 != 0o700:
        raise ValueError(f"unsafe credential directory permissions: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def capabilities_dir(config: OperationsConfig) -> Path:
    directory = config.config_dir / "capabilities"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o777 != 0o700:
        directory.chmod(0o700)
    return directory


def _validate_loopback(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("client base_url must be a bare loopback http URL")
    return base_url.rstrip("/")


def write_capability(
    config: OperationsConfig,
    name: str,
    *,
    actor_id: UUID,
    actor_kind: str,
    workspaces: tuple[UUID, ...],
    scopes: tuple[str, ...],
    candidate_ids: tuple[UUID, ...] | None = None,
) -> Path:
    if "work.candidate.read" in scopes and not candidate_ids:
        raise ValueError(
            "candidate read capabilities require a non-empty candidate_ids allowlist"
        )
    data: dict[str, object] = {
        "token": secrets.token_urlsafe(32),
        "actor_id": str(actor_id),
        "actor_kind": actor_kind,
        "workspaces": [str(workspace) for workspace in workspaces],
        "scopes": sorted(scopes),
    }
    if candidate_ids is not None:
        data["candidate_ids"] = [str(candidate) for candidate in candidate_ids]
    path = capabilities_dir(config) / f"{name}.json"
    _write_secret(path, json.dumps(data, indent=2, sort_keys=True))
    return path


def write_client_config(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    base_url: str,
    bearer_file: Path,
) -> Path:
    data = {
        "base_url": _validate_loopback(base_url),
        "workspace_id": str(workspace_id),
        "owner_id": str(owner_id),
        "bearer_file": str(bearer_file),
    }
    # NOT config.config_dir: this file is the shared contract with the TS
    # workflow client (session-system/extensions/workflow/config.ts), which
    # reads XDG_CONFIG_HOME/omp-work/client.json.
    path = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "omp-work"
        / "client.json"
    )
    _write_secret(path, json.dumps(data, indent=2, sort_keys=True))
    return path


def provision_owner(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    base_url: str = DEFAULT_BASE_URL,
) -> Path:
    bearer = write_capability(
        config,
        "owner",
        actor_id=owner_id,
        actor_kind="owner",
        workspaces=(workspace_id,),
        scopes=OWNER_SCOPES,
    )
    return write_client_config(
        config,
        workspace_id=workspace_id,
        owner_id=owner_id,
        base_url=base_url,
        bearer_file=bearer,
    )


def provision_candidate_reader(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    candidate_ids: tuple[UUID, ...],
    name: str = "candidate-reader",
) -> Path:
    return write_capability(
        config,
        name,
        actor_id=uuid4(),
        actor_kind="task-agent",
        workspaces=(workspace_id,),
        scopes=("work.candidate.read",),
        candidate_ids=candidate_ids,
    )


def provision_automation(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    name: str = "automation",
) -> Path:
    """OMP-402: mint the automation principal's own capability — a distinct
    actor_id and token with actor_kind "automation", one workspace, and
    AUTOMATION_SCOPES. The owner's capability is never reused or overwritten:
    ``name == "owner"`` would clobber it, so it is refused."""
    if name == "owner":
        raise ValueError("automation capability must not reuse the owner name")
    return write_capability(
        config,
        name,
        actor_id=uuid4(),
        actor_kind="automation",
        workspaces=(workspace_id,),
        scopes=AUTOMATION_SCOPES,
    )


def provision_event_push(
    config: OperationsConfig,
    workspace_id: UUID,
    name: str = "event-push",
) -> Path:
    """OMP-415: mint the push runner — actor_kind automation, one workspace,
    work.read and work.events.admin. ``name == "owner"`` would clobber the
    owner capability, so it is refused. OWNER_SCOPES stays unchanged."""
    if name == "owner":
        raise ValueError("event-push capability must not reuse the owner name")
    return write_capability(
        config,
        name,
        actor_id=uuid4(),
        actor_kind="automation",
        workspaces=(workspace_id,),
        scopes=EVENT_PUSH_SCOPES,
    )


def provision_client(
    config: OperationsConfig,
    workspace_id: UUID,
    name: str = "client",
    *,
    stop_only: bool = False,
) -> Path:
    """OMP-416: mint the monitoring principal — a distinct actor_id and token
    with actor_kind "client", one workspace. A full client can read the ledger
    (``work.read``), act as a client (``work.client``), and engage the agent
    stop (``work.stop``); ``stop_only`` narrows it to ``work.stop`` alone, so
    it can engage the stop and read the stop status but never read the ledger
    or mutate it."""
    return write_capability(
        config,
        name,
        actor_id=uuid4(),
        actor_kind="client",
        workspaces=(workspace_id,),
        scopes=CLIENT_STOP_ONLY_SCOPES if stop_only else CLIENT_SCOPES,
    )
