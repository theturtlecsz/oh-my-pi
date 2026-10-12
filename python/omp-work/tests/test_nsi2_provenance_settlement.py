"""Stored settlements use current finding membership without rewriting provenance."""
from copy import deepcopy
import hashlib
import os
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.intake_repository import (
    DecisionRecord, IntakeRevisionBody, append_revision, lock_revision, read_revision, revision_hashes,
)
from omp_work.v1.api_models import IntakeDecisionRecord, IntakeIssueRecord, IntakeRecordView
from omp_work.v1.intake_commands import execute_intake, read_intake_view
from omp_work.v1.intake_decisions import (
    _decision, _latest_findings, _settlement_membership, compute_settlement_assessment, open_issues,
    unresolved_findings,
)
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import connection, locked, revision_body, write_repository  # noqa: F401
from test_nsi2_claims import http_intake  # noqa: F401
from test_nsi2_decisions import INSTRUCTION, decision, envelope, seeded  # noqa: F401
from test_nsi_schemas import ASSESSMENT
from test_workflow_service import OWNER, service  # noqa: F401


@pytest.fixture
def body():
    return IntakeRevisionBody.model_validate(revision_body(uuid4()))


def membership(body, question=None):
    findings, links = _latest_findings(body)
    return _settlement_membership(body, question or findings["question-1"], findings, links)


def test_renewed_findings_replace_history_membership_and_keep_exact_words(body):
    body.history.issues = [
        IntakeIssueRecord(issue=deepcopy(body.conflicts[0]), gaps=()),
        IntakeIssueRecord(issue=deepcopy(body.questions[0]), gaps=("gap-1",)),
    ]
    body.snapshot.claims.append(body.snapshot.claims[0].model_copy(update={"id": "claim-2"}))
    body.conflicts[0].claim_ids = ["claim-1", "claim-2"]
    body.snapshot.claims.append(body.snapshot.claims[0].model_copy(update={"id": "claim-4"}))
    body.gaps[0].premise_id = "claim-4"
    body.questions[0].question = "  Which café?\r\n\t🙂  "
    before = body.model_dump()
    linked, required = membership(body)
    assert linked == {"question-1", "conflict-1", "gap-1"}
    assert required == {"claim-1", "claim-2", "claim-4"}
    question = next(i.issue for i in open_issues(body) if i.issue.id == "question-1")
    assert question.question == body.questions[0].question
    assert body.model_dump() == before


def test_latest_conflict_links_drop_obsolete_gap_and_its_claim(body):
    body.snapshot.claims.append(body.snapshot.claims[0].model_copy(update={"id": "claim-3"}))
    body.gaps.append(body.gaps[0].model_copy(update={"id": "obsolete-gap", "premise_id": "claim-3"}))
    body.questions[0].gap_ids = []
    body.history.issues = [
        IntakeIssueRecord(issue=body.conflicts[0], gaps=("obsolete-gap",)),
        IntakeIssueRecord(issue=body.conflicts[0], gaps=("gap-1",)),
    ]
    assert membership(body) == (
        {"question-1", "conflict-1", "gap-1"}, {"claim-1", "prior"},
    )
    conflict = next(i for i in open_issues(body) if i.issue.id == "conflict-1")
    assert conflict.gaps == ("gap-1",)
    body.history.issues.append(IntakeIssueRecord(issue=body.conflicts[0], gaps=()))
    assert membership(body)[0] == {"question-1", "conflict-1"}


