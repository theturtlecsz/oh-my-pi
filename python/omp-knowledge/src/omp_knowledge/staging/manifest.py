from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

import psycopg

from omp_work.knowledge_contracts import (
    PublicationStatus,
    SnapshotPublicationReceipt,
    SnapshotRef,
)
from omp_work.v1.canonical import canonical_json, sha256

from ..config import KnowledgeConfig
from ..models import SourceManifest, validate_canonical_snapshot_id
from ..storage.db import (
    IdempotencyConflictError,
    get_retained_artifact,
    stage_publication,
    store_retained_artifact,
)
from ..storage.jobs import SnapshotConflictError


class StagingError(Exception):
    pass


class MissingReceiptError(StagingError):
    pass


class UnsupportedFormatError(StagingError):
    pass


class CorruptArtifactError(StagingError):
    pass


class DuplicateFactIdError(CorruptArtifactError):
    pass


class CountMismatchError(StagingError):
    pass


def _parse_digest(digest_str: str) -> str:
    """Parse and validate sha256 digest string supporting 'sha256:<64hex>' or bare '<64hex>'.
    Rejects malformed, unsupported algorithm prefixes or non-hex characters.
    """
    if not isinstance(digest_str, str):
        raise CorruptArtifactError(f"Invalid digest type: {type(digest_str)}")
    if digest_str.startswith("sha256:"):
        bare = digest_str[len("sha256:"):]
    elif ":" in digest_str:
        prefix = digest_str.split(":", 1)[0]
        raise CorruptArtifactError(f"Unsupported digest algorithm prefix: {prefix}")
    else:
        bare = digest_str

    if len(bare) != 64 or not all(c in "0123456789abcdefABCDEF" for c in bare):
        raise CorruptArtifactError(f"Invalid SHA256 digest format: {digest_str}")
    return bare.lower()


