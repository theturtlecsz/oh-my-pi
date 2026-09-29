"""Effort gate at admission of policy-routed stage jobs.

The economy effort gate is enforced here, before any store access or enqueue:
a policy-routed stage job whose ``effort`` (and ``effort_reason``) fails
``validate_effort`` is refused with ``JobError("invalid_effort")`` and never
reaches ``enqueue_job``.
"""

from __future__ import annotations

from omp_work.economy_effort import validate_effort
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import JobError, NativeJobStore


def admit_stage_job(
    store: NativeJobStore,
    *,
    effort: object,
    effort_reason: object,
    **enqueue: object,
) -> dict[str, object]:
    """Refuse an invalid effort pair, else enqueue the stage job.

    The gate runs first, so an invalid effort raises
    ``JobError("invalid_effort", (message,))`` without reading the store or
    calling ``enqueue_job``. Every other keyword argument is passed to
    ``enqueue_job`` unchanged.
    """
    verdict = validate_effort(effort, effort_reason)
    if not verdict.get("ok"):
        raise JobError("invalid_effort", (verdict.get("message") or "effort validation failed",))
    return enqueue_job(store, **enqueue)
