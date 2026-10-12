"""Bounded analysis retains the persisted contract and refuses inconsistent proof."""
from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, nsi_to_bounded
from omp_work.contracts.nsi.v1.models import TypedBooleanValue, TypedIntegerValue
from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import read_revision
from omp_work.v1.intake_provenance import evaluate_intake_provenance, evaluate_provenance_projection
from omp_work.v1.intake_provenance_closure import resolve_provenance_closure
from omp_work.v1.models import IntakeConstraint, KnownIntakeValue, UnknownIntakeValue
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_provenance_closure import RUN, _draft, _entities, append_body, make_body
from test_nsi2_repository import connection, write_repository  # noqa: F401
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


def test_clean_analysis_does_not_certify_original_false_premise(service, write_repository):
    workspace_id = write_repository[0]
    body = make_body(workspace_id, relay=False)
    body.snapshot.state = "needs_spec"
    body.snapshot.claims = [
        claim.model_copy(update={"truth": "false"}) if claim.id == "goal-ready" else claim
        for claim in body.snapshot.claims
    ]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
        stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        resolved = resolve_provenance_closure(cur, stored)
        result = evaluate_provenance_projection(stored, resolved)
        assert result.findings.issue_count == 0
        assert dict(resolved.truths)["goal-ready"] == "false"
        assert resolved.ready is False
        assert next(claim.truth for claim in stored.snapshot.claims if claim.id == "goal-ready") == "false"


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
