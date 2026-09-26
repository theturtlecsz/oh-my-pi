"""Contract tests for the finished item's execution record on a captured trace (OMP-356).

Each test defends one externally observable contract of the FK-6 capture seam:

- a ``complete_work`` unit's generator request carries the finished item's own
  WorkService record (title, acceptance criteria, receipts, close history) when
  the item is readable, resolved from the bare ``work_id`` through the existing
  reads — while the ``no_lesson`` path still settles;
- an unreadable item degrades that one unit's evidence to an explicit ``null``
  record and never fails the capture;
- the ``work_id`` → ``key`` scan is bounded, so an oversized workspace yields no
  record instead of an unbounded walk;
- ``drain`` and ``retry`` ``--json`` name model, profile, and the source event
  sequence for every unit.

The fixture is a recorded ``WorkflowView`` projection of a real finished item
(OMP-175, state DONE, six receipts) stored under ``tests/fixtures/workflows``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_knowledge.learning.capture import NativeRecords, drain
from omp_knowledge.learning.cli import main as cli_main
from omp_knowledge.learning.generation import GenerationResult, GeneratorUnavailable
from omp_knowledge.learning.record import (
    bounded_work_record,
    resolve_work_key,
    work_record_block,
)
from omp_knowledge.learning.store import LearningStore
from omp_work.v1.api_models import WorkflowView, WorkItemsPage, WorkItemSummary
from omp_work.v1.service import WorkError
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflows" / "OMP-175.json"

TITLE = "Fix TypeScript compilation error in work.ts candidateDrift call"
CRITERIA = (
    (
        "bunx tsc --noEmit -p session-system/tsconfig.json passes cleanly without "
        "error TS2345 on candidateDrift calls"
    ),
    "candidate_commit being string | null is handled in candidateDrift or work.ts",
)


def load_recorded_view() -> tuple[str, WorkflowView]:
    payload = json.loads(FIXTURE.read_text())
    return str(payload["key"]), WorkflowView.model_validate(payload["workflow"])


def summary_for(
    key: str, view: WorkflowView, *, created_at: datetime
) -> WorkItemSummary:
    return WorkItemSummary(
        work_id=view.item.work_id,
        key=key,
        state=view.item.state,
        created_at=created_at,
    )


class RecordedWorkflows:
    """In-memory ``NativeRecords`` double over recorded ``WorkflowView`` fixtures."""

    def __init__(
        self,
        views: dict[str, WorkflowView],
        *,
        page_size: int = 500,
        created_at: datetime | None = None,
    ) -> None:
        self._views = dict(views)
        self._page_size = page_size
        self._created_at = created_at or datetime(2026, 9, 12, tzinfo=timezone.utc)
        self._summaries = sorted(
            (
                summary_for(key, view, created_at=self._created_at)
                for key, view in self._views.items()
            ),
            key=lambda summary: (summary.created_at, summary.work_id),
        )
        self.work_items_calls: list[
            tuple[tuple[datetime, UUID] | None, int | None]
        ] = []
        self.workflow_calls: list[str] = []

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        self.work_items_calls.append((after, limit))
        size = min(limit or self._page_size, self._page_size)
        remaining = [
            summary
            for summary in self._summaries
            if after is None or (summary.created_at, summary.work_id) > after
        ]
        page = remaining[:size]
        if not page or len(remaining) <= size:
            return WorkItemsPage(items=tuple(page))
        last = page[-1]
        return WorkItemsPage(
            items=tuple(page),
            next_created_at=last.created_at,
            next_work_id=last.work_id,
        )

    def workflow(self, key: str) -> WorkflowView:
        self.workflow_calls.append(key)
        if key not in self._views:
            raise AssertionError(f"unexpected workflow read for {key}")
        return self._views[key]


class UnreadableRecords:
    """A records seam whose reads refuse, as an unreachable service would."""

    def work_items(self, **_: Any) -> WorkItemsPage:
        raise WorkError("invalid_request", status=400, diagnostics=("unreachable",))

    def workflow(self, key: str) -> WorkflowView:
        raise AssertionError(f"workflow must not be read for {key}")


def make_no_lesson(
    model: str = "qwen-2.5", profile: str = "local-qwen"
) -> GenerationResult:
    return GenerationResult(
        model=model,
        profile=profile,
        request_sha256=HEX,
        response_sha256=HEX,
        no_lesson_reason="no transferable technique",
    )


def test_work_client_fits_native_records_protocol() -> None:
    """The read-only client satisfies the NativeRecords seam structurally."""
    from omp_work.v1.client import WorkClient

    assert issubclass(WorkClient, NativeRecords)


def test_work_record_block_renders_finished_item_evidence() -> None:
    """The recorded projection renders the item's title, criteria and receipt
    identities — the evidence a lesson's claims must cite — with nothing cut."""
    key, view = load_recorded_view()

    block = work_record_block(view)

    assert key == "OMP-175"
    assert block["work_key"] == "OMP-175"
    assert block["state"] == "DONE"
    assert block["revision"]["title"] == TITLE
    assert tuple(block["revision"]["acceptance_criteria"]) == CRITERIA
    assert [receipt["receipt_id"] for receipt in block["receipts"]] == [
        str(receipt.receipt_id) for receipt in view.receipts
    ]
    assert [receipt["kind"] for receipt in block["receipts"]] == [
        str(receipt.kind) for receipt in view.receipts
    ]
    assert [attempt["attempt_id"] for attempt in block["close_attempts"]] == [
        str(attempt.attempt_id) for attempt in view.close_attempts
    ]
    assert [event["event_type"] for event in block["close_attempt_events"]] == [
        event.event_type for event in view.close_attempt_events
    ]
    assert block["candidate"]["commit_sha"] == view.item.candidate.commit_sha
    assert block["project"]["name"] == view.project.name
    assert set(block["truncated"].values()) == {0}


