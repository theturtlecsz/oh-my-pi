"""Admission binding of source ids and claim span (OMP-607-s01-s01).

Legacy source JSON and a claim without a span still validate. Capture copies
work_id, candidate_id, and revision_id from the stored trace, and retry
recomputes them without another records read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work.v1.api_models import WorkflowView, WorkItemsPage, WorkItemSummary
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from pydantic import ValidationError
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import drain, retry
from omp_knowledge.learning.generation import GenerationResult, GeneratorUnavailable
from omp_knowledge.learning.models import Claim, Lesson, SourceIdentity, SourceSpan
from omp_knowledge.learning.store import LearningStore

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflows" / "OMP-175.json"


def load_fixture_workflow() -> tuple[str, WorkflowView]:
    payload = json.loads(FIXTURE.read_text())
    return str(payload["key"]), WorkflowView.model_validate(payload["workflow"])


class DictReceiptReader:
    def __init__(self, receipts: dict[str, EvidenceReceipt] | None = None) -> None:
        self._receipts = {str(key): value for key, value in (receipts or {}).items()}

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


class OneWorkRecord:
    """NativeRecords double. ``summary_work_id`` can disagree with the view."""

    def __init__(
        self,
        key: str,
        view: WorkflowView,
        *,
        summary_work_id: UUID | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._key = key
        self._view = view
        self._summary = WorkItemSummary(
            work_id=summary_work_id or view.item.work_id,
            key=key,
            state=view.item.state,
            created_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        del limit
        self.calls.append("work_items")
        if after is not None:
            return WorkItemsPage(items=())
        return WorkItemsPage(items=(self._summary,))

    def workflow(self, key: str) -> WorkflowView:
        self.calls.append("workflow")
        if key != self._key:
            raise KeyError(key)
        return self._view


def no_lesson_generator() -> StubGenerator:
    return StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="binding only",
            )
        ],
        model="m",
        profile="p",
    )


def drain_unit_source(
    records: OneWorkRecord,
    *,
    payload: dict[str, object],
    aggregate_id: UUID,
) -> tuple[LearningStore, SourceIdentity]:
    event = make_event(
        sequence=1,
        event_type="complete_execution_item",
        workspace_id=UUID(WORKSPACE),
        aggregate_id=aggregate_id,
        payload=payload,
    )
    store = LearningStore(":memory:")
    drain(
        store,
        FakeEvents([event]),
        DictReceiptReader(),
        no_lesson_generator(),
        workspace_id=WORKSPACE,
        records=records,
    )
    row = store.execute("SELECT source_json FROM units").fetchone()
    assert row is not None
    source = SourceIdentity.model_validate_json(row["source_json"])
    assert source.aggregate_id == str(aggregate_id)
    assert source.event_id == str(event.event_id)
    assert source.workspace_id == WORKSPACE
    assert source.event_sequence == 1
    return store, source


def test_legacy_source_json_without_binding_ids_validates() -> None:
    """Source JSON written before the three ids still parses, with those ids null."""
    raw = json.dumps(
        {
            "workspace_id": WORKSPACE,
            "event_id": str(uuid4()),
            "event_sequence": 3,
            "aggregate_id": str(uuid4()),
        }
    )
    source = SourceIdentity.model_validate_json(raw)
    assert source.work_id is None
    assert source.candidate_id is None
    assert source.revision_id is None
    assert source.event_sequence == 3


def test_source_identity_binding_ids_round_trip() -> None:
    """work_id, candidate_id, and revision_id are str and survive a JSON round trip."""
    original = SourceIdentity(
        workspace_id=WORKSPACE,
        event_id=str(uuid4()),
        event_sequence=4,
        aggregate_id=str(uuid4()),
        work_id=str(uuid4()),
        candidate_id=str(uuid4()),
        revision_id=str(uuid4()),
    )
    restored = SourceIdentity.model_validate_json(original.model_dump_json())
    assert restored == original
    with pytest.raises(ValidationError):
        SourceIdentity(
            workspace_id=WORKSPACE,
            event_id=str(uuid4()),
            event_sequence=1,
            aggregate_id=str(uuid4()),
            work_id=uuid4(),  # type: ignore[arg-type]
        )


def test_claim_without_span_validates() -> None:
    """A claim that names no span, evaluator, or outcome still validates."""
    claim = Claim.model_validate_json('{"text":"lease held","receipt_ids":[]}')
    assert claim.source_span is None
    assert claim.evaluator is None
    assert claim.outcome is None
    assert claim.text == "lease held"


@pytest.mark.parametrize("outcome", ["PASS", "NEEDS_FIX", "BLOCKED"])
def test_claim_span_evaluator_outcome_round_trip(outcome: str) -> None:
    """A span, evaluator, and outcome survive a JSON round trip."""
    claim = Claim(
        text="the audit passed",
        source_span=SourceSpan(start=4, end=18),
        evaluator="test-runner",
        outcome=outcome,  # type: ignore[arg-type]
    )
    restored = Claim.model_validate_json(claim.model_dump_json())
    assert restored == claim
    assert restored.source_span is not None
    assert restored.source_span.start == 4
    assert restored.source_span.end == 18
    assert restored.evaluator == "test-runner"
    assert restored.outcome == outcome


def test_source_span_is_non_negative_offsets_without_a_field_name() -> None:
    """start and end are >= 0, and a payload field name is not part of the span."""
    assert SourceSpan(start=0, end=0) == SourceSpan.model_validate(
        {"start": 0, "end": 0}
    )
    with pytest.raises(ValidationError):
        SourceSpan(start=-1, end=1)
    with pytest.raises(ValidationError):
        SourceSpan(start=0, end=-1)
    with pytest.raises(ValidationError):
        SourceSpan.model_validate({"start": 0, "end": 1, "field": "payload"})
    with pytest.raises(ValidationError):
        Claim(text="x", outcome="FAIL")  # type: ignore[arg-type]


def test_drain_copies_record_ids_when_payload_omits_candidate() -> None:
    """Payload work_id, no candidate_id, and a different aggregate copy the record ids."""
    key, view = load_fixture_workflow()
    assert view.item.candidate is not None
    aggregate_id = uuid4()
    assert aggregate_id != view.item.work_id
    records = OneWorkRecord(key, view)
    _store, source = drain_unit_source(
        records,
        payload={"work_id": str(view.item.work_id), "state": "DONE"},
        aggregate_id=aggregate_id,
    )
    assert source.work_id == str(view.item.work_id)
    assert source.work_id != source.aggregate_id
    assert source.candidate_id == str(view.item.candidate.candidate_id)
    assert source.revision_id == str(view.item.revision.revision_id)


def test_drain_conflicting_payload_candidate_stores_null_ids() -> None:
    """A payload candidate_id that differs from the record stores both ids null."""
    key, view = load_fixture_workflow()
    assert view.item.candidate is not None
    item_candidate = str(view.item.candidate.candidate_id)
    other = next(
        str(receipt.candidate_id)
        for receipt in view.receipts
        if receipt.candidate_id is not None
        and str(receipt.candidate_id) != item_candidate
    )
    aggregate_id = uuid4()
    _store, source = drain_unit_source(
        OneWorkRecord(key, view),
        payload={
            "work_id": str(view.item.work_id),
            "state": "DONE",
            "candidate_id": other,
        },
        aggregate_id=aggregate_id,
    )
    assert source.work_id == str(view.item.work_id)
    assert source.candidate_id is None
    assert source.revision_id is None


def test_drain_matching_payload_candidate_keeps_record_ids() -> None:
    """A payload candidate_id equal to the record is not a conflict."""
    key, view = load_fixture_workflow()
    assert view.item.candidate is not None
    _store, source = drain_unit_source(
        OneWorkRecord(key, view),
        payload={
            "work_id": str(view.item.work_id),
            "state": "DONE",
            "candidate_id": str(view.item.candidate.candidate_id),
        },
        aggregate_id=uuid4(),
    )
    assert source.candidate_id == str(view.item.candidate.candidate_id)
    assert source.revision_id == str(view.item.revision.revision_id)


def test_drain_payload_revision_mismatch_stores_null_ids() -> None:
    """A payload revision_id that differs stores candidate_id and revision_id null."""
    key, view = load_fixture_workflow()
    _store, source = drain_unit_source(
        OneWorkRecord(key, view),
        payload={
            "work_id": str(view.item.work_id),
            "state": "DONE",
            "revision_id": str(uuid4()),
        },
        aggregate_id=uuid4(),
    )
    assert source.work_id == str(view.item.work_id)
    assert source.candidate_id is None
    assert source.revision_id is None


def test_drain_record_work_id_mismatch_stores_null_ids() -> None:
    """A work record whose work_id differs from the event work id stores both null."""
    key, view = load_fixture_workflow()
    event_work_id = uuid4()
    assert event_work_id != view.item.work_id
    _store, source = drain_unit_source(
        OneWorkRecord(key, view, summary_work_id=event_work_id),
        payload={"work_id": str(event_work_id), "state": "DONE"},
        aggregate_id=uuid4(),
    )
    assert source.work_id == str(event_work_id)
    assert source.candidate_id is None
    assert source.revision_id is None


def test_retry_rebinds_overwritten_source_without_records_call() -> None:
    """Retry keeps the trace candidate on the proposal and does not read records."""
    key, view = load_fixture_workflow()
    assert view.item.candidate is not None
    item_candidate = str(view.item.candidate.candidate_id)
    item_revision = str(view.item.revision.revision_id)
    work_id = view.item.work_id
    aggregate_id = uuid4()
    records = OneWorkRecord(key, view)
    receipt = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=work_id,
        revision_id=view.item.revision.revision_id,
        candidate_id=view.item.candidate.candidate_id,
        kind=EvidenceKind.VERIFICATION,
        payload={"exit_code": 0},
        payload_sha256=HEX,
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict="PASS",
        independent=True,
    )
    reader = DictReceiptReader({str(receipt.receipt_id): receipt})
    event = make_event(
        sequence=1,
        event_type="complete_execution_item",
        workspace_id=UUID(WORKSPACE),
        aggregate_id=aggregate_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    store = LearningStore(":memory:")
    failed = drain(
        store,
        FakeEvents([event]),
        reader,
        StubGenerator([GeneratorUnavailable("down")], model="m", profile="p"),
        workspace_id=WORKSPACE,
        records=records,
    )
    assert failed.status == "failed"
    calls_after_drain = list(records.calls)
    assert calls_after_drain

    row = store.execute("SELECT unit_id, source_json FROM units").fetchone()
    assert row is not None
    queued = SourceIdentity.model_validate_json(row["source_json"])
    assert queued.candidate_id == item_candidate
    tampered = queued.model_copy(
        update={
            "candidate_id": "overwritten-candidate",
            "revision_id": "overwritten-revision",
        }
    )
    store.execute(
        "UPDATE units SET source_json = ? WHERE unit_id = ?",
        (tampered.model_dump_json(), row["unit_id"]),
    )

    lesson = Lesson(
        title="Cite the passing verification",
        steps=("read the finished item", "cite the receipt"),
        claims=(Claim(text="verification passed", receipt_ids=(receipt.receipt_id,)),),
    )
    recovered = StubGenerator(
        [
            GenerationResult(
                model="m2",
                profile="p2",
                request_sha256=HEX,
                response_sha256=HEX,
                lessons=(lesson,),
            )
        ],
        model="m2",
        profile="p2",
    )
    retried = retry(store, reader, recovered)
    assert retried.status == "succeeded"
    assert records.calls == calls_after_drain

    proposal = store.execute("SELECT source_json FROM proposals").fetchone()
    assert proposal is not None
    bound = SourceIdentity.model_validate_json(proposal["source_json"])
    assert bound.candidate_id == item_candidate
    assert bound.revision_id == item_revision
    assert bound.work_id == str(work_id)
    assert bound.aggregate_id == str(aggregate_id)
    assert bound.event_id == str(event.event_id)
    assert bound.workspace_id == WORKSPACE
    assert bound.candidate_id != "overwritten-candidate"

    unit = store.execute("SELECT source_json FROM units").fetchone()
    assert unit is not None
    restored = SourceIdentity.model_validate_json(unit["source_json"])
    assert restored.candidate_id == item_candidate
    assert restored.revision_id == item_revision
