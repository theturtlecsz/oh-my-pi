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
)
DEFAULT_BASE_URL = "http://127.0.0.1:54322"


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
