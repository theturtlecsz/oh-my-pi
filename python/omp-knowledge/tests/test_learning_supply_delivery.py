from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import (
    SupplyRecord,
    SupplyResult,
    procedure_history,
    record_supply,
    supply,
)


def _insert_procedure(
    store: LearningStore,
    *,
    procedure_id: str | None = None,
    fingerprint: str | None = None,
    status: str = "active",
    version: int = 1,
    title: str = "Delivery Test Procedure",
    steps: list[str] | tuple[str, ...] = ("step 1", "step 2"),
    preconditions: list[dict[str, str]] | tuple[()] = (),
) -> str:
    proc_id = procedure_id or str(uuid4())
    fp = fingerprint or str(uuid4())
    now = "2026-10-14T10:00:00Z"
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (proc_id, fp, status, version, title, now, now),
        )
        conn.execute(
            """
            INSERT INTO procedure_versions (procedure_id, version, title, steps_json, preconditions_json, model, profile, source_json, created_at)
            VALUES (?, ?, ?, ?, ?, 'qwen-2.5', 'local-qwen', '{}', ?)
            """,
            (proc_id, version, title, json.dumps(list(steps)), json.dumps(list(preconditions)), now),
        )
    return proc_id


def test_store_migration_adds_delivery_columns_and_preserves_existing_rows(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy.sqlite"

    with sqlite3.connect(db_file) as raw_conn:
        raw_conn.execute(
            """
            CREATE TABLE supplies (
                supply_id TEXT PRIMARY KEY,
                procedure_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                workspace_id TEXT NOT NULL,
                work_key TEXT NOT NULL,
                context_json TEXT NOT NULL,
                supplied_at TEXT NOT NULL
            )
            """
        )
        raw_conn.execute(
            """
            INSERT INTO supplies (supply_id, procedure_id, version, workspace_id, work_key, context_json, supplied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("sup-legacy", "proc-1", 1, "ws-1", "work-1", json.dumps({"legacy": True}), "2026-10-14T00:00:00Z"),
        )
        raw_conn.commit()

    store = LearningStore(db_file)
    cols = {row["name"] for row in store.connection.execute("PRAGMA table_info(supplies)").fetchall()}
    assert "worker_job_id" in cols
    assert "compiled_context_hash" in cols
    assert "prompt_hash" in cols

    row = store.connection.execute("SELECT * FROM supplies WHERE supply_id = ?", ("sup-legacy",)).fetchone()
    assert row is not None
    assert row["supply_id"] == "sup-legacy"
    assert row["procedure_id"] == "proc-1"
    assert row["version"] == 1
    assert row["workspace_id"] == "ws-1"
    assert row["work_key"] == "work-1"
    assert json.loads(row["context_json"]) == {"legacy": True}
    assert row["supplied_at"] == "2026-10-14T00:00:00Z"
    assert row["worker_job_id"] is None
    assert row["compiled_context_hash"] is None
    assert row["prompt_hash"] is None

    # Idempotent on second open
    store2 = LearningStore(db_file)
    cols2 = {row["name"] for row in store2.connection.execute("PRAGMA table_info(supplies)").fetchall()}
    assert "worker_job_id" in cols2
    assert "compiled_context_hash" in cols2
    assert "prompt_hash" in cols2


def test_supply_record_false_returns_candidates_without_db_rows() -> None:
    store = LearningStore(":memory:")
    proc_id = _insert_procedure(store, title="Candidate Only Procedure")

    result = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-key-1",
        context={"env": "testing"},
        record=False,
    )

    assert isinstance(result, SupplyResult)
    assert len(result.supply_ids) == 1
    assert len(result.lines) == 1
    assert len(result.records) == 1

    proposed = result.records[0]
    assert proposed.supply_id == result.supply_ids[0]
    assert proposed.procedure_id == proc_id
    assert proposed.version == 1
    assert proposed.work_key == "work-key-1"
    assert proposed.context == {"env": "testing"}
    assert proposed.worker_job_id is None
    assert proposed.compiled_context_hash is None
    assert proposed.prompt_hash is None

    count = store.connection.execute("SELECT count(*) AS cnt FROM supplies").fetchone()["cnt"]
    assert count == 0

    # Default record=True inserts rows
    recorded_result = supply(
        store,
        workspace_id=uuid4(),
        work_key="work-key-2",
        context={"env": "testing"},
    )
    assert len(recorded_result.records) == 1
    assert len(recorded_result.supply_ids) == 1
    count_after = store.connection.execute("SELECT count(*) AS cnt FROM supplies").fetchone()["cnt"]
    assert count_after == 1


def test_record_supply_persists_delivery_hashes() -> None:
    store = LearningStore(":memory:")
    rec = SupplyRecord(
        supply_id="sup-delivery-1",
        procedure_id="proc-test",
        version=1,
        workspace_id="ws-deliv",
        work_key="work-deliv",
        context={"model": "test-model"},
        supplied_at="2026-10-14T10:30:00Z",
        worker_job_id="job-999",
        compiled_context_hash="ctx-hash-abc",
        prompt_hash="prompt-hash-xyz",
    )

    record_supply(store, rec)

    row = store.connection.execute("SELECT * FROM supplies WHERE supply_id = ?", ("sup-delivery-1",)).fetchone()
    assert row is not None
    assert row["supply_id"] == "sup-delivery-1"
    assert row["procedure_id"] == "proc-test"
    assert row["version"] == 1
    assert row["workspace_id"] == "ws-deliv"
    assert row["work_key"] == "work-deliv"
    assert json.loads(row["context_json"]) == {"model": "test-model"}
    assert row["supplied_at"] == "2026-10-14T10:30:00Z"
    assert row["worker_job_id"] == "job-999"
    assert row["compiled_context_hash"] == "ctx-hash-abc"
    assert row["prompt_hash"] == "prompt-hash-xyz"


def test_procedure_history_reads_back_all_delivery_fields() -> None:
    store = LearningStore(":memory:")
    proc_id = _insert_procedure(store, title="History Delivery Procedure")

    rec1 = SupplyRecord(
        supply_id="sup-hist-1",
        procedure_id=proc_id,
        version=1,
        workspace_id="ws-hist",
        work_key="work-hist",
        context={"stage": "compile"},
        supplied_at="2026-10-14T08:00:00Z",
        worker_job_id="job-hist-1",
        compiled_context_hash="ctx-hash-hist-1",
        prompt_hash="prompt-hash-hist-1",
    )
    rec2 = SupplyRecord(
        supply_id="sup-hist-2",
        procedure_id=proc_id,
        version=1,
        workspace_id="ws-hist",
        work_key="work-hist",
        context={"stage": "execute"},
        supplied_at="2026-10-14T09:00:00Z",
    )

    record_supply(store, rec1)
    record_supply(store, rec2)

    history = procedure_history(store, proc_id)
    assert len(history.supplies) == 2

    s1 = next(s for s in history.supplies if s.supply_id == "sup-hist-1")
    assert s1.worker_job_id == "job-hist-1"
    assert s1.compiled_context_hash == "ctx-hash-hist-1"
    assert s1.prompt_hash == "prompt-hash-hist-1"

    s2 = next(s for s in history.supplies if s.supply_id == "sup-hist-2")
    assert s2.worker_job_id is None
    assert s2.compiled_context_hash is None
    assert s2.prompt_hash is None
