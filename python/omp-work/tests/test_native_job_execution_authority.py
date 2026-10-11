"""Tests for native job execution authority and renewal lifecycle (OMP-595-s03).

Proves:
1. Real DB recovery handler runs beyond initial lease interval with renewal.
2. Cancellation or reassignment during execution signals the existing cancellation
   event and forbids settlement.
3. Emergency stop signals frozen separately, preserving worker/fence/reservation;
   release resumes once.
4. Next tick starts uncancelled and never reuses the prior tick's cancellation event.
5. All background renewal threads end cleanly on every exit path.
6. Stale settlements are absent when authority is lost or frozen.
7. Shared read-only current-authority query yields live expiry, lost authority,
   or frozen ownership.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import (
    LeaseAuthority,
    current_authority,
    reconcile_jobs,
    renew_lease,
)
from omp_work.jobs.store import JobError, NativeJobStore
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


def _worker(native_jobs: Any, store: NativeJobStore, handler: Any, worker_id: str) -> JobWorker:
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


def _reconcile(native_jobs: Any, store: NativeJobStore) -> dict[str, object]:
    return reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )


def _expire_during_stop(native_jobs: Any, workspace_id: UUID, job_id: str) -> None:
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


def test_recovery_handler_runs_beyond_initial_interval_with_renewal(native_jobs: Any) -> None:
    """Real DB recovery handler runs beyond initial lease interval with renewal.

    ``observe`` executes past initial 1-second lease_seconds while renewal runs in background.
    The job is successfully settled and sealed. Renewal thread ends and is joined.
    """
    store = _store(native_jobs)
    worker_id = f"worker-recovery-{uuid4().hex[:8]}"

    class SlowRecoveryHandler:
        def __init__(self, receipt: dict[str, object]) -> None:
            self.receipt = receipt
            self.observed = False
            self.ran = False
            self.initial_expires_at: datetime | None = None
            self.final_expires_at: datetime | None = None

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.observed = True
            job_id = str(job["job_id"])
            fence = int(job["fence"])
            # Record initial expiry
            auth_start = current_authority(
                store,
                workspace_id=native_jobs.workspace_id,
                job_id=job_id,
                worker_id=worker.worker_id,
                fence=fence,
            )
            assert auth_start.is_live is True
            self.initial_expires_at = auth_start.expires_at

            # Sleep longer than the 1s initial lease_seconds to require background renewals
            time.sleep(1.8)

            auth_end = current_authority(
                store,
                workspace_id=native_jobs.workspace_id,
                job_id=job_id,
                worker_id=worker.worker_id,
                fence=fence,
            )
            assert auth_end.is_live is True
            self.final_expires_at = auth_end.expires_at
            # Verify lease was renewed past the initial expiration
            assert self.final_expires_at > self.initial_expires_at

            return Settlement(outcome="succeeded", receipts=[self.receipt])

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.ran = True
            return Settlement(outcome="succeeded", receipts=[self.receipt])

    handler = SlowRecoveryHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, worker_id)

    # Enqueue a job with a short 1s lease
    job_id = f"job-recovery-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=1)

    # Claim the job first
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        job_id=job_id,
    )
    fence = int(claimed["job"]["fence"])

    # Simulate an interruption / stop-release recovery by appending a lease_resumed event
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        store.append_event(
            cur,
            workspace_id=native_jobs.workspace_id,
            job_id=job_id,
            kind="lease_resumed",
            actor=worker_id,
            operation_id=str(uuid4()),
            payload={"worker_id": worker_id, "fence": fence, "release_event_id": str(uuid4())},
        )

    threads_before = threading.enumerate()
    # Now run tick - since job has lease_resumed, resume_from_saved is True and observe runs
    result = worker.tick(job_id=job_id)
    threads_after = threading.enumerate()

    assert result == job_id
    assert handler.observed is True
    assert handler.ran is False

    # Check that job sealed successfully and settlement is recorded
    row = _job_row(native_jobs, job_id)
    assert row["status"] == "sealed"
    assert row["settlement"] is not None
    assert row["settlement"]["outcome"] == "succeeded"

    # Assert no leaked renewal threads
    assert len(threads_after) <= len(threads_before)

    # Current authority on sealed job reports terminal loss
    auth = current_authority(
        store,
        workspace_id=native_jobs.workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert auth.is_lost is True
    assert "sealed" in str(auth.reason)


def test_cancellation_during_execution_signals_and_forbids_settlement(native_jobs: Any) -> None:
    """Cancellation during handler execution signals cancel_requested and forbids settlement."""
    store = _store(native_jobs)
    worker_id = f"worker-cancel-{uuid4().hex[:8]}"

    class CancellableHandler:
        def __init__(self, receipt: dict[str, object]) -> None:
            self.receipt = receipt
            self.started = threading.Event()
            self.saw_cancel = False

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement | None:
            return None

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.started.set()
            # Wait for cancel signal to arrive via background renewal check
            self.saw_cancel = worker.cancel_requested.wait(timeout=10.0)
            # Even if handler returns settlement after cancel, it must not be applied
            return Settlement(outcome="succeeded", receipts=[self.receipt])

    handler = CancellableHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, worker_id)

    job_id = f"job-cancel-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=2)

    tick_error: BaseException | None = None
    tick_res: str | None = None

    def _tick() -> None:
        nonlocal tick_res, tick_error
        try:
            tick_res = worker.tick(job_id=job_id)
        except BaseException as exc:
            tick_error = exc

    thread = threading.Thread(target=_tick)
    thread.start()

    assert handler.started.wait(timeout=5.0)

    # Cancel the job in PostgreSQL while handler is running
    cancel_res = cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason="user_aborted",
    )
    assert cancel_res["status"] == "applied"

    thread.join(timeout=15.0)
    assert not thread.is_alive()
    assert tick_error is None
    assert tick_res == job_id

    assert handler.saw_cancel is True
    assert worker.cancel_requested.is_set() is True
    assert worker.frozen_requested.is_set() is False

    # Job is cancelled, no stale settlement was accepted
    row = _job_row(native_jobs, job_id)
    assert row["status"] == "cancelled"
    assert row["settlement"] is None
    assert "settled" not in _job_event_kinds(native_jobs, job_id)


def test_reassignment_during_execution_signals_and_forbids_settlement(native_jobs: Any) -> None:
    """Stale fence / reassignment during execution signals cancel_requested and forbids settlement."""
    store = _store(native_jobs)
    worker_id = f"worker-stale-{uuid4().hex[:8]}"

    class ReassignedHandler:
        def __init__(self, receipt: dict[str, object]) -> None:
            self.receipt = receipt
            self.started = threading.Event()
            self.saw_cancel = False

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement | None:
            return None

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.started.set()
            self.saw_cancel = worker.cancel_requested.wait(timeout=10.0)
            return Settlement(outcome="succeeded", receipts=[self.receipt])

    handler = ReassignedHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, worker_id)

    job_id = f"job-reassigned-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=2)

    tick_error: BaseException | None = None
    tick_res: str | None = None

    def _tick() -> None:
        nonlocal tick_res, tick_error
        try:
            tick_res = worker.tick(job_id=job_id)
        except BaseException as exc:
            tick_error = exc

    thread = threading.Thread(target=_tick)
    thread.start()

    assert handler.started.wait(timeout=5.0)

    # Reassign job / advance fence in DB
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET fence = fence + 1 WHERE job_id = %s",
            (job_id,),
        )

    thread.join(timeout=15.0)
    assert not thread.is_alive()
    assert tick_error is None
    assert tick_res == job_id

    assert handler.saw_cancel is True
    assert worker.cancel_requested.is_set() is True
    assert worker.cancel_reason == "job_fence_stale"

    # Settlement was forbidden; no stale settlement
    row = _job_row(native_jobs, job_id)
    assert row["settlement"] is None
    assert "settled" not in _job_event_kinds(native_jobs, job_id)


def test_stop_signals_frozen_separately_preserves_holder_resumes_once(native_jobs: Any) -> None:
    """Agent stop signals frozen_requested separately, preserving worker/fence/reservation; release resumes once."""
    service = native_jobs.service
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    worker_id = f"worker-frozen-{uuid4().hex[:8]}"

    class StoppableHandler:
        def __init__(self, receipt: dict[str, object]) -> None:
            self.receipt = receipt
            self.started = threading.Event()
            self.saw_frozen = False
            self.saw_cancel = False
            self.observe_count = 0
            self.run_count = 0

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.observe_count += 1
            return Settlement(outcome="succeeded", receipts=[self.receipt])

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.run_count += 1
            self.started.set()
            # Wait for frozen signal to arrive
            self.saw_frozen = worker.frozen_requested.wait(timeout=10.0)
            self.saw_cancel = worker.cancel_requested.is_set()
            raise Frozen("stop engaged")

    handler = StoppableHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, worker_id)

    job_id = f"job-stop-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=2)

    tick_thread = threading.Thread(target=lambda: worker.tick(job_id=job_id))
    tick_thread.start()

    assert handler.started.wait(timeout=5.0)

    # Engage emergency stop while worker is running
    _engage(service, workspace_id, uuid4())
    try:
        tick_thread.join(timeout=15.0)
        assert not tick_thread.is_alive()

        # Stop signaled frozen separately
        assert handler.saw_frozen is True
        assert handler.saw_cancel is False
        assert worker.frozen_requested.is_set() is True
        assert worker.cancel_requested.is_set() is False
        assert worker.frozen_reason == "agent_stop_engaged"

        # Worker, fence, and reservation are preserved intact
        row = _job_row(native_jobs, job_id)
        assert row["status"] == "admitted"
        assert row["worker_id"] == worker_id
        assert int(row["fence"]) == 1
        assert row["settlement"] is None
        assert _reservation_reason(native_jobs, job_id) is None

        # Current authority reports frozen ownership
        auth_frozen = current_authority(
            store,
            workspace_id=workspace_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=1,
        )
        assert auth_frozen.is_frozen is True
        assert auth_frozen.state == "frozen"
        assert auth_frozen.reason == "runaway loop"

        # Even if lease expires while stopped, frozen expiry does NOT authorize reassignment
        _expire_during_stop(native_jobs, workspace_id, job_id)
        auth_expired_frozen = current_authority(
            store,
            workspace_id=workspace_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=1,
        )
        assert auth_expired_frozen.is_frozen is True
        assert auth_expired_frozen.state == "frozen"
        assert auth_expired_frozen.reason == "runaway loop"

        # Release stop
        _release(service, workspace_id)

        # Reconcile resumes the frozen lease once
        reconciled = _reconcile(native_jobs, store)
        assert reconciled["job_ids"] == [job_id]

        # Resumed authority is now live
        auth_live = current_authority(
            store,
            workspace_id=workspace_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=1,
        )
        assert auth_live.is_live is True
        assert auth_live.state == "live"
        assert auth_live.expires_at > datetime.now(UTC)

        # Second reconcile does NOT resume again (release resumes once)
        second_reconcile = _reconcile(native_jobs, store)
        assert job_id not in second_reconcile["job_ids"]

        # Next tick completes the resumed job through observe
        resumed_res = worker.tick(job_id=job_id)
        assert resumed_res == job_id
        assert handler.observe_count == 1

        final_row = _job_row(native_jobs, job_id)
        assert final_row["status"] == "sealed"
        assert final_row["settlement"] is not None
    finally:
        _release(service, workspace_id)


def test_next_tick_starts_uncancelled_never_reuses_prior_event(native_jobs: Any) -> None:
    """Next tick starts uncancelled; never reuse prior tick's cancellation event."""
    store = _store(native_jobs)
    worker_id = f"worker-tick-clean-{uuid4().hex[:8]}"

    class CancelFirstThenSucceedHandler:
        def __init__(self, receipt: dict[str, object]) -> None:
            self.receipt = receipt
            self.tick1_started = threading.Event()
            self.tick2_event_was_set = None
            self.tick2_cancel_event = None

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement | None:
            return None

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            if not self.tick1_started.is_set():
                self.tick1_started.set()
                worker.cancel_requested.wait(timeout=10.0)
                return Settlement(outcome="succeeded", receipts=[self.receipt])
            else:
                self.tick2_event_was_set = worker.cancel_requested.is_set()
                self.tick2_cancel_event = worker.cancel_requested
                return Settlement(outcome="succeeded", receipts=[self.receipt])

    handler = CancelFirstThenSucceedHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, worker_id)

    # Tick 1: cancelled job
    job1_id = f"job-tick1-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job1_id, lease_seconds=2)

    tick1_thread = threading.Thread(target=lambda: worker.tick(job_id=job1_id))
    tick1_thread.start()
    assert handler.tick1_started.wait(timeout=5.0)

    tick1_event = worker.cancel_requested

    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job1_id,
        reason="cancel_tick1",
    )
    tick1_thread.join(timeout=15.0)
    assert not tick1_thread.is_alive()
    assert tick1_event.is_set() is True

    # Tick 2: fresh uncancelled job
    job2_id = f"job-tick2-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job2_id, lease_seconds=30)

    tick2_res = worker.tick(job_id=job2_id)
    assert tick2_res == job2_id

    # Assert next tick started uncancelled and created a fresh event
    assert handler.tick2_event_was_set is False
    assert handler.tick2_cancel_event is not None
    assert handler.tick2_cancel_event is not tick1_event
    assert worker.cancel_requested.is_set() is False

    job2_row = _job_row(native_jobs, job2_id)
    assert job2_row["status"] == "sealed"


