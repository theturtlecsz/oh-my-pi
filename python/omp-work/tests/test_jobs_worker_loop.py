"""Worker loop acceptance tests (R03, OMP-324, OMP-400).

Proves the native jobs JobWorker runtime:
- A job runs once and is sealed with its receipts; its reservation is released.
  A usage item lands once in ledger_path; a second tick adds none.
- A new JobWorker with the same worker_id (restart) claims the next job; three
  renewals move lease_expires_at by 3×lease_seconds.
- resource_limits={"gpu":1}: a second gpu job waits, a cpu job is claimed.
- Lease expiry after the effect → observe seals it; run count 1.
- Cancel mid-run → not settled.
- start() refuses while an omp_jobs.schema_migrations row is missing (restored after).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import (
    Handler,
    JobWorker,
    Settlement,
    WorkerConfig,
    load_handler,
)
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _connect_admin(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("postgres"),
        row_factory=dict_row,
        autocommit=True,
    )


def _work_id(native_jobs) -> UUID:
    return UUID(native_jobs.item["work_id"])


def _resources(
    cpu: int = 1, memory_mib: int = 0, gpu: int = 0, model_calls: int = 0
) -> dict[str, int]:
    return {"cpu": cpu, "memory_mib": memory_mib, "gpu": gpu, "model_calls": model_calls}


def _receipt(native_jobs, role: str = "audit") -> dict[str, object]:
    return {
        "role": role,
        "issuer_component_sha256": native_jobs.components[role],
        "evidence": {
            "id": f"ev-{uuid4()}",
            "kind": "receipt",
            "content_digest": "abcd1234ef",
            "trust": "verified",
        },
    }


def _usage_item(job_id: str, request_id: str | None = None) -> dict[str, object]:
    return {
        "job_id": job_id,
        "request_id": request_id or f"req-{uuid4()}",
        "role": "worker",
        "model": "gemini-3.8-flash",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_tokens": 0,
        "measurement": "measured",
        "price_usd": "0.001",
    }


def _enqueue(native_jobs, store: NativeJobStore, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "job_id": f"job-{uuid4()}",
        "work_id": _work_id(native_jobs),
        "kind": "compute",
        "required_capabilities": ["compute.cpu"],
        "resources": _resources(),
        "lease_seconds": 30,
    }
    body.update(overrides)
    return enqueue_job(store, **body)


class MockHandler:
    def __init__(
        self,
        receipts: list[dict[str, object]] | None = None,
        usage: list[dict[str, object]] | None = None,
    ) -> None:
        self.receipts = receipts or []
        self.usage = usage or []
        self.run_count = 0
        self.observe_count = 0

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        self.observe_count += 1
        return None

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.run_count += 1
        return Settlement(
            outcome="succeeded",
            receipts=self.receipts,
            usage=self.usage,
        )


def test_job_runs_once_sealed_reservation_released_usage_delivered_once(
    native_jobs, tmp_path: Path
) -> None:
    """A job runs once and is sealed with its receipts; its reservation is released.

    A usage item lands once in ledger_path; a second tick adds none.
    """
    store = _store(native_jobs)
    job_id = f"job-seal-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)

    receipts = [_receipt(native_jobs, "audit")]
    usage = [_usage_item(job_id)]
    handler = MockHandler(receipts=receipts, usage=usage)

    ledger_path = tmp_path / "test_ledger.jsonl"
    worker_id = f"worker-seal-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=2,
        ledger_path=ledger_path,
    )

    worker = JobWorker(store, config, handler)
    worker.start()

    claimed_id = worker.tick()
    assert claimed_id == job_id
    assert handler.run_count == 1

    with _connect(native_jobs) as conn:
        job_row = conn.execute(
            "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row is not None
        assert job_row["status"] == "sealed"
        assert job_row["settlement"]["outcome"] == "succeeded"
        assert job_row["settlement"]["worker_id"] == worker_id
        assert len(job_row["settlement"]["receipts"]) == 1

        res_row = conn.execute(
            "SELECT released_at, release_reason FROM omp_jobs.reservations WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert res_row is not None
        assert res_row["released_at"] is not None
        assert res_row["release_reason"] == "settled"

    assert ledger_path.is_file()
    data = json.loads(ledger_path.read_text())
    assert len(data.get("events", [])) == 1

    second_tick_id = worker.tick()
    assert second_tick_id is None
    assert handler.run_count == 1

    data_after = json.loads(ledger_path.read_text())
    assert len(data_after.get("events", [])) == 1


def test_worker_restart_three_renewals_move_lease_expires_at(native_jobs) -> None:
    """A new JobWorker with the same worker_id (restart) claims the next job.

    Three renewals move lease_expires_at by 3×lease_seconds.
    """
    store = _store(native_jobs)
    worker_id = f"worker-restart-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=5,
    )

    job1_id = f"job-restart-1-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job1_id)
    w1 = JobWorker(store, config, MockHandler(receipts=[_receipt(native_jobs)]))
    w1.start()
    assert w1.tick() == job1_id

    lease_seconds = 1
    job2_id = f"job-restart-2-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job2_id, lease_seconds=lease_seconds)

    class RenewHandler:
        def __init__(self) -> None:
            self.initial_expiry = None
            self.final_expiry = None

        def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
            return None

        def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
            with _connect(native_jobs) as conn:
                row = conn.execute(
                    "SELECT lease_expires_at FROM omp_jobs.jobs WHERE job_id=%s",
                    (job["job_id"],),
                ).fetchone()
                self.initial_expiry = row["lease_expires_at"]

            expected = self.initial_expiry + timedelta(seconds=3 * lease_seconds)
            deadline = time.time() + 6.0
            while time.time() < deadline:
                with _connect(native_jobs) as conn:
                    row = conn.execute(
                        "SELECT lease_expires_at FROM omp_jobs.jobs WHERE job_id=%s",
                        (job["job_id"],),
                    ).fetchone()
                    if row["lease_expires_at"] >= expected:
                        self.final_expiry = row["lease_expires_at"]
                        break
                time.sleep(0.05)

            return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])

    renew_handler = RenewHandler()
    w2 = JobWorker(store, config, renew_handler)
    w2.start()

    claimed_id = w2.tick()
    assert claimed_id == job2_id
    assert renew_handler.final_expiry is not None
    assert renew_handler.final_expiry >= renew_handler.initial_expiry + timedelta(
        seconds=3 * lease_seconds
    )


def test_resource_limits_gpu_waits_cpu_claimed(native_jobs) -> None:
    """resource_limits={"gpu":1}: a second gpu job waits, a cpu job is claimed."""
    store = _store(native_jobs)
    worker_id = f"worker-reslim-{uuid4()}"

    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=10,
        resource_limits={"gpu": 1},
    )
    worker = JobWorker(store, config, MockHandler(receipts=[_receipt(native_jobs)]))
    worker.start()

    gpu_job_1 = f"gpu-job-1-{uuid4()}"
    gpu_job_2 = f"gpu-job-2-{uuid4()}"
    cpu_job_1 = f"cpu-job-1-{uuid4()}"

    _enqueue(
        native_jobs,
        store,
        job_id=gpu_job_1,
        required_capabilities=["compute.gpu"],
        resources=_resources(cpu=1, gpu=1),
    )
    _enqueue(
        native_jobs,
        store,
        job_id=gpu_job_2,
        required_capabilities=["compute.gpu"],
        resources=_resources(cpu=1, gpu=1),
    )
    _enqueue(
        native_jobs,
        store,
        job_id=cpu_job_1,
        required_capabilities=["compute.cpu"],
        resources=_resources(cpu=1, gpu=0),
    )

    claimed_1 = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        resource_limits={"gpu": 1},
    )
    assert claimed_1["job"]["job_id"] == gpu_job_1

    claimed_2 = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        resource_limits={"gpu": 1},
    )
    assert claimed_2["job"]["job_id"] == cpu_job_1

    with _connect(native_jobs) as conn:
        gpu2_row = conn.execute(
            "SELECT status FROM omp_jobs.jobs WHERE job_id=%s",
            (gpu_job_2,),
        ).fetchone()
        assert gpu2_row["status"] == "backlog"

    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=gpu_job_1,
        reason="cleanup",
    )
    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=gpu_job_2,
        reason="cleanup",
    )
    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=cpu_job_1,
        reason="cleanup",
    )


def test_lease_expiry_after_effect_observe_seals_run_count_one(native_jobs) -> None:
    """Lease expiry after the effect → observe seals it; run count 1."""
    store = _store(native_jobs)
    job_id = f"job-observe-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=1)

    class EffectHandler:
        def __init__(self) -> None:
            self.effect_done = False
            self.run_count = 0
            self.observe_count = 0

        def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
            self.observe_count += 1
            if self.effect_done:
                return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])
            return None

        def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
            self.run_count += 1
            self.effect_done = True
            return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])

    handler = EffectHandler()
    worker_id = f"worker-obs-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=2,
    )
    worker = JobWorker(store, config, handler)
    worker.start()

    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    handler.run(worker, claimed["job"])
    assert handler.run_count == 1
    assert handler.effect_done is True

    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id=%s",
            (job_id,),
        )

    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert job_id in reconciled["job_ids"]

    with _connect(native_jobs) as conn:
        job_row = conn.execute(
            "SELECT status FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "backlog"

    claimed_second = worker.tick()
    assert claimed_second == job_id
    assert handler.observe_count == 1
    assert handler.run_count == 1

    with _connect(native_jobs) as conn:
        job_row = conn.execute(
            "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "sealed"
        assert job_row["settlement"]["outcome"] == "succeeded"


def test_cancel_mid_run_not_settled(native_jobs) -> None:
    """Cancel mid-run → not settled."""
    store = _store(native_jobs)
    job_id = f"job-cancel-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=1)

    started_event = threading.Event()
    cancel_done_event = threading.Event()

    class CancelHandler:
        def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
            return None

        def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
            started_event.set()
            cancel_done_event.wait(timeout=5.0)
            time.sleep(0.5)
            return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])

    handler = CancelHandler()
    worker_id = f"worker-cancel-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=2,
    )
    worker = JobWorker(store, config, handler)
    worker.start()

    worker_thread = threading.Thread(target=worker.tick)
    worker_thread.start()

    assert started_event.wait(timeout=5.0)

    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason="user_stop",
    )
    cancel_done_event.set()

    worker_thread.join(timeout=5.0)

    with _connect(native_jobs) as conn:
        job_row = conn.execute(
            "SELECT status, settlement, cancel_reason FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "cancelled"
        assert job_row["settlement"] is None
        assert job_row["cancel_reason"] == "user_stop"


def test_start_refuses_while_schema_migration_row_is_missing(native_jobs) -> None:
    """start() refuses while an omp_jobs.schema_migrations row is missing (restored after)."""
    store = _store(native_jobs)
    worker_id = f"worker-migcheck-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=2,
    )
    worker = JobWorker(store, config, MockHandler())

    with _connect_admin(native_jobs) as conn:
        row = conn.execute(
            "SELECT ordinal, filename, sha256 FROM omp_jobs.schema_migrations ORDER BY ordinal DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        saved_ordinal = row["ordinal"]
        saved_filename = row["filename"]
        saved_sha256 = row["sha256"]

        conn.execute(
            "DELETE FROM omp_jobs.schema_migrations WHERE ordinal=%s",
            (saved_ordinal,),
        )

    try:
        with pytest.raises(RuntimeError, match="jobs migration"):
            worker.start()
    finally:
        with _connect_admin(native_jobs) as conn:
            conn.execute(
                "INSERT INTO omp_jobs.schema_migrations(ordinal, filename, sha256) VALUES (%s, %s, %s) ON CONFLICT (ordinal) DO NOTHING",
                (saved_ordinal, saved_filename, saved_sha256),
            )

    worker.start()
    with _connect(native_jobs) as conn:
        worker_row = conn.execute(
            "SELECT state FROM omp_jobs.workers WHERE worker_id=%s",
            (worker_id,),
        ).fetchone()
        assert worker_row is not None
        assert worker_row["state"] == "active"


def test_load_handler_factory() -> None:
    """load_handler parses factory strings and instantiates handlers with options."""
    spec = {
        "factory": "test_jobs_worker_loop:MockHandler",
        "options": {},
    }
    loaded = load_handler(spec)
    assert isinstance(loaded, Handler)

    existing = MockHandler()
    assert load_handler(existing) is existing

    with pytest.raises(TypeError):
        load_handler("not-a-dict")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        load_handler({"factory": "no_colon"})


def test_run_forever_stops_on_event(native_jobs) -> None:
    """run_forever runs until stop_event is set."""
    store = _store(native_jobs)
    worker_id = f"worker-forever-{uuid4()}"
    config = WorkerConfig(
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=1,
    )
    worker = JobWorker(store, config, MockHandler())
    stop_event = threading.Event()

    t = threading.Thread(target=worker.run_forever, args=(stop_event, 0.05))
    t.start()
    time.sleep(0.1)
    stop_event.set()
    t.join(timeout=2.0)
    assert not t.is_alive()
