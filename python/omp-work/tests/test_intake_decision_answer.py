"""OMP-407: PostgreSQL integration test suite for answering bot-filed intake decisions."""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import datetime
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
    root = tmp_path_factory.mktemp("intake-decision-answer-suite")
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


def test_intake_decision_answer_flow(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    _register_token(
        service,
        "agent-mutate-token",
        [workspace_id],
        ["work.read", "work.mutate"],
        actor_kind="agent",
    )
    _register_token(
        service,
        "agent-approve-token",
        [workspace_id],
        ["work.read", "work.mutate", "work.approve"],
        actor_kind="agent",
    )

    # 1. Agent files item -> held with pending decision in tree read
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "bot-task-1",
                        "title": "Autonomous agent work",
                        "description": "Filed by agent",
                    }
                ],
                "relations": [],
            },
        },
        token="agent-mutate-token",
    )
    assert status == 200, body
    item_row = body["result"]["items"][0]
    agent_id = UUID(str(item_row["work_id"]))
    agent_key = item_row["key"]
    agent_title = "Autonomous agent work"

    tree_res = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    )
    assert tree_res.status_code == 200
    tree_items = {UUID(str(it["work_id"])): it for it in tree_res.json()["items"]}
    agent_tree = tree_items[agent_id]
    assert agent_tree["scope_class"] == ScopeClass.new_scope
    assert agent_tree["intake_hold"] is True
    assert agent_tree["intake_decision"] == {
        "question": f"Approve intake of bot-filed new scope {agent_key}: {agent_title}?",
        "options": ["approve"],
        "state": "pending",
        "answered_at": None,
    }

    # 2. Agent capability holding work.approve gets 403
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(agent_id),
                "answer": "approve",
            },
        },
        token="agent-approve-token",
    )
    assert status == 403, body
    assert body["error"]["code"] == "forbidden"

    # 3. Owner POST releases hold -> tree intake_hold false, decision state approved
    op_id_1 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(agent_id),
                "answer": "approve",
            },
        },
        token="owner-token",
        operation_id=op_id_1,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    first_result = body["result"]
    assert first_result["type"] == "answer_intake_decision"
    assert first_result["work_id"] == str(agent_id)
    assert first_result["answer"] == "approve"
    answered_at_1 = first_result["answered_at"]
    assert answered_at_1 is not None

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT answer, answered_by, operation_id FROM omp_work.intake_decisions WHERE workspace_id=%s AND work_id=%s",
                (workspace_id, agent_id),
            )
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0]["answer"] == "approve"
            assert rows[0]["answered_by"] == "owner"
            assert rows[0]["operation_id"] == op_id_1

    tree_res = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    )
    assert tree_res.status_code == 200
    tree_items = {UUID(str(it["work_id"])): it for it in tree_res.json()["items"]}
    agent_tree = tree_items[agent_id]
    assert agent_tree["scope_class"] == ScopeClass.new_scope
    assert agent_tree["intake_hold"] is False
    assert agent_tree["intake_decision"]["state"] == "approved"
    assert agent_tree["intake_decision"]["question"] == f"Approve intake of bot-filed new scope {agent_key}: {agent_title}?"
    assert agent_tree["intake_decision"]["options"] == ["approve"]
    assert datetime.fromisoformat(agent_tree["intake_decision"]["answered_at"]) == datetime.fromisoformat(answered_at_1)

    # 4. Replay with the same operation_id -> receipt.state "replayed", result equal to first result, 1 row
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(agent_id),
                "answer": "approve",
            },
        },
        token="owner-token",
        operation_id=op_id_1,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "replayed"
    assert body["result"] == first_result

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM omp_work.intake_decisions WHERE workspace_id=%s AND work_id=%s",
                (workspace_id, agent_id),
            )
            assert cur.fetchone()["count"] == 1

    # 5. A new operation_id -> same answered_at, one row
    op_id_2 = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(agent_id),
                "answer": "approve",
            },
        },
        token="owner-token",
        operation_id=op_id_2,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["answered_at"] == answered_at_1

    with _app_connection(service, workspace_id) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM omp_work.intake_decisions WHERE workspace_id=%s AND work_id=%s",
                (workspace_id, agent_id),
            )
            assert cur.fetchone()["count"] == 1


def test_intake_decision_refusals(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    _register_token(
        service,
        "agent-token",
        [workspace_id],
        ["work.read", "work.mutate"],
        actor_kind="agent",
    )

    # 1. Owner-filed item -> owner_filed, intake_decision is null; answering -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "owner-task-1",
                        "title": "Owner human task",
                        "description": "Filed by owner",
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

    tree_res = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    )
    assert tree_res.status_code == 200
    tree_items = {UUID(str(it["work_id"])): it for it in tree_res.json()["items"]}
    assert tree_items[owner_id]["scope_class"] == ScopeClass.owner_filed
    assert tree_items[owner_id]["intake_decision"] is None

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(owner_id),
                "answer": "approve",
            },
        },
        token="owner-token",
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 2. Follow-up item -> follow_up, intake_decision is null; answering -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "follow-up-task-1",
                        "title": "Agent follow up",
                        "description": "Filed by agent",
                    }
                ],
                "relations": [],
            },
        },
        token="agent-token",
    )
    assert status == 200, body
    followup_item = body["result"]["items"][0]
    followup_id = UUID(str(followup_item["work_id"]))

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": str(followup_id),
                    "target_work_id": str(owner_id),
                    "kind": "parent",
                    "active": True,
                }
            },
        },
        token="owner-token",
    )
    assert status == 200, body

    tree_res = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree",
        headers=_headers(workspace_id),
    )
    assert tree_res.status_code == 200
    tree_items = {UUID(str(it["work_id"])): it for it in tree_res.json()["items"]}
    assert tree_items[followup_id]["scope_class"] == ScopeClass.follow_up
    assert tree_items[followup_id]["intake_hold"] is False
    assert tree_items[followup_id]["intake_decision"] is None

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(followup_id),
                "answer": "approve",
            },
        },
        token="owner-token",
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    # 3. Unknown work_id -> invalid_request
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_intake_decision",
            "payload": {
                "work_id": str(uuid4()),
                "answer": "approve",
            },
        },
        token="owner-token",
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
