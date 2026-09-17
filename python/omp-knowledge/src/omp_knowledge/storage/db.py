from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Generator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from omp_work.knowledge_contracts import (
    JobState,
    PublicationStatus,
    SnapshotPublicationReceipt,
    SnapshotRef,
    SourceObservation,
)
from omp_work.v1.canonical import canonical_json, sha256

from ..config import KnowledgeConfig
from ..models import validate_canonical_snapshot_id


class IdempotencyConflictError(Exception):
    def __init__(self, operation_id: UUID, current_hash: str, existing_hash: str) -> None:
        super().__init__(
            f"Operation {operation_id} payload conflict: existing={existing_hash}, current={current_hash}"
        )
        self.operation_id = operation_id
        self.current_hash = current_hash
        self.existing_hash = existing_hash


class SnapshotNotPublishedError(Exception):
    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"Snapshot {snapshot_id} is not published")
        self.snapshot_id = snapshot_id


class RebuildHashMismatchError(Exception):
    def __init__(self, message: str = "rebuild_hash_mismatch", expected_hash: str = "", actual_hash: str = "") -> None:
        super().__init__(message)
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash


def get_db_connection(config: KnowledgeConfig) -> psycopg.Connection:
    return psycopg.connect(config.pg_connection_string(), row_factory=dict_row)


@contextlib.contextmanager
def db_cursor(conn: psycopg.Connection) -> Generator[psycopg.Cursor, None, None]:
    with conn.cursor() as cur:
        yield cur


