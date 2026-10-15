"""Plan stage admission on the typed intake gate, driven through a real WorkService.

- No intake head: plan fails typed_intake_missing and the mission is not admitted.
- Head without a PASS, or whose newest assessment is CONFLICTED: plan fails intake_receipt_missing.
- Newest PASS whose binding hash differs from the revision: plan fails intake_receipt_stale.
- A PASS at the head: plan succeeds, names run_id/revision/assessment_id, writes an admitted
  binding row for that revision and receipt, and the mission is linked and running.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import orchestrator_e2e_support
from omp_work.intake_repository import append_assessment, lock_revision, read_revision
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.intake_binding import read
from omp_work.orchestrator.intake_gate import cursor, latest_pass, read_head
from omp_work.orchestrator.service import OrchestratorHandler, submit
from omp_work.v1.api_models import IntakeAssessmentView
from omp_work.v1.intake_provenance import evaluate_provenance
from orchestrator_e2e_support import (
    answer_intake,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER
from typed_intake_support import _assessment, advance_revision

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _scope(world):
    return dict(workspace_id=world.workspace_id, actor_id=OWNER, mission_id=world.mission_id)


def _no_receipt(service, world):
    advance_revision(NativeJobStore(service.config), **_scope(world))


def _conflicted(service, world):
    store = NativeJobStore(service.config)
    advance_revision(store, **_scope(world))
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        head = read_head(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
        _assessment(cur, world.workspace_id, world.mission_id, head.run_id, "fail")


def _hash_mismatch(service, world):
    store = NativeJobStore(service.config)
    ws, mission = world.workspace_id, world.mission_id
    with cursor(store, workspace_id=ws, actor_id=OWNER) as cur:
        head = read_head(cur, workspace_id=ws, mission_id=mission)
        body = read_revision(cur, workspace_id=ws, run_id=head.run_id, revision=head.revision).body
        evaluation = evaluate_provenance(cur, body)
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=ws, mission_id=mission, run_id=head.run_id,
                               expected_revision=head.revision, operation_id=operation_id)
        append_assessment(cur, locked, IntakeAssessmentView(
            id=f"assessment:{operation_id}",
            binding=evaluation.binding.model_copy(update={"source_hash": "f" * 64}),
            assessment=evaluation.assessment.model_copy(
                update={"source_hash": "f" * 64, "derivation_receipt_ids": []}),
            provenance=evaluation.closure.closure,
        ), operation_id)


_NO_HEAD = object()


def _drive_to_plan(service, tmp_path: Path, monkeypatch, mutate=None):
    if mutate is _NO_HEAD:
        # The world seeds typed intake for its mission; suppress it so no head exists.
        monkeypatch.setattr(orchestrator_e2e_support, "seed_typed_intake", lambda *a, **k: None)
        mutate = None
    world = open_e2e_world(
        service, tmp_path / "admission", monkeypatch, verifier_mode="pass", worker_mode="ok",
        extra_capability="egress", operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(i for i in tree["items"] if str(i.get("work_id")) == str(work_id))
    seed_published_budget(service, world.workspace_id, work_id,
                          UUID(str(item["revision"]["revision_id"])))
    worker = open_worker(world)
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id

    def paused(view: dict) -> bool:
        return any(s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
                   for s in view["steps"])

    tick_until(worker, world, paused, limit=20)
    intake = next(s for s in handler.steps()
                  if s.get("stage") == "intake" and s.get("kind") == "outcome")
    world.intake_decision_id = UUID(str(intake["data"]["decision_id"]))
    world.mission_revision = int(intake["data"]["mission_revision"])
    store = NativeJobStore(service.config)
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        head = read_head(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
        seeded = None if head is None else latest_pass(
            cur, workspace_id=world.workspace_id, run_id=head.run_id, revision=head.revision)
    if mutate is not None:
        mutate(service, world)
    answer_intake(world, "edited_draft")
    tick_until(worker, world, lambda v: any(
        s.get("stage") == "plan" and s.get("kind") == "outcome" for s in v["steps"]), limit=20)
    plan = [s for s in handler.steps() if s.get("stage") == "plan" and s.get("kind") == "outcome"][-1]
    return world, plan, seeded, store


@pytest.mark.parametrize(("mutate", "code"), [
    (_NO_HEAD, "typed_intake_missing"),
    (_no_receipt, "intake_receipt_missing"),
    (_conflicted, "intake_receipt_missing"),
    (_hash_mismatch, "intake_receipt_stale"),
])
def test_plan_refused_without_valid_pass(service, tmp_path, monkeypatch, mutate, code):
    world, plan, _seeded, store = _drive_to_plan(service, tmp_path, monkeypatch, mutate)
    assert plan["outcome"] == "failed"
    assert plan["data"]["code"] == code
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        binding = read(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
    assert binding is None or binding.state != "admitted"
    mission = world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))
    assert not mission.get("links")


def test_plan_admits_on_pass_and_binds_revision_and_receipt(service, tmp_path, monkeypatch):
    world, plan, seeded, store = _drive_to_plan(service, tmp_path, monkeypatch)
    assert plan["outcome"] == "succeeded"
    data = plan["data"]
    assert (data["revision"], data["assessment_id"]) == (0, seeded.assessment_id)
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        binding = read(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
    assert (binding.state, binding.run_id, binding.revision, binding.assessment_id) == (
        "admitted", data["run_id"], 0, seeded.assessment_id)
    mission = world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))
    assert mission["links"]
    assert mission["status"] == "running"
