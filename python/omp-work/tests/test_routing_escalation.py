from __future__ import annotations

import pytest

from omp_work.economy_effort import validate_effort
from omp_work.routing.escalation import next_route
from omp_work.routing.policy import (
    RoutingPolicy,
    RoutingRefused,
    StageRoute,
    load_policy,
)
from omp_work.v1.models import ItemBudget


def _make_implement_route(policy: RoutingPolicy) -> StageRoute:
    stg = policy.stages["implement"]
    return StageRoute(
        stage="implement",
        rule=stg.rule,
        provider=stg.provider,
        model=stg.model,
        effort=stg.effort,
    )


def _make_budget(tokens: int = 500000) -> ItemBudget:
    return ItemBudget(
        usd="10.00",
        tokens=tokens,
        wall_clock_seconds=3600,
        max_subagents=2,
    )


def test_2_failures_retry_at_e1():
    policy = load_policy()
    route = _make_implement_route(policy)
    budget = _make_budget()

    retried = next_route(
        policy,
        route,
        failed_attempts=2,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=10000,
    )

    assert retried.effort == "E1"
    assert retried.provider == "gemini_flash"
    assert retried.model == "gemini-3.8-flash-high"
    assert retried.attempt == 2
    assert retried.rule == "omp-241-implement"
    assert "retry-same-tier" in retried.record()["applied"]


def test_3_failures_escalate_to_e2():
    policy = load_policy()
    route = _make_implement_route(policy)
    budget = _make_budget()

    escalated = next_route(
        policy,
        route,
        failed_attempts=3,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=10000,
    )

    assert escalated.effort == "E2"
    assert escalated.provider == "anthropic_fable"
    assert escalated.model == "claude-opus-5-5"
    assert escalated.attempt == 2
    rec = escalated.record()
    assert "escalate-after-retries" in rec["applied"]
    assert escalated.effort_reason is not None
    assert len(escalated.effort_reason) >= 20
    assert "escalate-after-retries" in escalated.effort_reason
    assert "failed_attempts" in escalated.effort_reason


def test_review_rejected_escalates():
    policy = load_policy()
    route = _make_implement_route(policy)
    budget = _make_budget()

    escalated = next_route(
        policy,
        route,
        failed_attempts=1,
        review_rejected=True,
        item_budget=budget,
        spent_tokens=10000,
    )

    assert escalated.effort == "E2"
    assert escalated.provider == "anthropic_fable"
    assert escalated.model == "claude-opus-5-5"
    assert escalated.attempt == 2
    rec = escalated.record()
    assert "escalate-on-review-reject" in rec["applied"]
    assert escalated.effort_reason is not None
    assert len(escalated.effort_reason) >= 20
    assert "escalate-on-review-reject" in escalated.effort_reason
    assert "review_rejected" in escalated.effort_reason


def test_escalation_over_budget():
    policy = load_policy()
    route = _make_implement_route(policy)
    # E2 requires 80,000 tokens. With budget 100,000 and spent 30,000, remaining is 70,000.
    budget = _make_budget(tokens=100000)

    with pytest.raises(RoutingRefused) as excinfo:
        next_route(
            policy,
            route,
            failed_attempts=3,
            review_rejected=False,
            item_budget=budget,
            spent_tokens=30000,
        )
    assert excinfo.value.code == "escalation_over_budget"

    # Boundary test: exactly enough remaining tokens succeeds
    ok_route = next_route(
        policy,
        route,
        failed_attempts=3,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=20000,  # 100,000 - 20,000 = 80,000 == E2 est_tokens
    )
    assert ok_route.effort == "E2"

    # 1 token short raises
    with pytest.raises(RoutingRefused) as excinfo_short:
        next_route(
            policy,
            route,
            failed_attempts=3,
            review_rejected=False,
            item_budget=budget,
            spent_tokens=20001,  # 79,999 < 80,000
        )
    assert excinfo_short.value.code == "escalation_over_budget"


def test_e4_route_raises_no_higher_tier():
    policy = load_policy()
    e4_tier = policy.tiers["E4"]
    stg = policy.stages["implement"]
    e4_route = StageRoute(
        stage="implement",
        rule=stg.rule,
        provider=e4_tier.provider,
        model=e4_tier.model,
        effort="E4",
        effort_reason="initial e4 reason with sufficient length",
    )
    budget = _make_budget()

    # Failed retries at E4
    with pytest.raises(RoutingRefused) as excinfo:
        next_route(
            policy,
            e4_route,
            failed_attempts=3,
            review_rejected=False,
            item_budget=budget,
            spent_tokens=10000,
        )
    assert excinfo.value.code == "no_higher_tier"

    # Review rejected at E4
    with pytest.raises(RoutingRefused) as excinfo_rev:
        next_route(
            policy,
            e4_route,
            failed_attempts=1,
            review_rejected=True,
            item_budget=budget,
            spent_tokens=10000,
        )
    assert excinfo_rev.value.code == "no_higher_tier"


def test_none_budget_raises_no_item_budget():
    policy = load_policy()
    route = _make_implement_route(policy)

    with pytest.raises(RoutingRefused) as excinfo:
        next_route(
            policy,
            route,
            failed_attempts=1,
            review_rejected=False,
            item_budget=None,
            spent_tokens=0,
        )
    assert excinfo.value.code == "no_item_budget"

    with pytest.raises(RoutingRefused) as excinfo_esc:
        next_route(
            policy,
            route,
            failed_attempts=3,
            review_rejected=False,
            item_budget=None,
            spent_tokens=0,
        )
    assert excinfo_esc.value.code == "no_item_budget"


def test_escalation_reason_satisfies_economy_gate():
    policy = load_policy()
    stg = policy.stages["implement"]
    e3_tier = policy.tiers["E3"]
    e3_route = StageRoute(
        stage="implement",
        rule=stg.rule,
        provider=e3_tier.provider,
        model=e3_tier.model,
        effort="E3",
        effort_reason="tier e3 requires valid reason",
    )
    budget = _make_budget(tokens=500000)

    # Escalating from E3 lands on E4
    e4_route = next_route(
        policy,
        e3_route,
        failed_attempts=3,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=0,
    )
    assert e4_route.effort == "E4"
    verdict = validate_effort(e4_route.effort, e4_route.effort_reason)
    assert verdict["ok"] is True
    assert verdict["effort"] == "E4"


def test_sequential_retries_then_escalation():
    policy = load_policy()
    budget = _make_budget()

    r1 = _make_implement_route(policy)
    assert r1.attempt == 1
    assert r1.applied == ()

    # 1st failure -> retry 1
    r2 = next_route(
        policy,
        r1,
        failed_attempts=1,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=5000,
    )
    assert r2.attempt == 2
    assert r2.effort == "E1"
    assert r2.applied == ("retry-same-tier",)

    # 2nd failure -> retry 2
    r3 = next_route(
        policy,
        r2,
        failed_attempts=2,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=10000,
    )
    assert r3.attempt == 3
    assert r3.effort == "E1"
    assert r3.applied == ("retry-same-tier", "retry-same-tier")

    # 3rd failure -> escalate to E2
    r4 = next_route(
        policy,
        r3,
        failed_attempts=3,
        review_rejected=False,
        item_budget=budget,
        spent_tokens=15000,
    )
    assert r4.attempt == 4
    assert r4.effort == "E2"
    assert r4.applied == ("retry-same-tier", "retry-same-tier", "escalate-after-retries")
