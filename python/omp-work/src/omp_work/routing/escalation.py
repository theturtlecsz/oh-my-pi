"""Retries and automatic escalation by policy, within the item budget."""

from __future__ import annotations

from omp_work.routing.policy import (
    RoutingPolicy,
    RoutingRefused,
    StageRoute,
)
from omp_work.v1.models import ItemBudget


def next_route(
    policy: RoutingPolicy,
    route: StageRoute,
    *,
    failed_attempts: int,
    review_rejected: bool,
    item_budget: ItemBudget | None,
    spent_tokens: int,
) -> StageRoute:
    """Determine next route for a stage following failure or review rejection."""
    if item_budget is None:
        raise RoutingRefused("no_item_budget")

    stage_rule = policy.stages.get(route.stage)
    max_retries = stage_rule.max_retries if stage_rule is not None else 0

    if not review_rejected and failed_attempts <= max_retries:
        return StageRoute(
            stage=route.stage,
            rule=route.rule,
            provider=route.provider,
            model=route.model,
            effort=route.effort,
            attempt=route.attempt + 1,
            applied=(*route.applied, policy.retry.rule),
            effort_reason=route.effort_reason,
        )

    trigger = "review_rejected" if review_rejected else "failed_attempts"
    esc_rule = next((r for r in policy.escalation if r.on == trigger), None)
    if esc_rule is None:
        raise RoutingRefused(f"no_escalation_rule_for_{trigger}")

    if route.effort not in policy.ladder:
        raise RoutingRefused("unknown_effort")

    current_idx = policy.ladder.index(route.effort)
    target_idx = current_idx + esc_rule.step

    if target_idx >= len(policy.ladder) or target_idx > policy.ladder.index("E4"):
        raise RoutingRefused("no_higher_tier")

    target_effort = policy.ladder[target_idx]
    if target_effort not in policy.tiers:
        raise RoutingRefused("no_higher_tier")

    target_tier = policy.tiers[target_effort]
    remaining_tokens = item_budget.tokens - spent_tokens
    if target_tier.est_tokens > remaining_tokens:
        raise RoutingRefused("escalation_over_budget")

    effort_reason = f"escalation rule {esc_rule.rule} triggered by {trigger}"

    return StageRoute(
        stage=route.stage,
        rule=route.rule,
        provider=target_tier.provider,
        model=target_tier.model,
        effort=target_effort,
        attempt=route.attempt + 1,
        applied=(*route.applied, esc_rule.rule),
        effort_reason=effort_reason,
    )
