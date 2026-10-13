"""Atomic request publication and fresh-submitter recovery (OMP-595-s05)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from omp_work.jobs.admission import claim_job
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.orchestrator import service as orchestrator
from test_jobs_worker_process import _config_dict, _env_for_child
from test_orchestrator_service import _steps, _worker, open_world
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

# Each replay loads capabilities, config and request anew in a separate process.
_SUBMIT = """
import json, os, sys
from contextlib import contextmanager
from pathlib import Path
from omp_work.orchestrator import service as orch
ops = orch._ops_from(json.loads(sys.argv[3]))
config = orch.load_config(Path(sys.argv[1]), ops)
request = orch.load_request(Path(sys.argv[2]))
phase = sys.argv[4]
if phase == 'work_commit':
    execute = orch._execute
    def crash(*args, **kwargs):
        result = execute(*args, **kwargs)
        if args[2] == 'create_work_batch':
            os._exit(71)
        return result
    orch._execute = crash
if phase == 'publication_commit':
    transaction = orch.NativeJobStore.transaction
    append = orch.append_step
    published = False
    def record(*args, **kwargs):
        global published
        result = append(*args, **kwargs)
        published = True
        return result
    @contextmanager
    def lose_response(self, *args, **kwargs):
        with transaction(self, *args, **kwargs) as cur:
            yield cur
        if published:
            os._exit(72)
    orch.append_step = record
    orch.NativeJobStore.transaction = lose_response
