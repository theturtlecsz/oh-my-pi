"""OMP-402: the Work Ledger automation principal.

``ops capabilities automation`` mints a distinct capability (actor_kind
"automation", fresh actor_id/token, the owner's scopes) that the service
accepts as a mutate principal, and ``ops capabilities client-config`` lets the
automation write its own client.json without ever pointing at a bearer file it
could not authenticate with.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import OperationReceipt, OperationState
from omp_work.v1.server import create_app
from omp_work.__main__ import main


def _capability_path(xdg: Path, name: str) -> Path:
    return xdg / "omp" / "work-ledger" / "capabilities" / f"{name}.json"


def test_cli_automation_writes_distinct_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    workspace_id = uuid4()
    owner_id = uuid4()

    assert main(["ops", "capabilities", "init", "--workspace-id", str(workspace_id), "--owner-id", str(owner_id)]) == 0
    assert main(["ops", "capabilities", "automation", "--workspace-id", str(workspace_id)]) == 0

    automation = _capability_path(tmp_path, "automation")
    owner = _capability_path(tmp_path, "owner")
    assert automation.stat().st_mode & 0o777 == 0o600
    data = json.loads(automation.read_text())
    assert data["actor_kind"] == "automation"
    assert data["workspaces"] == [str(workspace_id)]
    assert data["scopes"] == sorted(
        ["work.read", "work.mutate", "work.approve", "work.close", "work.execute"]
    )
    # Fresh actor_id and token: never the owner's identity.
    assert data["actor_id"] != json.loads(owner.read_text())["actor_id"]
    assert data["token"] != json.loads(owner.read_text())["token"]

    # The owner name is reserved — provisioning it would clobber the owner.
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "ops",
                "capabilities",
                "automation",
                "--workspace-id",
                str(workspace_id),
                "--name",
                "owner",
            ]
        )
    assert exc.value.code != 0
    assert json.loads(owner.read_text())["actor_kind"] == "owner"


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        self.calls.append(
            {
                "actor_id": actor_id,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
                "workspace_id": envelope.workspace_id,
            }
        )
        receipt = OperationReceipt(
            operation_id=envelope.operation_id,
            request_id=envelope.request_id,
            state=OperationState.APPLIED,
            request_sha256="0" * 64,
            result_sha256="1" * 64,
        )
        result = {
            "type": "create_work_batch",
            "items": [
                {
                    "client_ref": "ref-1",
                    "work_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "key": "OMP-1",
                    "state": "BACKLOG",
                    "row_version": 1,
                }
            ],
        }
        return receipt, result


def _envelope(workspace_id: UUID) -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {"client_ref": "ref-1", "title": "OMP-402 automation item"}
                ]
            },
        },
    }


def test_service_accepts_automation_principal_and_rejects_wrong_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    workspace_id = uuid4()
    assert main(["ops", "capabilities", "automation", "--workspace-id", str(workspace_id)]) == 0
    automation = _capability_path(tmp_path, "automation")
    token = json.loads(automation.read_text())["token"]

    config = OperationsConfig.defaults()
    store = _RecordingStore()
    app = create_app(
        config, capabilities_dir=automation.parent, store=store  # type: ignore[arg-type]
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    response = client.post("/v1/commands", headers=headers, json=_envelope(workspace_id))
    assert response.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["actor_kind"] == "automation"
    assert store.calls[0]["required_scope"] == "work.mutate"
    assert store.calls[0]["workspace_id"] == workspace_id

    wrong = client.post(
        "/v1/commands",
        headers={**headers, "Authorization": "Bearer not-the-automation-token"},
        json=_envelope(workspace_id),
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthenticated"
    assert len(store.calls) == 1


def test_cli_client_config_requires_a_protected_bearer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "good"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    workspace_id, owner_id = uuid4(), uuid4()
    bearer = tmp_path / "automation.json"
    bearer.write_text(json.dumps({"token": "automation-token"}))
    bearer.chmod(0o600)

    assert (
        main(
            [
                "ops",
                "capabilities",
                "client-config",
                "--workspace-id",
                str(workspace_id),
                "--owner-id",
                str(owner_id),
                "--bearer-file",
                str(bearer),
            ]
        )
        == 0
    )
    client_json = xdg / "omp-work" / "client.json"
    data = json.loads(client_json.read_text())
    assert data["bearer_file"] == str(bearer)
    assert data["workspace_id"] == str(workspace_id)
    assert client_json.stat().st_mode & 0o777 == 0o600

    exposed = tmp_path / "exposed.json"
    exposed.write_text(json.dumps({"token": "automation-token"}))
    exposed.chmod(0o644)
    bad_xdg = tmp_path / "bad"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bad_xdg))
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "ops",
                "capabilities",
                "client-config",
                "--workspace-id",
                str(workspace_id),
                "--owner-id",
                str(owner_id),
                "--bearer-file",
                str(exposed),
            ]
        )
    assert exc.value.code != 0
    assert not (bad_xdg / "omp-work" / "client.json").exists()
