"""Tests for orchestrator front stage operations: intake and confirm (OMP-417-s07-s04-s01).

Defends front pipeline contracts:
- extra kept: intake data has decision_id, work_key; read item by work_key gives submitted work_id; confirm pause names decision; reject -> mission abandoned, no plan step.
- edited_draft -> confirm succeeded, then a plan step.
- extra_capability=None -> no decision_id, mission approved.
- crash_after_command("draft_mission_intake") -> tick raises SimulatedCrash, expired lease reclaimed, intake succeeded, same decision_id, 1 command row, conflict_count 0.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from omp_work.orchestrator import service
from omp_work.orchestrator.operations import run_confirm, run_intake
from omp_work.orchestrator.service import (
    OrchestratorHandler,
    submit,
)
from orchestrator_e2e_support import (
    SimulatedCrash,
    answer_intake,
    crash_after_command,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore service._REGISTRY after each test."""
    saved = dict(service._REGISTRY)
    try:
        yield
    finally:
        service._REGISTRY.clear()
        service._REGISTRY.update(saved)


def _seed_item_budget(world, service_fixture, work_id: UUID) -> UUID:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(i for i in tree["items"] if str(i.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)
    return revision_id


def test_extra_kept_intake_confirm_reject(service, tmp_path: Path, monkeypatch) -> None:
    """Extra capability kept: intake has decision_id, work_key; confirm pauses on decision; reject abandons."""
    world = open_e2e_world(
        service,
        tmp_path / "extra_kept",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    intake_data = intake_outcome["data"]
    assert "decision_id" in intake_data
    decision_id = intake_data["decision_id"]
    assert "work_key" in intake_data
    work_key = intake_data["work_key"]

    item_view = world.store.read(world.workspace_id, OWNER, "item", work_key)
    assert str(item_view["work_id"]) == str(work_id)

    pause_step = next(
        s
        for s in steps
        if s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
    )
    assert str(pause_step.get("decision_id")) == str(decision_id)

    # Test setup before answer_intake (Assumptions 20 / 46)
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])

    answer_intake(world, "reject")
    worker.tick()

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert mission_view["status"] == "abandoned"

    steps = handler.steps()
    assert not any(s.get("stage") == "plan" for s in steps)


def test_edited_draft_confirm_succeeded_then_plan(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Edited draft drops extra capability: confirm succeeds, then advances to a plan step."""
    world = open_e2e_world(
        service,
        tmp_path / "edited",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    intake_data = intake_outcome["data"]

    # Test setup before answer_intake (Assumptions 20 / 46)
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])

    answer_intake(world, "edited_draft")

    def plan_step_run(v: dict) -> bool:
        return any(s.get("stage") == "plan" for s in v["steps"])

    tick_until(worker, world, plan_step_run, limit=20)

    steps = handler.steps()
    confirm_outcomes = [
        s for s in steps if s.get("stage") == "confirm" and s.get("kind") == "outcome"
    ]
    assert len(confirm_outcomes) >= 1
    assert confirm_outcomes[-1]["outcome"] == "succeeded"

    assert any(s.get("stage") == "plan" for s in steps)


def test_no_extra_capability_no_decision_approved(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Extra capability None: intake within mandate, no decision_id, mission approved directly."""
    world = open_e2e_world(
        service,
        tmp_path / "no_extra",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def past_confirm(view: dict) -> bool:
        return any(s.get("stage") == "plan" for s in view["steps"])

    tick_until(worker, world, past_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    assert "decision_id" not in intake_outcome["data"]

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert mission_view["status"] in ("approved", "running")

    # Confirm never paused
    assert not any(
        s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
        for s in steps
    )


def test_crash_after_draft_mission_intake(service, tmp_path: Path, monkeypatch) -> None:
    """Crash after draft_mission_intake: lease expired, tick reclaims, intake succeeds with same decision_id."""
    world = open_e2e_world(
        service,
        tmp_path / "crash",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    crash_after_command(monkeypatch, "draft_mission_intake")

    with pytest.raises(SimulatedCrash):
        worker.tick()

    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )

    # Next tick reclaims the job and re-runs intake
    worker.tick()

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()
    intake_outcomes = [
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    ]
    assert len(intake_outcomes) == 1
    assert intake_outcomes[0]["outcome"] == "succeeded"
    decision_id = intake_outcomes[0]["data"].get("decision_id")
    assert decision_id is not None

    with (
        psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT operation_id, response, conflict_count FROM omp_control.idempotent_commands "
            "WHERE workspace_id=%s AND command_type='draft_mission_intake'",
            (world.workspace_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][2] == 0
        assert str(rows[0][1]["decision_id"]) == str(decision_id)


def test_intake_clarify_and_held_return_blockers() -> None:
    """Intake outcomes 'clarify' and 'held' return blockers 'intake_clarify' and 'intake_held'."""

    class DummyIntents:
        def open(self, key):
            return None

        def record_intent(self, key, action, target):
            pass

    class DummyContext:
        def __init__(self, outcome: str):
            self.mission_id = UUID("11111111-1111-1111-1111-111111111111")
            self.step_index = 0
            self.intents = DummyIntents()
            self.request = {"intake": {}, "scope": {}, "instruction": None}
            self.principal = None
            self.workspace_id = None
            self.service = type("DummyService", (), {"read": lambda *args: {}})()
            self._outcome = outcome

        def command(self, cmd_type, payload):
            return {"outcome": self._outcome}

    outcome_clarify = run_intake(DummyContext("clarify"))
    assert outcome_clarify.outcome == "failed"
    assert outcome_clarify.blocker == "intake_clarify"

    outcome_held = run_intake(DummyContext("held"))
    assert outcome_held.outcome == "failed"
    assert outcome_held.blocker == "intake_held"


def test_confirm_abandoned_status_fails() -> None:
    """Confirm returns failed when mission status is abandoned."""

    class DummyContext:
        def __init__(self):
            self.mission_id = UUID("11111111-1111-1111-1111-111111111111")
            self.workspace_id = UUID("22222222-2222-2222-2222-222222222222")
            self.principal = None
            self.prior = {}
            self.service = type(
                "DummyService",
                (),
                {"read": lambda *args: {"status": "abandoned"}},
            )()

    outcome = run_confirm(DummyContext())
    assert outcome.outcome == "failed"
    assert outcome.data.get("reason") == "abandoned"
