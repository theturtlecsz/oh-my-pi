"""Bounded analysis retains the persisted contract and refuses inconsistent proof."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.contracts.nsi.v1 import validate
from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, nsi_to_bounded
from omp_work.contracts.nsi.v1.models import TypedBooleanValue, TypedIntegerValue
from omp_work.contracts.nsi.v1.validate import closure_hash, content_hash, contract_hash
from omp_work.intake_repository import AssessmentRecord, DecisionRecord, append_revision, read_revision
from omp_work.v1.api_models import IntakeDecisionRecord, IntakeIssueRecord
from omp_work.v1 import intake_provenance
from omp_work.v1.intake_provenance import (
    build_provenance_binding,
    closure_document,
    evaluate_intake_provenance,
    evaluate_provenance,
    evaluate_provenance_projection,
    helper_sources,
    referenced_schemas,
    settle_intake_provenance,
    settle_provenance,
)
from omp_work.v1.intake_provenance_closure import resolve_provenance_closure
from omp_work.v1.models import (
    IntakeAcceptanceCriterion,
    IntakeConstraint,
    KnownIntakeValue,
    UnknownIntakeValue,
)
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_provenance_closure import (
    RUN, _criterion, _draft, _entities, _retarget, advance, append_body, insert_relay, make_body, relay_json, seal, sync_links,
)
from test_nsi2_repository import connection, locked, write_repository  # noqa: F401
from test_workflow_service import service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def proof(service, write_repository):
    workspace_id = write_repository[0]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, make_body(workspace_id, relay=False), uuid4(), -1)
        body = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        yield cur, body, resolve_provenance_closure(cur, body)


def test_actual_contract_ids_text_and_typed_results_survive(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    link = body.history.criteria[0]
    requirement = link.requirements[0].model_copy(update={"text": "Owner edited export requirement."})
    boolean = body.criteria[0].model_copy(update={"expected_result": TypedBooleanValue(type="boolean", value=True)})
    integer = body.criteria[1].model_copy(update={"expected_result": TypedIntegerValue(type="integer", value=1)})
    body.criteria = [boolean, integer]
    body.history.criteria = [
        link.model_copy(update={"criterion": boolean, "requirements": (requirement,)}),
        body.history.criteria[1].model_copy(update={"criterion": integer}),
    ]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        before = stored.model_dump(mode="json")
        resolved = resolve_provenance_closure(cur, stored)
        closure_before = resolved.closure.model_dump(mode="json")
        result = evaluate_intake_provenance(cur, stored)
        assert stored.model_dump(mode="json") == before
        assert resolved.closure.model_dump(mode="json") == closure_before
    requirements = {item.id: item for item in result.contract.requirements}
    assert "requirement:ac-export" not in requirements
    assert requirements["req-ac-export"].text == "Owner edited export requirement."
    assert result.contract.acceptance_criteria == stored.criteria
    assert result.contract.acceptance_criteria[0].requirement_ids == ["req-ac-export"]
    assert result.contract.acceptance_criteria[0].expected_result.type == "boolean"
    assert result.contract.acceptance_criteria[1].expected_result.type == "integer"
    altered = result.contract.model_copy(deep=True)
    altered.requirements[0].text = "Different words"
    assert content_hash(altered) != content_hash(result.contract)
    assert result.findings.issue_count == 0


def test_complete_proof_issues_linked_pinned_receipts_without_writes(proof):
    cur, body, resolved = proof
    before = body.model_dump(mode="python")
    result = evaluate_provenance(cur, body)
    assert result.assessment.status == "READY_FOR_RATIFICATION"
    assert result.unresolved == ()
    assert result.closure == resolved
    assert result.projection == resolved.projection
    assert result.assessment.derivation_receipt_ids == [item.id for item in result.receipts]
    assert len(set(result.assessment.derivation_receipt_ids)) == len(result.receipts)
    bound = result.binding.model_dump()
    assert {key: result.assessment.model_dump()[key] for key in bound} == bound
    validate.validate_instance("contract#/$defs/IntakeReadinessAssessment", result.assessment)
    for receipt in result.receipts:
        assert {key: receipt.model_dump()[key] for key in bound} == bound
        assert receipt.derived_by == result.assessment.assessed_by
        validate.validate_instance("status#/$defs/DerivationReceipt", receipt)
    claim_receipts = {item.conclusion_ids[0]: item for item in result.receipts if item.rule_id == "nsi2.provenance.claim"}
    assert set(claim_receipts) == {item.claim.id for item in resolved.closure.claims}
    for item in resolved.closure.claims:
        assert claim_receipts[item.claim.id].premise_ids == item.claim.premise_ids
        assert set(item.ancestry_ids) <= set(claim_receipts)
    assert claim_receipts["leaf"].premise_ids == ["mid"]
    assert claim_receipts["mid"].premise_ids == ["ancestor"]
    assert claim_receipts["ancestor"].premise_ids == ["span-export"]
    requirement_receipts = {item.conclusion_ids[0]: item for item in result.receipts if item.rule_id == "nsi2.provenance.requirement"}
    for requirement in result.contract.requirements:
        assert requirement_receipts[requirement.id].premise_ids == requirement.claim_ids
    criterion_receipts = {item.conclusion_ids[0]: item for item in result.receipts if item.rule_id == "nsi2.provenance.criterion"}
    for criterion in result.contract.acceptance_criteria:
        assert criterion_receipts[criterion.id].premise_ids == criterion.requirement_ids
    repeated = evaluate_provenance(cur, body)
    assert repeated.binding == result.binding
    assert [item.id for item in repeated.receipts] == result.assessment.derivation_receipt_ids
    assert body.model_dump(mode="python") == before
    assert read_revision(cur, workspace_id=body.workspace_id, run_id=RUN, revision=0).body.model_dump(mode="python") == before
    cur.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id=%s", (body.workspace_id, RUN))
    assert cur.fetchone() == (0,)
    cur.execute("SELECT count(*) FROM omp_work.intake_assessments WHERE workspace_id=%s AND run_id=%s", (body.workspace_id, RUN))
    assert cur.fetchone() == (0,)
    cur.execute("SELECT count(*) FROM omp_work.intake_derivations WHERE workspace_id=%s AND run_id=%s", (body.workspace_id, RUN))
    assert cur.fetchone() == (0,)


@pytest.mark.parametrize("missing", ["root", "ancestor_truth"])
def test_incomplete_consumed_proof_never_certifies_ready(proof, monkeypatch, missing):
    cur, body, resolved = proof
    # Exercise incomplete resolver output after resolving real PostgreSQL proof.
    if missing == "root":
        incomplete = replace(resolved, closure=resolved.closure.model_copy(update={
            "claims": tuple(item for item in resolved.closure.claims if item.claim.id != "leaf"),
        }))
    else:
        incomplete = replace(resolved, truths=tuple(item for item in resolved.truths if item[0] != "ancestor"))
    monkeypatch.setattr(intake_provenance, "resolve_provenance_closure", lambda _cur, _body: incomplete)
    result = evaluate_provenance(cur, body)
    assert result.unresolved == ()
    assert result.assessment.status == "NEEDS_SPEC"


@pytest.mark.parametrize("blocker,status", [("conflict", "CONFLICTED"), ("gap", "NEEDS_SPEC"), ("question", "NEEDS_SPEC")])
def test_open_findings_never_certify_ready(service, write_repository, blocker, status):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    if blocker == "conflict":
        body.conflicts = [nsi.Conflict(id="open", claim_ids=["ancestor", "leaf"], predicate_id="bounded.annotation", reason="Unsettled")]
    elif blocker == "gap":
        body.gaps.append(body.gaps[0].model_copy(update={"id": "open"}))
    else:
        body.questions.append(body.questions[0].model_copy(update={"id": "open", "gap_ids": []}))
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        result = evaluate_provenance(cur, stored)
        assert result.closure.ready is True
        assert result.assessment.status == status
        assert "open" in {item.id for item in result.unresolved}


def test_absent_method_refused_by_pinned_evaluator(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    body.criteria[0].verification_method = "absent"
    body.history.criteria[0].criterion.verification_method = "absent"
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        with pytest.raises(WorkStoreError) as caught:
            evaluate_provenance(cur, stored)
        assert caught.value.diagnostics == ("incompatible verification method",)


@pytest.mark.parametrize("edit", ["actor", "source_revision", "relation", "binary", "requirement", "finding", "link"])
def test_same_revision_content_edits_change_pinned_binding(service, write_repository, edit):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    payload = b"\x00\xff"
    body.snapshot.artifacts.append(nsi.EvidenceArtifact(
        id="binary", uri="intake:binary", revision="binary-1", actor=nsi.ActorRef(id="owner", kind="owner"),
        media_type="application/octet-stream", content_hash=hashlib.sha256(payload).hexdigest(),
    ))
    body.artifact_bytes["binary"] = payload
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        operation_id = uuid4()
        cur.execute("SAVEPOINT seed")
        append_revision(cur, locked(cur, write_repository, expected=-1, operation_id=operation_id),
                        seal(sync_links(body)), operation_id=operation_id)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        original = evaluate_provenance(cur, stored)
        cur.execute("ROLLBACK TO SAVEPOINT seed")
        # Re-seed same revision and operation, changing exactly one content input.
        if edit == "actor":
            next(claim for claim in stored.snapshot.claims if claim.id == "ancestor").asserted_by.id = "other-owner"
        elif edit == "source_revision":
            next(artifact for artifact in stored.snapshot.artifacts if artifact.id == "bounded-source").revision = "edited"
        elif edit == "relation":
            next(link for link in stored.evidence_links if link.claim_id == "ancestor").relation = "contradicts"
        elif edit == "binary":
            next(artifact for artifact in stored.snapshot.artifacts if artifact.id == "binary").media_type = "application/x-binary"
        elif edit == "requirement":
            stored.history.criteria[0].requirements[0].text = "Edited owner requirement"
        elif edit == "finding":
            stored.gaps.append(stored.gaps[0].model_copy(update={"id": "new-gap"}))
        else:
            stored.questions[0].gap_ids = []
        append_revision(cur, locked(cur, write_repository, expected=-1, operation_id=operation_id),
                        seal(stored), operation_id=operation_id)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        changed = evaluate_provenance(cur, stored)
        assert changed.binding.revision == original.binding.revision == 0
        assert changed.binding != original.binding
        assert changed.assessment.derivation_receipt_ids != original.assessment.derivation_receipt_ids
        assert changed.assessment.status == ("NEEDS_SPEC" if edit in {"finding", "link"} else "READY_FOR_RATIFICATION")


@pytest.mark.parametrize("method", ["absent", "manual_inspection"])
def test_stored_method_cannot_borrow_projected_oracle(proof, method):
    _cur, body, resolved = proof
    criterion = body.criteria[0].model_copy(update={"verification_method": method})
    body.criteria[0] = criterion
    body.history.criteria[0] = body.history.criteria[0].model_copy(update={"criterion": criterion})
    with pytest.raises(WorkStoreError) as caught:
        evaluate_provenance_projection(body, resolved)
    assert caught.value.code == "invalid_request"
    assert caught.value.diagnostics == ("incompatible verification method",)


@pytest.mark.parametrize("failure", ["criterion_copy", "typed_copy", "missing_history", "missing_trace", "missing_requirement", "unsupported"])
def test_inconsistent_or_unsupported_proof_refuses(proof, failure):
    _cur, body, resolved = proof
    if failure == "criterion_copy":
        body.criteria[0] = body.criteria[0].model_copy(update={"description": "Different copy"})
    elif failure == "typed_copy":
        body.criteria[0] = body.criteria[0].model_copy(update={"expected_result": TypedBooleanValue(type="boolean", value=True)})
        body.history.criteria[0] = body.history.criteria[0].model_copy(update={
            "criterion": body.criteria[0].model_copy(update={"expected_result": TypedIntegerValue(type="integer", value=1)}),
        })
    elif failure == "missing_history":
        body.history.criteria = []
    elif failure == "missing_trace":
        resolved = replace(resolved, closure=resolved.closure.model_copy(update={"traces": ()}))
    elif failure == "missing_requirement":
        body.history.criteria[0] = body.history.criteria[0].model_copy(update={"requirements": ()})
    else:
        resolved = replace(resolved, projection=resolved.projection.model_copy(update={"archetype": "unsupported"}))
    with pytest.raises(WorkStoreError) as caught:
        evaluate_provenance_projection(body, resolved)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("inconsistent", [False, True])
def test_shared_requirements_deduplicate_or_refuse(service, write_repository, inconsistent):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    shared = body.history.criteria[0].requirements[0].model_copy(update={
        "claim_ids": ["ac-export", "ac-table", "goal-ready"],
    })
    criteria, links = [], []
    for index, link in enumerate(body.history.criteria):
        criterion = link.criterion.model_copy(update={"requirement_ids": [shared.id]})
        requirement = shared.model_copy(deep=True)
        if inconsistent and index == 1:
            requirement.text = "Conflicting copy"
        criteria.append(criterion)
        links.append(link.model_copy(update={"criterion": criterion, "requirements": (requirement,)}))
    body.criteria, body.history.criteria = criteria, links
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        if inconsistent:
            with pytest.raises(WorkStoreError) as caught:
                evaluate_intake_provenance(cur, stored)
            assert caught.value.code == "invalid_request"
            assert caught.value.diagnostics == ("inconsistent requirement",)
        else:
            result = evaluate_intake_provenance(cur, stored)
            assert [item for item in result.contract.requirements if item.id == shared.id] == [shared]
            assert all(item.requirement_ids == [shared.id] for item in result.contract.acceptance_criteria)


@pytest.mark.parametrize("truth", ["false", "unknown"])
@pytest.mark.parametrize("claim_id", ["goal-ready", "ancestor"])
def test_clean_analysis_does_not_certify_original_false_premise(service, write_repository, truth, claim_id):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    body.snapshot.state = "needs_spec"
    body.snapshot.claims = [
        claim.model_copy(update={"truth": truth}) if claim.id == claim_id else claim
        for claim in body.snapshot.claims
    ]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        result = evaluate_provenance_projection(stored, resolved)
        assert result.findings.issue_count == 0
        assert dict(resolved.truths)[claim_id] == truth
        assert resolved.ready is False
        assert next(claim.truth for claim in stored.snapshot.claims if claim.id == claim_id) == truth
        pinned = evaluate_provenance(cur, stored)
        assert pinned.unresolved == ()  # Owner settlement cannot promote original truth.
        assert pinned.assessment.status == "NEEDS_SPEC"


def test_analysis_preserves_typed_conflict_and_unknown_truth(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, drop_unknowns=False, relay=False)
    body.snapshot.state = "needs_spec"
    constraints = tuple(IntakeConstraint(
        id=claim_id, statement=claim_id, source_span_ids=("span-export",), key=key, value=value, polarity="positive",
    ) for claim_id, key, value in (
        ("typed-bool", "typed", KnownIntakeValue(value=True)),
        ("typed-int", "typed", KnownIntakeValue(value=1)),
        ("typed-unknown", "unknown", UnknownIntakeValue()),
    ))
    draft = _draft(drop_unknowns=False).model_copy(update={"constraints": constraints})
    typed_claims = [claim for claim in bounded_to_nsi(draft).claims if claim.predicate_id in {
        "bounded.constraint", "bounded.annotation",
    }]
    body.snapshot.claims.extend(typed_claims)
    body.snapshot.entities = _entities(body.snapshot.claims)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        before = resolved.projection.model_dump(mode="json")
        result = evaluate_provenance_projection(stored, resolved)
        roundtrip = nsi_to_bounded(bounded_to_nsi(resolved.projection))
        by_id = {item.id: item for item in roundtrip.constraints}
        assert type(by_id["typed-bool"].value.value) is bool
        assert type(by_id["typed-int"].value.value) is int
        assert isinstance(by_id["typed-unknown"].value, UnknownIntakeValue)
        assert len(result.findings.conflicts) == 1
        assert set(result.findings.conflicts[0].claim_ids) == {"typed-bool", "typed-int"}
        assert dict(resolved.truths)["choice-name"] == "unknown"
        assert dict(resolved.truths)["choice-sort"] == "unknown"
        assert dict(resolved.truths)["typed-unknown"] == "unknown"
        assert resolved.ready is False
        assert resolved.projection.model_dump(mode="json") == before
        pinned = evaluate_provenance(cur, stored)
        assert pinned.assessment.status == "CONFLICTED"
        assert set(pinned.assessment.conflicts[0].claim_ids) == {"typed-bool", "typed-int"}


def test_same_id_conflict_membership_growth_defeats_old_coverage(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    conflict = nsi.Conflict(
        id="conflict-1", claim_ids=["goal-ready", "ancestor"],
        predicate_id="bounded.constraint", reason="Original conflict",
    )
    body.conflicts = [conflict]
    body.questions[0].conflict_ids = ["conflict-1"]
    body.history.decisions[0] = body.history.decisions[0].model_copy(update={
        "claim_ids": ("goal-ready", "ancestor"),
    })
    body.decisions[0].settled_claim_ids = ["goal-ready", "ancestor"]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        initial = settle_provenance(stored, resolved)
        assert initial.unresolved == ()

        # Same-ID conflict membership grows to require a third claim.
        # Broader revision copy cannot defeat immutable history coverage.
        grown = stored.model_copy(deep=True)
        grown.conflicts[0] = conflict.model_copy(update={
            "claim_ids": ["goal-ready", "ancestor", "leaf"],
        })
        grown.decisions[0].settled_claim_ids = ["goal-ready", "ancestor", "leaf"]
        before = grown.model_dump(mode="json")
        closure_before = resolved.closure.model_dump(mode="json")

        result = settle_provenance(grown, resolved)
        assert grown.model_dump(mode="json") == before
        assert resolved.closure.model_dump(mode="json") == closure_before
        assert {item.id for item in result.unresolved} >= {"conflict-1", "question-1"}
        settled_conflict = next(c for c in result.settlement.conflicts if c.id == "conflict-1")
        assert settled_conflict.claim_ids == ["goal-ready", "ancestor", "leaf"]


def test_latest_conflict_gap_links_and_empty_override(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    conflict = nsi.Conflict(
        id="conflict-1", claim_ids=["goal-ready", "ancestor"],
        predicate_id="bounded.constraint", reason="Constraint conflict",
    )
    body.conflicts = [conflict]
    body.questions[0].gap_ids = []
    body.questions[0].conflict_ids = ["conflict-1"]
    body.history.decisions[0] = body.history.decisions[0].model_copy(update={
        "claim_ids": ("goal-ready", "ancestor"),
    })
    body.decisions[0].settled_claim_ids = ["goal-ready", "ancestor"]
    body.history.issues = [
        IntakeIssueRecord(issue=conflict, gaps=("gap-1",)),
        IntakeIssueRecord(issue=conflict, gaps=()),  # empty override!
    ]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        before = stored.model_dump(mode="json")

        result = settle_provenance(stored, resolved)
        assert stored.model_dump(mode="json") == before
        assert result.links["conflict-1"] == ()
        # Empty override unlinked gap-1, so settling the question settled conflict-1 but left gap-1 open.
        assert {item.id for item in result.unresolved} == {"gap-1"}

        # Non-empty latest links preserve gap-1 while trimming obsolete targets.
        with_gap = stored.model_copy(deep=True)
        with_gap.history.issues.append(
            IntakeIssueRecord(issue=conflict, gaps=("gap-1", "obsolete-gap")),
        )
        linked_result = settle_provenance(with_gap, resolved)
        assert linked_result.links["conflict-1"] == ("gap-1",)
        assert linked_result.unresolved == ()


def test_regenerated_question_retaining_open_conflict(service, write_repository):
    workspace_id = write_repository[0]
    draft = _draft(drop_unknowns=False)
    draft = draft.model_copy(update={"acceptance_criteria": tuple(
        ac.model_copy(update={"oracle": None}) if ac.id == "ac-export" else ac
        for ac in draft.acceptance_criteria
    )})
    body = make_body(workspace_id, relay=False)
    snapshot = bounded_to_nsi(draft).model_copy(update={"run_id": RUN, "revision": 0})
    extra_claims = [c for c in body.snapshot.claims if c.id in {"ancestor", "mid", "sibling", "leaf", "late", "retired"}]
    all_claims = _retarget(list(snapshot.claims)) + extra_claims
    body.snapshot = snapshot.model_copy(update={
        "artifacts": body.snapshot.artifacts,
        "claims": all_claims,
        "entities": _entities(all_claims),
    })
    c1, l1 = _criterion("ac-export", "req-ac-export", "Owner edited export requirement.", "absent", ["ac-export", "goal-ready"])
    body.criteria = [c1, body.criteria[1]]
    body.history.criteria = [l1, body.history.criteria[1]]

    live_conflict = nsi.Conflict(
        id="conflict:live", claim_ids=["goal-ready", "ancestor"],
        predicate_id="bounded.constraint", reason="Live conflict",
    )
    body.conflicts = [live_conflict]
    body.gaps = [
        body.gaps[0],
        nsi.Gap(
            id="gap:oracle:ac-export", dimension_id="missing_verification_oracle", premise_id="ac-export",
            reason="Export requirement oracle missing", materiality="critical", waiver_policy="never",
        ),
    ]
    persisted_q = nsi.ClarificationQuestion(
        id="question:oracle:ac-export",
        question="Export requirement oracle missing",
        gap_ids=["gap:oracle:ac-export"],
        conflict_ids=["conflict:live"],
        category="completion_signals",
    )
    body.questions = [body.questions[0], persisted_q]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        before = stored.model_dump(mode="json")

        evaluation = evaluate_provenance_projection(stored, resolved, settle=False)
        assert any(q.id == "question:oracle:ac-export" for q in evaluation.findings.questions)

        result = settle_provenance(stored, resolved)
        assert stored.model_dump(mode="json") == before
        q = next(item for item in result.settlement.questions if item.id == "question:oracle:ac-export")
        assert "conflict:live" in q.conflict_ids
        assert any(item.id == "conflict:live" for item in result.unresolved)


def test_broader_body_coverage_does_not_replace_original_owner_coverage(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)

        body_probe = stored.model_copy(deep=True)
        # Original history decision covers ("goal-ready", "late")
        body_probe.history.decisions[0] = body_probe.history.decisions[0].model_copy(update={
            "claim_ids": ("goal-ready", "late"),
        })
        # Broader revision copy covers ("goal-ready", "leaf")
        body_probe.decisions[0].settled_claim_ids = ["goal-ready", "leaf"]

        # Remove "late" from snapshot claims so history decision has an obsolete target
        body_probe.snapshot.claims = [c for c in body_probe.snapshot.claims if c.id != "late"]
        body_probe.snapshot.entities = _entities(body_probe.snapshot.claims)

        # Current live conflict requires ("goal-ready", "leaf")
        live_conflict = nsi.Conflict(
            id="conflict:live", claim_ids=["goal-ready", "leaf"],
            predicate_id="bounded.constraint", reason="Conflict requiring leaf",
        )
        body_probe.conflicts = [live_conflict]
        body_probe.questions[0].conflict_ids = ["conflict:live"]

        result = settle_provenance(body_probe, resolved)
        # Broader body coverage must NOT replace owner history coverage.
        # Conflict remains unresolved because owner never authorized leaf.
        assert any(item.id == "conflict:live" for item in result.unresolved)


def test_superseded_decision_does_not_reactivate_when_successor_has_stale_claims(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)

        # D1 covers ("goal-ready", "leaf")
        d1_record = stored.history.decisions[0].model_copy(update={
            "claim_ids": ("goal-ready", "leaf"),
        })
        # D2 supersedes D1 on same question, covers ("goal-ready", "late")
        d2_decision = d1_record.decision.model_copy(update={
            "id": "decision-2",
            "reason": "Superseding decision covering late",
        })
        d2_record = IntakeDecisionRecord(
            decision=d2_decision,
            claim_ids=("goal-ready", "late"),
            instruction=d1_record.instruction,
            superseded_decision_id="decision-1",
        )
        body_probe = stored.model_copy(deep=True)
        body_probe.history.decisions = [d1_record, d2_record]
        body_probe.decisions = [
            DecisionRecord(decision=d1_record.decision.model_copy(deep=True), settled_claim_ids=["goal-ready", "leaf"]),
            DecisionRecord(decision=d2_decision.model_copy(deep=True), settled_claim_ids=["goal-ready", "late"]),
        ]

        # Remove "late" from snapshot claims so D2 has an obsolete target
        body_probe.snapshot.claims = [c for c in body_probe.snapshot.claims if c.id != "late"]
        body_probe.snapshot.entities = _entities(body_probe.snapshot.claims)

        # Current live conflict requires ("goal-ready", "leaf")
        live_conflict = nsi.Conflict(
            id="conflict:live", claim_ids=["goal-ready", "leaf"],
            predicate_id="bounded.constraint", reason="Conflict requiring leaf",
        )
        body_probe.conflicts = [live_conflict]
        body_probe.questions[0].conflict_ids = ["conflict:live"]

        result = settle_provenance(body_probe, resolved)
        # D2 drops due to obsolete target "late".
        # D1 was superseded in original history, so it must not reactivate and settle the conflict.
        assert any(item.id == "conflict:live" for item in result.unresolved)


def test_obsolete_derived_gap_and_question_with_retained_decision(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    # Revision 0 has an obsolete derived gap/question answered by decision-1
    body.gaps[0] = body.gaps[0].model_copy(update={"id": "gap:oracle:obsolete-ac"})
    body.questions[0] = body.questions[0].model_copy(update={
        "id": "question:oracle:obsolete-ac",
        "gap_ids": ["gap:oracle:obsolete-ac"],
    })
    body.history.decisions[0].decision.question_id = "question:oracle:obsolete-ac"
    body.decisions[0].decision.question_id = "question:oracle:obsolete-ac"

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        # Commit revision 0
        append_body(cur, write_repository, body, uuid4(), -1)

        # Advance to revision 1 where obsolete derived gap/question is no longer derived
        rev1 = advance(body, 1)
        rev1.gaps = []
        rev1.questions = []
        rev1.decisions = []
        rev1.instructions = []
        append_body(cur, write_repository, rev1, uuid4(), 0)

        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=1).body
        resolved = resolve_provenance_closure(cur, stored)
        before_history = [d.model_dump(mode="json") for d in stored.history.decisions]

        # Stored body with retained obsolete question decision fails unresolved_findings directly.
        with pytest.raises(WorkStoreError) as caught:
            from omp_work.v1.intake_decisions import unresolved_findings
            unresolved_findings(stored)
        assert caught.value.code == "invalid_request"

        # settle_provenance reconciles decision targets on the deep copy without raising invalid_request.
        result = settle_provenance(stored, resolved)
        assert [d.model_dump(mode="json") for d in stored.history.decisions] == before_history
        assert not any(item.id == "gap:oracle:obsolete-ac" for item in result.settlement.gaps)
        assert not any(item.id == "question:oracle:obsolete-ac" for item in result.settlement.questions)
        assert not any(d.decision.question_id == "question:oracle:obsolete-ac" for d in result.settlement.history.decisions)
        assert result.unresolved == ()


def test_unrelated_persisted_blockers_survive_merge(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    persisted_gap = nsi.Gap(
        id="unrelated-gap", dimension_id="missing_verification_oracle", premise_id="goal-ready",
        reason="Unrelated blocker gap", materiality="critical", waiver_policy="never",
    )
    persisted_conflict = nsi.Conflict(
        id="unrelated-conflict", claim_ids=["goal-ready", "ancestor"],
        predicate_id="bounded.constraint", reason="Unrelated blocker conflict",
    )
    body.gaps = [body.gaps[0], persisted_gap]
    body.conflicts = [persisted_conflict]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        result = settle_provenance(stored, resolved)

        assert any(g.id == "unrelated-gap" for g in result.settlement.gaps)
        assert any(c.id == "unrelated-conflict" for c in result.settlement.conflicts)
        assert any(item.id == "unrelated-gap" for item in result.unresolved)
        assert any(item.id == "unrelated-conflict" for item in result.unresolved)


def test_uncapped_gaps_preserved_beyond_ui_cap(service, write_repository):
    workspace_id = write_repository[0]
    draft = _draft(drop_unknowns=False).model_copy(update={"acceptance_criteria": (
        IntakeAcceptanceCriterion(
            id="ac-1", statement="one", source_span_ids=("span-export",), observable_outcome="out1", oracle=None,
        ),
        IntakeAcceptanceCriterion(
            id="ac-2", statement="two", source_span_ids=("span-export",), observable_outcome="out2", oracle=None,
        ),
        IntakeAcceptanceCriterion(
            id="ac-3", statement="three", source_span_ids=("span-export",), observable_outcome="out3", oracle=None,
        ),
    )})
    body = make_body(workspace_id, relay=False)
    snapshot = bounded_to_nsi(draft).model_copy(update={"run_id": RUN, "revision": 0})
    extra_claims = [c for c in body.snapshot.claims if c.id in {"ancestor", "mid", "sibling", "leaf", "late", "retired"}]
    all_claims = _retarget(list(snapshot.claims)) + extra_claims
    body.snapshot = snapshot.model_copy(update={
        "artifacts": body.snapshot.artifacts,
        "claims": all_claims,
        "entities": _entities(all_claims),
    })
    c1, l1 = _criterion("ac-1", "req-ac-1", "one", "absent", ["ac-1", "goal-ready"])
    c2, l2 = _criterion("ac-2", "req-ac-2", "two", "absent", ["ac-2", "goal-ready"])
    c3, l3 = _criterion("ac-3", "req-ac-3", "three", "absent", ["ac-3", "goal-ready"])
    body.criteria = [c1, c2, c3]
    body.history.criteria = [l1, l2, l3]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        result = settle_provenance(stored, resolved)

        oracle_gaps = [item for item in result.settlement.gaps if item.id.startswith("gap:oracle:")]
        assert len(oracle_gaps) == 3
        unresolved_oracle_gaps = [item for item in result.unresolved if item.id.startswith("gap:oracle:")]
        assert len(unresolved_oracle_gaps) == 3
        pinned = evaluate_provenance(cur, stored)
        assert pinned.assessment.status == "NEEDS_SPEC"
        assert {item.id for item in pinned.assessment.gaps} >= {item.id for item in unresolved_oracle_gaps}


@pytest.fixture
def complete_proof(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id)
    payload = b"\x00\xff\xfe\x80"
    body.snapshot.artifacts.append(nsi.EvidenceArtifact(
        id="binary", uri="intake:binary", revision="binary-rev-1",
        actor=nsi.ActorRef(id="binary-owner", kind="owner"),
        media_type="application/octet-stream", content_hash=hashlib.sha256(payload).hexdigest(),
    ))
    body.artifact_bytes["binary"] = payload
    body.conflicts = [nsi.Conflict(
        id="open-conflict", claim_ids=["ancestor", "leaf"],
        predicate_id="bounded.constraint", reason="Unsettled conflict",
    )]
    body.history.issues = [IntakeIssueRecord(issue=body.conflicts[0], gaps=("gap-1",))]
    body.questions[0].conflict_ids = ["open-conflict"]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        record = body.history.decisions[0]
        insert_relay(cur, workspace_id, write_repository[1], record.verified_relay_operation_id, relay_json(record.instruction))
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        yield stored, resolve_provenance_closure(cur, stored)


def _document(body, resolved):
    evaluation = evaluate_provenance_projection(body, resolved)
    return closure_document(body, resolved, evaluation.contract, evaluation.settlement)


def _edited(current, path, value):
    """Copy nested proof records, including frozen instruction/operation models."""
    if not path:
        return value
    key, *rest = path
    child = current[key] if isinstance(current, (dict, list, tuple)) else getattr(current, key)
    changed = _edited(child, rest, value)
    if isinstance(current, dict):
        return {**current, key: changed}
    if isinstance(current, (list, tuple)):
        items = list(current)
        items[key] = changed
        return tuple(items) if isinstance(current, tuple) else items
    return current.model_copy(update={key: changed})


def test_closure_is_lossless_repeatable_and_excludes_feedback(complete_proof):
    body, resolved = complete_proof
    before = body.model_dump(mode="python")
    document = _document(body, resolved)
    wire = json.loads(json.dumps(document))
    assert bytes.fromhex(wire["artifact_bytes_hex"]["binary"]) == body.artifact_bytes["binary"]
    assert all(source.artifact.id != "binary" for source in resolved.closure.sources)
    assert closure_hash(wire) == closure_hash(_document(body, resolved))
    assert body.model_dump(mode="python") == before
    altered = body.model_copy(deep=True)
    altered.binding = altered.binding.model_copy(update={"source_hash": "ab" * 32, "claim_set_hash": "cd" * 32})
    # Assessment/derivation payloads must never feed the next assessment digest.
    hashes = {key: "ef" * 32 for key in (
        "contract_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "source_hash",
    )}
    engine = nsi.RuleEngineRef(id="previous-engine", kind="rule_engine")
    altered.assessments = [AssessmentRecord(id="previous-assessment", assessment=nsi.IntakeReadinessAssessment(
        run_id=RUN, revision=0, **hashes, status="CONFLICTED", gaps=[], conflicts=body.conflicts,
        derivation_receipt_ids=["previous-receipt"], assessed_by=engine, created_at="2026-10-10T00:00:00Z",
    ))]
    altered.derivations = [nsi.DerivationReceipt(
        id="previous-receipt", run_id=RUN, revision=0, **hashes, rule_id="previous-rule",
        premise_ids=["ancestor"], conclusion_ids=["leaf"], derived_by=engine, created_at="2026-10-10T00:00:00Z",
    )]
    assert closure_hash(_document(altered, resolved)) == closure_hash(document)


@pytest.mark.parametrize("field,value", [
    ("uri", "intake:binary-other"),
    ("revision", "binary-rev-2"),
    ("actor", nsi.ActorRef(id="other-owner", kind="owner")),
    ("media_type", "application/x-binary"),
    ("content_hash", "ab" * 32),
    ("bytes", b"\x00\xff\xfe\x81"),
])
def test_unreferenced_binary_artifact_changes_closure_digest(complete_proof, field, value):
    body, resolved = complete_proof
    original = closure_hash(_document(body, resolved))
    altered = body.model_copy(deep=True)
    if field == "bytes":
        altered.artifact_bytes["binary"] = value
        altered.snapshot.artifacts[-1].content_hash = hashlib.sha256(value).hexdigest()
    else:
        setattr(altered.snapshot.artifacts[-1], field, value)
    assert closure_hash(_document(altered, resolved)) != original


@pytest.mark.parametrize("target,path,value", [
    ("body", ("snapshot", "claims", 0, "asserted_by", "id"), "other-assertor"),
    ("body", ("snapshot", "claims", 0, "admitted_by", "id"), "other-admitter"),
    ("closure", ("admissions", 0, "admitted_by", "id"), "other-admitter"),
    ("closure", ("admissions", 0, "operation", "operation_id"), uuid4()),
    ("assertion", ("operation", "event_id"), uuid4()),
    ("assertion", ("operation", "operation_id"), uuid4()),
    ("body", ("evidence_links", 0, "relation"), "contradicts"),
    ("body", ("evidence_links", 0, "start_byte"), 1),
    ("body", ("evidence_links", 0, "quote"), "Other exact words"),
    ("body", ("history", "decisions", 0, "instruction", "text"), "Other owner words"),
    ("body", ("history", "instruction_hashes", "decision-1"), "ab" * 32),
    ("body", ("history", "decisions", 0, "claim_ids"), ("ancestor",)),
    ("body", ("history", "decisions", 0, "superseded_decision_id"), "older-decision"),
    ("body", ("history", "decisions", 0, "verified_relay_operation_id"), uuid4()),
    ("closure", ("decisions", 0, "operation", "event_id"), uuid4()),
    ("closure", ("relays", 0, "operation", "event_id"), uuid4()),
    ("closure", ("relays", 0, "relay", "instruction", "source_message_ref"), "msg:other"),
    ("closure", ("supersessions", 0, "operation", "event_id"), uuid4()),
    ("closure", ("supersessions", 0, "decision_id"), "other-decision"),
    ("body", ("gaps", 0, "reason"), "Other open gap"),
    ("body", ("conflicts", 0, "reason"), "Other open conflict"),
    ("body", ("questions", 0, "question"), "Other question?"),
    ("body", ("questions", 0, "conflict_ids"), []),
    ("body", ("history", "issues", 0, "gaps"), ()),
    ("closure", ("claims", 0, "ancestry_ids"), ("ancestor",)),
    ("closure", ("traces", 0, "claim_ids"), ("ac-export",)),
])
def test_proof_and_finding_edits_change_closure_digest(complete_proof, target, path, value):
    body, resolved = complete_proof
    original = closure_hash(_document(body, resolved))
    body = body.model_copy(deep=True)
    resolved = replace(
        resolved, closure=resolved.closure.model_copy(deep=True),
        assertions=tuple(replace(item, operation=item.operation.model_copy(deep=True)) for item in resolved.assertions),
    )
    if target == "body":
        body = _edited(body, path, value)
    elif target == "closure":
        resolved = replace(resolved, closure=_edited(resolved.closure, path, value))
        if path == ("traces", 0, "claim_ids"):
            criterion_id = resolved.closure.traces[0].criterion_id
            index = next(index for index, link in enumerate(body.history.criteria) if link.criterion.id == criterion_id)
            body = _edited(body, ("history", "criteria", index, "requirements", 0, "claim_ids"), list(value))
    else:
        assertion = replace(resolved.assertions[0], operation=_edited(resolved.assertions[0].operation, path[1:], value))
        resolved = replace(resolved, assertions=(assertion, *resolved.assertions[1:]))
    assert closure_hash(_document(body, resolved)) != original


def test_effective_settlement_links_and_unresolved_membership_are_bound(complete_proof):
    body, resolved = complete_proof
    evaluation = evaluate_provenance_projection(body, resolved)
    settlement = evaluation.settlement
    assert settlement.links["open-conflict"] == ("gap-1",)
    assert {item.id for item in settlement.unresolved} >= {"open-conflict", "question-1"}
    original = closure_hash(closure_document(body, resolved, evaluation.contract, settlement))
    unlinked = settlement._replace(links={"open-conflict": ()})
    assert closure_hash(closure_document(body, resolved, evaluation.contract, unlinked)) != original
    covered = settlement._replace(unresolved=())
    assert closure_hash(closure_document(body, resolved, evaluation.contract, covered)) != original


def test_stored_requirement_words_change_contract_and_closure_hashes(proof):
    _cur, body, resolved = proof
    evaluation = evaluate_provenance_projection(body, resolved)
    original = closure_document(body, resolved, evaluation.contract, evaluation.settlement)
    altered = body.model_copy(deep=True)
    altered.history.criteria[0].requirements[0].text = "Owner changed required export behavior."
    changed = evaluate_provenance_projection(altered, resolved)
    assert contract_hash(changed.contract) != contract_hash(evaluation.contract)
    assert closure_hash(closure_document(altered, resolved, changed.contract, changed.settlement)) != closure_hash(original)


def test_closure_binds_history_even_when_settlement_filters_stale_decisions(proof):
    _cur, body, resolved = proof
    body = body.model_copy(deep=True)
    body.questions = []
    evaluation = evaluate_provenance_projection(body, resolved)
    assert evaluation.settlement.body.history.decisions == []
    original = closure_document(body, resolved, evaluation.contract, evaluation.settlement)
    body = _edited(body, ("history", "decisions", 0, "instruction", "text"), "Changed stale owner instruction")
    changed = evaluate_provenance_projection(body, resolved)
    assert changed.settlement.body.history.decisions == []
    assert closure_hash(closure_document(body, resolved, changed.contract, changed.settlement)) != closure_hash(original)


def _binding(body, resolved):
    evaluation = evaluate_provenance_projection(body, resolved)
    return build_provenance_binding(body, resolved, evaluation.contract, evaluation.settlement)


def test_binding_has_seven_snapshot_and_content_fields_without_feedback(proof):
    _cur, body, resolved = proof
    evaluation = evaluate_provenance_projection(body, resolved)
    digest = closure_hash(closure_document(body, resolved, evaluation.contract, evaluation.settlement))
    binding = _binding(body, resolved)
    assert binding.model_dump() == {
        "run_id": body.snapshot.run_id,
        "revision": body.snapshot.revision,
        "contract_hash": contract_hash(evaluation.contract),
        "claim_set_hash": digest,
        "source_hash": digest,
        "rule_bundle_hash": content_hash(helper_sources()),
        "schema_hash": content_hash(referenced_schemas()),
    }
    assert _binding(body, resolved) == binding
    altered = body.model_copy(deep=True)
    altered.binding = nsi.Binding(
        run_id="prior-run", revision=99,
        **{field: "ab" * 32 for field in (
            "contract_hash", "claim_set_hash", "source_hash", "rule_bundle_hash", "schema_hash",
        )},
    )
    assert _binding(altered, resolved) == binding


@pytest.mark.parametrize("module_name,edit", [
    ("omp_work.contracts.nsi.v1.compat", "module"),
    ("omp_work.contracts.nsi.v1.validate", "module"),
    ("omp_work.contracts.nsi.v1.authority", "module"),
    ("omp_work.contracts.nsi.v1.models", "module"),
    ("omp_work.v1.semantics", "module"),
    ("omp_work.v1.models", "module"),
    ("omp_work.v1.api_models", "module"),
    ("omp_work.v1.intake_provenance", "module"),
    ("omp_work.v1.intake_provenance_closure", "module"),
    ("omp_work.v1.intake_decisions", "module"),
    ("omp_work.contracts.nsi.v1.compat", "records"),
    ("omp_work.v1.intake_decisions", "readiness"),
    ("omp_work.v1.intake_provenance", "settlement"),
])
def test_binding_tracks_complete_loaded_helpers_without_paths_or_stale_cache(
    proof, tmp_path, monkeypatch, module_name, edit,
):
    _cur, body, resolved = proof
    module = sys.modules[module_name]
    original_content = Path(module.__file__).read_bytes()
    baseline = _binding(body, resolved)
    copy = tmp_path / "loaded.py"
    copy.write_bytes(original_content)
    monkeypatch.setattr(module, "__file__", str(copy))
    assert _binding(body, resolved) == baseline

    if edit == "records":
        changed_content = original_content.replace(b"def _records(", b"def _records_changed(")
    elif edit == "readiness":
        changed_content = original_content.replace(b'status = "CONFLICTED"', b'status = "NEEDS_SPEC"')
    elif edit == "settlement":
        changed_content = original_content.replace(b"links[cid] = ()", b'links[cid] = ("new-gap",)')
    else:
        changed_content = original_content + b"\n_PRIVATE_RULE_REVISION = 2\n"
    copy.write_bytes(changed_content)
    changed = _binding(body, resolved)
    assert changed.rule_bundle_hash != baseline.rule_bundle_hash
    assert changed == baseline.model_copy(update={"rule_bundle_hash": changed.rule_bundle_hash})
    assert _binding(body, resolved) == changed

    relocated = tmp_path / "relocated.py"
    relocated.write_bytes(changed_content)
    monkeypatch.setattr(module, "__file__", str(relocated))
    assert _binding(body, resolved) == changed
    relocated.write_bytes(original_content)
    assert _binding(body, resolved) == baseline


@pytest.mark.parametrize("schema_name", ["authority", "taxonomy", "claim", "status", "contract"])
def test_binding_tracks_bundled_and_transitively_referenced_schema_payloads(
    proof, monkeypatch, schema_name,
):
    _cur, body, resolved = proof
    baseline = _binding(body, resolved)
    loader = validate.load_schema
    loaded = []
    altered = False

    def load(name):
        loaded.append(name)
        document = loader(name)
        if altered and name == schema_name:
            document["$defs"]["FutureInternalValue"] = {"type": "null"}
        return document

    monkeypatch.setattr(validate, "load_schema", load)
    assert _binding(body, resolved) == baseline
    assert set(loaded) == {"authority", "taxonomy", "claim", "status", "contract"}
    assert len(loaded) == 5  # Local references and cycles load each document once.
    altered = True
    changed = _binding(body, resolved)
    assert changed.schema_hash != baseline.schema_hash
    assert changed == baseline.model_copy(update={"schema_hash": changed.schema_hash})
    assert _binding(body, resolved) == changed
    altered = False
    assert _binding(body, resolved) == baseline
