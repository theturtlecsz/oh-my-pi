"""Mission intake drafting: bounded intake to mission draft to confirmation route (OMP-426).

The handler bridges the pure rules in ``omp_work.mission_intake_rules`` to the
event-sourced mission record in ``omp_work.v1.missions``: clarify while the intake
still has blocking questions, hold when the admitted mission has no budget,
proceed when an approved mission or standing mandate already covers the scope,
and otherwise file one deterministic owner decision for the revision.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from omp_work.mission_intake_rules import (
    clarifying_questions,
    confirmation_route,
    draft_from_intake,
)
from omp_work.mission_scope import TERMINAL_STATUSES
from omp_work.standing_mandate import StandingMandate

from .api_models import DraftMissionIntakeResult, MissionView
from .missions import (
    approved_mission_view,
    effective_draft,
    latest_mission,
    new_mission_view,
    revised_mission_view,
    status_mission_view,
)
from .models import (
    CommandEnvelope,
    CreateDecisionPayload,
    IntakeBlockingQuestion,
    MissionDraft,
    MissionStatus,
    OwnerInstruction,
)
from .store_shared import WorkStoreError

__all__ = ["draft_mission_intake", "intake_decision"]

_Outcome = Literal["clarify", "held", "awaiting_owner", "proceeded"]
_Basis = Literal["approved_mission", "standing_mandate"]

# One decision per (mission, revision): a redraft of the same revision recomputes
# the same id without a decision table.
_DECISION_NS = uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-intake-decision")

_CONFIRMED_STATUSES = frozenset({MissionStatus.APPROVED, MissionStatus.RUNNING})

_OPTIONS: tuple[str, ...] = ("confirm", "reject")


def draft_mission_intake(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
    mandate: StandingMandate | None,
    standing_ceiling_usd: Decimal | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Draft one mission intake and route it to proceed, hold, or owner decision."""
    payload = envelope.command.payload
    mission_id = payload.mission_id

    prior = latest_mission(cur, envelope.workspace_id, mission_id)
    questions = clarifying_questions(payload.intake)
    if questions:
        result = _result(mission_id, "clarify", questions, prior)
        return result, _event(result, None, payload.instruction)

    draft = draft_from_intake(payload.intake, payload.scope.model_dump())
    view = _build_view(cur, envelope, prior, draft, actor_id, actor_kind)
    if view.status == MissionStatus.BLOCKED:
        result = _result(mission_id, "held", (), view)
        return result, _event(result, None, payload.instruction)

    approved_envelope = (
        prior.approved_scope.envelope.model_dump(mode="json")
        if prior is not None and prior.approved_scope is not None
        else None
    )
    route = confirmation_route(
        approved_envelope,
        effective_draft(view),
        mandate,
        standing_ceiling_usd,
    )

    if (
        route.kind == "approved_mission"
        and prior is not None
        and prior.status in _CONFIRMED_STATUSES
    ):
        result = _result(mission_id, "proceeded", (), view, basis="approved_mission")
        return result, _event(result, None, payload.instruction)
    if route.kind == "standing_mandate":
        approved = approved_mission_view(
            cur, view, "standing_mandate", str(route.mandate_id), actor_id, actor_kind
        )
        result = _result(
            mission_id, "proceeded", (), approved, basis="standing_mandate"
        )
        return result, _event(result, None, payload.instruction)

    decision = intake_decision(view, route.cases, actor_kind)
    if view.status != MissionStatus.AWAITING_CONFIRMATION:
        view = status_mission_view(
            cur,
            view,
            MissionStatus.AWAITING_CONFIRMATION,
            "decision",
            str(decision.decision_id),
            actor_id,
            actor_kind,
        )
    result = _result(
        mission_id, "awaiting_owner", (), view, decision_id=decision.decision_id
    )
    return result, _event(result, decision, payload.instruction)


def intake_decision(
    view: MissionView, cases: tuple[str, ...], actor_kind: str
) -> CreateDecisionPayload:
    """The owner decision asking to confirm one mission revision as drafted."""
    revision = view.revision
    mission_id = view.mission_id
    return CreateDecisionPayload(
        decision_id=uuid5(_DECISION_NS, f"{mission_id}:{revision}"),
        project_id=view.project_id,
        mission_id=str(mission_id),
        question=_question(view, cases, actor_kind),
        why_it_matters=(
            f"Mission {mission_id} revision {revision} is not covered by an "
            "approved scope or standing mandate."
        ),
        risk_of_delay=(
            "Work inside an unconfirmed mission scope could exceed what the "
            "owner approved."
        ),
        options=_OPTIONS,
        evidence_refs=(f"mission:{mission_id}@{revision}",),
        risk_of_each_choice={
            "confirm": "The mission proceeds with this scope.",
            "reject": "The mission is abandoned and no work proceeds.",
        },
    )


def _question(view: MissionView, cases: tuple[str, ...], filer: str) -> str:
    return (
        f"Confirm mission revision {view.revision} for objective "
        + view.objective
        + f" (cases: {','.join(cases)}); filed by {filer}."
    )


def _build_view(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    prior: MissionView | None,
    draft: MissionDraft,
    actor_id: UUID,
    actor_kind: str,
) -> MissionView:
    payload = envelope.command.payload
    workspace_id = envelope.workspace_id
    if prior is None:
        if payload.base_revision is not None:
            raise WorkStoreError("revision_conflict")
        return new_mission_view(
            cur, workspace_id, payload.mission_id, draft, actor_id, actor_kind
        )
    if prior.status.value in TERMINAL_STATUSES:
        raise WorkStoreError("mission_transition_refused")
    if payload.base_revision != prior.revision:
        raise WorkStoreError("revision_conflict")
    return revised_mission_view(cur, workspace_id, prior, draft, actor_id, actor_kind)


def _result(
    mission_id: UUID,
    outcome: _Outcome,
    questions: tuple[IntakeBlockingQuestion, ...],
    mission: MissionView | None,
    *,
    decision_id: UUID | None = None,
    basis: _Basis | None = None,
) -> dict[str, object]:
    return DraftMissionIntakeResult(
        type="draft_mission_intake",
        mission_id=mission_id,
        outcome=outcome,
        questions=questions,
        mission=mission,
        decision_id=decision_id,
        basis=basis,
    ).model_dump(mode="json")


def _event(
    result: dict[str, object],
    decision: CreateDecisionPayload | None,
    instruction: OwnerInstruction | None,
) -> dict[str, object]:
    return {
        **result,
        "decision": None if decision is None else decision.model_dump(mode="json"),
        "instruction": (
            None if instruction is None else instruction.model_dump(mode="json")
        ),
    }