def apply_migrations(conn: psycopg.Connection) -> None:
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_files = sorted(migrations_dir.glob("*.sql"))
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE SCHEMA IF NOT EXISTS omp_knowledge;
            CREATE TABLE IF NOT EXISTS omp_knowledge.schema_migrations (
                ordinal integer PRIMARY KEY,
                filename text NOT NULL,
                sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            """
        )
        cur.execute("SELECT filename, sha256 FROM omp_knowledge.schema_migrations")
        applied = {row["filename"]: row["sha256"] for row in cur.fetchall()}

        for idx, migration_file in enumerate(migration_files, start=1):
            sql = migration_file.read_text(encoding="utf-8")
            file_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if migration_file.name in applied:
                continue
            cur.execute(sql)
            cur.execute(
                """
                INSERT INTO omp_knowledge.schema_migrations (ordinal, filename, sha256)
                VALUES (%s, %s, %s)
                ON CONFLICT (ordinal) DO UPDATE SET filename = EXCLUDED.filename, sha256 = EXCLUDED.sha256
                """,
                (idx, migration_file.name, file_sha),
            )
    conn.commit()



def _advisory_lock_id(val: str | UUID) -> int:
    digest = hashlib.sha256(str(val).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def execute_idempotent_job_async(
    conn: psycopg.Connection,
    *,
    operation_id: UUID,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str | None,
    request_hash: str,
    action: Callable[[], Any],
    pre_check: Callable[[psycopg.Cursor], None] | None = None,
    writer: Any = None,
    snapshot_manifest: Any = None,
    skip_snapshot_owner_cas: bool = False,
    skip_ingest_snapshot_owner_cas: bool = False,
) -> dict[str, Any]:
    """Execute an async job with atomic admission, single-writer ownership, and strict idempotence.
    Delegates to jobs.run_operation for unified writer ownership, CAS, and resumption.
    """
    from .jobs import run_operation

    if snapshot_id is not None:
        snapshot_id = validate_canonical_snapshot_id(snapshot_id)

    if pre_check is not None:
        with conn.transaction():
            with conn.cursor() as cur:
                pre_check(cur)

    return await run_operation(
        conn,
        writer=writer,
        operation_id=operation_id,
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        request_hash=request_hash,
        action=action,
        snapshot_manifest=snapshot_manifest,
        skip_snapshot_owner_cas=skip_snapshot_owner_cas,
        skip_ingest_snapshot_owner_cas=skip_ingest_snapshot_owner_cas,
    )


def execute_idempotent_job(
    conn: psycopg.Connection,
    *,
    operation_id: UUID,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str | None,
    request_hash: str,
    action: Callable[[], Any],
    pre_check: Callable[[psycopg.Cursor], None] | None = None,
    writer: Any = None,
    snapshot_manifest: Any = None,
    skip_snapshot_owner_cas: bool = False,
    skip_ingest_snapshot_owner_cas: bool = False,
) -> dict[str, Any]:
    """Synchronous entry point delegating to execute_idempotent_job_async."""
    import asyncio

    if snapshot_id is not None:
        snapshot_id = validate_canonical_snapshot_id(snapshot_id)

    return asyncio.run(
        execute_idempotent_job_async(
            conn,
            operation_id=operation_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            request_hash=request_hash,
            action=action,
            pre_check=pre_check,
            writer=writer,
            snapshot_manifest=snapshot_manifest,
            skip_snapshot_owner_cas=skip_snapshot_owner_cas,
            skip_ingest_snapshot_owner_cas=skip_ingest_snapshot_owner_cas,
        )
    )


def recover_interrupted_jobs(conn: psycopg.Connection, current_token: str | None = None) -> int:
    """Mark orphaned RUNNING jobs as INTERRUPTED on server startup/restart.
    Interrupted jobs remain resumable under the same operation ID and payload.
    Uses owner-aware owner/attempt/state CAS semantics.
    """
    diag = f"interrupted_by_recovery:{current_token}" if current_token else "interrupted_by_server_restart"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation_id, attempt_count, owner_token
                FROM omp_knowledge.ingestion_jobs
                WHERE state = 'running'
                FOR UPDATE
                """
            )
            rows = cur.fetchall()
            count = 0
            for row in rows:
                op_id = row["operation_id"] if isinstance(row, dict) else row[0]
                attempt = row["attempt_count"] if isinstance(row, dict) else row[1]
                owner = row["owner_token"] if isinstance(row, dict) else row[2]
                if current_token is not None and owner == current_token:
                    continue
                if owner is not None:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.ingestion_jobs
                        SET state = %s,
                            diagnostics = array_append(diagnostics, %s),
                            updated_at = clock_timestamp()
                        WHERE operation_id = %s
                          AND state = %s
                          AND attempt_count = %s
                          AND owner_token = %s
                        """,
                        (JobState.INTERRUPTED.value, diag, op_id, JobState.RUNNING.value, attempt, owner),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.ingestion_jobs
                        SET state = %s,
                            diagnostics = array_append(diagnostics, %s),
                            updated_at = clock_timestamp()
                        WHERE operation_id = %s
                          AND state = %s
                          AND attempt_count = %s
                          AND owner_token IS NULL
                        """,
                        (JobState.INTERRUPTED.value, diag, op_id, JobState.RUNNING.value, attempt),
                    )
                count += cur.rowcount
            return count


