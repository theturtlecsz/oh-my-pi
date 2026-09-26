from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import (
    NativeEvents,
    drain,
    retry,
)
from omp_knowledge.learning.cli import main as cli_main
from omp_knowledge.learning.generation import (
    GenerationResult,
    GeneratorUnavailable,
    MalformedGeneratorOutput,
)
from omp_knowledge.learning.models import Claim, Lesson
from omp_knowledge.learning.store import LearningStore, compute_unit_id

WORKSPACE = "11111111-1111-1111-1111-111111111111"
HEX = "0" * 64


class DictReceiptReader:
    """Dict-backed fake reader conforming to the NativeReceipts protocol."""

    def __init__(self, receipts: dict[str, EvidenceReceipt] | None = None) -> None:
        self._receipts = {str(k): v for k, v in (receipts or {}).items()}

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(*, verdict: str | None = "PASS") -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
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


def make_lesson(title: str, receipt_id: UUID) -> Lesson:
    return Lesson(
        title=title,
        steps=("stop the worker", "clear the lease"),
        claims=(Claim(text="lease outlived the crash", receipt_ids=(receipt_id,)),),
    )


def make_result(
    *lessons: Lesson, model: str = "qwen-2.5", profile: str = "local-qwen"
) -> GenerationResult:
    return GenerationResult(
        model=model,
        profile=profile,
        request_sha256=HEX,
        response_sha256=HEX,
        lessons=lessons,
    )


def make_no_lesson(
    reason: str = "no transferable technique",
    *,
    model: str = "qwen-2.5",
    profile: str = "local-qwen",
) -> GenerationResult:
    return GenerationResult(
        model=model,
        profile=profile,
        request_sha256=HEX,
        response_sha256=HEX,
        no_lesson_reason=reason,
    )


def test_work_client_fits_native_events_protocol() -> None:
    """The read-only client satisfies the NativeEvents seam structurally."""
    from omp_work.v1.client import WorkClient

    assert issubclass(WorkClient, NativeEvents)


def test_drain_queues_one_unit_per_matching_event_with_full_attribution() -> None:
    """One unit per matching event, one proposal per lesson, run succeeded;
    every unit and proposal row carries model, profile, and source."""
    store = LearningStore(":memory:")
    receipt = make_receipt()
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    matching = make_event(sequence=1, event_type="complete_work")
    other = make_event(sequence=2, event_type="append_evidence")
    events = FakeEvents([matching, other])
    generator = StubGenerator(
        [
            make_result(
                make_lesson("Lesson A", receipt.receipt_id),
                make_lesson("Lesson B", receipt.receipt_id),
                model="qwen-3.8",
                profile="rtx-local",
            )
        ],
        model="qwen-3.8",
        profile="rtx-local",
    )

    run = drain(store, events, reader, generator, workspace_id=WORKSPACE)

    assert run.status == "succeeded"
    assert run.units_total == 1
    assert run.units_failed == 0
    assert run.proposals_accepted == 2
    assert run.proposals_rejected == 0

    unit_id = compute_unit_id(WORKSPACE, matching.event_id)
    assert [u.unit_id for u in run.units] == [unit_id]
    assert run.units[0].proposal_ids and len(run.units[0].proposal_ids) == 2

    unit_row = store.execute(
        "SELECT * FROM units WHERE unit_id = ?", (unit_id,)
    ).fetchone()
    assert unit_row["model"] == "qwen-3.8"
    assert unit_row["profile"] == "rtx-local"
    assert unit_row["source_json"]
    assert f'"{matching.event_id}"' in unit_row["source_json"]

    # The trace sent to the generator carries the event payload plus actor and event ids.
    assert len(generator.calls) == 1
    trace = generator.calls[0]
    assert trace["event_id"] == str(matching.event_id)
    assert trace["event_sequence"] == 1
    assert trace["actor_id"] == str(matching.actor_id)
    assert trace["actor_kind"] == matching.actor_kind
    assert trace["payload"] == matching.payload

    proposals = store.execute("SELECT * FROM proposals ORDER BY created_at").fetchall()
    assert len(proposals) == 2
    for row in proposals:
        assert row["model"] == "qwen-3.8"
        assert row["profile"] == "rtx-local"
        assert row["source_json"]
        assert row["status"] == "accepted"

    run_row = store.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert run_row["status"] == "succeeded"
    assert run_row["proposals_accepted"] == 2
    assert run_row["units_total"] == 1


