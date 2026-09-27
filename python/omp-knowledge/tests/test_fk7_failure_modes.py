from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.canonical import sha256
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.context.compiler import compile_bundle
from omp_knowledge.context.models import CompileRequest, StageIdentity
from omp_knowledge.context.sources import procedural_items
from omp_knowledge.context.store import ContextBundleStore
from omp_knowledge.learning.capture import drain, retry
from omp_knowledge.learning.cleanup import (
    CleanupTarget,
    NativeCommittedTarget,
    drain_cleanup,
)
from omp_knowledge.learning.corrections import correct
from omp_knowledge.learning.generation import (
    GenerationResult,
    GeneratorUnavailable,
    MalformedGeneratorOutput,
)
from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import supply
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator


class DictReceiptReader:
    """In-memory receipt reader conforming to NativeReceipts."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for key, value in receipts.items():
                self._receipts[str(key)] = value

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


class WordCounter:
    """Deterministic in-memory token counter matching tests/test_context_store.py."""

    profile = "test-word-counter-v1"

    def count(self, texts: list[str]) -> list[int]:
        return [len(text.split()) for text in texts]


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=receipt_id or uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=kind,
        payload={"exit_code": 0 if verdict == "PASS" else 1},
        payload_sha256="0" * 64,
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def make_lesson(title: str, receipt_id: UUID) -> Lesson:
    return Lesson(
        title=title,
        steps=("stop the worker", "clear the lease"),
        claims=(Claim(text="verified resolution", receipt_ids=(receipt_id,)),),
    )


def make_result(
    *lessons: Lesson,
    model: str = "stub-model",
    profile: str = "stub-profile",
) -> GenerationResult:
    return GenerationResult(
        model=model,
        profile=profile,
        request_sha256="0" * 64,
        response_sha256="0" * 64,
        lessons=lessons,
    )


def make_attribution(
    *,
    workspace_id: str,
    sequence: int = 1,
    model: str = "stub-model",
    profile: str = "stub-profile",
) -> Attribution:
    return Attribution(
        model=model,
        profile=profile,
        source=SourceIdentity(
            workspace_id=workspace_id,
            event_id=str(uuid4()),
            event_sequence=sequence,
            aggregate_id=str(uuid4()),
        ),
    )


def test_generator_outage(tmp_path: Path) -> None:
    """1. Generator outage: StubGenerator raising GeneratorUnavailable marks the unit

    failed and retryable with its error_code; retrying with a healthy generator succeeds
    and creates the proposal once.
    """
    store = LearningStore(tmp_path / "learning.sqlite")
    workspace_id = str(uuid4())
    receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    events = FakeEvents([event])

    outage_generator = StubGenerator(
        [GeneratorUnavailable("llm service unavailable")],
        model="outage-model",
        profile="outage-profile",
    )

    run = drain(store, events, reader, outage_generator, workspace_id=workspace_id)

    assert run.status == "failed"
    assert run.units_failed == 1
    assert len(run.units) == 1
    unit = run.units[0]
    assert unit.state == "failed"
    assert unit.retryable is True
    assert unit.error_code == "generator_unavailable"

    unit_row = store.execute(
        "SELECT state, retryable, error_code FROM units WHERE unit_id = ?",
        (unit.unit_id,),
    ).fetchone()
    assert unit_row is not None
    assert unit_row["state"] == "failed"
    assert unit_row["retryable"] == 1
    assert unit_row["error_code"] == "generator_unavailable"
    assert store.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"] == 0

    healthy_lesson = make_lesson("Recovered procedure", receipt.receipt_id)
    healthy_generator = StubGenerator(
        [make_result(healthy_lesson, model="healthy-model", profile="healthy-profile")],
        model="healthy-model",
        profile="healthy-profile",
    )

    retry_run = retry(store, reader, healthy_generator)

    assert retry_run.status == "succeeded"
    assert retry_run.units_failed == 0
    assert retry_run.proposals_accepted == 1

    unit_row_after = store.execute(
        "SELECT state, retryable, error_code FROM units WHERE unit_id = ?",
        (unit.unit_id,),
    ).fetchone()
    assert unit_row_after is not None
    assert unit_row_after["state"] == "succeeded"
    assert unit_row_after["retryable"] == 0
    assert unit_row_after["error_code"] is None

    proposals = store.execute("SELECT proposal_id, status FROM proposals").fetchall()
    assert len(proposals) == 1
    assert proposals[0]["status"] == "accepted"

    # Retrying again adds no additional proposals
    second_retry = retry(store, reader, healthy_generator)
    assert second_retry.units_total == 0
    assert store.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"] == 1


def test_malformed_output(tmp_path: Path) -> None:
    """2. Malformed output: MalformedGeneratorOutput fails with its error_code

    and leaves no proposal row in the store.
    """
    store = LearningStore(tmp_path / "learning.sqlite")
    workspace_id = str(uuid4())
    receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    events = FakeEvents([event])

    malformed_generator = StubGenerator(
        [MalformedGeneratorOutput("truncated response envelope")],
        model="malformed-model",
        profile="malformed-profile",
    )

    run = drain(store, events, reader, malformed_generator, workspace_id=workspace_id)

    assert run.status == "failed"
    assert run.units_failed == 1
    assert len(run.units) == 1
    unit = run.units[0]
    assert unit.state == "failed"
    assert unit.error_code == "malformed_generator_output"

    unit_row = store.execute(
        "SELECT state, retryable, error_code FROM units WHERE unit_id = ?",
        (unit.unit_id,),
    ).fetchone()
    assert unit_row is not None
    assert unit_row["state"] == "failed"
    assert unit_row["error_code"] == "malformed_generator_output"
    assert store.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"] == 0


def test_crash_between_store_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3. Crash between store writes: monkeypatching corrections._enqueue_cleanup to raise

    causes correct(narrow) to raise and roll back atomically, leaving no procedure v2,
    no corrections row, no cleanup row, and current_version unchanged.
    """
    store = LearningStore(tmp_path / "learning.sqlite")
    workspace_id = str(uuid4())
    pass_receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({pass_receipt.receipt_id: pass_receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    lesson = make_lesson("Procedure to narrow", pass_receipt.receipt_id)
    generator = StubGenerator([make_result(lesson)], model="seed-model", profile="seed-profile")

    # Step 1: establish active procedure v1 via native capture
    init_run = drain(store, FakeEvents([event]), reader, generator, workspace_id=workspace_id)
    assert init_run.proposals_accepted == 1

    proc = store.execute("SELECT procedure_id, current_version, status FROM procedures").fetchone()
    assert proc is not None
    procedure_id = str(proc["procedure_id"])
    assert proc["current_version"] == 1
    assert proc["status"] == "active"
    assert store.execute(
        "SELECT COUNT(*) AS c FROM procedure_versions WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()["c"] == 1

    # Step 2: add counterevidence receipt
    fail_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader.add(fail_receipt)

    # Step 3: monkeypatch corrections._enqueue_cleanup to raise (simulating process crash between writes)
    def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash between store writes")

    monkeypatch.setattr("omp_knowledge.learning.corrections._enqueue_cleanup", _crash)

    attribution = make_attribution(workspace_id=workspace_id, sequence=2)
    precondition = Precondition(key="repository", op="ne", value="sandbox")

    with pytest.raises(RuntimeError, match="simulated crash between store writes"):
        correct(
            store,
            reader,
            procedure_id=procedure_id,
            receipt_id=fail_receipt.receipt_id,
            action="narrow",
            preconditions=(precondition,),
            attribution=attribution,
        )

    # Step 4: verify atomic rollback
    # No procedure v2 exists
    assert store.execute(
        "SELECT COUNT(*) AS c FROM procedure_versions WHERE procedure_id = ? AND version = 2",
        (procedure_id,),
    ).fetchone()["c"] == 0
    # No corrections row was committed
    assert store.execute(
        "SELECT COUNT(*) AS c FROM corrections WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()["c"] == 0
    # No cleanup queue row was committed
    assert store.execute(
        "SELECT COUNT(*) AS c FROM cleanup_queue WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()["c"] == 0
    # current_version is unchanged
    proc_after = store.execute(
        "SELECT current_version, status FROM procedures WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()
    assert proc_after is not None
    assert proc_after["current_version"] == 1
    assert proc_after["status"] == "active"


def test_stale_cache(tmp_path: Path) -> None:
    """4. Stale cache: compile a context bundle while a procedure is current; withdraw it via

    correct; recompile -> the procedure is excluded as 'withdrawn' and the bundle id differs;
    the earlier bundle still reproduces byte-identical.
    """
    db_path = tmp_path / "learning.sqlite"
    store = LearningStore(db_path)
    workspace_id = str(uuid4())
    pass_receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({pass_receipt.receipt_id: pass_receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    lesson = make_lesson("Procedure for context cache", pass_receipt.receipt_id)
    generator = StubGenerator([make_result(lesson)], model="m", profile="p")

    # Step 1: establish active procedure v1 via capture
    drain(store, FakeEvents([event]), reader, generator, workspace_id=workspace_id)
    proc = store.execute("SELECT procedure_id, current_version, status FROM procedures").fetchone()
    assert proc is not None
    procedure_id = str(proc["procedure_id"])
    assert proc["status"] == "active"

    # Step 2: compile context bundle while procedure is current
    context = {"repository": "org/repo"}
    items_v1 = procedural_items(db_path, context)
    assert len(items_v1) == 1
    assert items_v1[0].status == "current"
    proc_ref = f"{procedure_id} @v1"
    assert items_v1[0].ref == proc_ref

    counter = WordCounter()
    identity = StageIdentity(
        work_id=str(uuid4()),
        work_key="OMP-312",
        revision_id=str(uuid4()),
        stage="implement",
        attempt_id=str(uuid4()),
        candidate_id=str(uuid4()),
    )
    request1 = CompileRequest(
        identity=identity,
        token_budget=10_000,
        encoding="ClaudeV5",
        items=items_v1,
    )
    bundle_store = ContextBundleStore(tmp_path / "bundle_store")
    compiled1 = compile_bundle(request1, counter)
    bundle_id1 = bundle_store.persist(request1, compiled1)

    assert any(item.ref == proc_ref for item in compiled1.included)
    assert len(compiled1.exclusions) == 0

    # Step 3: withdraw procedure via correct
    fail_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader.add(fail_receipt)
    attribution = make_attribution(workspace_id=workspace_id, sequence=2)
    record = correct(
        store,
        reader,
        procedure_id=procedure_id,
        receipt_id=fail_receipt.receipt_id,
        action="withdraw",
        attribution=attribution,
    )
    assert record.status == "accepted"

    # Step 4: recompile context bundle -> procedure is excluded as 'withdrawn' and bundle id differs
    items_v2 = procedural_items(db_path, context)
    assert len(items_v2) == 1
    assert items_v2[0].status == "withdrawn"
    assert items_v2[0].ref == proc_ref

    request2 = CompileRequest(
        identity=identity,
        token_budget=10_000,
        encoding="ClaudeV5",
        items=items_v2,
    )
    compiled2 = compile_bundle(request2, counter)
    bundle_id2 = bundle_store.persist(request2, compiled2)

    assert bundle_id2 != bundle_id1
    assert not any(item.ref == proc_ref for item in compiled2.included)
    assert len(compiled2.exclusions) == 1
    assert compiled2.exclusions[0].ref == proc_ref
    assert compiled2.exclusions[0].reason == "withdrawn"

    # Step 5: earlier bundle still reproduces byte-identical
    reproduced_text = bundle_store.reproduce(bundle_id1)
    assert reproduced_text == compiled1.text
    assert sha256(reproduced_text) == compiled1.sha256


def test_restart_replay(tmp_path: Path) -> None:
    """5. Restart/replay: drain(..., lease_seconds=0) with a generator raising a non-GeneratorError

    propagates and leaves the unit running; close and reopen the store; the next drain
    marks it failed 'dropped' (retryable); retry succeeds; a further drain over the same
    events adds no units or proposals.
    """
    db_path = tmp_path / "learning.sqlite"
    store = LearningStore(db_path)
    workspace_id = str(uuid4())
    receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({receipt.receipt_id: receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    events = FakeEvents([event])

    crashing_generator = StubGenerator(
        [RuntimeError("simulated unhandled process crash")],
        model="crash-model",
        profile="crash-profile",
    )

    # drain propagates non-GeneratorError and leaves unit running
    with pytest.raises(RuntimeError, match="simulated unhandled process crash"):
        drain(
            store,
            events,
            reader,
            crashing_generator,
            workspace_id=workspace_id,
            lease_seconds=0,
        )

    unit_row = store.execute("SELECT state, attempts, error_code, lease_until FROM units").fetchone()
    assert unit_row is not None
    assert unit_row["state"] == "running"
    assert unit_row["attempts"] == 1
    assert unit_row["error_code"] is None

    # Close and reopen store
    store.close()
    reopened = LearningStore(db_path)

    unit_reopened = reopened.execute("SELECT state FROM units").fetchone()
    assert unit_reopened is not None
    assert unit_reopened["state"] == "running"

    # Next drain marks the expired running unit failed 'dropped' (retryable)
    noop_generator = StubGenerator(
        [
            GenerationResult(
                model="noop",
                profile="noop",
                request_sha256="0" * 64,
                response_sha256="0" * 64,
                no_lesson_reason="noop",
            )
        ],
        model="noop",
        profile="noop",
    )
    drain_run2 = drain(reopened, events, reader, noop_generator, workspace_id=workspace_id)

    assert drain_run2.status == "failed"
    assert drain_run2.units_failed == 1
    assert len(drain_run2.units) == 1
    assert drain_run2.units[0].state == "failed"
    assert drain_run2.units[0].error_code == "dropped"
    assert drain_run2.units[0].retryable is True

    unit_dropped = reopened.execute("SELECT state, retryable, error_code FROM units").fetchone()
    assert unit_dropped is not None
    assert unit_dropped["state"] == "failed"
    assert unit_dropped["retryable"] == 1
    assert unit_dropped["error_code"] == "dropped"

    # retry succeeds
    healthy_lesson = make_lesson("Recovered post-crash", receipt.receipt_id)
    healthy_generator = StubGenerator(
        [make_result(healthy_lesson, model="recovery-model", profile="recovery-profile")],
        model="recovery-model",
        profile="recovery-profile",
    )
    retry_run = retry(reopened, reader, healthy_generator)

    assert retry_run.status == "succeeded"
    assert retry_run.units_failed == 0
    assert retry_run.proposals_accepted == 1

    unit_settled = reopened.execute("SELECT state, retryable, error_code FROM units").fetchone()
    assert unit_settled is not None
    assert unit_settled["state"] == "succeeded"
    assert unit_settled["retryable"] == 0
    assert unit_settled["error_code"] is None
    assert reopened.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"] == 1

    # A further drain over the same events adds no units or proposals
    drain_run3 = drain(reopened, events, reader, healthy_generator, workspace_id=workspace_id)
    assert drain_run3.units_total == 0
    assert drain_run3.proposals_accepted == 0
    assert drain_run3.proposals_rejected == 0
    assert reopened.execute("SELECT COUNT(*) AS c FROM units").fetchone()["c"] == 1
    assert reopened.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"] == 1


def test_cleanup_failure_and_retry(tmp_path: Path) -> None:
    """6. Cleanup failure and retry: after an accepted correction, drain_cleanup with a target

    that raises leaves the row pending (attempts=1, last_error set) and the procedure
    change still visible to supply; a second drain_cleanup with NativeCommittedTarget
    marks it done (attempts=2).
    """
    store = LearningStore(tmp_path / "learning.sqlite")
    workspace_id = str(uuid4())
    pass_receipt = make_receipt(verdict="PASS")
    reader = DictReceiptReader({pass_receipt.receipt_id: pass_receipt})
    event = make_event(sequence=1, event_type="complete_work", workspace_id=UUID(workspace_id))
    lesson = make_lesson("Procedure to narrow and clean", pass_receipt.receipt_id)
    generator = StubGenerator([make_result(lesson)], model="seed-model", profile="seed-profile")

    # Step 1: establish active procedure v1 via capture
    drain(store, FakeEvents([event]), reader, generator, workspace_id=workspace_id)
    proc = store.execute("SELECT procedure_id, current_version, status FROM procedures").fetchone()
    assert proc is not None
    procedure_id = str(proc["procedure_id"])
    assert proc["current_version"] == 1

    # Supply sees v1 initially
    initial_supply = supply(store, workspace_id=workspace_id, work_key="key-1", context={"repository": "repo-a"})
    assert len(initial_supply) == 1
    assert f"PROCEDURE {procedure_id}@v1" in initial_supply[0]

    # Step 2: accepted narrow correction
    fail_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader.add(fail_receipt)
    attribution = make_attribution(workspace_id=workspace_id, sequence=2)
    precondition = Precondition(key="repository", op="eq", value="repo-a")
    correction = correct(
        store,
        reader,
        procedure_id=procedure_id,
        receipt_id=fail_receipt.receipt_id,
        action="narrow",
        preconditions=(precondition,),
        attribution=attribution,
    )
    assert correction.status == "accepted"
    assert correction.to_version == 2

    # Step 3: drain_cleanup with a target that raises leaves row pending (attempts=1, last_error set)
    class FailingTarget(CleanupTarget):
        def clean(self, proc_id: str, action: str) -> None:
            raise RuntimeError("transient external cleanup service failure")

    run1 = drain_cleanup(store, FailingTarget(), limit=10)
    assert run1.processed == 1
    assert run1.succeeded == 0
    assert run1.failed == 1
    assert len(run1.items) == 1
    item1 = run1.items[0]
    assert item1.procedure_id == procedure_id
    assert item1.action == "narrow"
    assert item1.attempts == 1
    assert item1.done is False
    assert item1.last_error == "RuntimeError: transient external cleanup service failure"

    row1 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()
    assert row1 is not None
    assert row1["attempts"] == 1
    assert row1["done_at"] is None
    assert row1["last_error"] == "RuntimeError: transient external cleanup service failure"

    # Procedure change is still visible to supply even while cleanup is pending
    supply_matching = supply(store, workspace_id=workspace_id, work_key="key-2", context={"repository": "repo-a"})
    assert len(supply_matching) == 1
    assert f"PROCEDURE {procedure_id}@v2" in supply_matching[0]

    supply_non_matching = supply(store, workspace_id=workspace_id, work_key="key-3", context={"repository": "repo-b"})
    assert len(supply_non_matching) == 0

    # Step 4: a second drain_cleanup with NativeCommittedTarget marks it done (attempts=2)
    native_target = NativeCommittedTarget(store)
    run2 = drain_cleanup(store, native_target, limit=10)
    assert run2.processed == 1
    assert run2.succeeded == 1
    assert run2.failed == 0
    assert len(run2.items) == 1
    item2 = run2.items[0]
    assert item2.procedure_id == procedure_id
    assert item2.action == "narrow"
    assert item2.attempts == 2
    assert item2.done is True
    assert item2.last_error is None

    row2 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = ?",
        (procedure_id,),
    ).fetchone()
    assert row2 is not None
    assert row2["attempts"] == 2
    assert row2["done_at"] is not None
    assert row2["last_error"] is None

    # Step 5: completed cleanup rows are not reprocessed
    run3 = drain_cleanup(store, native_target, limit=10)
    assert run3.processed == 0