def test_history_only_findings_accept_settlement_and_preserve_instruction(body):
    question, conflict = body.questions.pop(), body.conflicts.pop()
    body.history.issues = [
        IntakeIssueRecord(issue=conflict, gaps=("gap-1",)),
        IntakeIssueRecord(issue=question.model_copy(update={"conflict_ids": []}), gaps=("gap-1",)),
        IntakeIssueRecord(issue=question, gaps=("gap-1",)),
    ]
    assert membership(body) == (
        {"question-1", "conflict-1", "gap-1"}, {"claim-1", "prior"},
    )
    body.decisions = []
    command = decision()
    command["payload"]["decision"]["id"] = "history-settlement"
    request = envelope(body.workspace_id, command)
    actor = nsi.ActorRef.model_validate(command["payload"]["decision"]["actor"])
    history = deepcopy(body.history.issues)
    incomplete = deepcopy(command)
    incomplete["payload"]["claim_ids"] = ["claim-1"]
    before = body.model_dump()
    with pytest.raises(WorkStoreError, match="^invalid_request$"):
        _decision(None, envelope(body.workspace_id, incomplete), actor, uuid4(), body)
    assert body.model_dump() == before
    _decision(None, request, actor, uuid4(), body)
    record = body.history.decisions[0]
    assert record.instruction.model_dump(mode="json") == INSTRUCTION
    assert record.decision.answer == request.command.payload.decision.answer
    assert record.decision.reason == request.command.payload.decision.reason
    assert body.artifact_bytes["instruction:history-settlement"] == INSTRUCTION["text"].encode("utf-8")
    assert body.questions == [question]
    assert body.conflicts == [conflict]
    assert body.history.issues == history
    assert open_issues(body) == ()


@pytest.mark.parametrize("target", [
    "missing-question", "wrong-question", "missing-conflict", "wrong-conflict",
    "missing-gap", "wrong-gap", "missing-linked-gap", "wrong-linked-gap",
    "missing-conflict-claim", "missing-gap-claim",
])
def test_invalid_settlement_targets_raise_invalid_request(body, target):
    question = deepcopy(body.questions[0])
    if target == "missing-question":
        body.questions = []
    elif target == "wrong-question":
        body.questions = []
        body.gaps[0].id = question.id
    elif target.endswith("conflict"):
        body.questions[0].conflict_ids = ["absent" if target.startswith("missing") else "gap-1"]
    elif target in ("missing-gap", "wrong-gap"):
        body.questions[0].gap_ids = ["absent" if target.startswith("missing") else "conflict-1"]
    elif target.endswith("linked-gap"):
        body.history.issues = [IntakeIssueRecord(
            issue=body.conflicts[0], gaps=("absent" if target.startswith("missing") else "question-1",),
        )]
    elif target == "missing-conflict-claim":
        body.conflicts[0].claim_ids = ["absent"]
    else:
        body.gaps[0].premise_id = "absent"
    with pytest.raises(WorkStoreError, match="^invalid_request$"):
        membership(body, question)


def stored_decision(body, claim_ids, *, decision_id="decision-1", superseded=None):
    command = decision(claim_ids=claim_ids, superseded_decision_id=superseded)
    command["payload"]["decision"]["id"] = decision_id
    payload = envelope(body.workspace_id, command).command.payload
    return IntakeDecisionRecord(
        decision=payload.decision, claim_ids=payload.claim_ids, instruction=payload.instruction,
        superseded_decision_id=payload.superseded_decision_id,
    )


def remaining(body):
    return {finding.id for finding in unresolved_findings(body)}


def test_renewal_reopens_all_findings_until_owner_covers_third_claim(body):
    record = stored_decision(body, ["claim-1", "prior"])
    body.history.decisions = [record]
    assert remaining(body) == set()
    body.snapshot.claims.append(body.snapshot.claims[0].model_copy(update={"id": "third"}))
    body.conflicts[0].claim_ids.append("third")
    # A broader revision copy cannot override immutable history coverage.
    body.decisions = [DecisionRecord(decision=record.decision,
                                    settled_claim_ids=["claim-1", "prior", "third"])]
    before = body.model_dump()
    assert remaining(body) == {"gap-1", "conflict-1", "question-1"}
    assert {i.issue.id for i in open_issues(body)} == {"conflict-1", "question-1"}
    assert body.model_dump() == before
    command = decision(claim_ids=["claim-1", "prior", "third"], superseded_decision_id="decision-1")
    command["payload"]["decision"]["id"] = "complete"
    request = envelope(body.workspace_id, command)
    actor = nsi.ActorRef.model_validate(command["payload"]["decision"]["actor"])
    partial = deepcopy(command)
    partial["payload"]["claim_ids"] = ["claim-1", "prior"]
    with pytest.raises(WorkStoreError, match="^invalid_request$"):
        _decision(None, envelope(body.workspace_id, partial), actor, uuid4(), body)
    assert body.model_dump() == before
    _decision(None, request, actor, uuid4(), body)
    assert remaining(body) == set()
    assert body.history.decisions[0] == record
    assert body.history.decisions[1].instruction.model_dump(mode="json") == INSTRUCTION


