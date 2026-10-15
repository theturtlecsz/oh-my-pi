"""Suspended lessons drop out of supply within one service cycle and stay out.

Serves OMP-607 r4: a lesson whose cited receipt is superseded, revoked, or
invalidated moves to suspended within one orchestrator cycle and is no longer
supplied. Exclusion holds after a service restart. Injection uses readers, not
a client or credentials. A transport failure surfaces and leaves the lesson
active.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn

from omp_work.orchestrator.service import (
    OrchestratorConfig,
    OrchestratorError,
    OrchestratorHandler,
)
from omp_work.v1.api_models import (
    DomainEventsPage,
    DomainEventView,
    WorkflowView,
    WorkItemsPage,
    WorkItemSummary,
)
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import Candidate, EvidenceKind, EvidenceReceipt, WorkAlias, WorkRevision
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkError
from test_workflow_service import (
    _command,
    _create,
    _grant,
    _owner_headers,
    _plan,
    _receipt,
)

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)

HEX = "a" * 64
OTHER_SHA = "b" * 64
CANDIDATE_SHA = "c" * 64
COMMIT = "d" * 40


@pytest.fixture
def knowledge_path(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "omp-knowledge" / "src"
    monkeypatch.syspath_prepend(str(root))
    current = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(root) if not current else os.pathsep.join((str(root), current)),
    )
    return root


@contextmanager
def _serving(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            end = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < end
                time.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


class _WordCounter:
    profile = "test-words-v1"

    def count(self, texts):
        return [len(text.split()) for text in texts]


class _Events:
    def __init__(self, events):
        self._events = list(events)

    def events(self, after_sequence, limit):
        selected = [event for event in self._events if event.sequence > after_sequence][:limit]
        watermark = max((event.sequence for event in self._events), default=0)
        next_after = selected[-1].sequence if selected else after_sequence
        return DomainEventsPage(
            events=tuple(selected),
            watermark_sequence=watermark,
            next_after_sequence=next_after,
            has_more=False,
        )


class _Generator:
    def __init__(self, lesson):
        from omp_knowledge.learning.generation import GenerationResult

        self.model = "m"
        self.profile = "p"
        self.calls = []
        self._result = GenerationResult(
            model="m",
            profile="p",
            request_sha256=HEX,
            response_sha256=HEX,
            lessons=(lesson,),
        )

    def generate(self, trace):
        self.calls.append(trace)
        return self._result


class _Receipts:
    def __init__(self, receipts):
        self._receipts = {str(key): value for key, value in receipts.items()}

    def receipt(self, receipt_id):
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(key)
        return self._receipts[key]


class _Records:
    def __init__(self, view):
        self.view = view
        self.calls = []

    def work_items(self, *, after=None, limit=None):
        del after, limit
        self.calls.append("work_items")
        item = self.view.item
        return WorkItemsPage(
            items=(
                WorkItemSummary(
                    work_id=item.work_id,
                    key=item.alias.key,
                    state=item.state,
                    created_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
                ),
            )
        )

    def workflow(self, key):
        self.calls.append("workflow")
        if key != self.view.item.alias.key:
            raise KeyError(key)
        return self.view


def _event(work_id, workspace_id):
    return DomainEventView(
        event_id=uuid4(),
        sequence=1,
        workspace_id=workspace_id,
        aggregate_type="work_item",
        aggregate_id=work_id,
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="owner",
        capability_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        operation_id=uuid4(),
        causation_id=uuid4(),
        event_type="complete_work",
        outcome="applied",
        payload={"work_id": str(work_id), "state": "DONE"},
        payload_sha256=HEX,
        previous_event_sha256=None,
        event_sha256=HEX,
        occurred_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )


def _receipt_for(text, *, candidate_sha256=None, candidate_commit=None, verdict="PASS"):
    payload = {"summary": text}
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.VERIFICATION,
        payload=payload,
        payload_sha256=HEX,
        issuer="test-runner",
        issued_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        candidate_sha256=candidate_sha256,
        candidate_commit=candidate_commit,
        verdict=verdict,
        independent=True,
    )


def _view_for(receipt, *, candidate_sha256=CANDIDATE_SHA, commit_sha=COMMIT, candidate_id=None):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    chosen = candidate_id or receipt.candidate_id
    return WorkflowView(
        item={
            "work_id": receipt.work_id,
            "workspace_id": uuid4(),
            "alias": WorkAlias(
                work_id=receipt.work_id, key="OMP-607", primary=True, origin="local"
            ),
            "state": "EXECUTING",
            "revision": WorkRevision(
                revision_id=receipt.revision_id,
                work_id=receipt.work_id,
                revision_number=1,
                title="Suspend lessons",
                description="support must stay fresh",
                scope="python/omp-knowledge",
                acceptance_criteria=("cited receipt stays fresh",),
                content_sha256=HEX,
                created_by="tester",
                created_at=now,
            ),
            "candidate": Candidate(
                candidate_id=chosen,
                work_id=receipt.work_id,
                revision_id=receipt.revision_id,
                candidate_sha256=candidate_sha256,
                commit_sha=commit_sha,
                kind="final",
                allocated_at=now,
            ),
        },
        receipts=(receipt,),
    )


def _lesson_for(receipt, title):
    from omp_knowledge.learning.models import Claim, Lesson, SourceSpan

    text = receipt.payload["summary"]
    start = canonical_json(receipt.payload).index(text)
    return Lesson(
        title=title,
        steps=(f"apply {title}",),
        claims=(
            Claim(
                text=text,
                receipt_ids=(receipt.receipt_id,),
                source_span=SourceSpan(start=start, end=start + len(text)),
                evaluator=receipt.issuer,
                outcome="PASS",
            ),
        ),
    )


def _status(store, procedure_id=None):
    if procedure_id is None:
        rows = store.execute("SELECT procedure_id, status FROM procedures ORDER BY procedure_id").fetchall()
        return {row["procedure_id"]: row["status"] for row in rows}
    row = store.execute(
        "SELECT status FROM procedures WHERE procedure_id = ?", (procedure_id,)
    ).fetchone()
    return None if row is None else row["status"]


def _open_store(path):
    from omp_knowledge.learning.store import LearningStore

    return LearningStore(path)


def _compile_texts(db_path):
    from omp_knowledge.context.compiler import compile_bundle
    from omp_knowledge.context.models import CompileRequest, StageIdentity
    from omp_knowledge.context.sources import procedural_items

    items = procedural_items(db_path, {})
    bundle = compile_bundle(
        CompileRequest(
            identity=StageIdentity(
                work_id="work",
                work_key="OMP-607",
                revision_id="revision",
                stage="implement",
                attempt_id="attempt",
                candidate_id="candidate",
            ),
            token_budget=100_000,
            encoding="test-words",
            items=items,
        ),
        _WordCounter(),
    )
    return items, bundle


def _supply_text(store, workspace_id):
    from omp_knowledge.learning.uses import supply

    result = supply(
        store,
        workspace_id=workspace_id,
        work_key="OMP-607",
        context={},
        limit=10,
        record=False,
    )
    return "\n".join(result.lines)


def _handler(tmp_path, workspace_id, learning_dir, url, capability):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": "owner-token"}))
    bearer.chmod(0o600)
    settings = tmp_path / "context.json"
    settings.write_text("{}")
    config = OrchestratorConfig(
        workspace_id=workspace_id,
        automation_capability_path=capability,
        qualification_path=tmp_path / "qualification.json",
        lock_map_path=tmp_path / "lock.json",
        verifier_key_path=tmp_path / "verifier.key",
        allowed_signers=tmp_path / "signers",
        control_repo=tmp_path / "control",
        worktrees_dir=tmp_path / "worktrees",
        live_checkout=tmp_path / "live",
        operations=(),
        workservice_url=url,
        context_command=(sys.executable, "-c", "raise SystemExit(0)"),
        context_settings_path=settings,
        learning_state_dir=learning_dir,
        learning_command=(sys.executable, "-m", "omp_knowledge.learning"),
        learning_bearer_file=bearer,
    )
    return OrchestratorHandler(config)


def _cycle(handler, *, observe=False):
    job = {"job_id": "not-a-job", "fence": 1}
    try:
        if observe:
            assert handler.observe(SimpleNamespace(), job) is None
            return
        handler.run(SimpleNamespace(), job)
    except OrchestratorError as error:
        assert error.code == "invalid_request"
        return
    except WorkError as error:
        raise AssertionError(error.diagnostics) from error
    if not observe:
        raise AssertionError("dispatch did not refuse the job")


def _workflow(service, workspace_id, key):
    response = service.client.get(
        f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _append_verification(service, workspace_id, item, candidate, verdict):
    extra = {"candidate_sha256": candidate["candidate_sha256"], "verdict": verdict}
    if candidate.get("commit_sha"):
        extra["candidate_commit"] = candidate["commit_sha"]
    receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        candidate["candidate_id"],
        "verification",
        body={"summary": f"{verdict} evidence"},
        **extra,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    return body["result"]["receipt"]


def _seed_service_lesson(learning_dir, *, procedure_id, title, workspace_id, work_id, candidate_id, revision_id, receipt_id):
    from omp_knowledge.learning.models import SourceIdentity

    source = SourceIdentity(
        workspace_id=str(workspace_id),
        event_id=str(uuid4()),
        event_sequence=1,
        aggregate_id=str(work_id),
        work_id=str(work_id),
        candidate_id=str(candidate_id),
        revision_id=str(revision_id),
    )
    now = "2026-10-15T00:00:00+00:00"
    store = _open_store(learning_dir)
    try:
        with store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                """,
                (procedure_id, procedure_id, title, now, now),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, ?, '[]', 'm', 'p', ?, ?)
                """,
                (
                    procedure_id,
                    title,
                    json.dumps([f"step for {title}"]),
                    source.model_dump_json(),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (procedure_id, str(receipt_id), now),
            )
    finally:
        store.close()


def _admit(service, workspace_id, learning_dir, title, *, cite="pass"):
    item = _create(service, workspace_id, title)
    _plan(service, workspace_id, item)
    workflow = _workflow(service, workspace_id, item["key"])
    candidate = workflow["item"]["candidate"]
    assert candidate is not None
    verification = _append_verification(service, workspace_id, item, candidate, "PASS")
    if cite == "missing":
        cited = str(uuid4())
    elif cite == "invalid":
        invalid = _append_verification(service, workspace_id, item, candidate, "NEEDS_FIX")
        cited = invalid["receipt_id"]
    else:
        cited = verification["receipt_id"]
    _seed_service_lesson(
        learning_dir,
        procedure_id=title,
        title=title,
        workspace_id=workspace_id,
        work_id=item["work_id"],
        candidate_id=candidate["candidate_id"],
        revision_id=workflow["item"]["revision"]["revision_id"],
        receipt_id=cited,
    )
    return item, candidate


def test_migrated_rows_survive_and_accept_suspended(tmp_path, knowledge_path):
    del knowledge_path
    db = tmp_path / "old.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE procedures (
            procedure_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
            current_version INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE procedure_versions (
            procedure_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            preconditions_json TEXT NOT NULL,
            model TEXT NOT NULL,
            profile TEXT NOT NULL,
            source_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (procedure_id, version)
        )
        """
    )
    connection.execute(
        "INSERT INTO procedures VALUES ('keep-active', 'fp-a', 'active', 1, 'Kept active', 't0', 'keep-active-stamp')"
    )
    connection.execute(
        "INSERT INTO procedures VALUES ('keep-withdrawn', 'fp-w', 'withdrawn', 1, 'Kept withdrawn', 't0', 'keep-withdrawn-stamp')"
    )
    connection.execute(
        "INSERT INTO procedure_versions VALUES ('keep-active', 1, 'Kept active', '[\"stay\"]', '[]', 'm', 'p', '{}', 't0')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO procedures VALUES ('nope', 'fp-s', 'suspended', 1, 'No', 't0', 't0')"
        )
    connection.commit()
    connection.close()

    store = _open_store(db)
    try:
        assert _status(store) == {"keep-active": "active", "keep-withdrawn": "withdrawn"}
        store.execute(
            "UPDATE procedures SET status = 'suspended', updated_at = 'suspended-stamp' WHERE procedure_id = 'keep-active'"
        )
        version = store.execute(
            "SELECT title FROM procedure_versions WHERE procedure_id = 'keep-active'"
        ).fetchone()
        assert version["title"] == "Kept active"
    finally:
        store.close()

    reopened = _open_store(db)
    try:
        assert _status(reopened) == {
            "keep-active": "suspended",
            "keep-withdrawn": "withdrawn",
        }
        withdrawn = reopened.execute(
            "SELECT updated_at FROM procedures WHERE procedure_id = 'keep-withdrawn'"
        ).fetchone()
        assert withdrawn["updated_at"] == "keep-withdrawn-stamp"
    finally:
        reopened.close()


