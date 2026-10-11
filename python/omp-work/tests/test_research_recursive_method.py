"""Recursive method identities, score isolation and failed-attempt accounting."""

import copy
import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from omp_work.research import accounting, mechanism, recursive_method
from omp_work.research.accounting import ResourceLimits
from omp_work.research.receipt import EvaluationProtocol, ReceiptExpectation, TrialReceipt
from omp_work.research.recursive import CallerGrant, RecursionState, RecursiveStudy, seal_json
from omp_work.research.recursive_method import (
    GeneratedPolicy, MethodContext, Observation, TrialFailed, lower_researcher_sha256, method_sha256, run_method,
)
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1 import canonical

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = NOW + timedelta(hours=1)
LIMITS = ResourceLimits(100, 1000, Decimal(10))
TRACES = [
    {"policy": "greedy", "action": "open", "metric": metric, "outcome": "completed"}
    for metric in (0.5, 0.4, 0.3)
]


def inputs(threshold=0.5, *, limits=LIMITS, traces=None):
    protocol = EvaluationProtocol(
        metrics=[{"name": "score", "direction": "higher"}], primary="score",
        candidate_command=["python", "candidate.py"], evaluator_command=["python", "judge.py"],
        confirmation_sha256="2" * 64, timeout_seconds=10,
    )
    draft = mechanism.explore("plateau:greedy", [{
        "name": "recursive", "kind": "code", "components": ["threshold"],
        "description": "Extend qualifying opened parents; otherwise open roots.",
    }])[0]
    spec = mechanism.specify(draft)
    recipe = seal_json({"min_metric": threshold})
    dev = {"dev": TRACES if traces is None else traces}
    digest = canonical.sha256(dev["dev"])
    study = RecursiveStudy(
        study_id="root", parent_id=None, depth=0, recipe=recipe, spec=spec,
        method_version="r13.v1", method_sha256=method_sha256(),
        lower_researcher_sha256=lower_researcher_sha256(), objective="Improve search",
        evaluator_component_sha256="c" * 64, evaluator_artifact_sha256="d" * 64,
        protocol_sha256=hashlib.sha256(protocol.protocol_bytes()).hexdigest(),
        dev=[{"task_id": "dev", "content_sha256": digest}],
        confirmation=[{"task_id": "confirm", "content_sha256": "2" * 64}],
        dev_sha256=canonical.sha256([digest]), confirmation_sha256=canonical.sha256(["2" * 64]),
        upstream_assets=[{"identity": "generator", "content_sha256": lower_researcher_sha256()}],
        window_start=NOW, window_end=END, max_depth=2, max_descendants=2, limits=limits,
    )
    grant = CallerGrant(
        caller_id="owner", policy_id="spend-policy", decision_id="decision",
        signature_sha256="3" * 64, window_start=NOW, window_end=END,
        max_depth=2, max_descendants=2, limits=limits,
    )
    context = MethodContext(
        state=RecursionState(grant=grant), grant=grant,
        policy=StandingPolicy(
            policy_id="spend-policy", action_class="spend_beyond_threshold",
            money_limit_usd=Decimal(10), expires_at=END, decision_id="decision",
        ),
        authority=ChangeAuthority("owner", "decision", "owner"),
        action=ActionRequest("spend_beyond_threshold", amount_usd=Decimal(10)),
        repositories={"repo": RepositoryRecord("repo", "main")},
        store=None, workspace_id="workspace", actor_id="actor", work_id="work",
        operation_id="operation", request_id="request", now=lambda: NOW,
    )
    return study, recipe, spec, dev, context, protocol


def tree():
    return {
        "a": {"parent_id": None, "children": ["a1"], "metric": 0.6, "cost": 1},
        "a1": {"parent_id": "a", "children": [], "metric": 0.99, "cost": 1},
        "b": {"parent_id": None, "children": [], "metric": 0.1, "cost": 1},
    }


