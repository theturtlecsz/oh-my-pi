"""OMP-417-s05: a stop freezes every native lease until release.

PostgreSQL integration (``OMP_WORK_POSTGRES_INTEGRATION=1``). While the stop
is engaged, reconcile writes nothing, so a lease outlives its expiry with the
same worker, fence, and status. After release the same worker is resumed
once (``lease_resumed``, then ``resume_claimed``). A holder that is no longer
active is ``lease_recovered`` back to the backlog, and only then can another
worker claim it. A lease that expired before the stop is reclaimed as
``lease_expired``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.store import JobError, drain_worker
from omp_work.jobs.worker import Frozen, JobWorker, Settlement, WorkerConfig
from test_jobs_worker_loop import (
    _connect,
    _connect_admin,
    _enqueue,
    _receipt,
    _store,
)
from test_stop_pause_resume import (
    _engage,
    _job_event_kinds,
    _job_row,
    _release,
    _reservation_reason,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


class _ObserveSettles:
    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt
        self.observe_count = 0
        self.run_count = 0

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.observe_count += 1
        return Settlement(outcome="succeeded", receipts=[self.receipt])

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.run_count += 1
        return Settlement(outcome="succeeded", receipts=[self.receipt])


class _RaisesFrozen:
    def observe(self, ctx: Any, job: dict[str, Any]) -> None:
        return None

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        raise Frozen("stop engaged")


def _worker(native_jobs, store, handler, worker_id: str) -> JobWorker:
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=worker_id,
            component_sha256=native_jobs.components["worker"],
            capabilities=["compute.cpu", "compute.gpu"],
            capacity=4,
        ),
        handler,
    )
    worker.start()
    return worker


def _claim(native_jobs, store, worker_id: str) -> tuple[str, int]:
    job_id = f"job-frozen-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=30)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    return job_id, int(claimed["job"]["fence"])


def _expire_during_stop(native_jobs, workspace_id: UUID, job_id: str) -> None:
    """Put the lease after engage and before now, so it expired during the stop."""
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            """
            UPDATE omp_jobs.jobs AS j
            SET lease_expires_at = GREATEST(
                e.occurred_at + interval '1 millisecond',
                clock_timestamp() - interval '1 millisecond'
            )
            FROM (
                SELECT occurred_at FROM omp_audit.domain_events
                WHERE workspace_id=%s AND aggregate_id=%s
                  AND event_type='engage_stop' AND outcome='applied'
                ORDER BY sequence DESC LIMIT 1
            ) AS e
            WHERE j.job_id=%s
            """,
            (workspace_id, workspace_id, job_id),
        )


def _event_payloads(native_jobs, job_id: str, kind: str) -> list[dict[str, Any]]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT payload FROM omp_jobs.job_events WHERE job_id=%s AND kind=%s ORDER BY seq",
            (job_id, kind),
        ).fetchall()
    return [row["payload"] for row in rows]


def _reconcile(native_jobs, store) -> dict[str, object]:
    return reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )


def test_frozen_lease_resumes_once_for_the_same_worker(native_jobs) -> None:
    service = native_jobs.service
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    handler = _ObserveSettles(_receipt(native_jobs))
    worker_id = f"worker-resume-{uuid4()}"
    worker = _worker(native_jobs, store, handler, worker_id)
    job_id, fence = _claim(native_jobs, store, worker_id)

    _engage(service, workspace_id, uuid4())
    try:
        _expire_during_stop(native_jobs, workspace_id, job_id)
        before = _job_row(native_jobs, job_id)
        assert before["lease_expires_at"] < datetime.now(UTC)

        assert _reconcile(native_jobs, store)["job_ids"] == []
        assert worker.tick() is None
        after = _job_row(native_jobs, job_id)
        assert after["status"] == before["status"] == "admitted"
        assert after["worker_id"] == before["worker_id"] == worker_id
        assert int(after["fence"]) == int(before["fence"]) == fence
        assert after["lease_expires_at"] == before["lease_expires_at"]
        assert _job_event_kinds(native_jobs, job_id) == ["enqueued", "claimed"]
        assert _reservation_reason(native_jobs, job_id) is None

        _release(service, workspace_id)
        first = _reconcile(native_jobs, store)
        assert first["job_ids"] == [job_id]
        resumed = _job_row(native_jobs, job_id)
        assert resumed["status"] == "admitted"
        assert resumed["worker_id"] == worker_id
        assert int(resumed["fence"]) == fence
        assert resumed["lease_expires_at"] > datetime.now(UTC)
        payloads = _event_payloads(native_jobs, job_id, "lease_resumed")
        assert len(payloads) == 1
        assert int(payloads[0]["fence"]) == fence
        assert payloads[0]["worker_id"] == worker_id
        with _connect_admin(native_jobs) as conn:
            release = conn.execute(
                """
                SELECT event_id::text AS event_id FROM omp_audit.domain_events
                WHERE workspace_id=%s AND event_type='release_stop' AND outcome='applied'
                ORDER BY sequence DESC LIMIT 1
                """,
                (workspace_id,),
            ).fetchone()
        assert payloads[0]["release_event_id"] == release["event_id"]

        second = _reconcile(native_jobs, store)
        assert job_id not in second["job_ids"]
        assert len(_event_payloads(native_jobs, job_id, "lease_resumed")) == 1

        assert worker.tick() == job_id
        assert handler.observe_count == 1
        assert handler.run_count == 0
        assert _job_event_kinds(native_jobs, job_id).count("resume_claimed") == 1
        assert _job_row(native_jobs, job_id)["status"] == "sealed"
        assert worker.tick() is None
        assert _job_event_kinds(native_jobs, job_id).count("lease_resumed") == 1
        assert _job_event_kinds(native_jobs, job_id).count("resume_claimed") == 1
    finally:
        _release(service, workspace_id)


def test_inactive_holder_is_recovered_before_another_worker_claims(native_jobs) -> None:
    service = native_jobs.service
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    holder_id = f"worker-holder-{uuid4()}"
    other_id = f"worker-other-{uuid4()}"
    _worker(native_jobs, store, _ObserveSettles(_receipt(native_jobs)), holder_id)
    other = _worker(native_jobs, store, _ObserveSettles(_receipt(native_jobs)), other_id)
    job_id, fence = _claim(native_jobs, store, holder_id)

    _engage(service, workspace_id, uuid4())
    try:
        _expire_during_stop(native_jobs, workspace_id, job_id)
        drain_worker(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=holder_id,
        )
        assert _reconcile(native_jobs, store)["job_ids"] == []
        assert _job_row(native_jobs, job_id)["worker_id"] == holder_id

        stopped_claim = claim_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=other_id,
        )
        assert stopped_claim["job"] is None

        _release(service, workspace_id)
        before = claim_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=other_id,
        )
        assert before["job"] is None

        recovered = _reconcile(native_jobs, store)
        assert recovered["job_ids"] == [job_id]
        assert _reconcile(native_jobs, store)["job_ids"] == []
        row = _job_row(native_jobs, job_id)
        assert row["status"] == "backlog"
        assert row["worker_id"] is None
        assert int(row["fence"]) == fence + 1
        assert _reservation_reason(native_jobs, job_id) == "lease_recovered"
        kinds = _job_event_kinds(native_jobs, job_id)
        assert kinds.count("lease_recovered") == 1
        assert "lease_resumed" not in kinds
        assert "lease_expired" not in kinds

        with pytest.raises(JobError) as excinfo:
            claim_job(
                store,
                operation_id=str(uuid4()),
                workspace_id=workspace_id,
                actor_id=native_jobs.actor_id,
                worker_id=holder_id,
            )
        assert excinfo.value.code == "job_worker_unavailable"
        assert _job_row(native_jobs, job_id)["status"] == "backlog"

        assert other.tick() == job_id
        assert _job_row(native_jobs, job_id)["worker_id"] == other_id
        assert _job_row(native_jobs, job_id)["status"] == "sealed"
    finally:
        _release(service, workspace_id)


def test_lease_expired_before_stop_is_reclaimed_as_usual(native_jobs) -> None:
    service = native_jobs.service
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    handler = _ObserveSettles(_receipt(native_jobs))
    worker_id = f"worker-preexpire-{uuid4()}"
    worker = _worker(native_jobs, store, handler, worker_id)
    job_id, fence = _claim(native_jobs, store, worker_id)

    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
            " WHERE job_id=%s",
            (job_id,),
        )
    expired = _job_row(native_jobs, job_id)

    _engage(service, workspace_id, uuid4())
    try:
        assert _reconcile(native_jobs, store)["job_ids"] == []
        held = _job_row(native_jobs, job_id)
        assert held["status"] == "admitted"
        assert held["worker_id"] == worker_id
        assert int(held["fence"]) == fence
        assert held["lease_expires_at"] == expired["lease_expires_at"]

        _release(service, workspace_id)
        reclaimed = _reconcile(native_jobs, store)
        assert reclaimed["job_ids"] == [job_id]
        row = _job_row(native_jobs, job_id)
        assert row["status"] == "backlog"
        assert row["worker_id"] is None
        assert int(row["fence"]) == fence + 1
        assert _reservation_reason(native_jobs, job_id) == "lease_expired"
        kinds = _job_event_kinds(native_jobs, job_id)
        assert kinds == ["enqueued", "claimed", "lease_expired"]

        assert worker.tick() == job_id
        assert handler.observe_count == 1
        assert _job_row(native_jobs, job_id)["status"] == "sealed"
    finally:
        _release(service, workspace_id)


def test_frozen_handler_leaves_the_job_leased(native_jobs) -> None:
    store = _store(native_jobs)
    worker_id = f"worker-frozen-handler-{uuid4()}"
    worker = _worker(native_jobs, store, _RaisesFrozen(), worker_id)
    job_id = f"job-frozen-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=30)

    assert worker.tick() == job_id
    row = _job_row(native_jobs, job_id)
    assert row["status"] == "admitted"
    assert row["worker_id"] == worker_id
    assert int(row["fence"]) == 1
    assert row["settlement"] is None
    assert "settled" not in _job_event_kinds(native_jobs, job_id)
    assert _reservation_reason(native_jobs, job_id) is None

    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason="frozen handler test",
    )
