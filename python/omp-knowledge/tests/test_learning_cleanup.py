from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from omp_knowledge.learning.cleanup import (
    CleanupItem,
    CleanupRun,
    CleanupTarget,
    NativeCommittedTarget,
    drain_cleanup,
)
from omp_knowledge.learning.cli import EXIT_OK, EXIT_RUN_FAILED, main
from omp_knowledge.learning.store import LearningStore


class FailingTarget:
    def __init__(self, message: str = "transient RPC failure") -> None:
        self.message = message

    def clean(self, procedure_id: str, action: str) -> None:
        raise RuntimeError(self.message)


class HealthyTarget:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def clean(self, procedure_id: str, action: str) -> None:
        self.calls.append((procedure_id, action))


def test_cleanup_failing_target_leaves_pending_and_retry_marks_done(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "learning.sqlite"
    store = LearningStore(db_file)

    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cleanup_queue (procedure_id, action, created_at)
            VALUES ('proc-1', 'withdraw', '2026-09-27T10:00:00Z')
            """
        )

    # 1. Failing target leaves the row pending with attempts=1 and last_error set
    failing = FailingTarget("service down")
    assert isinstance(failing, CleanupTarget)
    run1 = drain_cleanup(store, failing, limit=10)

    assert isinstance(run1, CleanupRun)
    assert run1.processed == 1
    assert run1.succeeded == 0
    assert run1.failed == 1
    assert len(run1.items) == 1
    item1 = run1.items[0]
    assert isinstance(item1, CleanupItem)
    assert item1.procedure_id == "proc-1"
    assert item1.action == "withdraw"
    assert item1.attempts == 1
    assert item1.done is False
    assert item1.last_error == "RuntimeError: service down"

    row1 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = 'proc-1'"
    ).fetchone()
    assert row1 is not None
    assert row1["attempts"] == 1
    assert row1["done_at"] is None
    assert row1["last_error"] == "RuntimeError: service down"

    # 2. A second drain with a healthy target marks it done with attempts=2
    healthy = HealthyTarget()
    run2 = drain_cleanup(store, healthy, limit=10)

    assert run2.processed == 1
    assert run2.succeeded == 1
    assert run2.failed == 0
    assert len(run2.items) == 1
    item2 = run2.items[0]
    assert item2.procedure_id == "proc-1"
    assert item2.action == "withdraw"
    assert item2.attempts == 2
    assert item2.done is True
    assert item2.last_error is None
    assert healthy.calls == [("proc-1", "withdraw")]

    row2 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = 'proc-1'"
    ).fetchone()
    assert row2 is not None
    assert row2["attempts"] == 2
    assert row2["done_at"] is not None
    assert row2["last_error"] is None

    # 3. Done rows are never re-processed
    run3 = drain_cleanup(store, healthy, limit=10)
    assert run3.processed == 0
    assert run3.succeeded == 0
    assert run3.failed == 0
    assert len(run3.items) == 0
    assert healthy.calls == [("proc-1", "withdraw")]  # no new calls


def test_old_cleanup_queue_schema_migration(tmp_path: Path) -> None:
    db_file = tmp_path / "old_learning.sqlite"

    # Create table with old schema (lacking attempts and last_error)
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.execute(
        """
        CREATE TABLE cleanup_queue (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL,
            done_at TEXT DEFAULT NULL
        )
        """
    )
    raw_conn.execute(
        """
        INSERT INTO cleanup_queue (procedure_id, action, created_at, done_at)
        VALUES ('proc-old-1', 'narrow', '2026-09-27T08:00:00Z', NULL)
        """
    )
    raw_conn.execute(
        """
        INSERT INTO cleanup_queue (procedure_id, action, created_at, done_at)
        VALUES ('proc-old-2', 'withdraw', '2026-09-27T08:00:00Z', '2026-09-27T08:30:00Z')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    # Open with LearningStore; _init_db should add attempts and last_error via ALTER TABLE
    store = LearningStore(db_file)
    cols = {
        row["name"]
        for row in store.execute("PRAGMA table_info(cleanup_queue)").fetchall()
    }
    assert "attempts" in cols
    assert "last_error" in cols

    # Verify pending row is intact with defaults
    pending = store.execute(
        "SELECT queue_id, procedure_id, action, attempts, last_error, done_at FROM cleanup_queue WHERE procedure_id = 'proc-old-1'"
    ).fetchone()
    assert pending is not None
    assert pending["procedure_id"] == "proc-old-1"
    assert pending["action"] == "narrow"
    assert pending["attempts"] == 0
    assert pending["last_error"] is None
    assert pending["done_at"] is None

    # Verify done row is intact with defaults
    done_row = store.execute(
        "SELECT attempts, last_error, done_at FROM cleanup_queue WHERE procedure_id = 'proc-old-2'"
    ).fetchone()
    assert done_row is not None
    assert done_row["attempts"] == 0
    assert done_row["last_error"] is None
    assert done_row["done_at"] == "2026-09-27T08:30:00Z"

    # Drain should process the pending row cleanly
    healthy = HealthyTarget()
    run = drain_cleanup(store, healthy, limit=10)
    assert run.processed == 1
    assert run.succeeded == 1
    assert run.failed == 0
    assert run.items[0].procedure_id == "proc-old-1"
    assert run.items[0].attempts == 1
    assert run.items[0].done is True


def test_native_committed_target_withdraw(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite")
    target = NativeCommittedTarget(store)
    assert isinstance(target, CleanupTarget)

    store.execute(
        """
        INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
        VALUES ('proc-w', 'fp-w', 'active', 1, 'Withdraw Target Test', 'now', 'now')
        """
    )

    # 1. Raises while procedure status is 'active'
    with pytest.raises(RuntimeError, match="status is 'active', expected 'withdrawn'"):
        target.clean("proc-w", "withdraw")

    # 2. Succeeds once status is committed as 'withdrawn'
    store.execute(
        "UPDATE procedures SET status = 'withdrawn' WHERE procedure_id = 'proc-w'"
    )
    target.clean("proc-w", "withdraw")  # does not raise

    # 3. Nonexistent procedure raises
    with pytest.raises(RuntimeError, match="Procedure nonexistent not found"):
        target.clean("nonexistent", "withdraw")

    # 4. Unknown action raises ValueError
    with pytest.raises(ValueError, match="Unknown cleanup action 'unknown'"):
        target.clean("proc-w", "unknown")


def test_native_committed_target_narrow(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite")
    target = NativeCommittedTarget(store)

    store.execute(
        """
        INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
        VALUES ('proc-n', 'fp-n', 'active', 1, 'Narrow Target Test', 'now', 'now')
        """
    )

    # 1. No corrections row exists -> raises
    with pytest.raises(
        RuntimeError, match="has no accepted narrow correction committed"
    ):
        target.clean("proc-n", "narrow")

    # 2. Rejected corrections row exists -> raises
    store.execute(
        """
        INSERT INTO corrections (
            correction_id, procedure_id, receipt_id, action, status, to_version,
            model, profile, source_json, created_at
        ) VALUES ('c-rej', 'proc-n', 'rec-1', 'narrow', 'rejected', 2, 'mod', 'prof', '{}', 'now')
        """
    )
    with pytest.raises(
        RuntimeError, match="has no accepted narrow correction committed"
    ):
        target.clean("proc-n", "narrow")

    # 3. Accepted correction row with to_version=2, but procedure current_version is still 1 -> raises
    store.execute(
        """
        INSERT INTO corrections (
            correction_id, procedure_id, receipt_id, action, status, to_version,
            model, profile, source_json, created_at
        ) VALUES ('c-acc', 'proc-n', 'rec-2', 'narrow', 'accepted', 2, 'mod', 'prof', '{}', 'now')
        """
    )
    with pytest.raises(
        RuntimeError, match="has no accepted narrow correction committed"
    ):
        target.clean("proc-n", "narrow")

    # 4. Procedure current_version updated to 2 (to_version <= current_version) -> succeeds
    store.execute(
        "UPDATE procedures SET current_version = 2 WHERE procedure_id = 'proc-n'"
    )
    target.clean("proc-n", "narrow")  # does not raise


def test_cli_cleanup_lifecycle_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "learning_cli"
    store = LearningStore(state_dir)

    store.execute(
        """
        INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
        VALUES ('proc-cli', 'fp-cli', 'active', 1, 'CLI Cleanup Test', 'now', 'now')
        """
    )
    store.execute(
        """
        INSERT INTO cleanup_queue (procedure_id, action, created_at)
        VALUES ('proc-cli', 'withdraw', 'now')
        """
    )

    # 1. While procedure is still active, NativeCommittedTarget fails the row; CLI exits 1
    rc1 = main(["cleanup", "--state-dir", str(state_dir)], store=store)
    assert rc1 == EXIT_RUN_FAILED

    out1 = capsys.readouterr().out
    assert "CLEANUP processed=1 succeeded=0 failed=1" in out1
    assert "proc-cli withdraw failed attempts=1" in out1

    row1 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = 'proc-cli'"
    ).fetchone()
    assert row1 is not None
    assert row1["attempts"] == 1
    assert row1["done_at"] is None
    assert "expected 'withdrawn'" in (row1["last_error"] or "")

    # 2. Once the change commits (status becomes 'withdrawn'), CLI exits 0
    store.execute(
        "UPDATE procedures SET status = 'withdrawn' WHERE procedure_id = 'proc-cli'"
    )
    rc2 = main(["cleanup", "--state-dir", str(state_dir)], store=store)
    assert rc2 == EXIT_OK

    out2 = capsys.readouterr().out
    assert "CLEANUP processed=1 succeeded=1 failed=0" in out2
    assert "proc-cli withdraw done attempts=2" in out2

    row2 = store.execute(
        "SELECT attempts, done_at, last_error FROM cleanup_queue WHERE procedure_id = 'proc-cli'"
    ).fetchone()
    assert row2 is not None
    assert row2["attempts"] == 2
    assert row2["done_at"] is not None
    assert row2["last_error"] is None

    # 3. Third run with --json when queue is empty exits 0
    rc3 = main(["cleanup", "--state-dir", str(state_dir), "--json"], store=store)
    assert rc3 == EXIT_OK
    out3 = capsys.readouterr().out
    data = json.loads(out3)
    assert data["processed"] == 0
    assert data["succeeded"] == 0
    assert data["failed"] == 0
    assert data["items"] == []


def test_drain_cleanup_limit_and_ordering(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite")

    for i in range(1, 6):
        store.execute(
            """
            INSERT INTO cleanup_queue (procedure_id, action, created_at)
            VALUES (?, 'withdraw', 'now')
            """,
            (f"proc-{i}",),
        )

    healthy = HealthyTarget()

    # Batch 1: limit 2
    run1 = drain_cleanup(store, healthy, limit=2)
    assert run1.processed == 2
    assert [item.procedure_id for item in run1.items] == ["proc-1", "proc-2"]

    # Batch 2: limit 2
    run2 = drain_cleanup(store, healthy, limit=2)
    assert run2.processed == 2
    assert [item.procedure_id for item in run2.items] == ["proc-3", "proc-4"]

    # Batch 3: limit 2
    run3 = drain_cleanup(store, healthy, limit=2)
    assert run3.processed == 1
    assert [item.procedure_id for item in run3.items] == ["proc-5"]

    # Batch 4: nothing left
    run4 = drain_cleanup(store, healthy, limit=2)
    assert run4.processed == 0
    assert run4.items == ()
