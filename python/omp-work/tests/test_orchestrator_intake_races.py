"""Independent intake appends fence plan recording and the next stage's effects."""
from __future__ import annotations

import os

import psycopg
import pytest

from omp_work.orchestrator import operations, service as orchestrator
from orchestrator_e2e_support import tick_until
from test_orchestrator_intake_hold import _binding, _outcomes, _scope
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
from test_workflow_service import _command
from typed_intake_support import advance_revision, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture(autouse=True)
def bounded_database_and_registry(monkeypatch):
    # The helper opens and closes a separate connection for each append. Bound
    # database waits too, so a regression cannot leave this race test hanging.
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "10")
    monkeypatch.setenv(
        "PGOPTIONS", f"{os.environ.get('PGOPTIONS', '')} -c statement_timeout=15000 -c lock_timeout=10000",
    )
    saved = dict(orchestrator._REGISTRY)
    yield
    orchestrator._REGISTRY.clear()
    orchestrator._REGISTRY.update(saved)


def _recorded(world, stage):
    handler = orchestrator.OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return _outcomes({"steps": handler.steps()}, stage)


def test_append_before_plan_returns_holds_old_binding_then_pass_rebinds(
    service, tmp_path, monkeypatch,
):
    original = operations.run_plan
    admitted = []

    def plan_then_append(ctx):
        outcome = original(ctx)
        assert outcome.outcome == "succeeded"
        admitted.append(_binding(world))
        # advance_revision uses its own committed transaction, on a second
        # connection, after real admission and before _run_stage records success.
        advance_revision(world.store, **_scope(world))
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(operations, "run_plan", plan_then_append)
        world, worker, _work_id = _open_front(
            service, tmp_path, patch, extra_capability=None,
        )
        tick_until(worker, world, lambda v: bool(_outcomes(v, "plan")), limit=20)
    operations.register()

    assert len(admitted) == 1
    bound = admitted[0]
    assert bound.state == "admitted"
    plan = _recorded(world, "plan")
    assert len(plan) == 1
    assert plan[0]["outcome"] == "failed"
    assert plan[0]["data"]["code"] == "intake_revision_advanced"
    held = _binding(world)
    assert (held.binding_id, held.state, held.hold_code, held.revision, held.assessment_id) == (
        bound.binding_id, "held", "intake_revision_advanced", bound.revision, bound.assessment_id,
    )
    assert _begin_rows(world) == _grant_rows(world) == []
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND mission_id=%s",
            (world.workspace_id, world.mission_id),
        ).fetchone() == (bound.revision + 1,)
        # Refusing the outcome does not undo admit_mission's committed effects.
        assert conn.execute(
            "SELECT status FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
            (world.workspace_id, world.mission_id),
        ).fetchone() == ("approved",)

    receipt = record_pass(world.store, **_scope(world))
    # Plan has no automatic retries. Resume its existing policy decision through
    # the real service; a new PASS does not bypass that owner gate.
    handler = orchestrator.OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    pause = next(step for step in handler.steps() if step.get("rule_id") == "retry-bound")
    status_code, body = _command(service, world.workspace_id, {
        "type": "answer_decision",
        "payload": {"decision_id": pause["decision_id"], "answer": "resume"},
    })
    assert status_code == 200, body
    tick_until(worker, world, lambda v: len(_outcomes(v, "plan")) == 2, limit=20)
    rebound = _binding(world)
    assert rebound.binding_id != bound.binding_id
    assert (rebound.state, rebound.revision, rebound.assessment_id) == (
        "admitted", bound.revision + 1, receipt,
    )
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT mission_id,state,revision,assessment_id FROM omp_work.mission_intake_bindings "
            "WHERE binding_id=%s", (bound.binding_id,),
        ).fetchone() == (world.mission_id, "held", bound.revision, bound.assessment_id)
        assert conn.execute(
            "SELECT mission_id FROM omp_work.mission_intake_bindings WHERE binding_id=%s",
            (rebound.binding_id,),
        ).fetchone() == (world.mission_id,)
    tick_until(worker, world, lambda v: bool(_outcomes(v, "grant")), limit=20)
    assert _recorded(world, "plan")[0] == plan[0]
    assert _recorded(world, "plan")[-1]["outcome"] == "succeeded"
    assert _recorded(world, "grant")[-1]["outcome"] == "succeeded"
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1


def test_append_after_recorded_plan_success_refuses_next_tick_before_grant(
    service, tmp_path, monkeypatch,
):
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda v: bool(_outcomes(v, "plan")), limit=20)
    plan = _recorded(world, "plan")
    assert [step["outcome"] for step in plan] == ["succeeded"]
    bound = _binding(world)
    assert bound.state == "admitted"
    assert _begin_rows(world) == _grant_rows(world) == []

    advance_revision(world.store, **_scope(world))
    worker.tick()
    grant = _recorded(world, "grant")
    assert len(grant) == 1
    assert grant[0]["outcome"] == "failed"
    assert grant[0]["data"]["code"] == "intake_revision_advanced"
    assert _recorded(world, "plan") == plan
    held = _binding(world)
    assert (held.binding_id, held.state, held.hold_code, held.revision, held.assessment_id) == (
        bound.binding_id, "held", "intake_revision_advanced", bound.revision, bound.assessment_id,
    )
    assert _begin_rows(world) == _grant_rows(world) == []
