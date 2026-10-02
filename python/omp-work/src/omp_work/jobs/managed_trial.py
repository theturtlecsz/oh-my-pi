"""Managed-trial evaluation worker on the s02 worker (R06, OMP-315).

``dispatch_managed_trial`` enqueues the ``managed-trial:<trial_id>`` native
compute job with the ``research.managed_trial`` capability, but only when the
work item's mission authorizes it: an approved or running ``research.run``
mission that requested that capability. ``evaluate_candidate`` is the database
free evaluation loop: it materializes the frozen candidate, runs its command,
runs the sealed evaluator on the produced outputs and on an empty negative
control, and returns a typed :class:`Evaluation`. The handler records the
evaluator's typed receipt as a custody artifact, records the legacy observation
that confirms it, and returns the evaluator receipt that settles the job.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.runner import Backend, LocalJailBackend, RunRequest
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, Settlement, WorkerConfig
from omp_work.operations.artifacts import bytes_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.research.engineering import materialize
from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    EvaluationProtocol,
    TrialReceipt,
    load_protocol,
    managed_job_id,
    output_digest,
)
from omp_work.v1.api_models import (
    CommandResponse,
    RecordResearchObservationResult,
    RegisterResearchArtifactResult,
)
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.missions import mission_for_work
from omp_work.v1.models import (
    CommandEnvelope,
    MissionStatus,
    RecordResearchObservationCommand,
    RecordResearchObservationPayload,
    RegisterResearchArtifactCommand,
    RegisterResearchArtifactPayload,
    ResearchArtifactManifest,
)

__all__ = [
    "CAPABILITY",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVALUATOR_FILENAME",
    "Evaluation",
    "ManagedTrialHandler",
    "ManagedTrialRefused",
    "dispatch_managed_trial",
    "evaluate_candidate",
    "factory",
    "main",
]

API_VERSION = "work.omp.dev/v1"
CAPABILITY = MANAGED_CAPABILITY
ISSUER_KIND = "legacy_autoresearch"
EVALUATOR_FILENAME = "evaluate"
INPUT_FILENAME = "input.json"
SCORE_FILENAME = "score.json"
DEFAULT_TIMEOUT_SECONDS = 600
_AUTHORIZING_KIND = "research.run"
_TRIAL_QUERY = """
SELECT t.trial_id, t.campaign_id, t.work_id, t.candidate_digest, t.evaluator_sha256,
       c.spec
FROM omp_research.trials t
JOIN omp_research.campaigns c
  ON c.workspace_id = t.workspace_id AND c.campaign_id = t.campaign_id