def test_superseded_full_coverage_cannot_rescue_partial_successor(body):
    body.history.decisions = [
        stored_decision(body, ["claim-1", "prior"]),
        stored_decision(body, ["claim-1"], decision_id="partial", superseded="decision-1"),
    ]
    assert remaining(body) == {"gap-1", "conflict-1", "question-1"}


def test_snapshot_only_settlement_requires_full_coverage(body):
    assert remaining(body) == {"gap-1", "conflict-1", "question-1"}
    body.decisions[0].settled_claim_ids.append("prior")
    assert remaining(body) == set()


def test_unlinked_findings_stay_open_and_obsolete_gap_links_do_not_settle(body):
    body.questions[0].gap_ids = []
    body.gaps.append(body.gaps[0].model_copy(update={"id": "unlinked-gap"}))
    body.conflicts.append(body.conflicts[0].model_copy(update={"id": "unlinked-conflict"}))
    body.questions.append(body.questions[0].model_copy(update={"id": "unlinked-question"}))
    body.history.issues = [
        IntakeIssueRecord(issue=body.conflicts[0], gaps=("gap-1",)),
        IntakeIssueRecord(issue=body.conflicts[0], gaps=()),
    ]
    body.history.decisions = [stored_decision(body, ["claim-1", "prior"])]
    assert remaining(body) == {"gap-1", "unlinked-gap", "unlinked-conflict", "unlinked-question"}
    assert {i.issue.id for i in open_issues(body)} == {"unlinked-conflict", "unlinked-question"}


@pytest.mark.parametrize("target", ["question", "decision-claim", "conflict-claim", "gap-claim"])
def test_dangling_active_references_refuse_reader_and_writer_without_mutation(body, target):
    record = stored_decision(body, ["claim-1", "prior"])
    body.history.decisions = [record]
    if target == "question":
        body.questions = []
    elif target == "decision-claim":
        record = record.model_copy(update={"claim_ids": (*record.claim_ids, "absent")})
        body.history.decisions = [record]
    elif target == "conflict-claim":
        body.conflicts[0].claim_ids.append("absent")
    else:
        body.gaps[0].premise_id = "absent"
    before = body.model_dump()
    for reader in (unresolved_findings, open_issues):
        with pytest.raises(WorkStoreError, match="^invalid_request$"):
            reader(body)
        assert body.model_dump() == before
    command = decision(superseded_decision_id="decision-1", claim_ids=list(record.claim_ids))
    command["payload"]["decision"]["id"] = "replacement"
    request = envelope(body.workspace_id, command)
    actor = nsi.ActorRef.model_validate(command["payload"]["decision"]["actor"])
    with pytest.raises(WorkStoreError, match="^invalid_request$"):
        _decision(None, request, actor, uuid4(), body)
    assert body.model_dump() == before


def test_superseded_dangling_decision_does_not_block_active_settlement(body):
    old = stored_decision(body, ["absent"])
    old = old.model_copy(update={"decision": old.decision.model_copy(update={"question_id": "absent"})})
    body.history.decisions = [old, stored_decision(
        body, ["claim-1", "prior"], decision_id="active", superseded="decision-1",
    )]
    assert remaining(body) == set()


def supplied_assessment(body, status="NEEDS_SPEC"):
    return body.assessments[0].assessment.model_copy(update={
        "status": status, "gaps": [], "conflicts": [],
    })


def preserved(computed, assessment):
    for field in ("run_id", "revision", "contract_hash", "claim_set_hash", "rule_bundle_hash",
                  "schema_hash", "source_hash", "derivation_receipt_ids", "created_at"):
        assert getattr(computed, field) == getattr(assessment, field)
    assert computed.assessed_by == assessment.assessed_by


