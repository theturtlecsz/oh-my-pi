from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.staging.manifest import (
    compute_graph_sha256,
    stage_snapshot_artifacts,
    validate_enola_artifacts,
)
from omp_knowledge.storage.db import get_retained_artifact, store_retained_artifact
from support.fixtures import make_test_enola_fixture, make_test_snapshot_ref
from support.null_engine import NullEngine


class FakeSqliteConn:
    """Mock connection for unit testing CAS and staging without PostgreSQL."""
    def __init__(self) -> None:
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.publications: dict[tuple, dict[str, Any]] = {}

    def cursor(self):
        conn = self
        class Cur:
            def execute(self, q, params=()):
                q_clean = " ".join(q.split()).lower()
                if "insert into omp_knowledge.artifacts" in q_clean:
                    h, sz, loc, meta = params
                    conn.artifacts[h] = {"content_sha256": h, "byte_size": sz, "locator": loc}
                elif "insert into omp_knowledge.snapshots" in q_clean:
                    sid = params[0]
                    mf = params[-1]
                    conn.snapshots[sid] = {"snapshot_id": sid, "manifest": json.loads(mf)}
                elif "insert into omp_knowledge.snapshot_publications" in q_clean:
                    pid, wid, rid, sid, st, fc, ic, ec, gh, rh = params[:10]
                    conn.publications[(wid, rid, sid)] = {"status": st, "graph_sha256": gh}
            def fetchone(self):
                return None
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return Cur()

    def transaction(self):
        conn = self
        class Tx:
            def __enter__(self):
                return conn
            def __exit__(self, *args):
                pass
        return Tx()


def test_cas_artifact_retention_and_verification(tmp_path: Path) -> None:
    config = KnowledgeConfig(state_dir=tmp_path / "state")
    conn: Any = FakeSqliteConn()

    content = b"User module contents for exact rebuild"
    content_hash = hashlib.sha256(content).hexdigest()

    # Store
    locator = store_retained_artifact(
        conn,
        config,
        content_bytes=content,
        metadata={"filename": "user.py"},
    )
    assert locator == f"blob:sha256:{content_hash}"

    # Read back and verify exact bytes
    retrieved = get_retained_artifact(config, content_hash)
    assert retrieved == content

    # Tamper with file to assert corruption detection
    disk_path = config.artifacts_dir / content_hash[:2] / content_hash
    disk_path.write_bytes(b"tampered-content")

    with pytest.raises(ValueError, match="Artifact corruption"):
        get_retained_artifact(config, content_hash)


@pytest.mark.asyncio
async def test_retained_source_rebuild_fidelity(tmp_path: Path) -> None:
    config = KnowledgeConfig(state_dir=tmp_path / "state")
    conn: Any = FakeSqliteConn()
    engine = NullEngine()

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "c" * 64

    facts_b, receipt_b, insights_b, raw_facts = make_test_enola_fixture(
        repo_name="rebuild-repo", snapshot_id=snap_id
    )
    snap_ref = make_test_snapshot_ref(repository_id=repo_id, snapshot_id=snap_id)

    # Ingest original into engine
    orig_result = await engine.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_ref=snap_ref,
        facts=raw_facts,
        receipt=json.loads(receipt_b),
        insights=[],
    )

    # Stage artifacts into CAS
    node_ids = [str(f["id"]) for f in raw_facts]
    staged_receipt = stage_snapshot_artifacts(
        conn,
        config,
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_ref=snap_ref,
        facts_bytes=facts_b,
        receipt_bytes=receipt_b,
        insights_bytes=insights_b,
        node_ids=node_ids,
        edge_keys=[],
    )
    assert staged_receipt.graph_sha256 == orig_result.graph_sha256

    # Rebuild from CAS artifacts
    retained_facts_b = get_retained_artifact(config, hashlib.sha256(facts_b).hexdigest())
    assert retained_facts_b is not None

    _, rebuilt_facts, _ = validate_enola_artifacts(
        facts_bytes=retained_facts_b,
        receipt_bytes=receipt_b,
        insights_bytes=insights_b,
    )

    # Re-ingest rebuilt facts into a fresh engine instance
    fresh_engine = NullEngine()
    rebuilt_result = await fresh_engine.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_ref=snap_ref,
        facts=rebuilt_facts,
        receipt=json.loads(receipt_b),
        insights=[],
    )

    assert rebuilt_result.graph_sha256 == orig_result.graph_sha256
    assert rebuilt_result.nodes_written == orig_result.nodes_written
