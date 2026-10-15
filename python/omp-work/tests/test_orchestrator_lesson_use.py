"""Worker delivery records supplies, independently of exit status or lesson use.

A delivered lesson's supply rows survive every later stage: a normal audited
close, and the two audit resume variants crash injects at ``seal_audit_manifest``
(``audit_ready``) and ``reserve_auditor_launch`` (``auditor_in_flight``) leave
behind. The worker is never relaunched and never re-delivers, the original
supply ids, count, lesson version, job, and hashes stay put, and neither an
uncited delivery nor the audited close infers a use or an outcome.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

import httpx
import pytest

from omp_work import egress_sandbox
from omp_work.orchestrator import model_proxy_client, operations, worker_sandbox
from omp_work.v1.canonical import sha256
from orchestrator_e2e_support import SimulatedCrash, crash_after_command, tick_until
from test_orchestrator_audit_stage import (
    _attempts,
    _expire_lease,
    _launches,
    _outcomes,
    _steps,
    _tick_to_push,
)
from test_orchestrator_worker_wiring import wiring, run
from test_orchestrator_worker_context import context, token_counter, enable_learning
from test_workflow_service import OWNER

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)

PROMPT_HASH = "a" * 64


def configure_context_cli(config, settings):
    """Configure the real context CLI, also usable when reopening a worker."""
    return dataclasses.replace(config, context_command=(sys.executable, "-m", "omp_knowledge.context"),
                               context_settings_path=settings)


def configure_learning_cli(config, learning_dir, bearer):
    """Restore owner-enabled learning configuration for a resumed worker."""
    return dataclasses.replace(config, learning_state_dir=learning_dir,
                               learning_command=(sys.executable, "-m", "omp_knowledge.learning"),
                               learning_bearer_file=bearer)


@pytest.fixture
def delivery(context, tmp_path, monkeypatch):
    state = context
    state.learning_dir = enable_learning(state, tmp_path)
    handler = state.worker.handlers["*"]
    handler.config = configure_learning_cli(
        configure_context_cli(handler.config, state.settings),
        state.learning_dir, handler.config.learning_bearer_file,
    )
    sandbox = worker_sandbox.run_sandboxed
    state.evidence = "valid"

    def delivered(*args, **kwargs):
        code = sandbox(*args, **kwargs)
        manifest = state.runs[-1][3]
        output = kwargs.get("output")
        if output is not None and manifest is not None and "context" in manifest:
            event = {
                "type": "context-delivery",
                "compiled_context_hash": sha256(manifest["context"]),
                "prompt_hash": PROMPT_HASH,
            }
            if state.evidence == "wrong-hash":
                event["compiled_context_hash"] = "b" * 64
            if state.evidence != "absent":
                output.extend((json.dumps(event) + "\n").encode())
            # Ordinary uncited worker output must not become a lesson use.
            output.extend(b'{"type":"message","text":"Completed work"}\n')
        return code

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", delivered)
    return state


def learning_rows(state, table):
    with sqlite3.connect(state.learning_dir / "learning.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def assert_no_uses_or_outcomes(state):
    assert learning_rows(state, "uses") == []
    assert learning_rows(state, "outcomes") == []


def assert_cleanup(state):
    assert not Path(state.runs[0][1]["OMP_WORKER_MANIFEST"]).exists()
    assert state.revoked == [state.grant["id"]]


@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_delivery_persists_supply_without_inferring_use_or_outcome(delivery, exit_code):
    state = delivery
    state.exit_code = exit_code
    outcome = run(state)
    assert outcome.outcome == ("succeeded" if exit_code == 0 else "crashed"), outcome.data
    if exit_code is None:
        assert outcome.data["code"] == "lease_dead"
    ctx = state.contexts[0]
    manifest = state.runs[0][3]
    compiled = json.loads(state.compiles[0][2].stdout)
    workflow = ctx.service.read(ctx.principal, ctx.workspace_id, "workflow", operations._work_key(ctx))
    assert manifest["candidate_id"] == str(workflow["item"]["candidate"]["candidate_id"])
    rows = learning_rows(state, "supplies")
    proposed = {row["supply_id"]: row for row in compiled["lesson_supplies"]}
    assert proposed
    assert {row["supply_id"] for row in rows} == set(compiled["lesson_supply_ids"]) == set(proposed)
    assert len(rows) == len(proposed)
    for row in rows:
        source = proposed[row["supply_id"]]
        assert row["procedure_id"] == source["procedure_id"] == "context-lesson"
        assert row["version"] == source["version"] == 1
        assert row["work_key"] == source["work_key"] == operations._work_key(ctx)
        assert row["worker_job_id"] == str(ctx.lease.job_id)
        assert row["compiled_context_hash"] == sha256(manifest["context"]) == outcome.data["compiled_context_hash"]
        assert row["prompt_hash"] == PROMPT_HASH
    assert outcome.data["prompt_hash"] == PROMPT_HASH
    recorded = next(step for step in state.worker.handlers["*"].steps()
                    if step["kind"] == "outcome" and step["stage"] == "implement")
    assert recorded["data"]["prompt_hash"] == PROMPT_HASH
    assert recorded["data"]["lesson_supply_ids"] == list(compiled["lesson_supply_ids"])
    assert_no_uses_or_outcomes(state)
    assert_cleanup(state)


@pytest.mark.parametrize("evidence", ["absent", "wrong-hash"])
def test_invalid_delivery_evidence_never_records_supply(delivery, evidence):
    delivery.evidence = evidence
    outcome = run(delivery)
    assert outcome.outcome == "failed"
    assert outcome.data["code"] == "context_delivery_failed"
    assert "prompt_hash" not in outcome.data
    assert learning_rows(delivery, "supplies") == []
    assert_no_uses_or_outcomes(delivery)
    assert_cleanup(delivery)


def test_compile_failure_never_registers_or_launches_or_records_supply(delivery):
    delivery.settings.write_text("{}")
    outcome = run(delivery)
    assert outcome.outcome == "failed"
    assert outcome.data["code"] == "context_compile_failed"
    assert delivery.posts == []
    assert delivery.runs == []
    assert learning_rows(delivery, "supplies") == []
    assert_no_uses_or_outcomes(delivery)


def test_register_failure_after_compile_never_records_unlaunched_supply(delivery, monkeypatch):
    def unavailable(*args, **kwargs):
        delivery.posts.append(args[2])
        raise httpx.TransportError("register unavailable")

    monkeypatch.setattr(model_proxy_client, "register", unavailable)
    outcome = run(delivery)
    assert outcome.outcome == "failed"
    assert outcome.data["code"] == "proxy_unavailable"
    compiled = json.loads(delivery.compiles[0][2].stdout)
    assert compiled["lesson_supply_ids"]
    assert len(delivery.posts) == 1
    assert delivery.runs == []
    assert delivery.revoked == []
    assert learning_rows(delivery, "supplies") == []
    assert_no_uses_or_outcomes(delivery)


def test_unconfigured_context_keeps_worker_output_uncaptured(delivery):
    handler = delivery.worker.handlers["*"]
    handler.config = dataclasses.replace(
        handler.config, context_command=(), context_settings_path=None,
        learning_command=(), learning_state_dir=None, learning_bearer_file=None,
    )
    outcome = run(delivery)
    assert outcome.outcome == "succeeded"
    assert delivery.runs[0][2]["output"] is None
    assert "context" not in delivery.runs[0][3]
    assert "prompt_hash" not in outcome.data
    assert learning_rows(delivery, "supplies") == []
    assert_no_uses_or_outcomes(delivery)
    assert_cleanup(delivery)


@pytest.fixture
def audited(context, tmp_path, monkeypatch):
    """A world that reaches a real audited close with one uncited delivery.

    ``run_sandboxed`` is intercepted only for worker launches that name
    ``OMP_WORKER_MANIFEST``: each emits a valid context-delivery event, writes
    the permitted ``feature.txt`` into ``kwargs["worktree"]``, and returns 0.
    Every other call — the evaluate test command and the verifier — passes
    through to the pristine ``egress_sandbox.run_sandboxed``, so the verifier
    really runs and writes its own signed verdict and report. The worker argv
    is absolute and names ``orchestrator-worker`` (from ``wiring``); the real
    context and learning CLIs are wired, so the delivered lesson is a supply.
    """
    state = context
    state.learning_dir = enable_learning(state, tmp_path)
    handler = state.worker.handlers["*"]
    handler.config = configure_learning_cli(
        configure_context_cli(handler.config, state.settings),
        state.learning_dir, handler.config.learning_bearer_file,
    )
    real_sandbox = egress_sandbox.run_sandboxed
    state.deliveries = []

    def intercept(argv, identity, recorder, workdir, env, timeout, **kwargs):
        manifest_path = env.get("OMP_WORKER_MANIFEST")
        if manifest_path is None:
            return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)
        manifest = json.loads(Path(manifest_path).read_text())
        state.deliveries.append((tuple(argv), manifest))
        output = kwargs["output"]
        output.extend((json.dumps({
            "type": "context-delivery",
            "compiled_context_hash": sha256(manifest["context"]),
            "prompt_hash": PROMPT_HASH,
        }) + "\n").encode())
        # Ordinary uncited worker output must not become a lesson use.
        output.extend(b'{"type":"message","text":"Completed work"}\n')
        (Path(kwargs["worktree"]) / "feature.txt").write_text("feature implemented\n")
        return 0

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", intercept)
    return state


def supply_snapshot(state):
    keys = ("supply_id", "procedure_id", "version", "work_key",
            "worker_job_id", "compiled_context_hash", "prompt_hash")
    rows = learning_rows(state, "supplies")
    return [{key: row[key] for key in keys} for row in sorted(rows, key=lambda row: row["supply_id"])]


def mission_work_id(world):
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    return UUID(str(next(row for row in tree["items"] if row.get("work_id"))["work_id"]))


def implement_job(world):
    """The implement step's job id, the one the delivered supply is bound to."""
    step = next(
        step for step in _steps(world)
        if step.get("stage") == "implement" and step.get("kind") == "outcome"
    )
    return f"orch:{world.mission_id}:{step['step_index']}"


