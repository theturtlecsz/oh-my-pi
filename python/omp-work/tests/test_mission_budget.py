"""OMP-404-s03: Mission budget gate and intake draft assignment tests.

Contract guarantees tested:
- A mission without a budget_policy, or with budget_policy=None, is held with an
  OMP-414 decision record and no budget is invented.
- The decision record includes every OMP-414 field (decision_id, project_id,
  mission_id, question, why_it_matters, options, evidence_refs, default_if_any,
  risk_of_delay, risk_of_each_choice) with a non-empty risk per option and
  default_if_any=None.
- The same mission_id deterministically yields the same decision_id via uuid5.
- A held admission causes mission_intake_draft to return None, preventing
  bounded intake publication without a budget.
- A valid budget_policy is admitted, and mission_intake_draft sets the draft
  budget to the admitted mission budget, overriding any prior budget.
- A malformed budget_policy raises pydantic.ValidationError at admission.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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

_REQUIRED_DECISION_FIELDS = {
    "decision_id",
    "project_id",
    "mission_id",
    "question",
    "why_it_matters",
    "options",
    "evidence_refs",
    "default_if_any",
    "risk_of_delay",
    "risk_of_each_choice",
}


def _sample_draft(budget: ItemBudget | None = None) -> BoundedIntakeDraft:
    source_text = "sample intake source"
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


def test_mission_without_budget_policy_is_held() -> None:
    mission_id = f"mission-{uuid4().hex[:8]}"
    project_id = f"proj-{uuid4().hex[:8]}"
    mission = {
        "mission_id": mission_id,
        "project_id": project_id,
        "objective": "Build autonomous feature",
    }

    admission = admit_mission_budget(mission)

    assert admission.state == "held"
    assert admission.budget is None
    assert isinstance(admission.decision, dict)

    decision = admission.decision
    assert _REQUIRED_DECISION_FIELDS.issubset(decision.keys())
    assert decision["project_id"] == project_id
    assert decision["mission_id"] == mission_id
    assert decision["decision_id"] == uuid5(NAMESPACE_URL, f"omp-work:mission-budget:{mission_id}")
    assert isinstance(decision["decision_id"], UUID)

    assert isinstance(decision["question"], str) and len(decision["question"]) > 0
    assert isinstance(decision["why_it_matters"], str) and len(decision["why_it_matters"]) > 0
    assert isinstance(decision["options"], list) and len(decision["options"]) == 2
    assert "set a budget" in decision["options"]
    assert "cancel the mission" in decision["options"]

    assert decision["evidence_refs"] == []
    assert decision["default_if_any"] is None
    assert isinstance(decision["risk_of_delay"], str) and len(decision["risk_of_delay"]) > 0

    assert isinstance(decision["risk_of_each_choice"], dict)
    for option in decision["options"]:
        assert option in decision["risk_of_each_choice"]
        risk = decision["risk_of_each_choice"][option]
        assert isinstance(risk, str) and len(risk.strip()) > 0

    # Held admission produces no intake draft
    draft = _sample_draft()
    assert mission_intake_draft(admission, draft) is None


def test_mission_with_budget_policy_none_is_held() -> None:
    mission_id = f"mission-{uuid4().hex[:8]}"
    project_id = f"proj-{uuid4().hex[:8]}"
    mission = {
        "mission_id": mission_id,
        "project_id": project_id,
        "objective": "Build autonomous feature",
        "budget_policy": None,
    }

    admission = admit_mission_budget(mission)

    assert admission.state == "held"
    assert admission.budget is None
    assert isinstance(admission.decision, dict)

    decision = admission.decision
    assert _REQUIRED_DECISION_FIELDS.issubset(decision.keys())
    assert decision["default_if_any"] is None
    assert decision["evidence_refs"] == []
    for option in decision["options"]:
        risk = decision["risk_of_each_choice"][option]
        assert isinstance(risk, str) and len(risk.strip()) > 0

    # Existing draft with budget is also suppressed when held
    prior_budget = ItemBudget(
        usd="10.00",
        tokens=100_000,
        wall_clock_seconds=600,
        max_subagents=1,
    )
    draft = _sample_draft(budget=prior_budget)
    assert mission_intake_draft(admission, draft) is None


def test_deterministic_decision_id_for_same_mission_id() -> None:
    mission_id = "mission-fixed-id-42"
    m1 = {"mission_id": mission_id, "project_id": "p1", "objective": "obj1"}
    m2 = {"mission_id": mission_id, "project_id": "p2", "objective": "obj2"}

    adm1 = admit_mission_budget(m1)
    adm2 = admit_mission_budget(m2)

    assert adm1.decision is not None and adm2.decision is not None
    assert adm1.decision["decision_id"] == adm2.decision["decision_id"]
    assert adm1.decision["decision_id"] == uuid5(NAMESPACE_URL, f"omp-work:mission-budget:{mission_id}")

    # Different mission_id yields different decision_id
    adm3 = admit_mission_budget({"mission_id": "mission-other-id", "project_id": "p1"})
    assert adm3.decision is not None
    assert adm3.decision["decision_id"] != adm1.decision["decision_id"]


def test_valid_budget_policy_admitted_and_overrides_draft_budget() -> None:
    mission_id = "mission-valid-budget"
    policy_dict = {
        "usd": "75.00",
        "tokens": 800_000,
        "wall_clock_seconds": 1800,
        "max_subagents": 3,
    }
    mission = {
        "mission_id": mission_id,
        "project_id": "proj-abc",
        "objective": "Objective with valid budget",
        "budget_policy": policy_dict,
    }

    admission = admit_mission_budget(mission)

    assert admission.state == "admitted"
    assert admission.decision is None
    assert admission.budget == ItemBudget.model_validate(policy_dict)

    # 1. Draft without existing budget receives mission budget
    draft_no_budget = _sample_draft(budget=None)
    res1 = mission_intake_draft(admission, draft_no_budget)
    assert res1 is not None
    assert res1.budget == admission.budget

    # 2. Draft with different existing budget is overridden by mission budget
    different_prior_budget = ItemBudget(
        usd="10.00",
        tokens=50_000,
        wall_clock_seconds=300,
        max_subagents=0,
    )
    draft_with_budget = _sample_draft(budget=different_prior_budget)
    res2 = mission_intake_draft(admission, draft_with_budget)
    assert res2 is not None
    assert res2.budget == admission.budget
    assert res2.budget != different_prior_budget


def test_valid_budget_policy_instance_admitted() -> None:
    mission_budget = ItemBudget(
        usd="120.00",
        tokens=1_200_000,
        wall_clock_seconds=7200,
        max_subagents=4,
    )
    mission = {
        "mission_id": "mission-instance",
        "project_id": "proj-xyz",
        "objective": "Objective with ItemBudget instance",
        "budget_policy": mission_budget,
    }

    admission = admit_mission_budget(mission)

    assert admission.state == "admitted"
    assert admission.budget == mission_budget
    assert admission.decision is None


@pytest.mark.parametrize(
    "malformed_policy",
    [
        {},  # empty dict, missing all fields
        {"usd": "50.00", "tokens": 500_000, "wall_clock_seconds": 3600},  # missing max_subagents
        {"usd": "-1.00", "tokens": 100, "wall_clock_seconds": 100, "max_subagents": 0},  # negative usd
        {"usd": "0.00", "tokens": 100, "wall_clock_seconds": 100, "max_subagents": 0},  # zero usd
        {"usd": "NaN", "tokens": 100, "wall_clock_seconds": 100, "max_subagents": 0},  # invalid decimal
        {"usd": "50.00", "tokens": 0, "wall_clock_seconds": 100, "max_subagents": 0},  # zero tokens (must be gt=0)
        {"usd": "50.00", "tokens": -10, "wall_clock_seconds": 100, "max_subagents": 0},  # negative tokens
        {"usd": "50.00", "tokens": 100, "wall_clock_seconds": 0, "max_subagents": 0},  # zero wall_clock_seconds
        {"usd": "50.00", "tokens": 100, "wall_clock_seconds": -5, "max_subagents": 0},  # negative wall_clock_seconds
        {"usd": "50.00", "tokens": 100, "wall_clock_seconds": 100, "max_subagents": -1},  # negative max_subagents
        {"usd": 50.0, "tokens": 100, "wall_clock_seconds": 100, "max_subagents": 0},  # float usd (must be str)
        {"usd": True, "tokens": 100, "wall_clock_seconds": 100, "max_subagents": 0},  # bool usd
        {"usd": "50.00", "tokens": True, "wall_clock_seconds": 100, "max_subagents": 0},  # bool tokens
        "not-a-dict",  # non-dict
        12345,  # non-dict number
    ],
)
def test_malformed_budget_policy_raises_validation_error(malformed_policy: Any) -> None:
    mission = {
        "mission_id": "mission-bad-budget",
        "project_id": "proj-bad",
        "objective": "Objective with bad budget",
        "budget_policy": malformed_policy,
    }
    with pytest.raises(ValidationError):
        admit_mission_budget(mission)
