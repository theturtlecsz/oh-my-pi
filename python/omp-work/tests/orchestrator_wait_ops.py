"""Confirm and merge stage operations for the owner-wait orchestrator (OMP-417-s07).

``confirm`` files one stable ``draft_mission_intake`` and waits on that decision.
``merge`` pushes ``main`` through ``StageContext.act``; the executor appends one
``1`` to the effect file named by ``controls["merge"]["effect"]``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator.service import Outcome, StageContext, register_stage_operation
from omp_work.orchestrator.stages import ActionVerdict
from omp_work.standing_policy import RepositoryRecord
from omp_work.v1.canonical import text_sha256

_RULE = "d41-merge-approval"
_COMMIT = "0123456789abcdef0123" * 2
_TEXT = "Confirm the orchestrated change."
_READY = frozenset({"approved", "running", "paused"})
_INTAKE = {
    "archetype": "small_code_change",
    "source": {"text": _TEXT, "sha256": text_sha256(_TEXT), "spans": []},
    "goal": {
        "id": "goal-1",
        "statement": "Ship the orchestrated change",
        "source_span_ids": [],
    },
    "acceptance_criteria": [
        {
            "id": "ac-1",
            "statement": "tests pass",
            "source_span_ids": [],
            "observable_outcome": "tests pass",
            "oracle": "automated_test",
        }
    ],
}


def register() -> None:
    """Bind confirm and merge. Every other stage stays unregistered."""
    register_stage_operation("confirm", run_confirm)
    register_stage_operation("merge", run_merge)


def push_submission() -> dict:
    """The git push ``run_merge`` asks ``act`` to authorize."""
    return {
        "kind": "git_push",
        "repository": "repo",
        "branch": "main",
        "commit": _COMMIT,
    }


def push_target() -> ResolvedTarget:
    """``main`` is the repository default and a protected branch."""
    return ResolvedTarget(
        repository=RepositoryRecord(
            key="repo",
            default_branch="main",
            protected_branches=("main",),
        ),
        commit=_COMMIT,
    )


def run_confirm(ctx: StageContext) -> Outcome:
    """Wait on the intake decision, then succeed once the owner has approved it."""
    mission = ctx._handler._mission()
    if str(mission.get("status") or "") in _READY:
        return Outcome(outcome="succeeded", data={"stage": "confirm"})
    scope = ctx.request.get("scope") if isinstance(ctx.request, dict) else None
    instruction = ctx.request.get("instruction") if isinstance(ctx.request, dict) else None
    result = ctx.command(
        "draft_mission_intake",
        {
            "mission_id": str(ctx.mission_id),
            "intake": _INTAKE,
            "scope": scope if isinstance(scope, dict) else {},
            "instruction": instruction if isinstance(instruction, dict) else None,
        },
        key="draft",
    )
    outcome = result.get("outcome")
    decision_id = result.get("decision_id")
    if outcome == "proceeded":
        return Outcome(outcome="succeeded", data={"stage": "confirm", "decision_id": decision_id})
    if outcome == "awaiting_owner" and decision_id:
        return Outcome(
            outcome="waiting",
            data={"stage": "confirm", "decision_id": decision_id},
        )
    return Outcome(outcome="failed", data={"stage": "confirm", "result": result})


def run_merge(ctx: StageContext) -> Outcome:
    """Authorize one protected push. A second ``act`` is the same operation."""
    controls = _controls(ctx)
    if controls.get("plant_intent"):
        ctx.intents.record_intent(
            _operation_id(ctx),
            "git_push",
            {"resource_id": None},
        )
    operation = push_submission()
    resolved = push_target()
    first = ctx.act(operation, resolved, _executor(controls), rule_id=_RULE)
    second = ctx.act(operation, resolved, _executor(controls), rule_id=_RULE)
    classified = classify(parse_submission(operation), resolved)
    allowed = first.status == "done" and second.status == "done"
    data = {
        "stage": "merge",
        "candidate_commit": _COMMIT,
        "target_ref": "main",
        "operation_id": first.operation_id,
        "decision_id": first.decision_id or second.decision_id,
        "second_status": second.status,
        "answered": ctx.answered_decision(_RULE),
    }
    if first.status != second.status:
        data["first_status"] = first.status
        data["code"] = second.code
    return Outcome(
        outcome="succeeded" if allowed else "failed",
        data=data,
        action=ActionVerdict(
            tier=classified.tier,
            action_class=classified.action_class,
            target_sha256=classified.target_sha256,
            allowed=allowed,
            code="allowed" if allowed else (first.code or first.status),
        ),
    )


def _operation_id(ctx: StageContext) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"omp-417:{ctx.mission_id}:{ctx.step_index}:{_RULE}:git_push:",
        )
    )


def _controls(ctx: StageContext) -> dict:
    raw = ctx.request.get("controls") if isinstance(ctx.request, dict) else None
    if not isinstance(raw, dict):
        return {}
    stage = raw.get("merge")
    return stage if isinstance(stage, dict) else {}


def _executor(controls: dict):
    effect = controls.get("effect")

    def execute(_operation, _resolved) -> None:
        if not effect:
            return
        path = Path(str(effect))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("1")

    return execute
