"""Admission binding: source ids, receipt ids, and the claim span, issuer, and PASS."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import get_args
from uuid import UUID, uuid4

import pytest
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from support.bound_evidence import bound_claim, bound_receipt

from omp_knowledge.learning import policy as policy_module
from omp_knowledge.learning.models import Attribution, Claim, Lesson, SourceIdentity, SourceSpan
from omp_knowledge.learning.policy import (
    REASON_INVALID_CITATION,
    REASON_NARRATION_ONLY,
    REASON_NO_CITATION,
    REASON_TOOL_SUCCESS_ONLY,
    REASON_UNBOUND_CLAIM,
    REASON_UNBOUND_RECEIPT,
    REASON_UNBOUND_SOURCE,
    PolicyDecision,
    evaluate,
)
from omp_knowledge.learning.proposals import create_proposal
from omp_knowledge.learning.store import LearningStore

TEXT = "proved"


class _Missing:
    pass


_MISSING = _Missing()


class Reader:
    def __init__(self, *receipts: EvidenceReceipt) -> None:
        self._receipts = {str(receipt.receipt_id): receipt for receipt in receipts}

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(key)
        return self._receipts[key]


class RaisingReader:
    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        raise RuntimeError(f"ledger down for {receipt_id}")


def _chosen(override: str | None | _Missing, default: str | None) -> str | None:
    if isinstance(override, _Missing):
        return default
    return override


def source_for(
    receipt: EvidenceReceipt,
    *,
    work_id: str | None | _Missing = _MISSING,
    candidate_id: str | None | _Missing = _MISSING,
    revision_id: str | None | _Missing = _MISSING,
) -> SourceIdentity:
    default_candidate = None if receipt.candidate_id is None else str(receipt.candidate_id)
    return SourceIdentity(
        workspace_id=str(uuid4()),
        event_id=str(uuid4()),
        event_sequence=1,
        aggregate_id=str(uuid4()),
        work_id=_chosen(work_id, str(receipt.work_id)),
        candidate_id=_chosen(candidate_id, default_candidate),
        revision_id=_chosen(revision_id, str(receipt.revision_id)),
    )


def make_lesson(*claims: Claim) -> Lesson:
    return Lesson(title="Bind the claim", steps=("cite the receipt",), claims=claims)


def bound_lesson(receipt: EvidenceReceipt) -> Lesson:
    return make_lesson(bound_claim(TEXT, receipt))


def attribution(source: SourceIdentity) -> Attribution:
    return Attribution(model="local-model", profile="local", source=source)


def assert_proposal_only(store: LearningStore, reason: str) -> None:
    rows = store.execute("SELECT status, reason, procedure_id FROM proposals").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["reason"] == reason
    assert rows[0]["procedure_id"] is None
    assert store.execute("SELECT COUNT(*) AS c FROM procedures").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM procedure_versions").fetchone()["c"] == 0
    assert store.execute("SELECT COUNT(*) AS c FROM procedure_support").fetchone()["c"] == 0


def test_binding_reasons_are_exported() -> None:
    assert get_args(policy_module.PolicyRejectionReason) == (
        "no_citation",
        "invalid_citation",
        "narration_only",
        "tool_success_only",
        "unbound_source",
        "unbound_receipt",
        "unbound_claim",
    )
    assert policy_module.REASON_UNBOUND_SOURCE == "unbound_source"
    assert policy_module.REASON_UNBOUND_RECEIPT == "unbound_receipt"
    assert policy_module.REASON_UNBOUND_CLAIM == "unbound_claim"
    for name in ("REASON_UNBOUND_SOURCE", "REASON_UNBOUND_RECEIPT", "REASON_UNBOUND_CLAIM"):
        assert name in policy_module.__all__


@pytest.mark.parametrize("kind", [EvidenceKind.VERIFICATION, EvidenceKind.AUDIT])
def test_bound_pass_is_accepted(kind: EvidenceKind) -> None:
    """str(source id) matches the receipt UUID, and the span slices the canonical payload."""
    receipt = bound_receipt(TEXT, kind=kind)
    source = source_for(receipt)
    decision = evaluate(bound_lesson(receipt), Reader(receipt), source)

    assert isinstance(receipt.work_id, UUID)
    assert isinstance(source.work_id, str)
    assert decision.accepted is True
    assert decision.reason is None
    assert decision.per_claim_results[0].supported is True
    assert decision.per_claim_results[0].reason is None


def test_evaluate_never_raises() -> None:
    receipt = bound_receipt(TEXT)
    lesson = bound_lesson(receipt)
    source = source_for(receipt)
    decisions = [
        evaluate(lesson, Reader(receipt), source),
        evaluate(lesson, Reader(receipt), None),
        evaluate(lesson, RaisingReader(), source),
        evaluate(Lesson(title="Bare", steps=("step",)), Reader(), None),
    ]
    assert all(isinstance(decision, PolicyDecision) for decision in decisions)
    assert decisions[1].reason == REASON_UNBOUND_SOURCE
    assert decisions[2].reason == REASON_INVALID_CITATION
    assert decisions[3].reason == REASON_NO_CITATION


@pytest.mark.parametrize("failure", ["mixed_keys", "circular_reference"])
def test_uncanonicalizable_payload_rejects_claim_and_stores_only_proposal(failure: str) -> None:
    receipt = bound_receipt(TEXT)
    lesson = bound_lesson(receipt)
    nested: dict[str | int, object] = {}
    if failure == "mixed_keys":
        nested.update({1: "one", "two": 2})
    else:
        nested["self"] = nested
    # Receipt fields are frozen, but payload contents can change after validation.
    receipt.payload["nested"] = nested
    source = source_for(receipt)
    reader = Reader(receipt)

    decision = evaluate(lesson, reader, source)

    assert decision.accepted is False
    assert decision.reason == REASON_UNBOUND_CLAIM
    assert decision.per_claim_results[0].supported is False
    assert decision.per_claim_results[0].reason == REASON_UNBOUND_CLAIM

    store = LearningStore(":memory:")
    proposal = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attribution(source),
        reader=reader,
    )
    assert proposal.status == "rejected"
    assert_proposal_only(store, REASON_UNBOUND_CLAIM)


def test_earlier_reasons_win_over_binding() -> None:
    """Binding runs only after citation, narration, and a PASS check."""
    receipt = bound_receipt(TEXT)
    assert evaluate(Lesson(title="t", steps=("s",)), Reader(receipt), None).reason == REASON_NO_CITATION

    uncited = make_lesson(Claim(text=TEXT, receipt_ids=()))
    assert evaluate(uncited, Reader(receipt), None).reason == REASON_NO_CITATION

    unknown = make_lesson(Claim(text=TEXT, receipt_ids=(uuid4(),)))
    assert evaluate(unknown, Reader(receipt), None).reason == REASON_INVALID_CITATION

    handoff = bound_receipt("note", kind=EvidenceKind.HANDOFF)
    narrated = make_lesson(bound_claim("note", handoff))
    assert evaluate(narrated, Reader(handoff), None).reason == REASON_NARRATION_ONLY


@pytest.mark.parametrize("verdict", ["NEEDS_FIX", "BLOCKED", None])
@pytest.mark.parametrize("with_source", [False, True])
def test_non_pass_stays_tool_success_only(verdict: str | None, with_source: bool) -> None:
    """A filled span and a matching source do not reclassify a non-PASS receipt."""
    receipt = bound_receipt(TEXT).model_copy(update={"verdict": verdict})
    source = source_for(receipt) if with_source else None
    decision = evaluate(bound_lesson(receipt), Reader(receipt), source)

    assert decision.accepted is False
    assert decision.reason == REASON_TOOL_SUCCESS_ONLY
    assert decision.per_claim_results[0].reason == REASON_TOOL_SUCCESS_ONLY


def test_missing_source_is_unbound_source() -> None:
    receipt = bound_receipt(TEXT)
    decision = evaluate(bound_lesson(receipt), Reader(receipt), None)

    assert decision.accepted is False
    assert decision.reason == REASON_UNBOUND_SOURCE
    assert decision.per_claim_results[0].supported is False
    assert decision.per_claim_results[0].reason == REASON_UNBOUND_SOURCE


@pytest.mark.parametrize("field", ["work_id", "candidate_id", "revision_id"])
@pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
def test_blank_source_id_is_unbound_source(field: str, blank: str | None) -> None:
    receipt = bound_receipt(TEXT)
    source = source_for(receipt, **{field: blank})
    decision = evaluate(bound_lesson(receipt), Reader(receipt), source)

    assert decision.reason == REASON_UNBOUND_SOURCE
    assert decision.per_claim_results[0].reason == REASON_UNBOUND_SOURCE


@pytest.mark.parametrize("field", ["work_id", "candidate_id", "revision_id"])
def test_differing_receipt_id_is_unbound_receipt(field: str) -> None:
    receipt = bound_receipt(TEXT)
    changed = receipt.model_copy(update={field: uuid4()})
    decision = evaluate(bound_lesson(receipt), Reader(changed), source_for(receipt))

    assert decision.accepted is False
    assert decision.reason == REASON_UNBOUND_RECEIPT
    assert decision.per_claim_results[0].reason == REASON_UNBOUND_RECEIPT


def test_padded_source_id_differs_without_stripping() -> None:
    receipt = bound_receipt(TEXT)
    source = source_for(receipt, work_id=f" {receipt.work_id} ")
    decision = evaluate(bound_lesson(receipt), Reader(receipt), source)

    assert decision.reason == REASON_UNBOUND_RECEIPT


def test_none_receipt_candidate_differs_from_source() -> None:
    """A cited receipt with candidate_id None is unbound even beside a matching PASS."""
    receipt = bound_receipt(TEXT)
    delivery = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=receipt.work_id,
        revision_id=receipt.revision_id,
        candidate_id=None,
        kind=EvidenceKind.EXTERNAL_DELIVERY,
        payload={"note": "shipped"},
        payload_sha256="0" * 64,
        issuer="shipper",
        issued_at=datetime.now(timezone.utc),
        verdict=None,
    )
    claim = bound_claim(TEXT, receipt).model_copy(
        update={"receipt_ids": (receipt.receipt_id, delivery.receipt_id)}
    )
    decision = evaluate(make_lesson(claim), Reader(receipt, delivery), source_for(receipt))

    assert decision.reason == REASON_UNBOUND_RECEIPT


def test_co_cited_non_pass_must_share_ids_but_not_the_span() -> None:
    receipt = bound_receipt(TEXT)
    needs_fix = receipt.model_copy(
        update={
            "receipt_id": uuid4(),
            "verdict": "NEEDS_FIX",
            "payload": {"exit_code": 1},
        }
    )
    claim = bound_claim(TEXT, receipt).model_copy(
        update={"receipt_ids": (receipt.receipt_id, needs_fix.receipt_id)}
    )
    decision = evaluate(make_lesson(claim), Reader(receipt, needs_fix), source_for(receipt))

    assert decision.accepted is True
    assert decision.reason is None


def test_co_cited_non_pass_with_a_different_work_id_is_unbound_receipt() -> None:
    receipt = bound_receipt(TEXT)
    needs_fix = receipt.model_copy(
        update={"receipt_id": uuid4(), "work_id": uuid4(), "verdict": "NEEDS_FIX"}
    )
    claim = bound_claim(TEXT, receipt).model_copy(
        update={"receipt_ids": (receipt.receipt_id, needs_fix.receipt_id)}
    )
    decision = evaluate(make_lesson(claim), Reader(receipt, needs_fix), source_for(receipt))

    assert decision.reason == REASON_UNBOUND_RECEIPT


@pytest.mark.parametrize(
    "update",
    [
        {"source_span": None},
        {"source_span": SourceSpan(start=0, end=0)},
        {"text": "", "source_span": SourceSpan(start=0, end=0)},
        {"text": "", "source_span": SourceSpan(start=4, end=1)},
        {"source_span": SourceSpan(start=4, end=1)},
        {"source_span": SourceSpan(start=10_000, end=10_001)},
        {"text": "not the payload slice"},
        {"evaluator": "someone-else"},
        {"evaluator": None},
        {"outcome": None},
        {"outcome": "NEEDS_FIX"},
    ],
)
def test_unbound_claim_when_span_issuer_or_outcome_does_not_match(update: dict[str, object]) -> None:
    receipt = bound_receipt(TEXT)
    claim = bound_claim(TEXT, receipt).model_copy(update=update)
    decision = evaluate(make_lesson(claim), Reader(receipt), source_for(receipt))

    assert decision.accepted is False
    assert decision.reason == REASON_UNBOUND_CLAIM
    assert decision.per_claim_results[0].reason == REASON_UNBOUND_CLAIM


def test_out_of_range_span_is_unbound_even_when_the_slice_equals_the_text() -> None:
    receipt = bound_receipt(TEXT)
    full = canonical_json(receipt.payload)
    claim = bound_claim(TEXT, receipt).model_copy(
        update={"text": full, "source_span": SourceSpan(start=0, end=len(full) + 5)}
    )
    decision = evaluate(make_lesson(claim), Reader(receipt), source_for(receipt))

    assert full == canonical_json(receipt.payload)[0 : len(full) + 5]
    assert decision.reason == REASON_UNBOUND_CLAIM


def test_each_pass_receipt_must_contain_the_claim_at_the_span() -> None:
    first = bound_receipt(TEXT)
    second = bound_receipt(TEXT).model_copy(
        update={
            "work_id": first.work_id,
            "candidate_id": first.candidate_id,
            "revision_id": first.revision_id,
        }
    )
    claim = bound_claim(TEXT, first).model_copy(
        update={"receipt_ids": (first.receipt_id, second.receipt_id)}
    )
    accepted = evaluate(make_lesson(claim), Reader(first, second), source_for(first))
    assert accepted.accepted is True

    shifted = second.model_copy(update={"payload": {"exit_code": 0, "summary": "other"}})
    rejected = evaluate(make_lesson(claim), Reader(first, shifted), source_for(first))
    assert rejected.reason == REASON_UNBOUND_CLAIM


def test_unbound_receipt_wins_over_unbound_claim() -> None:
    matching = bound_receipt(TEXT)
    foreign = bound_receipt("other proof")
    bad_span = bound_claim(TEXT, matching).model_copy(update={"source_span": None})
    foreign_claim = bound_claim("other proof", foreign)
    decision = evaluate(
        make_lesson(bad_span, foreign_claim),
        Reader(matching, foreign),
        source_for(matching),
    )

    assert decision.reason == REASON_UNBOUND_RECEIPT
    assert [(item.supported, item.reason) for item in decision.per_claim_results] == [
        (False, REASON_UNBOUND_CLAIM),
        (False, REASON_UNBOUND_RECEIPT),
    ]


def test_one_unbound_claim_rejects_and_leaves_a_bound_claim_supported() -> None:
    first = bound_receipt(TEXT)
    second = bound_receipt("other proof").model_copy(
        update={
            "work_id": first.work_id,
            "candidate_id": first.candidate_id,
            "revision_id": first.revision_id,
        }
    )
    bad = bound_claim(TEXT, first).model_copy(update={"evaluator": None})
    good = bound_claim("other proof", second)
    decision = evaluate(make_lesson(bad, good), Reader(first, second), source_for(first))

    assert decision.accepted is False
    assert decision.reason == REASON_UNBOUND_CLAIM
    assert [(item.supported, item.reason) for item in decision.per_claim_results] == [
        (False, REASON_UNBOUND_CLAIM),
        (True, None),
    ]


def test_create_proposal_passes_source_and_reject_stores_only_the_proposal() -> None:
    receipt = bound_receipt(TEXT)
    lesson = bound_lesson(receipt)
    reader = Reader(receipt)

    missing = LearningStore(":memory:")
    rejected = create_proposal(
        missing,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attribution(source_for(receipt, work_id=None)),
        reader=reader,
    )
    assert rejected.status == "rejected"
    assert_proposal_only(missing, REASON_UNBOUND_SOURCE)

    foreign = receipt.model_copy(update={"work_id": uuid4()})
    mismatched = LearningStore(":memory:")
    create_proposal(
        mismatched,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attribution(source_for(receipt)),
        reader=Reader(foreign),
    )
    assert_proposal_only(mismatched, REASON_UNBOUND_RECEIPT)

    unbound = lesson.model_copy(
        update={"claims": (bound_claim(TEXT, receipt).model_copy(update={"source_span": None}),)}
    )
    span_miss = LearningStore(":memory:")
    create_proposal(
        span_miss,
        unit_id=uuid4(),
        lesson=unbound,
        attribution=attribution(source_for(receipt)),
        reader=reader,
    )
    assert_proposal_only(span_miss, REASON_UNBOUND_CLAIM)

    accepted_store = LearningStore(":memory:")
    accepted = create_proposal(
        accepted_store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attribution(source_for(receipt)),
        reader=reader,
    )
    assert accepted.status == "accepted"
    assert accepted.reason is None
    assert accepted_store.execute("SELECT COUNT(*) AS c FROM procedures").fetchone()["c"] == 1
    assert accepted_store.execute("SELECT COUNT(*) AS c FROM procedure_versions").fetchone()["c"] == 1
    assert accepted_store.execute("SELECT COUNT(*) AS c FROM procedure_support").fetchone()["c"] == 1
