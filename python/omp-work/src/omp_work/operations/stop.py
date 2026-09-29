"""OMP-405: the ``omp-work stop`` operational commands.

``stop check`` is the systemd ``ExecCondition=`` probe for the host unit
timers: exit 0 when the agent stop is not engaged, 1 when it is (the unit start
is skipped), and 255 on any error, including an unreachable service (the unit
start fails so ``Restart=`` retries). ``stop status`` prints the workspace's
``StopStatusView`` as JSON; ``engage`` and ``release`` submit the matching
command envelope with fresh identifiers and print the receipt.

Credentials come from the shared client config
(``$XDG_CONFIG_HOME/omp-work/client.json``, written by
``write_client_config``); ``--client-config`` and ``--bearer-file`` override
the path and the capability file it names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from ..v1.api_models import CommandResponse, StopStatusView
from ..v1.client import WorkClient
from ..v1.models import (
    CommandEnvelope,
    EngageStopCommand,
    ReleaseStopCommand,
    StopReasonPayload,
)

__all__ = [
    "API_VERSION",
    "check",
    "engage",
    "load_client",
    "release",
    "status",
]

API_VERSION = "work.omp.dev/v1"


def load_client(
    client_config: Path, bearer_file: Path | None = None
) -> tuple[WorkClient, UUID]:
    """Build the WorkClient the client config names. ``bearer_file`` overrides
    the capability file the config points at (the ``--bearer-file`` flag)."""
    config = json.loads(client_config.read_text(encoding="utf-8"))
    if bearer_file is None:
        bearer_file = Path(str(config["bearer_file"]))
    workspace_id = UUID(str(config["workspace_id"]))
    return (
        WorkClient(str(config["base_url"]), workspace_id, bearer_file),
        workspace_id,
    )


def status(client: WorkClient) -> StopStatusView:
    return client.stop_status()


def check(client: WorkClient) -> int:
    """ExecCondition result: 0 running, 1 stopped, 255 on any error. systemd
    logs the diagnostic, so the failing reason goes to stderr."""
    try:
        stopped = client.stop_status().stopped
    except Exception as error:
        print(f"stop: {error}", file=sys.stderr)
        return 255
    return 1 if stopped else 0


def engage(client: WorkClient, workspace_id: UUID, reason: str) -> CommandResponse:
    return client.execute(_envelope(workspace_id, "engage_stop", reason))


def release(client: WorkClient, workspace_id: UUID, reason: str) -> CommandResponse:
    return client.execute(_envelope(workspace_id, "release_stop", reason))


def _envelope(
    workspace_id: UUID, command_type: str, reason: str
) -> CommandEnvelope:
    payload = StopReasonPayload(reason=reason)
    command = (
        EngageStopCommand(type="engage_stop", payload=payload)
        if command_type == "engage_stop"
        else ReleaseStopCommand(type="release_stop", payload=payload)
    )
    return CommandEnvelope(
        api_version=API_VERSION,
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )
