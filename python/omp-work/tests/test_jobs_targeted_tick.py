"""Targeted native-job tick (OMP-590-s02).

PostgreSQL integration (``OMP_WORK_POSTGRES_INTEGRATION=1``). Uses the
``native_jobs`` and ``service`` fixtures. A tick ``job_id`` admits, runs, and
settles only that job. An omitted target stays oldest-first. An unknown,
incompatible, or stopped target leases nothing else, and a targeted claim does
not consume another job's resumed lease.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.worker import JobWorker, Settlement, WorkerConfig
from omp_work.v1.canonical import sha256
from test_jobs_stop_frozen_lease import _expire_during_stop, _reconcile
from test_jobs_worker_loop import _receipt
from test_research_contract import _register_component
from test_stop_pause_resume import _engage, _job_event_kinds, _job_row, _release

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


class RecordingHandler:
    """Records each handler call and settles through the real worker path."""

    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt
        self.calls: list[tuple[str, str]] = []

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.calls.append(("observe", str(job["job_id"])))
        return Settlement(outcome="succeeded", receipts=[self.receipt])

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.calls.append(("run", str(job["job_id"])))
        return Settlement(outcome="succeeded", receipts=[self.receipt])


def _resources() -> dict[str, int]:
    return {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}


def _cap() -> str:
    # Second label must start with a letter: research capability pattern.
    return f"isolate.a{uuid4().hex}"


def _enqueue(
    native_jobs, store, job_id: str, capabilities: list[str]
) -> None:
    result = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=UUID(native_jobs.item["work_id"]),
        kind="compute",
        required_capabilities=capabilities,
        resources=_resources(),
        lease_seconds=30,
    )
    assert result["status"] == "applied", result


def _age(native_jobs, job_id: str, minutes: int) -> None:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("postgres"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        updated = conn.execute(
            """
            UPDATE omp_jobs.jobs
            SET created_at = clock_timestamp() - (%s * interval '1 minute')
            WHERE job_id=%s
            """,
            (minutes, job_id),
        )
        assert updated.rowcount == 1


def _worker(native_jobs, store, handler: RecordingHandler, capabilities: list[str]) -> JobWorker:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"target-{uuid4()}",
        capabilities=tuple(sorted(set(capabilities))),
    )
    worker = JobWorker(
        store,
        WorkerConfig(
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=f"worker-target-{uuid4()}",
            component_sha256=component,
            capabilities=list(capabilities),
            capacity=4,
        ),
        handler,
    )
    worker.start()
    return worker


def _pair(native_jobs, store, handler: RecordingHandler, capability: str) -> tuple[JobWorker, str, str]:
    worker = _worker(native_jobs, store, handler, [capability])
    older = f"job-older-{uuid4()}"
    requested = f"job-requested-{uuid4()}"
    _enqueue(native_jobs, store, older, [capability])
    _enqueue(native_jobs, store, requested, [capability])
    _age(native_jobs, older, 2)
    return worker, older, requested


def _operation_hash(native_jobs, operation_id: str) -> str:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("postgres"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        row = conn.execute(
            "SELECT request_sha256 FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()
    assert row is not None
    return str(row["request_sha256"])


def _hold_resumed_lease(native_jobs, store, worker: JobWorker, job_id: str) -> int:
    """Admit ``job_id`` to ``worker``, then leave one unconsumed ``lease_resumed``."""
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker.worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    fence = int(claimed["job"]["fence"])
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    _engage(service, workspace_id, uuid4())
    try:
        _expire_during_stop(native_jobs, workspace_id, job_id)
    finally:
        _release(service, workspace_id)
    reconciled = _reconcile(native_jobs, store)
    assert job_id in reconciled["job_ids"]
    assert _job_event_kinds(native_jobs, job_id) == [
        "enqueued",
        "claimed",
        "lease_resumed",
    ]
    row = _job_row(native_jobs, job_id)
    assert row["status"] == "admitted"
    assert row["worker_id"] == worker.worker_id
    assert int(row["fence"]) == fence
    return fence


def test_targeted_tick_settles_requested_and_leaves_older_backlog(native_jobs) -> None:
    """The requested job runs and seals; the older compatible job stays backlog."""
    store = NativeJobStore(native_jobs.service.config)
    handler = RecordingHandler(_receipt(native_jobs))
    worker, older, requested = _pair(native_jobs, store, handler, _cap())

    assert worker.tick(job_id=requested) == requested
    assert handler.calls == [("run", requested)]
    settled = _job_row(native_jobs, requested)
    assert settled["status"] == "sealed"
    assert settled["settlement"]["outcome"] == "succeeded"
    assert "settled" in _job_event_kinds(native_jobs, requested)

    older_row = _job_row(native_jobs, older)
    assert older_row["status"] == "backlog"
    assert older_row["worker_id"] is None
    assert _job_event_kinds(native_jobs, older) == ["enqueued"]


def test_untargeted_tick_keeps_fifo(native_jobs) -> None:
    """No target, including an explicit None, still claims the oldest job."""
    store = NativeJobStore(native_jobs.service.config)
    capability = _cap()
    handler = RecordingHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, [capability])
    oldest = f"job-oldest-{uuid4()}"
    middle = f"job-middle-{uuid4()}"
    newest = f"job-newest-{uuid4()}"
    for job_id in (oldest, middle, newest):
        _enqueue(native_jobs, store, job_id, [capability])
    _age(native_jobs, oldest, 3)
    _age(native_jobs, middle, 2)

    assert worker.tick() == oldest
    assert worker.tick(job_id=None) == middle
    assert _job_row(native_jobs, newest)["status"] == "backlog"
    assert handler.calls == [("run", oldest), ("run", middle)]
    assert worker.tick() == newest
    assert _job_row(native_jobs, newest)["status"] == "sealed"


def test_unknown_target_returns_none_without_another_claim(native_jobs) -> None:
    """A missing id leases nothing, and the older job stays available."""
    store = NativeJobStore(native_jobs.service.config)
    handler = RecordingHandler(_receipt(native_jobs))
    worker, older, requested = _pair(native_jobs, store, handler, _cap())

    assert worker.tick(job_id=f"missing-{uuid4()}") is None
    assert handler.calls == []
    assert _job_row(native_jobs, older)["status"] == "backlog"
    assert _job_row(native_jobs, requested)["status"] == "backlog"
    assert _job_event_kinds(native_jobs, older) == ["enqueued"]
    assert _job_event_kinds(native_jobs, requested) == ["enqueued"]

    assert worker.tick() == older
    assert handler.calls == [("run", older)]
    assert _job_row(native_jobs, requested)["status"] == "backlog"


def test_incompatible_target_stays_unclaimed(native_jobs) -> None:
    """A target the worker cannot run stays backlog, and so does the older job."""
    store = NativeJobStore(native_jobs.service.config)
    held = _cap()
    other = _cap()
    handler = RecordingHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, [held])
    older = f"job-older-{uuid4()}"
    requested = f"job-incompatible-{uuid4()}"
    _enqueue(native_jobs, store, older, [held])
    _enqueue(native_jobs, store, requested, [other])
    _age(native_jobs, older, 2)

    assert worker.tick(job_id=requested) is None
    assert handler.calls == []
    assert _job_row(native_jobs, requested)["status"] == "backlog"
    assert _job_row(native_jobs, older)["status"] == "backlog"
    assert _job_event_kinds(native_jobs, requested) == ["enqueued"]
    assert _job_event_kinds(native_jobs, older) == ["enqueued"]

    assert worker.tick() == older
    assert _job_row(native_jobs, requested)["status"] == "backlog"


def test_stopped_target_stays_unclaimed(native_jobs) -> None:
    """An engaged stop leaves the targeted job and the older job unclaimed."""
    store = NativeJobStore(native_jobs.service.config)
    handler = RecordingHandler(_receipt(native_jobs))
    worker, older, requested = _pair(native_jobs, store, handler, _cap())
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    _engage(service, workspace_id, uuid4())
    try:
        assert worker.tick(job_id=requested) is None
        assert handler.calls == []
        assert _job_row(native_jobs, requested)["status"] == "backlog"
        assert _job_row(native_jobs, older)["status"] == "backlog"
        assert _job_event_kinds(native_jobs, requested) == ["enqueued"]
        assert _job_event_kinds(native_jobs, older) == ["enqueued"]
    finally:
        _release(service, workspace_id)

    assert worker.tick(job_id=requested) == requested
    assert handler.calls == [("run", requested)]
    assert _job_row(native_jobs, older)["status"] == "backlog"


def test_targeted_tick_does_not_consume_another_jobs_resumed_lease(native_jobs) -> None:
    """Targeting a backlog job leaves another job's lease_resumed unclaimed."""
    store = NativeJobStore(native_jobs.service.config)
    capability = _cap()
    handler = RecordingHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, [capability])
    resumed = f"job-resumed-{uuid4()}"
    requested = f"job-requested-{uuid4()}"
    _enqueue(native_jobs, store, resumed, [capability])
    fence = _hold_resumed_lease(native_jobs, store, worker, resumed)
    _enqueue(native_jobs, store, requested, [capability])

    assert worker.tick(job_id=requested) == requested
    assert handler.calls == [("run", requested)]
    assert _job_row(native_jobs, requested)["status"] == "sealed"

    assert _job_event_kinds(native_jobs, resumed) == [
        "enqueued",
        "claimed",
        "lease_resumed",
    ]
    row = _job_row(native_jobs, resumed)
    assert row["status"] == "admitted"
    assert row["worker_id"] == worker.worker_id
    assert int(row["fence"]) == fence
    assert row["settlement"] is None

    assert worker.tick() == resumed
    assert handler.calls == [("run", requested), ("observe", resumed)]
    assert "resume_claimed" in _job_event_kinds(native_jobs, resumed)
    assert _job_row(native_jobs, resumed)["status"] == "sealed"