def test_same_drain_suspends_stale_hash_and_empty_cycles_sweep(knowledge_path):
    del knowledge_path
    from omp_knowledge.learning.capture import drain, retry

    workspace = uuid4()
    stale = _receipt_for(
        "stale hash lesson",
        candidate_sha256=OTHER_SHA,
        candidate_commit=COMMIT,
    )
    view = _view_for(stale)
    records = _Records(view)
    reader = _Receipts({stale.receipt_id: stale})
    store = _open_store(":memory:")
    try:
        run = drain(
            store,
            _Events([_event(stale.work_id, workspace)]),
            reader,
            _Generator(_lesson_for(stale, "Stale hash lesson")),
            workspace_id=workspace,
            records=records,
        )
        assert run.proposals_accepted == 1
        assert _status(store) == {next(iter(_status(store))): "suspended"}
        assert records.calls == ["work_items", "workflow"]
    finally:
        store.close()

    fresh = _receipt_for("fresh lesson")
    fresh_view = _view_for(fresh)
    fresh_records = _Records(fresh_view)
    fresh_reader = _Receipts({fresh.receipt_id: fresh})
    fresh_store = _open_store(":memory:")
    try:
        admitted = drain(
            fresh_store,
            _Events([_event(fresh.work_id, workspace)]),
            fresh_reader,
            _Generator(_lesson_for(fresh, "Fresh lesson")),
            workspace_id=workspace,
            records=fresh_records,
        )
        assert admitted.proposals_accepted == 1
        assert set(_status(fresh_store).values()) == {"active"}
        fresh_records.view = fresh_view.model_copy(
            update={"item": fresh_view.item.model_copy(update={"candidate": None})}
        )
        empty = drain(
            fresh_store,
            _Events([]),
            fresh_reader,
            _Generator(_lesson_for(fresh, "unused")),
            workspace_id=workspace,
            records=fresh_records,
        )
        assert empty.units_total == 0
        assert set(_status(fresh_store).values()) == {"suspended"}
    finally:
        fresh_store.close()

    replacement_receipt = _receipt_for("replacement lesson")
    replacement_view = _view_for(replacement_receipt)
    replacement_records = _Records(replacement_view)
    replacement_store = _open_store(":memory:")
    try:
        drain(
            replacement_store,
            _Events([_event(replacement_receipt.work_id, workspace)]),
            _Receipts({replacement_receipt.receipt_id: replacement_receipt}),
            _Generator(_lesson_for(replacement_receipt, "Replacement lesson")),
            workspace_id=workspace,
            records=replacement_records,
        )
        assert set(_status(replacement_store).values()) == {"active"}
        replaced = replacement_view.item.candidate.model_copy(update={"candidate_id": uuid4()})
        replacement_records.view = replacement_view.model_copy(
            update={"item": replacement_view.item.model_copy(update={"candidate": replaced})}
        )
        idle = retry(
            replacement_store,
            _Receipts({replacement_receipt.receipt_id: replacement_receipt}),
            _Generator(_lesson_for(replacement_receipt, "unused")),
            records=replacement_records,
            workspace_id=workspace,
        )
        assert idle.units_total == 0
        assert set(_status(replacement_store).values()) == {"suspended"}
    finally:
        replacement_store.close()