def check_or_reserve_snapshot(
    conn: psycopg.Connection,
    *,
    operation_id: UUID | None = None,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_ref: SnapshotRef,
    candidate_manifest: dict[str, Any],
) -> tuple[bool, SnapshotPublicationReceipt | None]:
    """Atomic check in PostgreSQL before engine write:
    1. Ensures repository exists.
    2. Checks if snapshot_id already exists in omp_knowledge.snapshots with row lock.
       - If existing manifest differs -> raises IdempotencyConflictError (409) BEFORE engine write.
       - If identical and already published -> returns (True, existing_receipt) immediately.
       - If identical and not published -> returns (False, None).
    3. If snapshot does not exist, returns (False, None) without redundant pre-reservation.
    """
    canonical_snapshot_id = validate_canonical_snapshot_id(snapshot_ref.snapshot_id)
    candidate_manifest_json = json.dumps(candidate_manifest)
    candidate_hash = sha256(candidate_manifest)

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
                SELECT manifest, ingest_operation_id, workspace_id, repository_id
                FROM omp_knowledge.snapshots
                WHERE snapshot_id = %s
                FOR UPDATE
                """,
                (canonical_snapshot_id,),
            )
            existing = cur.fetchone()
            if existing:
                existing_ws = existing["workspace_id"] if isinstance(existing, dict) else existing[2]
                existing_repo = existing["repository_id"] if isinstance(existing, dict) else existing[3]
                if existing_ws is None or existing_ws != workspace_id:
                    raise PermissionError(f"Snapshot {canonical_snapshot_id} belongs to another workspace")
                if existing_repo is not None and existing_repo != repository_id:
                    raise ValueError(f"Snapshot {canonical_snapshot_id} repository mismatch")

                existing_manifest = existing["manifest"] if isinstance(existing, dict) else existing[0]
                if isinstance(existing_manifest, str):
                    existing_manifest = json.loads(existing_manifest)
                existing_hash = sha256(existing_manifest)
                existing_op = existing["ingest_operation_id"] if isinstance(existing, dict) else existing[1]
                if existing_hash != candidate_hash:
                    canon_op = existing_op or operation_id or UUID("00000000-0000-0000-0000-000000000000")
                    raise IdempotencyConflictError(
                        operation_id=canon_op,
                        current_hash=candidate_hash,
                        existing_hash=existing_hash,
                    )

                # Manifest identical, check publication status
                cur.execute(
                    """
                    SELECT publication_id, workspace_id, repository_id, snapshot_id, status,
                           fact_count, insight_count, edge_count, graph_sha256, receipt_sha256,
                           staged_at, published_at
                    FROM omp_knowledge.snapshot_publications
                    WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                    """,
                    (workspace_id, repository_id, canonical_snapshot_id),
                )
                pub_row = cur.fetchone()
                if pub_row and pub_row["status"] == PublicationStatus.PUBLISHED.value:
                    receipt = SnapshotPublicationReceipt(
                        publication_id=pub_row["publication_id"],
                        workspace_id=pub_row["workspace_id"],
                        repository_id=pub_row["repository_id"],
                        snapshot_id=pub_row["snapshot_id"],
                        status=PublicationStatus.PUBLISHED,
                        fact_count=pub_row["fact_count"],
                        insight_count=pub_row["insight_count"],
                        edge_count=pub_row["edge_count"],
                        graph_sha256=pub_row["graph_sha256"],
                        receipt_sha256=pub_row["receipt_sha256"],
                        staged_at=pub_row["staged_at"],
                        published_at=pub_row["published_at"],
                    )
                    return True, receipt

                if isinstance(existing, dict) and existing.get("ingest_operation_id") is None and operation_id is not None:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.snapshots
                        SET ingest_operation_id = %s
                        WHERE snapshot_id = %s AND ingest_operation_id IS NULL
                        """,
                        (operation_id, canonical_snapshot_id),
                    )

                return False, None

            # Snapshot not present; do not pre-reserve
            return False, None


