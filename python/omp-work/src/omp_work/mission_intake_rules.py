"""Pure mission-intake bridge between bounded intake and the mission record (OMP-426).

Reuses the existing rule surfaces instead of restating them: blocked questions
come from ``omp_work.v1.semantics.evaluate_bounded_intake``, approval routing
from ``omp_work.mission_scope.material_cases``, and standing-mandate scope from
``omp_work.standing_mandate.mission_scope``. No database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from omp_work.mission_scope import material_cases
from omp_work.standing_mandate import MissionScopeDraft, StandingMandate, mission_scope
from omp_work.v1.models import BoundedIntakeDraft, IntakeBlockingQuestion, MissionDraft
from omp_work.v1.semantics import evaluate_bounded_intake

__all__ = [
    "Route",
    "clarifying_questions",
    "confirmation_route",
    "draft_from_intake",
    "scope_draft",
]

_RESERVED_SCOPE_KEYS: frozenset[str] = frozenset(
    {"objective", "acceptance_criteria", "constraints"}
)


def clarifying_questions(
    intake: BoundedIntakeDraft,
) -> tuple[IntakeBlockingQuestion, ...]:
    """Return the capped blocking questions for a bounded-intake draft."""
    questions, _issue_count = evaluate_bounded_intake(intake)
    return questions


def draft_from_intake(
    intake: BoundedIntakeDraft,
    scope: Mapping[str, object],
) -> MissionDraft:
    """Build a mission draft from a bounded-intake draft and a scope mapping.

    The objective, acceptance criteria (each criterion's observable outcome, in
    order), and constraints (each constraint's statement) come from the intake;
    every other mission field comes from ``scope``. A scope that carries
    ``objective``, ``acceptance_criteria``, or ``constraints`` raises
    ``ValueError``.
    """
    reserved = _RESERVED_SCOPE_KEYS & set(scope)
    if reserved:
        raise ValueError(
            "scope must not carry mission draft fields: "
            + ", ".join(sorted(reserved))
        )
    fields: dict[str, object] = dict(scope)
    fields["objective"] = intake.goal.statement
    fields["acceptance_criteria"] = tuple(
        criterion.observable_outcome for criterion in intake.acceptance_criteria
    )
    fields["constraints"] = tuple(
        constraint.statement for constraint in intake.constraints
    )
    return MissionDraft(**fields)


@dataclass(frozen=True)
class Route:
    kind: Literal["owner_decision", "approved_mission", "standing_mandate"]
    cases: tuple[str, ...]
    mandate_id: str | None = None


def scope_draft(draft: MissionDraft) -> MissionScopeDraft:
    """Build a mission scope draft from a mission draft."""
    budget = draft.budget_policy
    ceiling = None
    if budget is not None:
        usd_val = budget["usd"] if isinstance(budget, dict) else budget.usd
        ceiling = Decimal(str(usd_val))
    return MissionScopeDraft(
        goals=frozenset({draft.objective}),
        repositories=frozenset(draft.repositories),
        capabilities=frozenset(draft.requested_capabilities),
        tier3_classes=frozenset(draft.approval_classes),
        budget_ceiling_usd=ceiling,
    )


def confirmation_route(
    approved_envelope: Mapping[str, object] | None,
    draft: MissionDraft,
    mandate: StandingMandate | None,
    standing_ceiling_usd: Decimal | None,
) -> Route:
    """Route a mission draft: approved as-is, covered by a mandate, or owner-gated.

    ``approved_envelope`` is the approved draft's ``model_dump(mode="json")`` or
    ``None`` when nothing is approved. Material cases are computed against the
    revision; no case approves the mission, a mandate that covers the whole
    scope approves it, and anything else is an owner decision.
    """
    cases = material_cases(approved_envelope, draft.model_dump(mode="json"))
    if not cases:
        return Route(kind="approved_mission", cases=cases)

    scope = scope_draft(draft)
    if "undecidable" not in cases and mission_scope(
        mandate, scope, standing_ceiling_usd
    ).status == "approved":
        return Route(
            kind="standing_mandate",
            cases=cases,
            mandate_id=str(mandate.mandate_id) if mandate is not None else None,
        )
    return Route(kind="owner_decision", cases=cases)
