"""Trial binding on the s02 worker (R03, OMP-400).

``dispatch_trial`` enqueues the ``trial:<trial_id>`` native compute job with the
``research.trial`` capability. The handler runs the trial's evaluator harness in
a temporary working directory, ingests its ``METRIC name=value`` lines as a
``legacy_autoresearch`` observation through the WorkService, and returns the
evaluator receipt that seals the job. ``observe`` rebuilds the same settlement
from the stored observation when the lease expired before settlement.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, Settlement, WorkerConfig
from omp_work.operations.artifacts import bytes_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import CommandResponse, RecordResearchObservationResult
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    RecordResearchObservationCommand,
    RecordResearchObservationPayload,
)

__all__ = [
    "CAPABILITY",
    "DEFAULT_TIMEOUT_SECONDS",
    "HARNESS_FILENAME",
    "ResearchTrialHandler",
    "dispatch_trial",
    "factory",
    "main",
    "parse_metric_lines",
    "trial_job_id",
]

API_VERSION = "work.omp.dev/v1"
CAPABILITY = "research.trial"
ISSUER_KIND = "legacy_autoresearch"
HARNESS_FILENAME = "autoresearch.sh"
HARNESS_COMMAND = ("bash", HARNESS_FILENAME)
DEFAULT_TIMEOUT_SECONDS = 600
_METRIC_LINE = re.compile(r"^METRIC\s+([\w.µ-]+)=(\S+)\s*$", re.MULTILINE)
_DENIED_METRIC_KEYS = frozenset({"__proto__", "constructor", "prototype"})
_TRIAL_QUERY = """
SELECT t.trial_id, t.campaign_id, t.work_id, t.evaluator_sha256, c.spec
FROM omp_research.trials t
JOIN omp_research.campaigns c
  ON c.workspace_id = t.workspace_id AND c.campaign_id = t.campaign_id
