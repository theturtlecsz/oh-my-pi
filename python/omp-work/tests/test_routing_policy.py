from __future__ import annotations

import copy
import json
from pathlib import Path
import pytest

from omp_work.routing.policy import (
    RoutingPolicy,
    RoutingPolicyError,
    RoutingRefused,
    StageRoute,
    load_policy,
)


def _write_policy(tmp_path: Path, data: dict) -> Path:
    target = tmp_path / "routing-policy.v1.json"
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target


def test_packaged_policy_loads():
    policy = load_policy()
    assert isinstance(policy, RoutingPolicy)
    assert policy.version == "routing-policy.v1"
    assert policy.caller_model_fields == "refuse"
    assert policy.ladder == ("E0", "E1", "E2", "E3", "E4")

    # Tiers E1..E4
    assert set(policy.tiers.keys()) == {"E1", "E2", "E3", "E4"}
    assert policy.tiers["E1"].provider == "gemini_flash"
    assert policy.tiers["E1"].model == "gemini-3.8-flash-high"
    assert policy.tiers["E1"].est_tokens == 40000

    assert policy.tiers["E2"].provider == "anthropic_fable"
    assert policy.tiers["E2"].model == "claude-opus-5-5"
    assert policy.tiers["E2"].est_tokens == 80000

    assert policy.tiers["E3"].provider == "kimi"
    assert policy.tiers["E3"].model == "kimi-code/k3-256k"
    assert policy.tiers["E3"].est_tokens == 120000

    assert policy.tiers["E4"].provider == "chatgpt_astra"
    assert policy.tiers["E4"].model == "gpt-6-sol"
    assert policy.tiers["E4"].est_tokens == 200000

    # Concurrency
    assert policy.concurrency.in_flight_max == 10
    assert policy.concurrency.providers == {
        "gemini_flash": 6,
        "ollama_cloud": 4,
        "anthropic_fable": 2,
        "kimi": 2,
        "chatgpt_astra": 0,
        "openai_codex": 1,
    }

    # Alternates
    assert len(policy.alternates) == 1
    alt = policy.alternates[0]
    assert alt.rule == "gemini-flash-overflow"
    assert alt.from_provider == "gemini_flash"
    assert alt["from"] == "gemini_flash"
    assert alt.to.provider == "ollama_cloud"
    assert alt.to.model == "deepseek-v4.1-flash:cloud"

    # Stages
    assert set(policy.stages.keys()) == {
        "plan",
        "implement",
        "verify",
        "research",
        "audit",
        "acceptance_review",
    }
    assert policy.stages["plan"].rule == "omp-241-plan"
    assert policy.stages["plan"].effort == "E2"
    assert policy.stages["plan"].provider == "openai_codex"
    assert policy.stages["plan"].model == "gpt-5.6-sol"

    assert policy.stages["implement"].rule == "omp-241-implement"
    assert policy.stages["implement"].effort == "E1"
    assert policy.stages["implement"].provider == "gemini_flash"
    assert policy.stages["implement"].model == "gemini-3.8-flash-high"
    assert policy.stages["implement"].max_retries == 2

    assert policy.stages["verify"].rule == "omp-241-verify"
    assert policy.stages["verify"].effort == "E1"
    assert policy.stages["verify"].provider == "gemini_flash"
    assert policy.stages["verify"].model == "gemini-3.8-flash-high"
    assert policy.stages["verify"].max_retries == 2

    assert policy.stages["research"].rule == "research"
    assert policy.stages["research"].effort == "E1"
    assert policy.stages["research"].provider == "ollama_cloud"
    assert policy.stages["research"].model == "deepseek-v4.1-flash:cloud"
    assert policy.stages["research"].best_of_n is not None
    assert policy.stages["research"].best_of_n.n == 2
    assert policy.stages["research"].best_of_n.metric == "fact_coverage"

    assert policy.stages["audit"].rule == "audit"
    assert policy.stages["audit"].effort == "E3"
    assert policy.stages["audit"].reviewers == ("openai-codex/gpt-5.6-sol:medium",)

    assert policy.stages["acceptance_review"].rule == "acceptance_review"
    assert policy.stages["acceptance_review"].effort == "E3"
    assert policy.stages["acceptance_review"].reviewers == ("kimi-code/k3-256k",)

    # Missions
    assert policy.missions["engineering.execute"] == (
        "plan",
        "implement",
        "verify",
        "audit",
        "acceptance_review",
    )
    assert policy.missions["research.run"] == (
        "research",
        "acceptance_review",
    )

    # Retry and escalation
    assert policy.retry.rule == "retry-same-tier"
    assert len(policy.escalation) == 2
    assert policy.escalation[0].rule == "escalate-after-retries"
    assert policy.escalation[0].on == "failed_attempts"
    assert policy.escalation[0].step == 1
    assert policy.escalation[1].rule == "escalate-on-review-reject"
    assert policy.escalation[1].on == "review_rejected"
    assert policy.escalation[1].step == 1


