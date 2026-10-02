"""Stage operations for the front orchestrator pipeline (OMP-417-s07-s04-s01).

Provides deterministic intake and confirm stage operations.
No constants or SQL statements are defined in this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omp_work.orchestrator import service
from omp_work.orchestrator.service import (
    Outcome,
    StageContext,
    register_stage_operation,
)
from omp_work.v1.service import WorkError

__all__ = [
    "register",
    "run_confirm",
    "run_intake",
]


def register() -> None:
    """Register front stage operations."""
    register_stage_operation("intake", run_intake)
    register_stage_operation("confirm", run_confirm)


def run_intake(ctx: StageContext) -> Outcome:
    """Run the intake stage operation: pins base_revision, drafts intake, and routes outcome."""
    key = f"intake-pin:{ctx.mission_id}:{ctx.step_index}"
    open_intent = ctx.intents.open(key)
    if open_intent is not None:
        target = open_intent.get("target") or {}
        base = target.get("base_revision") if isinstance(target, dict) else None
    else:
        try:
            mission_doc = ctx.service.read(
                ctx.principal, ctx.workspace_id, "mission", str(ctx.mission_id)
            )
            base = mission_doc.get("revision") if isinstance(mission_doc, dict) else None
        except WorkError as err:
            if err.code == "invalid_request":
                base = None
            else:
                raise
        ctx.intents.record_intent(key, "draft_mission_intake", {"base_revision": base})

    rq = ctx.request
    if isinstance(rq, Mapping):
        intake = rq.get("intake")
        scope = rq.get("scope")
        instruction = rq.get("instruction")
    else:
        intake = getattr(rq, "intake", None)
        scope = getattr(rq, "scope", None)
        instruction = getattr(rq, "instruction", None)

    if hasattr(intake, "model_dump"):
        intake = intake.model_dump(mode="json")
    if hasattr(scope, "model_dump"):
        scope = scope.model_dump(mode="json")
    if hasattr(instruction, "model_dump"):
        instruction = instruction.model_dump(mode="json")

    if intake is None:
        return Outcome(outcome="failed", data={"stage": "intake", "code": "intake_missing"})
    if scope is None:
        return Outcome(outcome="failed", data={"stage": "intake", "code": "scope_missing"})
    if instruction is None:
        return Outcome(outcome="failed", data={"stage": "intake", "code": "instruction_missing"})

    payload: dict[str, Any] = {
        "mission_id": str(ctx.mission_id),
        "base_revision": base,
        "intake": intake,
        "scope": scope,
        "instruction": instruction,
    }

    result = ctx.command("draft_mission_intake", payload)
    outcome = str(result.get("outcome") or "")

    if outcome in ("proceeded", "awaiting_owner"):
        mission_doc = result.get("mission") or {}
        mission_revision = mission_doc.get("revision")
        if mission_revision is None:
            m = ctx.service.read(
                ctx.principal, ctx.workspace_id, "mission", str(ctx.mission_id)
            )
            mission_revision = m.get("revision") if isinstance(m, dict) else None

        if mission_revision is None:
            return Outcome(
                outcome="failed",
                data={"stage": "intake", "code": "mission_revision_missing"},
            )

        work_op_id = str(service._ids(ctx.mission_id, "work"))
        work_op = ctx.service.read(
            ctx.principal, ctx.workspace_id, "operation", work_op_id
        )
        work_key = work_op["result"]["items"][0]["key"]

        data: dict[str, Any] = {
            "stage": "intake",
            "outcome": outcome,
            "mission_revision": mission_revision,
            "work_key": work_key,
        }
        decision_id = result.get("decision_id")
        if decision_id is not None:
            data["decision_id"] = str(decision_id)
        return Outcome(outcome="succeeded", data=data)

    if outcome in ("clarify", "held"):
        return Outcome(
            outcome="failed",
            blocker=f"intake_{outcome}",
            data={"stage": "intake", "outcome": outcome, "result": result},
        )

    return Outcome(
        outcome="failed",
        data={
            "stage": "intake",
            "code": "unknown_intake_outcome",
            "outcome": outcome,
            "result": result,
        },
    )


def run_confirm(ctx: StageContext) -> Outcome:
    """Run the confirm stage operation: checks mission status and waits or advances."""
    mission = ctx.service.read(
        ctx.principal, ctx.workspace_id, "mission", str(ctx.mission_id)
    )
    status = str(mission.get("status") or "")

    if status in ("approved", "running"):
        return Outcome(outcome="succeeded", data={"stage": "confirm", "status": status})

    if status == "awaiting_confirmation":
        intake_prior = ctx.prior.get("intake") or {}
        intake_data = intake_prior.get("data") or {}
        decision_id = intake_data.get("decision_id")
        if decision_id is None:
            return Outcome(
                outcome="failed",
                data={"stage": "confirm", "code": "decision_id_missing", "status": status},
            )
        return Outcome(
            outcome="waiting",
            data={"stage": "confirm", "status": status, "decision_id": str(decision_id)},
        )

    if status == "abandoned":
        return Outcome(
            outcome="failed",
            data={"stage": "confirm", "status": status, "reason": "abandoned"},
        )

    return Outcome(outcome="failed", data={"stage": "confirm", "status": status})
