from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from omp_work.routing.policy import (
    AlternateRule,
    AlternateTarget,
    RoutingRefused,
    load_policy,
)
from omp_work.routing.router import route_mission, route_stage


def test_engineering_execute_yields_stage_routes():
    policy = load_policy()
    routes = route_mission(policy, {"kind": "engineering.execute"})
    assert len(routes) == 5
    assert [route.stage for route in routes] == list(policy.missions["engineering.execute"])

    for route in routes:
        spec = policy.stages[route.stage]
        record = route.record()
        assert record["rule"] == spec.rule
        assert record["provider"] == spec.provider
        assert record["model"] == spec.model
        assert record["effort"] == spec.effort
        assert record["attempt"] == 1
        assert record["applied"] == []
        if spec.effort == "E1":
            assert record["effort_reason"] is None
        else:
            assert record["effort_reason"] == f"policy rule {spec.rule}"

    implement = routes[1].record()
    assert implement["stage"] == "implement"
    assert implement["rule"] == "omp-241-implement"
    assert implement["provider"] == "gemini_flash"
    assert implement["model"] == "gemini-3.8-flash-high"
    assert implement["effort"] == "E1"


@pytest.mark.parametrize(
    "mission",
    [
        {"kind": "engineering.execute", "model": "gpt-x"},
        {"kind": "engineering.execute", "provider": "anthropic"},
        {"kind": "engineering.execute", "task": {"model": "gpt-x"}},
        {"kind": "engineering.execute", "steps": [{"provider": "kimi"}]},
        {"kind": "nope", "nested": {"deep": {"model": "x"}}},
    ],
)
def test_caller_named_model_or_provider_is_refused(mission):
    with pytest.raises(RoutingRefused) as exc:
        route_mission(load_policy(), mission)
    assert exc.value.code == "caller_named_model"


def test_model_mentioned_only_as_a_value_is_routed():
    routes = route_mission(
        load_policy(),
        {"kind": "engineering.execute", "note": "pick a model", "models": ["unused"]},
    )
    assert len(routes) == 5


def test_unknown_mission_kind_is_refused():
    with pytest.raises(RoutingRefused) as exc:
        route_mission(load_policy(), {"kind": "engineering.nope"})
    assert exc.value.code == "unknown_mission_kind"


def test_missing_kind_is_refused():
    with pytest.raises(RoutingRefused) as exc:
        route_mission(load_policy(), {})
    assert exc.value.code == "unknown_mission_kind"


def test_unknown_stage_is_refused():
    with pytest.raises(RoutingRefused) as exc:
        route_stage(load_policy(), "deploy")
    assert exc.value.code == "unknown_stage"


def test_gemini_flash_at_cap_routes_implement_to_ollama():
    policy = load_policy()
    route = route_stage(policy, "implement", provider_in_flight={"gemini_flash": 6})
    record = route.record()
    assert record["provider"] == "ollama_cloud"
    assert record["model"] == "deepseek-v4.1-flash:cloud"
    assert record["rule"] == "omp-241-implement"
    assert record["effort"] == "E1"
    assert record["effort_reason"] is None
    assert record["applied"] == ["gemini-flash-overflow"]
    assert route.applied == ("gemini-flash-overflow",)


def test_under_cap_stays_on_the_stage_provider():
    route = route_stage(load_policy(), "implement", provider_in_flight={"gemini_flash": 5})
    assert route.provider == "gemini_flash"
    assert route.model == "gemini-3.8-flash-high"
    assert route.applied == ()


def test_every_target_saturated_is_refused():
    with pytest.raises(RoutingRefused) as exc:
        route_stage(
            load_policy(),
            "implement",
            provider_in_flight={"gemini_flash": 6, "ollama_cloud": 4},
        )
    assert exc.value.code == "provider_saturated"


def test_saturated_provider_does_not_take_another_providers_alternate():
    with pytest.raises(RoutingRefused) as exc:
        route_stage(load_policy(), "audit", provider_in_flight={"kimi": 2})
    assert exc.value.code == "provider_saturated"


def test_full_alternate_is_skipped_for_the_next_with_room():
    policy = load_policy()
    routed = replace(
        policy,
        alternates=(
            AlternateRule(
                rule="gemini-to-kimi",
                from_provider="gemini_flash",
                to=AlternateTarget(provider="kimi", model="kimi-full"),
            ),
            policy.alternates[0],
        ),
    )
    route = route_stage(
        routed,
        "implement",
        provider_in_flight={"gemini_flash": 6, "kimi": 2},
    )
    assert route.provider == "ollama_cloud"
    assert route.model == "deepseek-v4.1-flash:cloud"
    assert route.applied == ("gemini-flash-overflow",)


def test_mission_passes_in_flight_to_each_stage():
    routes = route_mission(
        load_policy(),
        {"kind": "engineering.execute"},
        provider_in_flight={"gemini_flash": 6},
    )
    by_stage = {route.stage: route for route in routes}
    assert by_stage["plan"].provider == "openai_codex"
    assert by_stage["plan"].applied == ()
    assert by_stage["implement"].provider == "ollama_cloud"
    assert by_stage["implement"].applied == ("gemini-flash-overflow",)
    assert by_stage["verify"].provider == "ollama_cloud"
    assert by_stage["verify"].applied == ("gemini-flash-overflow",)
    assert by_stage["audit"].provider == "kimi"

    with pytest.raises(RoutingRefused) as exc:
        route_mission(
            load_policy(),
            {"kind": "engineering.execute"},
            provider_in_flight={"gemini_flash": 6, "ollama_cloud": 4},
        )
    assert exc.value.code == "provider_saturated"


def test_route_functions_do_not_accept_a_model():
    for fn in (route_stage, route_mission):
        assert "model" not in inspect.signature(fn).parameters
