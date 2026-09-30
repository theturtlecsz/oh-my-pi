"""OMP-413-s05: submit_mission persists as a domain event and the mission read returns it."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_work.v1.server import create_app
from test_workflow_service import OWNER, _command, _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = {
    "usd": "25.50",
    "tokens": 5000,
    "wall_clock_seconds": 1200,
    "max_subagents": 2,
}
_STANDING = {
    "usd": "8",
    "tokens": 800,
    "wall_clock_seconds": 90,
    "max_subagents": 1,
}


def _project(service, provenance: dict | None = None):
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Mission project",
                json.dumps(provenance or {}),
            ),
        )
    return workspace_id, project_id


def _draft(project_id, **overrides) -> dict:
    draft = {
        "project_id": str(project_id),
        "objective": "Keep the parent mission",
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    draft.update(overrides)
    return draft


def _envelope(workspace_id, mission_id, draft, operation_id=None) -> dict:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {
            "type": "submit_mission",
            "payload": {"mission_id": str(mission_id), "draft": draft},
        },
    }


def _post(client, workspace_id, envelope) -> tuple[int, dict]:
    response = client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id),
        json=envelope,
    )
    return response.status_code, response.json()


def _get(client, workspace_id, mission_id):
    return client.get(
        f"/v1/workspaces/{workspace_id}/missions/{mission_id}",
        headers=_owner_headers(workspace_id),
    )


def _events(service, workspace_id, mission_id) -> int:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND aggregate_id=%s AND aggregate_type='mission'",
            (workspace_id, mission_id),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_submit_every_field_replays_and_survives_restart(service) -> None:
    workspace_id, project_id = _project(service)
    parent_id = uuid4()
    continuation_id = uuid4()
    for mission_id, objective in (
        (parent_id, "Parent objective"),
        (continuation_id, "Continuation objective"),
    ):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "submit_mission",
                "payload": {
                    "mission_id": str(mission_id),
                    "draft": _draft(project_id, objective=objective),
                },
            },
        )
        assert status == 200, body

    mission_id = uuid4()
    draft = {
        "project_id": str(project_id),
        "objective": "Ship the mission record",
        "constraints": ["no migration", "events only"],
        "acceptance_criteria": ["persisted", "replayed"],
        "context_refs": ["doc:d29"],
        "artifact_expectations": ["mission-view"],
        "requested_capabilities": ["cap.work"],
        "repositories": ["oh-my-pi"],
        "approval_classes": ["broaden_scope"],
        "risk_policy": "risk-policy-full",
        "approval_policy": "approval-policy-full",
        "effort_policy": "effort-policy-full",
        "budget_policy": _BUDGET,
        "priority": 1,
        "continuation_of": str(continuation_id),
        "parent_mission": str(parent_id),
    }
    envelope = _envelope(workspace_id, mission_id, draft)
    status, body = _post(service.client, workspace_id, envelope)
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    mission = body["result"]["mission"]
    assert body["result"]["type"] == "submit_mission"
    for key, value in draft.items():
        assert mission[key] == value
    assert mission["mission_id"] == str(mission_id)
    assert mission["created_by"] == str(OWNER)
    assert mission["revision"] == 1
    assert mission["status"] == "awaiting_confirmation"
    assert mission["budget"] == _BUDGET
    assert mission["budget_source"] == "mission"
    assert mission["hold_decision"] is None
    assert mission["approved_scope"] is None
    assert mission["links"] == []
    assert mission["drawn"] == {"usd": "0", "tokens": 0, "wall_clock_seconds": 0}
    assert mission["transitions"] == [
        {
            "from_status": "draft",
            "to_status": "awaiting_confirmation",
            "cause_kind": "policy_rule",
            "cause_id": "D29.new_scope",
            "actor_id": str(OWNER),
            "actor_kind": "owner",
            "at": mission["created_at"],
            "revision": 1,
        }
    ]
    assert mission["created_at"]

    read = _get(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    assert read.json() == mission

    replay_status, replay = _post(service.client, workspace_id, envelope)
    assert replay_status == 200, replay
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == body["result"]
    assert {k: v for k, v in replay["receipt"].items() if k != "state"} == {
        k: v for k, v in body["receipt"].items() if k != "state"
    }
    assert _events(service, workspace_id, mission_id) == 1

    conflict_status, conflict = _post(
        service.client, workspace_id, _envelope(workspace_id, mission_id, draft)
    )
    assert conflict_status == 409, conflict
    assert conflict["error"]["code"] == "revision_conflict"
    assert _events(service, workspace_id, mission_id) == 1

    restarted = TestClient(
        create_app(service.config, capabilities_dir=service.capabilities)
    )
    again = _get(restarted, workspace_id, mission_id)
    assert again.status_code == 200, again.text
    assert again.json() == mission


def test_standing_budget_is_inherited_from_project_provenance(service) -> None:
    workspace_id, project_id = _project(service, {"standing_budget": _STANDING})
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, budget_policy=None),
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["budget_policy"] is None
    assert mission["budget"] == _STANDING
    assert mission["budget_source"] == "project"
    assert mission["status"] == "awaiting_confirmation"
    assert mission["hold_decision"] is None
    assert _get(service.client, workspace_id, mission_id).json()["budget"] == _STANDING


def test_no_budget_blocks_with_hold_decision(service) -> None:
    workspace_id, project_id = _project(service, {})
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, budget_policy=None),
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "blocked"
    assert mission["budget"] is None
    assert mission["budget_source"] is None
    blocked = next(
        transition
        for transition in mission["transitions"]
        if transition["from_status"] == "draft" and transition["to_status"] == "blocked"
    )
    assert blocked["cause_kind"] == "decision"
    assert mission["hold_decision"]["decision_id"] == blocked["cause_id"]
    assert isinstance(mission["hold_decision"]["decision_id"], str)
    read = _get(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    assert read.json() == mission


def test_unknown_project_or_mission_link_is_invalid(service) -> None:
    workspace_id, project_id = _project(service)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(uuid4()),
                "draft": _draft(uuid4()),
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(uuid4()),
                "draft": _draft(project_id, parent_mission=str(uuid4())),
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    missing = _get(service.client, workspace_id, uuid4())
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"