print(json.dumps(orch.submit(config, request)))
"""


def _fresh(service, world, phase="normal"):
    return subprocess.run(
        [sys.executable, "-c", _SUBMIT, str(world["config_path"]),
         str(world["request_path"]), json.dumps(_config_dict(service.config)), phase],
        env=_env_for_child(), capture_output=True, text=True, timeout=25,
    )


def _rows(world):
    config = world["config"]
    with NativeJobStore(config.ops).transaction(world["workspace_id"], OWNER) as cur:
        cur.execute(
            "SELECT job_id, work_id, status FROM omp_jobs.jobs WHERE workspace_id=%s",
            (world["workspace_id"],),
        )
        jobs = cur.fetchall()
        cur.execute(
            "SELECT result FROM omp_jobs.operations WHERE operation_id=%s AND workspace_id=%s",
            (str(orchestrator._ids(world["mission_id"], "enqueue:0")), world["workspace_id"]),
        )
        enqueue = cur.fetchone()
        cur.execute(
            "SELECT response FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s",
            (world["workspace_id"], orchestrator._ids(world["mission_id"], "work")),
        )
        work = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS count FROM omp_work.work_items WHERE workspace_id=%s",
            (world["workspace_id"],),
        )
        count = cur.fetchone()["count"]
    return jobs, enqueue, work, count


def _published_once(service, world, result, effect):
    jobs, enqueue, work, count = _rows(world)
    assert count == 1
    assert len(jobs) == 1
    assert str(jobs[0]["work_id"]) == result["work_id"]
    assert jobs[0]["job_id"] == result["job_id"]
    assert work["response"]["items"][0]["work_id"] == result["work_id"]
    assert enqueue["result"]["job"]["job_id"] == result["job_id"]
    steps = _steps(world["config"], world["mission_id"])
    assert len(steps) == 1
    assert steps[0]["request"] == world["request"].document()
    assert steps[0]["work_id"] == result["work_id"]
    assert steps[0]["previous_event_sha256"] is None
    with NativeJobStore(service.config).transaction(world["workspace_id"], OWNER) as cur:
        mission = orchestrator.read_mission(cur, world["workspace_id"], world["mission_id"])
    assert mission["status"] == "running"
    worker = _worker(service, world["config"], world["workspace_id"])
    assert worker.tick()
    assert effect.read_text() == "1"
    handler = orchestrator.OrchestratorHandler(world["config"])
    with handler.store.transaction(world["workspace_id"], OWNER) as cur:
        job = handler.store.job_view(cur, world["workspace_id"], result["job_id"])
    settled = handler.observe(None, job)
    assert settled.outcome == "succeeded"
    assert effect.read_text() == "1"
    steps = _steps(world["config"], world["mission_id"])
    assert sum(step["kind"] == "submitted" for step in steps) == 1
    assert sum(step["kind"] == "external_done" for step in steps) == 1


def test_claimant_cannot_see_request_or_job_before_publication_commit(service, tmp_path, monkeypatch):
    world = open_world(service, tmp_path)
    worker = _worker(service, world["config"], world["workspace_id"])
    appended, release = threading.Event(), threading.Event()
    append = orchestrator.append_step

    def barrier(*args, **kwargs):
        result = append(*args, **kwargs)
        appended.set()
        assert release.wait(10), "publication barrier timed out"
        return result

    monkeypatch.setattr(orchestrator, "append_step", barrier)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(orchestrator.submit, world["config"], world["request"])
        try:
            assert appended.wait(10)
            jobs, operation, work, count = _rows(world)
            assert jobs == [] and operation is None
            assert count == 1 and work is not None
            assert _steps(world["config"], world["mission_id"]) == []
            claim = claim_job(
                NativeJobStore(service.config), operation_id=str(uuid4()),
                workspace_id=world["workspace_id"], actor_id=OWNER,
                worker_id=worker.config.worker_id,
                job_id=orchestrator._stage_job(world["mission_id"], 0),
            )
            assert claim["job"] is None
        finally:
            release.set()
        result = pending.result(timeout=10)
    handler = orchestrator.OrchestratorHandler(world["config"])
    handler._bind(world["mission_id"])
    assert handler._request == world["request"].document()
    claimed = claim_job(
        NativeJobStore(service.config), operation_id=str(uuid4()),
        workspace_id=world["workspace_id"], actor_id=OWNER,
        worker_id=worker.config.worker_id, job_id=result["job_id"],
    )
    assert claimed["job"]["job_id"] == result["job_id"]
    assert str(claimed["job"]["work_id"]) == result["work_id"]


def test_enqueue_rolls_back_when_submitted_append_fails(service, tmp_path, monkeypatch):
    effect = tmp_path / "effect"
    world = open_world(service, tmp_path, controls={"intake": {"effect": str(effect)}})

    def fail(*args, **kwargs):
        # Enqueue already happened on this cursor, but is still unpublished.
        args[1].execute("SELECT status FROM omp_jobs.jobs WHERE job_id=%s", (kwargs["job_id"],))
        assert args[1].fetchone()["status"] == "backlog"
        raise RuntimeError("append failed")

    monkeypatch.setattr(orchestrator, "append_step", fail)
    with pytest.raises(RuntimeError, match="append failed"):
        orchestrator.submit(world["config"], world["request"])
    jobs, enqueue, work, count = _rows(world)
    assert jobs == [] and enqueue is None
    assert work is not None and count == 1
    assert _steps(world["config"], world["mission_id"]) == []
    monkeypatch.undo()
    replay = _fresh(service, world)
    assert replay.returncode == 0, replay.stderr
    _published_once(service, world, json.loads(replay.stdout), effect)


@pytest.mark.parametrize("phase,exit_code", [("work_commit", 71), ("publication_commit", 72)])
def test_crash_replays_exact_work_and_one_effect(service, tmp_path, phase, exit_code):
    effect = tmp_path / "effect"
    world = open_world(service, tmp_path, controls={"intake": {"effect": str(effect)}})
    crashed = _fresh(service, world, phase)
    assert crashed.returncode == exit_code, crashed.stderr
    jobs, enqueue, work, count = _rows(world)
    assert count == 1
    work_id = work["response"]["items"][0]["work_id"]
    if phase == "work_commit":
        assert jobs == [] and enqueue is None
        assert _steps(world["config"], world["mission_id"]) == []
    else:
        assert len(jobs) == 1 and enqueue is not None
    replay = _fresh(service, world)
    assert replay.returncode == 0, replay.stderr
    result = json.loads(replay.stdout)
    assert result["work_id"] == work_id
    _published_once(service, world, result, effect)


def test_concurrent_identical_submits_converge_and_changed_request_conflicts(service, tmp_path, monkeypatch):
    effect = tmp_path / "effect"
    world = open_world(service, tmp_path, controls={"intake": {"effect": str(effect)}})
    start = threading.Barrier(2)

    def submit():
        start.wait(timeout=10)
        return _fresh(service, world)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    for response in responses:
        assert response.returncode == 0, response.stderr
    first, second = [json.loads(response.stdout) for response in responses]
    assert first == second

    def forbidden(*args, **kwargs):
        raise AssertionError("changed submission reached preparation")

    monkeypatch.setattr(orchestrator, "_execute", forbidden)
    monkeypatch.setattr(orchestrator, "_prepare_mission", forbidden)
    with pytest.raises(orchestrator.OrchestratorError) as conflict:
        orchestrator.submit(world["config"], replace(world["request"], target_ref="changed"))
    assert conflict.value.code == "idempotency_conflict"
    monkeypatch.undo()
    _published_once(service, world, first, effect)


def test_changed_work_after_work_commit_is_not_ignored_replay(service, tmp_path):
    world = open_world(service, tmp_path)
    assert _fresh(service, world, "work_commit").returncode == 71
    changed = replace(world["request"], work=world["request"].work.model_copy(update={"title": "different"}))
    with pytest.raises(orchestrator.OrchestratorError) as conflict:
        orchestrator.submit(world["config"], changed)
    assert conflict.value.code == "idempotency_conflict"
    jobs, enqueue, work, count = _rows(world)
    assert jobs == [] and enqueue is None
    assert count == 1 and work is not None


@pytest.mark.parametrize("refusal", ["qualification", "admission"])
def test_refused_submission_publishes_no_runnable_job(service, tmp_path, refusal):
    world = open_world(service, tmp_path, qualification={} if refusal == "qualification" else None)
    if refusal == "qualification":
        first = orchestrator.submit(world["config"], world["request"])
        replay = orchestrator.submit(world["config"], world["request"])
        assert first == replay
        assert first["status"] == "refused" and first["decision_id"] is not None
        with NativeJobStore(service.config).transaction(world["workspace_id"], OWNER) as cur:
            decision = orchestrator.find_decision(cur, world["workspace_id"], first["decision_id"])
        assert decision is not None
    else:
        with pytest.raises(JobError) as refused:
            orchestrator.submit(replace(world["config"], lease_seconds=0), world["request"])
        assert refused.value.code == "invalid_request"
    jobs, enqueue, work, count = _rows(world)
    assert jobs == [] and enqueue is None
    assert work is not None and count == 1
    assert _steps(world["config"], world["mission_id"]) == []


def test_without_owner_submission_leaves_confirmation_to_stage(service, tmp_path):
    world = open_world(service, tmp_path)
    (tmp_path / "owner.json").unlink()
    result = orchestrator.submit(world["config"], world["request"])
    with NativeJobStore(service.config).transaction(world["workspace_id"], OWNER) as cur:
        cur.execute(
            "SELECT command_type FROM omp_control.idempotent_commands WHERE workspace_id=%s",
            (world["workspace_id"],),
        )
        commands = {row["command_type"] for row in cur.fetchall()}
    assert "create_work_batch" in commands
    assert commands.isdisjoint({"submit_mission", "approve_mission", "set_mission_status"})
    handler = orchestrator.OrchestratorHandler(world["config"])
    handler._bind(world["mission_id"])
    assert handler._request["instruction"] == world["request"].instruction.model_dump(mode="json")
    assert _steps(world["config"], world["mission_id"])[0]["work_id"] == result["work_id"]
