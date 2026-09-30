"""OMP-426: draft_mission_intake wired into PostgresWorkStore."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work import contract_sha256
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import StandingMandate
from omp_work.v1.canonical import text_sha256
from omp_work.v1.store import PostgresWorkStore
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


def _project(service, provenance: dict | None = None) -> tuple[UUID, UUID]:
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


def _grant_agent(service, workspace_id: UUID) -> tuple[str, UUID]:
    agent_file = service.capabilities / "agent.json"
    agent_id = uuid4()
    token = "agent-token"
    if agent_file.exists():
        data = json.loads(agent_file.read_text())
        if str(workspace_id) not in data["workspaces"]:
            data["workspaces"].append(str(workspace_id))
            agent_file.write_text(json.dumps(data))
            agent_file.chmod(0o600)
        return token, UUID(data["actor_id"])
    agent_file.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(agent_id),
                "actor_kind": "agent",
                "workspaces": [str(workspace_id)],
                "scopes": [
                    "work.read",
                    "work.mutate",
                    "work.approve",
                    "work.execute",
                ],
            }
        )
    )
    agent_file.chmod(0o600)
    return token, agent_id


def _get_mission(client, workspace_id: UUID, mission_id: UUID):
    return client.get(
        f"/v1/workspaces/{workspace_id}/missions/{mission_id}",
        headers=_owner_headers(workspace_id),
    )


def _get_decisions(client, workspace_id: UUID, status: str = "pending"):
    return client.get(
        f"/v1/workspaces/{workspace_id}/decisions?status={status}",
        headers=_owner_headers(workspace_id),
    )


def _intake(
    *,
    goal: str = "Bound memory growth",
    criteria: tuple[tuple[str, str, str | None], ...] = (
        ("ac-1", "RSS <= 256MB", "automated_test"),
    ),
) -> dict:
    text = "Bound the memory growth of the cache eviction path."
    return {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {"id": "goal-1", "statement": goal, "source_span_ids": []},
        "acceptance_criteria": [
            {
                "id": criterion_id,
                "statement": outcome,
                "source_span_ids": [],
                "observable_outcome": outcome,
                "oracle": oracle,
            }
            for criterion_id, outcome, oracle in criteria
        ],
    }


def _scope(project_id: UUID, **overrides) -> dict:
    scope = {
        "project_id": str(project_id),
        "repositories": ["repo-a", "repo-b"],
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    scope.update(overrides)
    return scope


def _draft_mission_intake(
    service,
    workspace_id: UUID,
    mission_id: UUID,
    project_id: UUID,
    *,
    base_revision: int | None = None,
    intake: dict | None = None,
    scope: dict | None = None,
    instruction: dict | None = None,
    token: str = "owner-token",
) -> tuple[int, dict]:
    payload: dict = {
        "mission_id": str(mission_id),
        "base_revision": base_revision,
        "intake": _intake() if intake is None else intake,
        "scope": _scope(project_id) if scope is None else scope,
    }
    if instruction is not None:
        payload["instruction"] = instruction
    return _command(
        service,
        workspace_id,
        {"type": "draft_mission_intake", "payload": payload},
        token=token,
    )


def _approve_mission(
    service,
    workspace_id: UUID,
    mission_id: UUID,
    revision: int,
    *,
    token: str = "owner-token",
) -> tuple[int, dict]:
    return _command(
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
        token=token,
    )


def _set_status(
    service,
    workspace_id: UUID,
    mission_id: UUID,
    target: str,
    *,
    token: str = "owner-token",
    **cause,
) -> tuple[int, dict]:
    return _command(
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
        token=token,
    )


def test_clear_intake_awaits_owner_and_folds_pending_decision(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    intake = _intake(
        criteria=(
            ("ac-1", "RSS <= 256MB", "automated_test"),
            ("ac-2", "Latency <= 5ms", "automated_test"),
        )
    )
    status, body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, intake=intake
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "awaiting_owner"
    assert result["decision_id"] is not None
    decision_id = result["decision_id"]

    read = _get_mission(service.client, workspace_id, mission_id)
    assert read.status_code == 200, read.text
    mission = read.json()
    assert mission["status"] == "awaiting_confirmation"
    assert mission["acceptance_criteria"] == ["RSS <= 256MB", "Latency <= 5ms"]

    decisions_resp = _get_decisions(service.client, workspace_id, status="pending")
    assert decisions_resp.status_code == 200, decisions_resp.text
    pending = decisions_resp.json()["decisions"]
    assert len(pending) == 1
    assert pending[0]["decision_id"] == str(decision_id)
    assert pending[0]["status"] == "pending"
    assert pending[0]["mission_id"] == str(mission_id)


def test_ambiguous_intake_returns_clarify_and_no_mission_or_decision(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    intake = _intake(criteria=(("ac-1", "RSS <= 256MB", None),))
    status, body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, intake=intake
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "clarify"
    assert len(result["questions"]) > 0
    assert result["decision_id"] is None

    read = _get_mission(service.client, workspace_id, mission_id)
    assert read.status_code == 400
    assert read.json()["error"]["code"] == "invalid_request"

    decisions_resp = _get_decisions(service.client, workspace_id, status="pending")
    assert decisions_resp.status_code == 200
    assert len(decisions_resp.json()["decisions"]) == 0


def test_agent_kind_new_scope_awaits_owner_and_agent_approve_is_refused(service) -> None:
    workspace_id, project_id = _project(service)
    agent_token, _agent_id = _grant_agent(service, workspace_id)
    mission_id = uuid4()
    status, body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, token=agent_token
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "awaiting_owner"
    assert result["mission"]["status"] == "awaiting_confirmation"

    status, approve_body = _approve_mission(
        service, workspace_id, mission_id, 1, token=agent_token
    )
    assert status == 409
    assert approve_body["error"]["code"] == "approval_required"


def test_inside_mandate_proceeds_as_approved_without_decision(service) -> None:
    workspace_id, project_id = _project(service)
    store = PostgresWorkStore(service.config)
    mandate_decision = uuid4()
    mandate_id = uuid4()
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100.00"), Decimal("40.00")),
        ChangeAuthority("owner", uuid4(), "owner"),
    )
    store.set_standing_mandate(
        workspace_id,
        OWNER,
        project_id,
        StandingMandate(
            mandate_id=mandate_id,
            goals=frozenset({"Bound memory growth"}),
            repositories=frozenset({"repo-a", "repo-b"}),
            capabilities=frozenset({"read", "test"}),
            tier3_classes=frozenset({"tier-1"}),
            decision_id=mandate_decision,
        ),
        ChangeAuthority("owner", mandate_decision, "owner"),
    )

    mission_id = uuid4()
    status, body = _draft_mission_intake(service, workspace_id, mission_id, project_id)
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "proceeded"
    assert result["basis"] == "standing_mandate"
    assert result["decision_id"] is None
    assert result["mission"]["status"] == "approved"
    assert result["mission"]["approved_scope"]["basis_kind"] == "standing_mandate"
    assert result["mission"]["approved_scope"]["basis_id"] == str(mandate_id)

    read = _get_mission(service.client, workspace_id, mission_id)
    assert read.status_code == 200
    assert read.json()["status"] == "approved"

    decisions_resp = _get_decisions(service.client, workspace_id, status="pending")
    assert decisions_resp.status_code == 200
    assert len(decisions_resp.json()["decisions"]) == 0


def test_approved_mission_reordered_redraft_proceeds_and_stays_approved(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    intake1 = _intake(
        criteria=(
            ("ac-1", "RSS <= 256MB", "automated_test"),
            ("ac-2", "Latency <= 5ms", "automated_test"),
        )
    )
    status1, body1 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, intake=intake1
    )
    assert status1 == 200
    assert body1["result"]["outcome"] == "awaiting_owner"

    approve_status, approve_body = _approve_mission(service, workspace_id, mission_id, 1)
    assert approve_status == 200
    assert approve_body["result"]["mission"]["status"] == "approved"

    intake2 = _intake(
        criteria=(
            ("ac-2", "Latency <= 5ms", "automated_test"),
            ("ac-1", "RSS <= 256MB", "automated_test"),
        )
    )
    status2, body2 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, base_revision=1, intake=intake2
    )
    assert status2 == 200, body2
    result2 = body2["result"]
    assert result2["outcome"] == "proceeded"
    assert result2["basis"] == "approved_mission"
    assert result2["mission"]["status"] == "approved"
    assert result2["mission"]["revision"] == 2


def test_approved_mission_added_criterion_awaits_owner_with_case_a(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    intake1 = _intake(
        criteria=(
            ("ac-1", "RSS <= 256MB", "automated_test"),
            ("ac-2", "Latency <= 5ms", "automated_test"),
        )
    )
    status1, body1 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, intake=intake1
    )
    assert status1 == 200

    approve_status, approve_body = _approve_mission(service, workspace_id, mission_id, 1)
    assert approve_status == 200

    intake2 = _intake(
        criteria=(
            ("ac-1", "RSS <= 256MB", "automated_test"),
            ("ac-2", "Latency <= 5ms", "automated_test"),
            ("ac-3", "Throughput >= 1000", "automated_test"),
        )
    )
    status2, body2 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, base_revision=1, intake=intake2
    )
    assert status2 == 200, body2
    result2 = body2["result"]
    assert result2["outcome"] == "awaiting_owner"
    assert result2["decision_id"] is not None

    decisions_resp = _get_decisions(service.client, workspace_id, status="pending")
    assert decisions_resp.status_code == 200
    pending = decisions_resp.json()["decisions"]
    matching = [d for d in pending if d["decision_id"] == str(result2["decision_id"])]
    assert len(matching) == 1
    assert "cases: a" in matching[0]["question"]


def test_paused_mission_in_scope_redraft_awaits_owner_and_resume_refused(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    intake1 = _intake(
        criteria=(
            ("ac-1", "RSS <= 256MB", "automated_test"),
            ("ac-2", "Latency <= 5ms", "automated_test"),
        )
    )
    status1, body1 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, intake=intake1
    )
    assert status1 == 200

    approve_status, approve_body = _approve_mission(service, workspace_id, mission_id, 1)
    assert approve_status == 200

    pause_status, pause_body = _set_status(
        service,
        workspace_id,
        mission_id,
        "paused",
        cause_kind="policy_rule",
        policy_rule_id="rule.pause_review",
    )
    assert pause_status == 200
    assert pause_body["result"]["mission"]["status"] == "paused"

    intake2 = _intake(
        criteria=(
            ("ac-2", "Latency <= 5ms", "automated_test"),
            ("ac-1", "RSS <= 256MB", "automated_test"),
        )
    )
    status2, body2 = _draft_mission_intake(
        service, workspace_id, mission_id, project_id, base_revision=1, intake=intake2
    )
    assert status2 == 200, body2
    result2 = body2["result"]
    assert result2["outcome"] == "awaiting_owner"
    assert result2["mission"]["status"] == "awaiting_confirmation"

    running_status, running_body = _set_status(
        service,
        workspace_id,
        mission_id,
        "running",
        cause_kind="principal",
    )
    assert running_status == 409
    assert running_body["error"]["code"] == "mission_transition_refused"
