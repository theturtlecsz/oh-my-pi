"""OMP-420-s08: end-to-end mission routing from the written policy.

One engineering.execute mission routed by the policy: every stage record names
its rule, provider, model, and effort, and each route admits through the effort
gate. Three implement failures escalate it to E2 within the item budget and the
escalated route still admits; a budget too small for E2 is refused. Audit admits
only the policy's native auditor, never the implementer identity. A research.run
mission's research stage runs the policy's Best-of-N with a stub harness.
"""

from __future__ import annotations

import pytest

from omp_work.jobs import stage_admission
from omp_work.jobs.stage_admission import admit_stage_job
from omp_work.jobs.store import JobError
from omp_work.research_campaign import run_campaign
from omp_work.routing.escalation import next_route
from omp_work.routing.policy import RoutingPolicy, RoutingRefused, StageRoute, load_policy
from omp_work.routing.review import check_reviewer
from omp_work.routing.router import route_mission
from omp_work.v1.models import ItemBudget

_STORE = object()
_ENQUEUE = {
    "operation_id": "op-1",
    "workspace_id": "ws",
    "actor_id": "act",
    "job_id": "j-1",
    "work_id": "w-1",
    "kind": "model",
    "required_capabilities": ["cpu"],
    "resources": {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
    "lease_seconds": 60,
}


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, store, **kwargs):
        self.calls.append((store, kwargs))
        return {"status": "applied"}


def _routes(policy: RoutingPolicy) -> dict[str, StageRoute]:
    return {route.stage: route for route in route_mission(policy, {"kind": "engineering.execute"})}


def _budget(tokens: int) -> ItemBudget:
    return ItemBudget(usd="10.00", tokens=tokens, wall_clock_seconds=3600, max_subagents=2)


def test_route_mission_names_each_stage_rule_provider_model_effort() -> None:
    policy = load_policy()
    routes = route_mission(policy, {"kind": "engineering.execute"})

    assert [route.stage for route in routes] == list(policy.missions["engineering.execute"])
    for route in routes:
        spec = policy.stages[route.stage]
        record = route.record()
        assert record["rule"] == spec.rule
        assert record["provider"] == spec.provider
        assert record["model"] == spec.model
        assert record["effort"] == spec.effort

    with pytest.raises(RoutingRefused) as exc:
        route_mission(policy, {"kind": "engineering.execute", "model": "gpt-x"})
    assert exc.value.code == "caller_named_model"


def test_each_stage_route_passes_the_gate_and_admits_once(monkeypatch) -> None:
    policy = load_policy()
    spy = _Spy()
    monkeypatch.setattr(stage_admission, "enqueue_job", spy)

    routes = route_mission(policy, {"kind": "engineering.execute"})
    for route in routes:
        assert admit_stage_job(
            _STORE, effort=route.effort, effort_reason=route.effort_reason, **_ENQUEUE
        ) == {"status": "applied"}

    assert len(spy.calls) == len(routes) == 5
    assert [store for store, _ in spy.calls] == [_STORE] * 5
    # The route's effort is the only gate input; it never leaks into the enqueue.
    assert [kwargs for _, kwargs in spy.calls] == [dict(_ENQUEUE)] * 5
    # Non-E1 routes carry the reason the gate demands; E1 needs none.
    assert {route.stage: route.effort_reason for route in routes if route.effort != "E1"} == {
        "plan": "policy rule omp-241-plan",
        "audit": "policy rule audit",
        "acceptance_review": "policy rule acceptance_review",
    }


def test_missing_effort_is_refused_before_enqueue(monkeypatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(stage_admission, "enqueue_job", spy)

    with pytest.raises(JobError) as exc:
        admit_stage_job(_STORE, effort=None, effort_reason="why", **_ENQUEUE)
    assert exc.value.code == "invalid_effort"
    assert spy.calls == []


def test_implement_failure_escalates_to_e2_and_admits(monkeypatch) -> None:
    policy = load_policy()
    implement = _routes(policy)["implement"]

    escalated = next_route(
        policy,
        implement,
        failed_attempts=3,
        review_rejected=False,
        item_budget=_budget(500000),
        spent_tokens=10000,
    )

    assert escalated.effort == "E2"
    assert escalated.provider == "anthropic_fable"
    assert escalated.model == "claude-opus-5-5"
    assert "escalate-after-retries" in escalated.record()["applied"]

    spy = _Spy()
    monkeypatch.setattr(stage_admission, "enqueue_job", spy)
    admit_stage_job(
        _STORE, effort=escalated.effort, effort_reason=escalated.effort_reason, **_ENQUEUE
    )
    assert len(spy.calls) == 1

    with pytest.raises(RoutingRefused) as exc:
        next_route(
            policy,
            implement,
            failed_attempts=3,
            review_rejected=False,
            item_budget=_budget(100000),
            spent_tokens=30000,
        )
    assert exc.value.code == "escalation_over_budget"


def test_audit_admits_native_auditor_and_refuses_the_implementer() -> None:
    policy = load_policy()
    implementer = _routes(policy)["implement"]

    assert (
        check_reviewer(policy, stage="audit", reviewer="openai-codex/gpt-5.6-sol:medium") is None
    )

    identity = f"{implementer.provider}/{implementer.model}"
    with pytest.raises(RoutingRefused) as exc:
        check_reviewer(policy, stage="audit", reviewer=identity, maker=identity)
    assert exc.value.code == "reviewer_not_independent"


def test_research_run_scores_best_of_n_with_a_stub_harness() -> None:
    policy = load_policy()
    research = route_mission(policy, {"kind": "research.run"})[0]
    assert research.stage == "research"

    best_of_n = policy.stages["research"].best_of_n
    assert best_of_n is not None
    scores = {"alpha": 0.9, "beta": 0.25}

    def generate(question: str, facts: list[str], n: int) -> list[str]:
        assert n == best_of_n.n
        return list(scores)

    def harness(text: str) -> str:
        return f"METRIC {best_of_n.metric}={scores[text]}\n"

    result = run_campaign(
        campaign_id="c",
        question="q",
        retrieved_facts=["f"],
        generate=generate,
        harness=harness,
        policy=policy,
    )

    assert len(result.candidates) == best_of_n.n
    assert result.eval_winner_n == 1
    assert result.revision == "Revised once from N1: alpha [adapted]"
