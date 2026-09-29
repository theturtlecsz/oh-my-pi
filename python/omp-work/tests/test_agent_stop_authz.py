"""OMP-405: command authorization for the agent stop.

Without a database: ``engage_stop`` is a ``work.stop`` action any stop client
may issue, while ``release_stop`` is owner-only — a non-owner holding
``work.approve`` is refused before the store is reached. ``POST /v1/commands``
lets an ``engage_stop`` through the OMP-89 stale-source refusal (safety action,
like halt/pause) while ``release_stop`` keeps the 503.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import omp_work.v1.server as server_module
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import OperationReceipt, OperationState
from omp_work.v1.server import create_app

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")

_AUTOMATION_SCOPES = [
    "work.read",
    "work.mutate",
    "work.approve",
    "work.close",
    "work.execute",
]


class _RecordingStore:
    """Fake WorkStore: every call is recorded; the result echoes the command."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
                "workspace_id": envelope.workspace_id,
            }
        )
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
                "stopped": envelope.command.type == "engage_stop",
                "reason": envelope.command.payload.reason,
            },
        )


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_kind, scopes in (
        ("grokbot", "grokbot", ["work.stop"]),
        ("automation", "automation", _AUTOMATION_SCOPES),
        ("owner", "owner", ["work.read", "work.approve"]),
        ("owner-no-stop", "owner", ["work.read", "work.approve", "work.close"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": actor_kind,
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _client(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities_dir(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def _envelope(command_type: str, reason: str) -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORKSPACE),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": command_type, "payload": {"reason": reason}},
    }


def _post(client: TestClient, token: str, command_type: str, reason: str):
    return client.post(
        "/v1/commands",
        headers={
            "Authorization": f"Bearer {token}",
            "X-OMP-Contract-SHA256": contract_sha256(),
            "X-OMP-Workspace-ID": str(WORKSPACE),
        },
        json=_envelope(command_type, reason),
    )


def test_grokbot_engages_but_release_is_refused(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    engaged = _post(client, "grokbot-token", "engage_stop", "runaway loop")
    assert engaged.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["command_type"] == "engage_stop"
    assert store.calls[0]["required_scope"] == "work.stop"
    assert store.calls[0]["actor_kind"] == "grokbot"

    refused = _post(client, "grokbot-token", "release_stop", "all clear")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert len(store.calls) == 1


def test_automation_with_work_approve_cannot_release(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    refused = _post(client, "automation-token", "release_stop", "all clear")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert store.calls == []


def test_owner_with_work_approve_releases(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    released = _post(client, "owner-token", "release_stop", "all clear")
    assert released.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["command_type"] == "release_stop"
    assert store.calls[0]["required_scope"] == "work.approve"
    assert store.calls[0]["actor_kind"] == "owner"


def test_owner_without_work_stop_cannot_engage(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    refused = _post(client, "owner-no-stop-token", "engage_stop", "runaway loop")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert store.calls == []


def test_stale_service_engages_but_cannot_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)
    monkeypatch.setattr(server_module, "code_fingerprint", lambda: "patched-stale")

    engaged = _post(client, "grokbot-token", "engage_stop", "runaway loop")
    assert engaged.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["command_type"] == "engage_stop"

    refused = _post(client, "owner-token", "release_stop", "all clear")
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "unavailable"
    assert len(store.calls) == 1