def test_work_record_block_clips_oversized_evidence_and_names_what_was_cut() -> None:
    """An item with more, longer evidence than the caps allows renders a fixed
    shape: receipts capped, every long text clipped with an explicit marker, and
    the ``truncated`` map naming exactly how many rows were dropped, so the
    generator request cannot grow without bound."""
    _, view = load_recorded_view()
    payload = view.model_dump(mode="json")

    long_report = "x" * 6000
    receipt = dict(payload["receipts"][4])
    receipt["payload"] = {"report": long_report}
    revision = dict(payload["item"]["revision"])
    revision["description"] = long_report
    revision["acceptance_criteria"] = [f"criterion {i}" for i in range(40)]
    item = dict(payload["item"])
    item["revision"] = revision
    oversized = dict(payload)
    oversized["item"] = item
    oversized["receipts"] = [{**receipt, "receipt_id": str(uuid4())} for _ in range(40)]

    block = work_record_block(oversized)

    assert len(block["receipts"]) == 32
    assert block["truncated"]["receipts"] == 8
    assert all(rendered["payload_truncated"] for rendered in block["receipts"])
    assert all(
        rendered["payload_text"].endswith(" ...[truncated]")
        for rendered in block["receipts"]
    )
    assert block["revision"]["description"].endswith(" ...[truncated]")
    assert len(block["revision"]["description"]) == 2000 + len(" ...[truncated]")
    assert len(block["revision"]["acceptance_criteria"]) == 32
    assert tuple(block["revision"]["acceptance_criteria"]) == tuple(
        f"criterion {i}" for i in range(32)
    )
    assert block["truncated"]["acceptance_criteria"] == 8


def test_bounded_work_record_resolves_key_and_reads_workflow() -> None:
    """A bare work_id resolves to the item's alias key and its workflow is read."""
    key, view = load_recorded_view()
    records = RecordedWorkflows({key: view})

    block = bounded_work_record(records, view.item.work_id)

    assert block is not None
    assert block["work_key"] == key
    assert records.workflow_calls == [key]


def test_bounded_work_record_is_none_when_service_refuses() -> None:
    """An unreadable item yields no record instead of raising through capture."""
    _, view = load_recorded_view()

    assert bounded_work_record(UnreadableRecords(), view.item.work_id) is None


def test_resolve_work_key_scan_is_bounded() -> None:
    """An oversized workspace yields no resolution rather than an unbounded walk."""
    key, view = load_recorded_view()
    other_key = "OMP-176"
    records = RecordedWorkflows({key: view, other_key: view}, page_size=1)

    resolved = resolve_work_key(records, uuid4(), scan_limit=1)

    assert resolved is None
    assert len(records.work_items_calls) == 1


def test_drain_traces_finished_item_record_and_keeps_no_lesson_path() -> None:
    """The unit for a real finished item carries that item's own title, criteria,
    receipts and close history in the generator request, and a generator that
    answers no_lesson still settles the unit as no_lesson."""
    key, view = load_recorded_view()
    records = RecordedWorkflows({key: view})
    event = make_event(
        sequence=1,
        aggregate_id=view.item.work_id,
        payload={
            "work_id": str(view.item.work_id),
            "state": "DONE",
            "type": "complete_work",
            "row_version": 3,
        },
    )
    generator = StubGenerator([make_no_lesson()], model="m", profile="p")

    run = drain(
        _store(),
        FakeEvents([event]),
        DictReader(),
        generator,
        workspace_id=WORKSPACE,
        records=records,
    )

    assert run.status == "no_lesson"
    assert run.units_no_lesson == 1
    assert len(generator.calls) == 1
    trace = generator.calls[0]
    assert trace["payload"]["work_id"] == str(view.item.work_id)

    record = trace["work_record"]
    assert record is not None
    assert record["work_key"] == key
    assert record["state"] == "DONE"
    assert record["revision"]["title"] == TITLE
    assert tuple(record["revision"]["acceptance_criteria"]) == CRITERIA
    assert len(record["receipts"]) == len(view.receipts)
    assert record["receipts"][0]["receipt_id"] == str(view.receipts[0].receipt_id)
    assert record["close_attempts"]
    assert record["close_attempt_events"]
    assert records.workflow_calls == [key]