def store_observation(
    conn: psycopg.Connection,
    *,
    observation: SourceObservation,
) -> None:
    """Store raw source observation into omp_knowledge.observations.
    Deterministic identity replays cleanly if payload and lineage match.
    Conflicting payload or lineage raises IdempotencyConflictError.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.repositories (repository_id, state)
                VALUES (%s, 'unbound')
                ON CONFLICT (repository_id) DO NOTHING
                """,
                (observation.source.repository_id,),
            )
            cur.execute(
                """
                SELECT observation_id, workspace_id, repository_id, source,
                       payload, payload_sha256, native_payload_sha256
                FROM omp_knowledge.observations
                WHERE observation_id = %s
                FOR UPDATE
                """,
                (observation.observation_id,),
            )
            existing = cur.fetchone()
            if existing:
                existing_payload_sha = existing["payload_sha256"] if isinstance(existing, dict) else existing[5]
                existing_native_payload_sha = (
                    existing.get("native_payload_sha256")
                    if isinstance(existing, dict)
                    else (existing[6] if len(existing) > 6 else None)
                )
                raw_source = existing["source"] if isinstance(existing, dict) else existing[3]
                existing_source = json.loads(raw_source) if isinstance(raw_source, str) else (raw_source or {})

                existing_ws = existing["workspace_id"] if isinstance(existing, dict) else existing[1]
                if isinstance(existing_ws, str):
                    existing_ws = UUID(existing_ws)

                existing_producer = str(existing_source.get("producer") or "")
                is_existing_native = existing_producer.startswith("native_event_consumer")
                is_incoming_native = observation.source.producer.startswith("native_event_consumer")

                # Trust boundary: client observations cannot poison or block real consumer-owned capture.
                # If existing observation is a client observation and incoming is authoritative native capture,
                # the authoritative native capture overwrites the client placeholder.
                if not is_existing_native and is_incoming_native:
                    cur.execute(
                        """
                        UPDATE omp_knowledge.observations
                        SET workspace_id = %s,
                            repository_id = %s,
                            source = %s,
                            kind = %s,
                            payload = %s,
                            payload_sha256 = %s,
                            native_payload_sha256 = %s,
                            relevance_tags = %s,
                            observed_at = %s,
                            created_at = clock_timestamp()
                        WHERE observation_id = %s
                        """,
                        (
                            observation.source.workspace_id,
                            observation.source.repository_id,
                            json.dumps(observation.source.model_dump(mode="json")),
                            observation.kind.value,
                            json.dumps(observation.payload),
                            observation.payload_sha256,
                            observation.native_payload_sha256,
                            list(observation.relevance_tags),
                            observation.observed_at,
                            observation.observation_id,
                        ),
                    )
                    return

                # If existing is native capture and incoming is a client observation, client cannot overwrite it
                if is_existing_native and not is_incoming_native:
                    raise IdempotencyConflictError(
                        operation_id=observation.observation_id,
                        current_hash=observation.payload_sha256,
                        existing_hash=existing_payload_sha,
                    )

                conflict = False
                if existing_payload_sha != observation.payload_sha256:
                    conflict = True
                elif existing_native_payload_sha != observation.native_payload_sha256:
                    conflict = True
                elif existing_source.get("native_event_sha256") != observation.source.native_event_sha256:
                    conflict = True
                elif existing_source.get("previous_event_sha256") != observation.source.previous_event_sha256:
                    conflict = True
                elif existing_source.get("sequence") != observation.source.sequence:
                    conflict = True
                elif existing_ws != observation.source.workspace_id:
                    conflict = True

                if conflict:
                    raise IdempotencyConflictError(
                        operation_id=observation.observation_id,
                        current_hash=observation.payload_sha256,
                        existing_hash=existing_payload_sha,
                    )
                return

            cur.execute(
                """
                INSERT INTO omp_knowledge.observations (
                    observation_id, workspace_id, repository_id, source,
                    kind, payload, payload_sha256, native_payload_sha256,
                    relevance_tags, observed_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (observation_id) DO NOTHING
                """,
                (
                    observation.observation_id,
                    observation.source.workspace_id,
                    observation.source.repository_id,
                    json.dumps(observation.source.model_dump(mode="json")),
                    observation.kind.value,
                    json.dumps(observation.payload),
                    observation.payload_sha256,
                    observation.native_payload_sha256,
                    list(observation.relevance_tags),
                    observation.observed_at,
                ),
            )


