"""Deterministic stage decision for one orchestrator step (OMP-417).

``bounds_for`` reads the recorded retry bound off the routing policy,
``decision_for`` builds the single owner-facing decision a pause carries, and
``decide`` applies rules 8-10 (repair, then retry, then capacity and stage
movement). Gates 1-7 are a later slice. All three are pure: they read only
their arguments, so equal inputs give equal results and no model output or
clock can choose a stage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, assert_never, get_args
from uuid import NAMESPACE_URL, UUID, uuid5

from omp_work.routing.policy import RoutingPolicy
from omp_work.v1.models import CreateDecisionPayload, DecisionActionClass

__all__ = [
    "STAGE_ORDER",
    "UNRECORDED_KINDS",
    "ActionVerdict",
    "Bounds",
    "Facts",
    "Step",
    "bounds_for",
    "decide",
    "decision_for",
]

# intake, confirm, plan, grant, implement, evaluate, freeze, push, audit, merge, close.
STAGE_ORDER: tuple[str, ...] = (
    "intake",
    "confirm",
    "plan",
    "grant",
    "implement",
    "evaluate",
    "freeze",
    "push",
    "audit",
    "merge",
    "close",
)

_STAGE_SET = frozenset(STAGE_ORDER)

StepKind = Literal[
    "frozen",
    "end",
    "pause",
    "abandon",
    "redefine",
    "repair",
    "retry",
    "reroute",
    "reschedule",
    "advance",
    "dispatch",
    "wait",
    "recovery",
]

# frozen and wait steps are not recorded as orchestrator step events.
UNRECORDED_KINDS: frozenset[str] = frozenset({"frozen", "wait"})

Outcome = Literal["none", "succeeded", "failed", "crashed", "waiting"]
TerminalRequest = Literal["abandon", "cancel", "redefine"]
Verdict = Literal["none", "pass", "fail"]

_OUTCOMES = frozenset(get_args(Outcome))
_TERMINAL_REQUESTS = frozenset(get_args(TerminalRequest))
_VERDICTS = frozenset(get_args(Verdict))
_DECISION_ACTION_CLASSES = frozenset(get_args(DecisionActionClass))

# Approval pauses record the action class and target digest; resume pauses do not.
_APPROVAL_RULES = frozenset({"d35-tier3", "d41-merge-approval", "terminal-needs-basis"})
_RESUME_RULES = frozenset(
    {
        "release-qualification",
        "d35-tier2-no-policy",
        "d21-new-scope",
        "budget-exhausted",
        "unrecoverable-blocker",
        "repair-bound",
        "retry-bound",
    }
)
_OPTIONS_APPROVAL = ("approve", "decline")
_OPTIONS_RESUME = ("resume", "abandon")

# rule id -> (question, why it matters, risk of delay); {mission} is always
# available and {commit}/{target} only for the merge rule.
_RULE_TEXT: Mapping[str, tuple[str, str, str]] = {
    "release-qualification": (
        "This release has no recorded qualification pass. Resume mission {mission}?",
        "Unqualified code must not run the production orchestrator.",
        "No work advances until the release is qualified.",
    ),
    "terminal-needs-basis": (
        "End mission {mission} with no recorded approval basis?",
        "Ending a mission without an approval basis is not autonomous.",
        "The mission stays open and pauses where it is.",
    ),
    "d35-tier2-no-policy": (
        "Allow this tier 2 action for mission {mission} with no covering policy?",
        "Tier 2 actions run only under a covering standing policy.",
        "The mission waits and no external action runs.",
    ),
    "d41-merge-approval": (
        "Candidate {commit} for mission {mission} is verified and ready to merge into {target}. Merge it?",
        "Merging into a protected or default branch is tier 3 and needs the owner's signed authorization.",
        "The candidate stays pushed and unmerged.",
    ),
    "d35-tier3": (
        "Allow this tier 3 action for mission {mission}?",
        "Tier 3 actions need the owner's signed authorization before they run.",
        "The action stays blocked and the mission waits.",
    ),
    "d21-new-scope": (
        "Allow this new scope for mission {mission}?",
        "New or materially changed mission scope needs the owner's confirmation.",
        "The mission waits without widening its scope.",
    ),
    "budget-exhausted": (
        "Resume mission {mission} after its budget was exhausted?",
        "Work must not continue past the mission envelope without the owner.",
        "The mission stays paused with no further spend.",
    ),
    "unrecoverable-blocker": (
        "Resume mission {mission} past an unrecoverable blocker?",
        "A blocked mission needs the owner before it continues.",
        "The mission stays paused at the blocked stage.",
    ),
    "repair-bound": (
        "Resume mission {mission} after its repair bound was reached?",
        "Repeated failed evaluations or audits need the owner before another round.",
        "The mission stays paused at the failed stage.",
    ),
    "retry-bound": (
        "Resume mission {mission} after its retry bound was reached?",
        "A step that keeps failing needs the owner before another attempt.",
        "The mission stays paused at the failed stage.",
    ),
}


@dataclass(frozen=True)
class ActionVerdict:
    """The control plane's classify-and-authorize verdict for one request."""

    tier: int
    action_class: str
    target_sha256: str | None
    allowed: bool
    code: str