def test_injected_sweep_suspends_missing_invalid_and_changed_identity(knowledge_path):
    del knowledge_path
    from omp_knowledge.learning.lifecycle import sweep

    workspace = uuid4()
    other = uuid4()
    receipt = _receipt_for("identity lesson")
    view = _view_for(receipt)

    def seed(path, procedure_id, *, workspace_id=workspace, receipt_id=None, candidate_id=None, revision_id=None, source_json=None):
        now = "2026-10-15T00:00:00+00:00"
        if source_json is None:
            from omp_knowledge.learning.models import SourceIdentity

            source_json = SourceIdentity(
                workspace_id=str(workspace_id),
                event_id=str(uuid4()),
                event_sequence=1,
                aggregate_id=str(receipt.work_id),
                work_id=str(receipt.work_id),
                candidate_id=str(candidate_id or receipt.candidate_id),
                revision_id=str(revision_id or receipt.revision_id),
            ).model_dump_json()
        db = _open_store(path)
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                """,
                (procedure_id, procedure_id, procedure_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, ?, '[]', 'm', 'p', ?, ?)
                """,
                (procedure_id, procedure_id, json.dumps([procedure_id]), source_json, now),
            )
            if receipt_id is not None:
                conn.execute(
                    """
                    INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
                    VALUES (?, ?, NULL, ?)
                    """,
                    (procedure_id, str(receipt_id), now),
                )
        return db

    path = ":memory:"
    db = seed(path, "fresh", receipt_id=receipt.receipt_id)
    try:
        records = _Records(view)
        assert sweep(db, _Receipts({receipt.receipt_id: receipt}), records, workspace) == []
        assert _status(db, "fresh") == "active"

        records.view = view.model_copy(update={"item": view.item.model_copy(update={"candidate": None})})
        assert sweep(db, _Receipts({receipt.receipt_id: receipt}), records, workspace) == ["fresh"]
        assert _status(db, "fresh") == "suspended"

        records.view = view
        assert sweep(db, _Receipts({receipt.receipt_id: receipt}), records, workspace) == []
        assert _status(db, "fresh") == "suspended"
    finally:
        db.close()

    cases = {
        "none": lambda _receipt_id: None,
        "missing-key": KeyError("gone"),
        "not-found": WorkError("not_found", status=404),
        "invalid-request": WorkError("invalid_request", status=400),
        "malformed": {"kind": "not-evidence"},
    }

    class _One:
        def __init__(self, value):
            self.value = value

        def receipt(self, receipt_id):
            del receipt_id
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    for name, value in cases.items():
        case_db = seed(":memory:", name, receipt_id=receipt.receipt_id)
        try:
            suspended = sweep(case_db, _One(value), _Records(view), workspace)
            assert suspended == [name]
            assert _status(case_db, name) == "suspended"
        finally:
            case_db.close()

    flipped = receipt.model_copy(update={"verdict": "NEEDS_FIX"})
    invalid_db = seed(":memory:", "invalidated", receipt_id=receipt.receipt_id)
    try:
        assert sweep(invalid_db, _Receipts({receipt.receipt_id: flipped}), _Records(view), workspace) == [
            "invalidated"
        ]
    finally:
        invalid_db.close()

    replaced = view.item.candidate.model_copy(update={"candidate_id": uuid4()})
    replaced_view = view.model_copy(
        update={"item": view.item.model_copy(update={"candidate": replaced})}
    )
    replace_db = seed(":memory:", "replaced", receipt_id=receipt.receipt_id)
    try:
        assert sweep(replace_db, _Receipts({receipt.receipt_id: receipt}), _Records(replaced_view), workspace) == [
            "replaced"
        ]
    finally:
        replace_db.close()

    revised = view.item.revision.model_copy(update={"revision_id": uuid4(), "revision_number": 2})
    revised_view = view.model_copy(
        update={
            "item": view.item.model_copy(update={"revision": revised, "candidate": None})
        }
    )
    revise_db = seed(":memory:", "revised", receipt_id=receipt.receipt_id)
    try:
        assert sweep(revise_db, _Receipts({receipt.receipt_id: receipt}), _Records(revised_view), workspace) == [
            "revised"
        ]
    finally:
        revise_db.close()

    outsider = seed(":memory:", "outsider", workspace_id=other, receipt_id=str(uuid4()))
    try:
        with outsider.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES ('fixture', 'fp-fixture', 'active', 1, 'Verified worker lesson', 't', 't')
                """
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES ('fixture', 1, 'Verified worker lesson', '["Inspect actual worktree"]', '[]', 'm', 'p', '{}', 't')
                """
            )
        assert sweep(outsider, _One(None), _Records(view), workspace) == []
        assert _status(outsider, "outsider") == "active"
        assert _status(outsider, "fixture") == "active"
    finally:
        outsider.close()


