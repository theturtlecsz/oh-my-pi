from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

import psycopg

from omp_work.v1.canonical import canonical_json, sha256


def insert_test_work_item(
    native_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    work_id: UUID | None = None,
    current_revision_id: UUID | None = None,
    current_candidate_id: UUID | None = None,
    state: str = "IN_PROGRESS",
) -> UUID:
    w_id = work_id or uuid4()
    with native_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
        cur.execute(
            """
            INSERT INTO omp_work.work_items (
                work_id, workspace_id, state, current_revision_id, current_candidate_id
            ) VALUES (%s, %s, %s, NULL, NULL)
            ON CONFLICT (work_id) DO UPDATE
                SET state = EXCLUDED.state
            """,
            (w_id, workspace_id, state),
        )

    if current_revision_id is not None:
        insert_test_work_revision(
            native_conn,
            workspace_id=workspace_id,
            work_id=w_id,
            revision_id=current_revision_id,
        )

    rev_id = current_revision_id
    if current_candidate_id is not None:
        if rev_id is None:
            rev_id = insert_test_work_revision(
                native_conn,
                workspace_id=workspace_id,
                work_id=w_id,
            )
        insert_test_candidate(
            native_conn,
            workspace_id=workspace_id,
            work_id=w_id,
            revision_id=rev_id,
            candidate_id=current_candidate_id,
        )

    if rev_id is not None or current_candidate_id is not None:
        with native_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE omp_work.work_items
                SET current_revision_id = COALESCE(%s, current_revision_id),
                    current_candidate_id = COALESCE(%s, current_candidate_id),
                    state = %s
                WHERE work_id = %s
                """,
                (rev_id, current_candidate_id, state, w_id),
            )

    return w_id


def insert_test_work_revision(
    native_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID | None = None,
    revision_number: int = 1,
    title: str = "Test Revision",
    description: str = "Description for revision",
    scope: str = "Scope for revision",
    content_sha256: str | None = None,
) -> UUID:
    rev_id = revision_id or uuid4()
    rev_hash = content_sha256 or sha256({"title": title, "rev": revision_number, "id": str(rev_id)})
    now = datetime.now(timezone.utc)
    with native_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO omp_work.work_revisions (
                revision_id, work_id, workspace_id, revision_number,
                title, description, scope, content_sha256,
                created_by, supplied_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (revision_id) DO NOTHING
            """,
            (
                rev_id,
                work_id,
                workspace_id,
                revision_number,
                title,
                description,
                scope,
                rev_hash,
                "test-agent",
                now,
            ),
        )
    return rev_id


def insert_test_candidate(
    native_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID | None = None,
    candidate_sha256: str | None = None,
    commit_sha: str | None = None,
    kind: str = "planned",
) -> UUID:
    cand_id = candidate_id or uuid4()
    cand_hash = candidate_sha256 or sha256({"cand": str(cand_id), "rev": str(revision_id)})
    now = datetime.now(timezone.utc)
    with native_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO omp_work.candidates (
                candidate_id, workspace_id, work_id, revision_id,
                candidate_sha256, commit_sha, allocated_at, kind
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidate_id) DO NOTHING
            """,
            (
                cand_id,
                workspace_id,
                work_id,
                revision_id,
                cand_hash,
                commit_sha,
                now,
                kind,
            ),
        )
    return cand_id


def insert_test_receipt(
    native_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    receipt_id: UUID | None = None,
    kind: str = "audit",
    payload: dict[str, Any] | None = None,
    issuer: str = "work-service/auditor-settle",
    verdict: str | None = "PASS",
    independent: bool = True,
    candidate_commit: str | None = None,
    candidate_sha256: str | None = None,
) -> dict[str, Any]:
    r_id = receipt_id or uuid4()
    r_payload = payload if payload is not None else {"verdict": verdict, "audit_rule": "strict"}
    r_hash = sha256(r_payload)
    now = datetime.now(timezone.utc)

    # 1. Ensure workspace and base work item exist in committed setup
    insert_test_work_item(
        native_conn,
        workspace_id=workspace_id,
        work_id=work_id,
    )
    # 2. Ensure revision exists referencing work item
    insert_test_work_revision(
        native_conn,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
    )
    # 3. Ensure candidate exists referencing work item and revision
    insert_test_candidate(
        native_conn,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        candidate_sha256=candidate_sha256,
        commit_sha=candidate_commit,
    )
    # 4. Set current revision and candidate pointers on the work item
    insert_test_work_item(
        native_conn,
        workspace_id=workspace_id,
        work_id=work_id,
        current_revision_id=revision_id,
        current_candidate_id=candidate_id,
    )

    with native_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO omp_evidence.receipts (
                receipt_id, workspace_id, work_id, revision_id, candidate_id,
                kind, payload, payload_sha256, issuer, issued_at,
                candidate_sha256, candidate_commit, verdict, independent
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (receipt_id) DO NOTHING
            """,
            (
                r_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                kind,
                json.dumps(r_payload),
                r_hash,
                issuer,
                now,
                candidate_sha256,
                candidate_commit,
                verdict,
                independent,
            ),
        )

    return {
        "receipt_id": r_id,
        "workspace_id": workspace_id,
        "work_id": work_id,
        "revision_id": revision_id,
        "candidate_id": candidate_id,
        "kind": kind,
        "payload": r_payload,
        "payload_sha256": r_hash,
        "issuer": issuer,
        "verdict": verdict,
        "independent": independent,
        "issued_at": now,
    }
