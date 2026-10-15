"""Intake changes hold a whole mission lifecycle until a current PASS exists.

One real WorkService drives the grant stage across workers rebuilt from the
persisted config:

- A mission admitted on revision N that gains revision N+1 is refused at the
  next stage with ``intake_revision_advanced`` and writes no execution grant or
  begin_execution row, while the N binding row stays held with its receipt.
- A PASS recorded for N+1 plus the owner's answer to the already-recorded retry
  pause rebinds the same mission to the new revision and receipt and runs the
  grant exactly once; the N row stays held.
- An owner-authorized terminal abandon while the mission is held ends the
  mission through the existing decision flow without running any stage or
  granting execution.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import load_config, request_terminal
from orchestrator_e2e_support import open_worker, tick_until
from test_orchestrator_intake_hold import (
    _binding,
    _no_grant,
    _outcome,
    _outcomes,
    _planned,
    _refused,
    _scope,
)
from test_orchestrator_intake_race import _pauses
from test_orchestrator_intake_replay import _steps
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
from test_orchestrator_service import _approve
from test_workflow_service import _command
from typed_intake_support import advance_revision, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore the stage operation registry each rebuilt config repopulates."""
    saved = dict(orchestrator._REGISTRY)
    try:
        yield
    finally:
        orchestrator._REGISTRY.clear()
        orchestrator._REGISTRY.update(saved)


def _reconstruct(world):
    """Reload the orchestrator config from its file and start a fresh worker."""
    world.config = load_config(world.config_path, world.service.config)
    return open_worker(world)


def _retry_pause(view):
    return next(step for step in view["steps"] if step.get("rule_id") == "retry-bound")


def _stage_effects(world):
    return [
        step for step in _steps(world)
        if step.get("kind") in {"dispatch", "outcome"}
    ]


def _authorized_abandon(service, tmp_path, world):
    """File and owner-approve the tier-3 mission_abandon decision for the mission."""
    classified = classify(
        parse_submission({"kind": "mission_abandon", "resource_id": str(world.mission_id)}),
        ResolvedTarget(),
    )
    decision_id = uuid4()
    status_code, body = _command(
        service,
        world.workspace_id,
        {
            "type": "create_decision",
            "payload": {
                "decision_id": str(decision_id),
                "project_id": str(world.project_id),
                "mission_id": str(world.mission_id),
                "question": "Abandon this mission?",
                "why_it_matters": "The owner asked to end it.",
                "risk_of_delay": "The mission keeps running.",
                "options": ["approve", "decline"],
                "risk_of_each_choice": {
                    "approve": "The mission is abandoned.",
                    "decline": "The mission stays.",
                },
                "default_if_any": "decline",
                "action_class": classified.action_class,
                "target_sha256": classified.target_sha256,
                "resume_state": "intake",
            },
        },
    )
    assert status_code == 200, body
    _approve(
        service,
        tmp_path,
        world.workspace_id,
        str(decision_id),
        action_class=classified.action_class,
        target_sha256=classified.target_sha256,
    )
    return decision_id


def test_advanced_intake_holds_next_stage_until_current_pass_on_restart(
    service, tmp_path, monkeypatch,
):
    world, _worker = _planned(service, tmp_path / "lifecycle", monkeypatch)
    admitted = _binding(world)
    assert admitted.state == "admitted"

    advance_revision(world.store, **_scope(world))
    worker = _reconstruct(world)
    view = tick_until(worker, world, lambda view: bool(_outcomes(view, "grant")), limit=20)

    _refused(world, "grant", "intake_revision_advanced")
    held = _binding(world)
    assert (held.binding_id, held.run_id) == (admitted.binding_id, admitted.run_id)
    assert (held.state, held.revision, held.assessment_id, held.hold_code) == (
        "held", admitted.revision, admitted.assessment_id, "intake_revision_advanced",
    )
    assert _pauses(world) == ["retry-bound"]
    _no_grant(world)

    receipt = record_pass(world.store, **_scope(world))
    pause = _retry_pause(view)
    status_code, body = _command(
        service,
        world.workspace_id,
        {"type": "answer_decision", "payload": {
            "decision_id": pause["decision_id"], "answer": "resume",
        }},
    )
    assert status_code == 200, body

    worker = _reconstruct(world)
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
            "WHERE binding_id=%s",
            (admitted.binding_id,),
        ).fetchone() == (world.mission_id, "held", admitted.revision, admitted.assessment_id)
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1


def test_authorized_abandon_while_held_leaves_no_stage_effect(service, tmp_path, monkeypatch):
    world, worker, _work_id = _open_front(
        service, tmp_path / "abandon", monkeypatch, extra_capability=None,
    )
    tick_until(worker, world, lambda view: bool(_outcomes(view, "plan")), limit=20)
    assert _outcome(world, "plan")["outcome"] == "succeeded"
    admitted = _binding(world)
    assert admitted.state == "admitted"

    advance_revision(world.store, **_scope(world))
    held = _binding(world)
    assert (held.binding_id, held.state) == (admitted.binding_id, "held")
    stage_effects = _stage_effects(world)

    decision_id = _authorized_abandon(service, tmp_path, world)
    request_terminal(world.config, world.mission_id, "abandon", basis=str(decision_id))

    worker = _reconstruct(world)
    tick_until(worker, world, lambda view: view["status"] == "abandoned", limit=20)

    assert any(step.get("rule_id") == "terminal-with-basis" for step in _steps(world))
    assert _stage_effects(world) == stage_effects
    released = _binding(world)
    assert (released.binding_id, released.state) == (admitted.binding_id, "held")
    _no_grant(world)