def assess_without_mutation(body, assessment):
    """Return the computed assessment and prove the inputs were not rewritten."""
    before = body.model_dump()
    history = body.history.model_dump()
    prior = assessment.model_dump()
    computed = compute_settlement_assessment(body, assessment)
    assert body.model_dump() == before
    assert body.history.model_dump() == history
    assert assessment.model_dump() == prior
    preserved(computed, assessment)
    unresolved = unresolved_findings(body)
    assert [item.model_dump() for item in computed.gaps] == [
        item.model_dump() for item in unresolved if isinstance(item, nsi.Gap)
    ]
    assert [item.model_dump() for item in computed.conflicts] == [
        item.model_dump() for item in unresolved if isinstance(item, nsi.Conflict)
    ]
    return computed


def test_renewed_third_claim_assessment_is_conflicted_with_gap_details(body):
    record = stored_decision(body, ["claim-1", "prior"])
    body.history.decisions = [record]
    body.snapshot.claims.append(body.snapshot.claims[0].model_copy(update={"id": "third"}))
    body.conflicts[0].claim_ids.append("third")
    computed = assess_without_mutation(body, supplied_assessment(body, "READY_FOR_RATIFICATION"))
    assert computed.status == "CONFLICTED"
    assert {item.id for item in computed.conflicts} == {"conflict-1"}
    assert {item.id for item in computed.gaps} == {"gap-1"}
    assert "third" in computed.conflicts[0].claim_ids
    assert computed.gaps[0].premise_id == body.gaps[0].premise_id
    assert computed.gaps[0].reason == body.gaps[0].reason
    assert {issue.issue.id for issue in open_issues(body)} == {"conflict-1", "question-1"}
    computed.conflicts[0].claim_ids.append("mutated")
    assert "mutated" not in body.conflicts[0].claim_ids


def test_unlinked_question_assessment_needs_spec_and_keeps_gap(body):
    body.history.decisions = [stored_decision(body, ["claim-1", "prior"])]
    body.gaps.append(body.gaps[0].model_copy(update={"id": "unlinked-gap", "reason": "Still unspecified."}))
    body.questions.append(body.questions[0].model_copy(update={
        "id": "unlinked-question", "question": "Which unbound detail?",
        "gap_ids": ["unlinked-gap"], "conflict_ids": [],
    }))
    computed = assess_without_mutation(body, supplied_assessment(body))
    assert computed.status == "NEEDS_SPEC"
    assert [item.id for item in computed.gaps] == ["unlinked-gap"]
    assert computed.conflicts == []
    assert computed.gaps[0].reason == "Still unspecified."
    question = next(issue for issue in open_issues(body) if issue.issue.id == "unlinked-question")
    assert question.issue.question == "Which unbound detail?"
    assert question.gaps == ("unlinked-gap",)


def test_complete_coverage_removes_findings_without_inventing_ready(body):
    body.decisions[0].settled_claim_ids.append("prior")
    assessment = supplied_assessment(body, "NEEDS_SPEC").model_copy(update={
        "gaps": [body.gaps[0].model_copy(deep=True)],
        "conflicts": [body.conflicts[0].model_copy(deep=True)],
    })
    computed = assess_without_mutation(body, assessment)
    assert unresolved_findings(body) == ()
    assert computed.gaps == []
    assert computed.conflicts == []
    assert computed.status == "NEEDS_SPEC"


def test_superseded_coverage_reopens_assessment_findings(body):
    body.history.decisions = [
        stored_decision(body, ["claim-1", "prior"]),
        stored_decision(body, ["claim-1"], decision_id="partial", superseded="decision-1"),
    ]
    computed = assess_without_mutation(body, supplied_assessment(body, "READY_FOR_RATIFICATION"))
    assert computed.status == "CONFLICTED"
    assert {item.id for item in computed.gaps} == {"gap-1"}
    assert {item.id for item in computed.conflicts} == {"conflict-1"}
    assert {issue.issue.id for issue in open_issues(body)} == {"conflict-1", "question-1"}


