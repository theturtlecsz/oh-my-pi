"""Read one settled trial's completion from custody-checked evaluator evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from uuid import UUID

from omp_work.jobs.research_trial import trial_job_id
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.client import WorkClient
from omp_work.v1.models import ResearchTrial

__all__ = ["TrialCompletion", "read_trial_completion"]

_ISSUER_KIND = "legacy_autoresearch"
_COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class TrialCompletion:
    """Receipt-derived result of one trial.

    ``value`` is the selection metric, or ``None`` with ``infra_error`` set when
    the execution did not complete or the metric is missing or not finite.
    """

    execution_status: str
    metrics: dict[str, object] = field(default_factory=dict)
    value: float | None = None
    infra_error: str | None = None


def read_trial_completion(
    client: WorkClient,
    store: NativeJobStore,
    key: str,
    campaign_id: UUID,
    trial: ResearchTrial,
    actor_id: UUID,
    metric: str,
) -> TrialCompletion:
    """Return metrics only from the trial's one verified evaluator receipt.

    Refuses unless the trial's job is settled, exactly one trusted-issuer
    observation carries the job's source_ref, and exactly one evaluator receipt
    is issued by the trial's evaluator, verified, and bound to that observation's
    payload digest. Observation payload metrics are never read.
    """
    if trial.campaign_id != campaign_id:
        raise RuntimeError(f"trial {trial.trial_id} does not belong to campaign {campaign_id}")
    job_id = trial_job_id(trial.trial_id)
    with store.transaction(trial.workspace_id, actor_id) as cur:
        job = store.job_view(cur, trial.workspace_id, job_id)
    if job is None or job["status"] not in {"sealed", "failed"} or not job["settlement"]:
        raise RuntimeError(f"research job {job_id} is unsettled")
    observations = [
        observation for observation in client.research(key).observations
        if observation.campaign_id == campaign_id
        and observation.trial_id == trial.trial_id
        and observation.source_ref == f"job:{job_id}"
        and observation.issuer_kind == _ISSUER_KIND
    ]
    if len(observations) != 1:
        raise RuntimeError(
            f"research job {job_id} has no unique matching persisted observation"
        )
    observation = observations[0]
    receipts = [
        receipt for receipt in job["settlement"].get("receipts") or []
        if isinstance(receipt, dict) and receipt.get("role") == "evaluator"
    ]
    if len(receipts) != 1:
        raise RuntimeError(f"research job {job_id} has no unique evaluator receipt")
    receipt = receipts[0]
    evidence = receipt.get("evidence")
    if (
        receipt.get("issuer_component_sha256") != trial.evaluator_sha256
        or not isinstance(evidence, dict)
        or evidence.get("trust") != "verified"
        or evidence.get("content_digest") != observation.payload_sha256
    ):
        raise RuntimeError(
            f"research job {job_id} evaluator receipt is not bound to its observation"
        )
    metrics = evidence.get("metrics")
    if not isinstance(metrics, dict):
        raise RuntimeError(f"research job {job_id} receipt has no metrics object")
    status = observation.execution_status
    if status != _COMPLETED:
        return TrialCompletion(
            status, metrics, None, f"execution status {status} is not completed",
        )
    value = metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return TrialCompletion(
            status, metrics, None, f"metric {metric} is missing or not a finite number",
        )
    return TrialCompletion(status, metrics, float(value), None)
