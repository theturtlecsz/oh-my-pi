"""Stage operations for the orchestrator pipeline (OMP-417-s07).

Provides deterministic intake, confirm, plan, grant, implement, evaluate,
freeze, and push stage operations. Repair runs the implement operation again.
No SQL statements are defined in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import omp_work
from omp_work import control_actions
from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.mission_intake_rules import scope_draft
from omp_work.operations import database, fingerprints
from omp_work.operations.capabilities import provision_task_capability
from omp_work.orchestrator import candidate_git, service, verifier, worker_sandbox
from omp_work.orchestrator.service import (
    OrchestratorConfig,
    Outcome,
    StageContext,
    register_stage_operation,
)
from omp_work.orchestrator.stages import ActionVerdict
from omp_work.standing_policy import RepositoryRecord
from omp_work.v1.canonical import canonical_json, sha256, text_sha256
from omp_work.v1.models import (
    EvidenceKind,
    EvidenceReceipt,
    MAX_AUDITOR_LAUNCHES,
    MissionDraft,
)
from omp_work.v1.service import WorkError

__all__ = [
    "grant_owned",
    "judge_manifest",
    "register",
    "run_audit",
    "run_confirm",
    "run_evaluate",
    "run_freeze",
    "run_grant",
    "run_implement",
    "run_intake",
    "run_plan",
    "run_push",
]


def register() -> None:
    """Register stage operations. Repair re-enters implement."""
    register_stage_operation("intake", run_intake)
    register_stage_operation("confirm", run_confirm)
    register_stage_operation("plan", run_plan)
    register_stage_operation("grant", run_grant)
    register_stage_operation("implement", run_implement)
    register_stage_operation("evaluate", run_evaluate)
    register_stage_operation("freeze", run_freeze)
    register_stage_operation("push", run_push)
    register_stage_operation("audit", run_audit)


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


_MAX_GRANT_PHASE_PASSES = 3


def run_grant(ctx: StageContext) -> Outcome:
    """Begin or resume one execution grant, then drive the active item's phase."""
    gid = service._ids(ctx.mission_id, "grant")
    pin_key = f"grant-issue:{ctx.mission_id}"

    def _attach_pin(outcome: Outcome) -> Outcome:
        if outcome.outcome == "succeeded":
            pin = ctx.intents.open(pin_key)
            if pin is not None:
                target = pin.get("target")
                if isinstance(target, str):
                    try:
                        target = json.loads(target)
                    except Exception:
                        target = None
                if isinstance(target, Mapping) and "issued_at" in target:
                    outcome.data["issued_at"] = target["issued_at"]
        return outcome

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
        expires_at = grant.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            else:
                expires_at = expires_at.astimezone(UTC)
        if (
            grant.get("state") != "active"
            or expires_at is None
            or expires_at <= datetime.now(UTC)
        ):
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "execution_grant_inactive"},
            )
        if grant.get("judge_sha256") != judge_sha:
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "judge_manifest_drift"},
            )
        return _attach_pin(_grant_phase(ctx, gid))

    open_pin = ctx.intents.open(pin_key)
    target = open_pin.get("target") if isinstance(open_pin, Mapping) else None
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except Exception:
            target = None
    issued_at = target.get("issued_at") if isinstance(target, Mapping) else None
    if issued_at is None:
        issued_at = datetime.now(UTC).isoformat()
        ctx.intents.record_intent(pin_key, "begin_execution", {"issued_at": issued_at})

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
    return _attach_pin(_grant_phase(ctx, gid))


