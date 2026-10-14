from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from omp_knowledge.learning.generation import LocalChatGenerator
from omp_knowledge.learning.models import Attribution, Lesson, SourceIdentity
from omp_knowledge.learning.proposals import ProposalRecord, create_proposal
from omp_knowledge.learning.store import LearningStore
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from support.bound_evidence import bound_claim

CAMPAIGN_ID = str(UUID(int=588))


class ReceiptReader:
    def __init__(self, receipt: EvidenceReceipt) -> None:
        self._receipt = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        if str(receipt_id) != str(self._receipt.receipt_id):
            raise KeyError(receipt_id)
        return self._receipt


@pytest.fixture
def receipt() -> EvidenceReceipt:
    payload = {"exit_code": 0, "summary": "Candidate verification passed"}
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.VERIFICATION,
        payload=payload,
        payload_sha256=sha256(payload),
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict="PASS",
        independent=True,
    )


@pytest.fixture
def attribution(receipt: EvidenceReceipt) -> Attribution:
    return Attribution(
        model="local-model",
        profile="local",
        source=SourceIdentity(
            workspace_id=uuid4(), event_id=uuid4(), event_sequence=1, aggregate_id=uuid4(),
            work_id=str(receipt.work_id), candidate_id=str(receipt.candidate_id),
            revision_id=str(receipt.revision_id),
        ),
    )


def lesson_payload(receipt: EvidenceReceipt) -> dict[str, object]:
    return {
        "title": "Verify the selected deliverable",
        "steps": ["Run verification on the selected candidate"],
        "claims": [bound_claim("Candidate verification passed", receipt).model_dump(mode="json")],
    }


def generate(payload: dict[str, object], receipt: EvidenceReceipt) -> Lesson:
    content = json.dumps({"lessons": [payload], "no_lesson_reason": None})
    trace = {
        "research_record": {
            "work_id": str(receipt.work_id),
            "campaigns": [{
                "campaign_id": CAMPAIGN_ID,
                "outcome": "selected",
                "outcome_reason": "Verification passed",
                "deliverable_refs": [{"native_candidate_id": str(receipt.candidate_id)}],
            }],
        },
        "receipts": [receipt.model_dump(mode="json")],
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]},
    ))
    generator = LocalChatGenerator(
        "http://127.0.0.1:18080", model="local-model", profile="local", transport=transport,
    )
    try:
        return generator.generate(trace).lessons[0]
    finally:
        generator.close()


def test_generated_campaign_citation_survives_accepted_proposal_reopen(
    tmp_path: Path, receipt: EvidenceReceipt, attribution: Attribution,
) -> None:
    """Chat citations survive proposal JSON persistence without becoming receipt support."""
    lesson = generate(lesson_payload(receipt) | {"research_campaign_ids": [CAMPAIGN_ID]}, receipt)
    assert lesson.research_campaign_ids == (CAMPAIGN_ID,)
    assert attribution.source.work_id == str(receipt.work_id)
    assert attribution.source.candidate_id == str(receipt.candidate_id)
    assert attribution.source.revision_id == str(receipt.revision_id)
    db_path = tmp_path / "learning.sqlite"
    with LearningStore(db_path) as store:
        record = create_proposal(
            store, unit_id=uuid4(), lesson=lesson, attribution=attribution, reader=ReceiptReader(receipt),
        )
        assert record.status == "accepted"
        assert record.reason is None
        assert json.loads(record.lesson_json)["research_campaign_ids"] == [CAMPAIGN_ID]

        # Citation metadata must not split the existing procedure fingerprint.
        uncited = generate(lesson_payload(receipt), receipt)
        second = create_proposal(
            store, unit_id=uuid4(), lesson=uncited, attribution=attribution, reader=ReceiptReader(receipt),
        )
        assert second.status == "accepted"
        assert second.procedure_id == record.procedure_id

    with LearningStore(db_path) as store:
        row = store.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,),
        ).fetchone()
        restored = ProposalRecord.model_validate(dict(row))
        assert restored.status == "accepted"
        assert restored.lesson == lesson
        assert restored.source.work_id == str(receipt.work_id)
        assert restored.source.candidate_id == str(receipt.candidate_id)
        assert restored.source.revision_id == str(receipt.revision_id)
        assert restored.lesson.research_campaign_ids == (CAMPAIGN_ID,)
        assert restored.lesson.claims[0].receipt_ids == (str(receipt.receipt_id),)
        for evidenced_lesson in (lesson, restored.lesson):
            for claim in evidenced_lesson.claims:
                assert claim.receipt_ids == (str(receipt.receipt_id),)
                for receipt_id in claim.receipt_ids:
                    resolved = ReceiptReader(receipt).receipt(receipt_id)
                    assert resolved == receipt
                    assert claim.source_span is not None
                    assert canonical_json(resolved.payload)[
                        claim.source_span.start:claim.source_span.end
                    ] == claim.text
                    assert claim.evaluator == resolved.issuer
                    assert claim.outcome == resolved.verdict == "PASS"
        support = store.execute("SELECT receipt_id FROM procedure_support").fetchall()
        assert [row["receipt_id"] for row in support] == [str(receipt.receipt_id)]


