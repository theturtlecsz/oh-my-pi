from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID


def compute_unit_id(workspace_id: str | UUID, event_id: str | UUID) -> str:
    """Compute deterministic unit_id as sha256 of workspace_id and event_id."""
    return hashlib.sha256(f"{workspace_id}:{event_id}".encode("utf-8")).hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS capture_cursor (
    workspace_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'no_lesson', 'partial', 'failed')),
    units_total INTEGER NOT NULL DEFAULT 0,
    units_failed INTEGER NOT NULL DEFAULT 0,
    units_no_lesson INTEGER NOT NULL DEFAULT 0,
    proposals_accepted INTEGER NOT NULL DEFAULT 0,
    proposals_rejected INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    workspace_id TEXT,
    event_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('queued', 'running', 'succeeded', 'no_lesson', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    lease_until TEXT,
    trace_json TEXT NOT NULL,
    model TEXT NOT NULL CHECK(length(model) > 0),
    profile TEXT NOT NULL CHECK(length(profile) > 0),
    source_json TEXT NOT NULL CHECK(length(source_json) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected')),
    reason TEXT,
    procedure_id TEXT,
    lesson_json TEXT NOT NULL,
    model TEXT NOT NULL CHECK(length(model) > 0),
    profile TEXT NOT NULL CHECK(length(profile) > 0),
    source_json TEXT NOT NULL CHECK(length(source_json) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedures (
    procedure_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
    current_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedure_versions (
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    preconditions_json TEXT NOT NULL,
    model TEXT NOT NULL CHECK(length(model) > 0),
    profile TEXT NOT NULL CHECK(length(profile) > 0),
    source_json TEXT NOT NULL CHECK(length(source_json) > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (procedure_id, version)
);

CREATE TABLE IF NOT EXISTS procedure_support (
    procedure_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    proposal_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (procedure_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS supplies (
    supply_id TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    work_key TEXT NOT NULL,
    context_json TEXT NOT NULL,
    supplied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uses (
    use_id TEXT PRIMARY KEY,
    supply_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    receipt_ids_json TEXT NOT NULL,
    used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    use_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    candidate_id TEXT,
    verdict TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    from_version INTEGER,
    to_version INTEGER,
    preconditions_json TEXT,
    model TEXT NOT NULL CHECK(length(model) > 0),
    profile TEXT NOT NULL CHECK(length(profile) > 0),
    source_json TEXT NOT NULL CHECK(length(source_json) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_state ON units(state);
CREATE INDEX IF NOT EXISTS idx_supplies_work_key ON supplies(work_key, procedure_id);
CREATE INDEX IF NOT EXISTS idx_cleanup_queue_done_at ON cleanup_queue(done_at);
"""


class LearningStore:
    """SQLite-backed store for learning proposals, units, procedures,
    supplies, uses, outcomes, corrections, and cleanup tasks.
    """

    def __init__(self, path: str | Path | None = ":memory:") -> None:
        if path is None or path == ":memory:":
            self._db_path = ":memory:"
            self.path = ":memory:"
        else:
            p = Path(path)
            if p.is_dir():
                self._db_path = str(p / "learning.sqlite")
                self.path = str(p / "learning.sqlite")
            elif not p.suffix:
                p.mkdir(parents=True, exist_ok=True)
                self._db_path = str(p / "learning.sqlite")
                self.path = str(p / "learning.sqlite")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                self._db_path = str(p)
                self.path = str(p)

        self._conn: sqlite3.Connection = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._in_transaction = False
        self._savepoint_count = 0

        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")

        self._init_db()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _init_db(self) -> None:
        self._conn.executescript(SCHEMA_SQL)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """BEGIN IMMEDIATE transaction context manager with savepoint nesting support."""
        if not self._in_transaction:
            self._in_transaction = True
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._in_transaction = False
        else:
            self._savepoint_count += 1
            sp_name = f"sp_{self._savepoint_count}"
            self._conn.execute(f"SAVEPOINT {sp_name}")
            try:
                yield self._conn
                self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except Exception:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                raise

    def execute(self, sql: str, parameters: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        return self._conn.executemany(sql, seq_of_parameters)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> LearningStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


__all__ = [
    "LearningStore",
    "compute_unit_id",
]