def test_supply_and_compile_exclude_suspended_text(tmp_path, knowledge_path):
    del knowledge_path
    from omp_knowledge.learning.lifecycle import sweep

    workspace = uuid4()
    receipt = _receipt_for("compile lesson")
    view = _view_for(receipt)
    db_path = tmp_path / "learning.sqlite"
    store = _open_store(db_path)
    now = "2026-10-15T00:00:00+00:00"
    from omp_knowledge.learning.models import SourceIdentity

    source = SourceIdentity(
        workspace_id=str(workspace),
        event_id=str(uuid4()),
        event_sequence=1,
        aggregate_id=str(receipt.work_id),
        work_id=str(receipt.work_id),
        candidate_id=str(receipt.candidate_id),
        revision_id=str(receipt.revision_id),
    ).model_dump_json()
    try:
        with store.transaction() as conn:
            for procedure_id, title in (
                ("shown", "Shown lesson text"),
                ("hidden", "Hidden lesson text"),
            ):
                conn.execute(
                    """
                    INSERT INTO procedures (
                        procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                    ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (procedure_id, procedure_id, title, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO procedure_versions (
                        procedure_id, version, title, steps_json, preconditions_json,
                        model, profile, source_json, created_at
                    ) VALUES (?, 1, ?, ?, '[]', 'm', 'p', ?, ?)
                    """,
                    (procedure_id, title, json.dumps([title]), source, now),
                )
                conn.execute(
                    """
                    INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
                    VALUES (?, ?, NULL, ?)
                    """,
                    (procedure_id, str(receipt.receipt_id), now),
                )
        # Both lessons share one workflow. Suspend only the hidden lesson by
        # citing a receipt the reader does not have, while the shown lesson's
        # receipt stays a fresh supporting PASS.
        class _Split:
            def receipt(self, receipt_id):
                if str(receipt_id) == str(receipt.receipt_id):
                    return receipt
                return None

        hidden_id = str(uuid4())
        with store.transaction() as conn:
            conn.execute(
                "UPDATE procedure_support SET receipt_id = ? WHERE procedure_id = 'hidden'",
                (hidden_id,),
            )
        assert sweep(store, _Split(), _Records(view), workspace) == ["hidden"]
        assert _status(store, "hidden") == "suspended"
        assert _status(store, "shown") == "active"
        supplied = _supply_text(store, workspace)
        assert "Shown lesson text" in supplied
        assert "Hidden lesson text" not in supplied
        items, bundle = _compile_texts(db_path)
        assert "Shown lesson text" in bundle.text
        assert "Hidden lesson text" not in bundle.text
        hidden = next(item for item in items if item.ref.startswith("hidden"))
        assert hidden.status == "withdrawn"
        assert hidden.detail == "procedure suspended"
        assert any(exclusion.reason == "withdrawn" and exclusion.ref.startswith("hidden") for exclusion in bundle.exclusions)
    finally:
        store.close()


