"""Prepare byte-bound transfer cohorts without consuming confirmation history."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from pydantic import Field

from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_transfer_evidence import NativeEvidence, TrialEvidence, TrialRefused, read_trial
from omp_work.v1.canonical import sha256
from omp_work.v1.models import StrictModel, hex64


@dataclass(frozen=True)
class StepArms:
    step_id: str
    baseline_trial_id: UUID | str | None = None
    candidate_trial_id: UUID | str | None = None


class CohortTask(StrictModel):
    phase: Literal["development", "confirmation"]
    domain: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    direction: Literal["higher", "lower"]
    protocol_sha256: hex64
    seed: int
    width: int = Field(gt=0)
    budget: int = Field(ge=0)
    dataset_id: hex64
    steps: tuple[StepArms, ...]


@dataclass(frozen=True)
class PreparedStep:
    step_id: str
    baseline_trial_id: UUID | str | None
    candidate_trial_id: UUID | str | None
    baseline: TrialEvidence | None
    candidate: TrialEvidence | None


@dataclass(frozen=True)
class PreparedTask:
    eligible: bool
    reasons: tuple[str, ...]
    dataset_id: str
    task_id: str | None
    content_sha256: str | None
    steps: tuple[PreparedStep, ...]


class _Dataset(StrictModel):
    task_id: str = Field(min_length=1)
    payload: object


def _dataset(native: NativeEvidence, digest: str) -> _Dataset:
    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute(
            "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
            (native.workspace_id, digest),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("missing_dataset")
    manifest = row["manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    if (
        not isinstance(manifest, dict)
        or manifest.get("artifact_sha256") != digest
        or type(manifest.get("size_bytes")) is not int
        or manifest["size_bytes"] < 0
    ):
        raise ValueError("dataset_manifest_mismatch")
    raw = read_verified_bytes(
        artifact_path(native.data_dir, native.workspace_id, digest), digest, manifest["size_bytes"],
    )
    # Reject non-finite values rather than canonicalizing invalid JSON identities.
    return _Dataset.model_validate(json.loads(raw, parse_constant=_reject_constant))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _trace_hashes(payload: object) -> set[str]:
    if not isinstance(payload, dict) or "traces" not in payload:
        return set()
    traces = payload["traces"]
    values = traces.values() if isinstance(traces, dict) else traces if isinstance(traces, list) else [traces]
    return {sha256(traces), *(sha256(trace) for trace in values)}


def _prepare(task: CohortTask, native: NativeEvidence) -> tuple[PreparedTask, set[str]]:
    reasons: list[str] = []
    task_id = content = None
    traces: set[str] = set()
    try:
        dataset = _dataset(native, task.dataset_id)
        task_id, content = dataset.task_id, sha256(dataset.payload)
        traces = _trace_hashes(dataset.payload)
    except (ValueError, RuntimeError, OSError) as error:
        reasons.append(str(error))

    steps: list[PreparedStep] = []
    if not task.steps:
        reasons.append("missing_steps")
    for step in task.steps:
        baseline = candidate = None
        if not step.baseline_trial_id or not step.candidate_trial_id:
            reasons.append("missing_trial_arm")
        elif str(step.baseline_trial_id) == str(step.candidate_trial_id):
            reasons.append("equal_trial_arms")
        else:
            arms: list[TrialEvidence | None] = []
            for trial_id in (step.baseline_trial_id, step.candidate_trial_id):
                try:
                    arm = read_trial(native, trial_id)
                    arms.append(arm)
                    if not arm.qualified:
                        reasons.extend(arm.reasons)
                    if arm.receipt.protocol_sha256 != task.protocol_sha256:
                        reasons.append("protocol_mismatch")
                    if not any(m.name == task.metric and m.direction == task.direction for m in arm.protocol.metrics):
                        reasons.append("metric_mismatch")
                except (TrialRefused, ValueError) as error:
                    arms.append(None)
                    reasons.append(str(error))
            baseline, candidate = arms
            if baseline is not None and candidate is not None and baseline.trial_id == candidate.trial_id:
                reasons.append("equal_trial_arms")
        steps.append(PreparedStep(step.step_id, step.baseline_trial_id, step.candidate_trial_id, baseline, candidate))
    return PreparedTask(not reasons, tuple(dict.fromkeys(reasons)), task.dataset_id, task_id, content, tuple(steps)), traces


def prepare_cohorts(
    cohorts: Sequence[CohortTask], native_evidence: NativeEvidence, studies: Sequence[RecursiveStudy],
) -> tuple[PreparedTask, ...]:
    """Resolve both sealed arms and reject holdouts exposed anywhere in the input.

    All development identities are gathered before confirmation decisions, even
    from ineligible development tasks. Preparation never writes history.
    """
    prepared = [_prepare(task, native_evidence) for task in cohorts]
    excluded: set[object] = set(native_evidence.history.confirmation_identities())
    # History uses (content hash, task id) identities; either component is stale.
    for identity in tuple(excluded):
        if isinstance(identity, tuple):
            excluded.update(identity)
    for study in studies:
        excluded.add(study.dev_sha256)
        for evidence in study.dev:
            excluded.update((evidence.task_id, evidence.content_sha256))
    for cohort, (task, traces) in zip(cohorts, prepared):
        if cohort.phase == "development":
            excluded.update(x for x in (task.task_id, task.content_sha256) if x is not None)
            excluded.update(traces)
    counts = Counter(
        task.content_sha256 for cohort, (task, _) in zip(cohorts, prepared)
        if cohort.phase == "confirmation" and task.content_sha256 is not None
    )
    results: list[PreparedTask] = []
    for cohort, (task, traces) in zip(cohorts, prepared):
        reasons = list(task.reasons)
        if cohort.phase == "confirmation":
            for step in task.steps:
                for arm in (step.baseline, step.candidate):
                    if arm is not None and (
                        arm.protocol.confirmation_sha256 is None
                        or arm.receipt.confirmation_sha256 != arm.protocol.confirmation_sha256
                    ):
                        reasons.append("confirmation_mismatch")
            if any(x in excluded for x in (task.task_id, task.content_sha256)) or traces & excluded:
                reasons.append("confirmation_reused")
            if task.content_sha256 is not None and counts[task.content_sha256] > 1:
                reasons.append("duplicate_confirmation_content")
        results.append(replace(task, eligible=not reasons, reasons=tuple(dict.fromkeys(reasons))))
    return tuple(results)
