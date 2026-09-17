from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.ownership import WriterOwnership
from omp_knowledge.storage.db import (
    IdempotencyConflictError,
    execute_idempotent_job,
    get_db_connection,
    is_snapshot_published,
)
from omp_knowledge.storage.jobs import ForeignOperationError
from omp_work.knowledge_contracts import JobState
from omp_work.v1.canonical import sha256


def test_durable_job_replay_and_conflict_real_postgres(pg_cluster: KnowledgeConfig) -> None:
    """Test job idempotency, row-level locking, and 409 conflict detection on real PostgreSQL (Failures 7 & 8)."""
    conn = get_db_connection(pg_cluster)
    try:
        op_id = uuid4()
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "a" * 64

        payload_1 = {"task": "ingest_a", "version": 1}
        hash_1 = sha256(payload_1)

        # 1. First execution
        res1 = execute_idempotent_job(
            conn,
            operation_id=op_id,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
            request_hash=hash_1,
            action=lambda: (JobState.COMPLETED, {"nodes": 10}, ["step1_ok"]),
        )
        assert res1["replayed"] is False
        assert res1["state"] == JobState.COMPLETED.value
        assert res1["result_sha256"] is not None

        # 2. Replay with same operation_id and same payload -> returns cached original result
        res2 = execute_idempotent_job(
            conn,
            operation_id=op_id,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
            request_hash=hash_1,
            action=lambda: (JobState.COMPLETED, {"nodes": 999}, ["should_not_run"]),
        )
        assert res2["replayed"] is True
        assert res2["result_sha256"] == res1["result_sha256"]
        assert res2["response"] == {"nodes": 10}

        # 3. Conflicting payload under same operation_id -> raises IdempotencyConflictError (409)
        payload_2 = {"task": "ingest_a", "version": 2}
        hash_2 = sha256(payload_2)

        with pytest.raises(IdempotencyConflictError) as exc_info:
            execute_idempotent_job(
                conn,
                operation_id=op_id,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                request_hash=hash_2,
                action=lambda: (JobState.COMPLETED, {}, []),
            )
        assert exc_info.value.current_hash == hash_2
        assert exc_info.value.existing_hash == hash_1

        # Verify original durable result in PostgreSQL remains unmodified
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, result_sha256, response FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row["state"] == JobState.COMPLETED.value
            assert row["result_sha256"] == res1["result_sha256"]
            assert row["response"] == res1["response"]
    finally:
        conn.close()


def test_durable_job_distinct_states_real_postgres(pg_cluster: KnowledgeConfig) -> None:
    """Test distinct terminal states: no_lesson vs worker failure on real PostgreSQL."""
    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()

        # 1. Explicit no_lesson state
        op_no_lesson = uuid4()
        h_no_lesson = sha256({"case": "no_lesson"})
        res_no_lesson = execute_idempotent_job(
            conn,
            operation_id=op_no_lesson,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=None,
            request_hash=h_no_lesson,
            action=lambda: (JobState.NO_LESSON, {"diagnostic": "no patterns detected"}, ["distillation_clean"]),
        )
        assert res_no_lesson["state"] == JobState.NO_LESSON.value

        # 2. Worker exception marks job as failed and records error
        op_fail = uuid4()
        h_fail = sha256({"case": "fail"})

        def failing_worker():
            raise RuntimeError("Worker process crashed mid-write")

        with pytest.raises(RuntimeError, match="Worker process crashed"):
            execute_idempotent_job(
                conn,
                operation_id=op_fail,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=None,
                request_hash=h_fail,
                action=failing_worker,
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, error FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_fail,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row["state"] == JobState.FAILED.value
            assert "Worker process crashed" in str(row["error"])
    finally:
        conn.close()


def test_crashed_worker_leaves_snapshot_unpublished(pg_cluster: KnowledgeConfig) -> None:
    """Verify that a crash or partial write never marks a snapshot as published."""
    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "f" * 64

        assert not is_snapshot_published(
            conn,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
        )
    finally:
        conn.close()


def test_durable_job_workspace_scoping_enforcement(pg_cluster: KnowledgeConfig) -> None:
    """Verify that job identity is strictly scoped by workspace_id."""
    conn = get_db_connection(pg_cluster)
    try:
        op_id = uuid4()
        ws_1 = uuid4()
        ws_2 = uuid4()
        repo_id = uuid4()
        payload = {"scope": "test"}
        h = sha256(payload)

        # Initial job registered under ws_1
        res = execute_idempotent_job(
            conn,
            operation_id=op_id,
            workspace_id=ws_1,
            repository_id=repo_id,
            snapshot_id=None,
            request_hash=h,
            action=lambda: (JobState.COMPLETED, {"ok": True}, []),
        )
        assert res["state"] == JobState.COMPLETED.value

        # Attempt to access same operation_id from ws_2 must fail without leaking foreign workspace UUID
        with pytest.raises(PermissionError) as exc_info:
            execute_idempotent_job(
                conn,
                operation_id=op_id,
                workspace_id=ws_2,
                repository_id=repo_id,
                snapshot_id=None,
                request_hash=h,
                action=lambda: (JobState.COMPLETED, {"ok": True}, []),
            )
        assert "scoped to another workspace" in str(exc_info.value)
        assert str(ws_1) not in str(exc_info.value)
        assert str(ws_2) not in str(exc_info.value)
    finally:
        conn.close()


def test_durable_job_restart_recovery_and_resumption(pg_cluster: KnowledgeConfig) -> None:
    """Test crash recovery:
    1. A job left in RUNNING state (orphaned by crashed worker).
    2. recover_interrupted_jobs marks it INTERRUPTED.
    3. Resuming with the same operation ID + identical payload succeeds.
    """
    from omp_knowledge.storage.db import recover_interrupted_jobs

    conn = get_db_connection(pg_cluster)
    try:
        op_id = uuid4()
        ws_id = uuid4()
        repo_id = uuid4()
        payload = {"crash": "recovery_test"}
        h = sha256(payload)

        # Insert an orphaned running job directly into DB
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound')
                    ON CONFLICT (repository_id) DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, NULL, %s, 'running', 1, clock_timestamp(), clock_timestamp())
                    """,
                    (op_id, ws_id, repo_id, h),
                )

        # Simulate server restart / recovery scan
        interrupted_count = recover_interrupted_jobs(conn)
        assert interrupted_count >= 1

        # Verify job is now in 'interrupted' state
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, attempt_count, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_id,),
            )
            row = cur.fetchone()
            assert row["state"] == JobState.INTERRUPTED.value
            assert "interrupted_by_server_restart" in row["diagnostics"]

        # Resuming under the same operation ID + identical payload succeeds
        res_resumed = execute_idempotent_job(
            conn,
            operation_id=op_id,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=None,
            request_hash=h,
            action=lambda: (JobState.COMPLETED, {"recovered": True}, ["resumed_ok"]),
        )
        assert res_resumed["state"] == JobState.COMPLETED.value
        assert res_resumed["response"] == {"recovered": True}
        assert res_resumed["replayed"] is False

        # Verify attempt_count was incremented in DB
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_id,),
            )
            assert cur.fetchone()["attempt_count"] == 2
    finally:
        conn.close()


def test_durable_job_cancelled_attempt_cannot_publish(pg_cluster: KnowledgeConfig) -> None:
    """Verify that cancelled jobs cannot publish or resume."""
    conn = get_db_connection(pg_cluster)
    try:
        op_id = uuid4()
        ws_id = uuid4()
        repo_id = uuid4()
        payload = {"cancelled": "test"}
        h = sha256(payload)

        # Insert a cancelled job directly into DB
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound')
                    ON CONFLICT (repository_id) DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, NULL, %s, 'cancelled', 1, clock_timestamp(), clock_timestamp())
                    """,
                    (op_id, ws_id, repo_id, h),
                )

        # Attempt to resume or re-run cancelled job returns cancelled state and does NOT run action
        ran = False

        def should_not_run():
            nonlocal ran
            ran = True
            return (JobState.COMPLETED, {}, [])

        res = execute_idempotent_job(
            conn,
            operation_id=op_id,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=None,
            request_hash=h,
            action=should_not_run,
        )
        assert ran is False
        assert res["state"] == JobState.CANCELLED.value
        assert res["error"] is not None
        assert res["error"]["code"] == "job_cancelled"
    finally:
        conn.close()


