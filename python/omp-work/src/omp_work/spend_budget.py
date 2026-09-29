"""Spend budget classification and authorization (OMP-418)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from omp_work.standing_policy import ActionRequest, StandingPolicy, covers

__all__ = [
    "SpendBudget",
    "SpendDecision",
    "authorize_spend",
    "budget_change_kind",
    "classify_spend",
    "effective_budgets",
]

SpendTier = Literal["tier1", "tier2", "over_ceiling"]
SpendCode = Literal[
    "budget_decision_required",
    "ceiling_exceeded",
    "standing_policy_required",
]
BudgetChangeKind = Literal["create", "widen", "narrow"]


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class SpendBudget:
    """A spend cap. ``threshold_usd`` None means the effective threshold is the ceiling."""

    budget_id: str
    ceiling_usd: Decimal
    threshold_usd: Decimal | None = None

    def __post_init__(self) -> None:
        ceiling = _as_decimal(self.ceiling_usd)
        object.__setattr__(self, "ceiling_usd", ceiling)
        if ceiling <= 0:
            raise ValueError("ceiling_usd must be greater than 0")
        if self.threshold_usd is None:
            return
        threshold = _as_decimal(self.threshold_usd)
        if threshold > ceiling:
            raise ValueError("threshold_usd must not exceed ceiling_usd")
        object.__setattr__(self, "threshold_usd", threshold)

    @property
    def effective_threshold_usd(self) -> Decimal:
        if self.threshold_usd is None:
            return self.ceiling_usd
        return self.threshold_usd


@dataclass(frozen=True)
class SpendDecision:
    allowed: bool
    tier: SpendTier | None
    policy_id: UUID | str | None
    code: SpendCode | None


def classify_spend(
    b: SpendBudget,
    spent: object,
    amount: object,
) -> SpendTier:
    """Classify spent+amount against the budget's effective threshold and ceiling."""
    total = _as_decimal(spent) + _as_decimal(amount)
    if total <= b.effective_threshold_usd:
        return "tier1"
    if total <= b.ceiling_usd:
        return "tier2"
    return "over_ceiling"


def effective_budgets(
    mission_b: SpendBudget | None,
    project_b: SpendBudget | None,
) -> tuple[SpendBudget, ...]:
    """Return the mission budget, the project budget, both, or neither."""
    found: list[SpendBudget] = []
    if mission_b is not None:
        found.append(mission_b)
    if project_b is not None:
        found.append(project_b)
    return tuple(found)


def authorize_spend(
    budgets: Sequence[SpendBudget],
    spent_by_id: Mapping[Any, object],
    amount: object,
    policies: Sequence[StandingPolicy],
    repos: Any,
    now: datetime,
) -> SpendDecision:
    """Authorize a spend against each budget's own spent total.

    No budgets yields budget_decision_required. Any budget over its ceiling
    yields ceiling_exceeded, whether or not a standing policy would cover the
    amount. Tier2 is allowed only when a policy covers
    ActionRequest("spend_beyond_threshold", amount_usd=amount): class, expiry,
    and amount <= money_limit_usd. The decision names that policy_id.
    Otherwise the code is standing_policy_required. Tier1 needs no policy.
    """
    budget_list = tuple(budgets)
    if not budget_list:
        return SpendDecision(False, None, None, "budget_decision_required")

    amount_dec = _as_decimal(amount)
    tier2 = False
    for budget in budget_list:
        spent = _as_decimal(spent_by_id.get(budget.budget_id, 0))
        kind = classify_spend(budget, spent, amount_dec)
        if kind == "over_ceiling":
            return SpendDecision(False, "over_ceiling", None, "ceiling_exceeded")
        if kind == "tier2":
            tier2 = True

    if tier2:
        action = ActionRequest("spend_beyond_threshold", amount_usd=amount_dec)
        for policy in policies:
            if covers(policy, action, repos, now):
                return SpendDecision(True, "tier2", policy.policy_id, None)
        return SpendDecision(False, "tier2", None, "standing_policy_required")

    return SpendDecision(True, "tier1", None, None)


def budget_change_kind(old: SpendBudget | None, new: SpendBudget) -> BudgetChangeKind:
    """Create when old is absent; widen when the ceiling or effective threshold rises."""
    if old is None:
        return "create"
    if (
        new.ceiling_usd > old.ceiling_usd
        or new.effective_threshold_usd > old.effective_threshold_usd
    ):
        return "widen"
    return "narrow"