def test_second_drain_on_same_events_adds_no_units() -> None:
    """The cursor advance plus INSERT OR IGNORE make a replay a no-op."""
    store = LearningStore(":memory:")
    receipt = make_receipt()
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    events = FakeEvents([make_event(sequence=1), make_event(sequence=2)])
    generator = StubGenerator(
        [make_result(make_lesson("Lesson", receipt.receipt_id))], model="m", profile="p"
    )

    first = drain(store, events, reader, generator, workspace_id=WORKSPACE)
    assert first.units_total == 2
    units_after_first = store.execute("SELECT COUNT(*) AS n FROM units").fetchone()["n"]

    second = drain(store, events, reader, generator, workspace_id=WORKSPACE)

    assert second.units_total == 0
    assert (
        store.execute("SELECT COUNT(*) AS n FROM units").fetchone()["n"]
        == units_after_first
    )
    assert store.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"] == 2
    # The second drain never resends an already-settled unit to the generator.
    assert len(generator.calls) == 2


def test_drain_pages_at_most_limit_events() -> None:
    """A bounded drain pages to the watermark, resuming from the cursor."""
    store = LearningStore(":memory:")
    receipt = make_receipt()
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    events = FakeEvents([make_event(sequence=seq) for seq in (1, 2, 3, 4, 5)])
    generator = StubGenerator([make_no_lesson()], model="m", profile="p")

    first = drain(store, events, reader, generator, workspace_id=WORKSPACE, limit=2)
    assert first.units_total == 2
    assert (
        store.execute("SELECT last_sequence AS s FROM capture_cursor").fetchone()["s"]
        == 2
    )

    second = drain(store, events, reader, generator, workspace_id=WORKSPACE, limit=2)
    assert second.units_total == 2
    assert (
        store.execute("SELECT last_sequence AS s FROM capture_cursor").fetchone()["s"]
        == 4
    )


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (GeneratorUnavailable("local generator down"), "generator_unavailable"),
        (MalformedGeneratorOutput("empty envelope"), "malformed_generator_output"),
    ],
)
def test_outage_and_empty_output_are_failed_retryable(
    failure: Exception, error_code: str
) -> None:
    """An unreachable generator and an unusable/empty envelope both surface as
    failed, retryable units, and the run is never succeeded."""
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})
    event = make_event(sequence=1)
    generator = StubGenerator([failure], model="m", profile="p")

    run = drain(store, FakeEvents([event]), reader, generator, workspace_id=WORKSPACE)

    assert run.status == "failed"
    assert run.units_failed == 1
    assert run.proposals_accepted == 0

    unit_row = store.execute("SELECT * FROM units").fetchone()
    assert unit_row["state"] == "failed"
    assert unit_row["retryable"] == 1
    assert unit_row["error_code"] == error_code
    assert unit_row["model"] == "m"
    assert unit_row["profile"] == "p"
    assert unit_row["source_json"]


def test_no_lesson_output_yields_no_lesson_not_succeeded() -> None:
    """A generator that reports a no_lesson_reason settles the unit as
    no_lesson, and the run is no_lesson — never succeeded at 0 proposals."""
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})
    generator = StubGenerator([make_no_lesson()], model="m", profile="p")

    run = drain(
        store,
        FakeEvents([make_event(sequence=1)]),
        reader,
        generator,
        workspace_id=WORKSPACE,
    )

    assert run.status == "no_lesson"
    assert run.units_no_lesson == 1
    assert run.proposals_accepted == 0
    unit_row = store.execute("SELECT state, error_code FROM units").fetchone()
    assert unit_row["state"] == "no_lesson"
    assert unit_row["error_code"] is None


def test_rejected_proposal_counts_as_a_proposal_for_status() -> None:
    """A lesson rejected by native acceptance still produces a proposal row, so a
    run with one rejected proposal and no failures is succeeded (>=1 proposal)
    while its accepted count stays 0."""
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})
    lesson = Lesson(
        title="Unsupported",
        steps=("step 1",),
        claims=(Claim(text="no citation", receipt_ids=()),),
    )
    generator = StubGenerator([make_result(lesson)], model="m", profile="p")

    run = drain(
        store,
        FakeEvents([make_event(sequence=1)]),
        reader,
        generator,
        workspace_id=WORKSPACE,
    )

    assert run.proposals_accepted == 0
    assert run.proposals_rejected == 1
    assert run.units_failed == 0
    assert run.status == "succeeded"
    proposal = store.execute("SELECT status FROM proposals").fetchone()
    assert proposal["status"] == "rejected"


