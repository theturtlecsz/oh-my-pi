"""Typed authority requires a full PASS bound to the current stored proof."""
from dataclasses import replace

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.intake_repository import append_assessment, lock_revision, read_revision
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.intake_gate import (
    INTAKE_REVISION_ADVANCED, Head, Refusal, cursor, execution_criteria,
    latest_pass, projection, read_head, verify,
)
from omp_work.v1.models import BoundedIntakeDraft
from omp_work.v1.api_models import IntakeAssessmentView
from omp_work.v1.intake_provenance import evaluate_provenance
from test_project_store import _insert_project, _workspace
from test_workflow_service import OWNER
from orchestrator_e2e_support import open_e2e_world
from typed_intake_support import advance_revision, forge_receipt, record_pass, seed_typed_intake

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def world(service):
    workspace_id, mission_id = uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, uuid4(), "gate")
    cases = json.loads((Path(__file__).parent / "fixtures/bounded_intake_golden.json").read_text())
    raw = next(case["input"]["draft"] for case in cases if case["id"] == "01-valid-ready")
    draft = BoundedIntakeDraft.model_validate({**raw, "unknowns": []})
    return NativeJobStore(service.config), dict(workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id), draft


def seed(world, mode="pass"):
    store, scope, draft = world
    return seed_typed_intake(store, **scope, run_id="gate", draft=draft, mode=mode)


def check(world, receipt, revision=0):
    store, scope, _draft = world
    with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
        return verify(cur, workspace_id=scope["workspace_id"], mission_id=scope["mission_id"],
                      revision=revision, assessment_id=receipt)


def test_missing_head(world):
    assert check(world, forge_receipt()) == Refusal("typed_intake_missing")


@pytest.mark.parametrize("summary", [False, True])
def test_fail_or_summary_is_not_pass(world, summary):
    receipt = seed(world, "fail")
    store, scope, _draft = world
    if summary:
        with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
            cur.execute(
                "INSERT INTO omp_work.intake_assessments "
                "(workspace_id,run_id,revision,operation_id,nsi_version,assessment_id,assessed_revision,status,assessed_by,created_at) "
                "SELECT workspace_id,run_id,revision,operation_id,nsi_version,'summary',assessed_revision,"
                "'ready_for_ratification',assessed_by,created_at FROM omp_work.intake_assessments WHERE workspace_id=%s",
                (scope["workspace_id"],),
            )
    assert check(world, receipt) == Refusal("intake_receipt_missing")


def test_no_receipt(world):
    receipt = seed(world)
    store, scope, _draft = world
    advance_revision(store, **scope)
    assert check(world, receipt, 1) == Refusal("intake_receipt_missing")


def test_advanced_revision_requires_new_pass(world):
    receipt = seed(world)
    assert check(world, receipt) is None
    store, scope, _draft = world
    assert advance_revision(store, **scope) == 1
    assert check(world, receipt) == Refusal("intake_receipt_stale")
    assert check(world, receipt, 1) == Refusal("intake_receipt_missing")
    current = record_pass(store, **scope)
    assert check(world, current, 1) is None
    assert check(world, receipt, 1) == Refusal("intake_receipt_forged")


@pytest.mark.parametrize("name", ["contract_hash", "claim_set_hash", "source_hash", "rule_bundle_hash", "schema_hash"])
def test_hash_mismatch_refuses_pass(world, name):
    receipt = seed(world)
    store, scope, _draft = world
    with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
        body = read_revision(cur, workspace_id=scope["workspace_id"], run_id="gate", revision=0).body
        evaluation = evaluate_provenance(cur, body)
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=scope["workspace_id"], mission_id=scope["mission_id"],
                               run_id="gate", expected_revision=0, operation_id=operation_id)
        receipt = f"assessment:{operation_id}"
        append_assessment(cur, locked, IntakeAssessmentView(
            id=receipt, binding=evaluation.binding.model_copy(update={name: "f" * 64}),
            assessment=evaluation.assessment.model_copy(update={name: "f" * 64, "derivation_receipt_ids": []}),
            provenance=evaluation.closure.closure,
        ), operation_id)
    assert check(world, receipt) == Refusal("intake_receipt_stale")


def test_forged_id(world):
    seed(world)
    assert check(world, forge_receipt()) == Refusal("intake_receipt_forged")


def test_stored_projection_and_execution_order(world):
    receipt = seed(world)
    store, scope, draft = world
    with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
        assert read_head(cur, workspace_id=scope["workspace_id"], mission_id=scope["mission_id"]) == Head("gate", 0)
        assert latest_pass(cur, workspace_id=scope["workspace_id"], run_id="gate", revision=0).assessment_id == receipt
        body = read_revision(cur, workspace_id=scope["workspace_id"], run_id="gate", revision=0).body
        projected = projection(cur, body)
        assert projected.model_copy(update={
            "acceptance_criteria": tuple(sorted(projected.acceptance_criteria, key=lambda item: item.id)),
        }) == draft
        criteria = tuple(sorted(draft.acceptance_criteria, key=lambda item: item.id))
        reordered = projected.model_copy(update={"acceptance_criteria": tuple(reversed(criteria))})
        assert execution_criteria(reordered) == tuple(item.observable_outcome for item in criteria)
        fallback = criteria[0].model_copy(update={"observable_outcome": ""})
        assert execution_criteria(projected.model_copy(update={"acceptance_criteria": (fallback,)})) == (fallback.statement,)
        body.snapshot.artifacts = []
        assert projection(cur, body) == Refusal("typed_intake_missing")
    # verify never emits the stage fence code, even after head advancement.
    advance_revision(store, **scope)
    refusal = check(world, receipt)
    assert refusal.code != INTAKE_REVISION_ADVANCED


def test_latest_pass_and_committed_cursor(world):
    first = seed(world)
    store, scope, _draft = world
    second = record_pass(store, **scope)
    with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
        assert latest_pass(cur, workspace_id=scope["workspace_id"], run_id="gate", revision=0).assessment_id == second
    assert first != second
    assert check(world, first) is None
    assert check(world, second) is None


def test_e2e_world_seeds_loaded_request_once(service, tmp_path, monkeypatch):
    world = open_e2e_world(service, tmp_path, monkeypatch, verifier_mode="pass", worker_mode="ok", operations=())
    original_request = world.request_path.read_bytes()
    store = NativeJobStore(service.config)
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        head = read_head(cur, workspace_id=world.workspace_id, mission_id=world.mission_id)
        assert head.revision == 0
        receipt = latest_pass(cur, workspace_id=world.workspace_id, run_id=head.run_id, revision=0)
        assert verify(cur, workspace_id=world.workspace_id, mission_id=world.mission_id,
                      revision=0, assessment_id=receipt.assessment_id) is None
        body = read_revision(cur, workspace_id=world.workspace_id, run_id=head.run_id, revision=0).body
        projected = projection(cur, body)
        assert projected.goal.statement == world.request.intake.goal.statement
        cur.execute("SELECT status FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
                    (world.workspace_id, world.mission_id))
        assert cur.fetchone() == ("draft",)
    world.request = replace(world.request, intake=world.request.intake.model_copy(update={
        "goal": world.request.intake.goal.model_copy(update={"statement": "Later request edit"}),
    }))
    with cursor(store, workspace_id=world.workspace_id, actor_id=OWNER) as cur:
        body = read_revision(cur, workspace_id=world.workspace_id, run_id=head.run_id, revision=0).body
        assert projection(cur, body) == projected
    assert world.request_path.read_bytes() == original_request
