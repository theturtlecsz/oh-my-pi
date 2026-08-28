from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority
from omp_work.operations.database import bootstrap
from omp_work.v1.models import (
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
)
from omp_work.v1.server import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
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
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=port,
    )


def test_loopback_service_replays_real_postgres_commands_and_enforces_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        bootstrap(config)
        workspace_id, actor_id, operation_id = uuid4(), uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)
        (capabilities / "reader.json").write_text(
            json.dumps(
                {
                    "token": "reader-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "task-agent",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.candidate.read"],
                    "candidate_ids": [str(uuid4())],
                }
            )
        )
        (capabilities / "reader.json").chmod(0o600)
        client = TestClient(create_app(config, capabilities_dir=capabilities))
        command = CreateWorkBatchCommand(
            type="create_work_batch",
            payload=CreateWorkBatchPayload(
                items=(
                    CreateWorkInput(client_ref="a", title="first"),
                    CreateWorkInput(client_ref="b", title="second"),
                )
            ),
        )
        envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=operation_id,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=command,
        )
        headers = {
            "Authorization": "Bearer owner-token",
            "X-OMP-Workspace-ID": str(workspace_id),
            "X-OMP-Contract-SHA256": contract_sha256(),
        }
        created = client.post(
            "/v1/commands", headers=headers, json=envelope.model_dump(mode="json")
        )
        assert created.status_code == 200
        replay = client.post(
            "/v1/commands",
            headers=headers,
            json=envelope.model_copy(update={"request_id": uuid4()}).model_dump(
                mode="json"
            ),
        )
        assert (
            replay.status_code == 200
            and replay.json()["receipt"]["state"] == "replayed"
        )
        key = created.json()["result"]["items"][0]["key"]
        assert client.get(f"/v1/work-items/{key}", headers=headers).status_code == 200
        assert (
            client.post(
                "/v1/commands",
                headers={
                    "Authorization": "Bearer reader-token",
                    "X-OMP-Contract-SHA256": contract_sha256(),
                },
                json=envelope.model_dump(mode="json"),
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/v1/work-items/{key}",
                headers={
                    "Authorization": "Bearer owner-token",
                    "X-OMP-Workspace-ID": str(uuid4()),
                    "X-OMP-Contract-SHA256": contract_sha256(),
                },
            ).status_code
            == 403
        )
