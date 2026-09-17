from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.staging.manifest import (
    CorruptArtifactError,
    CountMismatchError,
    DuplicateFactIdError,
    MissingReceiptError,
    UnsupportedFormatError,
    _parse_digest,
    stage_snapshot_artifacts,
    validate_enola_artifacts,
    validate_source_manifest_retention,
)
from omp_knowledge.storage.db import IdempotencyConflictError, get_db_connection
from omp_knowledge.storage.jobs import SnapshotConflictError
from support.fixtures import (
    load_staged_fixture,
    make_test_enola_fixture,
    make_test_snapshot_ref,
)


def test_parse_digest() -> None:
    """Verify digest parsing supports sha256: prefix and bare 64-hex, rejecting malformed values."""
    valid_hex = "a" * 64
    assert _parse_digest(valid_hex) == valid_hex
    assert _parse_digest(f"sha256:{valid_hex}") == valid_hex
    assert _parse_digest(f"sha256:{valid_hex.upper()}") == valid_hex

    with pytest.raises(CorruptArtifactError, match="Unsupported digest algorithm prefix"):
        _parse_digest(f"sha512:{valid_hex}")

    with pytest.raises(CorruptArtifactError, match="Invalid SHA256 digest format"):
        _parse_digest("short_hex")

    with pytest.raises(CorruptArtifactError, match="Invalid SHA256 digest format"):
        _parse_digest("z" * 64)


def test_validate_enola_artifacts_success() -> None:
    facts_b, receipt_b, insights_b, _ = make_test_enola_fixture()
    receipt, facts, insights = validate_enola_artifacts(
        facts_bytes=facts_b,
        receipt_bytes=receipt_b,
        insights_bytes=insights_b,
    )
    assert receipt["enola_version"] == "v0.4.18"
    assert len(facts) == 3
    assert len(insights) == 1


def test_validate_real_staged_fixtures_a_and_b() -> None:
    """Verify that real Enola 0.4.18 staged fixtures A and B validate cleanly (Failure 1)."""
    # Fixture A
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    rec_a, parsed_a, ins_a = validate_enola_artifacts(
        facts_bytes=facts_a,
        receipt_bytes=receipt_a,
        insights_bytes=insights_a,
    )
    assert rec_a["enola_version"] == "0.4.18"
    assert len(parsed_a) == 19
    assert len(ins_a) == 0

    # Fixture B
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    rec_b, parsed_b, ins_b = validate_enola_artifacts(
        facts_bytes=facts_b,
        receipt_bytes=receipt_b,
        insights_bytes=insights_b,
    )
    assert rec_b["enola_version"] == "0.4.18"
    assert len(parsed_b) == 19
    assert len(ins_b) == 0


def test_validate_enola_artifacts_missing_receipt() -> None:
    facts_b, _, insights_b, _ = make_test_enola_fixture()
    with pytest.raises(MissingReceiptError, match="receipt.json is missing"):
        validate_enola_artifacts(
            facts_bytes=facts_b,
            receipt_bytes=None,
            insights_bytes=insights_b,
        )


def test_validate_enola_artifacts_corrupt_json() -> None:
    facts_b, _, insights_b, _ = make_test_enola_fixture()
    with pytest.raises(CorruptArtifactError, match="not valid JSON"):
        validate_enola_artifacts(
            facts_bytes=facts_b,
            receipt_bytes=b"{invalid-json",
            insights_bytes=insights_b,
        )


def test_validate_enola_artifacts_unsupported_format_version() -> None:
    facts_b, receipt_b, insights_b, _ = make_test_enola_fixture()
    r = json.loads(receipt_b.decode())
    r["format_version"] = 999
    r_bytes = json.dumps(r).encode()

    with pytest.raises(UnsupportedFormatError, match="Unsupported Enola format_version"):
        validate_enola_artifacts(
            facts_bytes=facts_b,
            receipt_bytes=r_bytes,
            insights_bytes=insights_b,
        )


def test_validate_enola_artifacts_fact_count_mismatch() -> None:
    facts_b, receipt_b, insights_b, _ = make_test_enola_fixture()
    r = json.loads(receipt_b.decode())
    r["fact_count"] = 999  # actual count is 3
    r_bytes = json.dumps(r).encode()

    with pytest.raises(CountMismatchError, match="Fact count mismatch"):
        validate_enola_artifacts(
            facts_bytes=facts_b,
            receipt_bytes=r_bytes,
            insights_bytes=insights_b,
        )


def test_validate_enola_artifacts_hash_mismatch() -> None:
    facts_b, receipt_b, insights_b, _ = make_test_enola_fixture()
    # Mutate facts bytes slightly
    corrupt_facts_b = facts_b + b"\n"

    with pytest.raises(CorruptArtifactError, match="SHA256 mismatch"):
        validate_enola_artifacts(
            facts_bytes=corrupt_facts_b,
            receipt_bytes=receipt_b,
            insights_bytes=insights_b,
        )


