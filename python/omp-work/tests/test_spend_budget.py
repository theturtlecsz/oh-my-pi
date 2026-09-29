"""Tests for spend budget classification and authorization (OMP-418)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from omp_work.spend_budget import (
    SpendBudget,
    SpendDecision,
    authorize_spend,
    budget_change_kind,
    classify_spend,
    effective_budgets,
)
from omp_work.standing_policy import StandingPolicy

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _policy(policy_id: str, limit: str, *, expires_at: datetime | None = None) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal(limit),
        expires_at=expires_at,
        decision_id="dec-1",
    )


def test_no_threshold_tier1_through_ceiling_refused_above() -> None:
    budget = SpendBudget("b", Decimal("100"))
    assert budget.threshold_usd is None
    assert budget.effective_threshold_usd == Decimal("100")

    assert classify_spend(budget, Decimal("0"), Decimal("100")) == "tier1"
    assert classify_spend(budget, Decimal("40"), Decimal("60")) == "tier1"
    within = authorize_spend(
        (budget,),
        {"b": Decimal("40")},
        Decimal("60"),
        [_policy("pol-unused", "1000")],
        {},
        NOW,
    )
    assert within == SpendDecision(True, "tier1", None, None)

    assert classify_spend(budget, Decimal("40"), Decimal("60.01")) == "over_ceiling"
    refused = authorize_spend(
        (budget,),
        {"b": Decimal("40")},
        Decimal("60.01"),
        [],
        {},
        NOW,
    )
    assert refused == SpendDecision(False, "over_ceiling", None, "ceiling_exceeded")


def test_lower_threshold_tier1_through_it() -> None:
    budget = SpendBudget("b", Decimal("100"), Decimal("40"))
    assert classify_spend(budget, Decimal("0"), Decimal("40")) == "tier1"
    assert classify_spend(budget, Decimal("15"), Decimal("25")) == "tier1"
    decision = authorize_spend(
        (budget,),
        {"b": Decimal("15")},
        Decimal("25"),
        [],
        {},
        NOW,
    )
    assert decision == SpendDecision(True, "tier1", None, None)


def test_above_threshold_refused_without_policy() -> None:
    budget = SpendBudget("b", Decimal("100"), Decimal("40"))
    assert classify_spend(budget, Decimal("10"), Decimal("30.01")) == "tier2"
    assert classify_spend(budget, Decimal("0"), Decimal("100")) == "tier2"
    decision = authorize_spend(
        (budget,),
        {"b": Decimal("10")},
        Decimal("30.01"),
        [],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, "tier2", None, "standing_policy_required")


def test_tier2_allowed_with_covering_policy_names_it() -> None:
    budget = SpendBudget("b", Decimal("100"), Decimal("40"))
    covering = _policy("pol-cover", "50")
    decision = authorize_spend(
        (budget,),
        {"b": Decimal("0")},
        Decimal("50"),
        [_policy("pol-small", "10"), covering],
        {},
        NOW,
    )
    assert decision == SpendDecision(True, "tier2", "pol-cover", None)


def test_smaller_money_limit_does_not_cover() -> None:
    budget = SpendBudget("b", Decimal("100"), Decimal("40"))
    decision = authorize_spend(
        (budget,),
        {"b": Decimal("0")},
        Decimal("50"),
        [_policy("pol-small", "49.99")],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, "tier2", None, "standing_policy_required")


def test_above_ceiling_refused_under_policy() -> None:
    budget = SpendBudget("b", Decimal("100"), Decimal("40"))
    decision = authorize_spend(
        (budget,),
        {"b": Decimal("0")},
        Decimal("100.01"),
        [_policy("pol-cover", "1000")],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, "over_ceiling", None, "ceiling_exceeded")


def test_threshold_above_ceiling_rejected() -> None:
    with pytest.raises(ValueError):
        SpendBudget("b", Decimal("100"), Decimal("100.01"))
    with pytest.raises(ValueError):
        SpendBudget("b", Decimal("0"))
    with pytest.raises(ValueError):
        SpendBudget("b", Decimal("-1"), Decimal("0"))
    at_ceiling = SpendBudget("b", Decimal("100"), Decimal("100"))
    assert at_ceiling.threshold_usd == Decimal("100")
    assert classify_spend(at_ceiling, Decimal("0"), Decimal("100")) == "tier1"


def test_tighter_mission_ceiling_refuses() -> None:
    mission = SpendBudget("mission", Decimal("80"))
    project = SpendBudget("project", Decimal("200"))
    budgets = effective_budgets(mission, project)
    assert budgets == (mission, project)
    decision = authorize_spend(
        budgets,
        {"mission": Decimal("30"), "project": Decimal("10")},
        Decimal("60"),
        [_policy("pol-cover", "60")],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, "over_ceiling", None, "ceiling_exceeded")


def test_tighter_project_ceiling_refuses() -> None:
    mission = SpendBudget("mission", Decimal("200"))
    project = SpendBudget("project", Decimal("80"))
    decision = authorize_spend(
        effective_budgets(mission, project),
        {"mission": Decimal("10"), "project": Decimal("30")},
        Decimal("60"),
        [_policy("pol-cover", "60")],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, "over_ceiling", None, "ceiling_exceeded")


def test_mission_without_budget_uses_project() -> None:
    project = SpendBudget("project", Decimal("80"), Decimal("30"))
    budgets = effective_budgets(None, project)
    assert budgets == (project,)
    within = authorize_spend(
        budgets,
        {"project": Decimal("10")},
        Decimal("20"),
        [],
        {},
        NOW,
    )
    assert within == SpendDecision(True, "tier1", None, None)
    above_project_ceiling = authorize_spend(
        budgets,
        {"project": Decimal("70")},
        Decimal("20"),
        [_policy("pol-cover", "20")],
        {},
        NOW,
    )
    assert above_project_ceiling == SpendDecision(
        False, "over_ceiling", None, "ceiling_exceeded"
    )


def test_neither_budget_requires_decision() -> None:
    assert effective_budgets(None, None) == ()
    decision = authorize_spend(
        effective_budgets(None, None),
        {},
        Decimal("25"),
        [_policy("pol-cover", "100")],
        {},
        NOW,
    )
    assert decision == SpendDecision(False, None, None, "budget_decision_required")


def test_budget_change_kinds() -> None:
    base = SpendBudget("b", Decimal("100"), Decimal("40"))
    assert budget_change_kind(None, base) == "create"
    assert budget_change_kind(base, SpendBudget("b", Decimal("120"), Decimal("40"))) == "widen"
    assert budget_change_kind(base, SpendBudget("b", Decimal("100"), Decimal("70"))) == "widen"
    assert budget_change_kind(base, SpendBudget("b", Decimal("90"), Decimal("40"))) == "narrow"
    assert budget_change_kind(base, SpendBudget("b", Decimal("100"), Decimal("25"))) == "narrow"

    implicit = SpendBudget("b", Decimal("100"))
    lowered = SpendBudget("b", Decimal("100"), Decimal("80"))
    assert budget_change_kind(implicit, lowered) == "narrow"
    assert budget_change_kind(lowered, implicit) == "widen"
