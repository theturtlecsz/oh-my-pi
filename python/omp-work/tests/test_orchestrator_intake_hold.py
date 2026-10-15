"""Real-service dispatch refuses obsolete or forged intake authority across restarts."""
from __future__ import annotations

import os

import psycopg
import pytest

from omp_work.orchestrator import intake_binding, intake_gate
from omp_work.orchestrator.service import OrchestratorHandler
from orchestrator_e2e_support import open_worker, tick_until
from test_orchestrator_intake_admission import _hash_mismatch
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
from test_workflow_service import OWNER, _command
from typed_intake_support import advance_revision, forge_receipt, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _scope(world):
    return dict(workspace_id=world.workspace_id, actor_id=OWNER, mission_id=world.mission_id)


def _binding(world):
    handler = OrchestratorHandler(world.config)
    with intake_gate.cursor(handler.store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        return intake_binding.read(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)


def _outcomes(view, stage):
    return [step for step in view["steps"]
            if step.get("stage") == stage and step.get("kind") == "outcome"]


def _outcome(world, stage):
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return _outcomes({"steps": handler.steps()}, stage)[-1]


def _planned(service, tmp_path, monkeypatch):
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda view: bool(_outcomes(view, "plan")), limit=20)
    assert _outcome(world, "plan")["outcome"] == "succeeded"
    assert _binding(world).state == "admitted"
    return world, worker


def _refused(world, stage, code):
    outcome = _outcome(world, stage)
    assert outcome["outcome"] == "failed"
    assert outcome["data"]["code"] == code


def _no_grant(world):
    assert _begin_rows(world) == []
    assert _grant_rows(world) == []


def test_grant_holds_advanced_intake_then_rebinds_after_pass_on_restart(service, tmp_path, monkeypatch):
    world, worker = _planned(service, tmp_path / "advance", monkeypatch)
    admitted = _binding(world)
    advance_revision(world.store, **_scope(world))
    view = tick_until(worker, world, lambda view: bool(_outcomes(view, "grant")), limit=20)
    _refused(world, "grant", "intake_revision_advanced")
    held = _binding(world)
    assert held.binding_id == admitted.binding_id
    assert (held.state, held.revision, held.assessment_id, held.hold_code) == (
        "held", admitted.revision, admitted.assessment_id, "intake_revision_advanced",
    )
    _no_grant(world)

    receipt = record_pass(world.store, **_scope(world))
    pause = next(step for step in view["steps"] if step.get("rule_id") == "retry-bound")
    status_code, body = _command(service, world.workspace_id, {
        "type": "answer_decision",
        "payload": {"decision_id": pause["decision_id"], "answer": "resume"},
    })
    assert status_code == 200, body
    worker = open_worker(world)
    tick_until(worker, world, lambda view: len(_outcomes(view, "grant")) == 2, limit=20)
    assert _outcome(world, "grant")["outcome"] == "succeeded"
    rebound = _binding(world)
    assert rebound.binding_id != admitted.binding_id
    assert (rebound.state, rebound.revision, rebound.assessment_id) == (
        "admitted", admitted.revision + 1, receipt,
    )
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT mission_id,state,revision,assessment_id FROM omp_work.mission_intake_bindings "
            "WHERE binding_id=%s", (admitted.binding_id,),
        ).fetchone() == (world.mission_id, "held", admitted.revision, admitted.assessment_id)
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1


@pytest.mark.parametrize("tamper,code", [
    ("forged", "intake_receipt_forged"),
    ("hash", "intake_receipt_stale"),
])
def test_grant_verifies_stored_receipt_without_substitution(service, tmp_path, monkeypatch, tamper, code):
    world, _worker = _planned(service, tmp_path / tamper, monkeypatch)
    admitted = _binding(world)
    # A newer genuine PASS must not replace the receipt stored at admission.
    record_pass(world.store, **_scope(world))
    assessment_id = forge_receipt()
    if tamper == "hash":
        _hash_mismatch(service, world)
        handler = OrchestratorHandler(world.config)
        with intake_gate.cursor(handler.store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
            receipt = intake_gate.latest_pass(
                cur, workspace_id=world.workspace_id, run_id=admitted.run_id, revision=admitted.revision,
            )
            assessment_id = receipt.assessment_id
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        conn.execute(
            "UPDATE omp_work.mission_intake_bindings SET assessment_id=%s WHERE binding_id=%s",
            (assessment_id, admitted.binding_id),
        )
    if tamper == "hash":
        record_pass(world.store, **_scope(world))
    worker = open_worker(world)
    tick_until(worker, world, lambda view: bool(_outcomes(view, "grant")), limit=20)
    _refused(world, "grant", code)
    held = _binding(world)
    assert (held.binding_id, held.state, held.hold_code) == (admitted.binding_id, "held", code)
    _no_grant(world)


def test_revision_appended_during_plan_blocks_success_without_rolling_back_admission(
    service, tmp_path, monkeypatch,
):
    world, worker, _work_id = _open_front(service, tmp_path / "race", monkeypatch, extra_capability=None)
    original = OrchestratorHandler._operate

    def plan_and_advance(self, job, step_index, stage, facts):
        outcome = original(self, job, step_index, stage, facts)
        if stage == "plan":
            assert outcome.outcome == "succeeded"
            assert _binding(world).state == "admitted"
            advance_revision(world.store, **_scope(world))
        return outcome

    monkeypatch.setattr(OrchestratorHandler, "_operate", plan_and_advance)
    view = tick_until(worker, world, lambda view: bool(_outcomes(view, "plan")), limit=20)
    _refused(world, "plan", "intake_revision_advanced")
    assert not _outcomes(view, "grant")
    assert _binding(world).state == "held"
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT status FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
            (world.workspace_id, world.mission_id),
        ).fetchone() == ("approved",)
    _no_grant(world)