def tick_to_audited(world, worker):
    def audited(_view):
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=25)


@pytest.mark.parametrize("variant", ["normal", "audit_ready", "auditor_in_flight"])
def test_supplies_survive_audited_close_and_audit_resume(audited, variant, monkeypatch):
    """A delivered supply survives the audited close and both audit resume variants.

    The close never re-launches the worker or re-delivers: the original supply
    id, count, lesson version, job, and hashes stay put, and the uncited lesson
    gets no use or outcome. ``audit_ready`` crashes after ``seal_audit_manifest``
    and ``auditor_in_flight`` after ``reserve_auditor_launch``; each resume
    settles an audited PASS, and the in-flight one reserves a replacement launch
    while the original launch row stays.
    """
    state = audited
    world, worker = state.world, state.worker
    _tick_to_push(world, worker)
    work_id = mission_work_id(world)
    job_id = implement_job(world)
    before = supply_snapshot(state)
    assert len(before) == len(state.deliveries) == 1

    if variant == "normal":
        tick_to_audited(world, worker)
    else:
        command_type = {
            "audit_ready": "seal_audit_manifest",
            "auditor_in_flight": "reserve_auditor_launch",
        }[variant]
        crash_after_command(monkeypatch, command_type)
        with pytest.raises(SimulatedCrash):
            worker.tick()
        _expire_lease(world)
        tick_to_audited(world, worker)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "succeeded"
    assert audit_outcome["verdict"] == "pass"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["state"] == "audited"
    assert attempt["accepted_report_count"] == 1
    launches = _launches(world, work_id)
    if variant == "auditor_in_flight":
        # The resume cancels the in-flight launch and reserves a replacement;
        # the original launch row is retained.
        assert attempt["launch_count"] == 2
        assert attempt["cancelled_launch_count"] == 1
        assert len(launches) == 2
    else:
        assert attempt["launch_count"] == 1
        assert attempt["cancelled_launch_count"] == 0
        assert len(launches) == 1

    # One worker launch, one delivery: the close never re-ran or re-delivered.
    assert len(state.deliveries) == 1
    assert supply_snapshot(state) == before
    for row in before:
        assert row["procedure_id"] == "context-lesson"
        assert row["version"] == 1
        assert row["worker_job_id"] == job_id
        assert row["prompt_hash"] == PROMPT_HASH
    assert_no_uses_or_outcomes(state)