def test_stage_snapshot_immutability(pg_cluster: KnowledgeConfig) -> None:
    """Verify that conflicting content under the same snapshot ID raises 409 (Failure 6)."""
    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "a" * 64

        facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
        snap_ref = make_test_snapshot_ref(repository_id=repo_id, snapshot_id=snap_id)

        # First stage succeeds
        pub1 = stage_snapshot_artifacts(
            conn,
            pg_cluster,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_ref=snap_ref,
            facts_bytes=facts_a,
            receipt_bytes=receipt_a,
            insights_bytes=insights_a,
            node_ids=["node1"],
            edge_keys=[],
        )
        assert pub1.fact_count == 19

        # Staging with different content under the same snapshot ID raises IdempotencyConflictError
        facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
        with pytest.raises(IdempotencyConflictError):
            stage_snapshot_artifacts(
                conn,
                pg_cluster,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts_bytes=facts_b,
                receipt_bytes=receipt_b,
                insights_bytes=insights_b,
                node_ids=["node2"],
                edge_keys=[],
            )
    finally:
        conn.close()
def test_validate_enola_artifacts_rejects_duplicate_fact_id() -> None:
    """Duplicate fact IDs must be rejected with DuplicateFactIdError (Item 2)."""
    facts_b, receipt_b, insights_b, _ = make_test_enola_fixture()
    # Duplicate line 1 and recompute receipt output hash and fact count
    lines = [line for line in facts_b.splitlines() if line.strip()]
    dup_facts_b = b"\n".join([lines[0], lines[0]] + lines[1:]) + b"\n"
    r = json.loads(receipt_b.decode("utf-8"))
    r["output_hashes"]["facts.jsonl"] = hashlib.sha256(dup_facts_b).hexdigest()
    r["fact_count"] = len(lines) + 1
    dup_receipt_b = json.dumps(r).encode("utf-8")
    with pytest.raises(DuplicateFactIdError, match="Duplicate fact ID"):
        validate_enola_artifacts(
            facts_bytes=dup_facts_b,
            receipt_bytes=dup_receipt_b,
            insights_bytes=insights_b,
        )


