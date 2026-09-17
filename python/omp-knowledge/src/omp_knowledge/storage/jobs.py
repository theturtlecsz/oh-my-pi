from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Iterable
from uuid import UUID

import psycopg

from omp_work.knowledge_contracts import JobState
from omp_work.v1.canonical import sha256

from ..models import SnapshotManifest, validate_canonical_snapshot_id
from ..ownership import WriterOwnership
from .db import IdempotencyConflictError, RebuildHashMismatchError, recover_interrupted_jobs

logger = logging.getLogger(__name__)


class ForeignOperationError(PermissionError):
    """Generic 404/403 error without tenant or cross-workspace details."""

    def __init__(
        self,
        message: str = "Operation is scoped to another workspace",
        *,
        internal_diagnostics: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.internal_diagnostics = internal_diagnostics or []


class SnapshotConflictError(IdempotencyConflictError):
    """Snapshot conflict carrying the real canonical operation ID, never a random UUID."""

    def __init__(
        self,
        snapshot_id: str,
        canonical_operation_id: UUID,
        detail: str | None = None,
        *,
        current_hash: str | None = None,
        existing_hash: str | None = None,
    ) -> None:
        msg = (
            detail
            if detail is not None
            else f"Snapshot {snapshot_id} is owned by canonical operation {canonical_operation_id}"
        )
        super().__init__(
            operation_id=canonical_operation_id,
            current_hash=current_hash or "",
            existing_hash=existing_hash or "",
        )
        self.snapshot_id = snapshot_id
        self.canonical_operation_id = canonical_operation_id
        self.operation_id = canonical_operation_id
        self.detail = msg


def _normalize_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def _manifest_digest(snapshot_manifest: SnapshotManifest) -> tuple[dict[str, Any], str]:
    """Durable manifest projection and its sha256, identical for reservation and resume checks."""
    manifest_dict = snapshot_manifest.model_dump(mode="json", by_alias=True)
    for key in ("schema", "receipt_output_hashes", "unretained_outputs"):
        manifest_dict.pop(key, None)
    if manifest_dict.get("source_manifest") is None:
        manifest_dict.pop("source_manifest", None)
    return manifest_dict, sha256(manifest_dict)


def _reserve_snapshot(
    cur: psycopg.Cursor,
    *,
    snapshot_id: str,
    workspace_id: UUID,
    repository_id: UUID,
    operation_id: UUID,
    snapshot_manifest: SnapshotManifest,
    manifest_dict: dict[str, Any],
    manifest_sha: str,
) -> None:
    cur.execute(
        """
        INSERT INTO omp_knowledge.snapshots (
            snapshot_id, workspace_id, repository_id,
            ingest_operation_id, manifest_sha256,
            base_commit, tree_sha, candidate_tree_sha, manifest,
            created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
        ON CONFLICT (snapshot_id) DO NOTHING
        """,
        (
            snapshot_id,
            workspace_id,
            repository_id,
            operation_id,
            manifest_sha,
            snapshot_manifest.snapshot_ref.base_commit,
            snapshot_manifest.snapshot_ref.tree_sha,
            snapshot_manifest.snapshot_ref.candidate_tree_sha,
            json.dumps(manifest_dict),
        ),
    )


def _claim_snapshot_for_resume(
    cur: psycopg.Cursor,
    *,
    snapshot_id: str,
    workspace_id: UUID,
    repository_id: UUID,
    operation_id: UUID,
    snapshot_manifest: SnapshotManifest | None,
) -> None:
    """Snapshot ownership CAS for a resumed/taken-over job.

    A prior attempt may have died before reserving the snapshot row (reservation
    missing) or after staging created it without an owner (ingest_operation_id
    NULL). Both are claimed here so the completion CAS sees this operation as the
    snapshot owner. A row owned by another operation, or carrying a different
    manifest, is a conflict exactly as before.
    """
    cur.execute(
        """
        SELECT ingest_operation_id, manifest_sha256, workspace_id, repository_id
        FROM omp_knowledge.snapshots
        WHERE snapshot_id = %s
        FOR UPDATE
        """,
        (snapshot_id,),
    )
    snap = cur.fetchone()
    manifest_dict, manifest_sha = _manifest_digest(snapshot_manifest) if snapshot_manifest is not None else ({}, None)

    if snap is None:
        if snapshot_manifest is not None:
            _reserve_snapshot(
                cur,
                snapshot_id=snapshot_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
                operation_id=operation_id,
                snapshot_manifest=snapshot_manifest,
                manifest_dict=manifest_dict,
                manifest_sha=manifest_sha,
            )
        return

    if snap.get("workspace_id") is None or snap["workspace_id"] != workspace_id:
        raise ForeignOperationError(
            "Operation is scoped to another workspace",
            internal_diagnostics=[
                f"snapshot_{snapshot_id}_foreign_workspace"
            ],
        )
    if snap.get("repository_id") is not None and snap["repository_id"] != repository_id:
        raise ValueError(f"Snapshot {snapshot_id} repository mismatch")

    if snap["ingest_operation_id"] not in (None, operation_id):
        raise SnapshotConflictError(
            snapshot_id=snapshot_id,
            canonical_operation_id=snap["ingest_operation_id"],
            detail=f"Operation {operation_id} no longer owns snapshot {snapshot_id}; owned by {snap['ingest_operation_id']}",
        )
    if manifest_sha is not None and snap["manifest_sha256"] not in (None, manifest_sha):
        raise SnapshotConflictError(
            snapshot_id=snapshot_id,
            canonical_operation_id=snap["ingest_operation_id"] or operation_id,
            detail=f"Snapshot {snapshot_id} manifest mismatch: existing={snap['manifest_sha256']}, candidate={manifest_sha}",
            current_hash=manifest_sha,
            existing_hash=snap["manifest_sha256"],
        )
    if snap["ingest_operation_id"] is None:
        cur.execute(
            """
            UPDATE omp_knowledge.snapshots
            SET ingest_operation_id = %s,
                manifest_sha256 = COALESCE(manifest_sha256, %s)
            WHERE snapshot_id = %s AND ingest_operation_id IS NULL
            """,
            (operation_id, manifest_sha, snapshot_id),
        )


def _project_job(
    job_row: dict[str, Any],
    canon_row: dict[str, Any] | None = None,
    *,
    replayed: bool = True,
    override_state: str | None = None,
    override_response: Any = None,
    override_result_sha256: str | None = None,
    override_error: Any = None,
    override_diagnostics: list[str] | None = None,
) -> dict[str, Any]:
    op_id = job_row["operation_id"]
    canon_op_id = job_row.get("canonical_operation_id")

    if override_state is not None:
        return {
            "operation_id": op_id,
            "canonical_operation_id": canon_op_id,
            "state": override_state,
            "replayed": replayed,
            "result_sha256": override_result_sha256,
            "response": _normalize_json(override_response),
            "error": _normalize_json(override_error),
            "diagnostics": override_diagnostics or [],
        }

    if canon_row is not None:
        if job_row.get("state") == JobState.CANCELLED.value:
            err = _normalize_json(job_row.get("error")) or {
                "code": "job_cancelled",
                "message": "Job was cancelled and cannot publish",
            }
            return {
                "operation_id": op_id,
                "canonical_operation_id": canon_op_id,
                "state": JobState.CANCELLED.value,
                "replayed": replayed,
                "result_sha256": None,
                "response": None,
                "error": err,
                "diagnostics": override_diagnostics if override_diagnostics is not None else list(job_row.get("diagnostics") or []),
            }
        return {
            "operation_id": op_id,
            "canonical_operation_id": canon_op_id,
            "state": canon_row["state"],
            "replayed": replayed,
            "result_sha256": canon_row.get("result_sha256"),
            "response": _normalize_json(canon_row.get("response")),
            "error": _normalize_json(canon_row.get("error")),
            "diagnostics": override_diagnostics if override_diagnostics is not None else list(canon_row.get("diagnostics") or []),
        }

    curr_state = job_row["state"]
    curr_error = _normalize_json(job_row.get("error"))
    if curr_state == JobState.CANCELLED.value and curr_error is None:
        curr_error = {
            "code": "job_cancelled",
            "message": "Job was cancelled and cannot publish",
        }

    return {
        "operation_id": op_id,
        "canonical_operation_id": canon_op_id,
        "state": curr_state,
        "replayed": replayed,
        "result_sha256": job_row.get("result_sha256"),
        "response": _normalize_json(job_row.get("response")),
        "error": curr_error,
        "diagnostics": override_diagnostics if override_diagnostics is not None else list(job_row.get("diagnostics") or []),
    }


async def run_operation(
    conn: psycopg.Connection,
    *,
    writer: WriterOwnership | None = None,
    operation_id: UUID,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str | None,
    request_hash: str,
    action: Callable[[], Any],
    commit: Callable[[psycopg.Cursor, Any], tuple[JobState, dict[str, Any], list[str]]] | None = None,
    snapshot_manifest: SnapshotManifest | None = None,
    skip_snapshot_owner_cas: bool = False,
    skip_ingest_snapshot_owner_cas: bool = False,
) -> dict[str, Any]:
    if snapshot_id is not None:
        snapshot_id = validate_canonical_snapshot_id(snapshot_id)

    early_return: dict[str, Any] | None = None
    claimed_attempt: int = 1
    takeover = False
    owner_token: str | None = writer.token if writer is not None else None

    # 1. Admission short transaction
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.repositories (repository_id, state)
                VALUES (%s, 'unbound')
                ON CONFLICT (repository_id) DO NOTHING
                """,
                (repository_id,),
            )

            cur.execute(
                """
                INSERT INTO omp_knowledge.ingestion_jobs (
                    operation_id, workspace_id, repository_id, snapshot_id,
                    request_sha256, state, attempt_count, owner_token, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, clock_timestamp(), clock_timestamp())
                ON CONFLICT (operation_id) DO NOTHING
                RETURNING operation_id
                """,
                (
                    operation_id,
                    workspace_id,
                    repository_id,
                    snapshot_id,
                    request_hash,
                    JobState.RUNNING.value,
                    owner_token,
                ),
            )
            fresh_row = cur.fetchone()
            is_fresh = fresh_row is not None

            cur.execute(
                """
                SELECT operation_id, workspace_id, repository_id, snapshot_id,
                       request_sha256, state, attempt_count, canonical_operation_id,
                       owner_token, result_sha256, response, error, diagnostics
                FROM omp_knowledge.ingestion_jobs
                WHERE operation_id = %s
                FOR UPDATE
                """,
                (operation_id,),
            )
            job = cur.fetchone()
            if not job:
                raise RuntimeError(f"Unexpected: job {operation_id} not found after admission insert")

            if job["workspace_id"] != workspace_id:
                logger.warning(
                    "Cross-tenant admission rejected: operation %s belongs to foreign workspace %s, requested by %s",
                    operation_id,
                    job["workspace_id"],
                    workspace_id,
                )
                raise ForeignOperationError(
                    "Operation is scoped to another workspace",
                    internal_diagnostics=[
                        f"operation_{operation_id}_foreign_workspace"
                    ],
                )

            if job["request_sha256"] != request_hash:
                raise IdempotencyConflictError(operation_id, request_hash, job["request_sha256"])

            if not is_fresh:
                if job["canonical_operation_id"] is not None:
                    cur.execute(
                        """
                        SELECT operation_id, workspace_id, repository_id, snapshot_id,
                               request_sha256, state, attempt_count, canonical_operation_id,
                               owner_token, result_sha256, response, error, diagnostics
                        FROM omp_knowledge.ingestion_jobs
                        WHERE operation_id = %s
                        FOR UPDATE
                        """,
                        (job["canonical_operation_id"],),
                    )
                    canon_job = cur.fetchone()
                    if not canon_job or canon_job["workspace_id"] != workspace_id:
                        raise ForeignOperationError("Operation not found")
                    early_return = _project_job(job, canon_job, replayed=True)
                else:
                    curr_state = job["state"]
                    if curr_state in (
                        JobState.COMPLETED.value,
                        JobState.NO_LESSON.value,
                    ):
                        early_return = _project_job(job, replayed=True)
                    elif curr_state == JobState.CANCELLED.value:
                        # Cancelled jobs are terminal! Cannot resume or publish
                        early_return = _project_job(job, replayed=True)
                    elif curr_state == JobState.RUNNING.value:
                        # Live job owned by this writer (or writer-less caller): report in progress.
                        # Any other owner token (or NULL) belongs to a writer that lost the single-writer
                        # lock, because only one WriterOwnership can be acquired at a time.
                        if writer is None or job.get("owner_token") == writer.token:
                            early_return = _project_job(
                                job,
                                replayed=True,
                                override_diagnostics=list(job.get("diagnostics") or []) or ["job_in_progress"],
                            )
                        else:
                            takeover = True
                    elif curr_state in (
                        JobState.FAILED.value,
                        JobState.INTERRUPTED.value,
                        JobState.PARTIAL.value,
                    ):
                        takeover = True
                    else:
                        claimed_attempt = job["attempt_count"]

                    if takeover:
                        if snapshot_id is not None and not (skip_snapshot_owner_cas or skip_ingest_snapshot_owner_cas):
                            _claim_snapshot_for_resume(
                                cur,
                                snapshot_id=snapshot_id,
                                workspace_id=workspace_id,
                                repository_id=repository_id,
                                operation_id=operation_id,
                                snapshot_manifest=snapshot_manifest,
                            )
                        claimed_attempt = job["attempt_count"] + 1
                        cur.execute(
                            """
                            UPDATE omp_knowledge.ingestion_jobs
                            SET attempt_count = %s,
                                state = %s,
                                owner_token = %s,
                                error = NULL,
                                diagnostics = array_append(diagnostics, 'resumed'),
                                updated_at = clock_timestamp()
                            WHERE operation_id = %s
                            """,
                            (claimed_attempt, JobState.RUNNING.value, owner_token, operation_id),
                        )
            else:
                claimed_attempt = 1

                if snapshot_id is not None and snapshot_manifest is not None:
                    manifest_dict, manifest_sha = _manifest_digest(snapshot_manifest)
                    _reserve_snapshot(
                        cur,
                        snapshot_id=snapshot_id,
                        workspace_id=workspace_id,
                        repository_id=repository_id,
                        operation_id=operation_id,
                        snapshot_manifest=snapshot_manifest,
                        manifest_dict=manifest_dict,
                        manifest_sha=manifest_sha,
                    )

                    cur.execute(
                        """
                        SELECT snapshot_id, workspace_id, repository_id,
                               ingest_operation_id, manifest_sha256
                        FROM omp_knowledge.snapshots
                        WHERE snapshot_id = %s
                        FOR UPDATE
                        """,
                        (snapshot_id,),
                    )
                    snap = cur.fetchone()
                    if not snap:
                        raise RuntimeError(f"Unexpected: snapshot {snapshot_id} not found after reservation insert")

                    if snap["workspace_id"] != workspace_id:
                        logger.warning(
                            "Cross-tenant snapshot reservation rejected: snapshot %s belongs to foreign workspace %s, requested by %s",
                            snapshot_id,
                            snap["workspace_id"],
                            workspace_id,
                        )
                        raise ForeignOperationError(
                            "Operation is scoped to another workspace",
                            internal_diagnostics=[
                                f"snapshot_{snapshot_id}_foreign_workspace"
                            ],
                        )
                    if snap["repository_id"] != repository_id:
                        raise ValueError(f"Snapshot {snapshot_id} repository mismatch")

                    if snap["manifest_sha256"] != manifest_sha:
                        raise SnapshotConflictError(
                            snapshot_id=snapshot_id,
                            canonical_operation_id=snap["ingest_operation_id"],
                            detail=f"Snapshot {snapshot_id} manifest mismatch: existing={snap['manifest_sha256']}, current={manifest_sha}",
                            current_hash=manifest_sha,
                            existing_hash=snap["manifest_sha256"],
                        )

                    if snap["ingest_operation_id"] != operation_id:
                        canon_op_id = snap["ingest_operation_id"]
                        cur.execute(
                            """
                            SELECT operation_id, workspace_id, repository_id, snapshot_id,
                                   request_sha256, state, attempt_count, canonical_operation_id,
                                   owner_token, result_sha256, response, error, diagnostics
                            FROM omp_knowledge.ingestion_jobs
                            WHERE operation_id = %s
                            FOR UPDATE
                            """,
                            (canon_op_id,),
                        )
                        canon_job = cur.fetchone()
                        if not canon_job or canon_job["workspace_id"] != workspace_id:
                            raise ForeignOperationError("Operation not found")

                        real_canon_id = canon_job["canonical_operation_id"] or canon_job["operation_id"]
                        if real_canon_id != canon_job["operation_id"]:
                            cur.execute(
                                """
                                SELECT operation_id, workspace_id, repository_id, snapshot_id,
                                       request_sha256, state, attempt_count, canonical_operation_id,
                                       owner_token, result_sha256, response, error, diagnostics
                                FROM omp_knowledge.ingestion_jobs
                                WHERE operation_id = %s
                                FOR UPDATE
                                """,
                                (real_canon_id,),
                            )
                            canon_job = cur.fetchone()
                            if not canon_job or canon_job["workspace_id"] != workspace_id:
                                raise ForeignOperationError("Operation not found")

                        canon_state = canon_job["state"]
                        if canon_state in (
                            JobState.RUNNING.value,
                            JobState.COMPLETED.value,
                            JobState.NO_LESSON.value,
                        ):
                            cur.execute(
                                """
                                UPDATE omp_knowledge.ingestion_jobs
                                SET canonical_operation_id = %s,
                                    state = %s,
                                    result_sha256 = %s,
                                    response = %s,
                                    error = %s,
                                    diagnostics = array_append(diagnostics, 'alias_created'),
                                    updated_at = clock_timestamp()
                                WHERE operation_id = %s
                                """,
                                (
                                    canon_job["operation_id"],
                                    canon_state,
                                    canon_job["result_sha256"],
                                    json.dumps(canon_job["response"]) if isinstance(canon_job["response"], (dict, list)) else canon_job["response"],
                                    json.dumps(canon_job["error"]) if isinstance(canon_job["error"], (dict, list)) else canon_job["error"],
                                    operation_id,
                                ),
                            )
                            job["canonical_operation_id"] = canon_job["operation_id"]
                            job["state"] = canon_state
                            early_return = _project_job(job, canon_job, replayed=True)
                        elif canon_state in (
                            JobState.FAILED.value,
                            JobState.INTERRUPTED.value,
                            JobState.PARTIAL.value,
                        ):
                            # Takeover only after lock loss!
                            cur.execute(
                                """
                                UPDATE omp_knowledge.snapshots
                                SET ingest_operation_id = %s
                                WHERE snapshot_id = %s
                                """,
                                (operation_id, snapshot_id),
                            )
                        elif canon_state == JobState.CANCELLED.value:
                            # Cancelled jobs are terminal! Cannot take over
                            err = {
                                "code": "job_cancelled",
                                "message": "Canonical snapshot job was cancelled and cannot publish",
                            }
                            early_return = _project_job(
                                job,
                                replayed=True,
                                override_state=JobState.CANCELLED.value,
                                override_error=err,
                            )

    if early_return is not None:
        return early_return

    def _record_failure(exc: Exception) -> None:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state, attempt_count, owner_token
                    FROM omp_knowledge.ingestion_jobs
                    WHERE operation_id = %s
                    FOR UPDATE
                    """,
                    (operation_id,),
                )
                curr = cur.fetchone()
                if (
                    curr
                    and curr["state"] == JobState.RUNNING.value
                    and curr["attempt_count"] == claimed_attempt
                    and (owner_token is None or curr.get("owner_token") == owner_token)
                ):
                    err_payload = {"type": type(exc).__name__, "message": str(exc)}
                    cur.execute(
                        """
                        UPDATE omp_knowledge.ingestion_jobs
                        SET state = %s,
                            error = %s,
                            diagnostics = array_append(diagnostics, %s),
                            updated_at = clock_timestamp()
                        WHERE operation_id = %s
                          AND state = %s
                          AND attempt_count = %s
                          AND (owner_token = %s OR owner_token IS NULL)
                        """,
                        (
                            JobState.FAILED.value,
                            json.dumps(err_payload),
                            "rebuild_hash_mismatch" if (isinstance(exc, RebuildHashMismatchError) or "rebuild_hash_mismatch" in str(exc)) else str(exc)[:256],
                            operation_id,
                            JobState.RUNNING.value,
                            claimed_attempt,
                            owner_token,
                        ),
                    )

    # 2. Execute action outside DB transaction under writer.mutation() if writer provided
    try:
        if writer is not None:
            async with writer.mutation():
                res = action()
                if hasattr(res, "__await__"):
                    action_res = await res
                else:
                    action_res = res
            writer.ensure_alive()
        else:
            res = action()
            if hasattr(res, "__await__"):
                action_res = await res
            else:
                action_res = res
    except Exception as exc:
        _record_failure(exc)
        raise

    # 3. Completion transaction with claimed attempt + owner CAS
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT operation_id, workspace_id, repository_id, snapshot_id,
                           request_sha256, state, attempt_count, canonical_operation_id,
                           owner_token, result_sha256, response, error, diagnostics
                    FROM omp_knowledge.ingestion_jobs
                    WHERE operation_id = %s
                    FOR UPDATE
                    """,
                    (operation_id,),
                )
                curr_job = cur.fetchone()
                if not curr_job:
                    raise ForeignOperationError("Operation not found")
                if curr_job["workspace_id"] != workspace_id:
                    logger.warning(
                        "Cross-tenant completion rejected: operation %s belongs to foreign workspace %s, completed by %s",
                        operation_id,
                        curr_job["workspace_id"],
                        workspace_id,
                    )
                    raise ForeignOperationError(
                        "Operation is scoped to another workspace",
                        internal_diagnostics=[
                            f"operation_{operation_id}_foreign_workspace"
                        ],
                    )

                skip_owner_check = skip_snapshot_owner_cas or skip_ingest_snapshot_owner_cas
                is_snapshot_owner = True
                if snapshot_id is not None:
                    if not skip_owner_check:
                        cur.execute(
                            """
                            SELECT ingest_operation_id
                            FROM omp_knowledge.snapshots
                            WHERE snapshot_id = %s
                            FOR UPDATE
                            """,
                            (snapshot_id,),
                        )
                        snap_row = cur.fetchone()
                        if snap_row is not None:
                            snap_op_id = snap_row["ingest_operation_id"] if isinstance(snap_row, dict) else snap_row[0]
                            if snap_op_id != operation_id:
                                is_snapshot_owner = False
                    else:
                        cur.execute(
                            """
                            SELECT snapshot_id, workspace_id, repository_id
                            FROM omp_knowledge.snapshots
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            FOR UPDATE
                            """,
                            (workspace_id, repository_id, snapshot_id),
                        )
                        snap_row = cur.fetchone()
                        if not snap_row:
                            raise ValueError(f"Snapshot {snapshot_id} not found")

                # Owner/attempt/state CAS check:
                # Must still be in RUNNING state, with claimed attempt count, owner token match, and snapshot ownership
                if (
                    curr_job["state"] != JobState.RUNNING.value
                    or curr_job["attempt_count"] != claimed_attempt
                    or not is_snapshot_owner
                    or (owner_token is not None and curr_job.get("owner_token") is not None and curr_job["owner_token"] != owner_token)
                ):
                    return _project_job(curr_job, replayed=True)

                if commit is not None:
                    final_state, response_payload, diag_list = commit(cur, action_res)
                else:
                    final_state, response_payload, diag_list = action_res

                final_state_val = final_state.value if hasattr(final_state, "value") else str(final_state)
                res_sha256 = sha256(response_payload) if response_payload is not None else None
                response_json = json.dumps(response_payload) if response_payload is not None else None
                diag_list = list(diag_list) if diag_list else []

                cur.execute(
                    """
                    UPDATE omp_knowledge.ingestion_jobs
                    SET state = %s,
                        response = %s,
                        result_sha256 = %s,
                        diagnostics = array_cat(diagnostics, %s),
                        updated_at = clock_timestamp()
                    WHERE operation_id = %s
                      AND state = %s
                      AND attempt_count = %s
                      AND (owner_token = %s OR owner_token IS NULL)
                    """,
                    (
                        final_state_val,
                        response_json,
                        res_sha256,
                        diag_list[:8],
                        operation_id,
                        JobState.RUNNING.value,
                        claimed_attempt,
                        owner_token,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"Failed CAS update for operation {operation_id}: state, attempt, or owner changed concurrently"
                    )

                merged_diagnostics = list(curr_job["diagnostics"]) + diag_list[:8]
                return _project_job(
                    curr_job,
                    replayed=False,
                    override_state=final_state_val,
                    override_response=response_payload,
                    override_result_sha256=res_sha256,
                    override_error=None,
                    override_diagnostics=merged_diagnostics[:8],
                )
    except (PermissionError, ValueError) as exc:
        _record_failure(exc)
        raise


def read_operation(
    conn: psycopg.Connection,
    operation_id: UUID,
    workspace_id: UUID | Iterable[UUID],
) -> dict[str, Any] | None:
    workspaces = {workspace_id} if isinstance(workspace_id, UUID) else set(workspace_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT operation_id, workspace_id, repository_id, snapshot_id,
                   request_sha256, state, attempt_count, canonical_operation_id,
                   owner_token, result_sha256, response, error, diagnostics
            FROM omp_knowledge.ingestion_jobs
            WHERE operation_id = %s
            """,
            (operation_id,),
        )
        job = cur.fetchone()
        if not job or job["workspace_id"] not in workspaces:
            return None

        canon_job = None
        if job["canonical_operation_id"] is not None:
            cur.execute(
                """
                SELECT operation_id, workspace_id, repository_id, snapshot_id,
                       request_sha256, state, attempt_count, canonical_operation_id,
                       owner_token, result_sha256, response, error, diagnostics
                FROM omp_knowledge.ingestion_jobs
                WHERE operation_id = %s
                """,
                (job["canonical_operation_id"],),
            )
            canon_job = cur.fetchone()
            if canon_job and canon_job["workspace_id"] not in workspaces:
                return None

        return _project_job(job, canon_job, replayed=True)



__all__ = [
    "ForeignOperationError",
    "IdempotencyConflictError",
    "SnapshotConflictError",
    "read_operation",
    "recover_interrupted_jobs",
    "run_operation",
]