def _grant_phase(ctx: StageContext, gid: UUID) -> Outcome:
    """Drive the owned grant's active item through at most three re-read passes.

    Every pass re-reads the execution view and re-runs ``grant_owned``, so no
    version or revision from an earlier pass or a command's return is reused.
    ``criteria_pending`` seals the request's acceptance criteria; ``planning``
    stamps the execution plan; ``executing`` is the grant stage's success. Any
    other phase, or a phase that repeats, fails with ``grant_phase_<phase>``.
    """
    previous_phase: str | None = None
    for _ in range(_MAX_GRANT_PHASE_PASSES):
        view = ctx.service.read(ctx.principal, ctx.workspace_id, "execution", str(gid))
        if not grant_owned(ctx, view):
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "grant_foreign"},
            )
        grant = view["grant"]
        item = view.get("active_item")
        if not isinstance(item, Mapping):
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": "grant_phase_missing"},
            )
        phase = str(item.get("phase"))
        if phase == previous_phase:
            return Outcome(
                outcome="failed",
                data={"stage": "grant", "code": f"grant_phase_{phase}", "phase": phase},
            )
        if phase == "executing":
            return Outcome(
                outcome="succeeded",
                data={
                    "stage": "grant",
                    "grant_id": str(grant["grant_id"]),
                    "grant_version": grant["grant_version"],
                    "phase": phase,
                    "candidate_id": str(service._ids(ctx.mission_id, "plan-candidate")),
                },
            )
        if phase == "criteria_pending":
            work = _request_mapping(ctx.request)["work"]
            if isinstance(work, Mapping):
                raw_criteria = work["acceptance_criteria"]
                description = str(work["description"])
            else:
                raw_criteria = work.acceptance_criteria
                description = str(work.description)
            criteria = tuple(str(criterion) for criterion in raw_criteria)
            if not criteria:
                return Outcome(
                    outcome="failed",
                    data={
                        "stage": "grant",
                        "code": "criteria_missing",
                        "phase": phase,
                    },
                )
            try:
                ctx.command(
                    "seal_execution_criteria",
                    {
                        "grant_id": str(gid),
                        "expected_grant_version": grant["grant_version"],
                        "work_id": str(ctx.work_id),
                        "expected_revision_id": str(item["claimed_revision_id"]),
                        "criteria": list(criteria),
                        "description_sha256": text_sha256(description),
                        "judge_sha256": grant["judge_sha256"],
                    },
                )
            except WorkError as err:
                if err.code == "idempotency_conflict":
                    return Outcome(
                        outcome="failed",
                        data={"stage": "grant", "code": "grant_conflict"},
                    )
                raise
            previous_phase = phase
            continue
        if phase == "planning":
            rq = _request_mapping(ctx.request)
            raw_test_cmd = rq.get("test_command") or ()
            test_command = [str(x) for x in raw_test_cmd]
            if not test_command:
                return Outcome(
                    outcome="failed",
                    data={
                        "stage": "grant",
                        "code": "test_command_missing",
                        "phase": phase,
                    },
                )
            pid = service._ids(ctx.mission_id, "plan-candidate")
            intake = rq.get("intake") or {}
            if hasattr(intake, "model_dump"):
                intake = intake.model_dump(mode="json")
            if isinstance(intake, Mapping):
                goal = intake.get("goal") or {}
                if hasattr(goal, "model_dump"):
                    goal = goal.model_dump(mode="json")
                goal_statement = str(
                    goal.get("statement")
                    if isinstance(goal, Mapping)
                    else getattr(goal, "statement", "")
                )
                raw_constraints = intake.get("constraints") or ()
            else:
                goal = getattr(intake, "goal", None)
                goal_statement = str(getattr(goal, "statement", ""))
                raw_constraints = getattr(intake, "constraints", ())

            approach: list[str] = [goal_statement]
            for c in raw_constraints:
                if hasattr(c, "model_dump"):
                    c = c.model_dump(mode="json")
                stmt = (
                    c.get("statement")
                    if isinstance(c, Mapping)
                    else getattr(c, "statement", "")
                )
                approach.append(str(stmt))

            verification = test_command
            raw_paths = rq.get("allowed_paths") or ()
            paths = [str(x) for x in raw_paths]
            candidate_sha = sha256(
                {
                    "base_commit": rq["base_commit"],
                    "allowed_paths": rq["allowed_paths"],
                }
            )
            plan_body = canonical_json(
                {
                    "approach": approach,
                    "verification": verification,
                    "paths": paths,
                }
            )
            plan_sha = text_sha256(plan_body)
            ctx.command(
                "stamp_execution_plan",
                {
                    "grant_id": str(gid),
                    "expected_grant_version": grant["grant_version"],
                    "work_id": str(ctx.work_id),
                    "revision_id": str(item["criteria_revision_id"]),
                    "candidate_id": str(pid),
                    "approach": approach,
                    "verification": verification,
                    "paths": paths,
                    "candidate_sha256": candidate_sha,
                    "plan_body": plan_body,
                    "plan_sha256": plan_sha,
                    "plan_file": f"orchestrator:{ctx.mission_id}",
                    "judge_sha256": grant["judge_sha256"],
                },
            )
            previous_phase = phase
            continue
        return Outcome(
            outcome="failed",
            data={"stage": "grant", "code": f"grant_phase_{phase}", "phase": phase},
        )
    return Outcome(
        outcome="failed",
        data={"stage": "grant", "code": "grant_phase_unsettled"},
    )


def _argv(request: Mapping[str, Any], key: str) -> list[str]:
    raw = request.get(key) or ()
    return [str(part) for part in raw]


def _intake_data(ctx: StageContext) -> Mapping[str, Any]:
    data = (ctx.prior.get("intake") or {}).get("data") or {}
    if not isinstance(data, Mapping):
        raise WorkError("invalid_request", diagnostics=("intake data missing",))
    return data


def _work_key(ctx: StageContext) -> str:
    key = _intake_data(ctx).get("work_key")
    if not isinstance(key, str) or key == "":
        raise WorkError("invalid_request", diagnostics=("work_key missing",))
    return key


def _revision_id(ctx: StageContext) -> str:
    item = ctx.service.read(ctx.principal, ctx.workspace_id, "item", _work_key(ctx))
    revision = item.get("revision")
    if not isinstance(revision, Mapping) or not revision.get("revision_id"):
        raise WorkError("invalid_request", diagnostics=("revision missing",))
    return str(revision["revision_id"])


def _implement_data(ctx: StageContext) -> Mapping[str, Any]:
    data = (ctx.prior.get("implement") or {}).get("data") or {}
    if not isinstance(data, Mapping):
        return {}
    return data


def _worktree_name(ctx: StageContext) -> str:
    """``{mission}-{step}``. A retry in the same repair round resets that tree."""
    current = f"{ctx.mission_id}-{ctx.step_index}"
    data = _implement_data(ctx)
    raw = data.get("worktree")
    if not isinstance(raw, str) or raw == "":
        return current
    try:
        previous_round = int(data["repair_round"])
    except (KeyError, TypeError, ValueError):
        return current
    if previous_round != ctx.repair_round:
        return current
    return Path(raw).name


