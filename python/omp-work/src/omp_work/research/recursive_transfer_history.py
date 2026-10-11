"""Reserve one shared confirmation holdout on the caller's history.

``reserve_task`` and ``finish_arm`` mutate ``native.history`` in place. The
holdout ``(content_sha256, task_id)`` is noted once and shared by both arms.
Each arm consumes its own trial, job, receipt, and output. A new unqualified
arm is a failure and adds one to the denominator. A new qualified arm is only
an attempt until ``finish_arm``. A replay of the same holdout, trial, job,
receipt, or output is a duplicate and adds nothing, even when the trial or
dataset label is new. A receipt or output already in ``artifact_cache`` is not
an independent replicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from omp_work.research import custody
from omp_work.research.recursive_transfer_evidence import NativeEvidence, TrialEvidence
from omp_work.research.recursive_transfer_freshness import PreparedStep, PreparedTask

__all__ = [
    "ArmFinish",
    "Reservation",
    "finish_arm",
    "reserve_task",
]


def _identities(arm: TrialEvidence) -> tuple[tuple[str, object], ...]:
    output = arm.output_sha256 if arm.output_sha256 is not None else arm.output_id
    pairs = (
        ("trial", arm.trial_id),
        ("job", arm.job_id),
        ("receipt", arm.receipt_sha256 or arm.receipt_id),
        ("output", output),
    )
    return tuple((kind, value) for kind, value in pairs if value)


@dataclass(frozen=True)
class Reservation:
    """One task reservation. ``denominator`` and ``replicates`` are increments."""

    denominator: int
    replicates: int
    baseline_trial_id: UUID | str | None
    candidate_trial_id: UUID | str | None
    reasons: tuple[str, ...]
    steps: tuple[PreparedStep, ...]


@dataclass(frozen=True)
class ArmFinish:
    """Denominator increment for finishing one trial."""

    denominator: int
    trial_id: str


def _cached(native: NativeEvidence, digests: set[str]) -> set[str]:
    if not digests:
        return set()
    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute(
            """
            SELECT artifact_sha256 FROM omp_research.artifact_cache
            WHERE workspace_id=%s AND artifact_sha256 = ANY(%s)
            """,
            (native.workspace_id, list(digests)),
        )
        return {str(row["artifact_sha256"]) for row in cur.fetchall()}


def reserve_task(native: NativeEvidence, prepared: PreparedTask) -> Reservation:
    """Claim ``prepared`` on the existing history. Never replaces ``native.history``."""
    history = native.history
    reasons = list(prepared.reasons)
    denominator = 0
    replicates = 0

    holdout = (prepared.content_sha256, prepared.task_id)
    holdout_duplicate = False
    if prepared.content_sha256 and prepared.task_id:
        if holdout in history.consumed:
            history.record_duplicate(holdout)
            holdout_duplicate = True
            reasons.append("duplicate_holdout")
        else:
            history.note_confirmation(holdout)

    arms: list[TrialEvidence] = []
    digests: set[str] = set()
    for step in prepared.steps:
        for arm in (step.baseline, step.candidate):
            if arm is None:
                continue
            arms.append(arm)
            for kind, value in _identities(arm):
                if kind in {"receipt", "output"} and isinstance(value, str):
                    digests.add(value)
    cached = _cached(native, digests)

    for arm in arms:
        identities = _identities(arm)
        collisions = [(kind, value) for kind, value in identities if value in history.consumed]
        artifact_cached = any(
            kind in {"receipt", "output"} and isinstance(value, str) and value in cached
            for kind, value in identities
        )
        if artifact_cached:
            # Cached bytes cannot be counted as a new replicate. Keep the
            # identities failed so a later finish_arm adds nothing either.
            refusal = custody.replicate_masquerade(cached=True)
            if refusal:
                reasons.append(refusal)
        if holdout_duplicate or collisions or artifact_cached:
            for kind, value in collisions:
                history.record_duplicate(value)
                reasons.append(f"duplicate_{kind}")
            if artifact_cached:
                for _kind, value in identities:
                    if value not in history.consumed:
                        history.record_failure(value)
            continue
        method = history.record_attempt if arm.qualified else history.record_failure
        for _kind, value in identities:
            method(value)
        replicates += 1
        if not arm.qualified:
            denominator += 1

    first = prepared.steps[0] if prepared.steps else None
    return Reservation(
        denominator,
        replicates,
        None if first is None else first.baseline_trial_id,
        None if first is None else first.candidate_trial_id,
        tuple(dict.fromkeys(reasons)),
        prepared.steps,
    )


def _settled(history, trial_id: object) -> bool:
    key = str(trial_id)
    snap = history.snapshot()
    return key in snap["successes"] or key in snap["failures"]


def finish_arm(native: NativeEvidence, trial_id: object, *, ok: bool) -> ArmFinish:
    """Record the outcome of one reserved trial. The first settlement adds one."""
    history = native.history
    key = str(trial_id)
    if _settled(history, trial_id):
        return ArmFinish(0, key)
    if ok:
        history.record_success(trial_id)
    else:
        history.record_failure(trial_id)
    return ArmFinish(1, key)
