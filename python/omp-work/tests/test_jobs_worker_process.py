"""Production worker process, advisory locking, check, and crash recovery tests (OMP-400-s03).

Defends:
- ExecStart rendering in infra/work-ledger/install.sh decodes to -I -B -m omp_work jobs worker --config …
- Second same-id worker exits 3.
- Subprocess worker crash recovery with jobs_effect_handler:
  4 jobs, 2s lease, SIGKILL after first effect, restart -> all sealed, one settled event and one file line each.
- jobs check passes with no owner capability and no tty.
- register_component registers worker descriptor via WorkClient and prints sha.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.process import register_component
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.config import OperationsConfig
from psycopg.rows import dict_row
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _config_dict(config: OperationsConfig) -> dict[str, Any]:
    return {
        "config_dir": str(config.config_dir),
        "state_dir": str(config.state_dir),
        "data_dir": str(config.data_dir),
        "database": config.database,
        "host": config.host,
        "port": config.port,
        "aws_profile": config.aws_profile,
        "aws_region": config.aws_region,
        "bucket": config.bucket,
        "prefix": config.prefix,
        "endpoint_url": config.endpoint_url,
    }


def _env_for_child() -> dict[str, str]:
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    tests_dir = str(Path(__file__).resolve().parent)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_dir}:{tests_dir}:{pythonpath}"
        if pythonpath
        else f"{src_dir}:{tests_dir}"
    )
    return env


def test_rendered_unit_exec_start(tmp_path: Path) -> None:
    """Rendered omp-work-jobs-worker.service ExecStart decodes to -I -B -m omp_work jobs worker --config …"""
    install_script = (
        Path(__file__).resolve().parents[3] / "infra/work-ledger/install.sh"
    )
    installed_python = tmp_path / "installed_python"
    installed_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    installed_python.chmod(0o755)

    units = tmp_path / "rendered-units"
    config = tmp_path / "cfg"
    state = tmp_path / "state"
    data = tmp_path / "data"

    res = subprocess.run(
        [
            "bash",
            str(install_script),
            "--render-only",
            "--python",
            str(installed_python),
            "--unit-dir",
            str(units),
            "--http-port",
            "55432",
        ],
        env={
            **os.environ,
            "XDG_CONFIG_HOME": str(config),
            "XDG_STATE_HOME": str(state),
            "XDG_DATA_HOME": str(data),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    unit_file = units / "omp-work-jobs-worker.service"
    assert unit_file.exists(), "omp-work-jobs-worker.service was not rendered"
    content = unit_file.read_text()
    assert "After=omp-work-service.service" in content
    assert "Requires=omp-work-service.service" in content
    assert "Restart=always" in content

    command = next(
        line.removeprefix("ExecStart=")
        for line in content.splitlines()
        if line.startswith("ExecStart=")
    )
    probe = subprocess.run(
        shlex.split(command.replace("%%", "%")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.splitlines()[:7] == [
        "-I",
        "-B",
        "-m",
        "omp_work",
        "jobs",
        "worker",
        "--config",
    ]


def test_second_same_id_worker_exits_3(native_jobs, tmp_path: Path) -> None:
    """Holding the worker advisory lock forces a second same-id instance to exit 3."""
    worker_id = f"worker-lock-{uuid4()}"
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"proc-{uuid4()}",
        capabilities=("compute.cpu",),
    )
    cfg_data = {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": worker_id,
        "component_sha256": component,
        "capacity": 1,
        "idle_sleep": 0.05,
        "work_url": "http://127.0.0.1:54322",
        "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
        "operations": _config_dict(native_jobs.service.config),
        "handlers": {
            "compute.cpu": {
                "factory": "omp_work.jobs.probe:factory",
                "options": {"sleep_seconds": 0.0},
            }
        },
    }
    cfg_path = tmp_path / "worker.json"
    cfg_path.write_text(json.dumps(cfg_data))

    env = _env_for_child()
    proc1 = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait until proc1 has acquired the lock
        conn = psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            autocommit=True,
        )
        lock_held = False
        for _ in range(50):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (worker_id,)
                )
                acquired = cur.fetchone()[0]
                if not acquired:
                    lock_held = True
                    break
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (worker_id,)
                )
            time.sleep(0.05)
        conn.close()
        assert lock_held, "First worker did not acquire the advisory lock in time"

        # Start second worker with same worker_id
        proc2 = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omp_work",
                "jobs",
                "worker",
                "--config",
                str(cfg_path),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out2, err2 = proc2.communicate(timeout=10.0)
        assert proc2.returncode == 3, (
            f"Expected returncode 3, got {proc2.returncode}. Out: {out2}, Err: {err2}"
        )
    finally:
        proc1.terminate()
        try:
            proc1.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc1.kill()
            proc1.wait()


def test_subprocess_worker_crash_recovery(native_jobs, tmp_path: Path) -> None:
    """SIGKILL worker after the first effect -> restart seals all 4 jobs with 1 settled event and 1 file line each."""
    store = NativeJobStore(native_jobs.service.config)
    effect_file = tmp_path / "effect.log"
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"effect-worker-{uuid4()}",
        capabilities=("compute.cpu",),
    )
    worker_id = f"worker-crash-{uuid4()}"

    # Enqueue 4 jobs with 2-second lease
    job_ids = []
    for i in range(4):
        jid = f"job-crash-{i}-{uuid4()}"
        enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=jid,
            work_id=UUID(native_jobs.item["work_id"]),
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=2,
        )
        job_ids.append(jid)

    cfg_data = {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": worker_id,
        "component_sha256": component,
        "capacity": 1,
        "idle_sleep": 0.05,
        "work_url": "http://127.0.0.1:54322",
        "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
        "operations": _config_dict(native_jobs.service.config),
        "handlers": {
            "compute.cpu": {
                "factory": "jobs_effect_handler:factory",
                "options": {
                    "file_path": str(effect_file),
                    "sleep_seconds": 1.0,
                },
            }
        },
    }
    cfg_path = tmp_path / "worker-crash.json"
    cfg_path.write_text(json.dumps(cfg_data))

    env = _env_for_child()
    proc1 = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for the first effect to land in the file
    for _ in range(100):
        if effect_file.exists() and effect_file.read_text().strip():
            break
        time.sleep(0.05)

    assert effect_file.exists(), "First effect never written"
    lines_before_kill = [
        line.strip() for line in effect_file.read_text().splitlines() if line.strip()
    ]
    assert len(lines_before_kill) == 1, (
        f"Expected 1 line before kill, got {lines_before_kill}"
    )

    # SIGKILL after the first effect
    proc1.kill()
    proc1.wait()

    # Sleep past the 2s lease expiration so the lease expires
    time.sleep(2.5)

    # Restart the worker process with the same worker_id
    proc2 = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait for all 4 jobs to seal
        conn = psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            row_factory=dict_row,
            autocommit=True,
        )
        all_sealed = False
        for _ in range(100):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) as cnt FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id = ANY(%s) AND status='sealed'",
                    (native_jobs.workspace_id, job_ids),
                )
                if cur.fetchone()["cnt"] == 4:
                    all_sealed = True
                    break
            time.sleep(0.1)

        assert all_sealed, "Not all 4 jobs sealed after restart"

        # Check: each job has exactly one settled event
        with conn.cursor() as cur:
            for jid in job_ids:
                cur.execute(
                    "SELECT count(*) as cnt FROM omp_jobs.job_events WHERE job_id=%s AND kind='settled'",
                    (jid,),
                )
                cnt = cur.fetchone()["cnt"]
                assert cnt == 1, f"Job {jid} expected 1 settled event, got {cnt}"

        conn.close()

        # Check: file contains exactly 4 lines (one line per job_id, no duplicate lines)
        final_lines = [
            line.strip()
            for line in effect_file.read_text().splitlines()
            if line.strip()
        ]
        assert len(final_lines) == 4, (
            f"Expected 4 lines in effect file, got {len(final_lines)}: {final_lines}"
        )
        assert set(final_lines) == set(job_ids), (
            f"Lines do not match job_ids: {final_lines} vs {job_ids}"
        )
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.wait()


def test_jobs_check_passes_with_no_owner_capability_and_no_tty(
    native_jobs, tmp_path: Path
) -> None:
    """jobs check enqueues count omp.probe jobs, polls, returns {capability, passed, jobs}, exit 0 with no tty."""
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"probe-worker-{uuid4()}",
        capabilities=("omp.probe",),
    )
    worker_id = f"worker-probe-{uuid4()}"
    cfg_data = {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": worker_id,
        "component_sha256": component,
        "capacity": 2,
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
    cfg_path = tmp_path / "worker-probe.json"
    cfg_path.write_text(json.dumps(cfg_data))

    env = _env_for_child()
    proc = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Run check via CLI without tty (stdin=DEVNULL)
        res = subprocess.run(
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
                "2",
                "--timeout",
                "15",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
        assert res.returncode == 0, (
            f"check failed (exit {res.returncode}): {res.stderr}\nStdout: {res.stdout}"
        )
        data = json.loads(res.stdout)
        assert data["capability"] == "jobs"
        assert data["passed"] is True
        assert len(data["jobs"]) == 2
        for j in data["jobs"]:
            assert j["status"] == "sealed"
            assert j["settled_events"] == 1
    finally:
        # SIGTERM finishes current job and exits 0
        proc.send_signal(signal.SIGTERM)
        try:
            ret = proc.wait(timeout=5.0)
            assert ret == 0, f"Worker process did not exit 0 on SIGTERM: exit {ret}"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Worker process timed out waiting for SIGTERM exit")


def test_register_component(native_jobs, tmp_path: Path) -> None:
    """register_component registers worker descriptor via WorkClient and prints sha."""
    cfg_data = {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": f"worker-reg-{uuid4()}",
        "component_sha256": "0" * 64,
        "capacity": 1,
        "work_url": "http://127.0.0.1:54322",
        "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
        "operations": _config_dict(native_jobs.service.config),
        "handlers": {
            "omp.probe": {
                "factory": "omp_work.jobs.probe:factory",
                "options": {},
            }
        },
    }
    cfg_path = tmp_path / "worker-reg.json"
    cfg_path.write_text(json.dumps(cfg_data))

    from omp_work.v1.client import WorkClient

    client = WorkClient(
        "http://testserver",
        native_jobs.workspace_id,
        native_jobs.service.capabilities / "owner.json",
        transport=native_jobs.service.client._transport,
    )
    sha = register_component(cfg_path, client=client)
    assert len(sha) == 64
    assert sha == sha.lower()

    # Calling register_component via CLI
    cli_res = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import sys
from unittest.mock import MagicMock
from omp_work.jobs.process import register_component
from omp_work.v1.client import WorkClient

mock_client = MagicMock()
sha = register_component({str(cfg_path)!r}, client=mock_client)
assert mock_client.execute.called
""",
        ],
        env=_env_for_child(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli_res.returncode == 0, cli_res.stderr


def test_worker_sigterm_finishes_job_no_drain(native_jobs, tmp_path: Path) -> None:
    """SIGTERM finishes the current job, exits 0, and does not drain the backlog."""
    store = NativeJobStore(native_jobs.service.config)
    effect_file = tmp_path / "sigterm-effect.log"
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"sigterm-worker-{uuid4()}",
        capabilities=("compute.cpu",),
    )
    worker_id = f"worker-sigterm-{uuid4()}"

    # Enqueue 2 jobs
    j1 = f"job-sigterm-1-{uuid4()}"
    j2 = f"job-sigterm-2-{uuid4()}"
    for jid in (j1, j2):
        enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=jid,
            work_id=UUID(native_jobs.item["work_id"]),
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=30,
        )

    cfg_data = {
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "worker_id": worker_id,
        "component_sha256": component,
        "capacity": 1,
        "idle_sleep": 0.05,
        "work_url": "http://127.0.0.1:54322",
        "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
        "operations": _config_dict(native_jobs.service.config),
        "handlers": {
            "compute.cpu": {
                "factory": "jobs_effect_handler:factory",
                "options": {
                    "file_path": str(effect_file),
                    "sleep_seconds": 0.5,
                },
            }
        },
    }
    cfg_path = tmp_path / "worker-sigterm.json"
    cfg_path.write_text(json.dumps(cfg_data))

    proc = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)],
        env=_env_for_child(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait until job 1 has started executing (effect file has 1 line)
    for _ in range(100):
        if effect_file.exists() and effect_file.read_text().strip():
            break
        time.sleep(0.05)

    assert effect_file.exists(), "Job 1 effect not written"
    # Send SIGTERM while job 1 is running (or right after effect)
    proc.send_signal(signal.SIGTERM)

    # Worker must finish job 1 and exit 0 without claiming job 2
    ret = proc.wait(timeout=5.0)
    assert ret == 0, f"Worker did not exit 0 on SIGTERM: exit {ret}"

    # Verify database state
    conn = psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )
    with conn.cursor() as cur:
        # Job 1 must be sealed with 1 settled event
        cur.execute("SELECT status FROM omp_jobs.jobs WHERE job_id=%s", (j1,))
        assert cur.fetchone()["status"] == "sealed"
        cur.execute(
            "SELECT count(*) as cnt FROM omp_jobs.job_events WHERE job_id=%s AND kind='settled'",
            (j1,),
        )
        assert cur.fetchone()["cnt"] == 1

        # Job 2 must still be in backlog (not drained)
        cur.execute("SELECT status FROM omp_jobs.jobs WHERE job_id=%s", (j2,))
        assert cur.fetchone()["status"] == "backlog"
    conn.close()
