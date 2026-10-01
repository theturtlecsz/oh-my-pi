"""Mission orchestrator service (OMP-417).

Loads ``orchestrator.json`` and a mission request, submits the work item and
the first step job, and runs each stage through a registered operation. The
stage decision in ``stages`` chooses the next step. This module applies that
decision: it records the step, then enqueues, pauses, abandons, or revises.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess  # nosec B404 - pytest argv is a list; the executable is this interpreter
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.control_actions import HoldDecision, perform
from omp_work.control_plane.gate import LeaseClaim
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.budget import check_item_budget
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import Frozen, Settlement
from omp_work.operations.capabilities import DEFAULT_BASE_URL
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import _jobs_migration_state, check_migrations
from omp_work.orchestrator import qualification
from omp_work.orchestrator.stages import (
    STAGE_ORDER,
    UNRECORDED_KINDS,
    ActionVerdict,
    Facts,
    Step,
    bounds_for,
    decide,
    decision_for,
)
from omp_work.orchestrator.step_log import append_step, list_steps, open_intent
from omp_work.project_store import ProjectAuthorityRefused, ProjectNotFound
from omp_work.routing.policy import RoutingPolicy, load_policy
from omp_work.v1.agent_stop import read_stop_state
from omp_work.v1.decision_records import find_decision
from omp_work.v1.missions import read_mission
from omp_work.v1.models import (
    BoundedIntakeDraft,
    CommandEnvelope,
    CreateWorkInput,
    MissionIntakeScope,
    OwnerInstruction,
)
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError

__all__ = [
    "ActResult",
    "MissionRequest",
    "OrchestratorConfig",
    "OrchestratorError",
    "OrchestratorHandler",
    "Outcome",
    "StageContext",
    "dry_run",
    "factory",
    "qualify",
    "register_stage_operation",
    "request_terminal",
    "status",
    "submit",
]

_API = "work.omp.dev/v1"
_CAPABILITY = "omp.orchestrator"
_RESOURCES = {"cpu": 1, "memory_mib": 256, "gpu": 0, "model_calls": 0}
_POST_KINDS = frozenset(
    {
        "advance",
        "retry",
        "repair",
        "reroute",
        "reschedule",
        "pause",
        "end",
        "abandon",
        "redefine",
    }
)
_TERMINAL_RULES = frozenset({"terminal-needs-basis", "terminal-with-basis"})
_Run = Callable[["StageContext"], "Outcome"]
_REGISTRY: dict[str, _Run] = {}


class OrchestratorError(Exception):
    """A refused orchestrator command, addressed by a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OrchestratorConfig:
    """``orchestrator.json`` next to the operations config, unless overridden."""

    workspace_id: UUID
    automation_capability_path: Path
    qualification_path: Path
    lock_map_path: Path
    verifier_key_path: Path
    allowed_signers: Path
    control_repo: Path
    worktrees_dir: Path
    live_checkout: Path
    repair_rounds: int = 3
    max_workers: int = 1
    lease_seconds: int = 60
    wait_seconds: int = 30
    workservice_url: str = DEFAULT_BASE_URL
    verifier_argv: tuple[str, ...] = ()
    merge_credential_path: Path | None = None
    operations: tuple[str, ...] = ()
    ops: OperationsConfig = field(default_factory=OperationsConfig.defaults)

    @staticmethod
    def path(ops: OperationsConfig | None = None) -> Path:
        base = ops or OperationsConfig.defaults()
        return base.config_dir / "orchestrator.json"


@dataclass(frozen=True)
class MissionRequest:
    """The request stored on step 0 so a restart needs no CLI session."""

    mission_id: UUID
    project_id: UUID
    unattended: bool
    intake: BoundedIntakeDraft
    scope: MissionIntakeScope
    instruction: OwnerInstruction
    work: CreateWorkInput
    repository: str = ""
    remote: str = ""
    candidate_ref: str = ""
    target_ref: str = ""
    base_commit: str = ""
    allowed_paths: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    worker_argv: tuple[str, ...] = ()
    controls: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        body = {
            "mission_id": str(self.mission_id),
            "project_id": str(self.project_id),
            "unattended": self.unattended,
            "intake": self.intake.model_dump(mode="json"),
            "scope": self.scope.model_dump(mode="json"),
            "instruction": self.instruction.model_dump(mode="json"),
            "work": self.work.model_dump(mode="json"),
            "repository": self.repository,
            "remote": self.remote,
            "candidate_ref": self.candidate_ref,
            "target_ref": self.target_ref,
            "base_commit": self.base_commit,
            "allowed_paths": list(self.allowed_paths),
            "test_command": list(self.test_command),
            "worker_argv": list(self.worker_argv),
            "controls": self.controls,
        }
        return body


@dataclass
class Outcome:
    """What one stage operation reports back to the decision."""

    outcome: str = "succeeded"
    data: dict[str, Any] = field(default_factory=dict)
    action: ActionVerdict | None = None
    new_scope: bool = False
    budget_exhausted: bool = False
    blocker: str | None = None
    verdict: str = "none"


@dataclass(frozen=True)
class ActResult:
    """What ``StageContext.act`` did with one control-plane action."""

    status: str
    operation_id: str
    code: str | None = None
    decision_id: str | None = None


def register_stage_operation(stage: str, run: _Run) -> None:
    """Register the operation that runs when a step is at ``stage``."""
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown stage: {stage!r}")
    _REGISTRY[stage] = run


def _load_operations(specs: tuple[str, ...]) -> None:
    for spec in specs:
        module_name, _, callable_name = spec.partition(":")
        if not module_name or not callable_name:
            raise OrchestratorError("invalid_request")
        module = importlib.import_module(module_name)
        getattr(module, callable_name)()


def load_config(path: Path | None = None, ops: OperationsConfig | None = None) -> OrchestratorConfig:
    """Read orchestrator config. ``path`` defaults to ``<config_dir>/orchestrator.json``."""
    base = ops or OperationsConfig.defaults()
    source = Path(path) if path is not None else OrchestratorConfig.path(base)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OrchestratorError("invalid_request")
    operations = data.get("operations") or []
    if not isinstance(operations, list):
        raise OrchestratorError("invalid_request")

    def _path(key: str) -> Path:
        return Path(data[key])

    return OrchestratorConfig(
        workspace_id=UUID(str(data["workspace_id"])),
        automation_capability_path=_path("automation_capability_path"),
        qualification_path=_path("qualification_path"),
        lock_map_path=_path("lock_map_path"),
        verifier_key_path=_path("verifier_key_path"),
        allowed_signers=_path("allowed_signers"),
        control_repo=_path("control_repo"),
        worktrees_dir=_path("worktrees_dir"),
        live_checkout=_path("live_checkout"),
        repair_rounds=int(data.get("repair_rounds", 3)),
        max_workers=int(data.get("max_workers", 1)),
        lease_seconds=int(data.get("lease_seconds", 60)),
        wait_seconds=int(data.get("wait_seconds", 30)),
        workservice_url=str(data.get("workservice_url") or DEFAULT_BASE_URL),
        verifier_argv=tuple(str(item) for item in (data.get("verifier_argv") or ())),
        merge_credential_path=(
            None if not data.get("merge_credential_path") else Path(data["merge_credential_path"])
        ),
        operations=tuple(str(item) for item in operations),
        ops=base,
    )


