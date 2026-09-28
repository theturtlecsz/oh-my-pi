"""Capture of /execute completion (complete_execution_item) into an accepted procedure."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.v1.api_models import WorkflowView, WorkItemsPage, WorkItemSummary
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import DEFAULT_CAPTURE_TYPES, drain
from omp_knowledge.learning.generation import GenerationResult
from omp_knowledge.learning.models import Claim, Lesson
from omp_knowledge.learning.store import LearningStore

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflows" / "OMP-175.json"


class DictReceiptReader:
    """Dict-backed fake reader conforming to the NativeReceipts protocol."""

    def __init__(self, receipts: dict[str, EvidenceReceipt] | None = None) -> None:
        self._receipts = {str(key): value for key, value in (receipts or {}).items()}

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


class OneWorkRecord:
    """In-memory NativeRecords double for a single recorded workflow projection."""

    def __init__(self, key: str, view: WorkflowView) -> None:
        self._key = key
        self._view = view
        self._summary = WorkItemSummary(
            work_id=view.item.work_id,
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
        if after is not None:
            return WorkItemsPage(items=())
        return WorkItemsPage(items=(self._summary,))

    def workflow(self, key: str) -> WorkflowView:
        if key != self._key:
            raise KeyError(key)
        return self._view


def test_execute_completion_is_captured_and_accepted_on_pass() -> None:
    """complete_execution_item becomes one unit with that event's work record and
    an accepted procedure v1; skip_active_item is not captured."""
    assert DEFAULT_CAPTURE_TYPES == frozenset(
        {"complete_work", "complete_execution_item"}
    )

    payload = json.loads(FIXTURE.read_text())
    key = str(payload["key"])
    view = WorkflowView.model_validate(payload["workflow"])
    work_id = str(view.item.work_id)
    records = OneWorkRecord(key, view)

    audit = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=view.item.work_id,
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.AUDIT,
        payload={"report": "pass"},
        payload_sha256=HEX,
        issuer="test-auditor",
        issued_at=datetime.now(timezone.utc),
        verdict="PASS",
        independent=True,
    )
    lesson = Lesson(
        title="Finish the execution item from its own record",
        steps=("read the finished item", "cite the passing audit"),
        claims=(Claim(text="the audit passed", receipt_ids=(audit.receipt_id,)),),
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="stub-model",
                profile="stub-profile",
                request_sha256=HEX,
                response_sha256=HEX,
                lessons=(lesson,),
            )
        ],
        model="stub-model",
        profile="stub-profile",
    )

    completed = make_event(
        sequence=1,
        event_type="complete_execution_item",
        workspace_id=UUID(WORKSPACE),
        aggregate_id=uuid4(),
        payload={
            "work_id": work_id,
            "state": "DONE",
            "type": "complete_execution_item",
        },
    )
    skipped = make_event(
        sequence=2,
        event_type="skip_active_item",
        workspace_id=UUID(WORKSPACE),
        payload={"work_id": work_id, "state": "SKIPPED"},
    )

    store = LearningStore(":memory:")
    run = drain(
        store,
        FakeEvents([completed, skipped]),
        DictReceiptReader({str(audit.receipt_id): audit}),
        generator,
        workspace_id=WORKSPACE,
        records=records,
    )

    assert run.status == "succeeded"
    assert run.units_total == 1
    assert run.proposals_accepted == 1
    assert run.proposals_rejected == 0
    assert len(generator.calls) == 1

    trace = generator.calls[0]
    assert trace["event_type"] == "complete_execution_item"
    assert trace["event_id"] == str(completed.event_id)
    assert trace["payload"]["work_id"] == work_id
    assert trace["work_record"]["work_id"] == work_id
    assert trace["work_record"]["work_key"] == key
    assert trace["aggregate_id"] != work_id

    rows = store.execute("SELECT event_id, trace_json FROM units").fetchall()
    assert [row["event_id"] for row in rows] == [str(completed.event_id)]
    stored = json.loads(rows[0]["trace_json"])
    assert stored["event_type"] == "complete_execution_item"
    assert stored["work_record"]["work_id"] == work_id

    procedure = store.execute(
        "SELECT procedure_id, status, current_version FROM procedures"
    ).fetchone()
    assert procedure["status"] == "active"
    assert procedure["current_version"] == 1
    version = store.execute(
        "SELECT version FROM procedure_versions WHERE procedure_id = ? AND version = 1",
        (procedure["procedure_id"],),
    ).fetchone()
    assert version is not None
    proposal = store.execute("SELECT status, procedure_id FROM proposals").fetchone()
    assert proposal["status"] == "accepted"
    assert proposal["procedure_id"] == procedure["procedure_id"]
