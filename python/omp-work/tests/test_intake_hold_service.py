"""OMP-407: PostgreSQL integration test suite for bot-filed intake hold and tree classifications."""

from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.intake_hold import ScopeClass
from omp_work.v1.server import create_app
from omp_work.v1.store import _intake_classifications
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

OWNER = uuid4()


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
def service(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("intake-hold-service-suite")
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


def _grant(service, workspace_id: UUID) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    if str(workspace_id) not in data["workspaces"]:
        data["workspaces"].append(str(workspace_id))
        owner.write_text(json.dumps(data))
        owner.chmod(0o600)


def _register_token(
    service,
    token: str,
    workspace_ids: list[UUID],
    scopes: list[str],
    *,
    actor_kind: str = "agent",
) -> None:
    cap_file = service.capabilities / f"{token}.json"
    cap_file.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(uuid4()),
                "actor_kind": actor_kind,
                "workspaces": [str(ws) for ws in workspace_ids],
                "scopes": scopes,
            }
        )
    )
    cap_file.chmod(0o600)


def _headers(workspace_id: UUID, token: str = "owner-token") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _command(
    service,
    workspace_id: UUID,
    command: dict,
    *,
    token: str = "owner-token",
    operation_id: UUID | None = None,
) -> tuple[int, dict]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    response = service.client.post(
        "/v1/commands",
        headers=_headers(workspace_id, token),
        json=envelope,
    )
    return response.status_code, response.json()


def _app_connection(service, workspace_id: UUID):
    conn = psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    )
    conn.execute(
        "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
        (str(workspace_id), str(OWNER)),
    )
    return conn


def test_intake_hold_classification_and_tree_lifecycle(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    # 1. Register agent capability and create agent-filed item -> new_scope, held
    _register_token(
        service,
        "agent-token",
        [workspace_id],
        ["work.read", "work.mutate"],
        actor_kind="agent",
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "agent-task-1",
                        "title": "Agent filed item",
                        "description": "Filed by agent",
                    }
                ],
                "relations": [],
            },
        },
        token="agent-token",
    )
    assert status == 200, body
    agent_item = body["result"]["items"][0]
    agent_id = UUID(str(agent_item["work_id"]))

    # 2. Owner item -> owner_filed, not held
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "owner-task-1",
                        "title": "Owner filed item",
                        "description": "Filed by human owner",
                    }
                ],
                "relations": [],
            },
        },
        token="owner-token",
    )
    assert status == 200, body
    owner_item = body["result"]["items"][0]
    owner_id = UUID(str(owner_item["work_id"]))

    # Read tree and verify initial states
    tree_res = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    )
    assert tree_res.status_code == 200
    tree = tree_res.json()
    items_by_id = {UUID(str(it["work_id"])): it for it in tree["items"]}

    assert items_by_id[agent_id]["scope_class"] == ScopeClass.new_scope
    assert items_by_id[agent_id]["intake_hold"] is True

    assert items_by_id[owner_id]["scope_class"] == ScopeClass.owner_filed
    assert items_by_id[owner_id]["intake_hold"] is False

    # Check _intake_classifications for one id equals its tree fields
    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            classifications = _intake_classifications(cur, workspace_id, [agent_id])
            assert len(classifications) == 1
            assert classifications[agent_id].scope_class == items_by_id[agent_id]["scope_class"]
            assert classifications[agent_id].held == items_by_id[agent_id]["intake_hold"]

            owner_classifications = _intake_classifications(cur, workspace_id, [owner_id])
            assert len(owner_classifications) == 1
            assert owner_classifications[owner_id].scope_class == items_by_id[owner_id]["scope_class"]
            assert owner_classifications[owner_id].held == items_by_id[owner_id]["intake_hold"]

    # 3. Agent item after a parent edge to an owner item -> follow_up, not held
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": str(agent_id),
                    "target_work_id": str(owner_id),
                    "kind": "parent",
                    "active": True,
                }
            },
        },
        token="owner-token",
    )
    assert status == 200, body

    tree = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    ).json()
    items_by_id = {UUID(str(it["work_id"])): it for it in tree["items"]}

    assert items_by_id[agent_id]["scope_class"] == ScopeClass.follow_up
    assert items_by_id[agent_id]["intake_hold"] is False

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            classifications = _intake_classifications(cur, workspace_id, [agent_id])
            assert classifications[agent_id].scope_class == items_by_id[agent_id]["scope_class"]
            assert classifications[agent_id].held == items_by_id[agent_id]["intake_hold"]

    # 4. Owner-key "[flood-created] ..." item -> held
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "flood-task-1",
                        "title": "Flood task",
                        "description": "  [flood-created] automated bot work item",
                    }
                ],
                "relations": [],
            },
        },
        token="owner-token",
    )
    assert status == 200, body
    flood_item = body["result"]["items"][0]
    flood_id = UUID(str(flood_item["work_id"]))

    tree = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    ).json()
    items_by_id = {UUID(str(it["work_id"])): it for it in tree["items"]}

    assert items_by_id[flood_id]["scope_class"] == ScopeClass.new_scope
    assert items_by_id[flood_id]["intake_hold"] is True

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            classifications = _intake_classifications(cur, workspace_id, [flood_id])
            assert classifications[flood_id].scope_class == items_by_id[flood_id]["scope_class"]
            assert classifications[flood_id].held == items_by_id[flood_id]["intake_hold"]

    # 5. Seeded intake_decisions row clears the hold
    op_id = uuid4()
    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO omp_work.intake_decisions (workspace_id, work_id, answer, answered_by, operation_id) VALUES (%s, %s, 'approve', 'owner', %s)",
                (workspace_id, flood_id, op_id),
            )

    tree = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    ).json()
    items_by_id = {UUID(str(it["work_id"])): it for it in tree["items"]}

    assert items_by_id[flood_id]["scope_class"] == ScopeClass.new_scope
    assert items_by_id[flood_id]["intake_hold"] is False

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            classifications = _intake_classifications(cur, workspace_id, [flood_id])
            assert classifications[flood_id].scope_class == items_by_id[flood_id]["scope_class"]
            assert classifications[flood_id].held == items_by_id[flood_id]["intake_hold"]
