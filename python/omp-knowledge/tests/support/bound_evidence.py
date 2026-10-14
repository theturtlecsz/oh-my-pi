"""Setup for lessons bound to a receipt, claim span, and finished work record."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omp_work.v1.api_models import WorkflowView, WorkItemsPage, WorkItemSummary
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.learning.models import Claim, SourceSpan

HEX = "0" * 64
ISSUER = "test-runner"
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "workflows" / "OMP-175.json"


def bound_receipt(text: str, *, kind: EvidenceKind = EvidenceKind.VERIFICATION) -> EvidenceReceipt:
    """A PASS receipt whose canonical payload contains ``text``."""
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=kind,
        payload={"exit_code": 0, "summary": text},
        payload_sha256=HEX,
        issuer=ISSUER,
        issued_at=datetime.now(timezone.utc),
        verdict="PASS",
        independent=True,
    )


def bound_claim(text: str, receipt: EvidenceReceipt) -> Claim:
    """A claim whose span slices ``text`` out of the receipt's canonical payload."""
    start = canonical_json(receipt.payload).index(text)
    return Claim(
        text=text,
        receipt_ids=(receipt.receipt_id,),
        source_span=SourceSpan(start=start, end=start + len(text)),
        evaluator=receipt.issuer,
        outcome="PASS",
    )


def workflow_for(receipt: EvidenceReceipt, key: str = "OMP-607") -> WorkflowView:
    """The receipt's work item: its work, revision, and candidate ids and the receipt."""
    raw: dict[str, Any] = json.loads(FIXTURE.read_text())["workflow"]
    work_id, revision_id, candidate_id = (
        str(receipt.work_id),
        str(receipt.revision_id),
        str(receipt.candidate_id),
    )
    item = raw["item"]
    item["work_id"] = work_id
    item["alias"].update(work_id=work_id, key=key)
    item["revision"].update(work_id=work_id, revision_id=revision_id)
    item["candidate"].update(work_id=work_id, revision_id=revision_id, candidate_id=candidate_id)
    return WorkflowView.model_validate(
        {"item": item, "receipts": [receipt.model_dump(mode="json")]}
    )


class ReceiptRecords:
    """NativeRecords double serving one finished work item."""

    def __init__(self, receipt: EvidenceReceipt, key: str = "OMP-607") -> None:
        self.calls: list[str] = []
        self._key = key
        self._view = workflow_for(receipt, key)
        self._summary = WorkItemSummary(
            work_id=receipt.work_id,
            key=key,
            state=self._view.item.state,
            created_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        del after, limit
        self.calls.append("work_items")
        return WorkItemsPage(items=(self._summary,), next_created_at=None, next_work_id=None)

    def workflow(self, key: str) -> WorkflowView:
        self.calls.append("workflow")
        if key != self._key:
            raise KeyError(key)
        return self._view


def assert_work_record_bound(trace: dict[str, Any], receipt: EvidenceReceipt) -> None:
    """The stored trace's work_record carries the receipt's work, revision, candidate."""
    record = trace["work_record"]
    assert record["work_id"] == str(receipt.work_id)
    assert record["candidate"]["candidate_id"] == str(receipt.candidate_id)
    assert record["revision"]["revision_id"] == str(receipt.revision_id)
    assert [r["receipt_id"] for r in record["receipts"]] == [str(receipt.receipt_id)]
