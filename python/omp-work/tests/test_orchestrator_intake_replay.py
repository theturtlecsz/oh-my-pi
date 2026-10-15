"""Persisted successes never authorize recovery past changed intake authority."""
from __future__ import annotations

import dataclasses
import os

import psycopg
import pytest

from omp_work.orchestrator import intake_gate, service as orchestrator
from omp_work.orchestrator.service import OrchestratorError, OrchestratorHandler
from omp_work.orchestrator.stages import ActionVerdict
from orchestrator_e2e_support import SimulatedCrash, open_worker, tick_until
from test_orchestrator_intake_admission import _hash_mismatch
from test_orchestrator_intake_hold import _binding, _scope
from test_orchestrator_operations_front import _begin_rows, _expire_lease, _grant_rows, _open_front
from test_workflow_service import OWNER
from typed_intake_support import advance_revision, forge_receipt, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture(autouse=True)
def restore_registry():
    saved = dict(orchestrator._REGISTRY)
    yield
    orchestrator._REGISTRY.clear()
    orchestrator._REGISTRY.update(saved)


def _steps(world):
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _plan_steps(world, kind):
    return [step for step in _steps(world) if step.get("stage") == "plan" and step["kind"] == kind]


def _crashed_plan(service, tmp_path, monkeypatch, posted):
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    original = OrchestratorHandler._apply if posted else OrchestratorHandler._record_decision

    def crash(self, *args):
        chosen = args[2] if posted else args[1]
        if chosen.stage == "plan" and chosen.kind != "dispatch":
            raise SimulatedCrash("plan-success")
        return original(self, *args)

    with monkeypatch.context() as patch:
        patch.setattr(OrchestratorHandler, "_apply" if posted else "_record_decision", crash)
        with pytest.raises(SimulatedCrash):
            tick_until(worker, world, lambda _view: False, limit=20)
    assert len(_plan_steps(world, "outcome")) == 1
    assert _plan_steps(world, "outcome")[0]["outcome"] == "succeeded"
    assert len(_plan_steps(world, "advance")) == int(posted)
    assert _begin_rows(world) == _grant_rows(world) == []
    _expire_lease(world)
    return world


def _no_downstream(world):
    assert _begin_rows(world) == _grant_rows(world) == []
    assert not any(step.get("stage") == "grant" for step in _steps(world))
    advance = _plan_steps(world, "advance")
    if advance:
        with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
            assert conn.execute(
                "SELECT count(*) FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (world.workspace_id, advance[0]["next_job"]),
            ).fetchone() == (0,)
    assert world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))["status"] != "completed"


