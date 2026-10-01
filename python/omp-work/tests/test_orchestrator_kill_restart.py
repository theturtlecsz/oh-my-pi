"""SIGKILL after an orchestrator intent, then one recovered side effect (OMP-417-s06)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.orchestrator.service import submit
from test_jobs_worker_process import _config_dict, _env_for_child
from test_orchestrator_service import _steps, open_world
from test_research_contract import _register_component
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _intent(service, workspace_id, mission_id: str) -> dict | None:
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        return conn.execute(
            """
            SELECT payload FROM omp_jobs.job_events
            WHERE kind='orch_step' AND payload->>'mission_id'=%s AND payload->>'kind'='external_intent'
            """,
            (str(mission_id),),
        ).fetchone()


def _start(config_path: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(config_path)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    handle.close()
    return process


def _stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_kill_after_intent_restarts_once(service, tmp_path: Path) -> None:
    block = tmp_path / "block"
    effect = tmp_path / "effect"
    block.write_text("block")
    world = open_world(
        service,
        tmp_path / "world",
        controls={"intake": {"block": str(block), "effect": str(effect)}},
        lease_seconds=2,
    )
    submitted = submit(world["config"], world["request"])
    component = _register_component(
        service,
        world["workspace_id"],
        "worker",
        name=f"orch-{uuid4().hex[:8]}",
        capabilities=("omp.orchestrator",),
    )
    worker_id = f"orch-{uuid4().hex[:8]}"
    ops = _config_dict(service.config)
    body = {
        "workspace_id": str(world["workspace_id"]),
        "actor_id": str(OWNER),
        "worker_id": worker_id,
        "component_sha256": component,
        "capacity": 1024,
        "capabilities": ["omp.orchestrator"],
        "idle_sleep": 0.05,
        "operations": ops,
        "handlers": {
            "omp.orchestrator": {
                "factory": "omp_work.orchestrator.service:factory",
                "options": {"config_path": str(world["config_path"]), "operations": ops},
            }
        },
    }
    config_path = tmp_path / "worker.json"
    config_path.write_text(json.dumps(body))
    env = _env_for_child()
    first = _start(config_path, env, tmp_path / "first.log")
    second: subprocess.Popen | None = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if first.poll() is not None:
                raise AssertionError(f"worker exited {first.returncode}: {(tmp_path / 'first.log').read_text()}")
            if _intent(service, world["workspace_id"], world["mission_id"]) is not None and not effect.exists():
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"intent never recorded: {(tmp_path / 'first.log').read_text()[-2000:]}")
        first.kill()
        first.wait(timeout=5)
        block.unlink()
        time.sleep(3)
        restarted = time.monotonic() + 10
        while True:
            second = _start(config_path, env, tmp_path / "second.log")
            time.sleep(0.2)
            if second.poll() != 3:
                break
            second.wait(timeout=5)
            second = None
            if time.monotonic() >= restarted:
                raise AssertionError("restart kept exiting 3")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if effect.exists() and effect.read_text(encoding="utf-8") == "1":
                steps = _steps(world["config"], world["mission_id"])
                outcomes = [
                    step
                    for step in steps
                    if step.get("kind") == "outcome" and isinstance(step.get("data"), dict) and step["data"].get("operation_id")
                ]
                if len(outcomes) == 1:
                    break
            if second.poll() not in (None, 3):
                raise AssertionError(
                    f"restart exited {second.returncode}: {(tmp_path / 'second.log').read_text()[-2000:]}"
                )
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"effect={effect.read_text() if effect.exists() else None} log={(tmp_path / 'second.log').read_text()[-2000:]}"
            )
    finally:
        _stop(second)
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        if block.exists():
            block.unlink()

    steps = _steps(world["config"], world["mission_id"])
    intents = [step for step in steps if step.get("kind") == "external_intent"]
    dones = [step for step in steps if step.get("kind") == "external_done"]
    outcomes = [
        step
        for step in steps
        if step.get("kind") == "outcome" and isinstance(step.get("data"), dict) and step["data"].get("operation_id")
    ]
    assert len(intents) == 1
    assert len(dones) == 1
    assert len(outcomes) == 1
    operation_id = intents[0]["operation_id"]
    assert dones[0]["operation_id"] == operation_id
    assert outcomes[0]["data"]["operation_id"] == operation_id
    assert effect.read_text(encoding="utf-8") == "1"
    assert submitted["job_id"]
