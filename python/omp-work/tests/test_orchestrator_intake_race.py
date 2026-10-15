"""Intake revisions appended while a stage operation runs hold the mission, never roll it back."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from pathlib import Path

import psycopg
import pytest

from omp_work.orchestrator import intake_binding, intake_gate, operations
from omp_work.orchestrator.service import OrchestratorHandler
from omp_work.orchestrator.stages import ActionVerdict
from orchestrator_e2e_support import tick_until
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
from test_workflow_service import OWNER
from typed_intake_support import advance_revision

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


def _steps(world):
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _outcomes(world, stage):
    return [step for step in _steps(world)
            if step.get("stage") == stage and step.get("kind") == "outcome"]


def _pauses(world):
    return [step.get("rule_id") for step in _steps(world) if step.get("kind") == "pause"]


def _wrap_implement(monkeypatch, world, mutate):
    """Wrap the real run_implement: mutate its outcome, then append N+1 before it is recorded."""
    original = operations.run_implement

    def run(ctx):
        outcome = mutate(original(ctx))
        advance_revision(world.store, **_scope(world))
        return outcome

    monkeypatch.setattr(operations, "run_implement", run)


def test_plan_admission_without_earlier_binding_holds_when_revision_lands_before_return(
    service, tmp_path, monkeypatch,
):
    original = operations.run_plan
    seen = {}

    def plan_then_advance(ctx):
        seen["before"] = _binding(world)
        outcome = original(ctx)
        assert outcome.outcome == "succeeded"
        seen["admitted"] = _binding(world)
        advance_revision(world.store, **_scope(world))
        return outcome

    monkeypatch.setattr(operations, "run_plan", plan_then_advance)
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda _view: bool(_outcomes(world, "plan")), limit=20)
    assert seen["before"] is None
    assert seen["admitted"].state == "admitted"
    plan = _outcomes(world, "plan")[-1]
    assert plan["outcome"] == "failed"
    assert plan["data"]["code"] == "intake_revision_advanced"
    # Admission stays committed, but the binding is held and nothing is granted.
    held = _binding(world)
    assert (held.binding_id, held.state, held.hold_code) == (
        seen["admitted"].binding_id, "held", "intake_revision_advanced",
    )
    assert not _outcomes(world, "grant")
    assert _begin_rows(world) == []
    assert _grant_rows(world) == []


def test_new_scope_marker_is_consumed_and_owner_gate_wins_over_the_hold(service, tmp_path, monkeypatch):
    original = operations._sandbox
    calls = []

    def sandbox_then_race(ctx, argv, worktree, **kwargs):
        code = original(ctx, argv, worktree, **kwargs)
        if not calls:
            calls.append(worktree)
            (Path(worktree) / ".omp").mkdir(exist_ok=True)
            (Path(worktree) / ".omp" / "new-scope.json").write_text(json.dumps({"reason": "wider"}))
            advance_revision(world.store, **_scope(world))
        return code

    monkeypatch.setattr(operations, "_sandbox", sandbox_then_race)
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda _view: "d21-new-scope" in _pauses(world), limit=30)
    implemented = _outcomes(world, "implement")
    assert len(implemented) == 1  # no automatic retry
    assert implemented[0]["outcome"] == "failed"
    assert implemented[0]["data"]["code"] == "intake_revision_advanced"
    assert implemented[0]["new_scope"] is True
    assert not (calls[0] / ".omp" / "new-scope.json").exists()
    assert _pauses(world) == ["d21-new-scope"]
    assert _binding(world).state == "held"
    assert _outcomes(world, "evaluate") == []


@pytest.mark.parametrize("field,value,rule_id", [
    ("action", ActionVerdict(tier=3, action_class="unlisted", target_sha256=None, allowed=False, code="held"),
     "d35-tier3"),
    ("budget_exhausted", True, "budget-exhausted"),
    ("blocker", "needs-owner", "unrecoverable-blocker"),
])
def test_replaced_success_keeps_owner_gate_inputs(service, tmp_path, monkeypatch, field, value, rule_id):
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    _wrap_implement(monkeypatch, world, lambda outcome: dataclasses.replace(outcome, **{field: value}))
    tick_until(worker, world, lambda _view: rule_id in _pauses(world), limit=30)
    implemented = _outcomes(world, "implement")[-1]
    assert implemented["outcome"] == "failed"
    assert implemented["data"]["code"] == "intake_revision_advanced"
    assert implemented["data"]["worktree"]  # operation data survives the replacement
    if field == "action":
        assert implemented["action"]["allowed"] is False
        assert implemented["action"]["code"] == "held"
    else:
        assert implemented[field] == value
    assert _pauses(world) == [rule_id]  # no refusal blocker masks the owner gate
    assert implemented["blocker"] == (value if field == "blocker" else None)


def test_independent_append_during_operation_is_not_blocked_by_a_lock(service, tmp_path, monkeypatch):
    original = operations.run_implement
    finished = []

    def implement_with_concurrent_append(ctx):
        outcome = original(ctx)
        appender = threading.Thread(
            target=lambda: finished.append(advance_revision(world.store, **_scope(world))),
        )
        appender.start()
        appender.join(timeout=15)
        assert not appender.is_alive(), "append blocked by a lock held across the operation"
        return outcome

    monkeypatch.setattr(operations, "run_implement", implement_with_concurrent_append)
    world, worker, _work_id = _open_front(service, tmp_path, monkeypatch, extra_capability=None)
    tick_until(worker, world, lambda _view: bool(_outcomes(world, "implement")), limit=30)
    assert len(finished) == 1
    assert _outcomes(world, "implement")[-1]["data"]["code"] == "intake_revision_advanced"
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND mission_id=%s",
            (world.workspace_id, world.mission_id),
        ).fetchone() == (_binding(world).revision + 1,)