def _with_hashes(raw):
    parsed = IntakeRevisionBody.model_validate(raw)
    claim_hash, source_hash = revision_hashes(parsed.snapshot, parsed.artifact_bytes)
    raw["binding"].update(claim_set_hash=claim_hash, source_hash=source_hash)
    return raw


def append_assessed_revision(cur, workspace_id, mission_id, body):
    """Copy a revision while retaining original wire decisions in history."""
    revision = body.snapshot.revision + 1
    raw = body.model_dump()
    raw["snapshot"]["revision"] = raw["binding"]["revision"] = revision
    for record in raw["decisions"]:
        record["decision"]["revision"] = revision
    raw = _with_hashes(raw)
    raw["derivations"] = revision_body(workspace_id, revision)["derivations"]
    for receipt in raw["derivations"]:
        receipt.update(raw["binding"])
        receipt["created_at"] = receipt["created_at"].replace("Z", "+00:00")
    raw["assessments"] = [dict(id=f"assessment-{revision}", assessment={
        **ASSESSMENT, **raw["binding"], "gaps": [], "conflicts": [],
        "created_at": ASSESSMENT["created_at"].replace("Z", "+00:00"),
        "derivation_receipt_ids": [receipt["id"] for receipt in raw["derivations"]],
    })]
    live = IntakeRevisionBody.model_validate(raw)
    operation_id = uuid4()
    head = lock_revision(cur, workspace_id=workspace_id, run_id="run-1",
                         expected_revision=revision - 1, mission_id=mission_id,
                         operation_id=operation_id)
    assert append_revision(cur, head, live, operation_id=operation_id) == revision
    return live