def load_request(path: Path) -> MissionRequest:
    """Read a ``submit --request`` document."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OrchestratorError("invalid_request")
    controls = data.get("controls") or {}
    if not isinstance(controls, dict):
        raise OrchestratorError("invalid_request")
    return MissionRequest(
        mission_id=UUID(str(data["mission_id"])),
        project_id=UUID(str(data["project_id"])),
        unattended=bool(data.get("unattended", False)),
        intake=BoundedIntakeDraft.model_validate(data["intake"]),
        scope=MissionIntakeScope.model_validate(data["scope"]),
        instruction=OwnerInstruction.model_validate(data["instruction"]),
        work=CreateWorkInput.model_validate(data["work"]),
        repository=str(data.get("repository") or ""),
        remote=str(data.get("remote") or ""),
        candidate_ref=str(data.get("candidate_ref") or ""),
        target_ref=str(data.get("target_ref") or ""),
        base_commit=str(data.get("base_commit") or ""),
        allowed_paths=tuple(data.get("allowed_paths") or ()),
        test_command=tuple(data.get("test_command") or ()),
        worker_argv=tuple(data.get("worker_argv") or ()),
        controls=controls,
    )


def _principal(path: Path) -> Principal:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Principal(
        actor_id=UUID(str(data["actor_id"])),
        actor_kind=str(data["actor_kind"]),
        workspaces=frozenset(UUID(str(item)) for item in data["workspaces"]),
        scopes=frozenset(str(item) for item in data["scopes"]),
    )


def _owner_principal(config: OrchestratorConfig) -> Principal:
    return _principal(config.automation_capability_path.parent / "owner.json")


def qualification_record(path: Path) -> tuple[dict[str, dict[str, str]] | None, str]:
    """The check record ``qualification.qualified`` reads, and its release.

    ``qualify`` writes ``{release, results: {key: {item, test, result}}}``. A
    file that is already ``{key: {result, release}}`` is accepted too.
    """
    if not path.is_file():
        return None, ""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None, ""
    release = str(data.get("release") or "")
    results = data.get("results")
    if isinstance(results, dict):
        record: dict[str, dict[str, str]] = {}
        for key, entry in results.items():
            if isinstance(entry, dict) and "result" in entry:
                record[str(key)] = {
                    "result": str(entry["result"]),
                    "release": str(entry.get("release") or release),
                }
        return record, release
    record = {}
    for key, entry in data.items():
        if isinstance(entry, dict) and "result" in entry:
            record[str(key)] = {
                "result": str(entry["result"]),
                "release": str(entry.get("release") or release),
            }
            if not release and entry.get("release"):
                release = str(entry["release"])
    return (record or None), release


def _require_jobs(config: OperationsConfig) -> None:
    """The migration check ``JobWorker.start`` makes. Pending or drifted jobs refuse."""
    try:
        with psycopg.connect(**config.connection_kwargs("omp_work_migrator")) as conn:
            with conn.cursor() as cur:
                pending, drift = _jobs_migration_state(cur)
            if pending or drift:
                raise OrchestratorError("jobs_not_migrated")
            check_migrations(conn, allow_pending=True)
    except OrchestratorError:
        raise
    except (psycopg.Error, ValueError, RuntimeError) as exc:
        raise OrchestratorError("jobs_not_migrated") from exc


def _envelope(
    workspace_id: UUID,
    principal: Principal,
    command_type: str,
    payload: Mapping[str, Any],
    operation_id: UUID,
    request_id: UUID,
    correlation_id: UUID,
) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": _API,
            "workspace_id": str(workspace_id),
            "operation_id": str(operation_id),
            "request_id": str(request_id),
            "correlation_id": str(correlation_id),
            "command": {"type": command_type, "payload": dict(payload)},
        }
    )


def _service(config: OrchestratorConfig) -> WorkService:
    return WorkService(PostgresWorkStore(config.ops))


def _execute(
    config: OrchestratorConfig,
    principal: Principal,
    command_type: str,
    payload: Mapping[str, Any],
    operation_id: UUID,
    *,
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> dict[str, Any]:
    envelope = _envelope(
        config.workspace_id,
        principal,
        command_type,
        payload,
        operation_id,
        request_id or operation_id,
        correlation_id or operation_id,
    )
    _receipt, result = _service(config).execute(principal, envelope)
    if isinstance(result, dict):
        return result
    dumped = getattr(result, "model_dump", None)
    if dumped is not None:
        return dumped(mode="json")
    raise OrchestratorError("invalid_request")


def _ignored(error: WorkError) -> bool:
    if error.code in {"revision_conflict", "mission_transition_refused", "idempotency_conflict"}:
        return True
    return any(
        item in {"decision_exists", "decision_already_answered"} for item in error.diagnostics
    )


def _run_command(
    config: OrchestratorConfig,
    principal: Principal,
    command_type: str,
    payload: Mapping[str, Any],
    operation_id: UUID,
) -> dict[str, Any]:
    try:
        return _execute(config, principal, command_type, payload, operation_id)
    except WorkError as error:
        if _ignored(error):
            return {"replayed": True, "code": error.code}
        raise OrchestratorError(error.code) from error


class StepIntentLog:
    """``IntentLog`` backed by the mission step chain."""

    def __init__(self, handler: OrchestratorHandler, step_index: int) -> None:
        self._handler = handler
        self._step_index = step_index

    def record_intent(self, key: str, action: str, target: Any) -> None:
        self._handler.record(
            self._step_index,
            {
                "kind": "external_intent",
                "rule_id": "external-intent",
                "idempotency_key": key,
                "external_ref": key,
                "operation_id": key,
                "action": action,
                "target": target if isinstance(target, (dict, str, int, float, bool)) or target is None else str(target),
            },
        )

    def mark_done(self, key: str, ref: str) -> None:
        self._handler.record(
            self._step_index,
            {
                "kind": "external_done",
                "rule_id": "external-done",
                "idempotency_key": key,
                "external_ref": key,
                "operation_id": key,
                "ref": ref,
            },
        )

    def open(self, key: str) -> dict[str, Any] | None:
        with self._handler.store.transaction(
            self._handler.config.workspace_id, self._handler.principal.actor_id
        ) as cur:
            found = open_intent(cur, self._handler.config.workspace_id, str(self._handler.mission_id), key)
        return None if found is None else dict(found)


class StageContext:
    """One running stage step. ``command`` and ``act`` both stop before they write."""

    def __init__(
        self,
        handler: OrchestratorHandler,
        *,
        step_index: int,
        stage: str,
        attempt: int,
        repair_round: int,
        request: Mapping[str, Any],
        prior: dict[str, Any],
        lease: LeaseClaim,
        work_id: UUID,
    ) -> None:
        self._handler = handler
        self.mission_id = handler.mission_id
        self.workspace_id = handler.config.workspace_id
        self.project_id = handler.project_id
        self.work_id = work_id
        self.step_index = step_index
        self.stage = stage
        self.attempt = attempt
        self.repair_round = repair_round
        self.request = request
        self.prior = prior
        self.lease = lease
        self.intents = StepIntentLog(handler, step_index)
        self.service = _service(handler.config)
        self.principal = handler.principal
        self.projects = handler.work_store
        self.config = handler.config
        self._ordinals: dict[str, int] = {}

    def lease_ok(self) -> bool:
        with self._handler.store.transaction(self.workspace_id, self.principal.actor_id) as cur:
            cur.execute(
                """
                SELECT worker_id, fence, status,
                       lease_expires_at > clock_timestamp() AS live
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND job_id=%s AND source='native'
                """,
                (self.workspace_id, str(self.lease.job_id)),
            )
            row = cur.fetchone()
        if row is None or row["status"] not in ("admitted", "in_flight"):
            return False
        if str(row["worker_id"]) != self.lease.worker_id or int(row["fence"]) != self.lease.fence:
            return False
        return bool(row["live"])

    def _stop(self) -> None:
        with self._handler.store.transaction(self.workspace_id, self.principal.actor_id) as cur:
            stopped = bool(read_stop_state(cur, self.workspace_id)["stopped"])
        if stopped:
            raise Frozen

    def lease_expires_at(self) -> datetime | None:
        """This worker's lease expiry, or None when the row is dead or reassigned."""
        with self._handler.store.transaction(self.workspace_id, self.principal.actor_id) as cur:
            cur.execute(
                """
                SELECT worker_id, fence, status, lease_expires_at
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND job_id=%s AND source='native'
                """,
                (self.workspace_id, str(self.lease.job_id)),
            )
            row = cur.fetchone()
        if row is None or row["status"] not in ("admitted", "in_flight"):
            return None
        if str(row["worker_id"]) != self.lease.worker_id or int(row["fence"]) != self.lease.fence:
            return None
        return row["lease_expires_at"]

    def command(  # noqa: A002  # pylint: disable=redefined-builtin - command type is the public name
        self, type: str, payload: Mapping[str, Any], *, key: str | None = None
    ) -> dict[str, Any]:
        """Run one work command. Envelope ids are stable across a re-run of this step.

        ``key`` replaces the per-type ordinal in the envelope ids, so two
        different command payloads under the same key are an
        ``idempotency_conflict`` instead of an independent write.
        """
        self._stop()
        if key is None:
            ordinal = self._ordinals.get(type, 0)
            self._ordinals[type] = ordinal + 1
            base = f"omp-417:{self.mission_id}:{self.step_index}:{type}:{ordinal}"
        else:
            base = f"omp-417:{self.mission_id}:{self.step_index}:{type}:{key}"
        try:
            return _execute(
                self.config,
                self.principal,
                type,
                payload,
                uuid5(NAMESPACE_URL, base + ":operation"),
                request_id=uuid5(NAMESPACE_URL, base + ":request"),
                correlation_id=uuid5(NAMESPACE_URL, base + ":correlation"),
            )
        except WorkError as error:
            if _ignored(error):
                return {"replayed": True, "code": error.code}
            raise

    def hold_for(
        self,
        rule_id: str,
        step_index: int,
        stage: str,
        commit: str | None,
        target_ref: str | None,
    ) -> HoldDecision | None:
        """The tier-3 hold whose decision id is the pause step's id.

        The facts mirror the ones the stage decision reads for this step, so
        ``decision_for`` recomputes the same ``decision_id`` the pause records
        and the hold writes one decision, not two.
        """
        if self.mission_id is None or self.project_id is None:
            return None
        try:
            payload = decision_for(
                Facts(
                    mission_id=str(self.mission_id),
                    project_id=self.project_id,
                    step_index=step_index,
                    stage=stage,
                    mission_status="running",
                    outcome="none",
                    qualified=True,
                    candidate_commit=commit,
                    target_ref=target_ref,
                ),
                rule_id,
            )
        except ValueError:
            return None
        return HoldDecision(
            decision_id=payload.decision_id,
            question=payload.question,
            why_it_matters=payload.why_it_matters,
            risk_of_delay=payload.risk_of_delay,
            evidence_refs=payload.evidence_refs,
            resume_state=payload.resume_state or stage,
        )

    def answered_decision(self, rule_id: str) -> str | None:
        """This mission's answered decision recorded for ``rule_id``, newest first."""
        return self._handler.answered_decision(rule_id)

    def _target_ref(self) -> str | None:
        target = self.request.get("target_ref") if isinstance(self.request, dict) else None
        return str(target) if target else None

    def act(
        self,
        operation: Mapping[str, Any],
        resolved: ResolvedTarget,
        executor: Callable[[Any, ResolvedTarget], object],
        *,
        rule_id: str,
        decision_id: str | None = None,
    ) -> ActResult:
        """Run one control-plane action, or only its executor when the intent is still open.

        The intent is recorded only once ``perform`` has authorized the action,
        so an unreachable intent never precedes a refusal. An operation that
        presents a signed decision records that presentation first; a replay
        finds the presentation and the used authorization instead of
        re-presenting, and runs only the executor.
        """
        self._stop()
        parsed = parse_submission(operation)
        operation_id = str(
            uuid5(
                NAMESPACE_URL,
                f"omp-417:{self.mission_id}:{self.step_index}:{rule_id}:{parsed.kind}:{parsed.resource_id or ''}",
            )
        )
        if not self.lease_ok():
            self._record_action(rule_id, operation_id, "action_refused", "lease_lost")
            return ActResult(status="refused", operation_id=operation_id, code="lease_lost")
        if self._finished(operation_id):
            return ActResult(status="done", operation_id=operation_id)
        if self._allowed(operation_id):
            executor(parsed, resolved)
            self.intents.mark_done(operation_id, operation_id)
            return ActResult(status="done", operation_id=operation_id)
        digest = classify(parsed, resolved).target_sha256
        if decision_id is None:
            decision_id = self.answered_decision(rule_id)
        if decision_id is not None and self._presented(operation_id, decision_id, digest):
            self._record_action(rule_id, operation_id, "action_allowed", None, decision_id=decision_id)
            self.intents.record_intent(operation_id, parsed.kind, {"resource_id": parsed.resource_id})
            executor(parsed, resolved)
            self.intents.mark_done(operation_id, operation_id)
            return ActResult(status="done", operation_id=operation_id, decision_id=decision_id)
        if decision_id is not None:
            self._record_authorization(rule_id, operation_id, decision_id, digest)

        def wrapped(op: Any, target: ResolvedTarget) -> None:
            self._record_action(rule_id, operation_id, "action_allowed", None, decision_id=decision_id)
            self.intents.record_intent(operation_id, parsed.kind, {"resource_id": parsed.resource_id})
            executor(op, target)
            self.intents.mark_done(operation_id, operation_id)

        hold = self.hold_for(rule_id, self.step_index, self.stage, resolved.commit, self._target_ref())
        if decision_id is None and hold is not None and self._handler.find_decision(str(hold.decision_id)) is not None:
            self._record_action(rule_id, operation_id, "action_refused", "held")
            return ActResult(status="held", operation_id=operation_id, decision_id=str(hold.decision_id))
        try:
            outcome = perform(
                self.projects,
                self.workspace_id,
                self.principal.actor_id,
                self.project_id,
                self.mission_id,
                dict(operation),
                lambda _operation: resolved,
                wrapped,
                datetime.now(timezone.utc),
                None if decision_id is None else UUID(decision_id),
                hold=hold,
            )
        except (ProjectAuthorityRefused, ProjectNotFound) as error:
            code = getattr(error, "code", "refused")
            self._record_action(rule_id, operation_id, "action_refused", code, decision_id=decision_id)
            return ActResult(status="refused", operation_id=operation_id, code=str(code), decision_id=decision_id)
        if outcome.status == "done":
            return ActResult(
                status="done",
                operation_id=operation_id,
                decision_id=None if outcome.decision_id is None else str(outcome.decision_id),
            )
        self._record_action(rule_id, operation_id, "action_refused", outcome.code, decision_id=decision_id)
        return ActResult(
            status=outcome.status,
            operation_id=operation_id,
            code=outcome.code,
            decision_id=None if outcome.decision_id is None else str(outcome.decision_id),
        )

    def _presented(self, operation_id: str, decision_id: str, digest: str) -> bool:
        """Whether this operation presented this decision and the authorization is used."""
        for step in reversed(self._handler.steps()):
            if (
                step.get("kind") == "authorization_presented"
                and step.get("operation_id") == operation_id
                and str(step.get("decision_id")) == str(decision_id)
                and str(step.get("digest")) == digest
            ):
                break
        else:
            return False
        with self.projects._transaction(self.workspace_id, self.principal.actor_id) as cur:
            cur.execute(
                "SELECT 1 FROM omp_work.project_action_records"
                " WHERE workspace_id=%s AND decision_id=%s AND outcome='allowed' LIMIT 1",
                (self.workspace_id, UUID(decision_id)),
            )
            return cur.fetchone() is not None

    def _allowed(self, operation_id: str) -> bool:
        """Whether this operation already has a recorded ``action_allowed``."""
        return any(
            step.get("kind") == "action_allowed" and step.get("operation_id") == operation_id
            for step in self._handler.steps()
        )

    def _finished(self, operation_id: str) -> bool:
        """Whether this operation already reached ``external_done``."""
        return any(
            step.get("kind") == "external_done" and step.get("operation_id") == operation_id
            for step in self._handler.steps()
        )

    def _record_authorization(self, rule_id: str, operation_id: str, decision_id: str, digest: str) -> None:
        self._handler.record(
            self.step_index,
            {
                "kind": "authorization_presented",
                "rule_id": rule_id,
                "idempotency_key": operation_id,
                "operation_id": operation_id,
                "decision_id": decision_id,
                "digest": digest,
            },
        )

    def _record_action(
        self,
        rule_id: str,
        operation_id: str,
        kind: str,
        code: str | None,
        *,
        decision_id: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "kind": kind,
            "rule_id": rule_id,
            "idempotency_key": operation_id,
            "operation_id": operation_id,
        }
        if code is not None:
            body["code"] = code
        if decision_id is not None:
            body["decision_id"] = decision_id
        self._handler.record(self.step_index, body)