def test_legacy_payload_has_no_inferred_campaign_citation(
    tmp_path: Path, receipt: EvidenceReceipt, attribution: Attribution,
) -> None:
    """Omitted field stays empty in chat output and pre-field stored proposal JSON."""
    payload = lesson_payload(receipt)
    lesson = generate(payload, receipt)
    assert lesson.research_campaign_ids == ()
    assert attribution.source.work_id == str(receipt.work_id)
    assert attribution.source.candidate_id == str(receipt.candidate_id)
    assert attribution.source.revision_id == str(receipt.revision_id)
    db_path = tmp_path / "learning.sqlite"
    with LearningStore(db_path) as store:
        record = create_proposal(
            store, unit_id=uuid4(), lesson=lesson, attribution=attribution, reader=ReceiptReader(receipt),
        )
        assert record.status == "accepted"
        # Reproduce persisted bytes written before the optional field existed.
        with store.transaction() as conn:
            conn.execute(
                "UPDATE proposals SET lesson_json = ? WHERE proposal_id = ?",
                (json.dumps(payload), record.proposal_id),
            )
    with LearningStore(db_path) as store:
        row = store.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,),
        ).fetchone()
        restored = ProposalRecord.model_validate(dict(row))
        assert restored.source.work_id == str(receipt.work_id)
        assert restored.source.candidate_id == str(receipt.candidate_id)
        assert restored.source.revision_id == str(receipt.revision_id)
        assert restored.lesson.research_campaign_ids == ()
        assert restored.lesson.claims[0].receipt_ids == (str(receipt.receipt_id),)
        for evidenced_lesson in (lesson, restored.lesson):
            for claim in evidenced_lesson.claims:
                assert claim.receipt_ids == (str(receipt.receipt_id),)
                for receipt_id in claim.receipt_ids:
                    resolved = ReceiptReader(receipt).receipt(receipt_id)
                    assert resolved == receipt
                    assert claim.source_span is not None
                    assert canonical_json(resolved.payload)[
                        claim.source_span.start:claim.source_span.end
                    ] == claim.text
                    assert claim.evaluator == resolved.issuer
                    assert claim.outcome == resolved.verdict == "PASS"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no_claims", "no_citation"),
        ("empty_receipts", "no_citation"),
        ("campaign_as_receipt", "invalid_citation"),
        ("non_pass", "tool_success_only"),
        ("narration", "narration_only"),
    ],
)
def test_campaign_citations_cannot_bypass_receipt_admission(
    case: str, reason: str, receipt: EvidenceReceipt, attribution: Attribution,
) -> None:
    """Campaign grounding cannot admit missing, unresolvable, non-PASS or narration evidence."""
    payload = lesson_payload(receipt) | {"research_campaign_ids": [CAMPAIGN_ID]}
    if case == "no_claims":
        payload["claims"] = []
    elif case == "empty_receipts":
        payload["claims"] = [{"text": "Campaign selected a candidate", "receipt_ids": []}]
    elif case == "campaign_as_receipt":
        payload["claims"] = [{
            "text": "Campaign selected a candidate",
            "receipt_ids": [str(receipt.receipt_id), CAMPAIGN_ID],
        }]
    elif case == "non_pass":
        receipt = receipt.model_copy(update={"verdict": None})
    elif case == "narration":
        receipt = receipt.model_copy(update={"kind": EvidenceKind.HANDOFF, "verdict": None})
    lesson = generate(payload, receipt)
    with LearningStore(":memory:") as store:
        record = create_proposal(
            store, unit_id=uuid4(), lesson=lesson, attribution=attribution, reader=ReceiptReader(receipt),
        )
        assert record.status == "rejected"
        assert record.reason == reason
        assert record.procedure_id is None
        assert record.lesson.research_campaign_ids == (CAMPAIGN_ID,)
        assert store.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 0
        assert store.execute("SELECT COUNT(*) FROM procedure_support").fetchone()[0] == 0