def is_snapshot_published(
    conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
) -> bool:
    snapshot_id = validate_canonical_snapshot_id(snapshot_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM omp_knowledge.snapshot_publications
            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
            """,
            (workspace_id, repository_id, snapshot_id),
        )
        row = cur.fetchone()
        return bool(row and row["status"] == PublicationStatus.PUBLISHED.value)


def stage_publication(
    conn: psycopg.Connection,
    *,
    publication_id: UUID,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    fact_count: int,
    insight_count: int,
    edge_count: int,
    graph_sha256: str,
    receipt_sha256: str,
) -> None:
    snapshot_id = validate_canonical_snapshot_id(snapshot_id)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshot_publications AS sp (
                    publication_id, workspace_id, repository_id, snapshot_id,
                    status, fact_count, insight_count, edge_count,
                    graph_sha256, receipt_sha256, staged_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (workspace_id, repository_id, snapshot_id)
                DO UPDATE SET
                    publication_id = EXCLUDED.publication_id,
                    status = EXCLUDED.status,
                    fact_count = EXCLUDED.fact_count,
                    insight_count = EXCLUDED.insight_count,
                    edge_count = EXCLUDED.edge_count,
                    graph_sha256 = EXCLUDED.graph_sha256,
                    receipt_sha256 = EXCLUDED.receipt_sha256,
                    staged_at = clock_timestamp()
                WHERE sp.status != %s
                """,
                (
                    publication_id,
                    workspace_id,
                    repository_id,
                    snapshot_id,
                    PublicationStatus.STAGED.value,
                    fact_count,
                    insight_count,
                    edge_count,
                    graph_sha256,
                    receipt_sha256,
                    PublicationStatus.PUBLISHED.value,
                ),
            )


def publish_snapshot_gated(
    conn: psycopg.Connection,
    config: KnowledgeConfig,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
) -> bool:
    """Publish a staged snapshot with full invariant gating:
    1. Gated on existence of completed ingestion job in omp_knowledge.ingestion_jobs.
    2. Cancelled jobs are terminal and refuse publication.
    3. Exact content-addressed retained source artifacts in CAS verified.
    4. Atomic status flip to PUBLISHED.
    """
    snapshot_id = validate_canonical_snapshot_id(snapshot_id)
    with conn.transaction():
        with conn.cursor() as cur:
            # 1. Check publication record exists and is staged or published
            cur.execute(
                """
                SELECT status
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                FOR UPDATE
                """,
                (workspace_id, repository_id, snapshot_id),
            )
            pub = cur.fetchone()
            if not pub:
                return False
            pub_status = pub["status"] if isinstance(pub, dict) else pub[0]
            if pub_status not in (PublicationStatus.STAGED.value, PublicationStatus.PUBLISHED.value):
                return False

            # 2. Check snapshot record
            cur.execute(
                """
                SELECT workspace_id, repository_id, manifest
                FROM omp_knowledge.snapshots
                WHERE snapshot_id = %s
                FOR UPDATE
                """,
                (snapshot_id,),
            )
            snap = cur.fetchone()
            if not snap:
                return False
            snap_ws = snap.get("workspace_id") if isinstance(snap, dict) else snap[0]
            if snap_ws is None or snap_ws != workspace_id:
                raise PermissionError(f"Snapshot {snapshot_id} belongs to another workspace")
            if snap["repository_id"] != repository_id:
                raise ValueError(f"Snapshot {snapshot_id} repository mismatch")

            if pub_status == PublicationStatus.PUBLISHED.value:
                return True

            # 3. Check ingestion jobs for this snapshot:
            # Gated on completed job and cancelled jobs are terminal
            cur.execute(
                """
                SELECT operation_id, state
                FROM omp_knowledge.ingestion_jobs
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                FOR UPDATE
                """,
                (workspace_id, repository_id, snapshot_id),
            )
            jobs = cur.fetchall()
            if not jobs:
                return False

            has_completed = False
            for j in jobs:
                if j["state"] == JobState.CANCELLED.value:
                    raise RuntimeError(f"Snapshot {snapshot_id} has cancelled job {j['operation_id']} and cannot publish")
                if j["state"] == JobState.COMPLETED.value:
                    has_completed = True

            if not has_completed:
                return False

            # 4. Verify exact retained source artifacts in CAS
            manifest_raw = snap["manifest"]
            if isinstance(manifest_raw, str):
                manifest_data = json.loads(manifest_raw)
            elif isinstance(manifest_raw, dict):
                manifest_data = manifest_raw
            else:
                manifest_data = {}

            artifacts = manifest_data.get("artifacts") or {}
            for art_name, locator in artifacts.items():
                if isinstance(locator, str) and locator.startswith("blob:sha256:"):
                    expected_sha = locator[len("blob:sha256:"):]
                    data = get_retained_artifact(config, expected_sha)
                    if data is None:
                        raise RuntimeError(f"Missing retained artifact {art_name} ({expected_sha}) for snapshot {snapshot_id}")

            source_manifest = manifest_data.get("source_manifest")
            if isinstance(source_manifest, dict):
                src_locator = source_manifest.get("locator")
                if isinstance(src_locator, str) and src_locator.startswith("blob:sha256:"):
                    src_sha = src_locator[len("blob:sha256:"):]
                    if get_retained_artifact(config, src_sha) is None:
                        raise RuntimeError(f"Missing source manifest artifact ({src_sha})")
                for f in source_manifest.get("files", ()):
                    if isinstance(f, dict) and "sha256" in f:
                        f_sha = f["sha256"]
                        if get_retained_artifact(config, f_sha) is None:
                            raise RuntimeError(f"Missing source file artifact ({f_sha}) for {f.get('path')}")

            # 5. Flip status to PUBLISHED
            cur.execute(
                """
                UPDATE omp_knowledge.snapshot_publications
                SET status = %s, published_at = clock_timestamp()
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                  AND status = %s
                """,
                (
                    PublicationStatus.PUBLISHED.value,
                    workspace_id,
                    repository_id,
                    snapshot_id,
                    PublicationStatus.STAGED.value,
                ),
            )
            return cur.rowcount > 0


