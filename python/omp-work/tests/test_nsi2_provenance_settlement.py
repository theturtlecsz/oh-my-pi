"""Stored settlements use current finding membership without rewriting provenance."""
from copy import deepcopy
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.intake_repository import IntakeRevisionBody
from omp_work.v1.api_models import IntakeIssueRecord
from omp_work.v1.intake_decisions import (
    _decision, _latest_findings, _settlement_membership, open_issues,
)
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import revision_body
from test_nsi_schemas import DECISION


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
    text = "  Keep café ≥ 99.9%.\r\nDo NOT trim.\t🙂  "
    envelope = CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=body.workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
        command=dict(type="record_intake_decision", payload=dict(
            run_id="run-1", expected_revision=1,
            decision={**DECISION, "id": "history-settlement", "revision": 1}, claim_ids=["claim-1", "prior"],
            instruction=dict(text=text, provenance=dict(channel="owner-chat", message_ref="msg:exact",
                                                       received_at="2026-10-11T00:00:00Z")),
        )),
    ))
    history = deepcopy(body.history.issues)
    incomplete = envelope.model_dump(mode="json")
    incomplete["command"]["payload"]["claim_ids"] = ["claim-1"]
    incomplete = CommandEnvelope.model_validate(incomplete)
    with pytest.raises(WorkStoreError, match="^invalid_request$"):
        _decision(None, incomplete, nsi.ActorRef(id="owner-1", kind="owner"), uuid4(), body)
    assert body.history.decisions == []
    _decision(None, envelope, nsi.ActorRef(id="owner-1", kind="owner"), uuid4(), body)
    assert body.history.decisions[0].instruction.text == text
    assert body.artifact_bytes["instruction:history-settlement"] == text.encode("utf-8")
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