def test_current_authority_query_contracts(native_jobs: Any) -> None:
    """Validate current_authority across live, expired, mismatched, and terminal states."""
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    worker_id = f"worker-query-{uuid4().hex[:8]}"

    # 1. Non-existent job
    missing = current_authority(
        store,
        workspace_id=workspace_id,
        job_id="job-does-not-exist",
        worker_id=worker_id,
        fence=1,
    )
    assert missing.is_lost is True
    assert missing.state == "lost"
    assert missing["state"] == "lost"
    assert missing.reason == "job_not_found"

    # 2. Live admitted job
    class _DummyHandler:
        pass

    _worker(native_jobs, store, _DummyHandler(), worker_id)
    job_id = f"job-auth-{uuid4().hex[:8]}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=30)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        job_id=job_id,
    )
    fence = int(claimed["job"]["fence"])

    live = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert live.is_live is True
    assert live.state == "live"
    assert live.status == "live"
    assert live.expires_at is not None
    assert live.expires_at > datetime.now(UTC)
    assert live.reason is None
    assert live.job is not None

    # 3. Worker mismatch
    mismatch_w = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id="other-worker",
        fence=fence,
    )
    assert mismatch_w.is_lost is True
    assert mismatch_w.reason == "worker_mismatch"

    # 4. Stale fence
    stale_f = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence + 99,
    )
    assert stale_f.is_lost is True
    assert stale_f.reason == "fence_stale"

    # 5. Expired lease
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '10 seconds'"
            " WHERE job_id = %s",
            (job_id,),
        )
    expired = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert expired.is_lost is True
    assert expired.reason == "lease_expired"

    # 6. Cancelled job
    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason="test_explicit_cancel",
    )
    cancelled = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert cancelled.is_lost is True
    assert cancelled.reason == "test_explicit_cancel"

    # 7. Bounded transaction read with deadline
    bounded = current_authority(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
        deadline=time.monotonic() + 10.0,
    )
    assert bounded.is_lost is True
