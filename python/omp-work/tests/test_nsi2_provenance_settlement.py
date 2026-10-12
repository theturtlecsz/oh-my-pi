"""Stored settlements use current finding membership without rewriting provenance."""
from copy import deepcopy
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.intake_repository import DecisionRecord, IntakeRevisionBody
from omp_work.v1.api_models import IntakeDecisionRecord, IntakeIssueRecord
from omp_work.v1.intake_decisions import (
    _decision, _latest_findings, _settlement_membership, open_issues, unresolved_findings,
)
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import revision_body
from test_nsi2_decisions import INSTRUCTION, decision, envelope


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