@dataclass(frozen=True, kw_only=True)
class Facts:
    """Every input ``decision_for`` may read. No field can carry model output."""

    mission_id: str
    project_id: UUID
    step_index: int
    stage: str
    mission_status: str
    outcome: Outcome
    qualified: bool
    attempts: int = 0
    repair_rounds: int = 0
    stop_engaged: bool = False
    action: ActionVerdict | None = None
    terminal_request: TerminalRequest | None = None
    new_scope: bool = False
    budget_exhausted: bool = False
    blocker: str | None = None
    capacity_free: bool = True
    alternate: str | None = None
    verdict: Verdict = "none"
    candidate_commit: str | None = None
    target_ref: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in _STAGE_SET:
            raise ValueError(f"unknown stage: {self.stage!r}")
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome: {self.outcome!r}")
        if self.verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict: {self.verdict!r}")
        if self.terminal_request is not None and self.terminal_request not in _TERMINAL_REQUESTS:
            raise ValueError(f"unknown terminal_request: {self.terminal_request!r}")


@dataclass(frozen=True)
class Bounds:
    """The recorded retry and repair bounds for one orchestrator step."""

    max_retries: int
    repair_rounds: int


@dataclass(frozen=True)
class Step:
    """One orchestrator decision: kind, stage movement, rule, and any pause decision."""

    kind: StepKind
    stage: str
    next_stage: str | None
    rule_id: str
    bound: int | None
    decision: CreateDecisionPayload | None


def bounds_for(
    policy: RoutingPolicy,
    mission_kind: str,
    stage: str,
    repair_rounds: int = 3,
) -> Bounds:
    """The stage's policy retry bound when the stage belongs to the mission kind."""
    if mission_kind not in policy.missions:
        raise ValueError(f"unknown mission kind: {mission_kind!r}")
    if repair_rounds < 0:
        raise ValueError("repair_rounds must be non-negative")
    if stage in policy.missions[mission_kind]:
        stage_rule = policy.stages.get(stage)
        max_retries = stage_rule.max_retries if stage_rule is not None else 0
    else:
        max_retries = 0
    return Bounds(max_retries=max_retries, repair_rounds=repair_rounds)


def _recorded_class(facts: Facts) -> str:
    """The class a tier-3 or terminal decision records (unlisted when unclassified)."""
    action = facts.action
    if action is None or action.action_class not in _DECISION_ACTION_CLASSES:
        return "unlisted"
    return action.action_class


def decision_for(facts: Facts, rule_id: str) -> CreateDecisionPayload:
    """The one owner decision a pause raises, from a fixed per-rule text table."""
    if rule_id in _APPROVAL_RULES:
        options = _OPTIONS_APPROVAL
        default: str | None = "decline"
        action_class: str | None = _recorded_class(facts)
        target_sha256: str | None = None if facts.action is None else facts.action.target_sha256
        risks = {
            "approve": "The action runs exactly as classified against the recorded target.",
            "decline": "The action stays blocked and the mission waits.",
        }
    elif rule_id in _RESUME_RULES:
        options = _OPTIONS_RESUME
        default = None
        action_class = None
        target_sha256 = None
        risks = {
            "resume": "The mission continues from the paused stage.",
            "abandon": "The mission stays paused where it is.",
        }
    else:
        raise ValueError(f"unknown orchestrator rule id: {rule_id!r}")

    if rule_id == "d41-merge-approval" and (facts.candidate_commit is None or facts.target_ref is None):
        raise ValueError("d41-merge-approval requires candidate_commit and target_ref")

    question, why_it_matters, risk_of_delay = _RULE_TEXT[rule_id]
    fields = {
        "mission": facts.mission_id,
        "commit": None if facts.candidate_commit is None else facts.candidate_commit[:12],
        "target": facts.target_ref,
    }
    return CreateDecisionPayload(
        decision_id=uuid5(NAMESPACE_URL, f"omp-417:{facts.mission_id}:{facts.step_index}:{rule_id}"),
        project_id=facts.project_id,
        mission_id=facts.mission_id,
        question=question.format(**fields),
        why_it_matters=why_it_matters.format(**fields),
        risk_of_delay=risk_of_delay.format(**fields),
        options=options,
        risk_of_each_choice=risks,
        default_if_any=default,
        action_class=action_class,
        target_sha256=target_sha256,
        evidence_refs=(f"orch_step:{facts.mission_id}:{facts.step_index}",),
        resume_state=facts.stage,
    )


