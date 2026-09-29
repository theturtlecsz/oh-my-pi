"""Mission budget admission gate and intake draft budget assignment.

A mission's budget_policy provides the item's intake budget. A mission without
a budget_policy is held with a durable decision record, and no default budget
is invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from omp_work.v1.models import BoundedIntakeDraft, ItemBudget

__all__ = [
    "MissionBudgetAdmission",
    "admit_mission_budget",
    "mission_intake_draft",
]


@dataclass(frozen=True)
class MissionBudgetAdmission:
    state: Literal["admitted", "held"]
    budget: ItemBudget | None = None
    decision: dict[str, Any] | None = None


_BUDGET_OPTIONS = ["set a budget", "cancel the mission"]


def admit_mission_budget(mission: Mapping[str, Any]) -> MissionBudgetAdmission:
    """Evaluate a mission's budget_policy for admission into bounded intake.

    If budget_policy is present and non-None, validates it as an ItemBudget
    (raising pydantic.ValidationError if malformed) and returns state="admitted".
    If budget_policy is missing or None, returns state="held" with budget=None
    and an OMP-414 decision record dictionary.
    """
    budget_policy = mission.get("budget_policy")
    if budget_policy is not None:
        budget = ItemBudget.model_validate(budget_policy)
        return MissionBudgetAdmission(state="admitted", budget=budget, decision=None)

    mission_id = mission.get("mission_id")
    mission_id_str = str(mission_id) if mission_id is not None else ""
    project_id = mission.get("project_id")
    decision_id = uuid5(NAMESPACE_URL, "omp-work:mission-budget:" + mission_id_str)

    options = list(_BUDGET_OPTIONS)
    decision: dict[str, Any] = {
        "decision_id": decision_id,
        "project_id": project_id,
        "mission_id": mission_id,
        "question": "Mission has no budget policy. Set an intake budget or cancel the mission?",
        "why_it_matters": "A mission cannot publish bounded intake without an approved budget policy.",
        "options": options,
        "evidence_refs": [],
        "default_if_any": None,
        "risk_of_delay": "Execution remains held and no work items can be published until a budget is set or the mission is cancelled.",
        "risk_of_each_choice": {
            "set a budget": "Commits funds and resource limits to this mission.",
            "cancel the mission": "Abandons the mission objective without taking action.",
        },
    }
    return MissionBudgetAdmission(state="held", budget=None, decision=decision)


def mission_intake_draft(
    admission: MissionBudgetAdmission | Mapping[str, Any],
    draft: BoundedIntakeDraft | None,
) -> BoundedIntakeDraft | None:
    """Assign the admitted mission budget to a draft, or return None if held.

    When admitted, returns a new BoundedIntakeDraft with budget equal to the
    admission's budget, overriding any existing budget on the draft.
    When held, returns None, ensuring nothing reaches publish and no item is minted.
    """
    if draft is None:
        return None

    state = getattr(admission, "state", None)
    if state is None and isinstance(admission, Mapping):
        state = admission.get("state")

    if state == "admitted":
        budget = getattr(admission, "budget", None)
        if budget is None and isinstance(admission, Mapping):
            budget = admission.get("budget")
        return draft.model_copy(update={"budget": budget})

    return None
