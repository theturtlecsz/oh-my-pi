"""OMP-416-s04: unsigned relay authorization. No database.

The designated controller's unsigned pause reaches the store. Another client,
and a workspace with no designation, are 403 not_designated_controller with
the store uncalled. A relay without an instruction is 400.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import OperationReceipt, OperationState
from omp_work.v1.owner_controller import designation_message, write_designation
from omp_work.v1.owner_signature import NAMESPACE
from omp_work.v1.server import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
CONTROLLER = UUID("00000000-0000-7000-8000-0000000000c1")
OTHER = UUID("00000000-0000-7000-8000-0000000000c2")


class _RecordingStore:
    """Fake WorkStore: every call is recorded; the result echoes the relay."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        payload = envelope.command.payload
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_id": actor_id,
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
                "type": "relay_owner_intent",
                "intent": payload.intent,
                "mission": None,
                "decision_id": None,
                "answer": None,
                "relay": {
                    "instruction": payload.instruction.model_dump(mode="json"),
                    "relayed_by": str(actor_id),
                    "signed": payload.owner_signature is not None,
                },
            },
        )


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _capabilities(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_id in (("controller", CONTROLLER), ("other", OTHER)):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "client",
                    "workspaces": [str(WORKSPACE)],
                    "scopes": ["work.client"],
                }
            )
        )
        path.chmod(0o600)
    return directory


def _designate(config_dir: Path, key_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    key = _generate_key(key_dir, "owner")
    signers = config_dir / "owner_allowed_signers"
    signers.write_text(
        f"owner {Path(f'{key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    write_designation(
        config_dir,
        WORKSPACE,
        CONTROLLER,
        _sign(key, designation_message(WORKSPACE, CONTROLLER)),
    )


def _client(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config_dir = tmp_path / "config"
    config = OperationsConfig(
        config_dir=config_dir,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def _pause_body(*, instruction: dict[str, object] | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": "pause",
        "mission_id": str(uuid4()),
    }
    if instruction is not None:
        payload["instruction"] = instruction
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORKSPACE),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": "relay_owner_intent", "payload": payload},
    }


def _instruction() -> dict[str, object]:
    return {
        "text": "Pause the mission.",
        "source_message_ref": "msg-1",
        "owner_authored": True,
        "received_at": "2026-09-30T12:00:00+00:00",
    }


def _post(client: TestClient, token: str, body: dict[str, object]):
    return client.post(
        "/v1/commands",
        headers={
            "Authorization": f"Bearer {token}",
            "X-OMP-Contract-SHA256": contract_sha256(),
            "X-OMP-Workspace-ID": str(WORKSPACE),
        },
        json=body,
    )


def test_controller_unsigned_pause_reaches_the_store(tmp_path: Path) -> None:
    store = _RecordingStore()
    _designate(tmp_path / "config", tmp_path)
    client = _client(tmp_path, store)

    response = _post(client, "controller-token", _pause_body(instruction=_instruction()))

    assert response.status_code == 200, response.json()
    assert store.calls == [
        {
            "command_type": "relay_owner_intent",
            "actor_id": CONTROLLER,
            "actor_kind": "client",
            "required_scope": "work.client",
            "workspace_id": WORKSPACE,
        }
    ]


def test_other_client_unsigned_pause_is_not_the_controller(tmp_path: Path) -> None:
    store = _RecordingStore()
    _designate(tmp_path / "config", tmp_path)
    client = _client(tmp_path, store)

    response = _post(client, "other-token", _pause_body(instruction=_instruction()))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_designated_controller"
    assert store.calls == []


def test_no_designation_refuses_the_controller(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    response = _post(client, "controller-token", _pause_body(instruction=_instruction()))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_designated_controller"
    assert store.calls == []


def test_relay_without_instruction_is_invalid(tmp_path: Path) -> None:
    store = _RecordingStore()
    _designate(tmp_path / "config", tmp_path)
    client = _client(tmp_path, store)

    response = _post(client, "controller-token", _pause_body(instruction=None))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert store.calls == []
