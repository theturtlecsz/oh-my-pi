"""Tests for native jobs cancel signal propagation and worker drain (R05, OMP-315).

Proves:
1. Cancellation signal is propagated via worker.cancel_requested to a running stub handler,
   seen within lease_seconds + 2 s, tick returns, job is cancelled with no settlement.
2. An uncancelled job seals normally with worker.cancel_requested remaining unset.
3. Drain: run_worker as a real process completes and settles a running job on SIGTERM,
   exits 0, leaving a second backlog job unclaimed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess  # nosec B404
import sys
import threading
import time
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.jobs.worker import JobWorker, Settlement
from omp_work.operations.config import OperationsConfig
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs: Any) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _resources(
    cpu: int = 1, memory_mib: int = 0, gpu: int = 0, model_calls: int = 0
) -> dict[str, int]:
    return {"cpu": cpu, "memory_mib": memory_mib, "gpu": gpu, "model_calls": model_calls}


def _connect(native_jobs: Any) -> psycopg.Connection:
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _work_id(native_jobs: Any) -> UUID:
    return UUID(native_jobs.item["work_id"])


def _enqueue(native_jobs: Any, store: NativeJobStore, **overrides: object) -> dict[str, Any]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "job_id": f"job-{uuid4()}",
        "work_id": _work_id(native_jobs),
        "kind": "compute",
        "required_capabilities": ["compute.cpu"],
        "resources": _resources(),
        "lease_seconds": 60,
    }
    body.update(overrides)
    return enqueue_job(store, **body)


def _worker(native_jobs: Any, store: NativeJobStore, capabilities: list[str]) -> tuple[str, str]:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"cancel-worker-{uuid4()}",
        capabilities=tuple(sorted(set(capabilities))),
    )
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=component,
        capabilities=capabilities,
        capacity=1,
    )
    assert registered["status"] == "applied", registered
    return worker_id, component


def _cancel(native_jobs: Any, store: NativeJobStore, **overrides: object) -> dict[str, Any]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "reason": "stop",
    }
    body.update(overrides)
    return cancel_job(store, **body)


def _evidence() -> dict[str, object]:
    return {
        "id": f"ev-{uuid4()}",
        "kind": "receipt",
        "content_digest": "abcd1234ef",
        "trust": "verified",
    }


def _receipt(native_jobs: Any) -> dict[str, object]:
    return {
        "role": "audit",
        "issuer_component_sha256": native_jobs.components["audit"],
        "evidence": _evidence(),
    }


def _job(native_jobs: Any, job_id: str) -> dict[str, Any] | None:
    with _connect(native_jobs) as conn:
        return conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()


def _events(native_jobs: Any, job_id: str) -> list[str]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
            (job_id,),
        ).fetchall()
    return [row["kind"] for row in rows]


def _run_child(script: str, *args: str) -> subprocess.Popen:
    env = os.environ.copy()
    tests_dir = str(Path(__file__).parent.resolve())
    src_dir = str((Path(__file__).parent.parent / "src").resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tests_dir}:{src_dir}:{existing}" if existing else f"{tests_dir}:{src_dir}"
    return subprocess.Popen(  # nosec B603
        [sys.executable, "-c", script, *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_child(proc: subprocess.Popen, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except BaseException:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        raise


def drain_handler_factory(marker_path: str, issuer_sha: str, sleep_seconds: float = 1.0) -> Any:
    """Factory called by run_worker in child process for the drain test."""

    class DrainHandler:
        def run(self, worker: Any, job: dict[str, Any]) -> Settlement:
            Path(marker_path).write_text("running", encoding="utf-8")
            time.sleep(sleep_seconds)
            return Settlement(
                outcome="succeeded",
                receipts=[
                    {
                        "role": "audit",
                        "issuer_component_sha256": issuer_sha,
                        "evidence": {
                            "id": f"ev-{uuid4()}",
                            "kind": "receipt",
                            "content_digest": "abcd1234ef",
                            "trust": "verified",
                        },
                    }
                ],
            )

        def observe(self, worker: Any, job: dict[str, Any]) -> Settlement | None:
            return None

    return DrainHandler()


def test_stub_handler_cancel_requested_signal(native_jobs: Any) -> None:
    """A stub handler waits on worker.cancel_requested while tick runs in a thread;
    cancel_job on that job: signal seen within lease_seconds + 2 s, tick returns,
    job cancelled, no settlement."""
    store = _store(native_jobs)
    cap = f"cancel.sig.a{uuid4().hex}"
    worker_id, comp_sha = _worker(native_jobs, store, [cap])

    class CancelStubHandler:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.seen_cancel = False
            self.elapsed = 0.0

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.started.set()
            t0 = time.monotonic()
            self.seen_cancel = worker.cancel_requested.wait(timeout=20.0)
            self.elapsed = time.monotonic() - t0
            return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement | None:
            return None

    handler = CancelStubHandler()
    worker = JobWorker(
        store,
        {
            "workspace_id": native_jobs.workspace_id,
            "actor_id": native_jobs.actor_id,
            "worker_id": worker_id,
            "component_sha256": comp_sha,
            "capabilities": [cap],
            "capacity": 1,
        },
        {"*": handler},
    )

    lease_seconds = 2
    enqueued = _enqueue(
        native_jobs,
        store,
        required_capabilities=[cap],
        lease_seconds=lease_seconds,
    )
    job_id = enqueued["job"]["job_id"]

    tick_result: str | None = None
    tick_error: BaseException | None = None

    def _run_tick() -> None:
        nonlocal tick_result, tick_error
        try:
            tick_result = worker.tick()
        except BaseException as exc:
            tick_error = exc

    thread = threading.Thread(target=_run_tick)
    thread.start()

    assert handler.started.wait(timeout=5.0), "handler did not start in time"

    # cancel_job while handler is waiting on worker.cancel_requested
    cancel_res = _cancel(native_jobs, store, job_id=job_id, reason="test-cancel-signal")
    assert cancel_res["status"] == "applied"

    thread.join(timeout=30.0)
    assert not thread.is_alive(), "worker.tick thread hung"
    assert tick_error is None
    assert tick_result == job_id

    # Signal seen within lease_seconds + 2 s
    assert handler.seen_cancel is True
    assert handler.elapsed <= lease_seconds + 2.0

    # Job is cancelled, with no settlement
    job_row = _job(native_jobs, job_id)
    assert job_row is not None
    assert job_row["status"] == "cancelled"
    assert job_row["cancel_reason"] == "test-cancel-signal"
    assert job_row["settlement"] is None

    events = _events(native_jobs, job_id)
    assert "settled" not in events
    assert "cancelled" in events


def test_uncancelled_job_seals_normally_event_unset(native_jobs: Any) -> None:
    """An uncancelled job seals normally, its event unset."""
    store = _store(native_jobs)
    cap = f"normal.sig.a{uuid4().hex}"
    worker_id, comp_sha = _worker(native_jobs, store, [cap])

    class NormalStubHandler:
        def __init__(self) -> None:
            self.event_was_set = None

        def run(self, worker: JobWorker, job: dict[str, Any]) -> Settlement:
            self.event_was_set = worker.cancel_requested.is_set()
            return Settlement(outcome="succeeded", receipts=[_receipt(native_jobs)])

        def observe(self, worker: JobWorker, job: dict[str, Any]) -> Settlement | None:
            return None

    handler = NormalStubHandler()
    worker = JobWorker(
        store,
        {
            "workspace_id": native_jobs.workspace_id,
            "actor_id": native_jobs.actor_id,
            "worker_id": worker_id,
            "component_sha256": comp_sha,
            "capabilities": [cap],
            "capacity": 1,
        },
        {"*": handler},
    )

    enqueued = _enqueue(
        native_jobs,
        store,
        required_capabilities=[cap],
        lease_seconds=30,
    )
    job_id = enqueued["job"]["job_id"]

    tick_res = worker.tick()
    assert tick_res == job_id

    assert handler.event_was_set is False
    assert worker.cancel_requested.is_set() is False

    job_row = _job(native_jobs, job_id)
    assert job_row is not None
    assert job_row["status"] == "sealed"
    assert job_row["settlement"] is not None
    assert job_row["settlement"]["outcome"] == "succeeded"

    events = _events(native_jobs, job_id)
    assert "settled" in events


def test_drain_real_process_sigterm(native_jobs: Any, tmp_path: Path) -> None:
    """Drain: run_worker as a real process with one long job; SIGTERM mid-job:
    that job settles, exit 0, a second backlog job stays unclaimed."""
    store = _store(native_jobs)
    ops_config: OperationsConfig = native_jobs.service.config
    cap = "compute.cpu"

    comp_sha = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"drain-proc-{uuid4()}",
        capabilities=(cap,),
    )
    worker_id = f"w-drain-{uuid4()}"

    marker_file = tmp_path / "job_running_marker"

    config_data = {
        "worker_id": worker_id,
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "component_sha256": comp_sha,
        "operations": {
            "config_dir": str(ops_config.config_dir),
            "state_dir": str(ops_config.state_dir),
            "data_dir": str(ops_config.data_dir),
            "database": ops_config.database,
            "host": ops_config.host,
            "port": ops_config.port,
            "aws_profile": ops_config.aws_profile,
            "aws_region": ops_config.aws_region,
            "bucket": ops_config.bucket,
            "prefix": ops_config.prefix,
            "endpoint_url": ops_config.endpoint_url,
        },
        "handlers": {
            cap: {
                "factory": "test_native_jobs_cancel_signal:drain_handler_factory",
                "options": {
                    "marker_path": str(marker_file),
                    "issuer_sha": native_jobs.components["audit"],
                    "sleep_seconds": 1.2,
                },
            }
        },
        "capabilities": [cap],
        "idle_sleep": 0.05,
    }
    config_file = tmp_path / "worker_config.json"
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    # Enqueue two jobs: job1 will be claimed and run long; job2 remains in backlog
    job1 = _enqueue(
        native_jobs,
        store,
        required_capabilities=[cap],
        lease_seconds=60,
    )
    job1_id = job1["job"]["job_id"]

    job2 = _enqueue(
        native_jobs,
        store,
        required_capabilities=[cap],
        lease_seconds=60,
    )
    job2_id = job2["job"]["job_id"]

    child_script = """
import sys
from omp_work.jobs.process import run_worker

sys.exit(run_worker(sys.argv[1]))
"""
    proc = _run_child(child_script, str(config_file))

    # Wait until job1 is executing (mid-job)
    deadline = time.monotonic() + 10.0
    while not marker_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker_file.exists(), "job1 never started running"

    # SIGTERM mid-job: kill only your PID
    os.kill(proc.pid, signal.SIGTERM)

    rc, stdout, stderr = _wait_child(proc, timeout=20.0)
    assert rc == 0, f"child exited {rc}, stderr: {stderr}"

    # That job settles
    job1_row = _job(native_jobs, job1_id)
    assert job1_row is not None
    assert job1_row["status"] == "sealed"
    assert job1_row["settlement"] is not None
    assert job1_row["settlement"]["outcome"] == "succeeded"

    # A second backlog job stays unclaimed
    job2_row = _job(native_jobs, job2_id)
    assert job2_row is not None
    assert job2_row["status"] == "backlog"
    assert job2_row["worker_id"] is None