WHERE t.workspace_id=%s AND t.trial_id=%s
"""


class ManagedTrialRefused(Exception):
    """The work item's mission does not authorize a managed trial.

    ``code`` is ``mission_not_authorized``.
    """

    code: str

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Evaluation:
    """The outcome of one managed evaluation, before any receipt is issued.

    ``canceled`` short-circuits every other field. ``verdict`` is
    ``qualified``, ``candidate_failed``, or ``evaluator_failed``.
    """

    canceled: bool = False
    verdict: str | None = None
    failure_reason: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    output_sha256: str | None = None
    negative_control: str = "rejected"
    candidate_manifest: str | None = None
    evaluator_manifest: str | None = None


def dispatch_managed_trial(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    work_id: UUID,
    trial_id: UUID,
    resources: dict[str, int],
    lease_seconds: int,
) -> dict[str, object]:
    """Enqueue the compute job that evaluates one proposed managed trial.

    The work item's mission must be approved or running, of kind
    ``research.run``, and must have requested ``research.managed_trial``.
    Anything else is :class:`ManagedTrialRefused` with
    ``mission_not_authorized`` and no job.
    """
    with store.transaction(workspace_id, actor_id) as cur:
        mission = mission_for_work(cur, workspace_id, work_id)
    if not _mission_authorizes(mission):
        raise ManagedTrialRefused("mission_not_authorized")
    return enqueue_job(
        store,
        operation_id=operation_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=managed_job_id(trial_id),
        work_id=work_id,
        kind="compute",
        required_capabilities=[CAPABILITY],
        resources=resources,
        lease_seconds=lease_seconds,
        trial_id=trial_id,
    )


def _mission_authorizes(mission: object) -> bool:
    if mission is None:
        return False
    status = getattr(mission, "status", None)
    if status not in (MissionStatus.APPROVED, MissionStatus.RUNNING):
        return False
    if getattr(mission, "kind", None) != _AUTHORIZING_KIND:
        return False
    return CAPABILITY in tuple(getattr(mission, "requested_capabilities", ()))


def evaluate_candidate(
    backend: Backend,
    protocol: EvaluationProtocol,
    *,
    candidate_tar: bytes,
    candidate_digest: str,
    evaluator_script: bytes,
    confirmation: bytes | None,
    cancel: Any,
    scratch_dir: Path | None = None,
) -> Evaluation:
    """Run one candidate and its evaluator, with no database side effects.

    Every run dir lives under ``scratch_dir`` (system temp by default); with a
    caller-supplied ``scratch_dir`` the dirs are left in place for inspection.
    The candidate never receives the confirmation file. A canceled run returns
    ``Evaluation(canceled=True)``.
    """
    root = Path(
        tempfile.mkdtemp(
            prefix="omp-managed-trial-",
            dir=None if scratch_dir is None else str(scratch_dir),
        )
    )
    try:
        return _evaluate(
            backend,
            protocol,
            candidate_tar,
            candidate_digest,
            evaluator_script,
            confirmation,
            cancel,
            root,
        )
    finally:
        if scratch_dir is None:
            shutil.rmtree(root, ignore_errors=True)


def _evaluate(
    backend: Backend,
    protocol: EvaluationProtocol,
    candidate_tar: bytes,
    candidate_digest: str,
    evaluator_script: bytes,
    confirmation: bytes | None,
    cancel: Any,
    root: Path,
) -> Evaluation:
    candidate_dir = root / "candidate"
    try:
        materialize(candidate_tar, candidate_digest, candidate_dir)
    except (ValueError, OSError):
        return Evaluation(
            verdict="candidate_failed", failure_reason="candidate_unavailable"
        )

    candidate = backend.run(
        RunRequest(
            protocol.candidate_command,
            candidate_dir,
            timeout=protocol.timeout_seconds,
            requires=protocol.requires,
        ),
        cancel,
    )
    if candidate.status == "canceled":
        return Evaluation(canceled=True)
    if candidate.status != "completed" or candidate.exit_code != 0:
        reason = (
            "candidate_timed_out"
            if candidate.status == "timed_out"
            else "candidate_failed"
        )
        return Evaluation(
            verdict="candidate_failed",
            failure_reason=reason,
            candidate_manifest=candidate.manifest_sha256,
        )

    output_paths = _output_files(candidate_dir, protocol.outputs)
    if output_paths is None:
        return Evaluation(
            verdict="candidate_failed",
            failure_reason="candidate_output_missing",
            candidate_manifest=candidate.manifest_sha256,
        )
    produced = {relative: path.read_bytes() for relative, path in output_paths.items()}

    confirmation_path: Path | None = None
    if confirmation is not None:
        confirmation_path = root / "confirmation"
        confirmation_path.write_bytes(confirmation)

    evaluator_dir = root / "evaluator"
    evaluator_dir.mkdir()
    _write_evaluator(evaluator_dir, evaluator_script)
    reads = [str(path) for path in output_paths.values()]
    if confirmation_path is not None:
        reads.append(str(confirmation_path))
    _write_handoff(evaluator_dir, output_paths, confirmation_path)
    first = backend.run(
        RunRequest(
            protocol.evaluator_command,
            evaluator_dir,
            ro_files=reads,
            timeout=protocol.timeout_seconds,
            requires=protocol.requires,
        ),
        cancel,
    )
    if first.status == "canceled":
        return Evaluation(canceled=True)
    scored = (
        _read_score(evaluator_dir, protocol)
        if first.status == "completed" and first.exit_code == 0
        else None
    )
    if scored is None or not scored[0]:
        return Evaluation(
            verdict="evaluator_failed",
            failure_reason="evaluator_failed",
            candidate_manifest=candidate.manifest_sha256,
            evaluator_manifest=first.manifest_sha256,
        )
    metrics = scored[1]

    control_dir = root / "control"
    control_dir.mkdir()
    _write_evaluator(control_dir, evaluator_script)
    control_reads = [] if confirmation_path is None else [str(confirmation_path)]
    _write_handoff(control_dir, {}, confirmation_path)
    control = backend.run(
        RunRequest(
            protocol.evaluator_command,
            control_dir,
            ro_files=control_reads,
            timeout=protocol.timeout_seconds,
            requires=protocol.requires,
        ),
        cancel,
    )
    if control.status == "canceled":
        return Evaluation(canceled=True)
    control_score = (
        _read_score(control_dir, protocol)
        if control.status == "completed" and control.exit_code == 0
        else None
    )
    if control_score is not None and control_score[0]:
        return Evaluation(
            verdict="evaluator_failed",
            failure_reason="negative_control_accepted",
            negative_control="accepted",
            candidate_manifest=candidate.manifest_sha256,
            evaluator_manifest=first.manifest_sha256,
        )
    return Evaluation(
        verdict="qualified",
        metrics=metrics,
        output_sha256=output_digest(produced),
        negative_control="rejected",
        candidate_manifest=candidate.manifest_sha256,
        evaluator_manifest=first.manifest_sha256,
    )


def _output_files(
    candidate_dir: Path, outputs: tuple[str, ...]
) -> dict[str, Path] | None:
    """Candidate path of every declared output, each a regular file in the jail.

    ``lstat`` alone follows a symlinked parent, so a candidate could point a
    declared name at a host file and have the evaluator read it. Every ancestor
    must be a real directory that resolves inside ``candidate_dir``; a symlinked
    ancestor is refused rather than followed.
    """
    root = candidate_dir.resolve()
    files: dict[str, Path] = {}
    for relative in outputs:
        ancestor = root
        for part in Path(relative).parts[:-1]:
            ancestor = ancestor / part
            try:
                info = ancestor.lstat()
            except OSError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                return None
            resolved = ancestor.resolve()
            if resolved != root and root not in resolved.parents:
                return None
        path = root / relative
        try:
            info = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        files[relative] = path
    return files


def _write_evaluator(directory: Path, script: bytes) -> None:
    path = directory / EVALUATOR_FILENAME
    path.write_bytes(script)
    path.chmod(0o755)


def _write_handoff(
    directory: Path, output_paths: dict[str, Path], confirmation_path: Path | None
) -> None:
    """Write ``input.json``: each candidate output's jail path and the confirmation."""
    handoff = {
        "outputs": {relative: str(path) for relative, path in output_paths.items()},
        "confirmation": None if confirmation_path is None else str(confirmation_path),
    }
    (directory / INPUT_FILENAME).write_text(canonical_json(handoff), encoding="utf-8")


