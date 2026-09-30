"""OMP-413-s08: link_mission_work draws an item budget from a mission envelope."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from omp_work.v1.canonical import sha256
from omp_work.v1.server import create_app
from test_workflow_service import OWNER, _command, _create, _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = {
    "usd": "10",
    "tokens": 1000,
    "wall_clock_seconds": 600,
    "max_subagents": 2,
}
_ITEM_A = {
    "usd": "6",
    "tokens": 600,
    "wall_clock_seconds": 120,
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
        "objective": "Draw item budgets from the mission",
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


def _approve(service, workspace_id, mission_id, revision: int = 1):
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


def _approved(service, provenance: dict | None = None, **overrides):
    workspace_id, project_id = _project(service, provenance)
    mission_id, _submitted = _submit(service, workspace_id, project_id, **overrides)
    approved = _approve(service, workspace_id, mission_id)
    assert approved["status"] == "approved"
    assert approved["links"] == []
    assert approved["drawn"] == {"usd": "0", "tokens": 0, "wall_clock_seconds": 0}
    return workspace_id, project_id, mission_id, approved


def _seed_budget(service, workspace_id, *, work_id, revision_id, budget: dict) -> None:
    """Direct-SQL seed of the intake_publication receipt that carries the budget.

    The INSERT is the one ``_seed_budget`` in test_item_budget_stop.py uses.
    """
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(OWNER),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _item(service, workspace_id, title: str, budget: dict | None) -> dict:
    item = _create(service, workspace_id, title)
    if budget is not None:
        _seed_budget(
            service,
            workspace_id,
            work_id=item["work_id"],
            revision_id=item["revision_id"],
            budget=budget,
        )
    return item


def _link(service, workspace_id, mission_id, work_id, *, operation_id=None):
    return _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
        operation_id=operation_id,
    )


def _status(service, workspace_id, mission_id, target: str, **cause):
    payload = {
        "mission_id": str(mission_id),
        "target_status": target,
        "cause_kind": "principal",
    }
    payload.update(cause)
    status, body = _command(
        service,
        workspace_id,
        {"type": "set_mission_status", "payload": payload},
    )
    assert status == 200, body
    return body["result"]["mission"]


def _refuse(service, workspace_id, mission_id, work_id, code: str, events: int):
    status, body = _link(service, workspace_id, mission_id, work_id)
    assert status == (400 if code == "invalid_request" else 409), body
    assert body["error"]["code"] == code
    assert _events(service, workspace_id, mission_id) == events
    return body


def test_link_draws_item_budget_and_refuses_overrun(service) -> None:
    workspace_id, _project_id, mission_id, approved = _approved(service)
    assert _events(service, workspace_id, mission_id) == 2

    item_a = _item(service, workspace_id, "item A", _ITEM_A)
    operation_id = uuid4()
    status, body = _link(
        service,
        workspace_id,
        mission_id,
        item_a["work_id"],
        operation_id=operation_id,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["type"] == "link_mission_work"
    mission = body["result"]["mission"]
    assert mission["status"] == "approved"
    assert mission["revision"] == approved["revision"]
    assert mission["transitions"] == approved["transitions"]
    assert mission["drawn"] == {
        "usd": "6",
        "tokens": 600,
        "wall_clock_seconds": 120,
    }
    assert mission["links"] == [
        {
            "work_id": item_a["work_id"],
            "budget": _ITEM_A,
            "linked_at": mission["links"][0]["linked_at"],
        }
    ]
    assert mission["links"][0]["linked_at"]
    read = _get(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    assert read.json() == mission
    assert _events(service, workspace_id, mission_id) == 3

    replay_status, replay = _link(
        service,
        workspace_id,
        mission_id,
        item_a["work_id"],
        operation_id=operation_id,
    )
    assert replay_status == 200, replay
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == body["result"]
    assert _events(service, workspace_id, mission_id) == 3

    # 6 + 5 exceeds usd 10; the item alone would fit.
    item_b = _item(
        service,
        workspace_id,
        "item B",
        {"usd": "5", "tokens": 100, "wall_clock_seconds": 60, "max_subagents": 1},
    )
    _refuse(
        service,
        workspace_id,
        mission_id,
        item_b["work_id"],
        "mission_budget_exceeded",
        3,
    )
    unchanged = _get(service.client, workspace_id, mission_id)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["links"] == mission["links"]
    assert unchanged.json()["drawn"] == mission["drawn"]

    _refuse(
        service,
        workspace_id,
        mission_id,
        _item(
            service,
            workspace_id,
            "tokens",
            {"usd": "1", "tokens": 500, "wall_clock_seconds": 60, "max_subagents": 1},
        )["work_id"],
        "mission_budget_exceeded",
        3,
    )
    _refuse(
        service,
        workspace_id,
        mission_id,
        _item(
            service,
            workspace_id,
            "wall",
            {"usd": "1", "tokens": 10, "wall_clock_seconds": 500, "max_subagents": 1},
        )["work_id"],
        "mission_budget_exceeded",
        3,
    )
    _refuse(
        service,
        workspace_id,
        mission_id,
        _item(
            service,
            workspace_id,
            "subagents",
            {"usd": "1", "tokens": 10, "wall_clock_seconds": 60, "max_subagents": 3},
        )["work_id"],
        "mission_budget_exceeded",
        3,
    )
    _refuse(service, workspace_id, mission_id, item_a["work_id"], "invalid_request", 3)
    _refuse(service, workspace_id, mission_id, uuid4(), "invalid_request", 3)
    unbudgeted = _item(service, workspace_id, "unbudgeted", None)
    _refuse(
        service, workspace_id, mission_id, unbudgeted["work_id"], "invalid_request", 3
    )

    waiting_id, _waiting = _submit(service, workspace_id, _project_id)
    assert _events(service, workspace_id, waiting_id) == 1
    waiting_status, waiting_body = _link(
        service, workspace_id, waiting_id, item_a["work_id"]
    )
    assert waiting_status == 409, waiting_body
    assert waiting_body["error"]["code"] == "mission_transition_refused"
    assert _events(service, workspace_id, waiting_id) == 1
    assert _events(service, workspace_id, mission_id) == 3
    still = _get(service.client, workspace_id, mission_id)
    assert still.json()["links"] == mission["links"]
    assert still.json()["drawn"] == mission["drawn"]

    restarted = TestClient(
        create_app(service.config, capabilities_dir=service.capabilities)
    )
    again = _get(restarted, workspace_id, mission_id)
    assert again.status_code == 200, again.text
    assert again.json() == mission
    assert again.json()["links"][0]["work_id"] == item_a["work_id"]
    item_read = restarted.get(
        f"/v1/work-items/{item_a['key']}",
        headers=_owner_headers(workspace_id),
    )
    assert item_read.status_code == 200, item_read.text
    assert item_read.json()["work_id"] == item_a["work_id"]
    assert item_read.json()["alias"]["key"] == item_a["key"]

    # Equality stays inside the envelope; one unit past does not.
    exact_id, _exact = _submit(service, workspace_id, _project_id)
    _approve(service, workspace_id, exact_id)
    exact_budget = {
        "usd": "10",
        "tokens": 1000,
        "wall_clock_seconds": 600,
        "max_subagents": 2,
    }
    exact_item = _item(service, workspace_id, "exact", exact_budget)
    exact_status, exact_body = _link(
        service, workspace_id, exact_id, exact_item["work_id"]
    )
    assert exact_status == 200, exact_body
    assert exact_body["result"]["mission"]["drawn"] == {
        "usd": "10",
        "tokens": 1000,
        "wall_clock_seconds": 600,
    }
    past = _item(
        service,
        workspace_id,
        "past",
        {"usd": "1", "tokens": 1, "wall_clock_seconds": 1, "max_subagents": 0},
    )
    _refuse(
        service,
        workspace_id,
        exact_id,
        past["work_id"],
        "mission_budget_exceeded",
        3,
    )


def test_link_allowed_while_running_or_paused(service) -> None:
    workspace_id, _project_id, mission_id, _approved_mission = _approved(
        service,
        budget_policy={
            "usd": "30",
            "tokens": 3000,
            "wall_clock_seconds": 3000,
            "max_subagents": 2,
        },
    )
    small = {
        "usd": "1",
        "tokens": 10,
        "wall_clock_seconds": 10,
        "max_subagents": 1,
    }
    first = _item(service, workspace_id, "while running", small)
    second = _item(service, workspace_id, "while paused", small)
    third = _item(service, workspace_id, "after completed", small)

    _status(service, workspace_id, mission_id, "blocked")
    assert _events(service, workspace_id, mission_id) == 3
    _refuse(
        service,
        workspace_id,
        mission_id,
        first["work_id"],
        "mission_transition_refused",
        3,
    )

    _status(service, workspace_id, mission_id, "running")
    status, body = _link(service, workspace_id, mission_id, first["work_id"])
    assert status == 200, body
    assert body["result"]["mission"]["status"] == "running"
    assert body["result"]["mission"]["drawn"]["usd"] == "1"

    _status(service, workspace_id, mission_id, "paused")
    status, body = _link(service, workspace_id, mission_id, second["work_id"])
    assert status == 200, body
    paused = body["result"]["mission"]
    assert paused["status"] == "paused"
    assert paused["drawn"] == {"usd": "2", "tokens": 20, "wall_clock_seconds": 20}
    assert [link["work_id"] for link in paused["links"]] == [
        first["work_id"],
        second["work_id"],
    ]

    _status(service, workspace_id, mission_id, "running")
    _status(
        service,
        workspace_id,
        mission_id,
        "completed",
        cause_kind="decision",
        decision_id=str(uuid4()),
    )
    events = _events(service, workspace_id, mission_id)
    _refuse(
        service,
        workspace_id,
        mission_id,
        third["work_id"],
        "mission_transition_refused",
        events,
    )
    final = _get(service.client, workspace_id, mission_id)
    assert final.json()["status"] == "completed"
    assert final.json()["links"] == paused["links"]
    assert final.json()["drawn"] == paused["drawn"]


def test_link_draws_inherited_standing_budget(service) -> None:
    standing = {
        "usd": "10",
        "tokens": 1000,
        "wall_clock_seconds": 600,
        "max_subagents": 2,
    }
    workspace_id, _project_id, mission_id, approved = _approved(
        service, {"standing_budget": standing}, budget_policy=None
    )
    assert approved["budget_policy"] is None
    assert approved["budget"] == standing
    assert approved["budget_source"] == "project"
    item = _item(
        service,
        workspace_id,
        "standing",
        {"usd": "6", "tokens": 600, "wall_clock_seconds": 60, "max_subagents": 1},
    )
    status, body = _link(service, workspace_id, mission_id, item["work_id"])
    assert status == 200, body
    assert body["result"]["mission"]["drawn"] == {
        "usd": "6",
        "tokens": 600,
        "wall_clock_seconds": 60,
    }
    over = _item(
        service,
        workspace_id,
        "standing over",
        {"usd": "5", "tokens": 10, "wall_clock_seconds": 10, "max_subagents": 1},
    )
    _refuse(
        service,
        workspace_id,
        mission_id,
        over["work_id"],
        "mission_budget_exceeded",
        3,
    )
