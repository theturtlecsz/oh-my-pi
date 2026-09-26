from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.store import LearningStore, compute_unit_id


def test_reopened_file_store_keeps_rows(tmp_path: Path) -> None:
    db_file = tmp_path / "learning_persistent.sqlite"

    with LearningStore(db_file) as store1:
        with store1.transaction() as conn:
            conn.execute(
                """
                INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
                VALUES ('proc-1', 'fp-1', 'active', 1, 'Test Procedure', '2026-09-26T10:00:00Z', '2026-09-26T10:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO units (
                    unit_id, workspace_id, event_id, state, attempts, retryable, trace_json,
                    model, profile, source_json, created_at, updated_at
                ) VALUES (
                    'unit-1', 'ws-1', 'evt-1', 'queued', 0, 0, '{}',
                    'qwen-2.5', 'local-qwen', '{"event_id": "evt-1"}',
                    '2026-09-26T10:00:00Z', '2026-09-26T10:00:00Z'
                )
                """
            )

    with LearningStore(db_file) as store2:
        proc_row = store2.execute(
            "SELECT procedure_id, fingerprint, status, title FROM procedures WHERE procedure_id = ?",
            ("proc-1",),
        ).fetchone()
        assert proc_row is not None
        assert proc_row["procedure_id"] == "proc-1"
        assert proc_row["fingerprint"] == "fp-1"
        assert proc_row["status"] == "active"
        assert proc_row["title"] == "Test Procedure"

        unit_row = store2.execute(
            "SELECT unit_id, state, model, profile FROM units WHERE unit_id = ?",
            ("unit-1",),
        ).fetchone()
        assert unit_row is not None
        assert unit_row["unit_id"] == "unit-1"
        assert unit_row["state"] == "queued"
        assert unit_row["model"] == "qwen-2.5"
        assert unit_row["profile"] == "local-qwen"


def test_inserting_proposal_without_model_raises_integrity_error() -> None:
    store = LearningStore(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO proposals (
                proposal_id, unit_id, status, reason, lesson_json, profile, source_json, created_at
            ) VALUES (
                'prop-1', 'unit-1', 'accepted', NULL, '{"title": "Lesson"}', 'local-qwen', '{}', '2026-09-26T10:00:00Z'
            )
            """
        )


def test_inserting_proposal_with_empty_model_raises_integrity_error() -> None:
    store = LearningStore(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO proposals (
                proposal_id, unit_id, status, reason, lesson_json, model, profile, source_json, created_at
            ) VALUES (
                'prop-1', 'unit-1', 'accepted', NULL, '{"title": "Lesson"}', '', 'local-qwen', '{}', '2026-09-26T10:00:00Z'
            )
            """
        )


def test_attribution_rejects_empty_model_or_profile() -> None:
    source = SourceIdentity(
        workspace_id=str(uuid4()),
        event_id=str(uuid4()),
        event_sequence=1,
        aggregate_id=str(uuid4()),
    )

    with pytest.raises(ValidationError):
        Attribution(model="", profile="valid-profile", source=source)

    with pytest.raises(ValidationError):
        Attribution(model="valid-model", profile="", source=source)

    with pytest.raises(ValidationError):
        Attribution(model="   ", profile="valid-profile", source=source)

    with pytest.raises(ValidationError):
        Attribution(model="valid-model", profile="   ", source=source)

    valid_attr = Attribution(model="qwen-2.5", profile="local-qwen", source=source)
    assert valid_attr.model == "qwen-2.5"
    assert valid_attr.profile == "local-qwen"
    assert valid_attr.source == source


def test_attributed_tables_enforce_model_profile_source_json_constraints() -> None:
    store = LearningStore(":memory:")

    # units: model check
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO units (
                unit_id, state, trace_json, model, profile, source_json, created_at, updated_at
            ) VALUES ('u1', 'queued', '{}', '', 'prof', '{}', 'now', 'now')
            """
        )

    # units: profile check
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO units (
                unit_id, state, trace_json, model, profile, source_json, created_at, updated_at
            ) VALUES ('u1', 'queued', '{}', 'mod', '', '{}', 'now', 'now')
            """
        )

    # procedure_versions: model check
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO procedure_versions (
                procedure_id, version, title, steps_json, preconditions_json,
                model, profile, source_json, created_at
            ) VALUES ('proc1', 1, 'T', '[]', '[]', '', 'prof', '{}', 'now')
            """
        )

    # corrections: source_json check
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO corrections (
                correction_id, procedure_id, receipt_id, action, status,
                model, profile, source_json, created_at
            ) VALUES ('c1', 'proc1', 'r1', 'narrow', 'accepted', 'mod', 'prof', '', 'now')
            """
        )