def assert_reconnected_settlement(service, live, expected_ids):
    """Live resolution, reopened storage and serialized API expose one settlement."""
    assert remaining(live) == expected_ids
    computed = compute_settlement_assessment(live, live.assessments[0].assessment)
    with connection(service, live.workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        reopened = read_revision(cur, workspace_id=live.workspace_id, run_id="run-1",
                                 revision=live.snapshot.revision).body
        view = read_intake_view(cur, live.workspace_id, "run-1")
        serialized = IntakeRecordView.model_validate_json(view.model_dump_json())
        assert serialized == IntakeRecordView.model_validate(view.model_dump(mode="json")) == view
        assert remaining(reopened) == expected_ids
        assert {item.id: item.model_dump() for item in unresolved_findings(reopened)} == {
            item.id: item.model_dump() for item in unresolved_findings(live)
        }
        assert serialized.issues == open_issues(reopened) == open_issues(live)
        assessment = serialized.assessments[0]
        assert assessment.assessment == compute_settlement_assessment(
            reopened, reopened.assessments[0].assessment,
        ) == computed
        assert assessment.unresolved_issues == serialized.issues
        assert assessment.binding == reopened.binding == live.binding
        assert assessment.receipts == tuple(reopened.derivations) == tuple(live.derivations)
        assert assessment.assessment.revision == live.snapshot.revision
        assert assessment.assessment.derivation_receipt_ids == [item.id for item in assessment.receipts]
        assert reopened.history == live.history
        assert serialized.decisions == tuple(live.history.decisions)
    return serialized


postgres_settlement = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@postgres_settlement
def test_renewed_settlement_reconnects_open_until_full_successor(service, seeded):
    ws, mission, _ = seeded
    with connection(service, ws) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(ws, decision()), OWNER, "owner")
        original = read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body
        historical = read_intake_view(cur, ws, "run-1@2")
        assert historical.issues == ()
        renewed = original.model_copy(deep=True)
        renewed.snapshot.claims.append(renewed.snapshot.claims[0].model_copy(update={"id": "third"}))
        renewed.conflicts[0].claim_ids.append("third")
        renewed.history.issues = [
            IntakeIssueRecord(issue=original.conflicts[0], gaps=("gap-1",)),
            IntakeIssueRecord(issue=original.questions[0], gaps=("gap-1",)),
        ]
        # A broader copied row must not replace the original owner coverage.
        renewed.decisions[0].settled_claim_ids.append("third")
        renewed = append_assessed_revision(cur, ws, mission, renewed)
    reopened = assert_reconnected_settlement(service, renewed, {"gap-1", "conflict-1", "question-1"})
    assessment = reopened.assessments[0].assessment
    assert assessment.status == "CONFLICTED"
    assert assessment.conflicts == renewed.conflicts
    assert assessment.gaps == renewed.gaps
    assert next(i for i in reopened.issues if i.issue.id == "conflict-1").gaps == ("gap-1",)
    assert reopened.decisions[0].decision.revision == 1
    assert reopened.decisions[0].claim_ids == ("claim-1", "prior")

    command = decision(3, claim_ids=["claim-1", "prior", "third"], superseded_decision_id="decision-1")
    command["payload"]["decision"]["id"] = "complete"
    with connection(service, ws) as conn, conn.cursor() as cur:
        _, result = execute_intake(cur, envelope(ws, command), OWNER, "owner")
        assert result["revision"] == 4
        complete = read_revision(cur, workspace_id=ws, run_id="run-1", revision=3).body
        assert remaining(complete) == set()
        complete = append_assessed_revision(cur, ws, mission, complete)
    settled = assert_reconnected_settlement(service, complete, set())
    assert settled.assessments[0].assessment.gaps == settled.assessments[0].assessment.conflicts == []
    assert complete.history.issues == renewed.history.issues
    assert complete.history.decisions[0] == original.history.decisions[0]
    assert complete.questions == renewed.questions
    assert complete.conflicts == renewed.conflicts
    assert complete.gaps == renewed.gaps
    successor = settled.decisions[1]
    assert successor.decision.revision == 3
    assert successor.superseded_decision_id == "decision-1"
    assert successor.instruction.model_dump(mode="json") == INSTRUCTION
    assert successor.decision.answer.model_dump(mode="json") == command["payload"]["decision"]["answer"]
    assert successor.decision.reason == command["payload"]["decision"]["reason"]
    assert complete.artifact_bytes["instruction:complete"] == INSTRUCTION["text"].encode("utf-8")
    with connection(service, ws, "omp_work_readonly") as conn, conn.cursor() as cur:
        assert read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body == original
        assert read_intake_view(cur, ws, "run-1@2") == historical
        assert read_intake_view(cur, ws, "run-1@3") == reopened
        assert read_intake_view(cur, ws, "run-1@4").issues == ()


@postgres_settlement
def test_persisted_superseded_full_coverage_cannot_settle_partial_successor(service, seeded):
    ws, mission, _ = seeded
    with connection(service, ws) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(ws, decision()), OWNER, "owner")
        original = read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body
        partial = original.model_copy(deep=True)
        record = stored_decision(partial, ["claim-1"], decision_id="partial", superseded="decision-1")
        record = record.model_copy(update={"decision": record.decision.model_copy(update={"revision": 2})})
        partial.history.decisions.append(record)
        partial.decisions.append(DecisionRecord(decision=record.decision, settled_claim_ids=["claim-1"]))
        partial.history.issues = [IntakeIssueRecord(issue=partial.conflicts[0], gaps=("gap-1",))]
        partial = append_assessed_revision(cur, ws, mission, partial)
    reopened = assert_reconnected_settlement(service, partial, {"gap-1", "conflict-1", "question-1"})
    assert reopened.assessments[0].assessment.status == "CONFLICTED"
    assert reopened.assessments[0].assessment.conflicts == partial.conflicts
    assert reopened.assessments[0].assessment.gaps == partial.gaps
    assert reopened.decisions[0].claim_ids == ("claim-1", "prior")
    assert reopened.decisions[1].claim_ids == ("claim-1",)
    assert reopened.decisions[1].superseded_decision_id == "decision-1"
    with connection(service, ws, "omp_work_readonly") as conn, conn.cursor() as cur:
        assert read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body == original
        assert read_intake_view(cur, ws, "run-1@2").issues == ()


