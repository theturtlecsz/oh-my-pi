"""OMP-430-s01: Mission budget admission under standing budget policy (A4/D39).

Contracts tested:
- standing only is admitted, and mission_intake_draft takes it
- neither is held (decision record generated, mission_intake_draft returns None)
- budget_policy beats standing
- malformed standing raises ValidationError
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omp_work.mission_budget import (
    MissionBudgetAdmission,
    admit_mission_budget,
    mission_intake_draft,
)
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    ItemBudget,
)

_VALID_STANDING = {
    "tokens": 40_000,
    "usd": "0.50",
    "wall_clock_seconds": 600,
    "max_subagents": 1,
}

_VALID_MISSION_POLICY = {
    "tokens": 80_000,
    "usd": "1.00",
    "wall_clock_seconds": 1200,
    "max_subagents": 2,
}


def _sample_draft(budget: ItemBudget | None = None) -> BoundedIntakeDraft:
    source_text = "sample intake source for standing budget tests"
    span = IntakeSourceSpan(
        id="span-1",
        start=0,
        end=len(source_text.encode("utf-8")),
        exact_text_sha256=text_sha256(source_text),
    )
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=IntakeSource(
            text=source_text,
            sha256=text_sha256(source_text),
            spans=(span,),
        ),
        goal=IntakeGoal(id="goal-1", statement="sample goal", source_span_ids=("span-1",)),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="ac-1",
                statement="sample criteria",
                observable_outcome="test passes",
                oracle="automated_test",
                source_span_ids=("span-1",),
            ),
        ),
        budget=budget,
    )


def test_standing_only_is_admitted_and_intake_draft_takes_it() -> None:
    mission = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
    }
    admission = admit_mission_budget(mission, standing_budget=_VALID_STANDING)

    assert admission.state == "admitted"
    assert admission.decision is None
    expected_budget = ItemBudget.model_validate(_VALID_STANDING)
    assert admission.budget == expected_budget

    draft = _sample_draft()
    updated = mission_intake_draft(admission, draft)
    assert updated is not None
    assert updated.budget == expected_budget


def test_neither_is_held() -> None:
    mission = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
        "budget_policy": None,
    }
    admission = admit_mission_budget(mission, standing_budget=None)

    assert admission.state == "held"
    assert admission.budget is None
    assert isinstance(admission.decision, dict)

    draft = _sample_draft()
    assert mission_intake_draft(admission, draft) is None


def test_budget_policy_beats_standing() -> None:
    mission = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
        "budget_policy": _VALID_MISSION_POLICY,
    }
    admission = admit_mission_budget(mission, standing_budget=_VALID_STANDING)

    assert admission.state == "admitted"
    assert admission.decision is None
    expected_budget = ItemBudget.model_validate(_VALID_MISSION_POLICY)
    assert admission.budget == expected_budget

    draft = _sample_draft()
    updated = mission_intake_draft(admission, draft)
    assert updated is not None
    assert updated.budget == expected_budget


def test_malformed_standing_raises_validation_error() -> None:
    mission = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
    }
    malformed = {"tokens": -10, "usd": "not-a-dollar-amount"}
    with pytest.raises(ValidationError):
        admit_mission_budget(mission, standing_budget=malformed)