def _read_score(
    directory: Path, protocol: EvaluationProtocol
) -> tuple[bool, dict[str, float]] | None:
    try:
        data = json.loads((directory / SCORE_FILENAME).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    valid = data.get("valid")
    if not isinstance(valid, bool):
        return None
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        return None
    declared = {metric.name for metric in protocol.metrics}
    if set(metrics) != declared:
        return None
    clean: dict[str, float] = {}
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            numeric = float(value)
        except OverflowError:
            return None
        if not math.isfinite(numeric):
            return None
        clean[str(name)] = numeric
    return valid, clean


def factory(
    options: dict[str, Any] | None = None, **overrides: Any
) -> ManagedTrialHandler:
    """Build the handler from worker options; ``execute`` overrides the client.

    ``load_handler`` calls this as ``factory(**options)``; the same options may
    also be passed as a single mapping.
    """
    merged: dict[str, Any] = dict(options or {})
    merged.update(overrides)
    options = merged
    content_dir = options.get("content_dir")
    if content_dir is None:
        raise ValueError("content_dir is required")
    execute = options.get("execute")
    if execute is None:
        work_url = options.get("work_url")
        bearer_file = options.get("bearer_file")
        workspace_id = options.get("workspace_id")
        if not work_url or not bearer_file or not workspace_id:
            raise ValueError(
                "work_url, bearer_file, and workspace_id are required without execute"
            )
        client = WorkClient(
            str(work_url), UUID(str(workspace_id)), Path(str(bearer_file))
        )
        execute = client.execute
    scratch_dir = options.get("scratch_dir")
    backend = options.get("backend")
    if backend is None:
        backend = LocalJailBackend()
    return ManagedTrialHandler(
        content_dir=Path(str(content_dir)),
        backend=backend,
        execute=execute,
        scratch_dir=None if scratch_dir is None else Path(str(scratch_dir)),
    )


class ManagedTrialHandler:
    """Runs one managed trial's candidate and evaluator and records its receipt."""

    def __init__(
        self,
        *,
        content_dir: Path,
        backend: Backend | None = None,
        execute: Callable[[CommandEnvelope], CommandResponse] | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.content_dir = Path(content_dir)
        self.backend = backend if backend is not None else LocalJailBackend()
        self.execute = execute
        self.scratch_dir = None if scratch_dir is None else Path(scratch_dir)
        self.last_evaluation: Evaluation | None = None
        self.last_observation: dict[str, object] | None = None
        self.last_receipt: dict[str, object] | None = None
        self.last_settlement: Settlement | None = None
        self.run_count = 0
        self.observe_count = 0

    def observe(self, worker: Any, job: dict[str, Any]) -> Settlement | None:
        """Rebuild the settlement from the stored observation, without running."""
        store, workspace_id, actor_id = _worker_scope(worker)
        job_id = str(job["job_id"])
        row = self._observation_row(store, workspace_id, actor_id, job_id)
        if row is None:
            return None
        trial = self._trial_row(store, workspace_id, actor_id, job)
        if trial is None:
            return None
        payload = _json_object(row["payload"]) or {}
        verdict = str(payload.get("verdict") or "")
        metrics = payload.get("metrics")
        receipt = _receipt(
            job_id,
            str(trial["evaluator_sha256"]),
            str(payload.get("receipt_sha256") or ""),
            metrics if isinstance(metrics, dict) else {},
            qualified=verdict == "qualified",
        )
        settlement = Settlement(
            outcome="succeeded" if verdict == "qualified" else "failed",
            receipts=[receipt],
        )
        self.observe_count += 1
        self.last_observation = _observation_json(row)
        self.last_receipt = receipt
        self.last_settlement = settlement
        return settlement

    def run(self, worker: Any, job: dict[str, Any]) -> Settlement:
        # An observation is immutable and keyed by source_ref, so a second
        # execution must reuse it rather than record a conflicting one.
        existing = self.observe(worker, job)
        if existing is not None:
            return existing

        store, workspace_id, actor_id = _worker_scope(worker)
        job_id = str(job["job_id"])
        trial = self._trial_row(store, workspace_id, actor_id, job)
        if trial is None:
            return self._failed()
        trial_id = UUID(str(trial["trial_id"]))
        campaign_id = UUID(str(trial["campaign_id"]))
        candidate_digest = str(trial["candidate_digest"])
        evaluator_sha256 = str(trial["evaluator_sha256"])
        descriptor = self._component_descriptor(
            store, workspace_id, actor_id, evaluator_sha256
        )
        if descriptor is None or descriptor.get("kind") != "evaluator":
            return self._failed()
        evaluator_artifact = str(descriptor.get("artifact_sha256") or "")
        spec = _json_object(trial["spec"]) or {}
        protocol_sha256 = spec.get("evaluation_protocol_sha256")
        if not isinstance(protocol_sha256, str) or not protocol_sha256:
            return self._failed()
        protocol_bytes = self._read_content(protocol_sha256)
        candidate_tar = self._read_content(candidate_digest)
        evaluator_script = self._read_content(evaluator_artifact)
        if protocol_bytes is None or candidate_tar is None or evaluator_script is None:
            return self._failed()
        try:
            protocol = load_protocol(protocol_bytes, expected_sha256=protocol_sha256)
        except ValueError:
            return self._failed()
        confirmation: bytes | None = None
        if protocol.confirmation_sha256 is not None:
            confirmation = self._read_content(protocol.confirmation_sha256)
            if confirmation is None:
                return self._failed()

        self.run_count += 1
        evaluation = evaluate_candidate(
            self.backend,
            protocol,
            candidate_tar=candidate_tar,
            candidate_digest=candidate_digest,
            evaluator_script=evaluator_script,
            confirmation=confirmation,
            cancel=worker.cancel_requested,
            scratch_dir=self.scratch_dir,
        )
        self.last_evaluation = evaluation
        # Cancel is honored at any step: a cancel that arrives after the last
        # backend run still records no receipt and no observation.
        if evaluation.canceled or worker.cancel_requested.is_set():
            return self._failed()

        verdict = str(evaluation.verdict)
        receipt_model = TrialReceipt(
            trial_id=str(trial_id),
            campaign_id=str(campaign_id),
            job_id=job_id,
            candidate_digest=candidate_digest,
            evaluator_component_sha256=evaluator_sha256,
            evaluator_artifact_sha256=evaluator_artifact,
            protocol_sha256=protocol_sha256,
            confirmation_sha256=protocol.confirmation_sha256,
            candidate_manifest=evaluation.candidate_manifest,
            evaluator_manifest=evaluation.evaluator_manifest,
            output_sha256=evaluation.output_sha256,
            negative_control=evaluation.negative_control,
            metrics=evaluation.metrics,
            verdict=verdict,
            failure_reason=evaluation.failure_reason,
        )
        receipt_bytes = receipt_model.receipt_bytes()
        receipt_sha256 = bytes_sha256(receipt_bytes).hexdigest()
        self._register_receipt(
            workspace_id=workspace_id,
            job_id=job_id,
            receipt_bytes=receipt_bytes,
            receipt_sha256=receipt_sha256,
        )
        payload: dict[str, object] = {
            "receipt_sha256": receipt_sha256,
            "verdict": verdict,
            "metrics": evaluation.metrics,
        }
        self._record_observation(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            trial_id=trial_id,
            job_id=job_id,
            execution_status=_execution_status(evaluation),
            payload=payload,
            payload_sha256=sha256(payload),
        )
        receipt = _receipt(
            job_id,
            evaluator_sha256,
            receipt_sha256,
            evaluation.metrics,
            qualified=verdict == "qualified",
        )
        settlement = Settlement(
            outcome="succeeded" if verdict == "qualified" else "failed",
            receipts=[receipt],
        )
        self.last_receipt = receipt
        self.last_settlement = settlement
        return settlement

    def _failed(self) -> Settlement:
        settlement = Settlement(outcome="failed", receipts=[])
        self.last_settlement = settlement
        return settlement

    def _trial_row(
        self,
        store: NativeJobStore,
        workspace_id: UUID,
        actor_id: UUID,
        job: dict[str, Any],
    ) -> dict[str, Any] | None:
        trial_id = job.get("trial_id")
        if trial_id is None:
            return None
        with store.transaction(workspace_id, actor_id) as cur:
            cur.execute(_TRIAL_QUERY, (workspace_id, UUID(str(trial_id))))
            return cur.fetchone()

    def _component_descriptor(
        self,
        store: NativeJobStore,
        workspace_id: UUID,
        actor_id: UUID,
        component_sha256: str,
    ) -> dict[str, Any] | None:
        with store.transaction(workspace_id, actor_id) as cur:
            cur.execute(
                "SELECT descriptor FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
                (workspace_id, component_sha256),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _json_object(row["descriptor"])

    def _observation_row(
        self,
        store: NativeJobStore,
        workspace_id: UUID,
        actor_id: UUID,
        job_id: str,
    ) -> dict[str, Any] | None:
        with store.transaction(workspace_id, actor_id) as cur:
            cur.execute(
                """
                SELECT observation_id, workspace_id, campaign_id, trial_id,
                       issuer_kind, source_ref, execution_status, commit_sha,
                       payload, payload_sha256, observed_at, recorded_at
                FROM omp_research.observations
                WHERE workspace_id=%s AND issuer_kind=%s AND source_ref=%s
                """,
                (workspace_id, ISSUER_KIND, f"job:{job_id}"),
            )
            return cur.fetchone()

    def _read_content(self, digest: str) -> bytes | None:
        """Bytes only when the file exists and hashes to its content digest."""
        if not digest:
            return None
        try:
            data = (self.content_dir / digest).read_bytes()
        except OSError:
            return None
        if bytes_sha256(data).hexdigest() != digest:
            return None
        return data

    def _register_receipt(
        self,
        *,
        workspace_id: UUID,
        job_id: str,
        receipt_bytes: bytes,
        receipt_sha256: str,
    ) -> None:
        if self.execute is None:
            raise RuntimeError("managed trial handler has no WorkService execute")
        manifest = ResearchArtifactManifest(
            contract_version="research-artifact.v1",
            artifact_sha256=receipt_sha256,
            size_bytes=len(receipt_bytes),
            media_type="application/json",
            name=f"managed-trial-receipt-{job_id}"[:200],
            access_class="workspace",
            issuer_kind=ISSUER_KIND,
            source_ref=f"job:{job_id}",
            valid_until=None,
        )
        manifest_sha256 = sha256(manifest.model_dump(mode="json"))
        envelope = CommandEnvelope(
            api_version=API_VERSION,
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RegisterResearchArtifactCommand(
                type="register_research_artifact",
                payload=RegisterResearchArtifactPayload(
                    manifest_sha256=manifest_sha256,
                    manifest=manifest,
                    content_base64=base64.b64encode(receipt_bytes).decode("ascii"),
                ),
            ),
        )
        response = self.execute(envelope)
        if not isinstance(response.result, RegisterResearchArtifactResult):
            raise TypeError(
                "WorkService returned an unexpected result for register_research_artifact"
            )

    def _record_observation(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        trial_id: UUID,
        job_id: str,
        execution_status: str,
        payload: dict[str, object],
        payload_sha256: str,
    ) -> None:
        if self.execute is None:
            raise RuntimeError("managed trial handler has no WorkService execute")
        envelope = CommandEnvelope(
            api_version=API_VERSION,
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordResearchObservationCommand(
                type="record_research_observation",
                payload=RecordResearchObservationPayload(
                    observation_id=uuid5(NAMESPACE_URL, f"omp-job:{job_id}"),
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    issuer_kind=ISSUER_KIND,
                    source_ref=f"job:{job_id}",
                    execution_status=execution_status,
                    payload=payload,
                    payload_sha256=payload_sha256,
                    observed_at=datetime.now(UTC),
                ),
            ),
        )
        response = self.execute(envelope)
        result = response.result
        if not isinstance(result, RecordResearchObservationResult):
            raise TypeError(
                "WorkService returned an unexpected result for record_research_observation"
            )
        self.last_observation = result.observation.model_dump(mode="json")


def _execution_status(evaluation: Evaluation) -> str:
    if evaluation.verdict == "qualified":
        return "completed"
    if evaluation.failure_reason == "candidate_timed_out":
        return "timed_out"
    return "crashed"


def _worker_scope(worker: Any) -> tuple[NativeJobStore, UUID, UUID]:
    return worker.store, worker.workspace_id, worker.actor_id


def _receipt(
    job_id: str,
    issuer_component_sha256: str,
    content_digest: str,
    metrics: dict[str, object],
    *,
    qualified: bool,
) -> dict[str, object]:
    return {
        "role": "evaluator",
        "issuer_component_sha256": issuer_component_sha256,
        "evidence": {
            "id": f"ev-managed:{job_id}",
            "kind": "receipt",
            "content_digest": content_digest,
            "trust": "trusted" if qualified else "untrusted",
            "metrics": dict(metrics),
        },
    }


def _observation_json(row: dict[str, Any]) -> dict[str, object]:
    observed_at = row["observed_at"]
    trial_id = row["trial_id"]
    return {
        "observation_id": str(row["observation_id"]),
        "campaign_id": str(row["campaign_id"]),
        "trial_id": None if trial_id is None else str(trial_id),
        "issuer_kind": str(row["issuer_kind"]),
        "source_ref": str(row["source_ref"]),
        "execution_status": str(row["execution_status"]),
        "payload": _json_object(row["payload"]) or {},
        "payload_sha256": str(row["payload_sha256"]),
        "observed_at": observed_at.isoformat()
        if isinstance(observed_at, datetime)
        else str(observed_at),
    }


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _check(args: argparse.Namespace) -> int:
    workspace_id = UUID(args.workspace)
    actor_id = UUID(args.actor)
    work_id = UUID(args.work_id)
    trial_id = UUID(args.trial_id)
    store = NativeJobStore(OperationsConfig.defaults())
    handler = factory(
        content_dir=args.content_dir,
        work_url=args.work_url,
        bearer_file=args.bearer_file,
        workspace_id=str(workspace_id),
    )
    dispatch_managed_trial(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
        work_id=work_id,
        trial_id=trial_id,
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=args.lease_seconds,
    )
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=workspace_id,
            actor_id=actor_id,
            worker_id=args.worker_id or f"managed-trial:{trial_id}",
            component_sha256=args.component,
            capabilities=[CAPABILITY],
            capacity=1,
        ),
        handler,
    )
    worker.start()
    worker.tick()
    settlement = handler.last_settlement
    print(
        json.dumps(
            {
                "capability": CAPABILITY,
                "passed": bool(
                    settlement is not None and settlement.outcome == "succeeded"
                ),
                "observation": handler.last_observation,
                "receipt": handler.last_receipt,
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_work.jobs.managed_trial")
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "check", help="dispatch one managed trial job and tick the worker to settlement"
    )
    check.add_argument("--workspace", required=True)
    check.add_argument("--actor", required=True)
    check.add_argument("--work-id", required=True)
    check.add_argument("--trial-id", required=True)
    check.add_argument("--content-dir", required=True)
    check.add_argument("--bearer-file", required=True)
    check.add_argument("--component", required=True)
    check.add_argument("--work-url", default="http://127.0.0.1:54321")
    check.add_argument("--worker-id", default=None)
    check.add_argument("--lease-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command != "check":
        parser.error(f"unknown command {args.command}")
        return 2
    return _check(args)


if __name__ == "__main__":
    raise SystemExit(main())
