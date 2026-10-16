"""The implement launch verifies its persisted binding before any worker or model grant."""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.orchestrator import intake_binding, intake_gate, operations, worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler
from orchestrator_e2e_support import SimulatedCrash, crash_after_command, tick_until
from test_orchestrator_intake_race import _binding, _scope
from test_orchestrator_operations_front import _expire_lease, _open_front
from test_workflow_service import OWNER
from typed_intake_support import record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def launch(service, tmp_path, monkeypatch):
    original = operations.run_implement
    state = type("State", (), {"outcomes": [], "sandbox": [], "before": None})()

    def implement(ctx):
        if state.before is not None:
            state.before(ctx)
        outcome = original(ctx)
        state.outcomes.append(outcome)
        return outcome

    def sandbox(*args, **kwargs):
        state.sandbox.append(args)
        return 0

    monkeypatch.setattr(operations, "run_implement", implement)
    monkeypatch.setattr(worker_sandbox, "run_sandboxed", sandbox)
    state.world, state.worker, _work_id = _open_front(
        service, tmp_path, monkeypatch, extra_capability=None
    )
    return state


def _run(state):
    for _ in range(40):
        if state.outcomes:
            return state.outcomes[-1]
        state.worker.tick()
    raise AssertionError("implement stage never ran")


def _steps(world):
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _launch_rows(world):
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        return conn.execute(
            "SELECT count(*) FROM omp_work.mission_launch_bindings WHERE workspace_id=%s AND mission_id=%s",
            (world.workspace_id, world.mission_id),
        ).fetchone()[0]


def _rewrite(world, column, value):
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            f"UPDATE omp_work.mission_launch_bindings SET {column}=%s "
            "WHERE workspace_id=%s AND mission_id=%s",
            (value, world.workspace_id, world.mission_id),
        )


def test_matching_binding_launches_worker_and_names_its_ids(launch):
    outcome = _run(launch)
    assert outcome.outcome == "succeeded"
    assert launch.sandbox
    data = outcome.data
    assert all(data[key] for key in (
        "launch_binding_id", "assessment_id", "plan_receipt_id", "item_id", "candidate_id",
    ))
    assert data["intake_revision"] == _binding(launch.world).revision
    assert data["assessment_id"] == _binding(launch.world).assessment_id


@pytest.mark.parametrize(("column", "value", "field"), [
    ("revision", 7, "intake_revision"),
    ("assessment_id", "assessment:other", "assessment_receipt"),
    ("plan_receipt_id", "other-plan", "plan"),
    ("item_id", "other-item", "work_packet"),
    ("candidate_id", "other-candidate", "candidate"),
])
def test_changed_column_refuses_with_its_field_before_any_worker(launch, column, value, field):
    launch.before = lambda ctx: _rewrite(launch.world, column, value)
    outcome = _run(launch)
    assert outcome.outcome == "failed"
    assert outcome.data["code"] == "launch_binding_mismatch"
    assert outcome.data["field"] == field
    assert launch.sandbox == []


def test_replay_after_goal_change_is_plan_obsolete(launch, monkeypatch):
    world = launch.world
    crash_after_command(monkeypatch, "stamp_execution_plan")
    with pytest.raises(SimulatedCrash):
        for _ in range(40):
            launch.worker.tick()
    with intake_gate.cursor(OrchestratorHandler(world.config).store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        head = intake_gate.read_head(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
        body = read_revision(cur, workspace_id=world.workspace_id, run_id=head.run_id,
                             revision=head.revision).body
        body.snapshot.revision += 1
        body.binding.revision += 1
        body.assessments = []
        body.derivations = []
        for claim in body.snapshot.claims:
            if claim.predicate_id == "bounded.goal":
                claim.value = claim.value.model_copy(update={"value": "A different goal"})
        body.binding.claim_set_hash, body.binding.source_hash = revision_hashes(
            body.snapshot, body.artifact_bytes
        )
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=world.workspace_id, mission_id=world.mission_id,
                               run_id=head.run_id, expected_revision=head.revision,
                               operation_id=operation_id)
        append_revision(cur, locked, body, operation_id=operation_id)
    receipt = record_pass(world.store, **_scope(world))
    with intake_gate.cursor(OrchestratorHandler(world.config).store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        intake_binding.admit(cur, workspace_id=world.workspace_id, mission_id=world.mission_id,
                             run_id=head.run_id, revision=head.revision + 1, assessment_id=receipt)
    _expire_lease(world)
    tick_until(launch.worker, world, lambda _view: any(
        step.get("stage") == "grant" and step.get("kind") == "outcome"
        and step["data"].get("code") == "plan_obsolete"
        for step in _steps(world)
    ), limit=40)
    assert launch.outcomes == []
    assert launch.sandbox == []
    assert _launch_rows(world) == 0