def validate_enola_artifacts(
    *,
    facts_bytes: bytes,
    receipt_bytes: bytes | None,
    insights_bytes: bytes,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Strict verification of Enola extraction artifacts before staging.
    Rejects missing/corrupt receipts, hash mismatches, and count discrepancies.
    """
    if not receipt_bytes:
        raise MissingReceiptError("receipt.json is missing or empty")

    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except Exception as exc:
        raise CorruptArtifactError(f"receipt.json is not valid JSON: {exc}") from exc

    format_version = receipt.get("format_version")
    if format_version != 1:
        raise UnsupportedFormatError(f"Unsupported Enola format_version: {format_version}")

    output_hashes = receipt.get("output_hashes") or {}
    facts_sha256 = hashlib.sha256(facts_bytes).hexdigest()
    insights_sha256 = hashlib.sha256(insights_bytes).hexdigest()

    if "facts.jsonl" in output_hashes:
        expected_facts_hash = _parse_digest(output_hashes["facts.jsonl"])
        if expected_facts_hash != facts_sha256:
            raise CorruptArtifactError(
                f"facts.jsonl SHA256 mismatch: expected {expected_facts_hash}, got {facts_sha256}"
            )
    if "insights.json" in output_hashes:
        expected_insights_hash = _parse_digest(output_hashes["insights.json"])
        if expected_insights_hash != insights_sha256:
            raise CorruptArtifactError(
                f"insights.json SHA256 mismatch: expected {expected_insights_hash}, got {insights_sha256}"
            )

    # Validate facts.jsonl lines
    facts: list[dict[str, Any]] = []
    seen_fact_ids: set[str] = set()
    for line_num, line in enumerate(facts_bytes.splitlines(), start=1):
        line_str = line.strip()
        if not line_str:
            continue
        try:
            fact = json.loads(line_str.decode("utf-8"))
        except Exception as exc:
            raise CorruptArtifactError(f"facts.jsonl line {line_num} is not valid JSON: {exc}") from exc
        if not isinstance(fact, dict) or not fact.get("kind") or not fact.get("name") or not fact.get("id"):
            raise CorruptArtifactError(f"facts.jsonl line {line_num} is missing required fields (kind, name, id)")
        fact_id = fact["id"]
        if fact_id in seen_fact_ids:
            raise DuplicateFactIdError(f"Duplicate fact ID '{fact_id}' at line {line_num}")
        seen_fact_ids.add(fact_id)
        facts.append(fact)

    expected_fact_count = receipt.get("fact_count")
    if expected_fact_count is not None and len(facts) != expected_fact_count:
        raise CountMismatchError(
            f"Fact count mismatch: receipt says {expected_fact_count}, but parsed {len(facts)}"
        )

    # Validate insights.json
    try:
        insights = json.loads(insights_bytes.decode("utf-8"))
    except Exception as exc:
        raise CorruptArtifactError(f"insights.json is not valid JSON: {exc}") from exc
    if not isinstance(insights, list):
        raise CorruptArtifactError("insights.json must be a JSON array")

    expected_insight_count = receipt.get("insight_count")
    if expected_insight_count is not None and len(insights) != expected_insight_count:
        raise CountMismatchError(
            f"Insight count mismatch: receipt says {expected_insight_count}, but parsed {len(insights)}"
        )

    return receipt, facts, insights


def compute_graph_sha256(node_ids: Iterable[str], edge_keys: Iterable[tuple]) -> str:
    """Compute deterministic canonical hash of graph state."""
    payload = {
        "nodes": sorted(set(node_ids)),
        "edges": sorted([list(e) for e in edge_keys]),
    }
    return sha256(payload)


def validate_source_manifest_retention(
    config: KnowledgeConfig,
    source_manifest: SourceManifest | dict[str, Any] | None,
) -> None:
    """Validate that source manifest and all referenced files exist in CAS."""
    if source_manifest is None:
        return

    if isinstance(source_manifest, SourceManifest):
        src_dict = source_manifest.model_dump(mode="json", by_alias=True)
    elif hasattr(source_manifest, "model_dump"):
        src_dict = source_manifest.model_dump(mode="json", by_alias=True)
    elif isinstance(source_manifest, dict):
        src_dict = source_manifest
    else:
        return

    src_locator = src_dict.get("locator")
    if isinstance(src_locator, str) and src_locator.startswith("blob:sha256:"):
        src_sha = src_locator[len("blob:sha256:"):]
        if get_retained_artifact(config, src_sha) is None:
            raise CorruptArtifactError(f"Missing source manifest artifact ({src_sha})")
    for f in src_dict.get("files", ()):
        f_sha = f.get("sha256") if isinstance(f, dict) else getattr(f, "sha256", None)
        f_path = f.get("path") if isinstance(f, dict) else getattr(f, "path", "")
        if f_sha:
            if get_retained_artifact(config, f_sha) is None:
                raise CorruptArtifactError(
                    f"Missing source file artifact ({f_sha}) for {f_path}"
                )


def stage_snapshot_artifacts(
    conn: psycopg.Connection,
    config: KnowledgeConfig,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_ref: SnapshotRef,
    facts_bytes: bytes,
    receipt_bytes: bytes,
    insights_bytes: bytes,
    node_ids: Iterable[str],
    edge_keys: Iterable[tuple],
    source_manifest: SourceManifest | dict[str, Any] | None = None,
    operation_id: UUID | None = None,
) -> SnapshotPublicationReceipt:
    """Validate artifacts, store in content-addressed storage, and record staged publication."""
    receipt, facts, insights = validate_enola_artifacts(
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        insights_bytes=insights_bytes,
    )

    canonical_snapshot_id = validate_canonical_snapshot_id(snapshot_ref.snapshot_id)

    # Store in CAS
    facts_loc = store_retained_artifact(
        conn, config, content_bytes=facts_bytes, metadata={"name": "facts.jsonl", "snapshot_id": canonical_snapshot_id}
    )
    receipt_loc = store_retained_artifact(
        conn, config, content_bytes=receipt_bytes, metadata={"name": "receipt.json", "snapshot_id": canonical_snapshot_id}
    )
    insights_loc = store_retained_artifact(
        conn, config, content_bytes=insights_bytes, metadata={"name": "insights.json", "snapshot_id": canonical_snapshot_id}
    )

    # Insert snapshot metadata with immutability check
    manifest_payload: dict[str, Any] = {
        "snapshot_ref": snapshot_ref.model_dump(mode="json"),
        "artifacts": {
            "facts": facts_loc,
            "receipt": receipt_loc,
            "insights": insights_loc,
        },
    }
    if source_manifest is not None:
        if isinstance(source_manifest, SourceManifest):
            manifest_payload["source_manifest"] = source_manifest.model_dump(mode="json", by_alias=True)
        elif hasattr(source_manifest, "model_dump"):
            manifest_payload["source_manifest"] = source_manifest.model_dump(mode="json", by_alias=True)
        else:
            manifest_payload["source_manifest"] = source_manifest

        validate_source_manifest_retention(config, manifest_payload["source_manifest"])

    manifest_json = json.dumps(manifest_payload)
    manifest_hash = sha256(manifest_payload)

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
                if existing_hash != manifest_hash:
                    existing_op = existing["ingest_operation_id"] if isinstance(existing, dict) else existing[1]
                    canon_op = existing_op or operation_id or UUID("00000000-0000-0000-0000-000000000000")
                    raise SnapshotConflictError(
                        snapshot_id=canonical_snapshot_id,
                        canonical_operation_id=canon_op,
                        current_hash=manifest_hash,
                        existing_hash=existing_hash,
                    )
            else:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.snapshots (
                        snapshot_id, workspace_id, repository_id, manifest_sha256, base_commit, tree_sha, candidate_tree_sha, manifest, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                    ON CONFLICT (snapshot_id) DO NOTHING
                    """,
                    (
                        canonical_snapshot_id,
                        workspace_id,
                        repository_id,
                        manifest_hash,
                        snapshot_ref.base_commit,
                        snapshot_ref.tree_sha,
                        snapshot_ref.candidate_tree_sha,
                        manifest_json,
                    ),
                )

    graph_hash = compute_graph_sha256(node_ids, edge_keys)
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    pub_id = uuid4()
    now = datetime.now(timezone.utc)

    stage_publication(
        conn,
        publication_id=pub_id,
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=canonical_snapshot_id,
        fact_count=len(facts),
        insight_count=len(insights),
        edge_count=len(list(edge_keys)),
        graph_sha256=graph_hash,
        receipt_sha256=receipt_hash,
    )

    return SnapshotPublicationReceipt(
        publication_id=pub_id,
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=canonical_snapshot_id,
        status=PublicationStatus.STAGED,
        fact_count=len(facts),
        insight_count=len(insights),
        edge_count=len(list(edge_keys)),
        graph_sha256=graph_hash,
        receipt_sha256=receipt_hash,
        staged_at=now,
    )
