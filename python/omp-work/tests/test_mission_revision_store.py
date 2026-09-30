"""OMP-413-s07: revise_mission replaces the draft and applies the D29 rule."""

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
    "usd": "10.00",
    "tokens": 1000,
    "wall_clock_seconds": 3600,
    "max_subagents": 4,
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
        "objective": "Ship the widget",
        "constraints": ["keep the public API"],
        "acceptance_criteria": ["criterion one", "criterion two"],
        "repositories": ["repo-a", "repo-b"],
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
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


def _submit(service, workspace_id, project_id, **overrides):
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, **overrides),
            },
        },
    )
    assert status == 200, body
    return mission_id, body["result"]["mission"]


def _approve(service, workspace_id, mission_id, revision: int):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": revision,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def _revise(service, workspace_id, mission_id, base_revision: int, draft: dict, **payload):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "revise_mission",
            "payload": {
                "mission_id": str(mission_id),
                "base_revision": base_revision,
                "draft": draft,
                **payload,
            },
        },
    )
    return status, body


def _approved(service, **overrides):
    workspace_id, project_id = _project(service)
    mission_id, _submitted = _submit(service, workspace_id, project_id, **overrides)
    approved = _approve(service, workspace_id, mission_id, 1)
    assert approved["status"] == "approved"
    assert approved["revision"] == 1
    assert _events(service, workspace_id, mission_id) == 2
    return workspace_id, project_id, mission_id, approved


def test_in_scope_revision_stays_approved(service) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)
    lower = {**_BUDGET, "usd": "9.00"}
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(
            project_id,
            constraints=["keep the public API, reworded"],
            repositories=["repo-a"],
            budget_policy=lower,
        ),
        proposed_classification="material",
    )
    assert status == 200, body
    assert body["result"]["type"] == "revise_mission"
    mission = body["result"]["mission"]
    assert mission["status"] == "approved"
    assert mission["revision"] == 2
    assert mission["transitions"] == approved["transitions"]
    assert mission["approved_scope"] == approved["approved_scope"]
    assert mission["constraints"] == ["keep the public API, reworded"]
    assert mission["repositories"] == ["repo-a"]
    assert mission["budget_policy"] == lower
    assert mission["budget"] == lower
    assert mission["budget_source"] == "mission"
    assert mission["hold_decision"] is None
    assert mission["created_at"] == approved["created_at"]
    assert "proposed_classification" not in mission
    assert _events(service, workspace_id, mission_id) == 3
    read = _get(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    assert read.json() == mission


@pytest.mark.parametrize(
    ("code", "overrides"),
    [
        ("a", {"acceptance_criteria": ["criterion one", "criterion two", "criterion three"]}),
        ("b", {"acceptance_criteria": ["criterion two"]}),
        ("c", {"repositories": ["repo-a", "repo-b", "repo-c"]}),
        ("d", {"requested_capabilities": ["read", "test", "write"]}),
        ("e", {"budget_policy": {**_BUDGET, "usd": "10.01"}}),
        ("f", {"objective": "Ship a different widget"}),
    ],
)
def test_each_material_case_awaits_confirmation(service, code: str, overrides: dict) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, **overrides),
        proposed_classification="not_material",
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["revision"] == 2
    assert mission["approved_scope"] == approved["approved_scope"]
    assert mission["transitions"][:-1] == approved["transitions"]
    transition = mission["transitions"][-1]
    assert transition["from_status"] == "approved"
    assert transition["to_status"] == "awaiting_confirmation"
    assert transition["cause_kind"] == "policy_rule"
    assert transition["cause_id"] == f"D29.material:{code}"
    assert transition["revision"] == 2
    assert _events(service, workspace_id, mission_id) == 3


def test_new_repository_needs_owner_approval(service) -> None:
    workspace_id, project_id, mission_id, _approved_mission = _approved(service)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, repositories=["repo-a", "repo-b", "repo-new"]),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["transitions"][-1]["cause_id"] == "D29.material:c"
    assert _events(service, workspace_id, mission_id) == 3

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
                    "revision": 2,
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
    assert _events(service, workspace_id, mission_id) == 3

    approved = _approve(service, workspace_id, mission_id, 2)
    assert approved["status"] == "approved"
    assert approved["revision"] == 2
    assert approved["approved_scope"]["revision"] == 2
    assert approved["approved_scope"]["approved_by_actor_kind"] == "owner"
    assert "repo-new" in approved["approved_scope"]["envelope"]["repositories"]
    assert _events(service, workspace_id, mission_id) == 4