def test_stage_snapshot_with_source_manifest_retention(pg_cluster: KnowledgeConfig) -> None:
    """source_manifest retention must enforce fail-closed validation when files are missing
    from CAS before staged COMPLETED state, and succeed when retained (Item 5).
    """
    from omp_knowledge.models import SourceFile, SourceManifest
    from omp_knowledge.storage.db import publish_snapshot_gated, store_retained_artifact

    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "c" * 64

        facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
        snap_ref = make_test_snapshot_ref(repository_id=repo_id, snapshot_id=snap_id)

        # Retain manifest in CAS so missing source file check is reached
        dummy_manifest_bytes = b'{"schema": "omp_knowledge.source_manifest/v1"}'
        store_retained_artifact(conn, pg_cluster, content_bytes=dummy_manifest_bytes, metadata={"name": "manifest.json"})
        manifest_loc = f"blob:sha256:{hashlib.sha256(dummy_manifest_bytes).hexdigest()}"

        # 1. Unretained file sha in source manifest must fail closed
        missing_file_sha = "d" * 64
        manifest_data = {
            "schema": "omp_knowledge.source_manifest/v1",
            "locator": manifest_loc,
            "files": [{"path": "src/main.py", "sha256": missing_file_sha, "size": 100}],
        }
        with pytest.raises(CorruptArtifactError, match="Missing source file artifact"):
            stage_snapshot_artifacts(
                conn,
                pg_cluster,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts_bytes=facts_a,
                receipt_bytes=receipt_a,
                insights_bytes=insights_a,
                node_ids=["node1"],
                edge_keys=[],
                source_manifest=manifest_data,
            )

        # 2. Retain file and manifest in CAS, then staging succeeds
        file_bytes = b"print('hello from retained source file')"
        real_file_sha = hashlib.sha256(file_bytes).hexdigest()
        store_retained_artifact(conn, pg_cluster, content_bytes=file_bytes, metadata={"name": "src/main.py"})

        manifest_obj = SourceManifest(
            schema_name="omp_knowledge.source_manifest/v1",
            locator=manifest_loc,
            files=(SourceFile(path="src/main.py", sha256=real_file_sha, size=len(file_bytes)),),
        )

        receipt = stage_snapshot_artifacts(
            conn,
            pg_cluster,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_ref=snap_ref,
            facts_bytes=facts_a,
            receipt_bytes=receipt_a,
            insights_bytes=insights_a,
            node_ids=["node1"],
            edge_keys=[],
            source_manifest=manifest_obj,
        )
        assert receipt.snapshot_id == snap_id

        # Record completed ingestion job so gated publication passes
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'completed', 1, clock_timestamp(), clock_timestamp())
                    """,
                    (uuid4(), ws_id, repo_id, snap_id, hashlib.sha256(b"test_hash").hexdigest()),
                )

        # Staged snapshot can be published cleanly
        assert publish_snapshot_gated(
            conn,
            pg_cluster,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
        ) is True
    finally:
        conn.close()

def test_validate_source_manifest_retention_missing_in_cas(pg_cluster: KnowledgeConfig) -> None:
    """F6: CAS preflight validation must reject source manifests or source files missing from CAS."""
    from omp_knowledge.models import SourceFile, SourceManifest
    from omp_knowledge.storage.db import get_db_connection, store_retained_artifact

    conn = get_db_connection(pg_cluster)
    try:
        manifest_bytes = b'{"schema": "omp_knowledge.source_manifest/v1"}'
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_loc = f"blob:sha256:{manifest_sha}"

        # 1. Missing manifest in CAS fails closed
        manifest_obj = SourceManifest(
            schema_name="omp_knowledge.source_manifest/v1",
            locator=manifest_loc,
            files=(),
        )
        with pytest.raises(CorruptArtifactError, match="Missing source manifest artifact"):
            validate_source_manifest_retention(pg_cluster, manifest_obj)

        # Store manifest in CAS
        store_retained_artifact(conn, pg_cluster, content_bytes=manifest_bytes, metadata={"name": "manifest.json"})

        # 2. Manifest present, but referenced source file missing fails closed
        file_sha = "e" * 64
        manifest_with_missing_file = SourceManifest(
            schema_name="omp_knowledge.source_manifest/v1",
            locator=manifest_loc,
            files=(SourceFile(path="src/lib.py", sha256=file_sha, size=123),),
        )
        with pytest.raises(CorruptArtifactError, match="Missing source file artifact"):
            validate_source_manifest_retention(pg_cluster, manifest_with_missing_file)

        # Store file in CAS -> now validation succeeds
        file_bytes = b"def foo(): pass"
        real_sha = hashlib.sha256(file_bytes).hexdigest()
        store_retained_artifact(conn, pg_cluster, content_bytes=file_bytes, metadata={"name": "src/lib.py"})
        manifest_valid = SourceManifest(
            schema_name="omp_knowledge.source_manifest/v1",
            locator=manifest_loc,
            files=(SourceFile(path="src/lib.py", sha256=real_sha, size=len(file_bytes)),),
        )
        validate_source_manifest_retention(pg_cluster, manifest_valid)
    finally:
        conn.close()


def test_staging_snapshot_conflict_canonical_operation_identity(pg_cluster: KnowledgeConfig) -> None:
    """F8/F1: Staging conflict must raise SnapshotConflictError carrying the canonical operation ID, never a random UUID."""
    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()
        canonical_op_id = uuid4()
        snap_id = "e" * 64

        facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
        snap_ref = make_test_snapshot_ref(repository_id=repo_id, snapshot_id=snap_id)

        # Stage fixture A under canonical_op_id
        pub1 = stage_snapshot_artifacts(
            conn,
            pg_cluster,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_ref=snap_ref,
            facts_bytes=facts_a,
            receipt_bytes=receipt_a,
            insights_bytes=insights_a,
            node_ids=["node1"],
            edge_keys=[],
            operation_id=canonical_op_id,
        )
        assert pub1.snapshot_id == snap_id

        # Record ingest_operation_id in snapshot row
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE omp_knowledge.snapshots SET ingest_operation_id = %s WHERE snapshot_id = %s",
                    (canonical_op_id, snap_id),
                )

        # Now stage conflicting content under the same snapshot ID
        facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
        with pytest.raises(SnapshotConflictError) as exc_info:
            stage_snapshot_artifacts(
                conn,
                pg_cluster,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts_bytes=facts_b,
                receipt_bytes=receipt_b,
                insights_bytes=insights_b,
                node_ids=["node2"],
                edge_keys=[],
                operation_id=uuid4(),
            )
        assert exc_info.value.canonical_operation_id == canonical_op_id
        assert exc_info.value.operation_id == canonical_op_id
    finally:
        conn.close()


def test_publish_snapshot_gated_rejects_legacy_null_workspace(pg_cluster: KnowledgeConfig) -> None:
    """F10: publish_snapshot_gated must fail closed if snapshot row has NULL workspace_id."""
    from omp_knowledge.storage.db import publish_snapshot_gated

    conn = get_db_connection(pg_cluster)
    try:
        ws_id = uuid4()
        repo_id = uuid4()
        snap_id = "d" * 64

        facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
        snap_ref = make_test_snapshot_ref(repository_id=repo_id, snapshot_id=snap_id)

        # Stage snapshot
        stage_snapshot_artifacts(
            conn,
            pg_cluster,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_ref=snap_ref,
            facts_bytes=facts_a,
            receipt_bytes=receipt_a,
            insights_bytes=insights_a,
            node_ids=["node1"],
            edge_keys=[],
        )

        # Simulate legacy NULL workspace_id in snapshots table
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE omp_knowledge.snapshots SET workspace_id = NULL WHERE snapshot_id = %s",
                    (snap_id,),
                )

        # Attempt to publish under ws_id must fail closed with PermissionError
        with pytest.raises(PermissionError, match="belongs to another workspace"):
            publish_snapshot_gated(
                conn,
                pg_cluster,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
            )
    finally:
        conn.close()
