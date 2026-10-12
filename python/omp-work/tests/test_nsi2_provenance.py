"""Assessment commands persist pinned proof without semantic writes or replay effects."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from omp_work.intake_repository import append_assessment, read_revision
from omp_work.v1 import intake_provenance
from omp_work.v1.api_models import IntakeAssessmentView
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_claims import intake_capability
from test_nsi2_provenance_closure import RUN, advance, append_body, make_body
from test_nsi2_repository import connection, locked, write_repository  # noqa: F401
from test_workflow_service import OWNER, _command, _grant, _owner_headers, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def intake(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, make_body(workspace_id, relay=False), uuid4(), -1)
    return write_repository


def command(revision=1, **extra):
    return dict(type="assess_intake", payload=dict(run_id=RUN, expected_revision=revision, **extra))


def envelope(workspace_id, revision=1):
    return CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id, operation_id=uuid4(),
        request_id=uuid4(), correlation_id=uuid4(), command=command(revision),
    ))


def get(service, workspace_id, suffix=""):
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN}{suffix}",
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    return response.json()


def counts(service, workspace_id):
    with connection(service, workspace_id) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "omp_work.intake_revisions", "omp_work.intake_assessments",
            "omp_work.intake_derivations", "omp_work.intake_derivation_claims",
            "omp_audit.domain_events", "omp_control.idempotent_commands",
        ))


@pytest.mark.parametrize("kind", ["owner", "rule_engine"])
def test_http_assessment_serializes_full_proof_and_replay_does_not_compute(service, intake, monkeypatch, kind):
    workspace_id, mission_id = intake
    actor_id, token = intake_capability(service, workspace_id, kind=kind, scopes=("work.approve", "work.read"))
    operation_id = uuid4()
    status, result = _command(service, workspace_id, command(), token=token, operation_id=operation_id)
    assert status == 200
    assert result["result"] == dict(type="assess_intake", run_id=RUN, revision=1, assessed_revision=1)
    view = get(service, workspace_id)
    assessment = view["assessments"][0]
    assert view["revision"] == view["provenance"]["revision"] == 1
    assert assessment["assessment"]["revision"] == assessment["binding"]["revision"] == 0
    assert assessment["provenance"]["revision"] == 0
    assert assessment["assessment"]["status"] == "READY_FOR_RATIFICATION"
    assert assessment["assessment"]["assessed_by"] == dict(id="omp-work:nsi2-provenance", kind="rule_engine")
    assert {r["id"] for r in assessment["receipts"]} == set(assessment["assessment"]["derivation_receipt_ids"])
    for receipt in assessment["receipts"]:
        assert {name: receipt[name] for name in assessment["binding"]} == assessment["binding"]
    assert view["provenance"] == {**assessment["provenance"], "revision": 1}
    assert view["provenance"]["claims"][0]["ancestry_ids"]
    assert view["provenance"]["sources"][0]["operation"]["event_id"]
    assert {t["criterion_id"]: t for t in view["provenance"]["traces"]} == {
        t["criterion_id"]: t for t in view["traces"]
    }
    store = PostgresWorkStore(service.config)
    assert store.read(workspace_id, OWNER, "intake", RUN) == view
    before = counts(service, workspace_id)
    assert before[:3] == (1, 1, len(assessment["receipts"]))
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute(
            "SELECT aggregate_type,aggregate_id,actor_id,actor_kind,payload FROM omp_audit.domain_events "
            "WHERE operation_id=%s", (operation_id,),
        ).fetchone() == ("mission", mission_id, actor_id, kind, result["result"])

    def unexpected(*args):
        pytest.fail("replay must bypass assessment computation")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    status, replay = _command(service, workspace_id, command(), token=token, operation_id=operation_id)
    assert status == 200
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == result["result"]
    assert counts(service, workspace_id) == before
    assert get(service, workspace_id) == view


def test_store_assessment_uses_caller_transaction_and_pinned_read(service, intake):
    workspace_id, _ = intake
    store = PostgresWorkStore(service.config)
    receipt, result = store.execute(envelope(workspace_id), actor_id=OWNER, actor_kind="owner", required_scope="work.approve")
    assert receipt.state == "applied"
    assert result["revision"] == result["assessed_revision"] == 1
    historical = get(service, workspace_id, "@1")
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        prior = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0)
        assert historical["assessments"][0] == prior.assessments[0].model_dump(mode="json")
        append_body(cur, intake, advance(prior.body, 1), uuid4(), 0)
    current = get(service, workspace_id)
    assert current["revision"] == 2
    assert current["assessments"] == []
    assert get(service, workspace_id, "@1") == historical


def test_stale_request_refused_before_computation(service, intake, monkeypatch):
    workspace_id, _ = intake
    before = counts(service, workspace_id)

    def unexpected(*args):
        pytest.fail("stale request must not read or compute proof")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    status, result = _command(service, workspace_id, command(2))
    assert status == 409
    assert result["error"]["code"] == "stale_intake"
    assert counts(service, workspace_id) == before


@pytest.mark.parametrize("kind", ["agent", "task-agent", "model", "tool", "client", "automation", "service"])
def test_approve_scope_does_not_authorize_other_kinds(service, intake, kind):
    workspace_id, _ = intake
    _, token = intake_capability(service, workspace_id, kind=kind, scopes=("work.approve", "work.read"))
    before = counts(service, workspace_id)
    status, result = _command(service, workspace_id, command(), token=token)
    assert status == 403
    assert result["error"]["code"] == "forbidden"
    assert counts(service, workspace_id) == before
    with pytest.raises(WorkStoreError, match="forbidden"):
        PostgresWorkStore(service.config).execute(
            envelope(workspace_id), actor_id=OWNER, actor_kind=kind, required_scope="work.approve",
        )
    assert counts(service, workspace_id) == before


@pytest.mark.parametrize("extra", [dict(verdict="READY_FOR_RATIFICATION"), dict(issuer="owner"), dict(assessed_by=dict(id="owner", kind="owner"))])
def test_forged_payload_refused_without_rows(service, intake, extra):
    workspace_id, _ = intake
    before = counts(service, workspace_id)
    status, result = _command(service, workspace_id, command(**extra))
    assert status == 400
    assert result["error"]["code"] == "invalid_request"
    assert counts(service, workspace_id) == before


def test_refusal_after_persistence_rolls_back_proof_journal_and_event(service, intake, monkeypatch):
    workspace_id, _ = intake
    before = counts(service, workspace_id)

    def refuse(cur, head, view, operation_id):
        append_assessment(cur, head, view, operation_id)
        raise WorkStoreError("invalid_request")

    monkeypatch.setattr(intake_provenance, "append_assessment", refuse)
    status, result = _command(service, workspace_id, command())
    assert status == 400
    assert result["error"]["code"] == "invalid_request"
    assert counts(service, workspace_id) == before


def test_reads_settle_explicit_persisted_findings_without_question_id_inference(service, intake):
    workspace_id, _ = intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        evaluation = intake_provenance.evaluate_provenance(cur, body)
        gap = dict(id="opaque-gap", dimension_id="missing_verification_oracle", premise_id="missing", reason="Need proof", materiality="material", waiver_policy="owner_with_reason")
        question = dict(id="opaque-question", question="What proves it?", category="completion_signals", gap_ids=["opaque-gap"], conflict_ids=[])
        assessment = evaluation.assessment.model_dump()
        assessment.update(status="READY_FOR_RATIFICATION", gaps=[gap])
        view = IntakeAssessmentView.model_validate(dict(
            id="stored-proof", assessment=assessment, binding=evaluation.binding,
            receipts=evaluation.receipts, provenance=evaluation.closure.closure,
            unresolved_issues=[dict(issue=question, gaps=["opaque-gap"])],
        ))
        append_assessment(cur, locked(cur, intake, expected=0, operation_id=uuid4()), view, uuid4())
    result = get(service, workspace_id)
    proof = result["assessments"][0]
    assert proof["assessment"]["status"] == "NEEDS_SPEC"
    assert proof["assessment"]["gaps"] == [gap]
    assert proof["unresolved_issues"] == result["issues"] == [dict(issue=question, gaps=["opaque-gap"])]
    assert proof["binding"] == evaluation.binding.model_dump()
    assert proof["receipts"] == [receipt.model_dump(mode="json") for receipt in evaluation.receipts]


def test_http_computed_findings_remain_visible_without_read_recomputation(service, write_repository, monkeypatch):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = make_body(workspace_id, relay=False)
        body.gaps.append(body.gaps[0].model_copy(update={"id": "open-gap"}))
        body.questions.append(body.questions[0].model_copy(update={
            "id": "open-question", "gap_ids": ["open-gap"],
        }))
        append_body(cur, write_repository, body, uuid4(), -1)
    status, result = _command(service, workspace_id, command())
    assert status == 200
    assert result["result"]["revision"] == 1

    def unexpected(*args):
        pytest.fail("GET must read persisted findings without computing")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    view = get(service, workspace_id)
    proof = view["assessments"][0]
    assert proof["assessment"]["status"] == "NEEDS_SPEC"
    gaps = {gap["id"] for gap in proof["assessment"]["gaps"]}
    assert gaps
    assert any(set(record["gaps"]) & gaps for record in proof["unresolved_issues"])
    assert proof["unresolved_issues"] == view["issues"]


def test_ready_read_does_not_revive_obsolete_bounded_findings(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False)
    body.gaps.append(body.gaps[0].model_copy(update={"id": "gap:oracle:obsolete"}))
    body.questions.append(body.questions[0].model_copy(update={
        "id": "question:oracle:obsolete", "gap_ids": ["gap:oracle:obsolete"],
    }))
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    assert _command(service, workspace_id, command())[0] == 200
    result = get(service, workspace_id)
    assert result["assessments"][0]["assessment"]["status"] == "READY_FOR_RATIFICATION"
    assert result["assessments"][0]["assessment"]["gaps"] == []
    assert result["issues"] == []