def test_reject_wrong_version(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["version"] = "routing-policy.v2"
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Invalid policy version"):
        load_policy(p)


def test_reject_wrong_ladder(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["ladder"] = ["E1", "E2", "E3", "E4"]
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Invalid ladder"):
        load_policy(p)


def test_reject_tier_effort_outside_e1_e4(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["tiers"]["E0"] = {
        "provider": "gemini_flash",
        "model": "gemini-3.8-flash-high",
        "est_tokens": 10000,
    }
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Tier effort outside E1..E4"):
        load_policy(p)


def test_reject_stage_effort_outside_e1_e4(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["stages"]["plan"]["effort"] = "E0"
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="effort outside E1..E4"):
        load_policy(p)


def test_reject_provider_without_cap(tmp_path: Path):
    base = load_policy().raw

    # In tier
    bad_tier = copy.deepcopy(base)
    bad_tier["tiers"]["E1"]["provider"] = "uncapped_provider"
    p = _write_policy(tmp_path, bad_tier)
    with pytest.raises(RoutingPolicyError, match="no concurrency cap"):
        load_policy(p)

    # In stage
    bad_stage = copy.deepcopy(base)
    bad_stage["stages"]["implement"]["provider"] = "uncapped_provider"
    p2 = _write_policy(tmp_path, bad_stage)
    with pytest.raises(RoutingPolicyError, match="no concurrency cap"):
        load_policy(p2)

    # In alternate target
    bad_alt = copy.deepcopy(base)
    bad_alt["alternates"][0]["to"]["provider"] = "uncapped_provider"
    p3 = _write_policy(tmp_path, bad_alt)
    with pytest.raises(RoutingPolicyError, match="no concurrency cap"):
        load_policy(p3)


def test_reject_unknown_mission_stage(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["missions"]["engineering.execute"].append("phantom_stage")
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Unknown mission stage"):
        load_policy(p)


def test_reject_duplicate_rule_ids(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    # verify reuses implement's rule id
    bad["stages"]["verify"]["rule"] = "omp-241-implement"
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Duplicate rule id"):
        load_policy(p)


def test_reject_best_of_n_below_2(tmp_path: Path):
    base = load_policy().raw

    bad_1 = copy.deepcopy(base)
    bad_1["stages"]["research"]["best_of_n"]["n"] = 1
    p1 = _write_policy(tmp_path, bad_1)
    with pytest.raises(RoutingPolicyError, match="best_of_n.n below 2"):
        load_policy(p1)

    bad_0 = copy.deepcopy(base)
    bad_0["stages"]["research"]["best_of_n"]["n"] = 0
    p2 = _write_policy(tmp_path, bad_0)
    with pytest.raises(RoutingPolicyError, match="best_of_n.n below 2"):
        load_policy(p2)


def test_reject_unknown_on(tmp_path: Path):
    base = load_policy().raw
    bad = copy.deepcopy(base)
    bad["escalation"][0]["on"] = "bad_trigger"
    p = _write_policy(tmp_path, bad)
    with pytest.raises(RoutingPolicyError, match="Unknown escalation 'on' trigger"):
        load_policy(p)


def test_stage_route_record_contract():
    route = StageRoute(
        stage="implement",
        rule="omp-241-implement",
        provider="gemini_flash",
        model="gemini-3.8-flash-high",
        effort="E1",
        attempt=2,
        applied=("retry-same-tier",),
        effort_reason="policy rule omp-241-implement",
    )
    rec = route.record()
    assert rec == {
        "stage": "implement",
        "rule": "omp-241-implement",
        "provider": "gemini_flash",
        "model": "gemini-3.8-flash-high",
        "effort": "E1",
        "attempt": 2,
        "applied": ["retry-same-tier"],
        "effort_reason": "policy rule omp-241-implement",
    }


def test_routing_refused_code():
    err = RoutingRefused("caller_named_model")
    assert isinstance(err, Exception)
    assert err.code == "caller_named_model"
    assert str(err) == "caller_named_model"

    err_custom_msg = RoutingRefused("no_item_budget", "Budget not found")
    assert err_custom_msg.code == "no_item_budget"
    assert str(err_custom_msg) == "Budget not found"


def test_routing_policy_error_is_value_error():
    assert issubclass(RoutingPolicyError, ValueError)