class OrchestratorHandler:
    """Job handler for ``omp.orchestrator``. ``observe`` settles a recorded step or re-runs it."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.store = NativeJobStore(config.ops)
        self.work_store = PostgresWorkStore(config.ops)
        self.principal = _principal(config.automation_capability_path)
        self.mission_id: UUID | None = None
        self.project_id: UUID | None = None
        self._request: dict[str, Any] | None = None
        self._job_id: str | None = None
        _load_operations(config.operations)

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        del ctx
        return self._dispatch(job, recovering=False)

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        del ctx
        parsed = _parse_job(str(job["job_id"]))
        if parsed is None:
            return None
        self._job_id = str(job["job_id"])
        self._bind(parsed[0])
        if parsed[1] == "wait":
            return self._wait(job, str(parsed[2]), int(parsed[3]))
        step_index = int(parsed[2])
        steps = self.steps()
        posted = _posted(steps, step_index)
        if posted is not None:
            self._replay_post(job, posted)
            return Settlement(outcome="succeeded", receipts=[])
        outcome = _outcome_step(steps, step_index)
        if outcome is not None:
            self._finish_recorded(job, step_index, outcome)
            return Settlement(outcome="succeeded", receipts=[])
        self.record(
            step_index,
            {
                "kind": "recovery",
                "rule_id": "lease-recovery",
                "idempotency_key": f"recovery:{self.mission_id}:{step_index}",
            },
        )
        return self._dispatch(job, recovering=True)

    def steps(self) -> list[dict[str, Any]]:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            return list_steps(cur, self.config.workspace_id, str(self.mission_id))

    def record(self, step_index: int, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.mission_id is None or self._job_id is None:
            raise AssertionError("mission_id or job_id is None")
        step = {
            "mission_id": str(self.mission_id),
            "step_index": step_index,
            "kind": body["kind"],
            "rule_id": body["rule_id"],
            "idempotency_key": body.get("idempotency_key")
            or f"{self.mission_id}:{step_index}:{body['kind']}",
        }
        for key, value in body.items():
            if key not in step:
                step[key] = value
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            stored = append_step(
                self.store,
                cur,
                workspace_id=self.config.workspace_id,
                job_id=self._job_id,
                step=step,
            )
        return stored

    def answered_decision(self, rule_id: str) -> str | None:
        for step in reversed(self.steps()):
            if step.get("rule_id") != rule_id or not step.get("decision_id"):
                continue
            decision_id = str(step["decision_id"])
            record = self.find_decision(decision_id)
            if record is not None and record.get("status") == "answered":
                return decision_id
        return None

    def find_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            return find_decision(cur, self.config.workspace_id, decision_id)

    def _bind(self, mission_id: UUID) -> None:
        self.mission_id = mission_id
        submitted = next((step for step in self.steps() if step.get("kind") == "submitted"), None)
        if submitted is None:
            raise OrchestratorError("invalid_request")
        request = submitted.get("request")
        if not isinstance(request, dict):
            raise OrchestratorError("invalid_request")
        self._request = request
        self.project_id = UUID(str(request["project_id"]))

    def _dispatch(self, job: dict[str, Any], *, recovering: bool) -> Settlement:
        parsed = _parse_job(str(job["job_id"]))
        if parsed is None:
            raise OrchestratorError("invalid_request")
        self._bind(parsed[0])
        self._job_id = str(job["job_id"])
        if parsed[1] == "wait":
            return self._wait(job, str(parsed[2]), int(parsed[3]))
        step_index = int(parsed[2])
        if recovering:
            posted = _posted(self.steps(), step_index)
            if posted is not None:
                return Settlement(outcome="succeeded", receipts=[])
        self._run_stage(job, step_index)
        return Settlement(outcome="succeeded", receipts=[])

    def _run_stage(self, job: dict[str, Any], step_index: int) -> None:
        if self._request is None or self.mission_id is None or self.project_id is None:
            raise AssertionError("stage context is unbound")
        steps = self.steps()
        recorded = _outcome_step(steps, step_index)
        stage = _stage_for(steps, step_index)
        facts = self._facts(steps, step_index, stage, _outcome_from(recorded))
        if recorded is None and facts.stop_engaged:
            raise Frozen
        chosen = decide(facts, self._bounds(stage))
        # ``none`` means this step has not run. Confirm treats that as unanswered,
        # so dispatch the stage and decide again from the outcome it records.
        if recorded is None and chosen.rule_id == "d23-owner-confirm":
            chosen = Step(
                kind="dispatch",
                stage=stage,
                next_stage=stage,
                rule_id="stage-dispatch",
                bound=None,
                decision=None,
            )
        produced: Outcome | None = None
        if recorded is None and chosen.kind == "dispatch":
            if chosen.kind not in UNRECORDED_KINDS:
                self._record_decision(step_index, chosen, None)
            outcome = self._operate(job, step_index, stage, facts)
            self.record(step_index, _outcome_body(self.mission_id, step_index, stage, outcome))
            produced = outcome
            facts = self._facts(self.steps(), step_index, stage, outcome)
            chosen = decide(facts, self._bounds(stage))
        elif recorded is not None:
            produced = _outcome_from(recorded)
            facts = self._facts(steps, step_index, stage, produced)
            chosen = decide(facts, self._bounds(stage))
        if chosen.kind == "frozen":
            raise Frozen
        if chosen.kind in UNRECORDED_KINDS:
            return
        # The confirm stage's pause is the intake decision's own pause: the stage
        # operation already filed the decision, so the step records that id and a
        # wait job, and neither writes a decision nor moves the status.
        if chosen.kind == "pause" and chosen.rule_id == "d23-owner-confirm":
            supplied = _outcome_decision_id(produced)
            if supplied is not None:
                self._record_confirm_pause(step_index, supplied, stage)
                self._enqueue(
                    _wait_job(self.mission_id, UUID(supplied), 0),
                    UUID(str(job["work_id"])),
                )
                return
        outcome = produced
        self._record_decision(step_index, chosen, outcome)
        self._apply(job, step_index, chosen, outcome)

    def _record_confirm_pause(self, step_index: int, decision_id: str, stage: str) -> None:
        """Record the confirm pause that adopted the intake stage's own decision."""
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        self.record(
            step_index,
            {
                "kind": "pause",
                "rule_id": "d23-owner-confirm",
                "idempotency_key": f"pause:{self.mission_id}:{step_index}:d23-owner-confirm",
                "stage": stage,
                "next_stage": stage,
                "bound": None,
                "decision_id": decision_id,
                "wait_job": _wait_job(self.mission_id, UUID(decision_id), 0),
            },
        )

    def _operate(self, job: dict[str, Any], step_index: int, stage: str, facts: Facts) -> Outcome:
        if self._request is None or self.project_id is None:
            raise AssertionError("stage context is unbound")
        run = _REGISTRY.get(stage)
        if run is None:
            verdict = "pass" if stage == "audit" else "none"
            return Outcome(outcome="succeeded", data={"stage": stage}, verdict=verdict)
        work_id = UUID(str(job["work_id"]))
        ctx = StageContext(
            self,
            step_index=step_index,
            stage=stage,
            attempt=facts.attempts,
            repair_round=facts.repair_rounds,
            request=self._request,
            prior=_prior(self.steps()),
            lease=LeaseClaim(
                job_id=str(job["job_id"]),
                worker_id=str(job["worker_id"]),
                fence=int(job["fence"]),
            ),
            work_id=work_id,
        )
        produced = run(ctx)
        if stage == "audit" and produced.verdict == "none" and produced.outcome == "succeeded":
            produced.verdict = "pass"
        return produced

    def _facts(self, steps: list[dict[str, Any]], step_index: int, stage: str, outcome: Outcome | None) -> Facts:
        if self.mission_id is None or self.project_id is None or self._request is None:
            raise AssertionError("stage context is unbound")
        record, release = qualification_record(self.config.qualification_path)
        qualified, _missing = qualification.qualified(
            record,
            unattended=bool(self._request.get("unattended")),
            release=release,
        )
        mission = self._mission()
        produced = outcome or Outcome(outcome="none")
        budget = produced.budget_exhausted or self._budget_hit(UUID(str(self._work_id(steps))))
        terminal = _open_terminal(steps)
        action = produced.action
        if terminal is not None:
            action = _terminal_action(self, terminal)
        candidate = _nested(produced.data, "candidate_commit") or _prior_field(steps, "candidate_commit")
        target = _nested(produced.data, "target_ref") or self._request.get("target_ref") or None
        return Facts(
            mission_id=str(self.mission_id),
            project_id=self.project_id,
            step_index=step_index,
            stage=stage,
            mission_status=str(mission.get("status") or "draft"),
            outcome=produced.outcome if produced.outcome in {"none", "succeeded", "failed", "crashed", "waiting"} else "failed",
            qualified=qualified,
            attempts=_count(steps, "retry", stage),
            repair_rounds=_count(steps, "repair", None),
            stop_engaged=self._stopped(),
            action=action,
            terminal_request=None if terminal is None else terminal.get("terminal_request"),
            new_scope=produced.new_scope,
            budget_exhausted=budget,
            blocker=produced.blocker,
            capacity_free=self._capacity_free(),
            alternate=_alternate(stage),
            verdict=produced.verdict if produced.verdict in {"none", "pass", "fail"} else "none",
            candidate_commit=None if candidate is None else str(candidate),
            target_ref=None if not target else str(target),
        )

    def _bounds(self, stage: str):
        if self._request is None:
            raise AssertionError("request is None")
        kind = str((self._request.get("scope") or {}).get("kind") or "engineering.execute")
        return bounds_for(load_policy(), kind, stage, self.config.repair_rounds)

    def _record_decision(self, step_index: int, chosen: Step, outcome: Outcome | None) -> None:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        body: dict[str, Any] = {
            "kind": chosen.kind,
            "rule_id": chosen.rule_id,
            "idempotency_key": f"{chosen.kind}:{self.mission_id}:{step_index}:{chosen.rule_id}",
            "stage": chosen.stage,
            "next_stage": chosen.next_stage,
            "bound": chosen.bound,
        }
        if chosen.decision is not None:
            body["decision_id"] = str(chosen.decision.decision_id)
        if chosen.kind in {"advance", "retry", "repair", "reroute", "reschedule"} and chosen.next_stage is not None:
            body["next_job"] = _stage_job(self.mission_id, step_index + 1)
        if chosen.kind == "pause" and chosen.decision is not None:
            body["wait_job"] = _wait_job(self.mission_id, chosen.decision.decision_id, 0)
        if outcome is not None:
            body["data"] = outcome.data
        self.record(step_index, body)

    def _apply(self, job: dict[str, Any], step_index: int, chosen: Step, outcome: Outcome | None) -> None:
        del outcome
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        if chosen.kind in {"advance", "retry", "repair", "reroute", "reschedule"}:
            self._enqueue(_stage_job(self.mission_id, step_index + 1), UUID(str(job["work_id"])))
            return
        if chosen.kind == "pause" and chosen.decision is not None:
            self._record_owner_decision(chosen)
            self._set_status("paused", "policy_rule", policy_rule_id=chosen.rule_id)
            self._enqueue(_wait_job(self.mission_id, chosen.decision.decision_id, 0), UUID(str(job["work_id"])))
            return
        if chosen.kind == "end":
            if chosen.rule_id == "mission-complete":
                self._set_status("completed", "policy_rule", policy_rule_id=chosen.rule_id)
            return
        if chosen.kind == "abandon":
            self._abandon(job, step_index, chosen)
            return
        if chosen.kind == "redefine":
            self._redefine(job, step_index)

    def _finish_recorded(self, job: dict[str, Any], step_index: int, outcome_step: Mapping[str, Any]) -> None:
        posted = _posted(self.steps(), step_index)
        if posted is None:
            self._run_stage(job, step_index)
            return
        self._replay_post(job, posted)

    def _replay_post(self, job: dict[str, Any], posted: Mapping[str, Any]) -> None:
        kind = str(posted.get("kind"))
        work_id = UUID(str(job["work_id"]))
        if kind in {"advance", "retry", "repair", "reroute", "reschedule"} and posted.get("next_job"):
            self._enqueue(str(posted["next_job"]), work_id)
        elif kind == "pause" and posted.get("wait_job"):
            self._enqueue(str(posted["wait_job"]), work_id)
        elif kind == "end" and posted.get("rule_id") == "mission-complete":
            self._set_status("completed", "policy_rule", policy_rule_id="mission-complete")

    def _wait(self, job: dict[str, Any], decision_id: str, generation: int) -> Settlement:
        self._job_id = str(job["job_id"])
        answer = self._answer(decision_id)
        if answer is None and self._lease_has_room(job):
            time.sleep(max(0.0, float(self.config.wait_seconds)))
            answer = self._answer(decision_id)
        if answer is None:
            if self.mission_id is None:
                raise AssertionError("mission_id is None")
            self._enqueue(
                _wait_job(self.mission_id, UUID(decision_id), generation + 1),
                UUID(str(job["work_id"])),
            )
            return Settlement(outcome="succeeded", receipts=[])
        self._apply_answer(job, decision_id, answer)
        return Settlement(outcome="succeeded", receipts=[])

    def _apply_answer(self, job: dict[str, Any], decision_id: str, answer: str) -> None:
        pause = next(
            (step for step in reversed(self.steps()) if step.get("decision_id") == decision_id),
            None,
        )
        rule_id = None if pause is None else str(pause.get("rule_id"))
        if rule_id == "d23-owner-confirm":
            # The answer is the intake decision's: confirm/edited_draft approve the
            # mission and re-run the confirm stage, which then advances to plan; a
            # note redrafts and waits on the new decision; reject already abandoned.
            if answer in {"confirm", "edited_draft", "note"}:
                if answer != "note":
                    self._set_status("running", "decision", decision_id=decision_id)
                if self.mission_id is None:
                    raise AssertionError("mission_id is None")
                self._enqueue(
                    _stage_job(self.mission_id, int(pause["step_index"]) + 1),
                    UUID(str(job["work_id"])),
                )
            return
        if answer == "resume" or (answer == "approve" and rule_id != "terminal-needs-basis"):
            self._set_status("running", "decision", decision_id=decision_id)
            if pause is not None and pause.get("next_stage"):
                if self.mission_id is None:
                    raise AssertionError("mission_id is None")
                self._enqueue(
                    _stage_job(self.mission_id, int(pause["step_index"]) + 1),
                    UUID(str(job["work_id"])),
                )
            return
        if answer in {"abandon", "approve", "decline"} and rule_id == "terminal-needs-basis":
            if answer == "decline":
                return
            terminal = _open_terminal_including_paused(self.steps())
            kind = "abandon" if terminal is None else str(terminal.get("terminal_request") or "abandon")
            if kind == "redefine":
                self._redefine(job, int(pause["step_index"]) if pause else 0, decision_id)
            else:
                self._abandon(job, int(pause["step_index"]) if pause else 0, None, decision_id)
            return
        if answer == "abandon":
            self._abandon(job, int(pause["step_index"]) if pause else 0, None, decision_id)

    def _abandon(
        self,
        job: dict[str, Any],
        step_index: int,
        chosen: Step | None,
        decision_id: str | None = None,
    ) -> None:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        cause = decision_id
        if cause is None and chosen is not None and chosen.decision is not None:
            cause = str(chosen.decision.decision_id)
        if cause is None:
            terminal = _open_terminal_including_paused(self.steps())
            basis = None if terminal is None else terminal.get("basis")
            if isinstance(basis, str) and basis:
                cause = basis
        if cause is None:
            cause = self.answered_decision("terminal-needs-basis") or self.answered_decision(
                "terminal-with-basis"
            )
        self._act_terminal(
            job,
            step_index,
            {"kind": "mission_abandon", "resource_id": str(self.mission_id)},
            "terminal-with-basis",
            decision_id=cause,
        )
        if cause is not None:
            self._set_status("abandoned", "decision", decision_id=cause)

    def _redefine(self, job: dict[str, Any], step_index: int, decision_id: str | None = None) -> None:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        self._act_terminal(
            job,
            step_index,
            {"kind": "scope_broaden", "resource_id": str(self.mission_id)},
            "terminal-with-basis",
            decision_id=decision_id,
        )
        terminal = _latest(self.steps(), "terminal_request")
        draft = None if terminal is None else terminal.get("draft")
        if not isinstance(draft, dict):
            return
        mission = self._mission()
        self._command(
            "revise_mission",
            {
                "mission_id": str(self.mission_id),
                "base_revision": int(mission.get("revision") or 1),
                "draft": draft,
            },
            f"revise:{self.mission_id}:{step_index}",
        )
        if decision_id is not None:
            self._set_status("running", "decision", decision_id=decision_id)

    def _act_terminal(
        self,
        job: dict[str, Any],
        step_index: int,
        operation: Mapping[str, Any],
        rule_id: str,
        decision_id: str | None = None,
    ) -> None:
        if self.project_id is None:
            raise AssertionError("project_id is None")
        ctx = StageContext(
            self,
            step_index=step_index,
            stage="close",
            attempt=0,
            repair_round=0,
            request=self._request or {},
            prior={},
            lease=LeaseClaim(
                job_id=str(job["job_id"]),
                worker_id=str(job.get("worker_id") or ""),
                fence=int(job.get("fence") or 0),
            ),
            work_id=UUID(str(job["work_id"])),
        )
        ctx.act(
            operation,
            ResolvedTarget(),
            lambda _op, _resolved: None,
            rule_id=rule_id,
            decision_id=decision_id,
        )

    def _record_owner_decision(self, chosen: Step) -> None:
        if chosen.decision is None:
            raise AssertionError("decision is None")
        # A tier-3 hold already wrote the same decision through ``perform``; the
        # pause reuses it instead of writing a second one under the same id.
        if self.find_decision(str(chosen.decision.decision_id)) is not None:
            return
        payload = chosen.decision.model_dump(mode="json")
        self._command(
            "create_decision",
            payload,
            str(chosen.decision.decision_id),
        )

    def _set_status(
        self,
        target: str,
        cause_kind: str,
        *,
        policy_rule_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        payload: dict[str, Any] = {
            "mission_id": str(self.mission_id),
            "target_status": target,
            "cause_kind": cause_kind,
        }
        if policy_rule_id is not None:
            payload["policy_rule_id"] = policy_rule_id
        if decision_id is not None:
            payload["decision_id"] = decision_id
        self._command(
            "set_mission_status",
            payload,
            str(uuid5(NAMESPACE_URL, f"omp-417:{self.mission_id}:status:{target}:{cause_kind}:{policy_rule_id or decision_id}")),
        )

    def _command(self, command_type: str, payload: Mapping[str, Any], operation_id: str) -> None:
        try:
            operation = UUID(operation_id)
        except ValueError:
            operation = uuid5(NAMESPACE_URL, operation_id)
        _run_command(self.config, self.principal, command_type, payload, operation)

    def _enqueue(self, job_id: str, work_id: UUID) -> None:
        enqueue_job(
            self.store,
            operation_id=str(uuid5(NAMESPACE_URL, f"omp-417:enqueue:{job_id}")),
            workspace_id=self.config.workspace_id,
            actor_id=self.principal.actor_id,
            job_id=job_id,
            work_id=work_id,
            kind="compute",
            required_capabilities=[_CAPABILITY],
            resources=dict(_RESOURCES),
            lease_seconds=self.config.lease_seconds,
        )

    def _mission(self) -> dict[str, Any]:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        try:
            with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
                return read_mission(cur, self.config.workspace_id, self.mission_id)
        except WorkStoreError:
            return {"status": "draft", "revision": 1}

    def _stopped(self) -> bool:
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            return bool(read_stop_state(cur, self.config.workspace_id)["stopped"])

    def _budget_hit(self, work_id: UUID) -> bool:
        try:
            with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
                exhausted = check_item_budget(
                    self.store,
                    cur,
                    workspace_id=self.config.workspace_id,
                    actor_id=self.principal.actor_id,
                    work_id=work_id,
                    operation_id=str(uuid5(NAMESPACE_URL, f"omp-417:budget:{work_id}")),
                )
        except Exception:  # noqa: BLE001 - a missing budget row is not exhaustion
            return False
        return bool(exhausted)

    def _capacity_free(self) -> bool:
        if self.mission_id is None:
            raise AssertionError("mission_id is None")
        record, _release = qualification_record(self.config.qualification_path)
        limit = qualification.max_workers(self.config.max_workers, record)
        prefix = f"orch:{self.mission_id}:"
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            cur.execute(
                """
                SELECT job_id, required_capabilities
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND source='native'
                  AND status IN ('admitted', 'in_flight')
                """,
                (self.config.workspace_id,),
            )
            rows = cur.fetchall()
        others = 0
        for row in rows:
            job_id = str(row["job_id"])
            if job_id.startswith(prefix):
                continue
            caps = row["required_capabilities"] or []
            if _CAPABILITY in caps:
                others += 1
        return others < limit

    def _answer(self, decision_id: str) -> str | None:
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            record = find_decision(cur, self.config.workspace_id, decision_id)
        if record is None or record.get("status") != "answered":
            return None
        answer = record.get("answer")
        return None if not isinstance(answer, str) else answer

    def _lease_has_room(self, job: dict[str, Any]) -> bool:
        with self.store.transaction(self.config.workspace_id, self.principal.actor_id) as cur:
            cur.execute(
                """
                SELECT lease_expires_at > clock_timestamp() + make_interval(secs => %s) AS room
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND job_id=%s
                """,
                (float(self.config.wait_seconds), self.config.workspace_id, str(job["job_id"])),
            )
            row = cur.fetchone()
        return bool(row and row["room"])

    def _work_id(self, steps: list[dict[str, Any]]) -> str:
        submitted = next(step for step in steps if step.get("kind") == "submitted")
        return str(submitted["work_id"])


def _parse_job(job_id: str) -> tuple[UUID, str, str, int] | None:
    """``(mission, 'stage'|'wait', step-or-decision, generation)``."""
    if not job_id.startswith("orch:"):
        return None
    body = job_id[len("orch:") :]
    mission_text, _, rest = body.partition(":")
    try:
        mission_id = UUID(mission_text)
    except ValueError:
        return None
    if rest.startswith("wait:"):
        _wait, decision_text, generation_text = rest.split(":")
        return mission_id, "wait", decision_text, int(generation_text)
    return mission_id, "stage", rest, 0


def _stage_job(mission_id: UUID, step_index: int) -> str:
    return f"orch:{mission_id}:{step_index}"


def _wait_job(mission_id: UUID, decision_id: UUID, generation: int) -> str:
    return f"orch:{mission_id}:wait:{decision_id}:{generation}"


def _stage_for(steps: list[dict[str, Any]], step_index: int) -> str:
    if step_index == 0:
        return STAGE_ORDER[0]
    previous = _posted(steps, step_index - 1)
    if previous is not None and previous.get("next_stage"):
        return str(previous["next_stage"])
    for step in reversed(steps):
        if step.get("kind") in _POST_KINDS and step.get("next_stage") and int(step["step_index"]) < step_index:
            return str(step["next_stage"])
    return STAGE_ORDER[0]


def _posted(steps: list[dict[str, Any]], step_index: int) -> dict[str, Any] | None:
    found = [step for step in steps if int(step.get("step_index", -1)) == step_index and step.get("kind") in _POST_KINDS]
    return found[-1] if found else None


def _outcome_step(steps: list[dict[str, Any]], step_index: int) -> dict[str, Any] | None:
    found = [step for step in steps if int(step.get("step_index", -1)) == step_index and step.get("kind") == "outcome"]
    return found[-1] if found else None


def _action_document(action: ActionVerdict | None) -> dict[str, Any] | None:
    if action is None:
        return None
    return {
        "tier": action.tier,
        "action_class": action.action_class,
        "target_sha256": action.target_sha256,
        "allowed": action.allowed,
        "code": action.code,
    }


def _action_from(value: Any) -> ActionVerdict | None:
    if not isinstance(value, dict) or "tier" not in value:
        return None
    return ActionVerdict(
        tier=int(value["tier"]),
        action_class=str(value.get("action_class") or "unlisted"),
        target_sha256=None if value.get("target_sha256") is None else str(value["target_sha256"]),
        allowed=bool(value.get("allowed")),
        code=str(value.get("code") or ""),
    )


def _outcome_body(mission_id: UUID, step_index: int, stage: str, outcome: Outcome) -> dict[str, Any]:
    return {
        "kind": "outcome",
        "rule_id": "stage-outcome",
        "idempotency_key": f"outcome:{mission_id}:{step_index}",
        "stage": stage,
        "outcome": outcome.outcome,
        "data": outcome.data,
        "verdict": outcome.verdict,
        "action": _action_document(outcome.action),
        "new_scope": outcome.new_scope,
        "budget_exhausted": outcome.budget_exhausted,
        "blocker": outcome.blocker,
    }


def _outcome_from(step: Mapping[str, Any] | None) -> Outcome | None:
    if step is None:
        return None
    data = step.get("data")
    blocker = step.get("blocker")
    return Outcome(
        outcome=str(step.get("outcome") or "succeeded"),
        data=data if isinstance(data, dict) else {},
        action=_action_from(step.get("action")),
        new_scope=bool(step.get("new_scope")),
        budget_exhausted=bool(step.get("budget_exhausted")),
        blocker=None if not isinstance(blocker, str) else blocker,
        verdict=str(step.get("verdict") or "none"),
    )


def _outcome_decision_id(outcome: Outcome | None) -> str | None:
    """The decision an outcome names for the owner to answer, if any."""
    if outcome is None:
        return None
    value = outcome.data.get("decision_id")
    return str(value) if value else None


def _count(steps: list[dict[str, Any]], kind: str, stage: str | None) -> int:
    total = 0
    for step in steps:
        if step.get("kind") != kind:
            continue
        if stage is not None and step.get("stage") != stage:
            continue
        total += 1
    return total


def _prior(steps: list[dict[str, Any]]) -> dict[str, Any]:
    prior: dict[str, Any] = {}
    for step in steps:
        if step.get("kind") != "outcome":
            continue
        stage = str(step.get("stage") or "")
        bucket = prior.setdefault(stage, {"visits": 0, "data": {}})
        bucket["visits"] = int(bucket["visits"]) + 1
        if isinstance(step.get("data"), dict):
            bucket["data"] = step["data"]
    return prior


def _prior_field(steps: list[dict[str, Any]], key: str) -> Any:
    for step in reversed(steps):
        data = step.get("data")
        if isinstance(data, dict) and data.get(key):
            return data[key]
    return None


def _nested(data: Mapping[str, Any], key: str) -> Any:
    value = data.get(key)
    return value or None


def _latest(steps: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    found = [step for step in steps if step.get("kind") == kind]
    return found[-1] if found else None


def _open_terminal(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for step in steps:
        if step.get("kind") == "terminal_request":
            latest = step
        elif step.get("rule_id") in _TERMINAL_RULES:
            latest = None
    return latest


def _open_terminal_including_paused(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The terminal request a pause answer applies, including one already paused."""
    found = [step for step in steps if step.get("kind") == "terminal_request"]
    return found[-1] if found else None


def _terminal_action(handler: OrchestratorHandler, terminal: Mapping[str, Any]) -> ActionVerdict:
    kind = str(terminal.get("terminal_request") or "abandon")
    operation = (
        {"kind": "scope_broaden", "resource_id": str(handler.mission_id)}
        if kind == "redefine"
        else {"kind": "mission_abandon", "resource_id": str(handler.mission_id)}
    )
    classified = classify(parse_submission(operation), ResolvedTarget())
    allowed = False
    basis = terminal.get("basis")
    if basis:
        with handler.store.transaction(handler.config.workspace_id, handler.principal.actor_id) as cur:
            record = find_decision(cur, handler.config.workspace_id, str(basis))
        if (
            record is not None
            and record.get("status") == "answered"
            and record.get("answer") == "approve"
            and record.get("action_class") == classified.action_class
            and str(record.get("target_sha256") or "") == classified.target_sha256
        ):
            allowed = True
    return ActionVerdict(
        tier=classified.tier,
        action_class=classified.action_class,
        target_sha256=classified.target_sha256,
        allowed=allowed,
        code="decision" if allowed else "no_basis",
    )


def _alternate(stage: str) -> str | None:
    policy: RoutingPolicy = load_policy()
    rule = policy.stages.get(stage)
    if rule is None:
        return None
    for alternate in policy.alternates:
        if alternate.from_provider == rule.provider:
            return f"{alternate.to.provider}:{alternate.to.model}"
    return None


def _draft(request: MissionRequest) -> dict[str, Any]:
    draft = request.scope.model_dump(mode="json")
    draft["objective"] = request.intake.goal.statement
    draft["constraints"] = [item.statement for item in request.intake.constraints]
    draft["acceptance_criteria"] = [item.statement for item in request.intake.acceptance_criteria]
    return draft


def _ids(mission_id: UUID, name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"omp-417:{mission_id}:{name}")


def submit(config: OrchestratorConfig, request: MissionRequest) -> dict[str, Any]:
    """Create the work item and either record a qualification decision or enqueue step 0.

    Without an ``owner.json`` beside the automation capability there is no owner
    to approve the mission, so nothing is pre-approved: intake drafts the
    mission and the confirm stage waits for the owner's answer.
    """
    _require_jobs(config.ops)
    automation = _principal(config.automation_capability_path)
    created = _run_command(
        config,
        automation,
        "create_work_batch",
        {"items": [request.work.model_dump(mode="json")], "relations": []},
        _ids(request.mission_id, "work"),
    )
    if created.get("replayed"):
        work_id = _existing_work_id(config, request.mission_id)
    else:
        work_id = UUID(str(created["items"][0]["work_id"]))
    record, release = qualification_record(config.qualification_path)
    qualified, _missing = qualification.qualified(
        record, unattended=request.unattended, release=release
    )
    if not qualified:
        facts = Facts(
            mission_id=str(request.mission_id),
            project_id=request.project_id,
            step_index=0,
            stage=STAGE_ORDER[0],
            mission_status="draft",
            outcome="none",
            qualified=False,
        )
        chosen = decide(facts, bounds_for(load_policy(), request.scope.kind, STAGE_ORDER[0], config.repair_rounds))
        decision_id = None if chosen.decision is None else str(chosen.decision.decision_id)
        if chosen.decision is not None:
            _run_command(
                config,
                automation,
                "create_decision",
                chosen.decision.model_dump(mode="json"),
                chosen.decision.decision_id,
            )
        return {
            "status": "refused",
            "code": chosen.rule_id,
            "work_id": str(work_id) if work_id is not None else None,
            "decision_id": decision_id,
            "job_id": None,
        }
    _prepare_mission(config, request, automation)
    job_id = _stage_job(request.mission_id, 0)
    if work_id is None:
        raise AssertionError("work_id is None")
    enqueue_job(
        NativeJobStore(config.ops),
        operation_id=str(_ids(request.mission_id, "enqueue:0")),
        workspace_id=config.workspace_id,
        actor_id=automation.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=[_CAPABILITY],
        resources=dict(_RESOURCES),
        lease_seconds=config.lease_seconds,
    )
    handler = OrchestratorHandler(config)
    handler.mission_id = request.mission_id
    handler.project_id = request.project_id
    handler._job_id = job_id
    handler.record(
        0,
        {
            "kind": "submitted",
            "rule_id": "submitted",
            "idempotency_key": f"submitted:{request.mission_id}",
            "request": request.document(),
            "work_id": str(work_id),
            "stage": STAGE_ORDER[0],
        },
    )
    return {
        "status": "enqueued",
        "code": None,
        "work_id": str(work_id),
        "decision_id": None,
        "job_id": job_id,
    }


def _existing_work_id(config: OrchestratorConfig, mission_id: UUID) -> UUID | None:
    handler = OrchestratorHandler(config)
    handler.mission_id = mission_id
    try:
        submitted = next(step for step in handler.steps() if step.get("kind") == "submitted")
    except StopIteration:
        return None
    return UUID(str(submitted["work_id"]))


def _prepare_mission(
    config: OrchestratorConfig,
    request: MissionRequest,
    automation: Principal,
) -> None:
    owner_path = config.automation_capability_path.parent / "owner.json"
    if not owner_path.is_file():
        return
    owner = _principal(owner_path)
    draft = _draft(request)
    _run_command(
        config,
        automation,
        "submit_mission",
        {"mission_id": str(request.mission_id), "draft": draft},
        _ids(request.mission_id, "submit-mission"),
    )
    _run_command(
        config,
        owner,
        "approve_mission",
        {
            "mission_id": str(request.mission_id),
            "revision": 1,
            "basis_kind": "decision",
            "basis_id": str(_ids(request.mission_id, "basis")),
        },
        _ids(request.mission_id, "approve-mission"),
    )
    _run_command(
        config,
        automation,
        "set_mission_status",
        {
            "mission_id": str(request.mission_id),
            "target_status": "running",
            "cause_kind": "principal",
        },
        _ids(request.mission_id, "start"),
    )
    with NativeJobStore(config.ops).transaction(config.workspace_id, automation.actor_id) as cur:
        cur.execute(
            """
            INSERT INTO omp_work.project_missions
                (workspace_id, mission_id, project_id, objective, status)
            VALUES (%s, %s, %s, %s, 'running')
            ON CONFLICT DO NOTHING
            """,
            (
                config.workspace_id,
                request.mission_id,
                request.project_id,
                request.intake.goal.statement,
            ),
        )


def request_terminal(
    config: OrchestratorConfig,
    mission_id: UUID,
    kind: str,
    *,
    basis: str | None = None,
    draft: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a terminal request. The next stage applies it, or pauses when it has no basis."""
    if kind not in {"abandon", "cancel", "redefine"}:
        raise OrchestratorError("invalid_request")
    handler = OrchestratorHandler(config)
    handler.mission_id = mission_id
    steps = handler.steps()
    submitted = next((step for step in steps if step.get("kind") == "submitted"), None)
    if submitted is None:
        raise OrchestratorError("invalid_request")
    handler.project_id = UUID(str(submitted["request"]["project_id"]))
    handler._job_id = _stage_job(mission_id, 0)
    key = f"terminal:{mission_id}:{len([step for step in steps if step.get('kind') == 'terminal_request'])}"
    handler.record(
        0,
        {
            "kind": "terminal_request",
            "rule_id": "terminal-request",
            "idempotency_key": key,
            "terminal_request": kind,
            "basis": basis,
            "draft": None if draft is None else dict(draft),
        },
    )
    return {"status": "recorded", "mission_id": str(mission_id), "kind": kind}


def dry_run(config: OrchestratorConfig, request: MissionRequest) -> dict[str, Any]:
    """Decide the next step and write nothing."""
    record, release = qualification_record(config.qualification_path)
    qualified, _missing = qualification.qualified(
        record, unattended=request.unattended, release=release
    )
    facts = Facts(
        mission_id=str(request.mission_id),
        project_id=request.project_id,
        step_index=0,
        stage=STAGE_ORDER[0],
        mission_status="running",
        outcome="none",
        qualified=qualified,
        alternate=_alternate(STAGE_ORDER[0]),
        target_ref=request.target_ref or None,
    )
    chosen = decide(facts, bounds_for(load_policy(), request.scope.kind, facts.stage, config.repair_rounds))
    return _step_document(chosen)


def status(config: OrchestratorConfig, mission_id: UUID) -> dict[str, Any]:
    """The mission's recorded steps and current status."""
    handler = OrchestratorHandler(config)
    handler.mission_id = mission_id
    steps = []
    try:
        raw = handler.steps()
    except OrchestratorError:
        raw = []
    for step in raw:
        steps.append(
            {
                "step_index": step.get("step_index"),
                "kind": step.get("kind"),
                "rule_id": step.get("rule_id"),
                "stage": step.get("stage"),
                "decision_id": step.get("decision_id"),
            }
        )
    mission: dict[str, Any] = {}
    try:
        handler._bind(mission_id)
        mission = handler._mission()
    except (OrchestratorError, StopIteration, WorkStoreError):
        mission = {}
    return {
        "mission_id": str(mission_id),
        "status": mission.get("status"),
        "steps": steps,
    }


def _step_document(chosen: Step) -> dict[str, Any]:
    return {
        "kind": chosen.kind,
        "stage": chosen.stage,
        "next_stage": chosen.next_stage,
        "rule_id": chosen.rule_id,
        "bound": chosen.bound,
        "decision_id": None if chosen.decision is None else str(chosen.decision.decision_id),
    }


def qualify(lock_map: Path, out: Path, *, release: str | None = None) -> dict[str, Any]:
    """Run each mapped pytest and write ``{release, results: {key: {item, test, result}}}``."""
    document = json.loads(Path(lock_map).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise OrchestratorError("invalid_request")
    chosen = release if release is not None else str(document.get("release") or "")
    locks = document.get("locks") if isinstance(document.get("locks"), dict) else document
    if not isinstance(locks, dict):
        raise OrchestratorError("invalid_request")
    results: dict[str, dict[str, str]] = {}
    for key, entry in locks.items():
        if key == "release":
            continue
        if isinstance(entry, str):
            item, test = str(key), entry
        elif isinstance(entry, dict) and "test" in entry:
            item, test = str(entry.get("item") or key), str(entry["test"])
        else:
            continue
        completed = subprocess.run(  # nosec B603 - pytest is this interpreter, argv is the mapped test path
            [sys.executable, "-m", "pytest", test, "-q", "--tb=no"],
            capture_output=True,
            text=True,
            check=False,
        )
        results[str(key)] = {
            "item": item,
            "test": test,
            "result": "pass" if completed.returncode == 0 else "fail",
        }
    body = {"release": chosen, "results": results}
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return body


def _ops_from(value: Any) -> OperationsConfig | None:
    if value is None or isinstance(value, OperationsConfig):
        return value
    if not isinstance(value, dict):
        raise OrchestratorError("invalid_request")
    return OperationsConfig(
        config_dir=Path(value["config_dir"]),
        state_dir=Path(value["state_dir"]),
        data_dir=Path(value["data_dir"]),
        database=str(value.get("database") or "omp_work"),
        host=str(value.get("host") or "127.0.0.1"),
        port=int(value.get("port") or 54321),
        aws_profile=str(value.get("aws_profile") or "default"),
        aws_region=str(value.get("aws_region") or "us-east-1"),
        bucket=str(value.get("bucket") or "omp-work-ledger-037842804132-us-east-1"),
        prefix=str(value.get("prefix") or "work-ledger/v1"),
        endpoint_url=None if value.get("endpoint_url") is None else str(value["endpoint_url"]),
    )


def factory(config_path: str | None = None, **kwargs: Any) -> OrchestratorHandler:
    """Handler factory for the jobs worker. ``config_path`` is the orchestrator config."""
    path = config_path or kwargs.get("config_path")
    ops = _ops_from(kwargs.get("ops") if kwargs.get("ops") is not None else kwargs.get("operations"))
    return OrchestratorHandler(load_config(None if path is None else Path(str(path)), ops))


def main(argv: list[str]) -> int:
    """``omp-work orchestrator`` command dispatch. ``argv`` excludes the subcommand name."""
    parser = argparse.ArgumentParser(prog="omp-work orchestrator")
    commands = parser.add_subparsers(dest="orchestrator_command", required=True)

    def _with_config(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command.add_argument("--config", type=Path)
        return command

    submit_parser = _with_config(commands.add_parser("submit"))
    submit_parser.add_argument("--request", required=True, type=Path)
    status_parser = _with_config(commands.add_parser("status"))
    status_parser.add_argument("--mission", required=True, type=UUID)
    terminal_parser = _with_config(commands.add_parser("request-terminal"))
    terminal_parser.add_argument("--mission", required=True, type=UUID)
    terminal_parser.add_argument("--kind", required=True)
    terminal_parser.add_argument("--basis")
    terminal_parser.add_argument("--draft", type=Path)
    dry_parser = _with_config(commands.add_parser("dry-run"))
    dry_parser.add_argument("--request", required=True, type=Path)
    qualify_parser = commands.add_parser("qualify")
    qualify_parser.add_argument("--lock-map", required=True, type=Path)
    qualify_parser.add_argument("--out", required=True, type=Path)
    qualify_parser.add_argument("--release")
    args = parser.parse_args(argv)
    try:
        if args.orchestrator_command == "qualify":
            body = qualify(args.lock_map, args.out, release=args.release)
        else:
            config = load_config(args.config)
            if args.orchestrator_command == "submit":
                body = submit(config, load_request(args.request))
            elif args.orchestrator_command == "status":
                body = status(config, args.mission)
            elif args.orchestrator_command == "dry-run":
                body = dry_run(config, load_request(args.request))
            else:
                draft = None
                if args.draft is not None:
                    draft = json.loads(args.draft.read_text(encoding="utf-8"))
                body = request_terminal(
                    config,
                    args.mission,
                    args.kind,
                    basis=args.basis,
                    draft=draft,
                )
    except OrchestratorError as error:
        print(error.code, file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0