def observation(study, protocol, step_id="a", score=0.6):
    expected = ReceiptExpectation(
        trial_id=step_id, campaign_id="dev", job_id=f"managed-trial:{step_id}",
        candidate_digest="f" * 64, evaluator_component_sha256=study.evaluator_component_sha256,
        evaluator_artifact_sha256=study.evaluator_artifact_sha256,
    )
    receipt = TrialReceipt(
        **expected.model_dump(), protocol_sha256=study.protocol_sha256,
        confirmation_sha256=protocol.confirmation_sha256,
        candidate_manifest="1" * 64, evaluator_manifest="2" * 64,
        output_sha256="3" * 64, metrics={"score": score}, verdict="qualified",
        negative_control="rejected",
    )
    return Observation(receipt.receipt_bytes(), expected)


def generated(threshold=0.5, **kwargs):
    study, recipe, spec, dev, context, protocol = inputs(threshold, **kwargs)
    result = run_method(study, recipe, spec, dev, context=context)
    assert result.failures == ()
    policy = GeneratedPolicy(result, study, context=context, protocol=protocol,
                             observations={"a": observation(study, protocol)})
    return result, policy, study, context, protocol


@pytest.mark.parametrize("changed", ["recipe", "method", "researcher", "spec", "dev", "authority"])
def test_changed_bindings_refused_before_generator(monkeypatch, changed):
    study, recipe, spec, dev, context, _ = inputs()
    if changed == "recipe":
        recipe = seal_json({"min_metric": 0.8})
    elif changed == "method":
        study = study.model_copy(update={"method_sha256": "a" * 64})
    elif changed == "researcher":
        study = study.model_copy(update={"lower_researcher_sha256": "a" * 64})
    elif changed == "spec":
        spec = {**spec, "expected_behavior": "replacement"}
    elif changed == "dev":
        dev = {**dev, "confirm": TRACES}
    else:
        context = replace(context, authority=ChangeAuthority("owner", "other", "owner"))
    calls = []
    monkeypatch.setattr(mechanism, "generation_job", lambda *args: calls.append(args))
    result = run_method(study, recipe, spec, dev, context=context)
    assert result.failures
    assert result.source_bytes is None
    assert result.denominator == 1
    assert not calls
    assert result.state.usage == ()


def test_changed_recipe_changes_development_selection_and_distinct_digests():
    low, low_policy, _, _, _ = generated(0.5)
    high, high_policy, _, _, _ = generated(0.8)
    steps = tree()
    assert low_policy(steps, {"a"}) == ("a1", 0)
    assert high_policy(steps, {"a"}) == ("b", 0)
    assert low.recipe_sha256 != high.recipe_sha256
    assert low.source_sha256 != high.source_sha256
    assert low.artifact_sha256 != high.artifact_sha256
    assert len({low.recipe_sha256, low.source_sha256, low.artifact_sha256}) == 3
    with pytest.raises(FrozenInstanceError):
        low.source_bytes = b"replacement"


def test_unopened_scores_and_unverified_opened_scores_hidden_caller_and_judge_unchanged(monkeypatch):
    result, policy, study, context, protocol = generated()
    steps = tree()
    opened = {"a"}
    before = copy.deepcopy(steps)
    judge_before = protocol.protocol_bytes()
    observed = []
    strategy_run = recursive_method.strategy_protocol.run_candidate

    def inspect(source, snapshot, snap_opened, **kwargs):
        observed.append(copy.deepcopy(snapshot))
        return strategy_run(source, snapshot, snap_opened, **kwargs)

    monkeypatch.setattr(recursive_method.strategy_protocol, "run_candidate", inspect)
    assert policy(steps, opened) == ("a1", 0)
    assert observed[-1]["a1"]["metric"] is None
    assert observed[-1]["b"]["metric"] is None
    assert observed[-1]["a"]["metric"] == 0.6
    steps["a1"]["metric"] = -1000
    steps["b"]["metric"] = 1000
    steps["a"]["metric"] = 9999
    assert policy(steps, opened) == ("a1", 0)
    assert observed[-1] == observed[-2]
    assert opened == {"a"}
    assert protocol.protocol_bytes() == judge_before
    assert context.state.studies == () and context.state.usage == ()
    steps = before
    unverified = GeneratedPolicy(result, study, context=context, protocol=protocol, observations={})
    assert unverified(steps, opened) == ("b", 0)
    assert observed[-1]["a"]["metric"] is None
    assert steps == before
    # Sound unopened receipts still cannot reveal future observations.
    future = GeneratedPolicy(result, study, context=context, protocol=protocol, observations={
        "a": observation(study, protocol), "b": observation(study, protocol, "b", 1000),
    })
    assert future(steps, opened) == ("a1", 0)
    assert observed[-1]["b"]["metric"] is None


