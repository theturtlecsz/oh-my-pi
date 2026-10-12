"""Bounded analysis retains the persisted contract and refuses inconsistent proof."""
from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, nsi_to_bounded
from omp_work.contracts.nsi.v1.models import TypedBooleanValue, TypedIntegerValue
from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import DecisionRecord, read_revision
from omp_work.v1.api_models import IntakeDecisionRecord, IntakeIssueRecord
from omp_work.v1.intake_provenance import (
    evaluate_intake_provenance,
    evaluate_provenance_projection,
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
from test_nsi2_provenance_closure import RUN, _criterion, _draft, _entities, _retarget, advance, append_body, make_body
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

