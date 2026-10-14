"""Tests for research-aware execution record capture into learning traces (OMP-588-s03)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from omp_work.v1.api_models import ResearchView, WorkflowView, WorkItemsPage, WorkItemSummary
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import (
    EvidenceKind,
    EvidenceReceipt,
    ResearchCampaign,
    ResearchDeliverableBinding,
)
from omp_work.v1.service import WorkError
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import DEFAULT_CAPTURE_TYPES, drain, retry
from omp_knowledge.learning.generation import GenerationResult, GeneratorUnavailable
from omp_knowledge.learning.models import Claim, Lesson, SourceSpan
from omp_knowledge.learning.record import (
    NativeRecords,
    NativeResearchRecords,
    bounded_capture_records,
)
from omp_knowledge.learning.store import LearningStore

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64
WORK = UUID(int=100)
FOREIGN = UUID(int=999)
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflows" / "OMP-175.json"


def load_fixture_workflow() -> tuple[str, WorkflowView]:
    payload = json.loads(FIXTURE.read_text())
    return str(payload["key"]), WorkflowView.model_validate(payload["workflow"])


def make_campaign(
    number: int,
    *,
    work_id: UUID = WORK,
    state: str = "concluded",
    reason: str | None = "Verification passed",
) -> ResearchCampaign:
    return ResearchCampaign.model_validate({
        "campaign_id": UUID(int=number),
        "workspace_id": UUID(WORKSPACE),
        "work_id": work_id,
        "revision_id": UUID(int=4),
        "domain": "engineering",
        "state": state,
        "created_at": NOW,
        "outcome": "supported" if state == "concluded" else None,
        "outcome_reason": reason,
        "spec_sha256": "a" * 64,
        "spec": {
            "objective": "Compare candidates",
            "evaluation_protocol_id": "eval",
            "evaluation_protocol_sha256": "b" * 64,
            "resource_policy_ref": "bounded",
            "candidate_mapping_policy": "native",
        },
    })


def make_binding(
    campaign_number: int,
    trial_number: int,
    *,
    work_id: UUID = WORK,
) -> ResearchDeliverableBinding:
    return ResearchDeliverableBinding(
        campaign_id=UUID(int=campaign_number),
        trial_id=UUID(int=trial_number),
        workspace_id=UUID(WORKSPACE),
        work_id=work_id,
        revision_id=UUID(int=4),
        native_candidate_id=UUID(int=trial_number + 1000),
        candidate_digest=f"{trial_number:064x}",
        binding_sha256="c" * 64,
        bound_at=NOW,
    )


class DictReceiptReader:
    def __init__(self, receipts: dict[str, EvidenceReceipt] | None = None) -> None:
        self._receipts = {str(k): v for k, v in (receipts or {}).items()}

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(work_id: UUID = WORK, verdict: str = "PASS") -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=work_id,
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.VERIFICATION,
        payload={"exit_code": 0},
        payload_sha256=HEX,
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


class RecordingRecordsDouble:
    """Test double implementing NativeResearchRecords with call tracking."""

    def __init__(
        self,
        items: dict[str, tuple[UUID, WorkflowView | Exception | None, ResearchView | Exception | None]],
        *,
        store: LearningStore | None = None,
    ) -> None:
        self._items = items
        self._store = store
        self.work_items_calls: list[tuple[tuple[datetime, UUID] | None, int | None]] = []
        self.workflow_calls: list[str] = []
        self.research_calls: list[str] = []
        self.calls_in_transaction: list[str] = []

    def _check_transaction(self, method: str) -> None:
        if self._store is not None and getattr(self._store, "_in_transaction", False):
            self.calls_in_transaction.append(method)

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        self._check_transaction("work_items")
        self.work_items_calls.append((after, limit))
        summaries = [
            WorkItemSummary(
                work_id=work_id,
                key=key,
                state="DONE",
                created_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
            )
            for key, (work_id, _, _) in self._items.items()
        ]
        return WorkItemsPage(items=tuple(summaries))

    def workflow(self, key: str) -> WorkflowView:
        self._check_transaction("workflow")
        self.workflow_calls.append(key)
        if key not in self._items:
            raise KeyError(key)
        _, view_or_exc, _ = self._items[key]
        if isinstance(view_or_exc, Exception):
            raise view_or_exc
        if view_or_exc is None:
            raise AssertionError(f"unexpected workflow call for {key}")
        return view_or_exc

    def research(self, key: str) -> ResearchView:
        self._check_transaction("research")
        self.research_calls.append(key)
        if key not in self._items:
            raise KeyError(key)
        _, _, view_or_exc = self._items[key]
        if isinstance(view_or_exc, Exception):
            raise view_or_exc
        if view_or_exc is None:
            raise AssertionError(f"unexpected research call for {key}")
        return view_or_exc


class LegacyNativeRecordsDouble:
    """Test double conforming only to NativeRecords (no research method)."""

    def __init__(self, key: str, work_id: UUID, workflow_view: WorkflowView) -> None:
        self._key = key
        self._work_id = work_id
        self._view = workflow_view
        self.workflow_calls: list[str] = []

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        return WorkItemsPage(items=(
            WorkItemSummary(
                work_id=self._work_id,
                key=self._key,
                state="DONE",
                created_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
            ),
        ))

    def workflow(self, key: str) -> WorkflowView:
        self.workflow_calls.append(key)
        if key != self._key:
            raise KeyError(key)
        return self._view


def test_drain_complete_work_attaches_bounded_research_and_excludes_foreign_running() -> None:
    """complete_work trace receives concluded own-work research; running/foreign excluded."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    campaigns = (
        make_campaign(10, work_id=work_id, state="concluded", reason="Proved fix"),
        make_campaign(11, work_id=work_id, state="running", reason="Still active"),
        make_campaign(12, work_id=FOREIGN, state="concluded", reason="Other work"),
    )
    bindings = (
        make_binding(10, 201, work_id=work_id),
        make_binding(10, 202, work_id=work_id),
        make_binding(11, 203, work_id=work_id),
        make_binding(12, 204, work_id=FOREIGN),
    )
    res_view = ResearchView(work_id=work_id, campaigns=campaigns, deliverable_bindings=bindings)

    store = LearningStore(":memory:")
    records = RecordingRecordsDouble(
        {key: (work_id, wf_view, res_view)},
        store=store,
    )

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="tested",
            )
        ],
        model="m",
        profile="p",
    )

    run = drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    assert run.status == "no_lesson"
    assert len(generator.calls) == 1
    trace = generator.calls[0]
    assert trace["work_record"]["work_id"] == str(work_id)
    assert trace["research_record"] is not None
    assert trace["research_record"]["work_id"] == str(work_id)
    # Only campaign 10 is present (concluded and same work_id)
    assert len(trace["research_record"]["campaigns"]) == 1
    camp = trace["research_record"]["campaigns"][0]
    assert camp["campaign_id"] == str(UUID(int=10))
    assert camp["outcome"] == "supported"
    assert camp["outcome_reason"] == "Proved fix"
    assert [ref["trial_id"] for ref in camp["deliverable_refs"]] == [str(UUID(int=201)), str(UUID(int=202))]
    assert camp["truncated"] == {"deliverable_refs": 0}
    assert trace["research_record"]["truncated"] == {"campaigns": 0}

    # E0142 defense: no network or alias read happened inside the write transaction
    assert records.calls_in_transaction == []