@pytest.mark.parametrize("failure", ["crashed", "unknown_node", "changed_source"])
def test_proposal_failures_spend_and_never_receive_fallback_credit(failure):
    result, policy, study, context, protocol = generated()
    steps = tree()
    opened = {"a"}
    if failure == "crashed":
        opened.add("missing")
    elif failure == "unknown_node":
        steps["a"]["children"] = ["missing"]
    else:
        forged = replace(result, source_bytes=b"def propose(steps, opened, *, seed=0, width=1):\n    return 'b', seed\n")
        policy = GeneratedPolicy(forged, study, context=context, protocol=protocol, observations={})
    prior_usage = len(policy.state.usage)
    with pytest.raises(TrialFailed, match=failure):
        policy(steps, opened)
    assert policy.failures == (failure,)
    assert policy.denominator == 1
    assert len(policy.state.usage) == prior_usage + 1
    assert policy.state.usage[-1].compute_seconds > 0


def test_generation_crash_and_quota_crossing_retained_in_native_accounting(monkeypatch):
    events = []
    monkeypatch.setattr(accounting, "record_usage", lambda store, **kwargs: events.append((store, kwargs)))
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(recursive_method, "perf_counter", lambda: next(clock))
    study, recipe, spec, dev, context, _ = inputs(
        limits=ResourceLimits(1, 1000, Decimal(10)),
        traces=[{"policy": "greedy", "metric": "bad"}],
    )
    store = object()
    context = replace(context, store=store)
    result = run_method(study, recipe, spec, dev, context=context)
    assert result.failures[-1] == "quota_exhausted"
    assert len(result.failures) == 2
    assert result.denominator == 1 and result.source_bytes is None
    assert result.state.terminal_reason == "quota_exhausted"
    assert result.state.usage[0].compute_seconds == 2
    assert events[0][0] is store
    assert events[0][1]["measurement"] == "measured"
    assert events[0][1]["workspace_id"] == context.workspace_id
    assert events[0][1]["actor_id"] == context.actor_id
    assert events[0][1]["work_id"] == context.work_id
    assert context.state.usage == ()


def test_proposal_crossing_retains_usage_then_blocks_later_proposal(monkeypatch):
    ticks = iter([0.0, 0.25, 0.25, 1.25])
    monkeypatch.setattr(recursive_method, "perf_counter", lambda: next(ticks))
    result, policy, _, _, _ = generated(limits=ResourceLimits(1, 1000, Decimal(10)))
    with pytest.raises(TrialFailed, match="quota_exhausted"):
        policy(tree(), {"a"})
    assert [event.compute_seconds for event in policy.state.usage] == [0.25, 1.0]
    assert policy.state.terminal_reason == "quota_exhausted"
    with pytest.raises(TrialFailed, match="quota_exhausted"):
        policy(tree(), {"a"})
    assert policy.denominator == 2
    assert policy.failures == ("quota_exhausted", "quota_exhausted")
    assert len(policy.state.usage) == 2
    assert result.state.usage[0].compute_seconds == 0.25


