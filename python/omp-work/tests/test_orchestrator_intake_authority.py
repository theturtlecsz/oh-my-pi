"""Grant authority for request criteria and the admitted intake revision.

A real WorkService drives the grant stage:

- An empty work list still fails criteria_missing while a typed PASS exists.
  The item stays in criteria_pending with a null criteria_revision_id.
- A non-empty work list is sealed as that list, in that order.
- A request goal edited after the seed does not replace the typed goal.
  A constraint that exists only on the request is still appended.
  Verification and paths stay the request test command and allowed paths.
- With no plan stamp, an admitted binding supplies that same approach.
  With no binding, the request intake remains the fallback.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator import service
from omp_work.orchestrator.intake_gate import cursor, latest_pass, read_head
from omp_work.orchestrator.operations import _worker_approach
from omp_work.orchestrator.service import LeaseClaim, OrchestratorHandler, StageContext, submit
from omp_work.v1.models import IntakeConstraint
from orchestrator_e2e_support import (
    answer_intake,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _seed_budget(world, service_fixture, work_id: UUID) -> None:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service_fixture, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"])),
    )


def _open(service_fixture, root: Path, monkeypatch):
    world = open_e2e_world(
        service_fixture, root, monkeypatch, verifier_mode="pass", worker_mode="ok",
        extra_capability=None, operations=("omp_work.orchestrator.operations:register",),
    )
    return world


def _submit(world, service_fixture):
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_budget(world, service_fixture, work_id)
    return work_id, open_worker(world)


def _grant_ran(view: dict) -> bool:
    return any(
        step.get("stage") == "grant" and step.get("kind") == "outcome" for step in view["steps"]
    )


def _plan_ran(view: dict) -> bool:
    return any(
        step.get("stage") == "plan" and step.get("kind") == "outcome" for step in view["steps"]
    )


def _paused_confirm(view: dict) -> bool:
    return any(
        step.get("kind") == "pause" and step.get("rule_id") == "d23-owner-confirm"
        for step in view["steps"]
    )


def _answer_edited_draft(world) -> None:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    intake = next(
        step for step in handler.steps()
        if step.get("stage") == "intake" and step.get("kind") == "outcome"
    )
    world.intake_decision_id = UUID(str(intake["data"]["decision_id"]))
    world.mission_revision = int(intake["data"]["mission_revision"])
    answer_intake(world, "edited_draft")


def _grant_outcome(world) -> dict:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return next(
        step for step in handler.steps()
        if step.get("stage") == "grant" and step.get("kind") == "outcome"
    )


def _query(world, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row,
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _grant_items(world) -> list[dict]:
    gid = str(service._ids(world.mission_id, "grant"))
    return _query(
        world,
        """
        SELECT phase, criteria_revision_id::text AS criteria_revision_id, plan_stamp
        FROM omp_work.execution_grant_items
        WHERE workspace_id = %s AND grant_id = %s
        ORDER BY position
        """,
        (world.workspace_id, gid),
    )


def _sealed_criteria(world, revision_id: str) -> list[str]:
    rows = _query(
        world,
        "SELECT criterion FROM omp_work.acceptance_criteria WHERE revision_id = %s ORDER BY position",
        (revision_id,),
    )
    return [str(row["criterion"]) for row in rows]


def _stamp(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _pass_receipt(service_fixture, world):
    store = NativeJobStore(service_fixture.config)
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        head = read_head(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
        assert head is not None
        return latest_pass(
            cur, workspace_id=world.workspace_id, run_id=head.run_id, revision=head.revision,
        )


def _context(world, request: dict, *, mission_id: UUID, work_id: UUID) -> StageContext:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = mission_id
    handler.project_id = world.project_id
    return StageContext(
        handler,
        step_index=0,
        stage="implement",
        attempt=1,
        repair_round=0,
        request=request,
        prior={},
        lease=LeaseClaim(job_id=str(uuid4()), worker_id="authority", fence=1),
        work_id=work_id,
    )


def test_empty_work_list_with_typed_pass_stays_criteria_missing(service, tmp_path, monkeypatch):
    world = _open(service, tmp_path / "empty-criteria", monkeypatch)
    assert _pass_receipt(service, world) is not None
    world.request = dataclasses.replace(
        world.request, work=world.request.work.model_copy(update={"acceptance_criteria": ()}),
    )
    _work_id, worker = _submit(world, service)
    tick_until(worker, world, _grant_ran, limit=40)

    outcome = _grant_outcome(world)
    assert outcome["outcome"] == "failed"
    assert outcome["data"]["code"] == "criteria_missing"
    items = _grant_items(world)
    assert len(items) == 1
    assert items[0]["phase"] == "criteria_pending"
    assert items[0]["criteria_revision_id"] is None


def test_nonempty_work_list_is_sealed_as_itself(service, tmp_path, monkeypatch):
    world = _open(service, tmp_path / "seal-itself", monkeypatch)
    criteria = ("owner criterion one", "owner criterion two")
    world.request = dataclasses.replace(
        world.request, work=world.request.work.model_copy(update={"acceptance_criteria": criteria}),
    )
    _work_id, worker = _submit(world, service)
    tick_until(worker, world, _grant_ran, limit=40)

    outcome = _grant_outcome(world)
    assert outcome["outcome"] == "succeeded"
    items = _grant_items(world)
    assert len(items) == 1
    assert items[0]["criteria_revision_id"] is not None
    assert items[0]["phase"] != "criteria_pending"
    assert _sealed_criteria(world, items[0]["criteria_revision_id"]) == list(criteria)


def test_edited_goal_keeps_typed_goal_and_request_constraint(service, tmp_path, monkeypatch):
    world = _open(service, tmp_path / "typed-goal", monkeypatch)
    typed_goal = world.request.intake.goal.statement
    constraint = IntakeConstraint.model_validate({
        "id": "c-constraint-1",
        "statement": "latency under 10ms",
        "source_span_ids": (),
        "key": "latency",
        "value": {"kind": "known", "value": "10ms"},
        "polarity": "positive",
    })
    world.request = dataclasses.replace(world.request, intake=world.request.intake.model_copy(update={
        "goal": world.request.intake.goal.model_copy(update={"statement": "edited after the seed"}),
        "constraints": (*world.request.intake.constraints, constraint),
    }))
    work_id, worker = _submit(world, service)
    tick_until(worker, world, _paused_confirm, limit=20)
    _answer_edited_draft(world)
    tick_until(worker, world, _plan_ran, limit=20)

    before_stamp = _worker_approach(_context(
        world, world.request.document(), mission_id=world.mission_id, work_id=work_id,
    ))
    assert before_stamp == [typed_goal, constraint.statement]
    assert before_stamp[0] != "edited after the seed"

    tick_until(worker, world, _grant_ran, limit=40)
    outcome = _grant_outcome(world)
    assert outcome["outcome"] == "succeeded"
    assert outcome["data"]["phase"] == "executing"
    items = _grant_items(world)
    assert len(items) == 1
    stamp = _stamp(items[0]["plan_stamp"])
    assert stamp["approach"] == [typed_goal, constraint.statement]
    assert stamp["approach"] == before_stamp
    assert stamp["verification"] == list(world.request.test_command)
    assert stamp["paths"] == list(world.request.allowed_paths)

    after_stamp = _worker_approach(_context(
        world, world.request.document(), mission_id=world.mission_id, work_id=work_id,
    ))
    assert after_stamp == stamp["approach"]


def test_missing_binding_keeps_request_intake_fallback(service, tmp_path, monkeypatch):
    world = _open(service, tmp_path / "no-binding", monkeypatch)
    request = {
        "intake": {
            "goal": {"statement": "Implement requested feature"},
            "constraints": [{"statement": "Preserve CLI behavior"}],
        },
    }
    approach = _worker_approach(_context(
        world, request, mission_id=uuid4(), work_id=uuid4(),
    ))
    assert approach == ["Implement requested feature", "Preserve CLI behavior"]
