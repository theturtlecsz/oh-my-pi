"""Per-stage routing from the written routing policy.

Provider and model come from the stage rule. No argument accepts a model.
When the stage provider is at its concurrency cap, the first alternate from
that provider whose target still has room is used.
"""

from __future__ import annotations

from typing import Any, Mapping

from omp_work.routing.policy import AlternateRule, RoutingPolicy, RoutingRefused, StageRoute

_CALLER_ROUTE_KEYS = frozenset({"model", "provider"})


def route_stage(
    policy: RoutingPolicy,
    stage: str,
    *,
    provider_in_flight: Mapping[str, int] | None = None,
) -> StageRoute:
    """Route ``stage`` at attempt 1.

    Unknown stages raise ``RoutingRefused("unknown_stage")``. A supplied
    in-flight count at or over the stage provider's cap selects the first
    alternate from that provider whose target is under its cap, and appends
    that alternate's rule to ``applied``. When every such target is at cap,
    raises ``RoutingRefused("provider_saturated")``. Non-E1 routes set
    ``effort_reason`` to ``policy rule {rule}`` using the stage rule.
    """
    spec = policy.stages.get(stage)
    if spec is None:
        raise RoutingRefused("unknown_stage")

    provider = spec.provider
    model = spec.model
    applied: tuple[str, ...] = ()
    if _at_cap(policy, provider, provider_in_flight):
        alternate = _alternate_with_room(policy, provider, provider_in_flight)
        if alternate is None:
            raise RoutingRefused("provider_saturated")
        provider = alternate.to.provider
        model = alternate.to.model
        applied = (alternate.rule,)

    return StageRoute(
        stage=stage,
        rule=spec.rule,
        provider=provider,
        model=model,
        effort=spec.effort,
        attempt=1,
        applied=applied,
        effort_reason=None if spec.effort == "E1" else f"policy rule {spec.rule}",
    )


def route_mission(
    policy: RoutingPolicy,
    request: Mapping[str, Any],
    *,
    provider_in_flight: Mapping[str, int] | None = None,
) -> list[StageRoute]:
    """Return one route per stage of ``request["kind"]``, in policy order.

    When ``policy.caller_model_fields`` is ``"refuse"``, a ``model`` or
    ``provider`` key anywhere in ``request`` raises
    ``RoutingRefused("caller_named_model")`` before any stage is routed.
    An unknown kind raises ``RoutingRefused("unknown_mission_kind")``.
    """
    if policy.caller_model_fields == "refuse" and _has_caller_route_key(request):
        raise RoutingRefused("caller_named_model")
    kind = request["kind"] if "kind" in request else None
    if kind not in policy.missions:
        raise RoutingRefused("unknown_mission_kind")
    return [
        route_stage(policy, stage, provider_in_flight=provider_in_flight)
        for stage in policy.missions[kind]
    ]


def _supplied_count(provider_in_flight: Mapping[str, int] | None, provider: str) -> int | None:
    if provider_in_flight is None or provider not in provider_in_flight:
        return None
    return provider_in_flight[provider]


def _at_cap(
    policy: RoutingPolicy,
    provider: str,
    provider_in_flight: Mapping[str, int] | None,
) -> bool:
    count = _supplied_count(provider_in_flight, provider)
    if count is None:
        return False
    return count >= policy.concurrency.providers[provider]


def _alternate_with_room(
    policy: RoutingPolicy,
    provider: str,
    provider_in_flight: Mapping[str, int] | None,
) -> AlternateRule | None:
    for alternate in policy.alternates:
        if alternate.from_provider != provider:
            continue
        if not _at_cap(policy, alternate.to.provider, provider_in_flight):
            return alternate
    return None


def _has_caller_route_key(node: object) -> bool:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in _CALLER_ROUTE_KEYS or _has_caller_route_key(value):
                return True
        return False
    if isinstance(node, (list, tuple)):
        return any(_has_caller_route_key(item) for item in node)
    return False
