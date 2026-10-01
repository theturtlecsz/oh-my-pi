"""Tests for the pure orchestrator stage decision inputs (OMP-417)."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from omp_work.orchestrator.stages import (
    ActionVerdict,
    Bounds,
    Facts,
    Step,
    StepKind,
    bounds_for,
    decide,
    decision_for,
)
from omp_work.routing.policy import load_policy
from omp_work.v1.models import CreateDecisionPayload

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_UUID = UUID(PROJECT_ID)
DIGEST = "ab" * 32


def facts(**overrides: object) -> Facts:
    base: dict[str, object] = {
        "mission_id": "m1",
        "project_id": PROJECT_UUID,
        "step_index": 7,
        "stage": "plan",
        "mission_status": "active",
        "outcome": "none",
        "qualified": True,
    }
    base.update(overrides)
    return Facts(**base)  # type: ignore[arg-type]


def verdict(*, tier: int, action_class: str, target_sha256: str | None, allowed: bool) -> ActionVerdict:
    return ActionVerdict(
        tier=tier,
        action_class=action_class,
        target_sha256=target_sha256,
        allowed=allowed,
        code="verdict-code",
    )


# ==============================================================================
# bounds_for reads the recorded retry bound off the packaged routing policy.
# ==============================================================================


def test_bounds_for_reads_policy_retries_for_a_mission_stage() -> None:
    assert bounds_for(load_policy(), "engineering.execute", "implement") == Bounds(
        max_retries=2, repair_rounds=3
    )


def test_bounds_for_stage_absent_from_the_mission_is_zero() -> None:
    # evaluate is a STAGE_ORDER stage but not one of engineering.execute's stages.
    assert bounds_for(load_policy(), "engineering.execute", "evaluate").max_retries == 0


def test_bounds_for_keeps_recorded_repair_rounds() -> None:
    assert bounds_for(load_policy(), "engineering.execute", "implement", 5).repair_rounds == 5


def test_bounds_for_unknown_mission_kind_raises() -> None:
    with pytest.raises(ValueError):
        bounds_for(load_policy(), "not.a.mission", "implement")


# ==============================================================================
# Approval pauses: approve/decline with a recorded class and target digest.
# ==============================================================================


def test_tier3_pause_records_class_and_target() -> None:
    action_class = "broaden_scope"
    action = verdict(tier=3, action_class=action_class, target_sha256=DIGEST, allowed=False)
    decision = decision_for(facts(action=action), "d35-tier3")
    assert isinstance(decision, CreateDecisionPayload)
    assert decision.options == ("approve", "decline")
    assert decision.default_if_any == "decline"
    assert decision.action_class == action_class
    assert decision.target_sha256 == DIGEST


def test_unlisted_action_class_is_recorded_as_unlisted() -> None:
    action = verdict(tier=3, action_class="mission_abandon", target_sha256=None, allowed=False)
    decision = decision_for(facts(action=action), "d35-tier3")
    assert decision.action_class == "unlisted"
    assert decision.target_sha256 is None


def test_terminal_pause_without_action_records_no_class() -> None:
    decision = decision_for(facts(terminal_request="abandon"), "terminal-needs-basis")
    assert decision.options == ("approve", "decline")
    assert decision.default_if_any == "decline"
    assert decision.action_class == "unlisted"
    assert decision.target_sha256 is None


def test_d41_question_is_exact_and_binds_commit_and_target() -> None:
    action = verdict(
        tier=3,
        action_class="merge_protected_branch",
        target_sha256=DIGEST,
        allowed=False,
    )
    decision = decision_for(
        facts(stage="merge", candidate_commit="0123456789abcdef", target_ref="main", action=action),
        "d41-merge-approval",
    )
    exact = "Candidate 0123456789ab for mission m1 is verified and ready to merge into main. Merge it?"
    assert decision.question == exact
    assert decision.action_class == "merge_protected_branch"
    assert decision.target_sha256 == DIGEST


def test_d41_requires_commit_and_target() -> None:
    with pytest.raises(ValueError):
        decision_for(facts(stage="merge", target_ref="main"), "d41-merge-approval")
    with pytest.raises(ValueError):
        decision_for(facts(stage="merge", candidate_commit="abc"), "d41-merge-approval")


# ==============================================================================
# Resume pauses: resume/abandon with no default, class, or target.
# ==============================================================================


@pytest.mark.parametrize(
    "rule_id",
    [
        "release-qualification",
        "d35-tier2-no-policy",
        "d21-new-scope",
        "budget-exhausted",
        "unrecoverable-blocker",
        "repair-bound",
        "retry-bound",
    ],
)
def test_resume_rules_pause_with_resume_options(rule_id: str) -> None:
    action = verdict(tier=3, action_class="broaden_scope", target_sha256=DIGEST, allowed=False)
    decision = decision_for(facts(action=action), rule_id)
    assert decision.options == ("resume", "abandon")
    assert decision.default_if_any is None
    assert decision.action_class is None
    assert decision.target_sha256 is None


# ==============================================================================
# Every decision carries the deterministic id, evidence refs, and resume state.
# ==============================================================================


def test_decision_identity_is_deterministic() -> None:
    decision = decision_for(facts(stage="plan", step_index=7), "d21-new-scope")
    assert decision.decision_id == uuid5(NAMESPACE_URL, "omp-417:m1:7:d21-new-scope")
    assert decision.project_id == PROJECT_UUID
    assert decision.mission_id == "m1"
    assert decision.evidence_refs == ("orch_step:m1:7",)
    assert decision.resume_state == "plan"


def test_distinct_rules_get_distinct_decision_ids() -> None:
    scope = decision_for(facts(), "d21-new-scope")
    budget = decision_for(facts(), "budget-exhausted")
    assert scope.decision_id != budget.decision_id


# ==============================================================================
# Bad inputs are refused.
# ==============================================================================


def test_unknown_rule_raises() -> None:
    with pytest.raises(ValueError):
        decision_for(facts(), "not-a-rule")


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "nowhere"},
        {"outcome": "nowhere"},
        {"verdict": "nowhere"},
        {"terminal_request": "nowhere"},
    ],
)
def test_unknown_facts_values_raise(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        facts(**overrides)


# ==============================================================================
# decide: rules 8-10, first match. Only pauses carry a decision.
# ==============================================================================

_BOUNDS = Bounds(max_retries=2, repair_rounds=3)


def _step(
    fact: Facts,
    kind: StepKind,
    rule_id: str,
    *,
    next_stage: str | None,
    bound: int | None = None,
    decision: bool = False,
) -> Step:
    return Step(
        kind=kind,
        stage=fact.stage,
        next_stage=next_stage,
        rule_id=rule_id,
        bound=bound,
        decision=decision_for(fact, rule_id) if decision else None,
    )


def test_evaluate_crashed_retries_and_failed_repairs() -> None:
    crashed = facts(stage="evaluate", outcome="crashed", attempts=0)
    assert decide(crashed, _BOUNDS) == _step(
        crashed, "retry", "retry-policy", next_stage="evaluate", bound=2
    )

    failed = facts(stage="evaluate", outcome="failed", attempts=0, capacity_free=False)
    assert decide(failed, _BOUNDS) == _step(
        failed, "repair", "repair-round", next_stage="implement", bound=3
    )

    # Retry still wins when capacity is busy: the failure is not a repair.
    busy = facts(stage="evaluate", outcome="crashed", attempts=0, capacity_free=False)
    assert decide(busy, _BOUNDS) == _step(busy, "retry", "retry-policy", next_stage="evaluate", bound=2)


def test_audit_branches_on_verdict_and_outcome() -> None:
    crashed = facts(stage="audit", outcome="crashed", verdict="none", repair_rounds=0)
    assert decide(crashed, _BOUNDS) == _step(
        crashed, "repair", "repair-round", next_stage="implement", bound=3
    )

    failed_pass = facts(stage="audit", outcome="failed", verdict="pass", attempts=0)
    assert decide(failed_pass, _BOUNDS) == _step(
        failed_pass, "retry", "retry-policy", next_stage="audit", bound=2
    )

    succeeded_fail = facts(stage="audit", outcome="succeeded", verdict="fail", repair_rounds=0)
    assert decide(succeeded_fail, _BOUNDS) == _step(
        succeeded_fail, "repair", "repair-round", next_stage="implement", bound=3
    )

    succeeded_pass = facts(stage="audit", outcome="succeeded", verdict="pass")
    assert decide(succeeded_pass, _BOUNDS) == _step(
        succeeded_pass, "advance", "stage-order", next_stage="merge"
    )

    pending = facts(stage="audit", outcome="none")
    assert decide(pending, _BOUNDS) == _step(pending, "dispatch", "stage-dispatch", next_stage="audit")


def test_repair_bound_pauses_and_retry_bound_reroutes_or_pauses() -> None:
    exhausted = facts(stage="evaluate", outcome="failed", repair_rounds=3, attempts=0)
    assert decide(exhausted, _BOUNDS) == _step(
        exhausted, "pause", "repair-bound", next_stage="evaluate", bound=3, decision=True
    )

    # An alternate is consulted only after the retry budget is spent.
    still_retrying = facts(stage="implement", outcome="failed", attempts=1, alternate="other-route")
    assert decide(still_retrying, _BOUNDS) == _step(
        still_retrying, "retry", "retry-policy", next_stage="implement", bound=2
    )

    alternate = facts(stage="implement", outcome="failed", attempts=2, alternate="other-route")
    assert decide(alternate, _BOUNDS) == _step(
        alternate, "reroute", "reroute-alternate", next_stage="implement", bound=2
    )

    stuck = facts(stage="implement", outcome="failed", attempts=2, alternate=None)
    assert decide(stuck, _BOUNDS) == _step(
        stuck, "pause", "retry-bound", next_stage="implement", bound=2, decision=True
    )


def test_no_capacity_reschedules_close_waiting_and_intake() -> None:
    bounds = Bounds(max_retries=2, repair_rounds=3)
    for stage, outcome in (("close", "succeeded"), ("implement", "waiting"), ("intake", "none")):
        busy = facts(stage=stage, outcome=outcome, capacity_free=False)
        assert decide(busy, bounds) == _step(busy, "reschedule", "reschedule-capacity", next_stage=stage)


def test_free_capacity_dispatches_waits_and_ends_without_a_decision() -> None:
    pending = facts(stage="intake", outcome="none", capacity_free=True)
    assert decide(pending, _BOUNDS) == _step(pending, "dispatch", "stage-dispatch", next_stage="intake")

    inflight = facts(stage="intake", outcome="waiting", capacity_free=True)
    assert decide(inflight, _BOUNDS) == _step(inflight, "wait", "in-flight", next_stage="intake")

    done = facts(stage="close", outcome="succeeded", capacity_free=True)
    assert decide(done, _BOUNDS) == _step(done, "end", "mission-complete", next_stage=None)


def test_equal_facts_give_equal_steps() -> None:
    left = facts(stage="implement", outcome="failed", attempts=2, alternate=None)
    right = facts(stage="implement", outcome="failed", attempts=2, alternate=None)
    assert left == right
    assert decide(left, _BOUNDS) == decide(right, Bounds(max_retries=2, repair_rounds=3))