def test_procedure_fingerprint_uniqueness() -> None:
    store = LearningStore(":memory:")
    store.execute(
        """
        INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
        VALUES ('proc-1', 'fp-shared', 'active', 1, 'Title 1', 'now', 'now')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO procedures (procedure_id, fingerprint, status, current_version, title, created_at, updated_at)
            VALUES ('proc-2', 'fp-shared', 'active', 1, 'Title 2', 'now', 'now')
            """
        )


def test_procedure_support_pk_deduplication() -> None:
    store = LearningStore(":memory:")
    store.execute(
        """
        INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
        VALUES ('proc-1', 'receipt-1', 'prop-1', 'now')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            """
            INSERT INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
            VALUES ('proc-1', 'receipt-1', 'prop-2', 'now')
            """
        )

    # INSERT OR IGNORE succeeds and leaves original row intact
    store.execute(
        """
        INSERT OR IGNORE INTO procedure_support (procedure_id, receipt_id, proposal_id, created_at)
        VALUES ('proc-1', 'receipt-1', 'prop-ignored', 'now')
        """
    )
    row = store.execute("SELECT proposal_id FROM procedure_support WHERE procedure_id = 'proc-1'").fetchone()
    assert row is not None
    assert row["proposal_id"] == "prop-1"


def test_cleanup_queue_defaults_to_pending_null() -> None:
    store = LearningStore(":memory:")
    store.execute(
        """
        INSERT INTO cleanup_queue (procedure_id, action, created_at)
        VALUES ('proc-1', 'withdraw', '2026-09-26T10:00:00Z')
        """
    )
    row = store.execute(
        "SELECT procedure_id, action, done_at FROM cleanup_queue WHERE procedure_id = 'proc-1'"
    ).fetchone()
    assert row is not None
    assert row["procedure_id"] == "proc-1"
    assert row["action"] == "withdraw"
    assert row["done_at"] is None


def test_transaction_commits_and_rolls_back() -> None:
    store = LearningStore(":memory:")

    with store.transaction():
        store.execute(
            """
            INSERT INTO capture_cursor (workspace_id, last_sequence, updated_at)
            VALUES ('ws-1', 42, 'now')
            """
        )

    row = store.execute("SELECT last_sequence FROM capture_cursor WHERE workspace_id = 'ws-1'").fetchone()
    assert row is not None
    assert row["last_sequence"] == 42

    with pytest.raises(RuntimeError):
        with store.transaction():
            store.execute(
                """
                UPDATE capture_cursor SET last_sequence = 99 WHERE workspace_id = 'ws-1'
                """
            )
            raise RuntimeError("intentional abort")

    row_after = store.execute("SELECT last_sequence FROM capture_cursor WHERE workspace_id = 'ws-1'").fetchone()
    assert row_after is not None
    assert row_after["last_sequence"] == 42


def test_nested_transaction_savepoints() -> None:
    store = LearningStore(":memory:")

    with store.transaction():
        store.execute(
            """
            INSERT INTO capture_cursor (workspace_id, last_sequence, updated_at)
            VALUES ('ws-1', 10, 'now')
            """
        )
        try:
            with store.transaction():
                store.execute(
                    """
                    UPDATE capture_cursor SET last_sequence = 20 WHERE workspace_id = 'ws-1'
                    """
                )
                raise ValueError("abort inner")
        except ValueError:
            pass

    row = store.execute("SELECT last_sequence FROM capture_cursor WHERE workspace_id = 'ws-1'").fetchone()
    assert row is not None
    assert row["last_sequence"] == 10


def test_models_validation_and_immutability() -> None:
    source = SourceIdentity(
        workspace_id=uuid4(),
        event_id=uuid4(),
        event_sequence=5,
        aggregate_id=uuid4(),
    )
    assert isinstance(source.workspace_id, str)

    precond = Precondition(key="repository", op="ne", value="org/repo-b")
    claim = Claim(text="Claim 1", receipt_ids=(str(uuid4()),))

    lesson = Lesson(
        title="Valid Lesson",
        steps=("step one", "step two"),
        preconditions=(precond,),
        claims=(claim,),
    )
    assert lesson.title == "Valid Lesson"
    assert len(lesson.steps) == 2
    assert len(lesson.preconditions) == 1
    assert len(lesson.claims) == 1

    # Lesson steps >= 1
    with pytest.raises(ValidationError):
        Lesson(title="Empty Steps", steps=())

    # Forbid extra fields
    with pytest.raises(ValidationError):
        Lesson(title="Extra", steps=("s1",), unexpected_field=True)  # type: ignore[call-arg]

    # Immutability
    with pytest.raises(ValidationError):
        lesson.title = "New Title"  # type: ignore[misc]


def test_compute_unit_id_deterministic() -> None:
    ws = "workspace-123"
    evt = "event-456"
    uid1 = compute_unit_id(ws, evt)
    uid2 = compute_unit_id(ws, evt)
    assert uid1 == uid2
    assert len(uid1) == 64