def test_partial_status_when_some_units_fail() -> None:
    """Some units failing while others settle makes the run partial, not failed."""
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})
    events = FakeEvents([make_event(sequence=1), make_event(sequence=2)])
    generator = StubGenerator(
        [
            GenerationResult(
                model="m",
                profile="p",
                request_sha256=HEX,
                response_sha256=HEX,
                no_lesson_reason="nothing learned",
            ),
            GeneratorUnavailable("down"),
        ],
        model="m",
        profile="p",
    )

    run = drain(store, events, reader, generator, workspace_id=WORKSPACE)

    assert run.status == "partial"
    assert run.units_total == 2
    assert run.units_failed == 1
    assert run.units_no_lesson == 1


def test_expired_running_unit_becomes_dropped() -> None:
    """A running unit whose lease has elapsed is failed, retryable, error_code dropped."""
    store = LearningStore(":memory:")
    reader = DictReceiptReader({})
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    unit_id = compute_unit_id(WORKSPACE, uuid4())
    store.execute(
        """
        INSERT INTO units (
            unit_id, workspace_id, event_id, state, attempts, retryable, lease_until,
            trace_json, model, profile, source_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'running', 1, 0, ?, '{}', 'm', 'p', '{}', ?, ?)
        """,
        (unit_id, WORKSPACE, str(uuid4()), expired, expired, expired),
    )
    generator = StubGenerator([make_no_lesson()], model="m", profile="p")

    run = drain(store, FakeEvents([]), reader, generator, workspace_id=WORKSPACE)

    row = store.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
    assert row["state"] == "failed"
    assert row["retryable"] == 1
    assert row["error_code"] == "dropped"
    assert run.units_failed == 1
    assert run.status == "failed"
    assert [u.error_code for u in run.units] == ["dropped"]
    # No generator work was dispatched for the dropped unit.
    assert generator.calls == []


def test_retry_after_recovery_creates_proposals() -> None:
    """Failed retryable units requeue and, once the generator recovers, produce
    attributed proposals."""
    store = LearningStore(":memory:")
    receipt = make_receipt()
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    event = make_event(sequence=1)
    failing = StubGenerator([GeneratorUnavailable("down")], model="m", profile="p")

    failed_run = drain(
        store, FakeEvents([event]), reader, failing, workspace_id=WORKSPACE
    )
    assert failed_run.status == "failed"

    recovered = StubGenerator(
        [
            make_result(
                make_lesson("Recovered", receipt.receipt_id), model="m2", profile="p2"
            )
        ],
        model="m2",
        profile="p2",
    )
    retried = retry(store, reader, recovered)

    assert retried.status == "succeeded"
    assert retried.proposals_accepted == 1
    assert retried.units_failed == 0

    proposal = store.execute("SELECT * FROM proposals").fetchone()
    assert proposal["status"] == "accepted"
    assert proposal["model"] == "m2"
    assert proposal["profile"] == "p2"
    assert proposal["source_json"]

    unit_row = store.execute("SELECT * FROM units").fetchone()
    assert unit_row["state"] == "succeeded"
    assert unit_row["model"] == "m2"
    assert unit_row["profile"] == "p2"


def test_cli_main_exits_nonzero_on_failed_run(tmp_path) -> None:
    """An injected failing run exits 1; a no_lesson run exits 0."""
    argv = [
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
    ]

    failing_store = LearningStore(":memory:")
    failing_reader = DictReceiptReader({})
    failing = StubGenerator([GeneratorUnavailable("down")], model="m", profile="p")
    failed_code = cli_main(
        argv,
        store=failing_store,
        events=FakeEvents([make_event(sequence=1)]),
        receipts=failing_reader,
        generator=failing,
    )
    assert failed_code == 1

    ok_store = LearningStore(":memory:")
    ok = StubGenerator([make_no_lesson()], model="m", profile="p")
    ok_code = cli_main(
        argv,
        store=ok_store,
        events=FakeEvents([make_event(sequence=1)]),
        receipts=DictReceiptReader({}),
        generator=ok,
    )
    assert ok_code == 0
