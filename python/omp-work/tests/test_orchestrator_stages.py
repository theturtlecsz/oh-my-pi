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


# ==============================================================================
# Gate rules 1-7: stop, mission terminal, qualification, terminal request,
# refused actions, scope/budget/blockers, and owner confirm.
# ==============================================================================


@pytest.mark.parametrize(
    "trigger",
    [
        {"mission_status": "abandoned"},
        {"mission_status": "completed"},
        {"mission_status": "failed"},
        {"qualified": False},
        {"terminal_request": "abandon"},
        {"terminal_request": "redefine"},
        {"action": verdict(tier=2, action_class="push_code", target_sha256=DIGEST, allowed=False)},
        {
            "stage": "merge",
            "candidate_commit": "0123456789abcdef",
            "target_ref": "main",
            "action": verdict(tier=3, action_class="merge_protected_branch", target_sha256=DIGEST, allowed=False),
        },
        {"action": verdict(tier=3, action_class="broaden_scope", target_sha256=DIGEST, allowed=False)},
        {"new_scope": True},
        {"budget_exhausted": True},
        {"blocker": "unrecoverable"},
        {"stage": "confirm", "outcome": "none"},
        {"stage": "confirm", "outcome": "waiting"},
        {"stage": "evaluate", "outcome": "failed"},
        {"stage": "audit", "outcome": "failed", "verdict": "fail"},
        {"outcome": "crashed"},
        {"capacity_free": False},
        {
            "mission_status": "failed",
            "qualified": False,
            "terminal_request": "abandon",
            "action": verdict(tier=3, action_class="broaden_scope", target_sha256=DIGEST, allowed=False),
            "new_scope": True,
            "budget_exhausted": True,
            "blocker": "hard-stop",
            "capacity_free": False,
        },
    ],
)
def test_stop_engaged_wins_over_every_other_trigger(trigger: dict[str, object]) -> None:
    fact = facts(stop_engaged=True, **trigger)
    assert decide(fact, _BOUNDS) == Step(
        kind="frozen",
        stage=fact.stage,
        next_stage=fact.stage,
        rule_id="stop-freeze",
        bound=None,
        decision=None,
    )


@pytest.mark.parametrize(
    ("rule_id", "fact_overrides", "expected_options"),
    [
        (
            "release-qualification",
            {"qualified": False},
            ("resume", "abandon"),
        ),
        (
            "d35-tier2-no-policy",
            {"action": verdict(tier=2, action_class="push_code", target_sha256=DIGEST, allowed=False)},
            ("resume", "abandon"),
        ),
        (
            "d41-merge-approval",
            {
                "stage": "merge",
                "candidate_commit": "0123456789abcdef",
                "target_ref": "main",
                "action": verdict(tier=3, action_class="merge_protected_branch", target_sha256=DIGEST, allowed=False),
            },
            ("approve", "decline"),
        ),
        (
            "d35-tier3",
            {
                "stage": "implement",
                "action": verdict(tier=3, action_class="broaden_scope", target_sha256=DIGEST, allowed=False),
            },
            ("approve", "decline"),
        ),
        (
            "d21-new-scope",
            {"new_scope": True},
            ("resume", "abandon"),
        ),
        (
            "budget-exhausted",
            {"budget_exhausted": True},
            ("resume", "abandon"),
        ),
        (
            "unrecoverable-blocker",
            {"blocker": "infra-down"},
            ("resume", "abandon"),
        ),
    ],
)
def test_gate_pauses_carry_rule_decision_id_and_options(
    rule_id: str,
    fact_overrides: dict[str, object],
    expected_options: tuple[str, ...],
) -> None:
    fact = facts(**fact_overrides)
    step = decide(fact, _BOUNDS)
    assert step.kind == "pause"
    assert step.rule_id == rule_id
    assert step.stage == fact.stage
    assert step.next_stage == fact.stage
    assert step.bound is None
    assert step.decision is not None
    assert step.decision.decision_id == uuid5(
        NAMESPACE_URL, f"omp-417:{fact.mission_id}:{fact.step_index}:{rule_id}"
    )
    assert step.decision.options == expected_options


def test_terminal_allowed_and_refused_or_no_action() -> None:
    allowed_abandon = facts(
        terminal_request="abandon",
        action=verdict(tier=1, action_class="mission_abandon", target_sha256=None, allowed=True),
    )
    assert decide(allowed_abandon, _BOUNDS) == Step(
        kind="abandon",
        stage=allowed_abandon.stage,
        next_stage=None,
        rule_id="terminal-with-basis",
        bound=None,
        decision=None,
    )

    allowed_cancel = facts(
        terminal_request="cancel",
        action=verdict(tier=1, action_class="mission_abandon", target_sha256=None, allowed=True),
    )
    assert decide(allowed_cancel, _BOUNDS) == Step(
        kind="abandon",
        stage=allowed_cancel.stage,
        next_stage=None,
        rule_id="terminal-with-basis",
        bound=None,
        decision=None,
    )

    allowed_redefine = facts(
        terminal_request="redefine",
        action=verdict(tier=1, action_class="redefine_scope", target_sha256=None, allowed=True),
    )
    assert decide(allowed_redefine, _BOUNDS) == Step(
        kind="redefine",
        stage=allowed_redefine.stage,
        next_stage=None,
        rule_id="terminal-with-basis",
        bound=None,
        decision=None,
    )

    refused = facts(
        terminal_request="abandon",
        action=verdict(tier=3, action_class="mission_abandon", target_sha256=None, allowed=False),
    )
    step_refused = decide(refused, _BOUNDS)
    assert step_refused.kind == "pause"
    assert step_refused.rule_id == "terminal-needs-basis"
    assert step_refused.next_stage == refused.stage
    assert step_refused.bound is None
    assert step_refused.decision is not None
    assert step_refused.decision.options == ("approve", "decline")
    assert step_refused.decision.default_if_any == "decline"

    no_action = facts(terminal_request="redefine", action=None)
    step_no_action = decide(no_action, _BOUNDS)
    assert step_no_action.kind == "pause"
    assert step_no_action.rule_id == "terminal-needs-basis"
    assert step_no_action.next_stage == no_action.stage
    assert step_no_action.bound is None
    assert step_no_action.decision is not None
    assert step_no_action.decision.options == ("approve", "decline")
    assert step_no_action.decision.default_if_any == "decline"