def _sandbox(
    ctx: StageContext,
    argv: Sequence[str],
    worktree: Path,
    *,
    identity: str = "worker",
    ro_binds: Sequence[str | Path] = (),
) -> int | None:
    """Run ``argv`` in the worker jail until the stage lease expires.

    ``None`` means the lease is already dead or the process timed out, so the
    caller records ``crashed``. ``WorkerSandboxRefused`` propagates.
    """
    exp = ctx.lease_expires_at()
    if exp is None:
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    else:
        exp = exp.astimezone(UTC)
    remaining = (exp - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return None
    planned = service._ids(ctx.mission_id, "plan-candidate")
    capability = provision_task_capability(
        ctx.config.ops,
        workspace_id=ctx.workspace_id,
        candidate_ids=(planned,),
        expires_at=exp,
        name=f"task-{ctx.mission_id}-{ctx.step_index}-{ctx.stage}",
    )
    token = str(json.loads(capability.read_text(encoding="utf-8"))["token"])
    try:
        return worker_sandbox.run_worker(
            list(argv),
            worktree=worktree,
            token=token,
            workservice_socket=ctx.config.workservice_url,
            identity=identity,
            ro_binds=ro_binds,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return None


def run_implement(ctx: StageContext) -> Outcome:
    """Reset this step's worktree, run the worker, and surface a new scope."""
    request = _request_mapping(ctx.request)
    argv = _argv(request, "worker_argv")
    if not argv:
        return Outcome(
            outcome="crashed",
            data={"stage": "implement", "code": "worker_argv_missing"},
        )
    worktree = candidate_git.reset_worktree(
        ctx.config.control_repo,
        ctx.config.worktrees_dir,
        _worktree_name(ctx),
        str(request["base_commit"]),
    )
    data: dict[str, Any] = {
        "stage": "implement",
        "worktree": str(worktree),
        "identity": "worker",
        "repair_round": ctx.repair_round,
    }
    try:
        code = _sandbox(ctx, argv, worktree)
    except worker_sandbox.WorkerSandboxRefused as err:
        data["code"] = err.code
        return Outcome(outcome="crashed", data=data)
    data["exit_code"] = code
    if code is None:
        data["code"] = "lease_dead"
        return Outcome(outcome="crashed", data=data)
    if code != 0:
        return Outcome(outcome="crashed", data=data)
    scope_path = worktree / ".omp" / "new-scope.json"
    new_scope = scope_path.is_file()
    if new_scope:
        scope_path.unlink()
    return Outcome(outcome="succeeded", data=data, new_scope=new_scope)


def run_evaluate(ctx: StageContext) -> Outcome:
    """Run the test command in the implement worktree."""
    request = _request_mapping(ctx.request)
    command = _argv(request, "test_command")
    raw = _implement_data(ctx).get("worktree")
    finished_at = datetime.now(UTC).isoformat()
    data: dict[str, Any] = {
        "stage": "evaluate",
        "command": command,
        "identity": "worker",
        "finished_at": finished_at,
    }
    if not isinstance(raw, str) or raw == "":
        data["code"] = "implement_worktree_missing"
        return Outcome(outcome="failed", data=data)
    if not command:
        data["code"] = "test_command_missing"
        return Outcome(outcome="failed", data=data)
    worktree = Path(raw)
    try:
        code = _sandbox(ctx, command, worktree)
    except worker_sandbox.WorkerSandboxRefused as err:
        data["code"] = err.code
        return Outcome(outcome="crashed", data=data)
    data["exit_code"] = code
    if code is None:
        data["code"] = "lease_dead"
        return Outcome(outcome="crashed", data=data)
    if code != 0:
        return Outcome(outcome="failed", data=data)
    return Outcome(outcome="succeeded", data=data)


def _repository_record(ctx: StageContext, key: str) -> RepositoryRecord | None:
    project = ctx.projects.read_project(
        UUID(str(ctx.workspace_id)),
        UUID(str(ctx.principal.actor_id)),
        UUID(str(ctx.project_id)),
    )
    repositories = project.get("repositories") or ()
    row = next(
        (
            item
            for item in repositories
            if isinstance(item, Mapping) and str(item.get("key")) == key
        ),
        None,
    )
    if row is None:
        return None
    protected = row.get("protected_branches") or ()
    return RepositoryRecord(
        key=str(row["key"]),
        default_branch=str(row["default_branch"]),
        protected_branches=tuple(str(item) for item in protected),
        automation_ci_secret_free=bool(row.get("automation_ci_secret_free")),
    )


def _verdict(
    operation: Mapping[str, Any], resolved: ResolvedTarget, *, allowed: bool, code: str
) -> ActionVerdict:
    classified = classify(parse_submission(dict(operation)), resolved)
    return ActionVerdict(
        tier=classified.tier,
        action_class=classified.action_class,
        target_sha256=classified.target_sha256,
        allowed=allowed,
        code=code,
    )


def run_freeze(ctx: StageContext) -> Outcome:
    """Commit the candidate when the envelope is clean, then finalize it."""
    request = _request_mapping(ctx.request)
    raw = _implement_data(ctx).get("worktree")
    if not isinstance(raw, str) or raw == "":
        return Outcome(
            outcome="failed",
            data={"stage": "freeze", "code": "implement_worktree_missing"},
        )
    worktree = Path(raw)
    base = str(request["base_commit"])
    allowed = _argv(request, "allowed_paths")
    repository = ctx.config.control_repo
    try:
        violations = candidate_git.check_envelope(
            worktree,
            base,
            allowed,
            repository=repository,
            live_checkout=ctx.config.live_checkout,
        )
    except candidate_git.CandidateGitError as err:
        return Outcome(
            outcome="failed",
            data={"stage": "freeze", "code": err.code, "codes": list(err.violations)},
        )
    if violations:
        return Outcome(
            outcome="failed",
            data={"stage": "freeze", "code": "envelope_violation", "codes": violations},
        )
    try:
        commit = candidate_git.freeze(
            repository,
            worktree,
            base,
            allowed,
            repository=repository,
            live_checkout=ctx.config.live_checkout,
            message=f"candidate {ctx.mission_id}",
            author="Orchestrator <orchestrator@omp.local>",
        )
    except candidate_git.CandidateGitError as err:
        return Outcome(
            outcome="failed",
            data={"stage": "freeze", "code": err.code, "codes": list(err.violations)},
        )
    audited = candidate_git.audit_inputs(repository, base, commit)
    diff_sha = str(audited["diff_sha256"])
    candidate_id = service._ids(ctx.mission_id, f"final:{diff_sha}")
    revision_id = _revision_id(ctx)
    payload = {
        "work_id": str(ctx.work_id),
        "revision_id": revision_id,
        "planned_candidate_id": str(service._ids(ctx.mission_id, "plan-candidate")),
        "candidate_id": str(candidate_id),
        "candidate_sha256": diff_sha,
        "commit_sha": commit,
    }
    try:
        result = ctx.command("finalize_candidate", payload, key=f"finalize:{diff_sha}")
    except WorkError as err:
        if err.code == "stale_evidence":
            return Outcome(
                outcome="failed",
                data={"stage": "freeze", "code": "stale_evidence"},
            )
        raise
    final_id = str(candidate_id)
    returned = result.get("candidate") if isinstance(result, Mapping) else None
    if isinstance(returned, Mapping) and returned.get("candidate_id"):
        final_id = str(returned["candidate_id"])
    return Outcome(
        outcome="succeeded",
        data={
            "stage": "freeze",
            "candidate_commit": commit,
            "candidate_id": final_id,
            "candidate_sha256": diff_sha,
            "revision_id": revision_id,
            "worktree": str(worktree),
        },
    )


def _push_receipt_exists(ctx: StageContext, commit: str) -> bool:
    view = ctx.service.read(ctx.principal, ctx.workspace_id, "workflow", _work_key(ctx))
    receipts = view.get("receipts") or ()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        if str(receipt.get("kind") or "") != EvidenceKind.PUSH.value:
            continue
        if commit in {
            str(receipt.get("candidate_commit") or ""),
            str(receipt.get("remote_commit") or ""),
        }:
            return True
    return False


def run_push(ctx: StageContext) -> Outcome:
    """Push the frozen commit to ``candidate_ref`` and record one push receipt."""
    request = _request_mapping(ctx.request)
    frozen = (ctx.prior.get("freeze") or {}).get("data") or {}
    if not isinstance(frozen, Mapping):
        frozen = {}
    commit = frozen.get("candidate_commit")
    candidate_id = frozen.get("candidate_id")
    candidate_sha = frozen.get("candidate_sha256")
    revision_id = frozen.get("revision_id")
    if not all(
        isinstance(item, str) and item
        for item in (commit, candidate_id, candidate_sha, revision_id)
    ):
        return Outcome(
            outcome="failed",
            data={"stage": "push", "code": "freeze_incomplete"},
        )
    commit = str(commit)
    ref = str(request["candidate_ref"])
    remote = str(request["remote"])
    repository_key = str(request["repository"])
    record = _repository_record(ctx, repository_key)
    operation = {
        "kind": "git_push",
        "repository": repository_key,
        "branch": ref,
        "commit": commit,
    }
    if record is None:
        return Outcome(
            outcome="failed",
            data={"stage": "push", "code": "repository_missing", "candidate_commit": commit},
        )
    resolved = ResolvedTarget(repository=record, commit=commit, changed_paths=())

    def executor(_operation: object, _resolved: object) -> None:
        del _operation, _resolved
        candidate_git.push(
            ctx.config.control_repo,
            commit,
            remote,
            ref,
            intents=ctx.intents,
            lease_ok=ctx.lease_ok,
        )

    result = ctx.act(operation, resolved, executor, rule_id="push-candidate")
    allowed = result.status == "done"
    verdict = _verdict(
        operation,
        resolved,
        allowed=allowed,
        code="allowed" if allowed else (result.code or result.status),
    )
    data = {
        "stage": "push",
        "candidate_commit": commit,
        "candidate_ref": ref,
        "status": result.status,
    }
    if not allowed:
        data["code"] = result.code or result.status
        return Outcome(outcome="failed", data=data, action=verdict)
    if not _push_receipt_exists(ctx, commit):
        done_at = datetime.now(UTC)
        body = {
            "remote_url": remote,
            "refs": [ref],
            "tips": [commit],
            "issued_at": done_at.isoformat(),
        }
        receipt = EvidenceReceipt(
            receipt_id=uuid5(
                NAMESPACE_URL, f"omp-417:{ctx.mission_id}:push-receipt:{commit}"
            ),
            work_id=ctx.work_id,
            revision_id=UUID(str(revision_id)),
            candidate_id=UUID(str(candidate_id)),
            kind=EvidenceKind.PUSH,
            payload=body,
            payload_sha256=sha256(body),
            issuer="orchestrator",
            issued_at=done_at,
            candidate_sha256=str(candidate_sha),
            candidate_commit=commit,
            remote_ref=ref,
            remote_commit=commit,
        )
        try:
            ctx.command(
                "append_evidence",
                {"receipt": receipt.model_dump(mode="json")},
                key=f"push-receipt:{commit}",
            )
        except WorkError as err:
            if err.code != "idempotency_conflict" or not _push_receipt_exists(ctx, commit):
                raise
    return Outcome(outcome="succeeded", data=data, action=verdict)


def run_audit(ctx: StageContext) -> Outcome:
    """Run candidate verification, record auditor launches, and settle the verdict."""
    request = _request_mapping(ctx.request)
    test_command = _argv(request, "test_command")
    if not test_command:
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "test_command_missing"},
        )

    if not ctx.config.verifier_argv:
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "verifier_argv_missing"},
        )

    if not ctx.config.verifier_key_path or not ctx.config.verifier_key_path.is_file():
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "verifier_key_missing"},
        )

    view = ctx.service.read(ctx.principal, ctx.workspace_id, "workflow", _work_key(ctx))
    item = view.get("item") or {}
    candidate = item.get("candidate") or {}

    frozen = (ctx.prior.get("freeze") or {}).get("data") or {}
    if not isinstance(frozen, Mapping):
        frozen = {}
    commit = frozen.get("candidate_commit") or candidate.get("commit_sha")
    candidate_id = frozen.get("candidate_id") or candidate.get("candidate_id")
    candidate_sha = frozen.get("candidate_sha256") or candidate.get("candidate_sha256")
    revision_id = (
        frozen.get("revision_id")
        or (item.get("revision") or {}).get("revision_id")
        or item.get("current_revision_id")
    )

    if not all(
        isinstance(val, (str, UUID)) and val
        for val in (commit, candidate_id, candidate_sha, revision_id)
    ):
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "freeze_missing"},
        )

    commit = str(commit)
    candidate_id = str(candidate_id)
    candidate_sha = str(candidate_sha)
    revision_id = str(revision_id)

    eval_bucket = ctx.prior.get("evaluate")
    if not isinstance(eval_bucket, Mapping):
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "evaluate_missing"},
        )
    eval_prior = eval_bucket.get("data") or {}
    if not isinstance(eval_prior, Mapping):
        eval_prior = {}
    eval_cmd = eval_prior.get("command")
    eval_code = eval_prior.get("exit_code")
    eval_finished_at_raw = eval_prior.get("finished_at")

    if (
        not isinstance(eval_cmd, (list, tuple))
        or not eval_cmd
        or not all(isinstance(x, str) for x in eval_cmd)
    ):
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "command_missing"},
        )
    if not isinstance(eval_code, int):
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "exit_code_missing"},
        )
    if not eval_finished_at_raw:
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "finished_at_missing"},
        )

    try:
        eval_finished_at = datetime.fromisoformat(str(eval_finished_at_raw))
        if eval_finished_at.tzinfo is None:
            eval_finished_at = eval_finished_at.replace(tzinfo=UTC)
        else:
            eval_finished_at = eval_finished_at.astimezone(UTC)
    except Exception:
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "finished_at_missing"},
        )

    # 0. Candidate's attempt audited, accepted_report_count >= 1 -> succeeded, pass; no reserve/cancel/settle.
    close_attempts = view.get("close_attempts") or ()
    audited_attempt = next(
        (
            a
            for a in close_attempts
            if isinstance(a, Mapping)
            and str(a.get("candidate_id")) == candidate_id
            and a.get("state") == "audited"
            and int(a.get("accepted_report_count", 0)) >= 1
        ),
        None,
    )
    if audited_attempt is not None:
        attempt_id_str = str(audited_attempt.get("attempt_id"))
        launch_count = int(audited_attempt.get("launch_count", 1))
        if ctx.intents.open(f"verifier:{attempt_id_str}:{launch_count}"):
            ctx.intents.mark_done(f"verifier:{attempt_id_str}:{launch_count}", "settled")
        return Outcome(
            outcome="succeeded",
            verdict="pass",
            data={
                "stage": "audit",
                "candidate_commit": commit,
                "candidate_id": candidate_id,
                "candidate_sha256": candidate_sha,
                "attempt_id": attempt_id_str,
            },
        )

    # 1. Reuse its audit_ready/auditor_in_flight attempt; else begin_close_attempt, verification evidence, seal_audit_manifest.
    attempt = next(
        (
            a
            for a in close_attempts
            if isinstance(a, Mapping)
            and str(a.get("candidate_id")) == candidate_id
            and a.get("state") in ("audit_ready", "auditor_in_flight")
        ),
        None,
    )

    task_sha256: str | None = None

    if attempt is not None:
        attempt_id = UUID(str(attempt["attempt_id"]))
        manifest_row = view.get("audit_manifest")
        if manifest_row and manifest_row.get("task_sha256"):
            task_sha256 = str(manifest_row["task_sha256"])
    else:
        active_attempt = next(
            (
                a
                for a in close_attempts
                if isinstance(a, Mapping)
                and str(a.get("candidate_id")) == candidate_id
                and a.get("state") == "active"
            ),
            None,
        )
        if active_attempt is not None:
            attempt_id = UUID(str(active_attempt["attempt_id"]))
        else:
            gid = service._ids(ctx.mission_id, "grant")
            exec_view = ctx.service.read(
                ctx.principal, ctx.workspace_id, "execution", str(gid)
            )
            grant = exec_view.get("grant") or {}
            items = exec_view.get("items") or ()
            grant_item = next(
                (it for it in items if str(it.get("work_id")) == str(ctx.work_id)),
                exec_view.get("active_item") or {},
            )
            if grant.get("state") != "active":
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "execution_grant_inactive"},
                )
            grant_id = grant.get("grant_id")
            if not grant_id:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "grant_id_missing"},
                )
            judged = judge_manifest(ctx.config)
            if judged is None:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "verifier_argv_missing"},
                )
            judge_sha, _manifest_dict = judged
            if not grant.get("judge_sha256"):
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "judge_sha256_missing"},
                )
            if grant.get("judge_sha256") != judge_sha:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "judge_manifest_drift"},
                )
            for binding in (
                "original_request_sha256",
                "criteria_sha256",
                "plan_stamp_sha256",
            ):
                if not grant_item.get(binding):
                    return Outcome(
                        outcome="failed",
                        data={"stage": "audit", "code": f"{binding}_missing"},
                    )
            grant_issued_at_raw = grant.get("issued_at")
            if not grant_issued_at_raw:
                prov = grant.get("provenance")
                if isinstance(prov, str):
                    try:
                        prov = json.loads(prov)
                    except Exception:
                        prov = {}
                if isinstance(prov, Mapping):
                    grant_issued_at_raw = prov.get("issued_at")
            if not grant_issued_at_raw:
                grant_prior = (ctx.prior.get("grant") or {}).get("data") or {}
                if isinstance(grant_prior, Mapping):
                    grant_issued_at_raw = grant_prior.get("issued_at")
            if not grant_issued_at_raw:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "grant_issued_at_missing"},
                )
            if isinstance(grant_issued_at_raw, datetime):
                grant_issued_at = grant_issued_at_raw
            elif isinstance(grant_issued_at_raw, str):
                grant_issued_at = datetime.fromisoformat(grant_issued_at_raw)
            else:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "grant_issued_at_missing"},
                )
            if grant_issued_at.tzinfo is None:
                grant_issued_at = grant_issued_at.replace(tzinfo=UTC)
            else:
                grant_issued_at = grant_issued_at.astimezone(UTC)

            base_commit_value = request.get("base_commit")
            if not base_commit_value:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "base_commit_missing"},
                )
            repository_value = request.get("repository")
            if not repository_value:
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": "repository_missing"},
                )

            base_commit = str(base_commit_value)
            audited = candidate_git.audit_inputs(
                ctx.config.control_repo, base_commit, commit
            )
            diff_sha256 = str(audited["diff_sha256"])

            attempt_id = service._ids(ctx.mission_id, f"close-attempt:{candidate_id}")
            begin_payload = {
                "work_id": str(ctx.work_id),
                "attempt_id": str(attempt_id),
                "authorization_ref": f"orchestrator:{ctx.mission_id}",
                "owner_session_id": f"orchestrator:{ctx.mission_id}",
                "owner_session_started_at": grant_issued_at.isoformat(),
                "owner_session_start_commit": base_commit,
                "repository": str(repository_value),
                "diff_sha256": diff_sha256,
                "starting_dirty_paths": [],
                "riders": [],
                "authorization_kind": "execution",
                "execution_grant_id": str(grant_id),
                "candidate_tree_sha": candidate_sha,
                "original_request_sha256": str(grant_item["original_request_sha256"]),
                "criteria_sha256": str(grant_item["criteria_sha256"]),
                "plan_stamp_sha256": str(grant_item["plan_stamp_sha256"]),
                "judge_sha256": str(grant["judge_sha256"]),
            }
            begin_res = ctx.command(
                "begin_close_attempt",
                begin_payload,
                key=f"begin_close_attempt:{attempt_id}",
            )
            if begin_res.get("status") == "refused":
                reason_code = (begin_res.get("event") or {}).get("reason_code") or "begin_close_attempt_refused"
                return Outcome(
                    outcome="failed",
                    data={"stage": "audit", "code": reason_code},
                )
            active_attempt = begin_res.get("attempt") or {}

        body_text = f"{shlex.join(eval_cmd)} exited {eval_code}"
        verification_payload = {
            "body": body_text,
            "command": list(eval_cmd),
            "exit_code": eval_code,
        }
        receipt_id = service._ids(ctx.mission_id, f"verification-receipt:{commit}")
        evidence_receipt = EvidenceReceipt(
            receipt_id=receipt_id,
            work_id=ctx.work_id,
            revision_id=UUID(revision_id),
            candidate_id=UUID(candidate_id),
            kind=EvidenceKind.VERIFICATION,
            payload=verification_payload,
            payload_sha256=sha256(verification_payload),
            issuer="orchestrator",
            issued_at=eval_finished_at,
            candidate_sha256=candidate_sha,
            candidate_commit=commit,
        )
        ctx.command(
            "append_evidence",
            {"receipt": evidence_receipt.model_dump(mode="json")},
            key=f"verification-receipt:{commit}",
        )

        seal_res = ctx.command(
            "seal_audit_manifest",
            {
                "attempt_id": str(attempt_id),
                "verification_receipt_id": str(receipt_id),
            },
            key=f"seal_audit_manifest:{attempt_id}",
        )
        if seal_res.get("status") == "refused":
            reason_code = (seal_res.get("event") or {}).get("reason_code") or "seal_refused"
            return Outcome(
                outcome="failed",
                data={"stage": "audit", "code": reason_code},
            )

        attempt = seal_res.get("attempt") or active_attempt
        manifest_data = seal_res.get("manifest") or {}
        if manifest_data.get("task_sha256"):
            task_sha256 = str(manifest_data["task_sha256"])

    # 2. Dead in-flight launch: cancel, mark_done "cancelled".
    if attempt.get("state") == "auditor_in_flight" and attempt.get("in_flight_launch_id"):
        in_flight_id_str = str(attempt["in_flight_launch_id"])
        auditor_launches = view.get("auditor_launches") or ()
        launch_row = next(
            (l for l in auditor_launches if str(l.get("launch_id")) == in_flight_id_str),
            None,
        )
        launch_n = (
            int(launch_row["launch_number"])
            if launch_row and launch_row.get("launch_number") is not None
            else int(attempt.get("launch_count", 1))
        )
        cancel_res = ctx.command(
            "cancel_auditor_launch",
            {"attempt_id": str(attempt_id), "launch_id": in_flight_id_str},
            key=f"cancel_auditor_launch:{in_flight_id_str}",
        )
        ctx.intents.mark_done(f"verifier:{attempt_id}:{launch_n}", "cancelled")
        attempt = cancel_res.get("attempt") or attempt

    if not task_sha256:
        fresh_view = ctx.service.read(
            ctx.principal, ctx.workspace_id, "workflow", _work_key(ctx)
        )
        manifest_row = fresh_view.get("audit_manifest")
        if manifest_row and manifest_row.get("task_sha256"):
            task_sha256 = str(manifest_row["task_sha256"])
    if not task_sha256:
        return Outcome(
            outcome="failed",
            data={"stage": "audit", "code": "manifest_missing"},
        )

    # 3-5. Launch and evaluate verifier.
    while True:
        launch_count = int(attempt.get("launch_count", 0))
        if launch_count >= MAX_AUDITOR_LAUNCHES:
            return Outcome(
                outcome="failed",
                blocker="unrecoverable-blocker",
                data={
                    "stage": "audit",
                    "code": "max_auditor_launches_exceeded",
                    "launch_count": launch_count,
                },
            )
        n = launch_count + 1
        intent_key = f"verifier:{attempt_id}:{n}"
        ctx.intents.record_intent(
            intent_key,
            "verifier",
            {"attempt_id": str(attempt_id), "launch_number": n},
        )
        tool_call_id = f"call:{attempt_id}:{n}"
        reserve_res = ctx.command(
            "reserve_auditor_launch",
            {
                "attempt_id": str(attempt_id),
                "task_sha256": task_sha256,
                "tool_call_id": tool_call_id,
            },
            key=f"reserve_auditor_launch:{attempt_id}:{n}",
        )
        if reserve_res.get("status") == "refused":
            code = (reserve_res.get("event") or {}).get("reason_code") or "reserve_refused"
            return Outcome(
                outcome="failed",
                blocker="unrecoverable-blocker" if "exhausted" in code else None,
                data={"stage": "audit", "code": code},
            )
        launch = reserve_res.get("launch") or {}
        launch_id = str(launch.get("launch_id"))
        attempt = reserve_res.get("attempt") or attempt

        worktree_name = f"{ctx.mission_id}-{ctx.step_index}-verify"
        worktree = candidate_git.reset_worktree(
            ctx.config.control_repo,
            ctx.config.worktrees_dir,
            worktree_name,
            commit,
        )

        omp_dir = worktree / ".omp"
        omp_dir.mkdir(parents=True, exist_ok=True)
        (omp_dir / "verdict.json").unlink(missing_ok=True)
        (omp_dir / "audit-report.txt").unlink(missing_ok=True)

        intake_data = request.get("intake") or {}
        raw_criteria = (
            intake_data.get("acceptance_criteria")
            if isinstance(intake_data, Mapping)
            else getattr(intake_data, "acceptance_criteria", ())
        )
        criteria_json = []
        for c in raw_criteria or ():
            if hasattr(c, "model_dump"):
                criteria_json.append(c.model_dump(mode="json"))
            elif isinstance(c, Mapping):
                criteria_json.append(dict(c))
            else:
                criteria_json.append({"statement": str(c)})

        task_json = {
            "candidate_sha256": candidate_sha,
            "attempt_id": str(attempt_id),
            "verifier_id": "verifier",
            "key_path": str(ctx.config.verifier_key_path),
            "test_command": list(test_command),
            "criteria": criteria_json,
        }
        (omp_dir / "verify-task.json").write_text(
            json.dumps(task_json), encoding="utf-8"
        )

        try:
            code = _sandbox(
                ctx,
                ctx.config.verifier_argv,
                worktree,
                identity="verifier",
                ro_binds=[ctx.config.verifier_key_path],
            )
        except worker_sandbox.WorkerSandboxRefused as err:
            cancel_res = ctx.command(
                "cancel_auditor_launch",
                {"attempt_id": str(attempt_id), "launch_id": str(launch_id)},
                key=f"cancel_auditor_launch:{launch_id}",
            )
            ctx.intents.mark_done(intent_key, "crashed")
            attempt = cancel_res.get("attempt") or attempt
            if int(attempt.get("launch_count", 0)) < MAX_AUDITOR_LAUNCHES:
                continue
            return Outcome(
                outcome="failed",
                blocker="unrecoverable-blocker",
                data={"stage": "audit", "code": err.code},
            )

        verdict_path = omp_dir / "verdict.json"
        verdict_data: Mapping[str, object] | None = None
        if verdict_path.is_file():
            try:
                loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    verdict_data = loaded
            except Exception:
                verdict_data = None

        # 4. Exit!=0, no verdict: cancel, mark_done "crashed"; relaunch while launch_count < MAX_AUDITOR_LAUNCHES, then blocker.
        if (code is None or code != 0) and verdict_data is None:
            cancel_res = ctx.command(
                "cancel_auditor_launch",
                {"attempt_id": str(attempt_id), "launch_id": str(launch_id)},
                key=f"cancel_auditor_launch:{launch_id}",
            )
            ctx.intents.mark_done(intent_key, "crashed")
            attempt = cancel_res.get("attempt") or attempt
            if int(attempt.get("launch_count", 0)) < MAX_AUDITOR_LAUNCHES:
                continue
            return Outcome(
                outcome="failed",
                blocker="unrecoverable-blocker",
                data={
                    "stage": "audit",
                    "code": "max_auditor_launches_exceeded",
                    "launch_count": int(attempt.get("launch_count", 0)),
                },
            )

        # 5. accept_candidate: False -> cancel; True -> settle_auditor_launch; mark_done.
        verifier_key = ctx.config.verifier_key_path.read_bytes()
        accepted = verifier.accept_candidate(
            verifier_key,
            verdict_data,
            candidate_sha256=candidate_sha,
            attempt_id=str(attempt_id),
            producer_id="worker",
        )

        if not accepted:
            cancel_res = ctx.command(
                "cancel_auditor_launch",
                {"attempt_id": str(attempt_id), "launch_id": str(launch_id)},
                key=f"cancel_auditor_launch:{launch_id}",
            )
            ctx.intents.mark_done(intent_key, "rejected")
            return Outcome(
                outcome="failed",
                verdict="fail",
                data={
                    "stage": "audit",
                    "code": "candidate_rejected",
                    "candidate_commit": commit,
                    "candidate_sha256": candidate_sha,
                    "attempt_id": str(attempt_id),
                },
            )

        report_path = omp_dir / "audit-report.txt"
        transport_payload = (
            report_path.read_text(encoding="utf-8") if report_path.is_file() else None
        )
        settle_res = ctx.command(
            "settle_auditor_launch",
            {
                "attempt_id": str(attempt_id),
                "launch_id": str(launch_id),
                "transport_payload": transport_payload,
                "transport_failed": False,
            },
            key=f"settle_auditor_launch:{launch_id}",
        )
        ctx.intents.mark_done(intent_key, "settled")

        # 6. Accepted PASS -> succeeded, pass; else failed, fail.
        settled_verdict = settle_res.get("verdict")
        if not settled_verdict and isinstance(settle_res.get("receipt"), Mapping):
            settled_verdict = settle_res["receipt"].get("verdict")

        is_applied = settle_res.get("status") == "applied" or bool(settle_res.get("receipt"))
        if is_applied and isinstance(settled_verdict, str) and settled_verdict.upper() == "PASS":
            return Outcome(
                outcome="succeeded",
                verdict="pass",
                data={
                    "stage": "audit",
                    "candidate_commit": commit,
                    "candidate_id": candidate_id,
                    "candidate_sha256": candidate_sha,
                    "attempt_id": str(attempt_id),
                },
            )
        reason_code = (settle_res.get("event") or {}).get("reason_code")
        if not reason_code and isinstance(settled_verdict, str):
            reason_code = f"verdict_{settled_verdict.lower()}"
        return Outcome(
            outcome="failed",
            verdict="fail",
            data={
                "stage": "audit",
                "code": reason_code or "settle_failed",
                "candidate_commit": commit,
                "candidate_sha256": candidate_sha,
                "attempt_id": str(attempt_id),
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

