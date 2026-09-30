"""OMP-413-s06: approve_mission and set_mission_status store transitions."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import psycopg
import pytest

from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
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
        "objective": "Status transition mission",
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    draft.update(overrides)
    return draft


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


def test_mission_status_lifecycle_and_causes(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()

    # 1. Budgeted mission
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id),
            },
        },
    )
    assert status == 200, body
    assert _events(service, workspace_id, mission_id) == 1

    # 2. PostgresWorkStore(service.config).execute approve by "automation": approval_required, no event
    store = PostgresWorkStore(service.config)
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {
                "type": "approve_mission",
                "payload": {
                    "mission_id": str(mission_id),
                    "revision": 1,
                    "basis_kind": "decision",
                    "basis_id": str(uuid4()),
                },
            },
        }
    )
    with pytest.raises(WorkStoreError) as exc_info:
        store.execute(
            envelope,
            actor_id=uuid4(),
            actor_kind="automation",
            required_scope="work.approve",
        )
    assert exc_info.value.code == "approval_required"
    assert _events(service, workspace_id, mission_id) == 1

    # 3. Owner approve over HTTP: approved, basis, envelope
    basis_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": basis_id,
            },
        },
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["type"] == "approve_mission"
    mission = body["result"]["mission"]
    assert mission["status"] == "approved"
    assert mission["approved_scope"] is not None
    assert mission["approved_scope"]["revision"] == 1
    assert mission["approved_scope"]["basis_kind"] == "decision"
    assert mission["approved_scope"]["basis_id"] == basis_id
    assert mission["approved_scope"]["approved_by"] == str(OWNER)
    assert mission["approved_scope"]["approved_by_actor_kind"] == "owner"
    assert mission["approved_scope"]["envelope"]["budget_policy"] == _BUDGET
    assert mission["approved_scope"]["envelope"]["project_id"] == str(project_id)
    assert _events(service, workspace_id, mission_id) == 2

    # 4. Recorded causes: running (principal), paused (policy_rule), running, completed (decision)
    # running (principal)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "running"
    t1 = mission["transitions"][-1]
    assert t1["from_status"] == "approved"
    assert t1["to_status"] == "running"
    assert t1["cause_kind"] == "principal"
    assert t1["cause_id"] == str(OWNER)
    assert t1["actor_id"] == str(OWNER)
    assert _events(service, workspace_id, mission_id) == 3

    # paused (policy_rule)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "paused",
                "cause_kind": "policy_rule",
                "policy_rule_id": "rule.pause_review",
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "paused"
    t2 = mission["transitions"][-1]
    assert t2["from_status"] == "running"
    assert t2["to_status"] == "paused"
    assert t2["cause_kind"] == "policy_rule"
    assert t2["cause_id"] == "rule.pause_review"
    assert _events(service, workspace_id, mission_id) == 4

    # running
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "running"
    t3 = mission["transitions"][-1]
    assert t3["from_status"] == "paused"
    assert t3["to_status"] == "running"
    assert t3["cause_kind"] == "principal"
    assert t3["cause_id"] == str(OWNER)
    assert _events(service, workspace_id, mission_id) == 5

    # completed (decision)
    decision_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "completed",
                "cause_kind": "decision",
                "decision_id": str(decision_id),
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "completed"
    t4 = mission["transitions"][-1]
    assert t4["from_status"] == "running"
    assert t4["to_status"] == "completed"
    assert t4["cause_kind"] == "decision"
    assert t4["cause_id"] == str(decision_id)
    assert _events(service, workspace_id, mission_id) == 6


def test_transition_refusals_and_budget_hold(service) -> None:
    workspace_id, project_id = _project(service)

    # 1. completed->running refused (409 mission_transition_refused, no transition)
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id),
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "completed",
                "cause_kind": "decision",
                "decision_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body

    before_events = _events(service, workspace_id, mission_id)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    read_completed = _get(service.client, workspace_id, mission_id).json()
    assert read_completed["status"] == "completed"
    assert len(read_completed["transitions"]) == 4
    assert _events(service, workspace_id, mission_id) == before_events

    # 2. awaiting_confirmation->running refused (409 mission_transition_refused, no transition)
    unapproved_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(unapproved_id),
                "draft": _draft(project_id, objective="Unapproved mission"),
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(unapproved_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    read_unapproved = _get(service.client, workspace_id, unapproved_id).json()
    assert read_unapproved["status"] == "awaiting_confirmation"
    assert len(read_unapproved["transitions"]) == 1
    assert _events(service, workspace_id, unapproved_id) == 1

    # 3. Budget hold tests: held->awaiting_confirmation, ->running, ->abandoned by principal
    workspace_held, project_held = _project(service, {})
    held_id = uuid4()
    status, body = _command(
        service,
        workspace_held,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(held_id),
                "draft": _draft(project_held, budget_policy=None),
            },
        },
    )
    assert status == 200, body
    held_mission = body["result"]["mission"]
    assert held_mission["status"] == "blocked"
    assert held_mission["budget"] is None
    assert held_mission["hold_decision"] is not None
    hold_decision_id = held_mission["hold_decision"]["decision_id"]
    assert _events(service, workspace_held, held_id) == 1

    # held->awaiting_confirmation (approve_mission refused on budget hold)
    status, body = _command(
        service,
        workspace_held,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(held_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    assert _events(service, workspace_held, held_id) == 1

    # held->running refused
    status, body = _command(
        service,
        workspace_held,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(held_id),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    assert _events(service, workspace_held, held_id) == 1

    # held->abandoned by principal refused
    status, body = _command(
        service,
        workspace_held,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(held_id),
                "target_status": "abandoned",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    read_held = _get(service.client, workspace_held, held_id).json()
    assert read_held["status"] == "blocked"
    assert len(read_held["transitions"]) == 1
    assert _events(service, workspace_held, held_id) == 1

    # Held->abandoned, hold decision cause: accepted
    status, body = _command(
        service,
        workspace_held,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(held_id),
                "target_status": "abandoned",
                "cause_kind": "decision",
                "decision_id": str(hold_decision_id),
            },
        },
    )
    assert status == 200, body
    abandoned_mission = body["result"]["mission"]
    assert abandoned_mission["status"] == "abandoned"
    t_abandoned = abandoned_mission["transitions"][-1]
    assert t_abandoned["from_status"] == "blocked"
    assert t_abandoned["to_status"] == "abandoned"
    assert t_abandoned["cause_kind"] == "decision"
    assert t_abandoned["cause_id"] == str(hold_decision_id)
    assert _events(service, workspace_held, held_id) == 2


def test_standing_mandate_approval_and_revision_conflict(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id),
            },
        },
    )
    assert status == 200, body

    # Revision conflict
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 2,
                "basis_kind": "standing_mandate",
                "basis_id": "mandate-418",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "revision_conflict"

    # Standing mandate approval
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "standing_mandate",
                "basis_id": "mandate-418",
            },
        },
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "approved"
    assert mission["approved_scope"]["basis_kind"] == "standing_mandate"
    assert mission["approved_scope"]["basis_id"] == "mandate-418"
    transition = mission["transitions"][-1]
    assert transition["cause_kind"] == "policy_rule"
    assert transition["cause_id"] == "standing_mandate:mandate-418"

    # Cannot approve again (needs awaiting_confirmation)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "standing_mandate",
                "basis_id": "mandate-418",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
