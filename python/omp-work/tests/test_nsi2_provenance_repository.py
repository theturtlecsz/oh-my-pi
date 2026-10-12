"""Evaluation history stays bound to its semantic revision across reconnects."""
from __future__ import annotations

import os
from copy import deepcopy
from uuid import uuid4

import psycopg
import pytest

from omp_work.intake_repository import (
    IntakeRepositoryError, LockedHead, append_assessment, append_revision, lock_revision, read_revision,
)
from omp_work.contracts.nsi.v1 import models
from omp_work.v1.api_models import IntakeAssessmentView
from test_nsi2_repository import connection, revision_body, write_repository  # noqa: F401
from test_workflow_service import service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def lock(cur, repository, revision):
    workspace_id, mission_id = repository
    return lock_revision(cur, workspace_id=workspace_id, run_id="run-1",
                         expected_revision=revision, mission_id=mission_id, operation_id=uuid4())


@pytest.fixture
def semantic_revision(service, write_repository):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    body.update(assessments=[], derivations=[])
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, lock(cur, write_repository, -1), body, operation_id=uuid4())
    return write_repository, body


def assessment_view(body, suffix="one", ready=False):
    binding = {**body["binding"], "source_hash": "b" * 64, "claim_set_hash": "c" * 64}
    sample = revision_body(body["workspace_id"])
    receipt = {**sample["derivations"][0], **binding, "id": f"receipt-{suffix}",
               "created_at": "2026-10-10T12:34:56.123456+02:00"}
    assessment = {**sample["assessments"][0]["assessment"], **binding,
                  "status": "READY_FOR_RATIFICATION" if ready else "CONFLICTED",
                  "gaps": [] if ready else body["gaps"],
                  "conflicts": [] if ready else body["conflicts"],
                  "derivation_receipt_ids": [receipt["id"]]}
    return IntakeAssessmentView.model_validate(dict(
        id=f"evaluation-{suffix}", binding=binding, assessment=assessment, receipts=(receipt,),
        unresolved_issues=() if ready else (dict(issue=body["questions"][0], gaps=(body["gaps"][0]["id"],)),),
        provenance=dict(run_id="run-1", revision=0, source_sha256="d" * 64,
                        claims=(dict(claim=body["snapshot"]["claims"][0], ancestry_ids=("prior",),
                                     artifacts=tuple(body["snapshot"]["artifacts"])),),
                        admissions=(dict(claim_id=body["snapshot"]["claims"][0]["id"],
                                         admitted_by=dict(id="owner", kind="owner"),
                                         operation=dict(operation_id=uuid4(), event_id=uuid4())),),
                        traces=(dict(criterion_id="criterion", requirement_ids=("requirement",),
                                     claim_ids=("prior",)),)),
    ))


def test_full_evaluations_roundtrip_without_advancing_revision(service, semantic_revision):
    repository, body = semantic_revision
    workspace_id, _ = repository
    expected = [assessment_view(body, "one"), assessment_view(body, "two", ready=True)]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        locked = lock(cur, repository, 0)
        for view in expected:
            append_assessment(cur, locked, view, uuid4())
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (1,)
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        loaded = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        assert {view.id: view.model_dump(mode="json", exclude_unset=True) for view in loaded.assessments} == {
            view.id: view.model_dump(mode="json", exclude_unset=True) for view in expected}
        assert loaded.body.binding.source_hash != expected[0].binding.source_hash
        assert conn.execute("SELECT count(*) FROM omp_work.intake_derivation_claims").fetchone() == (4,)


