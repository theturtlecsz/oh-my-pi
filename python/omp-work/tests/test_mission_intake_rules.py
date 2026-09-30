"""OMP-426-s02: pure mission-intake rules over bounded intake and mission scope.

Fixtures mirror ``test_bounded_intake_semantics.py`` (read only).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from omp_work.mission_intake_rules import (
    Route,
    clarifying_questions,
    confirmation_route,
    draft_from_intake,
)
from omp_work.standing_mandate import StandingMandate
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    ItemBudget,
    KnownIntakeValue,
    MissionDraft,
)

PROJECT = UUID("00000000-0000-7000-8000-0000000000a1")
MANDATE_ID = UUID("00000000-0000-7000-8000-0000000000b1")


def _make_ready_draft() -> BoundedIntakeDraft:
    text = "Optimize cache eviction policy to avoid unbounded memory growth."
    span1_text = "Optimize cache eviction policy"
    span1 = IntakeSourceSpan(
        id="span-1",
        start=0,
        end=len(span1_text.encode("utf-8")),
        exact_text_sha256=text_sha256(span1_text),
    )
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=IntakeSource(text=text, sha256=text_sha256(text), spans=(span1,)),
        goal=IntakeGoal(
            id="claim-goal-1",
            statement="Bound memory growth",
            source_span_ids=("span-1",),
        ),
        constraints=(
            IntakeConstraint(
                id="claim-c-1",
                statement="Max latency 5ms",
                source_span_ids=("span-1",),
                key="max_latency_ms",
                value=KnownIntakeValue(value=5),
                polarity="positive",
            ),
        ),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="claim-ac-1",
                statement="RSS remains bounded",
                source_span_ids=(),
                observable_outcome="RSS <= 256MB",
                oracle="automated_test",
            ),
        ),
    )


def _budget() -> ItemBudget:
    return ItemBudget(
        usd="10.00",
        tokens=1000,
        wall_clock_seconds=3600,
        max_subagents=4,
    )


def _scope(**overrides: object) -> dict[str, object]:
    scope: dict[str, object] = {
        "project_id": PROJECT,
        "repositories": ("repo-a",),
        "requested_capabilities": ("read",),
        "approval_classes": (),
        "risk_policy": "risk-default",
        "approval_policy": "approval-default",
        "effort_policy": "effort-default",
        "budget_policy": _budget(),
    }
    scope.update(overrides)
    return scope


def _mission_draft(**overrides: object) -> MissionDraft:
    fields: dict[str, object] = {
        "project_id": PROJECT,
        "objective": "Bound memory growth",
        "acceptance_criteria": ("RSS <= 256MB", "Latency <= 5ms"),
        "constraints": ("Max latency 5ms",),
        "repositories": ("repo-a",),
        "requested_capabilities": ("read",),
        "approval_classes": (),
        "risk_policy": "risk-default",
        "approval_policy": "approval-default",
        "effort_policy": "effort-default",
        "budget_policy": _budget(),
    }
    fields.update(overrides)
    return MissionDraft(**fields)


def _covering_mandate(**overrides: object) -> StandingMandate:
    fields: dict[str, object] = {
        "mandate_id": MANDATE_ID,
        "goals": frozenset({"Bound memory growth"}),
        "repositories": frozenset({"repo-a"}),
        "capabilities": frozenset({"read"}),
        "tier3_classes": frozenset(),
        "decision_id": "dec-426",
    }
    fields.update(overrides)
    return StandingMandate(**fields)


# ---------------------------------------------------------------------------
# clarifying_questions
# ---------------------------------------------------------------------------


def test_complete_intake_has_no_questions() -> None:
    assert clarifying_questions(_make_ready_draft()) == ()


def test_criterion_without_oracle_yields_question() -> None:
    base = _make_ready_draft()
    criterion = base.acceptance_criteria[0].model_copy(update={"oracle": None})
    draft = base.model_copy(update={"acceptance_criteria": (criterion,)})
    questions = clarifying_questions(draft)
    assert len(questions) == 1
    assert questions[0].rule_class == "missing_verification_oracle"
    assert questions[0].deduplication_key == "oracle:claim-ac-1"
    assert questions[0].claim_ids == ("claim-ac-1",)


# ---------------------------------------------------------------------------
# draft_from_intake
# ---------------------------------------------------------------------------


def test_draft_from_intake_maps_goal_criteria_constraints() -> None:
    draft = draft_from_intake(_make_ready_draft(), _scope())
    assert draft.objective == "Bound memory growth"
    assert draft.acceptance_criteria == ("RSS <= 256MB",)
    assert draft.constraints == ("Max latency 5ms",)
    assert draft.project_id == PROJECT
    assert draft.repositories == ("repo-a",)
    assert draft.budget_policy == _budget()


@pytest.mark.parametrize(
    "reserved",
    [
        {"objective": "taken"},
        {"acceptance_criteria": ("taken",)},
        {"constraints": ("taken",)},
    ],
)
def test_draft_from_intake_refuses_reserved_scope_fields(
    reserved: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        draft_from_intake(_make_ready_draft(), _scope(**reserved))


# ---------------------------------------------------------------------------
# confirmation_route
# ---------------------------------------------------------------------------


def test_route_approved_mission_for_reordered_criteria() -> None:
    approved = _mission_draft().model_dump(mode="json")
    revised = _mission_draft(acceptance_criteria=("Latency <= 5ms", "RSS <= 256MB"))
    route = confirmation_route(approved, revised, None, None)
    assert route == Route(kind="approved_mission", cases=())


def test_route_owner_decision_for_new_without_mandate() -> None:
    route = confirmation_route(None, _mission_draft(), None, None)
    assert route.kind == "owner_decision"
    assert route.cases == ("new",)
    assert route.mandate_id is None


def test_route_standing_mandate_for_new_inside_mandate() -> None:
    route = confirmation_route(
        None,
        _mission_draft(),
        _covering_mandate(),
        Decimal("100.00"),
    )
    assert route.kind == "standing_mandate"
    assert route.cases == ("new",)
    assert route.mandate_id == str(MANDATE_ID)


def test_route_owner_decision_for_repository_outside_mandate() -> None:
    route = confirmation_route(
        None,
        _mission_draft(repositories=("repo-b",)),
        _covering_mandate(),
        Decimal("100.00"),
    )
    assert route.kind == "owner_decision"
    assert route.cases == ("new",)
    assert route.mandate_id is None


def test_route_owner_decision_for_added_criterion_without_mandate() -> None:
    approved = _mission_draft().model_dump(mode="json")
    revised = _mission_draft(
        acceptance_criteria=("RSS <= 256MB", "Latency <= 5ms", "P99 <= 10ms"),
    )
    route = confirmation_route(approved, revised, None, None)
    assert route.kind == "owner_decision"
    assert route.cases == ("a",)