def test_targeted_tick_resumes_only_the_requested_lease(native_jobs) -> None:
    """Targeting the resumed job settles it and leaves the backlog job alone."""
    store = NativeJobStore(native_jobs.service.config)
    capability = _cap()
    handler = RecordingHandler(_receipt(native_jobs))
    worker = _worker(native_jobs, store, handler, [capability])
    resumed = f"job-resumed-{uuid4()}"
    backlog = f"job-backlog-{uuid4()}"
    _enqueue(native_jobs, store, resumed, [capability])
    _hold_resumed_lease(native_jobs, store, worker, resumed)
    _enqueue(native_jobs, store, backlog, [capability])

    assert worker.tick(job_id=resumed) == resumed
    assert handler.calls == [("observe", resumed)]
    assert _job_row(native_jobs, resumed)["status"] == "sealed"
    assert "resume_claimed" in _job_event_kinds(native_jobs, resumed)
    assert _job_row(native_jobs, backlog)["status"] == "backlog"
    assert _job_event_kinds(native_jobs, backlog) == ["enqueued"]


def test_claim_request_hash_includes_target_and_preserves_unfiltered_shape(native_jobs) -> None:
    """A target changes the claim hash; an omitted target keeps the old shape."""
    store = NativeJobStore(native_jobs.service.config)
    handler = RecordingHandler(_receipt(native_jobs))
    worker, older, requested = _pair(native_jobs, store, handler, _cap())
    plain = {
        "workspace_id": str(native_jobs.workspace_id),
        "worker_id": worker.worker_id,
        "resource_limits": None,
    }
    operation_id = str(uuid4())
    first = claim_job(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker.worker_id,
    )
    assert first["status"] == "applied"
    assert first["job"]["job_id"] == older
    assert _operation_hash(native_jobs, operation_id) == sha256(plain)
    assert sha256(plain) != sha256({**plain, "job_id": None})

    replayed = claim_job(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker.worker_id,
    )
    assert replayed["status"] == "replayed"
    assert replayed["job"]["job_id"] == older
    with pytest.raises(JobError) as excinfo:
        claim_job(
            store,
            operation_id=operation_id,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=worker.worker_id,
            job_id=requested,
        )
    assert excinfo.value.code == "idempotency_conflict"
    assert _job_row(native_jobs, requested)["status"] == "backlog"

    targeted_operation = str(uuid4())
    targeted = claim_job(
        store,
        operation_id=targeted_operation,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker.worker_id,
        job_id=requested,
    )
    assert targeted["status"] == "applied"
    assert targeted["job"]["job_id"] == requested
    targeted_request = {**plain, "job_id": requested}
    assert _operation_hash(native_jobs, targeted_operation) == sha256(targeted_request)
    assert _operation_hash(native_jobs, targeted_operation) != sha256(plain)
    targeted_replay = claim_job(
        store,
        operation_id=targeted_operation,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker.worker_id,
        job_id=requested,
    )
    assert targeted_replay["status"] == "replayed"
    assert targeted_replay["job"]["job_id"] == requested
    assert handler.calls == []