def test_live_authority_rechecked_before_each_proposal():
    result, _, study, context, protocol = generated()
    expired = replace(context, now=lambda: END)
    policy = GeneratedPolicy(result, study, context=expired, protocol=protocol, observations={})
    with pytest.raises(TrialFailed, match="policy_not_covered"):
        policy(tree(), {"a"})
    assert policy.state.usage == result.state.usage
    assert policy.denominator == 1


def test_spent_controller_state_cannot_be_reset_by_generated_result():
    result, _, study, context, protocol = generated()
    latest = recursive_method.recursive.record_usage(result.state, study.study_id, usd=Decimal(10))
    policy = GeneratedPolicy(result, study, context=replace(context, state=latest),
                             protocol=protocol, observations={})
    with pytest.raises(TrialFailed, match="quota_exhausted"):
        policy(tree(), {"a"})
    assert policy.state.usage == latest.usage


def test_invalid_opened_receipt_cannot_supply_metric():
    result, _, study, context, protocol = generated()
    valid = observation(study, protocol)
    changed = TrialReceipt.model_validate_json(valid.receipt_bytes).model_copy(update={
        "evaluator_component_sha256": "0" * 64,
    })
    policy = GeneratedPolicy(result, study, context=context, protocol=protocol,
                             observations={"a": Observation(changed.receipt_bytes(), valid.expected)})
    assert policy(tree(), {"a"}) == ("b", 0)


def test_altered_development_content_refused_before_generation(monkeypatch):
    study, recipe, spec, dev, context, _ = inputs()
    dev["dev"] = [{**trace, "metric": 99} for trace in dev["dev"]]
    calls = []
    monkeypatch.setattr(mechanism, "generation_job", lambda *args: calls.append(args))
    result = run_method(study, recipe, spec, dev, context=context)
    assert result.failures == ("development_content_changed",)
    assert result.state.usage == () and not calls


def test_generator_and_proposals_use_distinct_native_request_identities(monkeypatch):
    events = []
    monkeypatch.setattr(accounting, "record_usage", lambda store, **kwargs: events.append(kwargs))
    study, recipe, spec, dev, context, protocol = inputs()
    context = replace(context, store=object())
    result = run_method(study, recipe, spec, dev, context=context)
    policy = GeneratedPolicy(result, study, context=context, protocol=protocol,
                             observations={"a": observation(study, protocol)})
    assert policy(tree(), {"a"}) == ("a1", 0)
    assert [event["request_id"] for event in events] == ["request:1", "request:2"]
    assert all(event["price_usd"] == "0" and event["input_tokens"] == 0 for event in events)
    assert len(policy.state.usage) == 2


def test_crashing_proposal_crossing_quota_records_both_failures(monkeypatch):
    ticks = iter([0.0, 0.25, 0.25, 1.25])
    monkeypatch.setattr(recursive_method, "perf_counter", lambda: next(ticks))
    _, policy, _, _, _ = generated(limits=ResourceLimits(1, 1000, Decimal(10)))
    with pytest.raises(TrialFailed, match="quota_exhausted"):
        policy(tree(), {"a", "missing"})
    assert policy.failures == ("crashed", "quota_exhausted")
    assert policy.denominator == 1
    assert policy.state.usage[-1].compute_seconds == 1


def test_existing_quota_exhaustion_blocks_generation(monkeypatch):
    result, _, study, context, _ = generated()
    spent = recursive_method.recursive.record_usage(result.state, study.study_id, usd=Decimal(10))
    calls = []
    monkeypatch.setattr(mechanism, "generation_job", lambda *args: calls.append(args))
    refused = run_method(study, study.recipe, study.spec, {"dev": TRACES},
                         context=replace(context, state=spent))
    assert refused.failures == ("quota_exhausted",)
    assert refused.state.usage == spent.usage
    assert refused.source_bytes is None and not calls
