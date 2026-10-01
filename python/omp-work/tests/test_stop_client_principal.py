"""OMP-430: only owner or client principals engage the agent stop.

Defended contracts over create_app HTTP:
- Two provision_client principals (full, stop_only) each engage the stop (200).
- An automation principal holding work.stop gets 403 forbidden without calling store.
- A client principal attempting release_stop gets 403 forbidden.
- An owner principal attempting release_stop succeeds (200).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.capabilities import (
    capabilities_dir,
    provision_client,
    write_capability,
)
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import (
    CommandEnvelope,
    OperationReceipt,
    OperationState,
)
from omp_work.v1.server import create_app

WORKSPACE = UUID("00000000-0000-7000-8000-000000000430")


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        envelope: CommandEnvelope,
        *,
        actor_id: UUID,
        actor_kind: str,
        required_scope: str,
    ) -> tuple[OperationReceipt, dict[str, object]]:
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_id": actor_id,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
            }
        )
        stopped = envelope.command.type == "engage_stop"
        reason = getattr(envelope.command.payload, "reason", None)
        return (
            OperationReceipt(
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                state=OperationState.APPLIED,
                request_sha256="0" * 64,
                result_sha256="1" * 64,
            ),
            {
                "type": envelope.command.type,
                "stopped": stopped,
                "reason": reason,
            },
        )


def _envelope(command_type: str, reason: str = "test reason") -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORKSPACE),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": command_type, "payload": {"reason": reason}},
    }


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-OMP-Workspace-ID": str(WORKSPACE),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def test_stop_client_principal_engages_over_http(tmp_path: Path) -> None:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )

    full_path = provision_client(config, WORKSPACE, name="client-full", stop_only=False)
    stop_path = provision_client(config, WORKSPACE, name="client-stop", stop_only=True)
    token_full = json.loads(full_path.read_text())["token"]
    token_stop = json.loads(stop_path.read_text())["token"]

    auto_path = write_capability(
        config,
        "automation",
        actor_id=uuid4(),
        actor_kind="automation",
        workspaces=(WORKSPACE,),
        scopes=("work.stop", "work.read"),
    )
    token_auto = json.loads(auto_path.read_text())["token"]

    owner_path = write_capability(
        config,
        "owner",
        actor_id=uuid4(),
        actor_kind="owner",
        workspaces=(WORKSPACE,),
        scopes=("work.read", "work.approve"),
    )
    token_owner = json.loads(owner_path.read_text())["token"]

    store = _RecordingStore()
    app = create_app(
        config,
        capabilities_dir=capabilities_dir(config),
        store=store,  # type: ignore[arg-type]
    )
    client = TestClient(app)

    # 1. Full client engages (200)
    resp = client.post(
        "/v1/commands",
        headers=_headers(token_full),
        json=_envelope("engage_stop", "full engage"),
    )
    assert resp.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["command_type"] == "engage_stop"
    assert store.calls[0]["actor_kind"] == "client"
    assert store.calls[0]["required_scope"] == "work.stop"

    # 2. Stop-only client engages (200)
    resp = client.post(
        "/v1/commands",
        headers=_headers(token_stop),
        json=_envelope("engage_stop", "stop only engage"),
    )
    assert resp.status_code == 200
    assert len(store.calls) == 2
    assert store.calls[1]["command_type"] == "engage_stop"
    assert store.calls[1]["actor_kind"] == "client"
    assert store.calls[1]["required_scope"] == "work.stop"

    # 3. Automation holding work.stop gets 403; store not called
    resp = client.post(
        "/v1/commands",
        headers=_headers(token_auto),
        json=_envelope("engage_stop", "automation attempt"),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert len(store.calls) == 2

    # 4. Client release_stop is 403; store not called
    resp = client.post(
        "/v1/commands",
        headers=_headers(token_full),
        json=_envelope("release_stop", "client release attempt"),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert len(store.calls) == 2

    # 5. Owner release succeeds (200)
    resp = client.post(
        "/v1/commands",
        headers=_headers(token_owner),
        json=_envelope("release_stop", "owner release"),
    )
    assert resp.status_code == 200
    assert len(store.calls) == 3
    assert store.calls[2]["command_type"] == "release_stop"
    assert store.calls[2]["actor_kind"] == "owner"
    assert store.calls[2]["required_scope"] == "work.approve"
