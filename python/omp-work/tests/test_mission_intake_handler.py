"""OMP-426-s06: the draft_mission_intake handler over the event-sourced mission record.

Defends the externally observable contract of ``draft_mission_intake`` called on a
dict_row cursor inside a rolled-back transaction:

1. Blocking intake questions return outcome ``clarify`` with the prior mission
   unchanged and no decision.
2. The routing arms: a blocked mission is ``held``, a new revision inside an
   approved mission or a standing mandate is ``proceeded``, and anything else
   files the owner decision; a redraft whose only change is criterion order on a
   paused mission moves it back to ``awaiting_confirmation``.
3. A stale ``base_revision`` is ``revision_conflict`` and a terminal prior is
   ``mission_transition_refused``.
4. Two calls on one (mission, revision) recompute one decision id, and its
   question names the objective and the exact material cases.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.standing_mandate import StandingMandate
from omp_work.v1.canonical import text_sha256
from omp_work.v1.mission_intake import draft_mission_intake
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store_shared import WorkStoreError
from psycopg.rows import dict_row
from test_workflow_service import OWNER, _command, _grant

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

_MANDATE_ID = UUID("00000000-0000-7000-8000-0000000000c1")


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


def _draft(project_id: UUID, **overrides) -> dict:
    draft = {
        "project_id": str(project_id),
        "objective": "Bound memory growth",
        "acceptance_criteria": ["RSS <= 256MB", "Latency <= 5ms"],
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


def _submit(
    service, workspace_id: UUID, project_id: UUID, **overrides
) -> tuple[UUID, dict]:
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


def _approve(service, workspace_id: UUID, mission_id: UUID, revision: int) -> dict:
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


def _set_status(
    service, workspace_id: UUID, mission_id: UUID, target: str, **cause
) -> dict:
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
    return body["result"]["mission"]


def _approved(service) -> tuple[UUID, UUID, UUID, dict]:
    workspace_id, project_id = _project(service)
    mission_id, _submitted = _submit(service, workspace_id, project_id)
    approved = _approve(service, workspace_id, mission_id, 1)
    assert approved["status"] == "approved"
    assert approved["revision"] == 1
    return workspace_id, project_id, mission_id, approved


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


def _command_payload(
    mission_id: UUID,
    project_id: UUID,
    *,
    base_revision: int | None,
    intake: dict,
    scope: dict | None = None,
    instruction: dict | None = None,
) -> dict:
    payload: dict = {
        "mission_id": str(mission_id),
        "base_revision": base_revision,
        "intake": intake,
        "scope": _scope(project_id) if scope is None else scope,
    }
    if instruction is not None:
        payload["instruction"] = instruction
    return {"type": "draft_mission_intake", "payload": payload}


def _run(
    service,
    workspace_id: UUID,
    command: dict,
    *,
    actor_id: UUID | None = None,
    actor_kind: str = "agent",
    mandate: StandingMandate | None = None,
    standing_ceiling_usd: Decimal | None = None,
) -> tuple[dict, dict]:
    """Call the handler on a dict_row cursor and roll the transaction back."""
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )
    conn = psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    )
    try:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        with conn.cursor() as cur:
            result, event = draft_mission_intake(
                cur,
                envelope,
                actor_id or OWNER,
                actor_kind,
                mandate,
                standing_ceiling_usd,
            )
        conn.rollback()
        return result, event
    finally:
        conn.close()


def test_answering_questions_clarifies_and_leaves_prior_unchanged(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id, submitted = _submit(service, workspace_id, project_id)

    command = _command_payload(
        mission_id,
        project_id,
        base_revision=1,
        intake=_intake(criteria=(("ac-1", "RSS <= 256MB", None),)),
    )
    result, event = _run(service, workspace_id, command)

    assert result["outcome"] == "clarify"
    assert result["decision_id"] is None
    assert result["basis"] is None
    assert len(result["questions"]) == 1
    assert result["questions"][0]["rule_class"] == "missing_verification_oracle"
    assert result["mission"] == submitted
    assert event["decision"] is None
    assert event["instruction"] is None
    assert event["type"] == "draft_mission_intake"


def test_new_mission_without_prior_files_owner_decision(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    instruction = {
        "text": "File this objective for confirmation.",
        "provenance": {
            "channel": "owner-chat",
            "message_ref": "msg-426-s06",
            "received_at": "2026-09-30T12:00:00+00:00",
        },
    }
    command = _command_payload(
        mission_id,
        project_id,
        base_revision=None,
        intake=_intake(),
        instruction=instruction,
    )
    result, event = _run(service, workspace_id, command)

    assert result["outcome"] == "awaiting_owner"
    assert result["basis"] is None
    assert result["decision_id"] is not None
    assert result["mission"]["revision"] == 1
    assert result["mission"]["status"] == "awaiting_confirmation"
    assert result["questions"] == []
    assert event["instruction"]["text"] == instruction["text"]
    assert event["decision"]["decision_id"] == result["decision_id"]
    assert event["decision"]["options"] == ["confirm", "reject"]
    assert event["decision"]["evidence_refs"] == [f"mission:{mission_id} @1"]
    assert event["decision"]["mission_id"] == str(mission_id)
    assert event["decision"]["action_class"] is None


def test_blocked_mission_is_held(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id, held = _submit(service, workspace_id, project_id, budget_policy=None)
    assert held["status"] == "blocked"
    # The budgetless mission stays held when the redraft still carries no budget.
    command = _command_payload(
        mission_id,
        project_id,
        base_revision=1,
        intake=_intake(),
        scope=_scope(project_id, budget_policy=None),
    )
    result, event = _run(service, workspace_id, command)

    assert result["outcome"] == "held"
    assert result["decision_id"] is None
    assert result["mission"]["status"] == "blocked"
    assert result["mission"]["revision"] == 2
    assert event["decision"] is None


def test_material_case_files_one_decision_for_the_revision(service) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)

    # A criterion whose collapsed text is not approved is material case "a";
    # the approved criteria are kept so no other case applies.
    command = _command_payload(
        mission_id,
        project_id,
        base_revision=1,
        intake=_intake(
            criteria=(
                ("ac-1", "RSS <= 256MB", "automated_test"),
                ("ac-2", "Latency <= 5ms", "automated_test"),
                ("ac-3", "P99 <= 10ms", "automated_test"),
            )
        ),
    )
    first, _first_event = _run(service, workspace_id, command)
    second, second_event = _run(service, workspace_id, command)

    assert first["outcome"] == "awaiting_owner"
    assert second["outcome"] == "awaiting_owner"
    assert first["decision_id"] == second["decision_id"]
    assert first["mission"]["revision"] == 2
    assert first["mission"]["status"] == "awaiting_confirmation"
    assert "Bound memory growth" in second_event["decision"]["question"]
    assert "cases: a" in second_event["decision"]["question"]
    assert second_event["decision"]["evidence_refs"] == [f"mission:{mission_id} @2"]
    # The prior approval carries to the revised snapshot untouched.
    assert first["mission"]["approved_scope"] == approved["approved_scope"]


def test_paused_mission_redraft_returns_to_awaiting_confirmation(service) -> None:
    workspace_id, project_id, mission_id, _approved_mission = _approved(service)
    paused = _set_status(
        service, workspace_id, mission_id, "paused", cause_kind="principal"
    )
    assert paused["status"] == "paused"

    # Same objective, criteria, scope, and budget — only criterion order changes,
    # so the D29 rule finds no material case and the approved mission still
    # covers the revision. The paused status must not resume it unconfirmed.
    command = _command_payload(
        mission_id,
        project_id,
        base_revision=1,
        intake=_intake(
            criteria=(
                ("ac-1", "Latency <= 5ms", "automated_test"),
                ("ac-2", "RSS <= 256MB", "automated_test"),
            )
        ),
    )
    result, _event = _run(service, workspace_id, command)

    assert result["outcome"] == "awaiting_owner"
    assert result["decision_id"] is not None
    assert result["basis"] is None
    assert result["mission"]["status"] == "awaiting_confirmation"
    assert result["mission"]["revision"] == 2
    assert result["mission"]["transitions"][-1]["to_status"] == "awaiting_confirmation"
    assert result["mission"]["transitions"][-1]["cause_kind"] == "decision"
    assert result["mission"]["transitions"][-1]["cause_id"] == result["decision_id"]


def test_standing_mandate_proceeds_with_basis(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    mandate = StandingMandate(
        mandate_id=_MANDATE_ID,
        goals=frozenset({"Bound memory growth"}),
        repositories=frozenset({"repo-a", "repo-b"}),
        capabilities=frozenset({"read", "test"}),
        tier3_classes=frozenset({"tier-1"}),
        decision_id="dec-426",
    )
    command = _command_payload(
        mission_id, project_id, base_revision=None, intake=_intake()
    )
    result, event = _run(
        service,
        workspace_id,
        command,
        mandate=mandate,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert result["outcome"] == "proceeded"
    assert result["basis"] == "standing_mandate"
    assert result["decision_id"] is None
    assert result["mission"]["status"] == "approved"
    assert result["mission"]["approved_scope"]["basis_kind"] == "standing_mandate"
    assert result["mission"]["approved_scope"]["basis_id"] == str(_MANDATE_ID)
    assert event["decision"] is None


def test_stale_base_revision_is_refused(service) -> None:
    workspace_id, project_id, mission_id, approved = _approved(service)
    command = _command_payload(
        mission_id,
        project_id,
        base_revision=2,
        intake=_intake(criteria=(("ac-1", "P99 <= 10ms", "automated_test"),)),
    )
    with pytest.raises(WorkStoreError) as excinfo:
        _run(service, workspace_id, command)
    assert excinfo.value.code == "revision_conflict"

    # A first draft that carries a base_revision has no prior to revise.
    with pytest.raises(WorkStoreError) as excinfo:
        _run(
            service,
            workspace_id,
            _command_payload(uuid4(), project_id, base_revision=1, intake=_intake()),
        )
    assert excinfo.value.code == "revision_conflict"

    # The prior is untouched.
    assert approved["revision"] == 1


def test_terminal_prior_is_refused(service) -> None:
    workspace_id, project_id, mission_id, _approved_mission = _approved(service)
    _set_status(service, workspace_id, mission_id, "running", cause_kind="principal")
    completed = _set_status(
        service,
        workspace_id,
        mission_id,
        "completed",
        cause_kind="decision",
        decision_id=str(uuid4()),
    )
    assert completed["status"] == "completed"

    command = _command_payload(
        mission_id, project_id, base_revision=1, intake=_intake()
    )
    with pytest.raises(WorkStoreError) as excinfo:
        _run(service, workspace_id, command)
    assert excinfo.value.code == "mission_transition_refused"
