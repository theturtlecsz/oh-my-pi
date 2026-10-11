"""Research capacity gate in front of native compute enqueue (OMP-318).

``ResearchCapacity`` remembers each worker after ``register_worker`` succeeds.
``enqueue`` admits a compute job only when a remembered worker holds every
required capability and at least one of those workers still covers the job
weight after ``engineering_headroom`` is held back for ordinary work. A miss
raises ``CapacityRefused`` and does not call ``enqueue_job``.

``claim``, ``cancel``, and ``reclaim`` are the assignment step: thin passes to
``claim_job``, ``cancel_job``, and ``reconcile_jobs`` on the same store and
workspace/actor identity. ``claim`` leases with no per-resource limits and
returns the admitted job (with ``worker_id`` and ``fence``) or ``job=None``.
All three return the native result dict unchanged and propagate ``JobError``.
"""

from __future__ import annotations

from uuid import UUID

from omp_work.jobs import (
    JobError,
    claim_job,
    enqueue_job,
    reconcile_jobs,
    register_worker,
)
from omp_work.jobs.cancel import cancel_job

__all__ = ["CapacityRefused", "ResearchCapacity"]

_RESOURCE_KEYS = ("cpu", "memory_mib", "gpu", "model_calls")
_RESOURCE_ERROR = (
    "resources must be cpu, memory_mib, gpu, and model_calls integers >= 0",
)


class CapacityRefused(Exception):
    """Enqueue refused before a job row is written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ResearchCapacity:
    """Registered workers and the engineering reserve for one workspace."""

    def __init__(
        self,
        store: object,
        workspace_id: UUID,
        actor_id: UUID,
        engineering_headroom: int = 1,
    ) -> None:
        self._store = store
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        self.engineering_headroom = engineering_headroom
        self._workers: dict[str, tuple[frozenset[str], int]] = {}

    def register(
        self,
        operation_id: str,
        worker_id: str,
        component_sha256: str,
        capabilities: list[str],
        capacity: int,
    ) -> dict[str, object]:
        """Register the worker, then remember it. A failed register is not remembered."""
        result = register_worker(
            self._store,
            operation_id=operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            worker_id=worker_id,
            component_sha256=component_sha256,
            capabilities=capabilities,
            capacity=capacity,
        )
        self._workers[worker_id] = (
            frozenset(str(capability) for capability in capabilities),
            int(capacity),
        )
        return result

    def enqueue(
        self,
        operation_id: str,
        job_id: str,
        work_id: UUID,
        required_capabilities: list[str],
        resources: dict[str, int],
        lease_seconds: int,
        trial_id: UUID | None = None,
        location: str | None = None,
    ) -> dict[str, object]:
        """Enqueue one compute job, or refuse without writing a job row.

        Weight is the sum of ``cpu``, ``memory_mib``, ``gpu``, and
        ``model_calls``. A supplied ``location`` is appended as
        ``location:<location>`` when that token is not already required.
        Eligible workers are the remembered ones that hold every capability.
        None of them is ``capability_mismatch``. When every eligible worker's
        ``capacity - engineering_headroom`` is below the weight, the refusal
        is ``engineering_headroom``. Otherwise the job is enqueued with
        ``kind="compute"`` and ``parent_job_id=None`` and no worker is chosen.
        """
        weight = _weight(resources)
        capabilities = list(required_capabilities)
        if location is not None:
            token = f"location:{location}"
            if token not in capabilities:
                capabilities.append(token)
        required = set(capabilities)
        eligible = [
            (held, capacity)
            for held, capacity in self._workers.values()
            if required <= held
        ]
        if not eligible:
            raise CapacityRefused("capability_mismatch")
        # Hold engineering_headroom weight units back on every worker. Refuse
        # only when none of the eligible workers still cover this job.
        if all(capacity - self.engineering_headroom < weight for _, capacity in eligible):
            raise CapacityRefused("engineering_headroom")
        return enqueue_job(
            self._store,
            operation_id=operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            job_id=job_id,
            work_id=work_id,
            kind="compute",
            required_capabilities=capabilities,
            resources=resources,
            lease_seconds=lease_seconds,
            parent_job_id=None,
            trial_id=trial_id,
        )


    def claim(self, operation_id: str, worker_id: str) -> dict[str, object]:
        """Lease the oldest matching backlog job to ``worker_id``.

        Thin pass to ``claim_job`` with ``resource_limits=None``: per-resource
        caps are not the gate's concern. The result is the native claim dict —
        the admitted job with its ``worker_id`` and ``fence``, or ``job=None``
        when nothing routable fits. ``JobError`` propagates.
        """
        return claim_job(
            self._store,
            operation_id=operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            worker_id=worker_id,
            resource_limits=None,
        )

    def cancel(
        self,
        operation_id: str,
        job_id: str,
        reason: str,
    ) -> dict[str, object]:
        """Cancel ``job_id`` and its non-terminal descendants for ``reason``.

        Thin pass to ``cancel_job``: the native cancel dict is returned
        unchanged and ``JobError`` propagates.
        """
        return cancel_job(
            self._store,
            operation_id=operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            job_id=job_id,
            reason=reason,
        )

    def reclaim(self, operation_id: str) -> dict[str, object]:
        """Reclaim this workspace's expired leases.

        Thin pass to ``reconcile_jobs``: the native reconcile dict is returned
        unchanged and ``JobError`` propagates.
        """
        return reconcile_jobs(
            self._store,
            operation_id=operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
        )


def _weight(resources: object) -> int:
    if not isinstance(resources, dict) or set(resources) != set(_RESOURCE_KEYS):
        raise JobError("invalid_request", _RESOURCE_ERROR)
    total = 0
    for key in _RESOURCE_KEYS:
        value = resources[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JobError("invalid_request", _RESOURCE_ERROR)
        total += value
    return total