def test_held_mission_leaves_hold_when_budget_is_supplied(service) -> None:
    workspace_id, project_id = _project(service, {})
    mission_id, held = _submit(service, workspace_id, project_id, budget_policy=None)
    assert held["status"] == "blocked"
    assert held["budget"] is None
    assert held["hold_decision"] is not None

    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, budget_policy=_BUDGET),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["revision"] == 2
    assert mission["hold_decision"] is None
    assert mission["budget"] == _BUDGET
    assert mission["budget_source"] == "mission"
    transition = mission["transitions"][-1]
    assert transition["from_status"] == "blocked"
    assert transition["to_status"] == "awaiting_confirmation"
    assert transition["cause_kind"] == "policy_rule"
    assert transition["cause_id"] == "D29.budget_supplied"
    assert len(mission["transitions"]) == 2

    approved = _approve(service, workspace_id, mission_id, 2)
    assert approved["status"] == "approved"
    assert approved["hold_decision"] is None
    assert approved["budget"] == _BUDGET


def test_removing_the_budget_holds_unless_already_blocked(service) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, budget_policy=None),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "blocked"
    assert mission["revision"] == 2
    assert mission["budget"] is None
    assert mission["budget_source"] is None
    assert mission["budget_policy"] is None
    assert mission["approved_scope"] == approved["approved_scope"]
    assert mission["hold_decision"]["decision_id"]
    transition = mission["transitions"][-1]
    assert transition["from_status"] == "approved"
    assert transition["to_status"] == "blocked"
    assert transition["cause_kind"] == "decision"
    assert transition["cause_id"] == mission["hold_decision"]["decision_id"]
    assert len(mission["transitions"]) == len(approved["transitions"]) + 1

    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        2,
        _draft(project_id, budget_policy=None, objective="Ship the widget, still held"),
    )
    assert status == 200, body
    again = body["result"]["mission"]
    assert again["status"] == "blocked"
    assert again["revision"] == 3
    assert again["objective"] == "Ship the widget, still held"
    assert again["transitions"] == mission["transitions"]
    assert again["hold_decision"]["decision_id"] == mission["hold_decision"]["decision_id"]


def test_stale_base_revision_conflicts(service) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)
    before = _events(service, workspace_id, mission_id)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        2,
        _draft(project_id, objective="Ship a different widget"),
    )
    assert status == 409, body
    assert body["error"]["code"] == "revision_conflict"
    assert _events(service, workspace_id, mission_id) == before
    read = _get(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    assert read.json()["revision"] == approved["revision"]
    assert read.json()["status"] == "approved"
    assert read.json()["transitions"] == approved["transitions"]


def test_completed_mission_revision_is_refused(service) -> None:
    workspace_id, project_id, mission_id, _approved_mission = _approved(service)
    for target in ("running", "completed"):
        cause = (
            {"cause_kind": "principal"}
            if target == "running"
            else {"cause_kind": "decision", "decision_id": str(uuid4())}
        )
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "set_mission_status",
                "payload": {
                    "mission_id": str(mission_id),
                    "target_status": target,
                    **cause,
                },
            },
        )
        assert status == 200, body
    completed = body["result"]["mission"]
    assert completed["status"] == "completed"
    assert completed["revision"] == 1
    before = _events(service, workspace_id, mission_id)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, objective="Ship a different widget"),
    )
    assert status == 409, body
    assert body["error"]["code"] == "mission_transition_refused"
    assert _events(service, workspace_id, mission_id) == before
    read = _get(service.client, workspace_id, mission_id).json()
    assert read["status"] == "completed"
    assert read["transitions"] == completed["transitions"]


def test_unknown_project_is_invalid(service) -> None:
    workspace_id, _project_id, mission_id, _approved_mission = _approved(service)
    before = _events(service, workspace_id, mission_id)
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(uuid4()),
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert _events(service, workspace_id, mission_id) == before