@postgres_settlement
def test_history_only_question_owner_write_settles_after_reconnect(service, seeded):
    ws, mission, _ = seeded
    with connection(service, ws) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=ws, run_id="run-1", revision=0).body
        question, conflict = body.questions.pop(), body.conflicts.pop()
        question.question = "  Which café?\r\n\t🙂  "
        body.history.issues = [IntakeIssueRecord(issue=conflict, gaps=("gap-1",)),
                               IntakeIssueRecord(issue=question, gaps=("gap-1",))]
        live = append_assessed_revision(cur, ws, mission, body)
    prior = assert_reconnected_settlement(service, live, {"gap-1", "conflict-1", "question-1"})
    command = decision(2)
    with connection(service, ws) as conn, conn.cursor() as cur:
        original = read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body
        for actor, kind in ((OWNER, "agent"), (uuid4(), "owner")):
            with pytest.raises(WorkStoreError, match="^forbidden$"), conn.transaction():
                execute_intake(cur, envelope(ws, command), actor, kind)
        for changes, error in (({"verified_relay_operation_id": str(uuid4())}, "forbidden"),
                               ({"claim_ids": ["claim-1"]}, "invalid_request")):
            invalid = deepcopy(command)
            invalid["payload"].update(changes)
            with pytest.raises(WorkStoreError, match=f"^{error}$"), conn.transaction():
                execute_intake(cur, envelope(ws, invalid), OWNER, "owner")
        assert read_intake_view(cur, ws, "run-1") == prior
        _, result = execute_intake(cur, envelope(ws, command), OWNER, "owner")
        assert result["revision"] == 3
        settled = read_revision(cur, workspace_id=ws, run_id="run-1", revision=2).body
        assert settled.questions == [question]
        assert settled.conflicts == [conflict]
        settled = append_assessed_revision(cur, ws, mission, settled)
    reopened = assert_reconnected_settlement(service, settled, set())
    assert settled.history.issues == live.history.issues
    assert reopened.decisions[0].instruction.model_dump(mode="json") == INSTRUCTION
    assert reopened.decisions[0].decision.answer.model_dump(mode="json") == command["payload"]["decision"]["answer"]
    assert reopened.decisions[0].decision.reason == command["payload"]["decision"]["reason"]
    assert settled.artifact_bytes["instruction:decision-1"] == INSTRUCTION["text"].encode("utf-8")
    assert settled.questions[0].question == "  Which café?\r\n\t🙂  "
    with connection(service, ws, "omp_work_readonly") as conn, conn.cursor() as cur:
        assert read_revision(cur, workspace_id=ws, run_id="run-1", revision=1).body == original
        assert read_intake_view(cur, ws, "run-1@2") == prior
        assert read_intake_view(cur, ws, "run-1@3").issues == ()


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                     reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_read_view_serializes_assessments_for_their_own_revisions(service, write_repository):
    workspace_id, _ = write_repository
    opening = _with_hashes(revision_body(workspace_id))
    opening["derivations"][0]["id"] = "receipt-old"
    opening["assessments"] = [dict(
        id="assessment-0",
        assessment={**ASSESSMENT, "revision": 0, "status": "READY_FOR_RATIFICATION", "gaps": [], "conflicts": []},
    )]
    later = _with_hashes(revision_body(workspace_id, 1))
    later["derivations"][0]["id"] = "receipt-new"
    artifact_id = next(iter(later["artifact_bytes"]))
    later["artifact_bytes"][artifact_id] += b"\x01"
    later["snapshot"]["artifacts"][0]["uri"] += "/1"
    later["questions"][0]["question"] = "changed on revision 1"
    later = _with_hashes(later)
    later["history"] = {"decisions": [stored_decision(
        IntakeRevisionBody.model_validate(opening), ["claim-1", "prior"],
    ).model_dump(mode="json")]}
    later["assessments"] = [
        dict(id="carried-forward", assessment={
            **ASSESSMENT, "revision": 0, "status": "READY_FOR_RATIFICATION", "gaps": [], "conflicts": [],
        }),
        dict(id="assessment-1", assessment={
            **ASSESSMENT, "revision": 1, "status": "NEEDS_SPEC", "gaps": [], "conflicts": [],
        }),
    ]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), opening, operation_id=uuid4())
        append_revision(cur, locked(cur, write_repository, 0), later, operation_id=uuid4())
        stored0 = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        stored1 = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=1)
        raw = read_intake_view(cur, workspace_id, "run-1")
        parsed = IntakeRecordView.model_validate(raw.model_dump(mode="json"))
        assert parsed == IntakeRecordView.model_validate_json(raw.model_dump_json())
        assert read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0).body.model_dump() == stored0.body.model_dump()
        assert read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=1).body.model_dump() == stored1.body.model_dump()
        pinned = IntakeRecordView.model_validate(
            read_intake_view(cur, workspace_id, "run-1@1").model_dump(mode="json"),
        )

    by_id = {item.id: item for item in parsed.assessments}
    older, current = by_id["carried-forward"], by_id["assessment-1"]
    carried = next(item for item in stored1.body.assessments if item.id == "carried-forward")
    current_record = next(item for item in stored1.body.assessments if item.id == "assessment-1")
    assert older.assessment == compute_settlement_assessment(stored0.body, carried.assessment.model_copy(update={
        "derivation_receipt_ids": [receipt.id for receipt in stored0.body.derivations],
    }))
    assert current.assessment == compute_settlement_assessment(stored1.body, current_record.assessment)
    assert older.assessment.status == "CONFLICTED"
    assert {item.id for item in older.assessment.gaps} == {"gap-1"}
    assert {item.id for item in older.assessment.conflicts} == {"conflict-1"}
    assert older.assessment.gaps[0].reason == stored0.body.gaps[0].reason
    assert set(older.assessment.conflicts[0].claim_ids) == set(stored0.body.conflicts[0].claim_ids)
    question = next(issue for issue in older.unresolved_issues if issue.issue.id == "question-1")
    assert question.issue.question == stored0.body.questions[0].question
    assert question.gaps == ("gap-1",)
    unresolved_ids = {finding.id for finding in unresolved_findings(stored0.body)}
    assert {issue.issue.id for issue in older.unresolved_issues} == {
        finding_id for finding_id in unresolved_ids if finding_id != "gap-1"
    }
    assert {item.id for item in (*older.assessment.gaps, *older.assessment.conflicts)} | {
        issue.issue.id for issue in older.unresolved_issues
    } == unresolved_ids
    assert older.binding == stored0.body.binding
    assert older.assessment.revision == older.binding.revision == 0
    assert older.receipts == tuple(stored0.body.derivations)
    assert older.assessment.derivation_receipt_ids == [receipt.id for receipt in older.receipts] == ["receipt-old"]
    assert older.receipts[0].revision == 0
    assert older.provenance.revision == 1
    assert older.provenance.source_sha256 == hashlib.sha256(next(iter(stored0.artifact_bytes.values()))).hexdigest()
    assert current.assessment.status == "NEEDS_SPEC"
    assert current.assessment.gaps == current.assessment.conflicts == []
    assert current.unresolved_issues == ()
    assert current.binding == stored1.body.binding
    assert current.assessment.revision == current.binding.revision == 1
    assert current.receipts == tuple(stored1.body.derivations)
    assert current.assessment.derivation_receipt_ids == [receipt.id for receipt in current.receipts] == ["receipt-new"]
    assert current.receipts[0].revision == 1
    assert current.receipts != older.receipts
    assert current.provenance.revision == parsed.revision == 2
    assert current.provenance.source_sha256 == hashlib.sha256(next(iter(stored1.artifact_bytes.values()))).hexdigest()
    assert current.provenance.source_sha256 != older.provenance.source_sha256
    assert parsed.issues == ()
    pinned_only = pinned.assessments
    assert [item.id for item in pinned_only] == ["assessment-0"]
    assert pinned_only[0].binding.revision == 0
    assert pinned_only[0].assessment.status == "CONFLICTED"
    assert pinned_only[0].receipts[0].revision == 0
    assert pinned_only[0].unresolved_issues == open_issues(stored0.body)