def test_drain_complete_execution_item_uses_payload_work_id_differing_aggregate_ignored() -> None:
    """complete_execution_item selects by payload work_id, never execution aggregate."""
    key, wf_view = load_fixture_workflow()
    target_work_id = wf_view.item.work_id
    exec_aggregate_id = uuid4()
    other_work_id = uuid4()

    target_campaign = make_campaign(10, work_id=target_work_id)
    target_res = ResearchView(work_id=target_work_id, campaigns=(target_campaign,), deliverable_bindings=())

    other_campaign = make_campaign(99, work_id=other_work_id)
    other_res = ResearchView(work_id=other_work_id, campaigns=(other_campaign,), deliverable_bindings=())

    records = RecordingRecordsDouble({
        key: (target_work_id, wf_view, target_res),
        "EXEC-OTHER": (exec_aggregate_id, wf_view, other_res),
    })

    event = make_event(
        sequence=1,
        event_type="complete_execution_item",
        aggregate_id=exec_aggregate_id,
        payload={"work_id": str(target_work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="done",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    run = drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    assert run.status == "no_lesson"
    trace = generator.calls[0]
    assert trace["aggregate_id"] == str(exec_aggregate_id)
    assert trace["payload"]["work_id"] == str(target_work_id)
    assert trace["work_record"]["work_id"] == str(target_work_id)
    assert trace["research_record"]["work_id"] == str(target_work_id)
    assert [c["campaign_id"] for c in trace["research_record"]["campaigns"]] == [str(UUID(int=10))]
    assert records.workflow_calls == [key]
    assert records.research_calls == [key]


def test_drain_oversize_research_discloses_omissions_and_clips_reasons() -> None:
    """Oversized campaign counts, refs, and long reasons disclose truncation in trace."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    # 40 campaigns (limit is 32), each with 12 bindings (limit is 8) and 5000-char reasons
    campaigns = tuple(
        make_campaign(n, work_id=work_id, reason="y" * 5000)
        for n in range(1, 41)
    )
    bindings = tuple(
        make_binding(c_idx, t_idx, work_id=work_id)
        for c_idx in range(1, 41)
        for t_idx in range(101, 113)
    )
    res_view = ResearchView(work_id=work_id, campaigns=campaigns, deliverable_bindings=bindings)

    records = RecordingRecordsDouble({key: (work_id, wf_view, res_view)})
    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="oversize",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    trace = generator.calls[0]
    res_block = trace["research_record"]
    assert len(res_block["campaigns"]) == 32
    assert res_block["truncated"]["campaigns"] == 8
    for camp in res_block["campaigns"]:
        assert len(camp["deliverable_refs"]) == 8
        assert camp["truncated"]["deliverable_refs"] == 4
        assert camp["outcome_reason"].endswith(" ...[truncated]")
        assert len(camp["outcome_reason"]) == 2000 + len(" ...[truncated]")


def test_drain_workflow_failure_preserves_readable_research() -> None:
    """Workflow read failure sets work_record=null but preserves research_record."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    campaigns = (make_campaign(10, work_id=work_id),)
    res_view = ResearchView(work_id=work_id, campaigns=campaigns, deliverable_bindings=())

    # workflow read raises WorkError
    workflow_err = WorkError("service_unavailable", status=503, diagnostics=("timeout",))
    records = RecordingRecordsDouble({key: (work_id, workflow_err, res_view)})

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="ok",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    run = drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    assert run.status == "no_lesson"
    trace = generator.calls[0]
    assert trace["work_record"] is None
    assert trace["research_record"] is not None
    assert trace["research_record"]["work_id"] == str(work_id)
    assert len(trace["research_record"]["campaigns"]) == 1


def test_drain_research_read_failure_preserves_work_record_and_leaves_research_null() -> None:
    """Research read failure leaves research_record=null while work_record survives."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    # research read raises httpx.HTTPError
    res_err = httpx.ReadTimeout("read timed out")
    records = RecordingRecordsDouble({key: (work_id, wf_view, res_err)})

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="ok",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    run = drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    assert run.status == "no_lesson"
    trace = generator.calls[0]
    assert trace["work_record"] is not None
    assert trace["work_record"]["work_key"] == key
    assert trace["research_record"] is None


def test_readable_empty_research_is_empty_campaigns() -> None:
    """Readable empty research yields empty campaigns list, not null."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    res_view = ResearchView(work_id=work_id, campaigns=(), deliverable_bindings=())
    records = RecordingRecordsDouble({key: (work_id, wf_view, res_view)})

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="empty",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    trace = generator.calls[0]
    assert trace["research_record"] == {
        "work_id": str(work_id),
        "campaigns": [],
        "truncated": {"campaigns": 0},
    }


def test_legacy_native_records_double_without_research_method_works() -> None:
    """A legacy NativeRecords double lacking research method leaves research_record null."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    records = LegacyNativeRecordsDouble(key, work_id, wf_view)
    assert not hasattr(records, "research")

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="legacy",
            )
        ],
        model="m",
        profile="p",
    )

    store = LearningStore(":memory:")
    run = drain(store, FakeEvents([event]), DictReceiptReader(), generator, workspace_id=WORKSPACE, records=records)

    assert run.status == "no_lesson"
    trace = generator.calls[0]
    assert trace["work_record"] is not None
    assert trace["work_record"]["work_key"] == key
    assert trace["research_record"] is None


def test_retry_retains_stored_trace_snapshot_without_new_reads() -> None:
    """Retry reuses stored unit trace (including research_record) without re-reading records."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id

    campaigns = (make_campaign(10, work_id=work_id),)
    res_view = ResearchView(work_id=work_id, campaigns=campaigns, deliverable_bindings=())

    store = LearningStore(":memory:")
    records = RecordingRecordsDouble({key: (work_id, wf_view, res_view)}, store=store)

    claim_text = "verified"
    receipt_payload = {"exit_code": 0, "claim": claim_text}
    claim_start = canonical_json(receipt_payload).index(claim_text)
    receipt = make_receipt(work_id=work_id).model_copy(update={
        "revision_id": wf_view.item.revision.revision_id,
        "candidate_id": wf_view.item.candidate.candidate_id,
        "payload": receipt_payload,
        "payload_sha256": sha256(receipt_payload),
    })
    reader = DictReceiptReader({str(receipt.receipt_id): receipt})

    event = make_event(
        sequence=1,
        event_type="complete_work",
        aggregate_id=work_id,
        payload={"work_id": str(work_id), "state": "DONE"},
    )

    # First drain fails due to generator outage
    failing_gen = StubGenerator([GeneratorUnavailable("down")], model="m1", profile="p1")
    failed_run = drain(store, FakeEvents([event]), reader, failing_gen, workspace_id=WORKSPACE, records=records)
    assert failed_run.status == "failed"
    assert len(records.workflow_calls) == 1
    assert len(records.research_calls) == 1

    # Recovered generator produces an accepted lesson citing receipt and campaign
    lesson = Lesson(
        title="Apply campaign outcome",
        steps=("step 1",),
        claims=(Claim(
            text=claim_text,
            receipt_ids=(receipt.receipt_id,),
            source_span=SourceSpan(start=claim_start, end=claim_start + len(claim_text)),
            evaluator=receipt.issuer,
            outcome="PASS",
        ),),
        research_campaign_ids=(str(UUID(int=10)),),
    )
    recovered_gen = StubGenerator(
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

    # Run retry — no records argument is even accepted or needed by retry
    retried_run = retry(store, reader, recovered_gen)
    assert retried_run.status == "succeeded"
    assert retried_run.proposals_accepted == 1

    # Verified: no new reads were made to records during retry
    assert len(records.workflow_calls) == 1
    assert len(records.research_calls) == 1

    # Verified: recovered generator received the original snapshot trace
    assert len(recovered_gen.calls) == 1
    retried_trace = recovered_gen.calls[0]
    assert retried_trace["work_record"]["work_key"] == key
    assert retried_trace["work_record"]["work_id"] == str(work_id)
    assert retried_trace["work_record"]["revision"]["revision_id"] == str(receipt.revision_id)
    assert retried_trace["work_record"]["candidate"]["candidate_id"] == str(receipt.candidate_id)
    assert retried_trace["research_record"]["work_id"] == str(work_id)
    assert [c["campaign_id"] for c in retried_trace["research_record"]["campaigns"]] == [str(UUID(int=10))]


def test_bounded_capture_records_unit_contract() -> None:
    """Direct contract tests for bounded_capture_records helper."""
    key, wf_view = load_fixture_workflow()
    work_id = wf_view.item.work_id
    res_view = ResearchView(work_id=work_id, campaigns=(make_campaign(10, work_id=work_id),), deliverable_bindings=())

    records = RecordingRecordsDouble({key: (work_id, wf_view, res_view)})

    # 1. Normal resolution: both records returned
    w_rec, r_rec = bounded_capture_records(records, work_id)
    assert w_rec is not None and w_rec["work_key"] == key
    assert r_rec is not None and len(r_rec["campaigns"]) == 1

    # 2. Unknown work_id: returns (None, None)
    assert bounded_capture_records(records, uuid4()) == (None, None)

    # 3. WorkItems failure: returns (None, None)
    class RefusingWorkItems:
        def work_items(self, **_: Any) -> WorkItemsPage:
            raise WorkError("forbidden", status=403)
        def workflow(self, key: str) -> WorkflowView:
            raise AssertionError("unreachable")

    assert bounded_capture_records(RefusingWorkItems(), work_id) == (None, None)

    # 4. Legacy double: returns (w_rec, None)
    legacy = LegacyNativeRecordsDouble(key, work_id, wf_view)
    w_rec2, r_rec2 = bounded_capture_records(legacy, work_id)
    assert w_rec2 is not None
    assert r_rec2 is None
