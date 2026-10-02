"""Stage operations for the front orchestrator pipeline (OMP-417-s07-s04).

Provides deterministic intake, confirm, plan, and grant stage operations.
No constants or SQL statements are defined in this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sys
from typing import Any
from uuid import UUID

import omp_work
from omp_work import control_actions
from omp_work.mission_intake_rules import scope_draft
from omp_work.operations import database, fingerprints
from omp_work.orchestrator import candidate_git, service, verifier, worker_sandbox
from omp_work.orchestrator.service import (
    OrchestratorConfig,
    Outcome,
    StageContext,
    register_stage_operation,
)
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import MissionDraft
from omp_work.v1.service import WorkError

__all__ = [
    "grant_owned",
    "judge_manifest",
    "register",
    "run_confirm",
    "run_grant",
    "run_intake",
    "run_plan",
]


def register() -> None:
    """Register front stage operations."""
    register_stage_operation("intake", run_intake)
    register_stage_operation("confirm", run_confirm)
    register_stage_operation("plan", run_plan)
    register_stage_operation("grant", run_grant)


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


def run_plan(ctx: StageContext) -> Outcome:
    """Run the plan stage operation: admits mission to project, links work item, and starts mission."""
    mission = ctx.service.read(
        ctx.principal, ctx.workspace_id, "mission", str(ctx.mission_id)
    )
    approved_scope = (
        mission.get("approved_scope") if isinstance(mission, dict) else None
    )
    if approved_scope is None or not isinstance(approved_scope, dict):
        return Outcome(
            outcome="failed",
            data={"stage": "plan", "code": "mission_not_admitted"},
        )

    envelope_data = approved_scope.get("envelope")
    if envelope_data is None:
        return Outcome(
            outcome="failed",
            data={"stage": "plan", "code": "mission_not_admitted"},
        )

    envelope = (
        envelope_data
        if isinstance(envelope_data, MissionDraft)
        else MissionDraft.model_validate(envelope_data)
    )

    draft_scope = scope_draft(envelope)
    workspace_id = UUID(str(ctx.workspace_id))
    actor_id = UUID(str(ctx.principal.actor_id))
    project_id = UUID(str(ctx.project_id))
    mission_id = UUID(str(ctx.mission_id))

    ctx.projects.admit_mission(
        workspace_id,
        actor_id,
        project_id,
        mission_id,
        envelope.objective,
        draft_scope,
    )

    project_doc = ctx.projects.read_project(workspace_id, actor_id, project_id)
    missions = project_doc.get("missions") or []
    mission_row = next(
        (
            m
            for m in missions
            if isinstance(m, dict) and str(m.get("mission_id")) == str(ctx.mission_id)
        ),
        None,
    )
    if mission_row is None or mission_row.get("status") != "approved":
        return Outcome(
            outcome="failed",
            data={"stage": "plan", "code": "mission_not_admitted"},
        )

    links = mission.get("links") or ()
    already_linked = any(
        isinstance(link, dict) and str(link.get("work_id")) == str(ctx.work_id)
        for link in links
    )
    if not already_linked:
        ctx.command(
            "link_mission_work",
            {"mission_id": str(ctx.mission_id), "work_id": str(ctx.work_id)},
        )

    status = str(mission.get("status") or "")
    if status != "running":
        ctx.command(
            "set_mission_status",
            {
                "mission_id": str(ctx.mission_id),
                "target_status": "running",
                "cause_kind": "policy_rule",
                "policy_rule_id": "stage-order",
            },
        )

    return Outcome(outcome="succeeded", data={"stage": "plan"})


def _request_mapping(request: object) -> Mapping[str, Any]:
    if isinstance(request, Mapping):
        return request
    raise WorkError("invalid_request", diagnostics=("request is not a mapping",))


def _owner_input_id(ctx: StageContext, intake_data: Mapping[str, Any]) -> str:
    decision_id = intake_data.get("decision_id")
    if decision_id is not None:
        return str(decision_id)
    project = ctx.projects.read_project(
        UUID(str(ctx.workspace_id)),
        UUID(str(ctx.principal.actor_id)),
        UUID(str(ctx.project_id)),
    )
    mandate = project.get("standing_mandate") or {}
    if not isinstance(mandate, Mapping):
        raise WorkError("invalid_request", diagnostics=("standing mandate missing",))
    return str(mandate["mandate_id"])


def grant_owned(ctx: StageContext, view: Mapping[str, Any]) -> bool:
    """A grant view belongs to this mission only when its id is the mission grant
    id and it claims this stage's work item in this stage's project."""
    gid = service._ids(ctx.mission_id, "grant")
    grant = view.get("grant")
    if not isinstance(grant, Mapping) or str(grant.get("grant_id")) != str(gid):
        return False
    items = view.get("items") or ()
    return any(
        isinstance(item, Mapping)
        and str(item.get("work_id")) == str(ctx.work_id)
        and str(item.get("project_id")) == str(ctx.project_id)
        for item in items
    )


def run_grant(ctx: StageContext) -> Outcome:
    """Begin one execution grant, or report the grant this mission already has."""
    gid = service._ids(ctx.mission_id, "grant")
    judged = judge_manifest(ctx.config)
    if judged is None:
        return Outcome(
            outcome="failed",
            data={"stage": "grant", "code": "verifier_argv_missing"},
        )
    judge_sha, manifest = judged
    try:
        view = ctx.service.read(
            ctx.principal, ctx.workspace_id, "execution", str(gid)
        )
    except WorkError as err:
        if err.code not in {"invalid_request", "not_found"}:
            raise
        view = None
    if view is not None:
        if not grant_owned(ctx, view):
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "grant_foreign"},
            )
        grant = view["grant"]
        return Outcome(
            outcome="succeeded",
            data={
                "stage": "grant",
                "grant_id": str(grant["grant_id"]),
                "grant_version": grant["grant_version"],
                "state": grant["state"],
            },
        )

    rq = _request_mapping(ctx.request)
    intake_data = (ctx.prior.get("intake") or {}).get("data") or {}
    if not isinstance(intake_data, Mapping):
        intake_data = {}
    key = str(intake_data["work_key"])
    item = ctx.service.read(ctx.principal, ctx.workspace_id, "item", key)
    work = rq["work"]
    if isinstance(work, Mapping):
        description = str(work["description"])
    else:
        description = str(getattr(work, "description"))
    actor_id = ctx.principal.actor_id
    focus = ctx.service.read(ctx.principal, ctx.workspace_id, "focus", str(actor_id))
    issued_at = datetime.now(UTC).isoformat()
    project_id = item.get("project_id")
    payload: dict[str, Any] = {
        "grant_id": str(gid),
        "provenance": {
            "owner_input_id": _owner_input_id(ctx, intake_data),
            "owner_session_id": f"orchestrator:{ctx.mission_id}",
            "normalized_command": f"/execute {key}",
            "workspace_id": str(ctx.workspace_id),
            "repository": rq["repository"],
            "nonce": str(service._ids(ctx.mission_id, "nonce")),
            "issued_at": issued_at,
        },
        "remote_ref": rq["candidate_ref"],
        "mode": "single",
        "items": [
            {
                "work_id": str(item["work_id"]),
                "revision_id": str(item["revision"]["revision_id"]),
                "position": 0,
                "original_request": description,
                "original_request_sha256": text_sha256(description),
                "initial_git_baseline": rq["base_commit"],
                "project_id": None if project_id is None else str(project_id),
            }
        ],
        "expected_focus_version": int(focus["version"]),
        "judge_sha256": judge_sha,
        "judge_manifest": manifest,
    }
    try:
        ctx.command("begin_execution", payload)
    except WorkError as err:
        if err.code == "idempotency_conflict":
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "grant_conflict"},
            )
        raise
    return Outcome(
        outcome="succeeded",
        data={
            "stage": "grant",
            "grant_id": str(gid),
            "judge_sha256": judge_sha,
            "issued_at": issued_at,
        },
    )


def _file_sha256(mod: object) -> str:
    path = getattr(mod, "__file__", None)
    if not path:
        raise ValueError(f"module {mod} has no __file__")
    file_path = Path(path)
    if file_path.suffix in (".pyc", ".pyo"):
        file_path = file_path.with_suffix(".py")
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def judge_manifest(config: OrchestratorConfig) -> tuple[str, dict] | None:
    """Build and seal the execution judge manifest for an orchestrator config."""
    if not config.verifier_argv:
        return None

    manifest = {
        "auditor_agent_sha256": sha256(
            {
                "verifier_argv": list(config.verifier_argv),
                "verifier_sha256": _file_sha256(verifier),
            }
        ),
        "host_sha256": _file_sha256(service),
        "adapter_sha256": _file_sha256(sys.modules[__name__]),
        "freeze_sha256": _file_sha256(candidate_git),
        "runner_sha256": _file_sha256(worker_sandbox),
        "executor_sha256": _file_sha256(control_actions),
        "contract_sha256": omp_work.contract_sha256(),
        "service_fingerprint": fingerprints.service_runtime_fingerprint(),
        "service_code_fingerprint": fingerprints.code_fingerprint(),
        "service_migration_sha256": database.migration_set_sha256(),
    }
    return sha256(manifest), manifest