@pytest.mark.parametrize("posted", [False, True], ids=["outcome-only", "posted-decision"])
@pytest.mark.parametrize("tamper,code", [
    ("advanced", "intake_revision_advanced"),
    ("forged", "intake_receipt_forged"),
    ("hash", "intake_receipt_stale"),
])
def test_recovery_refuses_obsolete_or_invalid_success(
    service, tmp_path, monkeypatch, posted, tamper, code,
):
    world = _crashed_plan(service, tmp_path, monkeypatch, posted)
    admitted = _binding(world)
    persisted = _steps(world)
    if tamper == "advanced":
        advance_revision(world.store, **_scope(world))
    else:
        assessment_id = forge_receipt()
        if tamper == "hash":
            _hash_mismatch(service, world)
            handler = OrchestratorHandler(world.config)
            with intake_gate.cursor(handler.store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
                assessment_id = intake_gate.latest_pass(
                    cur, workspace_id=world.workspace_id, run_id=admitted.run_id,
                    revision=admitted.revision,
                ).assessment_id
        with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
            conn.execute(
                "UPDATE omp_work.mission_intake_bindings SET assessment_id=%s WHERE binding_id=%s",
                (assessment_id, admitted.binding_id),
            )
        # A newer valid receipt must not conceal a forged or mismatched bound receipt.
        record_pass(world.store, **_scope(world))
    worker = open_worker(world)
    with pytest.raises(OrchestratorError) as refused:
        worker.tick()
    assert refused.value.code == code
    held = _binding(world)
    assert (held.binding_id, held.state, held.hold_code) == (admitted.binding_id, "held", code)
    assert _steps(world) == persisted  # append_step identities and original success stay immutable
    _no_downstream(world)
    # Repeated restart cannot bypass the refusal or replace the recorded decision.
    _expire_lease(world)
    with pytest.raises(OrchestratorError) as repeated:
        open_worker(world).tick()
    assert repeated.value.code == code
    assert _steps(world) == persisted
    _no_downstream(world)


@pytest.mark.parametrize("posted", [False, True], ids=["outcome-only", "posted-decision"])
@pytest.mark.parametrize("rebind", [False, True], ids=["unchanged", "current-head-pass"])
def test_valid_recovery_advances_once_without_reexecuting_plan(
    service, tmp_path, monkeypatch, posted, rebind,
):
    world = _crashed_plan(service, tmp_path, monkeypatch, posted)
    admitted = _binding(world)
    outcome = _plan_steps(world, "outcome")
    receipt = admitted.assessment_id
    if rebind:
        advance_revision(world.store, **_scope(world))
        # First replay holds, then current-head PASS permits append-only rebind.
        with pytest.raises(OrchestratorError, match="intake_revision_advanced"):
            open_worker(world).tick()
        _expire_lease(world)
        receipt = record_pass(world.store, **_scope(world))
    worker = open_worker(world)
    worker.tick()
    assert _plan_steps(world, "outcome") == outcome
    assert len(_plan_steps(world, "advance")) == 1
    assert _begin_rows(world) == _grant_rows(world) == []
    rebound = _binding(world)
    assert (rebound.state, rebound.revision, rebound.assessment_id) == (
        "admitted", admitted.revision + int(rebind), receipt,
    )
    assert (rebound.binding_id != admitted.binding_id) == rebind
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT state,revision,assessment_id FROM omp_work.mission_intake_bindings WHERE binding_id=%s",
            (admitted.binding_id,),
        ).fetchone() == ("held" if rebind else "admitted", admitted.revision, admitted.assessment_id)
    tick_until(worker, world, lambda _view: bool(_begin_rows(world)), limit=10)
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1
    assert _begin_rows(world)[0]["conflict_count"] == 0
    assert _plan_steps(world, "outcome") == outcome
    assert len(_plan_steps(world, "advance")) == 1


@pytest.mark.parametrize("field,value,rule_id", [
    ("action", ActionVerdict(tier=3, action_class="unlisted", target_sha256=None, allowed=False, code="held"),
     "d35-tier3"),
    ("new_scope", True, "d21-new-scope"),
    ("budget_exhausted", True, "budget-exhausted"),
    ("blocker", "needs-owner", "unrecoverable-blocker"),
])
def test_recovery_keeps_recorded_owner_gate_facts(service, tmp_path, monkeypatch, field, value, rule_id):
    original = OrchestratorHandler._operate

    def operate(self, job, step_index, stage, facts):
        outcome = original(self, job, step_index, stage, facts)
        return dataclasses.replace(outcome, **{field: value}) if stage == "plan" else outcome

    with monkeypatch.context() as patch:
        patch.setattr(OrchestratorHandler, "_operate", operate)
        world = _crashed_plan(service, tmp_path, patch, posted=False)
    persisted = _plan_steps(world, "outcome")
    advance_revision(world.store, **_scope(world))
    open_worker(world).tick()
    assert _plan_steps(world, "outcome") == persisted
    pauses = _plan_steps(world, "pause")
    assert [step["rule_id"] for step in pauses] == [rule_id]
    assert world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))["status"] == "paused"
    assert _binding(world).state == "held"
    _no_downstream(world)