WHERE t.workspace_id=%s AND t.trial_id=%s
"""


def trial_job_id(trial_id: UUID | str) -> str:
    return f"trial:{trial_id}"


def parse_metric_lines(output: str) -> dict[str, float]:
    """Collect finite ``METRIC name=value`` numbers, skipping denied key names."""
    metrics: dict[str, float] = {}
    for match in _METRIC_LINE.finditer(output):
        name = match.group(1)
        if name in _DENIED_METRIC_KEYS:
            continue
        try:
            value = float(match.group(2))
        except ValueError:
            continue
        if math.isfinite(value):
            metrics[name] = value
    return metrics


def dispatch_trial(
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
    """Enqueue the compute job that evaluates one proposed trial."""
    return enqueue_job(
        store,
        operation_id=operation_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=trial_job_id(trial_id),
        work_id=work_id,
        kind="compute",
        required_capabilities=[CAPABILITY],
        resources=resources,
        lease_seconds=lease_seconds,
        trial_id=trial_id,
    )


def factory(
    options: dict[str, Any] | None = None, **overrides: Any
) -> ResearchTrialHandler:
    """Build the handler from worker options; ``execute`` overrides the client.

    ``load_handler`` calls this as ``factory(**options)``; the same options may
    also be passed as a single mapping.
    """
    merged: dict[str, Any] = dict(options or {})
    merged.update(overrides)
    options = merged
    harness_dir = options.get("harness_dir")
    if harness_dir is None:
        raise ValueError("harness_dir is required")
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
    return ResearchTrialHandler(
        harness_dir=Path(str(harness_dir)),
        execute=execute,
        default_timeout=int(options.get("default_timeout", DEFAULT_TIMEOUT_SECONDS)),
    )


class ResearchTrialHandler:
    """Runs one trial's evaluator harness and records it as an observation."""

    def __init__(
        self,
        *,
        harness_dir: Path,
        execute: Callable[[CommandEnvelope], CommandResponse] | None = None,
        default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.harness_dir = Path(harness_dir)
        self.execute = execute
        self.default_timeout = int(default_timeout)
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
        metrics = payload.get("metrics")
        receipt = _receipt(
            job_id,
            str(trial["evaluator_sha256"]),
            str(row["payload_sha256"]),
            metrics if isinstance(metrics, dict) else {},
        )
        outcome = (
            "succeeded" if str(row["execution_status"]) == "completed" else "failed"
        )
        settlement = Settlement(outcome=outcome, receipts=[receipt])
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
        trial_id = UUID(str(job["trial_id"]))
        trial = self._trial_row(store, workspace_id, actor_id, job)
        if trial is None:
            return self._failed()
        descriptor = self._evaluator_descriptor(
            store, workspace_id, actor_id, str(trial["evaluator_sha256"])
        )
        if descriptor is None:
            return self._failed()
        artifact_sha256 = str(descriptor.get("artifact_sha256"))
        harness = self._read_harness(artifact_sha256)
        if harness is None:
            return self._failed()

        self.run_count += 1
        timeout = _campaign_timeout(trial["spec"], self.default_timeout)
        exit_code, output, duration, timed_out = self._execute_harness(harness, timeout)
        metrics = parse_metric_lines(output)
        if timed_out:
            execution_status = "timed_out"
        elif exit_code == 0:
            execution_status = "completed"
        else:
            execution_status = "crashed"
        payload: dict[str, object] = {
            "metrics": metrics,
            "exit_code": exit_code,
            "duration_seconds": duration,
            "harness_sha256": artifact_sha256,
        }
        payload_sha256 = sha256(payload)
        self._record_observation(
            workspace_id=workspace_id,
            campaign_id=UUID(str(trial["campaign_id"])),
            trial_id=trial_id,
            job_id=job_id,
            execution_status=execution_status,
            payload=payload,
            payload_sha256=payload_sha256,
        )
        receipt = _receipt(
            job_id, str(trial["evaluator_sha256"]), payload_sha256, metrics
        )
        settlement = Settlement(
            outcome="succeeded" if execution_status == "completed" else "failed",
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

    def _evaluator_descriptor(
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

    def _read_harness(self, artifact_sha256: str) -> bytes | None:
        """Harness bytes only when the file exists and hashes to its artifact digest."""
        try:
            data = (self.harness_dir / artifact_sha256).read_bytes()
        except OSError:
            return None
        if bytes_sha256(data).hexdigest() != artifact_sha256:
            return None
        return data

    def _execute_harness(
        self, harness: bytes, timeout: int
    ) -> tuple[int | None, str, float, bool]:
        with tempfile.TemporaryDirectory(prefix="omp-research-trial-") as workdir:
            (Path(workdir) / HARNESS_FILENAME).write_bytes(harness)
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    list(HARNESS_COMMAND),
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as expired:
                duration = time.monotonic() - started
                return (
                    None,
                    _decoded(expired.stdout) + _decoded(expired.stderr),
                    duration,
                    True,
                )
            duration = time.monotonic() - started
            output = (completed.stdout or "") + (completed.stderr or "")
            return completed.returncode, output, duration, False

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
            raise RuntimeError("research trial handler has no WorkService execute")
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


def _worker_scope(worker: Any) -> tuple[NativeJobStore, UUID, UUID]:
    return worker.store, worker.workspace_id, worker.actor_id


def _receipt(
    job_id: str,
    issuer_component_sha256: str,
    content_digest: str,
    metrics: dict[str, object],
) -> dict[str, object]:
    return {
        "role": "evaluator",
        "issuer_component_sha256": issuer_component_sha256,
        "evidence": {
            "id": f"ev-trial:{job_id}",
            "kind": "receipt",
            "content_digest": content_digest,
            "trust": "verified",
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


def _campaign_timeout(spec: object, default: int) -> int:
    spec_object = _json_object(spec)
    vector = _json_object(spec_object.get("resource_vector")) if spec_object else None
    value = vector.get("max_wall_seconds") if vector else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


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


def _decoded(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _check(args: argparse.Namespace) -> int:
    workspace_id = UUID(args.workspace)
    actor_id = UUID(args.actor)
    work_id = UUID(args.work_id)
    trial_id = UUID(args.trial_id)
    store = NativeJobStore(OperationsConfig.defaults())
    handler = factory(
        harness_dir=args.harness_dir,
        work_url=args.work_url,
        bearer_file=args.bearer_file,
        workspace_id=str(workspace_id),
        default_timeout=args.timeout,
    )
    dispatch_trial(
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
            worker_id=args.worker_id or f"research-trial:{trial_id}",
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
                "capability": "autoresearch",
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
    parser = argparse.ArgumentParser(prog="python -m omp_work.jobs.research_trial")
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "check", help="dispatch one trial job and tick the worker to settlement"
    )
    check.add_argument("--workspace", required=True)
    check.add_argument("--actor", required=True)
    check.add_argument("--work-id", required=True)
    check.add_argument("--trial-id", required=True)
    check.add_argument("--harness-dir", required=True)
    check.add_argument("--bearer-file", required=True)
    check.add_argument("--component", required=True)
    check.add_argument("--work-url", default="http://127.0.0.1:54321")
    check.add_argument("--worker-id", default=None)
    check.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    check.add_argument("--lease-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command != "check":
        parser.error(f"unknown command {args.command}")
        return 2
    return _check(args)


if __name__ == "__main__":
    raise SystemExit(main())
