import importlib.resources
import json

from omp_work.routing.policy import load_policy


def _record():
    ref = importlib.resources.files("omp_work.routing").joinpath("substitutions.v1.json")
    return json.loads(ref.read_text(encoding="utf-8"))


def test_every_stage_recorded_once_with_policy_route():
    policy = load_policy()
    entries = _record()["stages"]
    assert sorted(e["stage"] for e in entries) == sorted(policy.stages)
    for e in entries:
        rule = policy.stages[e["stage"]]
        assert (e["rule"], e["provider"], e["model"]) == (rule.rule, rule.provider, rule.model)
        assert tuple(e["reviewers"]) == rule.reviewers
        assert e["approval"] is None
        assert e["disposition"] in {"kept", "pending_owner"}


def test_every_alternate_recorded_once_pending_owner():
    policy = load_policy()
    entries = _record()["overflow"]
    assert sorted(e["rule"] for e in entries) == sorted(a.rule for a in policy.alternates)
    for e in entries:
        alt = next(a for a in policy.alternates if a.rule == e["rule"])
        assert (e["from_provider"], e["provider"], e["model"]) == (alt.from_provider, alt.to.provider, alt.to.model)
        assert e["disposition"] == "pending_owner"
        assert e["approval"] is None


def test_plan_pending_and_callers():
    by_stage = {e["stage"]: e for e in _record()["stages"]}
    assert by_stage["plan"]["disposition"] == "pending_owner"
    assert by_stage["plan"]["caller"] is None
    assert by_stage["implement"]["caller"] == "omp_work.model_proxy"
    assert by_stage["research"]["caller"] == "omp_work.intake_support_admission"
