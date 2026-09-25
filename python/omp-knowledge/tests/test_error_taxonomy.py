from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import omp_knowledge
from omp_knowledge.errors import (
    ERROR_TAXONOMY,
    CancelledError,
    ContractMismatchError,
    EngineTimeoutError,
    EngineUnavailableError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidRequestError,
    KnowledgeError,
    ScopeViolationError,
    SnapshotNotPublishedError,
    UnauthenticatedError,
)
from omp_work.knowledge_contracts import (
    ALLOWED_EXTRACTION_STATES,
    ALLOWED_JOB_STATES,
    IngestionJob,
    IngestionReceipt,
)


def test_explicit_error_taxonomy_coverage() -> None:
    """Explicit error taxonomy must cover all required failure modes with exact status codes:
    invalid_request (400), unauthenticated (401), forbidden (403),
    idempotency_conflict (409), scope_violation (403), snapshot_not_published (400),
    engine_unavailable (503), engine_timeout (504), cancelled (409), contract_mismatch (400).
    """
    expected_taxonomy = {
        "invalid_request": (InvalidRequestError, 400),
        "unauthenticated": (UnauthenticatedError, 401),
        "forbidden": (ForbiddenError, 403),
        "idempotency_conflict": (IdempotencyConflictError, 409),
        "scope_violation": (ScopeViolationError, 403),
        "snapshot_not_published": (SnapshotNotPublishedError, 400),
        "engine_unavailable": (EngineUnavailableError, 503),
        "engine_timeout": (EngineTimeoutError, 504),
        "cancelled": (CancelledError, 409),
        "contract_mismatch": (ContractMismatchError, 400),
    }

    assert set(ERROR_TAXONOMY.keys()) == set(expected_taxonomy.keys())

    for code, (cls, expected_status) in expected_taxonomy.items():
        assert issubclass(cls, KnowledgeError)
        assert cls.code == code
        assert cls.status_code == expected_status
        assert ERROR_TAXONOMY[code] is cls


def test_idempotency_conflict_carries_operation_and_both_digests() -> None:
    """idempotency_conflict (409) carries operation_id and both current and existing digests."""
    op_id = uuid4()
    current_hash = "a" * 64
    existing_hash = "b" * 64

    err = IdempotencyConflictError(op_id, current_hash, existing_hash)
    assert err.code == "idempotency_conflict"
    assert err.status_code == 409
    assert err.operation_id == op_id
    assert err.current_hash == current_hash
    assert err.existing_hash == existing_hash
    assert str(op_id) in str(err)
    assert current_hash in str(err)
    assert existing_hash in str(err)
    assert err.details["current_hash"] == current_hash
    assert err.details["existing_hash"] == existing_hash


def test_snapshot_not_published_carries_snapshot_id() -> None:
    """snapshot_not_published carries the target snapshot_id."""
    snap_id = "c" * 64
    err = SnapshotNotPublishedError(snap_id)
    assert err.code == "snapshot_not_published"
    assert err.status_code == 400
    assert err.snapshot_id == snap_id
    assert snap_id in str(err)


def test_extraction_state_no_lesson_distinct_from_failed() -> None:
    """Explicit extraction state: no_lesson represents a zero-fact extraction success,
    completely distinct from a job failure state.
    """
    assert "no_lesson" in ALLOWED_EXTRACTION_STATES
    assert "extracted" in ALLOWED_EXTRACTION_STATES
    assert "failed" in ALLOWED_JOB_STATES
    assert "no_lesson" not in ALLOWED_JOB_STATES
    assert "failed" not in ALLOWED_EXTRACTION_STATES

    job_id = uuid4()
    receipt_id = uuid4()
    snap_id = "d" * 64

    # Successful extraction with zero lessons
    receipt_no_lesson = IngestionReceipt(
        receipt_id=receipt_id,
        job_id=job_id,
        snapshot_id=snap_id,
        extraction_state="no_lesson",
        fact_count=0,
        graph_sha256="0" * 64,
    )
    assert receipt_no_lesson.extraction_state == "no_lesson"
    assert receipt_no_lesson.fact_count == 0

    # fact_count == 0 forbids extraction_state='extracted'
    with pytest.raises(ValidationError, match="fact_count 0 requires extraction_state='no_lesson'"):
        IngestionReceipt(
            receipt_id=receipt_id,
            job_id=job_id,
            snapshot_id=snap_id,
            extraction_state="extracted",
            fact_count=0,
            graph_sha256="0" * 64,
        )

    # fact_count > 0 requires extraction_state='extracted'
    with pytest.raises(ValidationError, match="requires extraction_state='extracted'"):
        IngestionReceipt(
            receipt_id=receipt_id,
            job_id=job_id,
            snapshot_id=snap_id,
            extraction_state="no_lesson",
            fact_count=5,
            graph_sha256="0" * 64,
        )

    # Failed job requires error_code; no_lesson is not a job state
    with pytest.raises(ValidationError, match="error_code must be provided when state is 'failed'"):
        IngestionJob(
            job_id=job_id,
            workspace_id=uuid4(),
            repository_id=uuid4(),
            snapshot_id=snap_id,
            idempotency_key="key-1",
            request_sha256="e" * 64,
            state="failed",
            error_code=None,
        )


def test_no_null_engine_under_src() -> None:
    """Architectural constraint: No null engine under src/; null engine lives only under tests/support/."""
    # 1. Verify src/omp_knowledge has no NullEngine
    assert not hasattr(omp_knowledge, "NullEngine")

    src_dir = Path(omp_knowledge.__file__).resolve().parent
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "class NullEngine" not in text, f"NullEngine found under production source: {py_file}"

    # 2. Verify NullEngine is properly located under tests/support/
    from support.null_engine import NullEngine

    assert inspect.isclass(NullEngine)
    assert NullEngine.__module__ == "support.null_engine"