def publish_staged_snapshot(
    conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    config: KnowledgeConfig | None = None,
) -> bool:
    """Atomic single-row flip from staged to published, with gating if config is provided."""
    snapshot_id = validate_canonical_snapshot_id(snapshot_id)
    if config is not None:
        return publish_snapshot_gated(
            conn,
            config,
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                FOR UPDATE
                """,
                (workspace_id, repository_id, snapshot_id),
            )
            pub = cur.fetchone()
            if not pub:
                return False
            pub_status = pub["status"] if isinstance(pub, dict) else pub[0]
            if pub_status == PublicationStatus.PUBLISHED.value:
                return True
            if pub_status != PublicationStatus.STAGED.value:
                return False

            cur.execute(
                """
                UPDATE omp_knowledge.snapshot_publications
                SET status = %s, published_at = clock_timestamp()
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                  AND status = %s
                """,
                (
                    PublicationStatus.PUBLISHED.value,
                    workspace_id,
                    repository_id,
                    snapshot_id,
                    PublicationStatus.STAGED.value,
                ),
            )
            return cur.rowcount > 0


def store_retained_artifact(
    conn: psycopg.Connection,
    config: KnowledgeConfig,
    *,
    content_bytes: bytes,
    metadata: dict[str, Any],
) -> str:
    """Store content-addressed artifact with verified SHA-256."""
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    prefix = content_sha256[:2]
    store_dir = config.artifacts_dir / prefix
    store_dir.mkdir(parents=True, exist_ok=True)
    target_path = store_dir / content_sha256

    if not target_path.exists():
        target_path.write_bytes(content_bytes)

    locator = f"blob:sha256:{content_sha256}"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.artifacts (
                    content_sha256, byte_size, locator, artifact_metadata, created_at
                ) VALUES (%s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (content_sha256) DO NOTHING
                """,
                (
                    content_sha256,
                    len(content_bytes),
                    locator,
                    json.dumps(metadata),
                ),
            )
    return locator


def get_retained_artifact(
    config: KnowledgeConfig,
    content_sha256: str,
) -> bytes | None:
    target_path = config.artifacts_dir / content_sha256[:2] / content_sha256
    if not target_path.is_file():
        return None
    data = target_path.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != content_sha256:
        raise ValueError(f"Artifact corruption: expected {content_sha256}, got {actual_hash}")
    return data