def test_action_authorization_routes_by_stage_not_action_class() -> None:
    # Unlisted tier 3 at merge -> d41 with target digest and exact merge question.
    unlisted_merge = facts(
        stage="merge",
        candidate_commit="0123456789abcdef",
        target_ref="main",
        action=verdict(tier=3, action_class="not_a_v1_class", target_sha256=DIGEST, allowed=False),
    )
    step_merge = decide(unlisted_merge, _BOUNDS)
    assert step_merge.kind == "pause"
    assert step_merge.rule_id == "d41-merge-approval"
    assert step_merge.decision is not None
    assert step_merge.decision.action_class == "unlisted"
    assert step_merge.decision.target_sha256 == DIGEST
    exact_question = "Candidate 0123456789ab for mission m1 is verified and ready to merge into main. Merge it?"
    assert step_merge.decision.question == exact_question

    # merge_protected_branch at implement -> d35-tier3 (by stage, not action_class).
    merge_at_implement = facts(
        stage="implement",
        action=verdict(tier=3, action_class="merge_protected_branch", target_sha256=DIGEST, allowed=False),
    )
    step_impl = decide(merge_at_implement, _BOUNDS)
    assert step_impl.kind == "pause"
    assert step_impl.rule_id == "d35-tier3"
    assert step_impl.decision is not None
    assert step_impl.decision.action_class == "merge_protected_branch"

    # Tier 2 at push -> d35-tier2-no-policy, action_class is None.
    tier2_at_push = facts(
        stage="push",
        action=verdict(tier=2, action_class="push_code", target_sha256=DIGEST, allowed=False),
    )
    step_push = decide(tier2_at_push, _BOUNDS)
    assert step_push.kind == "pause"
    assert step_push.rule_id == "d35-tier2-no-policy"
    assert step_push.decision is not None
    assert step_push.decision.action_class is None


def test_confirm_unanswered_pauses_and_succeeded_advances_to_plan() -> None:
    for outcome in ("none", "waiting", "failed", "crashed"):
        unanswered = facts(stage="confirm", outcome=outcome)
        assert decide(unanswered, _BOUNDS) == Step(
            kind="pause",
            stage="confirm",
            next_stage="confirm",
            rule_id="d23-owner-confirm",
            bound=None,
            decision=None,
        )

    answered = facts(stage="confirm", outcome="succeeded")
    assert decide(answered, _BOUNDS) == Step(
        kind="advance",
        stage="confirm",
        next_stage="plan",
        rule_id="stage-order",
        bound=None,
        decision=None,
    )


def test_gate_precedence_rules() -> None:
    refused = verdict(tier=3, action_class="broaden_scope", target_sha256=DIGEST, allowed=False)

    # 1. Terminal status wins over not-qualified and refused action.
    for status in ("abandoned", "completed", "failed"):
        term = facts(mission_status=status, qualified=False, action=refused, new_scope=True)
        assert decide(term, _BOUNDS) == Step(
            kind="end",
            stage=term.stage,
            next_stage=None,
            rule_id="mission-terminal",
            bound=None,
            decision=None,
        )

    # 2. Not-qualified wins over refused action and terminal request.
    unqualified = facts(
        qualified=False,
        terminal_request="redefine",
        action=refused,
        new_scope=True,
    )
    assert decide(unqualified, _BOUNDS).rule_id == "release-qualification"

    # 3. Terminal request wins over refused action.
    term_req = facts(terminal_request="abandon", action=refused, new_scope=True)
    assert decide(term_req, _BOUNDS).rule_id == "terminal-needs-basis"

    # 4. Refused action wins over scope, budget, blocker.
    auth = facts(action=refused, new_scope=True, budget_exhausted=True, blocker="hard-block")
    assert decide(auth, _BOUNDS).rule_id == "d35-tier3"

    # 5. new_scope wins over budget_exhausted.
    scope = facts(new_scope=True, budget_exhausted=True, blocker="hard-block")
    assert decide(scope, _BOUNDS).rule_id == "d21-new-scope"

    # 6. budget_exhausted wins over blocker.
    budget = facts(budget_exhausted=True, blocker="hard-block")
    assert decide(budget, _BOUNDS).rule_id == "budget-exhausted"
