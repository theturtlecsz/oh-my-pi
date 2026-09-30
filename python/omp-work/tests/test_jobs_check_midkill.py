"""jobs check recovers when the claiming worker is killed mid-probe (OMP-475-s03).

Defends:
- Killing the worker that claimed a ``probe-sleep2000`` job while it is
  admitted leaves the job leased; a second worker under the same worker_id
  reclaims it after the 3 s lease expires, observes (no renewal), and seals it,
  so ``jobs check --lease 3 --sleep 2`` still passes.
- That job keeps lease_seconds 3, and its events are exactly one
  lease_expired, two claimed with the second at least the lease after the
  first, and one settled.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_jobs_worker_process import _config_dict, _env_for_child
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _config(native_jobs) -> dict[str, Any]:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"midkill-worker-{uuid4()}",
        capabilities=("omp.probe",),
    )
    return {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": f"worker-midkill-{uuid4()}",
        "component_sha256": component,
        "capacity": 1,
        "idle_sleep": 0.05,
        "work_url": "http://127.0.0.1:54322",
        "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
        "operations": _config_dict(native_jobs.service.config),
        "handlers": {
            "omp.probe": {
                "factory": "omp_work.jobs.probe:factory",
                "options": {"sleep_seconds": 0.0},
            }
        },
    }


def _worker(cfg_path: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_jobs_check_recovers_after_worker_killed_mid_probe(
    native_jobs, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "worker-midkill.json"
    cfg_path.write_text(json.dumps(_config(native_jobs)))

    env = _env_for_child()
    worker_a = _worker(cfg_path, env)
    worker_b: subprocess.Popen[str] | None = None
    check: subprocess.Popen[str] | None = None
    try:
        check = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omp_work",
                "jobs",
                "check",
                "--config",
                str(cfg_path),
                "--work-id",
                str(native_jobs.item["work_id"]),
                "--count",
                "1",
                "--timeout",
                "30",
                "--lease",
                "3",
                "--sleep",
                "2",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        conn = psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            row_factory=dict_row,
            autocommit=True,
        )
        job_id: str | None = None
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT job_id, status FROM omp_jobs.jobs"
                    " WHERE workspace_id=%s AND job_id LIKE 'probe-%%'",
                    (native_jobs.workspace_id,),
                ).fetchone()
                if row is not None and row["status"] in ("admitted", "in_flight"):
                    job_id = row["job_id"]
                    break
                time.sleep(0.05)
        finally:
            conn.close()
        assert job_id is not None, "probe job never reached admitted/in_flight"

        # Kill the worker holding the lease mid-probe; its advisory lock and
        # connection die with it.
        worker_a.kill()
        worker_a.wait()

        # Restart under the same worker_id. Exit 3 means the dead worker's
        # advisory lock is not yet released, so retry briefly.
        deadline = time.monotonic() + 5.0
        while True:
            worker_b = _worker(cfg_path, env)
            time.sleep(0.2)
            worker_b.poll()
            if worker_b.returncode != 3:
                break
            worker_b.wait()
            worker_b = None
            if time.monotonic() >= deadline:
                pytest.fail("second worker kept exiting 3; advisory lock never released")

        out, err = check.communicate(timeout=35.0)
        assert check.returncode == 0, (
            f"check failed (exit {check.returncode}): {err}\nStdout: {out}"
        )
        data = json.loads(out)
        assert data["capability"] == "jobs"
        assert data["passed"] is True
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == job_id
        assert data["jobs"][0]["status"] == "sealed"
        assert data["jobs"][0]["settled_events"] == 1

        conn = psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            row_factory=dict_row,
            autocommit=True,
        )
        try:
            job = conn.execute(
                "SELECT lease_seconds FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (native_jobs.workspace_id, job_id),
            ).fetchone()
            assert job["lease_seconds"] == 3

            events = conn.execute(
                "SELECT kind, at FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
                (job_id,),
            ).fetchall()
        finally:
            conn.close()

        kinds = [event["kind"] for event in events]
        assert kinds.count("lease_expired") == 1
        claimed = [event["at"] for event in events if event["kind"] == "claimed"]
        assert len(claimed) == 2
        assert claimed[1] - claimed[0] >= timedelta(seconds=3)
        assert kinds.count("settled") == 1
    finally:
        if worker_b is not None:
            if worker_b.poll() is None:
                worker_b.send_signal(signal.SIGTERM)
                try:
                    worker_b.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    worker_b.kill()
                    worker_b.wait()
        if check is not None and check.poll() is None:
            check.kill()
            check.wait()
        if worker_a.poll() is None:
            worker_a.kill()
            worker_a.wait()