def test_transport_and_permission_do_not_withdraw(tmp_path, knowledge_path, monkeypatch):
    from omp_knowledge.learning.cli import main as cli_main
    from omp_knowledge.learning.lifecycle import sweep

    workspace = uuid4()
    receipt = _receipt_for("transport lesson")
    view = _view_for(receipt)

    def new_store(procedure_id="live"):
        db = _open_store(":memory:")
        now = "2026-10-15T00:00:00+00:00"
        from omp_knowledge.learning.models import SourceIdentity

        source = SourceIdentity(
            workspace_id=str(workspace),
            event_id=str(uuid4()),
            event_sequence=1,
            aggregate_id=str(receipt.work_id),
            work_id=str(receipt.work_id),
            candidate_id=str(receipt.candidate_id),
            revision_id=str(receipt.revision_id),
        ).model_dump_json()
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                """,
                (procedure_id, procedure_id, procedure_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, '["stay"]', '[]', 'm', 'p', ?, ?)
                """,
                (procedure_id, procedure_id, source, now),
            )
            conn.execute(
                """
                INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (procedure_id, str(receipt.receipt_id), now),
            )
        return db

    class _Boom:
        def __init__(self, error):
            self.error = error

        def receipt(self, receipt_id):
            del receipt_id
            raise self.error

        def work_items(self, *, after=None, limit=None):
            del after, limit
            raise self.error

        def workflow(self, key):
            del key
            raise self.error

    for error in (
        httpx.ConnectError("down"),
        WorkError("forbidden", status=403),
        WorkError("unavailable", status=503),
        PermissionError("no"),
    ):
        db = new_store()
        try:
            with pytest.raises(type(error)):
                sweep(db, _Boom(error), _Boom(error), workspace)
            assert _status(db, "live") == "active"
        finally:
            db.close()

    class _ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("WorkClient constructed")

    monkeypatch.setattr("omp_knowledge.learning.cli.WorkClient", _ExplodingClient)
    injected = new_store("injected")
    try:
        missing = str(uuid4())
        with injected.transaction() as conn:
            conn.execute(
                "UPDATE procedure_support SET receipt_id = ? WHERE procedure_id = 'injected'",
                (missing,),
            )

        class _Missing:
            def receipt(self, receipt_id):
                del receipt_id
                return None

            def work_items(self, *, after=None, limit=None):
                return _Records(view).work_items()

            def workflow(self, key):
                return _Records(view).workflow(key)

        code = cli_main(
            [
                "sweep",
                "--state-dir",
                str(tmp_path / "unused-state"),
                "--work-url",
                "http://127.0.0.1:9",
                "--workspace",
                str(workspace),
                "--bearer-file",
                str(tmp_path / "missing-bearer"),
                "--json",
            ],
            store=injected,
            receipts=_Missing(),
            records=_Missing(),
        )
        assert code == 0
        assert _status(injected, "injected") == "suspended"
    finally:
        injected.close()

    # A configured sweep with no resolvable lesson does not open the bearer.
    bare = _open_store(tmp_path / "bare")
    try:
        with bare.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES ('context-lesson', 'fp', 'active', 1, 'Verified worker lesson', 't', 't')
                """
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES ('context-lesson', 1, 'Verified worker lesson', '["Inspect actual worktree"]', '[]', 'm', 'p', '{}', 't')
                """
            )
        code = cli_main(
            [
                "sweep",
                "--state-dir",
                str(tmp_path / "bare"),
                "--work-url",
                "http://127.0.0.1:9",
                "--workspace",
                str(workspace),
                "--bearer-file",
                str(tmp_path / "missing-bearer"),
            ],
            store=bare,
        )
        assert code == 0
        assert _status(bare, "context-lesson") == "active"
    finally:
        bare.close()


def test_cli_transport_failure_leaves_the_lesson_active(tmp_path, knowledge_path):
    workspace = uuid4()
    receipt = _receipt_for("remote lesson")
    learning_dir = tmp_path / "learning"
    _seed_service_lesson(
        learning_dir,
        procedure_id="remote",
        title="Remote lesson text",
        workspace_id=workspace,
        work_id=receipt.work_id,
        candidate_id=receipt.candidate_id,
        revision_id=receipt.revision_id,
        receipt_id=receipt.receipt_id,
    )
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": "owner-token"}))
    bearer.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = sock.getsockname()[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(knowledge_path), env.get("PYTHONPATH", ""))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_knowledge.learning",
            "sweep",
            "--state-dir",
            str(learning_dir),
            "--work-url",
            f"http://127.0.0.1:{dead}",
            "--workspace",
            str(workspace),
            "--bearer-file",
            str(bearer),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode != 0, result.stdout
    store = _open_store(learning_dir)
    try:
        assert _status(store, "remote") == "active"
    finally:
        store.close()

    capability = tmp_path / "automation.json"
    capability.write_text(
        json.dumps(
            {
                "actor_id": str(uuid4()),
                "actor_kind": "automation",
                "workspaces": [str(workspace)],
                "scopes": ["work.read"],
            }
        )
    )
    handler = _handler(tmp_path / "handler", workspace, learning_dir, f"http://127.0.0.1:{dead}", capability)
    with pytest.raises(WorkError) as caught:
        handler.run(SimpleNamespace(), {"job_id": "not-a-job", "fence": 1})
    assert caught.value.code == "lesson_sweep_failed"
    store = _open_store(learning_dir)
    try:
        assert _status(store, "remote") == "active"
    finally:
        store.close()


def test_service_cycle_suspends_and_restart_keeps_the_lesson_out(service, tmp_path, knowledge_path):
    workspace_id = uuid4()
    _grant(service, workspace_id)
    learning_dir = tmp_path / "learning"
    _admit(service, workspace_id, learning_dir, "Keep supplying this lesson")
    cleared_item, _cleared_candidate = _admit(
        service, workspace_id, learning_dir, "Drop after revision clear"
    )
    replaced_item, replaced_candidate = _admit(
        service, workspace_id, learning_dir, "Drop after replacement"
    )
    _admit(service, workspace_id, learning_dir, "Drop missing receipt", cite="missing")
    _admit(service, workspace_id, learning_dir, "Drop invalidated receipt", cite="invalid")

    title = "Drop after revision clear"
    revision_id = uuid4()
    revision = {
        "revision_id": str(revision_id),
        "work_id": cleared_item["work_id"],
        "revision_number": 2,
        "title": title,
        "description": "revised",
        "scope": "",
        "acceptance_criteria": [],
        "content_sha256": sha256({"title": title, "description": "revised"}),
        "created_by": "owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "revise_work",
            "payload": {
                "work_id": cleared_item["work_id"],
                "expected_revision_id": cleared_item["revision_id"],
                "revision": revision,
            },
        },
    )
    assert status == 200 and body["result"]["changed"] is True, body
    cleared = _workflow(service, workspace_id, cleared_item["key"])
    assert cleared["item"]["candidate"] is None

    _plan(service, workspace_id, replaced_item)
    replaced = _workflow(service, workspace_id, replaced_item["key"])
    assert replaced["item"]["candidate"]["candidate_id"] != str(replaced_candidate["candidate_id"])

    app = create_app(service.config, capabilities_dir=service.capabilities)
    with _serving(app) as url:
        handler = _handler(
            tmp_path / "cycle",
            workspace_id,
            learning_dir,
            url,
            service.capabilities / "owner.json",
        )
        _cycle(handler)
        store = _open_store(learning_dir)
        try:
            assert _status(store, "Keep supplying this lesson") == "active"
            assert _status(store, "Drop after revision clear") == "suspended"
            assert _status(store, "Drop after replacement") == "suspended"
            assert _status(store, "Drop missing receipt") == "suspended"
            assert _status(store, "Drop invalidated receipt") == "suspended"
            supplied = _supply_text(store, workspace_id)
            assert "Keep supplying this lesson" in supplied
            assert "Drop after revision clear" not in supplied
            assert "Drop after replacement" not in supplied
            assert "Drop missing receipt" not in supplied
            assert "Drop invalidated receipt" not in supplied
        finally:
            store.close()

        restarted = _handler(
            tmp_path / "restart",
            workspace_id,
            learning_dir,
            url,
            service.capabilities / "owner.json",
        )
        _cycle(restarted, observe=True)

    store = _open_store(learning_dir)
    try:
        assert _status(store, "Keep supplying this lesson") == "active"
        for procedure_id in (
            "Drop after revision clear",
            "Drop after replacement",
            "Drop missing receipt",
            "Drop invalidated receipt",
        ):
            assert _status(store, procedure_id) == "suspended"
        supplied = _supply_text(store, workspace_id)
        assert "Keep supplying this lesson" in supplied
        assert "Drop after revision clear" not in supplied
        items, bundle = _compile_texts(learning_dir / "learning.sqlite")
        assert "Keep supplying this lesson" in bundle.text
        assert "Drop after revision clear" not in bundle.text
        assert "Drop invalidated receipt" not in bundle.text
        dropped = next(item for item in items if "Drop after revision clear" in item.text)
        assert dropped.status == "withdrawn"
        assert dropped.detail == "procedure suspended"
    finally:
        store.close()