def test_drain_traces_null_record_when_item_is_unreadable() -> None:
    """An unreadable item leaves the trace's record explicitly null and still
    settles the unit — absence of evidence, not a failed capture."""
    _, view = load_recorded_view()
    event = make_event(sequence=1, aggregate_id=view.item.work_id)
    generator = StubGenerator([make_no_lesson()], model="m", profile="p")

    run = drain(
        _store(),
        FakeEvents([event]),
        DictReader(),
        generator,
        workspace_id=WORKSPACE,
        records=UnreadableRecords(),
    )

    assert run.status == "no_lesson"
    assert run.units_failed == 0
    assert generator.calls[0]["work_record"] is None


def test_drain_json_lists_model_profile_and_source_sequence_per_unit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """drain --json names model, profile and the source event sequence for each unit."""
    key, view = load_recorded_view()
    event = make_event(
        sequence=7,
        aggregate_id=view.item.work_id,
        payload={"work_id": str(view.item.work_id), "state": "DONE"},
    )
    generator = StubGenerator(
        [make_no_lesson(model="qwen-3.8", profile="rtx-local")],
        model="qwen-3.8",
        profile="rtx-local",
    )

    code = cli_main(
        _drain_argv(tmp_path),
        store=_store(),
        events=FakeEvents([event]),
        receipts=DictReader(),
        generator=generator,
        records=RecordedWorkflows({key: view}),
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [unit["event_sequence"] for unit in payload["units"]] == [7]
    assert payload["units"][0]["model"] == "qwen-3.8"
    assert payload["units"][0]["profile"] == "rtx-local"
    assert payload["units"][0]["state"] == "no_lesson"


def test_retry_json_lists_model_profile_and_source_sequence_per_unit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """retry --json names model, profile and the source event sequence for every
    requeued unit, recovered from the queued row's own source record."""
    key, view = load_recorded_view()
    event = make_event(
        sequence=11,
        aggregate_id=view.item.work_id,
        payload={"work_id": str(view.item.work_id), "state": "DONE"},
    )
    store = _store()
    failing = StubGenerator([GeneratorUnavailable("down")], model="m", profile="p")
    failed = drain(
        store,
        FakeEvents([event]),
        DictReader(),
        failing,
        workspace_id=WORKSPACE,
        records=RecordedWorkflows({key: view}),
    )
    assert failed.status == "failed"

    recovered = StubGenerator(
        [make_no_lesson(model="m2", profile="p2")], model="m2", profile="p2"
    )
    code = cli_main(
        _retry_argv(tmp_path),
        store=store,
        events=FakeEvents([]),
        receipts=DictReader(),
        generator=recovered,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [unit["event_sequence"] for unit in payload["units"]] == [11]
    assert payload["units"][0]["model"] == "m2"
    assert payload["units"][0]["profile"] == "p2"


def _drain_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "drain",
        "--state-dir",
        str(tmp_path / "state"),
        "--work-url",
        "http://127.0.0.1:54322",
        "--workspace",
        WORKSPACE,
        "--bearer-file",
        str(tmp_path / "bearer.json"),
        "--generator-url",
        "http://127.0.0.1:18080",
        "--model",
        "m",
        "--profile",
        "p",
        "--json",
        *extra,
    ]


def _retry_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "retry",
        "--state-dir",
        str(tmp_path / "state"),
        "--work-url",
        "http://127.0.0.1:54322",
        "--workspace",
        WORKSPACE,
        "--bearer-file",
        str(tmp_path / "bearer.json"),
        "--generator-url",
        "http://127.0.0.1:18080",
        "--model",
        "m",
        "--profile",
        "p",
        "--json",
        *extra,
    ]


def _store() -> LearningStore:
    return LearningStore(":memory:")


class DictReader:
    """Empty native receipt reader: the no_lesson path never cites a receipt."""

    def receipt(self, receipt_id: UUID | str) -> Any:
        raise KeyError(f"unexpected receipt read for {receipt_id}")