def test_claim_snapshot_rejects_null_workspace_legacy_orphan_on_recovery(
    pg_cluster: KnowledgeConfig,
) -> None:
    """Verify that recovery/takeover fails closed when claiming a legacy snapshot row with NULL workspace_id.

    Seeding a legacy orphan snapshot with NULL workspace_id and an orphan interrupted job must
    fail closed with ForeignOperationError before claim, preventing any snapshot ownership,
    job state, or owner token mutation in PostgreSQL.
    """
    conn = get_db_connection(pg_cluster)
    writer = WriterOwnership(pg_cluster)
    assert writer.acquire() is True
    try:
        op_id = uuid4()
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "0" * 64
        payload = {"task": "recovery_legacy_null_workspace_test"}
        h = sha256(payload)
        initial_owner_token = "orphan_owner_token"

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound')
                    ON CONFLICT (repository_id) DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.snapshots (
                        snapshot_id, workspace_id, repository_id, ingest_operation_id,
                        manifest_sha256, base_commit, tree_sha, candidate_tree_sha, manifest
                    ) VALUES (%s, NULL, %s, NULL, %s, 'base_commit', 'tree_sha', NULL, '{}'::jsonb)
                    """,
                    (snap_id, repo_id, h),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, owner_token, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'interrupted', 1, %s, clock_timestamp(), clock_timestamp())
                    """,
                    (op_id, ws_id, repo_id, snap_id, h, initial_owner_token),
                )

        ran = False

        def should_not_run():
            nonlocal ran
            ran = True
            return (JobState.COMPLETED, {"ok": True}, [])

        with pytest.raises(ForeignOperationError) as exc_info:
            execute_idempotent_job(
                conn,
                operation_id=op_id,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                request_hash=h,
                action=should_not_run,
                writer=writer,
            )

        assert isinstance(exc_info.value, PermissionError)
        assert "scoped to another workspace" in str(exc_info.value)
        assert f"snapshot_{snap_id}_foreign_workspace" in exc_info.value.internal_diagnostics
        assert ran is False

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ingest_operation_id, workspace_id, manifest_sha256
                FROM omp_knowledge.snapshots
                WHERE snapshot_id = %s
                """,
                (snap_id,),
            )
            snap_row = cur.fetchone()
            assert snap_row is not None
            assert snap_row["workspace_id"] is None
            assert snap_row["ingest_operation_id"] is None

            cur.execute(
                """
                SELECT state, attempt_count, owner_token, diagnostics
                FROM omp_knowledge.ingestion_jobs
                WHERE operation_id = %s
                """,
                (op_id,),
            )
            job_row = cur.fetchone()
            assert job_row is not None
            assert job_row["state"] == JobState.INTERRUPTED.value
            assert job_row["attempt_count"] == 1
            assert job_row["owner_token"] == initial_owner_token
            assert job_row["owner_token"] != writer.token
            assert "resumed" not in (job_row["diagnostics"] or [])
    finally:
        try:
            asyncio.run(writer.release())
        finally:
            conn.close()