def test_legacy_summaries_remain_readable_alongside_full_evaluations(service, write_repository):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    view = assessment_view(body)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, lock(cur, write_repository, -1), body, operation_id=uuid4())
        append_assessment(cur, lock(cur, write_repository, 0), view, uuid4())
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        loaded = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        assert loaded.assessments == (view,)
        legacy = loaded.body.assessments[0]
        assert legacy.id == body["assessments"][0]["id"]
        assert legacy.assessment.status == body["assessments"][0]["assessment"]["status"]
        assert legacy.assessment.source_hash == loaded.body.binding.source_hash
        assert legacy.assessment.derivation_receipt_ids == [body["derivations"][0]["id"]]
        assert [receipt.id for receipt in loaded.body.derivations] == [body["derivations"][0]["id"]]
        assert loaded.assessments[0].binding.source_hash != legacy.assessment.source_hash


def test_evaluations_keep_workspace_rls_and_immutability(service, semantic_revision):
    repository, body = semantic_revision
    workspace_id, _ = repository
    view = assessment_view(body)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_assessment(cur, lock(cur, repository, 0), view, uuid4())
    with connection(service, uuid4()) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="revision_not_found"):
            read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        for table in ("intake_assessments", "intake_derivations"):
            assert conn.execute(f"SELECT full_record FROM omp_work.{table}").fetchall() == []
        # A real locked key cannot bypass the connection's workspace claims.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            append_assessment(cur, LockedHead(workspace_id, "run-1", repository[1], 0), view, uuid4())
    with connection(service, workspace_id, "postgres") as conn:
        for table in ("intake_assessments", "intake_derivations"):
            with pytest.raises(psycopg.Error, match="immutable"), conn.transaction():
                conn.execute(f"UPDATE omp_work.{table} SET full_record='{{}}'::jsonb")
            with pytest.raises(psycopg.Error, match="immutable"), conn.transaction():
                conn.execute(f"DELETE FROM omp_work.{table}")
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            append_assessment(cur, LockedHead(workspace_id, "run-1", repository[1], 0),
                              assessment_view(body, "readonly"), uuid4())


@pytest.mark.parametrize("field", ["run", "revision", "assessment_hash", "receipt_hash", "provenance", "links"])
def test_mismatched_evaluation_scope_refused_before_inserts(service, semantic_revision, field):
    repository, body = semantic_revision
    workspace_id, _ = repository
    data = assessment_view(body).model_dump()
    if field in ("run", "revision"):
        data["binding"]["run_id" if field == "run" else "revision"] = "other" if field == "run" else 1
    elif field == "assessment_hash":
        data["assessment"]["schema_hash"] = "e" * 64
    elif field == "receipt_hash":
        data["receipts"][0]["contract_hash"] = "e" * 64
    elif field == "provenance":
        data["provenance"]["run_id"] = "other"
    else:
        data["assessment"]["derivation_receipt_ids"] = ["missing"]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="receipt_links_mismatch" if field == "links" else "scope_mismatch"):
            append_assessment(cur, lock(cur, repository, 0), IntakeAssessmentView.model_validate(data), uuid4())
        assert conn.execute("SELECT count(*) FROM omp_work.intake_assessments").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_derivations").fetchone() == (0,)
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)


def test_semantic_mutation_does_not_carry_ready_or_receipts_forward(service, semantic_revision):
    repository, body = semantic_revision
    workspace_id, _ = repository
    ready = assessment_view(body, ready=True)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_assessment(cur, lock(cur, repository, 0), ready, uuid4())
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        prior = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        changed = deepcopy(prior.body)
        changed.snapshot.revision = changed.binding.revision = 1
        changed.snapshot.claims[0].value = models.TypedStringValue(type="string", value="changed")
        changed.decisions[0].decision.revision = 1
        append_revision(cur, lock(cur, repository, 0), changed, operation_id=uuid4())
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        current = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=1)
        historical = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        assert current.assessments == ()
        assert current.body.assessments == current.body.derivations == []
        assert historical.assessments == (ready,)
        assert conn.execute("SELECT revision FROM omp_work.intake_assessments").fetchall() == [(0,)]
        assert conn.execute("SELECT revision FROM omp_work.intake_derivations").fetchall() == [(0,)]