def decide(facts: Facts, bounds: Bounds) -> Step:
    """Rules 8-10, first match within each outcome. Gates 1-7 are a later slice.

    Repair wins over retry, and both win over a busy capacity. A failed or
    crashed step that is not a repair retries, reroutes, or pauses.
    """
    outcome = facts.outcome
    if outcome == "failed":
        return _repair_or_retry(facts, bounds)
    if outcome == "crashed":
        return _repair_or_retry(facts, bounds)
    if outcome == "succeeded":
        return _succeeded(facts, bounds)
    if outcome == "none":
        if not facts.capacity_free:
            return _reschedule(facts)
        return _step(facts, "dispatch", "stage-dispatch", next_stage=facts.stage)
    if outcome == "waiting":
        if not facts.capacity_free:
            return _reschedule(facts)
        return _step(facts, "wait", "in-flight", next_stage=facts.stage)
    assert_never(outcome)


def _is_repair(facts: Facts) -> bool:
    """A failed evaluation, or an audit (succeeded, failed, or crashed) that did not pass."""
    if facts.stage == "evaluate":
        return facts.outcome == "failed"
    if facts.stage == "audit" and facts.verdict != "pass":
        return facts.outcome in ("succeeded", "failed", "crashed")
    return False


def _repair_or_retry(facts: Facts, bounds: Bounds) -> Step:
    if _is_repair(facts):
        return _repair(facts, bounds)
    return _retry(facts, bounds)


def _repair(facts: Facts, bounds: Bounds) -> Step:
    bound = bounds.repair_rounds
    if facts.repair_rounds < bounds.repair_rounds:
        return _step(facts, "repair", "repair-round", next_stage="implement", bound=bound)
    return _step(
        facts,
        "pause",
        "repair-bound",
        next_stage=facts.stage,
        bound=bound,
        decision=decision_for(facts, "repair-bound"),
    )


def _retry(facts: Facts, bounds: Bounds) -> Step:
    bound = bounds.max_retries
    if facts.attempts < bounds.max_retries:
        return _step(facts, "retry", "retry-policy", next_stage=facts.stage, bound=bound)
    if facts.alternate is not None:
        return _step(facts, "reroute", "reroute-alternate", next_stage=facts.stage, bound=bound)
    return _step(
        facts,
        "pause",
        "retry-bound",
        next_stage=facts.stage,
        bound=bound,
        decision=decision_for(facts, "retry-bound"),
    )


def _succeeded(facts: Facts, bounds: Bounds) -> Step:
    if _is_repair(facts):
        return _repair(facts, bounds)
    if not facts.capacity_free:
        return _reschedule(facts)
    if facts.stage == "close":
        return _step(facts, "end", "mission-complete", next_stage=None)
    return _step(facts, "advance", "stage-order", next_stage=_following(facts.stage))


def _reschedule(facts: Facts) -> Step:
    return _step(facts, "reschedule", "reschedule-capacity", next_stage=facts.stage)


def _following(stage: str) -> str:
    """The next ``STAGE_ORDER`` entry. ``close`` ends and never advances."""
    return STAGE_ORDER[STAGE_ORDER.index(stage) + 1]


def _step(
    facts: Facts,
    kind: StepKind,
    rule_id: str,
    *,
    next_stage: str | None,
    bound: int | None = None,
    decision: CreateDecisionPayload | None = None,
) -> Step:
    return Step(
        kind=kind,
        stage=facts.stage,
        next_stage=next_stage,
        rule_id=rule_id,
        bound=bound,
        decision=decision,
    )
