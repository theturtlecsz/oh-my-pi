from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import CommandResponse
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

OWNER = uuid4()


def test_no_postgres_store_exception_unavailable(tmp_path: Path) -> None:
    capabilities_dir = tmp_path / "capabilities"
    capabilities_dir.mkdir(mode=0o700)
    owner = capabilities_dir / "owner.json"
    owner_id = uuid4()
    workspace_id = uuid4()
    owner.write_text(
        json.dumps(
            {
                "token": "owner-token",
                "actor_id": str(owner_id),
                "actor_kind": "owner",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.mutate"],
            }
        )
    )
    owner.chmod(0o600)

    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=8000,
    )

    class FakeStore:
        def execute(self, envelope, *, actor_id, actor_kind, required_scope):
            raise RuntimeError("simulated store failure")

    app = create_app(config, capabilities_dir=capabilities_dir, store=FakeStore())
    client = TestClient(app)

    request_id = uuid4()
    correlation_id = uuid4()
    op_id = uuid4()
    headers = {
        "Authorization": "Bearer owner-token",
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(op_id),
        "request_id": str(request_id),
        "correlation_id": str(correlation_id),
        "command": {
            "type": "create_work_batch",
            "payload": {
                "items": [{"client_ref": "root", "title": "test item"}],
                "relations": [],
            },
        },
    }

    resp = client.post("/v1/commands", headers=headers, json=envelope)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "unavailable"
    assert body["error"]["request_id"] == str(request_id)
    assert body["error"]["correlation_id"] == str(correlation_id)
    assert any(
        "RuntimeError: simulated store failure" in d
        for d in body["error"]["diagnostics"]
    )
    assert any(
        "outcome unknown, reconcile by operation_id" in d
        for d in body["error"]["diagnostics"]
    )

    bad_resp = client.post("/v1/commands", headers=headers, json={"malformed": "body"})
    assert bad_resp.status_code == 400
    bad_body = bad_resp.json()
    assert bad_body["error"]["code"] == "invalid_request"


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


@pytest.fixture(scope="module")
def postgres_service(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("post-commit-service")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()
        capabilities = root / "capabilities"
        capabilities.mkdir(mode=0o700)
        owner = capabilities / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(OWNER),
                    "actor_kind": "owner",
                    "workspaces": [],
                    "scopes": [
                        "work.read",
                        "work.mutate",
                        "work.approve",
                        "work.close",
                        "work.execute",
                    ],
                }
            )
        )
        owner.chmod(0o600)
        yield SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )


def _grant(service, workspace_id) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    if str(workspace_id) not in data["workspaces"]:
        data["workspaces"].append(str(workspace_id))
        owner.write_text(json.dumps(data))
        owner.chmod(0o600)


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_postgres_post_commit_validation_failure_reconciliation(
    postgres_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = uuid4()
    _grant(postgres_service, workspace_id)

    orig_validate = CommandResponse.model_validate
    raised = False

    def failing_validate(*args, **kwargs):
        nonlocal raised
        if not raised:
            raised = True
            raise ValueError("simulated post-commit response validation error")
        return orig_validate(*args, **kwargs)

    monkeypatch.setattr(
        "omp_work.v1.server.CommandResponse.model_validate", failing_validate
    )

    operation_id = uuid4()
    request_id = uuid4()
    correlation_id = uuid4()
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id),
        "request_id": str(request_id),
        "correlation_id": str(correlation_id),
        "command": {
            "type": "create_work_batch",
            "payload": {
                "items": [{"client_ref": "root", "title": "Post-Commit Item"}],
                "relations": [],
            },
        },
    }
    headers = {
        "Authorization": "Bearer owner-token",
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    # 1. POST a real create_work_batch and expect 503
    resp1 = postgres_service.client.post("/v1/commands", headers=headers, json=envelope)
    assert resp1.status_code == 503
    body1 = resp1.json()
    assert body1["error"]["code"] == "unavailable"
    assert body1["error"]["request_id"] == str(request_id)
    assert body1["error"]["correlation_id"] == str(correlation_id)
    assert any(
        "ValueError: simulated post-commit response validation error" in d
        for d in body1["error"]["diagnostics"]
    )
    assert any(
        "outcome unknown, reconcile by operation_id" in d
        for d in body1["error"]["diagnostics"]
    )

    # 2. GET /v1/operations/{op} shows it applied, with a result
    op_resp = postgres_service.client.get(
        f"/v1/operations/{operation_id}",
        headers=headers | {"X-OMP-Workspace-ID": str(workspace_id)},
    )
    assert op_resp.status_code == 200
    op_body = op_resp.json()
    assert op_body["receipt"]["state"] == "applied"
    assert op_body["result"] is not None
    assert "items" in op_body["result"]
    assert len(op_body["result"]["items"]) == 1
    result_sha256 = op_body["receipt"]["result_sha256"]
    assert result_sha256

    # 3. Re-POST the same envelope and expect 200 replayed with the same result_sha256
    resp2 = postgres_service.client.post("/v1/commands", headers=headers, json=envelope)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["receipt"]["state"] == "replayed"
    assert body2["receipt"]["result_sha256"] == result_sha256

    # 4. Exactly one item exists
    items_resp = postgres_service.client.get(
        f"/v1/workspaces/{workspace_id}/work-items",
        headers=headers | {"X-OMP-Workspace-ID": str(workspace_id)},
    )
    assert items_resp.status_code == 200
    items_body = items_resp.json()
    assert len(items_body["items"]) == 1
    assert items_body["items"][0]["key"] == "OMP-1"
