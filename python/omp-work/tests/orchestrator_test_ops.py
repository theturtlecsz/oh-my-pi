"""Stage operations whose behavior comes from the mission request's ``controls``.

Each stage reads ``request["controls"][stage]``. ``fails`` returns that many
failed visits. ``once`` applies ``action``, ``new_scope``, ``budget_exhausted``
or ``blocker`` on the first visit only. ``hold`` blocks before ``act`` (a stop
can land with no side effect). ``block`` blocks inside the executor, after the
intent is recorded. ``effect`` is the file ``act`` appends one ``1`` to.
"""

from __future__ import annotations

import time
from pathlib import Path

from omp_work.action_classify import ResolvedTarget
from omp_work.orchestrator.service import Outcome, StageContext, register_stage_operation
from omp_work.orchestrator.stages import STAGE_ORDER, ActionVerdict


def register() -> None:
    """Bind every stage to :func:`run_stage`."""
    for stage in STAGE_ORDER:
        register_stage_operation(stage, run_stage)


def run_stage(ctx: StageContext) -> Outcome:
    """One stub visit of ``ctx.stage``."""
    import hashlib
    from uuid import uuid5

    from omp_work.orchestrator.verifier import sign_verdict

    controls = _controls(ctx)
    visits = int((ctx.prior.get(ctx.stage) or {}).get("visits") or 0)
    hold = controls.get("hold")
    if hold:
        _wait(Path(str(hold)))
    operation_id = None
    if controls.get("effect") or controls.get("block"):
        result = ctx.act(
            {"kind": "read_state"},
            ResolvedTarget(),
            _executor(controls),
            rule_id="stage-effect",
        )
        operation_id = result.operation_id
        if result.status != "done":
            return Outcome(
                outcome="failed",
                data={"stage": ctx.stage, "operation_id": operation_id, "code": result.code},
            )
    if controls.get("fails") is not None and visits < int(controls["fails"]):
        return Outcome(outcome="failed", data={"stage": ctx.stage, "visits": visits})
    once = bool(controls.get("once")) and visits == 0
    action = None
    if once and isinstance(controls.get("action"), dict):
        raw = controls["action"]
        action = ActionVerdict(
            tier=int(raw["tier"]),
            action_class=str(raw["action_class"]),
            target_sha256=None if raw.get("target_sha256") is None else str(raw["target_sha256"]),
            allowed=bool(raw.get("allowed", False)),
            code=str(raw.get("code") or "refused"),
        )
    blocker = controls.get("blocker") if once else None
    if ctx.stage == "audit" and not hold and int(controls.get("fails") or 0) <= 0:
        key = ctx.config.verifier_key_path
        if not key.exists() or key.stat().st_size == 0:
            key.write_bytes(b"0123456789abcdef0123456789abcdef")
            key.chmod(0o600)
        if action is None and blocker is None and not (
            once and (controls.get("new_scope") or controls.get("budget_exhausted"))
        ):
            record = sign_verdict(
                key.read_bytes(),
                verifier_id="verifier",
                candidate_sha256=hashlib.sha256(f"stub:{ctx.mission_id}".encode()).hexdigest(),
                attempt_id=str(uuid5(ctx.mission_id, "stub-audit")),
                verdict="pass",
            )
            ctx.intents.record_intent(
                f"audit-accept:{ctx.mission_id}:{ctx.step_index}", "sign_verdict", record
            )
    return Outcome(
        outcome="succeeded",
        data={"stage": ctx.stage, "visits": visits, "operation_id": operation_id},
        action=action,
        new_scope=bool(once and controls.get("new_scope")),
        budget_exhausted=bool(once and controls.get("budget_exhausted")),
        blocker=None if not isinstance(blocker, str) else blocker,
        verdict="pass" if ctx.stage == "audit" else "none",
    )


def _controls(ctx: StageContext) -> dict:
    raw = ctx.request.get("controls") if isinstance(ctx.request, dict) else None
    if not isinstance(raw, dict):
        return {}
    stage = raw.get(ctx.stage)
    return stage if isinstance(stage, dict) else {}


def _wait(path: Path) -> None:
    while path.exists():
        time.sleep(0.05)


def _executor(controls: dict):
    block = controls.get("block")
    effect = controls.get("effect")

    def execute(_operation, _resolved) -> None:
        if block:
            _wait(Path(str(block)))
        if effect:
            path = Path(str(effect))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("1")

    return execute
