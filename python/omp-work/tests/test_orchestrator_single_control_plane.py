"""OMP-417-s05: one orchestrator jobs worker, and parallel_streams stands down.

PostgreSQL integration (``OMP_WORK_POSTGRES_INTEGRATION=1``) for the worker
process. A second jobs worker whose capabilities include ``omp.orchestrator``
exits 3 even with a different worker id. ``parallel_streams.tick`` and
``enqueue`` (and ``claim_one``) raise when ``orchestrator.json`` is present.
An unregistered worker id leases nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from omp_work.jobs.admission import claim_job
from omp_work.jobs.store import JobError
from omp_work.parallel_streams import claim_one, enqueue, tick
from test_jobs_worker_loop import _connect, _enqueue, _store
from test_jobs_worker_process import _config_dict, _env_for_child
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _controller_held(conn, workspace_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('omp_jobs.controller:' || %s, 0))",
            (workspace_id,),
        )
        acquired = cur.fetchone()[0]
        if acquired:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtextextended('omp_jobs.controller:' || %s, 0))",
                (workspace_id,),
            )
            return False
        return True


def test_second_orchestrator_worker_exits_3(native_jobs, tmp_path: Path) -> None:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"orch-{uuid4()}",
        capabilities=("omp.orchestrator",),
    )
    workspace_id = str(native_jobs.workspace_id)

    def write_config(worker_id: str) -> Path:
        body = {
            "workspace_id": workspace_id,
            "actor_id": str(native_jobs.actor_id),
            "worker_id": worker_id,
            "component_sha256": component,
            "capacity": 1,
            "idle_sleep": 0.05,
            "work_url": "http://127.0.0.1:9",
            "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
            "operations": _config_dict(native_jobs.service.config),
            "handlers": {
                "omp.orchestrator": {
                    "factory": "omp_work.jobs.probe:factory",
                    "options": {"sleep_seconds": 0.0},
                }
            },
        }
        path = tmp_path / f"{worker_id}.json"
        path.write_text(json.dumps(body))
        return path

    worker_a = f"orch-a-{uuid4()}"
    worker_b = f"orch-b-{uuid4()}"
    env = _env_for_child()
    proc1 = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(write_config(worker_a))],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    conn = psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        autocommit=True,
    )
    try:
        ready = False
        for _ in range(100):
            if proc1.poll() is not None:
                out, err = proc1.communicate()
                raise AssertionError(
                    f"first orchestrator exited {proc1.returncode}: {out} {err}"
                )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM omp_jobs.workers WHERE worker_id=%s", (worker_a,)
                )
                registered = cur.fetchone() is not None
            if registered and _controller_held(conn, workspace_id):
                ready = True
                break
            time.sleep(0.05)
        assert ready, "first orchestrator did not hold the controller lock"

        proc2 = subprocess.Popen(
            [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(write_config(worker_b))],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out2, err2 = proc2.communicate(timeout=15)
        assert proc2.returncode == 3, (
            f"expected exit 3, got {proc2.returncode}. out={out2} err={err2}"
        )
    finally:
        conn.close()
        if proc1.poll() is None:
            proc1.terminate()
            try:
                proc1.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc1.kill()
                proc1.wait(timeout=5)


def test_unregistered_worker_claims_nothing(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-unregistered-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    missing = f"missing-{uuid4()}"
    with pytest.raises(JobError) as excinfo:
        claim_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id=missing,
        )
    assert excinfo.value.code == "job_worker_unavailable"
    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT status, worker_id FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert row["status"] == "backlog"
    assert row["worker_id"] is None


def test_parallel_streams_refuse_when_orchestrator_config_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_dir = tmp_path / "omp" / "work-ledger"
    config_dir.mkdir(parents=True)
    (config_dir / "orchestrator.json").write_text("{}\n")
    with pytest.raises(RuntimeError, match="control_plane_owns_admission"):
        tick()
    with pytest.raises(RuntimeError, match="control_plane_owns_admission"):
        enqueue(
            job_id="job-refused",
            provider_partition="grok",
            path_lease="python/**",
            expected_max=10,
        )
    with pytest.raises(RuntimeError, match="control_plane_owns_admission"):
        claim_one()
