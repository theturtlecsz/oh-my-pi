"""Tests for orchestrator grant issued_at pinning (OMP-417-s07-s06-s01).

Defends:
- grant step succeeded: data.issued_at equals the pin step target.issued_at.
- crash_after_command("begin_execution"), lease expiry, tick: grant succeeded,
  same issued_at, exactly one external_intent step with external_ref grant-issue:<mission>.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from omp_work.orchestrator import service
from omp_work.orchestrator.service import (
    OrchestratorHandler,
    submit,
)
from orchestrator_e2e_support import (
    SimulatedCrash,
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


def _expire_lease(world) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def _json_obj(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def test_grant_step_issued_at_equals_pin_target(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Grant step succeeded; data.issued_at equals the pin step target.issued_at."""
    world = open_e2e_world(
        service,
        tmp_path / "grant_pin",
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

    def grant_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "grant" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, grant_ran, limit=40)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    grant_step = next(
        step
        for step in steps
        if step.get("stage") == "grant" and step.get("kind") == "outcome"
    )
    assert grant_step["outcome"] == "succeeded"
    data = grant_step["data"]
    assert "issued_at" in data
    issued_at = data["issued_at"]

    pin_key = f"grant-issue:{world.mission_id}"
    intent_steps = [
        step
        for step in steps
        if step.get("kind") == "external_intent" and step.get("external_ref") == pin_key
    ]
    assert len(intent_steps) == 1
    pin_target = _json_obj(intent_steps[0]["target"])
    assert isinstance(pin_target, dict)
    assert pin_target.get("issued_at") == issued_at


def test_crash_after_begin_execution_replays_with_same_issued_at(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Crash after begin_execution, lease expiry, tick -> grant succeeded, same issued_at, 1 intent step."""
    world = open_e2e_world(
        service,
        tmp_path / "grant_crash_pin",
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

    def plan_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "plan" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, plan_ran, limit=20)
    crash_after_command(monkeypatch, "begin_execution")
    with pytest.raises(SimulatedCrash):
        worker.tick()

    pin_key = f"grant-issue:{world.mission_id}"
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps_pre = handler.steps()
    intent_steps_pre = [
        step
        for step in steps_pre
        if step.get("kind") == "external_intent" and step.get("external_ref") == pin_key
    ]
    assert len(intent_steps_pre) == 1
    pre_pin_target = _json_obj(intent_steps_pre[0]["target"])
    assert isinstance(pre_pin_target, dict)
    pre_issued_at = pre_pin_target["issued_at"]

    _expire_lease(world)
    worker.tick()

    steps = handler.steps()
    grant_step = next(
        step
        for step in steps
        if step.get("stage") == "grant" and step.get("kind") == "outcome"
    )
    assert grant_step["outcome"] == "succeeded"
    assert grant_step["data"]["issued_at"] == pre_issued_at

    intent_steps = [
        step
        for step in steps
        if step.get("kind") == "external_intent" and step.get("external_ref") == pin_key
    ]
    assert len(intent_steps) == 1
    post_pin_target = _json_obj(intent_steps[0]["target"])
    assert isinstance(post_pin_target, dict)
    assert post_pin_target.get("issued_at") == pre_issued_at


def test_grant_without_pin_omits_issued_at_key(
    service, tmp_path: Path, monkeypatch
) -> None:
    """When a grant succeeds without a pin, issued_at key is absent."""
    world = open_e2e_world(
        service,
        tmp_path / "grant_no_pin",
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

    def grant_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "grant" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, grant_ran, limit=40)

    # Now simulate a second run_grant on the already-begun grant where the pin is not present.
    from omp_work.orchestrator.operations import run_grant
    from omp_work.orchestrator.service import LeaseClaim, StageContext

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.project_id
    steps = handler.steps()
    plan_outcome = next(
        step
        for step in steps
        if step.get("stage") == "plan" and step.get("kind") == "outcome"
    )
    intake_outcome = next(
        step
        for step in steps
        if step.get("stage") == "intake" and step.get("kind") == "outcome"
    )

    ctx = StageContext(
        handler,
        step_index=999,
        stage="grant",
        attempt=1,
        repair_round=0,
        request=world.request.document(),
        prior={"intake": intake_outcome, "plan": plan_outcome},
        lease=LeaseClaim(
            job_id=str(UUID("00000000-0000-0000-0000-000000000000")),
            worker_id="dummy",
            fence=1,
        ),
        work_id=work_id,
    )
    # Monkeypatch ctx.intents.open to return None
    monkeypatch.setattr(ctx.intents, "open", lambda key: None)

    outcome = run_grant(ctx)
    assert outcome.outcome == "succeeded"
    assert "issued_at" not in outcome.data
