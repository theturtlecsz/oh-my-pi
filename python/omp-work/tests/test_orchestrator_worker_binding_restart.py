"""The persisted launch binding, and its hold, survive a full service and worker restart."""
from __future__ import annotations

import os
import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.orchestrator import operations, worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import open_worker, tick_until
from test_orchestrator_intake_hold import _binding, _outcomes, _scope
from test_orchestrator_intake_restart import _close_worker, _recreate_service, _stage_outcome
from test_orchestrator_intake_restart import restore_registry  # noqa: F401 - autouse fixture
from test_orchestrator_operations_front import _open_front
from test_workflow_service import _command
from typed_intake_support import advance_revision, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_COLUMNS = (
    "launch_binding_id,intake_binding_id,run_id,revision,assessment_id,plan_receipt_id,"
    "plan_sha256,item_id,work_id,candidate_id,state,hold_code"
)


@pytest.fixture(autouse=True)
def bounded_database(monkeypatch):
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "10")
    monkeypatch.setenv(
        "PGOPTIONS", f"{os.environ.get('PGOPTIONS', '')} -c statement_timeout=15000 -c lock_timeout=10000",
    )


@pytest.fixture
def restartable(service):
    """The module-scoped service, with its client rebuilt after a test closes it."""
    yield service
    service.client.close()
    service.client = TestClient(create_app(
        service.config, capabilities_dir=service.capabilities, store=PostgresWorkStore(service.config),
    ))


def _launch_rows(world):
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM omp_work.mission_launch_bindings "
            "WHERE workspace_id=%s AND mission_id=%s ORDER BY created_at,launch_binding_id",
            (world.workspace_id, world.mission_id),
        ).fetchall()


def _world(restartable, tmp_path, monkeypatch):
    sandbox = []
    outcomes = []
    original = operations.run_implement

    def implement(ctx):
        outcome = original(ctx)
        outcomes.append(outcome)
        return outcome

    def run_sandboxed(*args, **kwargs):
        sandbox.append(args)
        return 0

    monkeypatch.setattr(operations, "run_implement", implement)
    monkeypatch.setattr(worker_sandbox, "run_sandboxed", run_sandboxed)
    world, worker, _work_id = _open_front(restartable, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda v: bool(_outcomes(v, "grant")), limit=40)
    assert _stage_outcome(_handler(world), "grant")["outcome"] == "succeeded"
    return world, worker, sandbox, outcomes


def _handler(world):
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler


def _restart(world, worker):
    _close_worker(worker)
    handler = _recreate_service(world)
    return handler, open_worker(world)


def test_launch_check_passes_from_persisted_rows_after_restart(restartable, tmp_path, monkeypatch):
    world, worker, sandbox, outcomes = _world(restartable, tmp_path, monkeypatch)
    before = _launch_rows(world)
    assert len(before) == 1
    assert before[0][-2:] == ("bound", None)

    handler, worker = _restart(world, worker)
    assert _launch_rows(world) == before
    tick_until(worker, world, lambda v: bool(_outcomes(v, "implement")), limit=40)
    _close_worker(worker)

    assert len(outcomes) == 1
    assert outcomes[0].outcome == "succeeded"
    data = outcomes[0].data
    row = before[0]
    assert data["launch_binding_id"] == str(row[0])
    assert (data["assessment_id"], data["plan_receipt_id"], data["item_id"], data["candidate_id"]) == (
        row[4], row[5], row[7], row[9],
    )
    assert len(sandbox) == 1
    assert _launch_rows(world) == before


def test_held_launch_row_survives_restarts_and_pass_does_not_reauthorize_old_plan(
    restartable, tmp_path, monkeypatch,
):
    world, worker, sandbox, outcomes = _world(restartable, tmp_path, monkeypatch)
    bound = _launch_rows(world)
    assert len(bound) == 1
    admitted = _binding(world)
    _close_worker(worker)

    advance_revision(world.store, **_scope(world))
    held = _launch_rows(world)
    assert [row[:-2] for row in held] == [row[:-2] for row in bound]
    assert held[0][-2:] == ("held", "intake_revision_advanced")

    handler, worker = _restart(world, worker)
    assert _launch_rows(world) == held

    record_pass(world.store, **_scope(world))
    # The refused implement may leave a retry-bound pause; resume it if present.
    tick_until(worker, world, lambda v: bool(_outcomes(v, "implement")), limit=40)
    for _ in range(3):
        pauses = [s for s in handler.steps() if s.get("rule_id") == "retry-bound"]
        if not pauses or len(_outcomes({"steps": handler.steps()}, "implement")) > 1:
            break
        code, body = _command(world.service, world.workspace_id, {
            "type": "answer_decision",
            "payload": {"decision_id": pauses[-1]["decision_id"], "answer": "resume"},
        })
        assert code == 200, body
        handler, worker = _restart(world, worker)
        count = len(_outcomes({"steps": handler.steps()}, "implement"))
        tick_until(worker, world, lambda v: len(_outcomes(v, "implement")) > count, limit=40)
    _close_worker(worker)

    refused = _outcomes({"steps": handler.steps()}, "implement")[-1]
    assert refused["outcome"] == "failed"
    assert refused["data"]["code"] == "launch_binding_held"
    assert sandbox == []
    assert all(o.outcome == "failed" for o in outcomes)
    rebound = _binding(world)
    assert rebound.binding_id != admitted.binding_id
    assert (rebound.state, rebound.revision) == ("admitted", admitted.revision + 1)
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT mission_id FROM omp_work.mission_intake_bindings WHERE binding_id=%s",
            (rebound.binding_id,),
        ).fetchone() == (world.mission_id,)
    assert _launch_rows(world) == held
