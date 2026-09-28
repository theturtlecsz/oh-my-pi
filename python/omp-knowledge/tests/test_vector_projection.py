"""Contract tests for VectorProjectionStore and CLI (OMP-278-s03 / FK-3).

Defends:
- Building B leaves A's rows and digests unchanged.
- Embedder failure mid-build keeps the prior projection.
- Rebuild repeats projection_sha256, vectors_sha256 and search hits.
- Rebuild from maintenance rebuild_from_records structural root repeats digests.
- Backup + rollback restore vectors.sqlite byte-identical; backup without vectors.sqlite rebuilds.
- Retired/unpublished snapshot and unbuilt generation are refused.
- Vectors build CLI behavior on success (0) and error (2).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import sqlite3
import struct
from collections.abc import Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from omp_work.knowledge_publication import StructuralPublicationStore
from support.fixtures import load_staged_fixture

from omp_knowledge.errors import KnowledgeError, SnapshotNotPublishedError
from omp_knowledge.inference.embedding import EmbeddingProfile
from omp_knowledge.inference.routes import (
    RouteHealth,
    RouteProfile,
    resolve,
)
from omp_knowledge.maintenance import (
    create_backup,
    rebuild_from_records,
    rollback,
)
from omp_knowledge.vectors.cli import EXIT_ERROR, EXIT_OK, build_parser, main as vectors_main
from omp_knowledge.vectors.store import (
    VectorHit,
    VectorProjection,
    VectorProjectionStore,
)

WS = uuid5(NAMESPACE_URL, "omp-test/fk3-workspace")
REPO = uuid5(NAMESPACE_URL, "omp-test/fk3-repository")
_SNAP_A = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
_SNAP_B = "b3649242961413ac43d41254871ebfd9a74bed88045d1c58972f67267216db55"


class DeterministicFakeEmbedder:
    """Deterministic fake embedder producing normalized float32 vectors."""

    def __init__(
        self,
        profile: EmbeddingProfile | None = None,
        *,
        fail_after: int | None = None,
    ) -> None:
        self.profile = profile or EmbeddingProfile(
            model="fake-qwen3-embed",
            model_revision="v1",
            dimensions=4,
            pooling="last",
            query_prefix="query: ",
            document_prefix="doc: ",
            normalize=True,
        )
        self.fail_after = fail_after
        self.calls = 0

    def _hash_vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        dim = self.profile.dimensions
        # Generate predictable float coordinates from byte values
        raw = [float((digest[i % len(digest)] % 100) + 1) for i in range(dim)]
        if self.profile.normalize:
            norm = math.sqrt(sum(x * x for x in raw))
            if norm > 0:
                raw = [x / norm for x in raw]
        return raw

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            if self.fail_after is not None and self.calls >= self.fail_after:
                raise RuntimeError("simulated mid-build embedder failure")
            self.calls += 1
            results.append(self._hash_vector(self.profile.document_prefix + text))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._hash_vector(self.profile.query_prefix + text)


def _publish_fixture(
    state_dir: Path,
    fixture_name: str,
    snapshot_id: str,
    *,
    workspace_id: UUID = WS,
    repository_id: UUID = REPO,
) -> tuple[StructuralPublicationStore, str]:
    facts_bytes, receipt_bytes, _insights, _raw = load_staged_fixture(fixture_name)
    store = StructuralPublicationStore(state_dir)
    store.stage_enola_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        manifest_files=[],
    )
    store.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
    )
    return store, snapshot_id


def _routes_payload(profile: EmbeddingProfile) -> dict[str, object]:
    return {
        "routes": [
            {
                "role": "embedding",
                "primary": {
                    "name": "embed-gpu",
                    "provider": "llama.cpp",
                    "model": profile.model,
                    "endpoint": "http://127.0.0.1:18081",
                    "accelerator": "gpu",
                    "embedding_profile": profile.model_dump(mode="json"),
                },
                "fallback": {
                    "name": "embed-cpu",
                    "provider": "llama.cpp",
                    "model": profile.model,
                    "endpoint": "http://127.0.0.1:18084",
                    "accelerator": "cpu",
                    "embedding_profile": profile.model_dump(mode="json"),
                },
            },
            {
                "role": "reranker",
                "primary": {
                    "name": "rerank-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3-Reranker-0.6B",
                    "endpoint": "http://127.0.0.1:18082",
                    "accelerator": "gpu",
                },
                "fallback": {
                    "name": "rerank-order",
                    "provider": "order",
                    "accelerator": "cpu",
                },
            },
            {
                "role": "generator",
                "primary": {
                    "name": "gen-gpu",
                    "provider": "llama.cpp",
                    "model": "Qwen3.8-27B",
                    "endpoint": "http://127.0.0.1:18083",
                    "accelerator": "gpu",
                },
                "fallback": None,
            },
        ]
    }


def test_build_and_search_basic(tmp_path: Path) -> None:
    pub_store, snap = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    embedder = DeterministicFakeEmbedder()

    proj = vec_store.build(pub_store, WS, REPO, snap, embedder)

    assert isinstance(proj, VectorProjection)
    assert proj.namespace == f"{WS}:{REPO}@{snap}"
    assert proj.generation_id == embedder.profile.generation_id
    assert proj.vector_count == 19
    assert len(proj.projection_sha256) == 64
    assert len(proj.vectors_sha256) == 64

    # Verify rows in sqlite
    conn = sqlite3.connect(str(tmp_path / "vectors" / "vectors.sqlite"))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM vector_rows WHERE namespace = ?", (proj.namespace,)
        ).fetchall()
        assert len(rows) == 19
        for r in rows:
            assert len(r["vector"]) == embedder.profile.dimensions * 4
            assert r["text_sha256"] == hashlib.sha256(r["text"].encode("utf-8")).hexdigest()
    finally:
        conn.close()

    # Search
    q_vec = embedder.embed_query("User model")
    hits = vec_store.search(pub_store, WS, REPO, snap, proj.generation_id, q_vec, k=2)
    assert len(hits) == 2
    assert isinstance(hits[0], VectorHit)
    assert hits[0].score >= hits[1].score


def test_search_ties_broken_by_fact_id(tmp_path: Path) -> None:
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    gen_id = "test-gen"
    namespace = f"{WS}:{REPO}@{_SNAP_A}"
    now = "2026-09-28T00:00:00Z"

    # Insert two facts with identical vectors (and thus identical cosine scores)
    vec = [1.0, 0.0, 0.0, 0.0]
    blob = struct.pack("<4f", *vec)

    with vec_store._connection() as conn:
        conn.execute(
            "INSERT INTO vector_projections VALUES (?, ?, ?, ?, ?, ?)",
            (namespace, gen_id, 2, "proj-sha", "vec-sha", now),
        )
        conn.execute(
            "INSERT INTO vector_rows VALUES (?, ?, ?, ?, ?, ?)",
            (namespace, gen_id, "fact-z", "text z", "tz", blob),
        )
        conn.execute(
            "INSERT INTO vector_rows VALUES (?, ?, ?, ?, ?, ?)",
            (namespace, gen_id, "fact-a", "text a", "ta", blob),
        )

    # Search with identical vector: scores both 1.0, fact-a must precede fact-z
    hits = vec_store.search(None, WS, REPO, _SNAP_A, gen_id, [1.0, 0.0, 0.0, 0.0], k=2)
    assert len(hits) == 2
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(1.0)
    assert hits[0].fact_id == "fact-a"
    assert hits[1].fact_id == "fact-z"


def test_building_b_leaves_a_rows_and_digests_unchanged(tmp_path: Path) -> None:
    pub_store, _ = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    _publish_fixture(tmp_path / "structural", "B", _SNAP_B)
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    embedder = DeterministicFakeEmbedder()

    # Build A
    proj_a = vec_store.build(pub_store, WS, REPO, _SNAP_A, embedder)

    conn = sqlite3.connect(str(tmp_path / "vectors" / "vectors.sqlite"))
    try:
        conn.row_factory = sqlite3.Row
        a_rows_before = conn.execute(
            "SELECT fact_id, text, text_sha256, vector FROM vector_rows "
            "WHERE namespace = ? ORDER BY fact_id",
            (proj_a.namespace,),
        ).fetchall()
        a_rows_before_dump = [
            (r["fact_id"], r["text"], r["text_sha256"], r["vector"]) for r in a_rows_before
        ]
    finally:
        conn.close()

    # Build B
    proj_b = vec_store.build(pub_store, WS, REPO, _SNAP_B, embedder)

    assert proj_b.namespace != proj_a.namespace
    assert proj_b.vector_count == 19

    # Verify A is completely untouched
    proj_a_after = vec_store.get_projection(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=_SNAP_A,
        generation_id=proj_a.generation_id,
    )
    assert proj_a_after is not None
    assert proj_a_after.projection_sha256 == proj_a.projection_sha256
    assert proj_a_after.vectors_sha256 == proj_a.vectors_sha256
    assert proj_a_after.vector_count == proj_a.vector_count

    conn = sqlite3.connect(str(tmp_path / "vectors" / "vectors.sqlite"))
    try:
        conn.row_factory = sqlite3.Row
        a_rows_after = conn.execute(
            "SELECT fact_id, text, text_sha256, vector FROM vector_rows "
            "WHERE namespace = ? ORDER BY fact_id",
            (proj_a.namespace,),
        ).fetchall()
        a_rows_after_dump = [
            (r["fact_id"], r["text"], r["text_sha256"], r["vector"]) for r in a_rows_after
        ]
    finally:
        conn.close()

    assert a_rows_after_dump == a_rows_before_dump


def test_embedder_failure_mid_build_keeps_prior_projection(tmp_path: Path) -> None:
    pub_store, _ = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    embedder = DeterministicFakeEmbedder()

    # Initial successful build
    proj_initial = vec_store.build(pub_store, WS, REPO, _SNAP_A, embedder)

    conn = sqlite3.connect(str(tmp_path / "vectors" / "vectors.sqlite"))
    try:
        conn.row_factory = sqlite3.Row
        rows_initial = conn.execute(
            "SELECT fact_id, text, text_sha256, vector FROM vector_rows "
            "WHERE namespace = ? ORDER BY fact_id",
            (proj_initial.namespace,),
        ).fetchall()
        rows_initial_dump = [
            (r["fact_id"], r["text"], r["text_sha256"], r["vector"]) for r in rows_initial
        ]
    finally:
        conn.close()

    # Now attempt build with an embedder that fails after 2 documents
    failing_embedder = DeterministicFakeEmbedder(
        profile=embedder.profile, fail_after=2
    )
    with pytest.raises(RuntimeError, match="simulated mid-build embedder failure"):
        vec_store.build(pub_store, WS, REPO, _SNAP_A, failing_embedder)

    # Prior projection and rows must be intact and unchanged
    proj_after = vec_store.get_projection(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=_SNAP_A,
        generation_id=proj_initial.generation_id,
    )
    assert proj_after is not None
    assert proj_after.projection_sha256 == proj_initial.projection_sha256
    assert proj_after.vectors_sha256 == proj_initial.vectors_sha256
    assert proj_after.vector_count == proj_initial.vector_count

    conn = sqlite3.connect(str(tmp_path / "vectors" / "vectors.sqlite"))
    try:
        conn.row_factory = sqlite3.Row
        rows_after = conn.execute(
            "SELECT fact_id, text, text_sha256, vector FROM vector_rows "
            "WHERE namespace = ? ORDER BY fact_id",
            (proj_initial.namespace,),
        ).fetchall()
        rows_after_dump = [
            (r["fact_id"], r["text"], r["text_sha256"], r["vector"]) for r in rows_after
        ]
    finally:
        conn.close()

    assert rows_after_dump == rows_initial_dump


def test_rebuild_repeats_digests_and_search_hits(tmp_path: Path) -> None:
    pub_store, _ = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    vec_dir = tmp_path / "vectors"
    vec_store = VectorProjectionStore(vec_dir)
    embedder = DeterministicFakeEmbedder()

    # Build original
    proj_orig = vec_store.build(pub_store, WS, REPO, _SNAP_A, embedder)
    q_vec = embedder.embed_query("User class")
    hits_orig = vec_store.search(
        pub_store, WS, REPO, _SNAP_A, proj_orig.generation_id, q_vec, k=3
    )

    # Delete vectors.sqlite
    db_file = vec_dir / "vectors.sqlite"
    assert db_file.exists()
    db_file.unlink()

    # Rebuild from same publication
    vec_store_rebuilt = VectorProjectionStore(vec_dir)
    proj_rebuilt = vec_store_rebuilt.build(pub_store, WS, REPO, _SNAP_A, embedder)

    assert proj_rebuilt.projection_sha256 == proj_orig.projection_sha256
    assert proj_rebuilt.vectors_sha256 == proj_orig.vectors_sha256
    assert proj_rebuilt.vector_count == proj_orig.vector_count

    hits_rebuilt = vec_store_rebuilt.search(
        pub_store, WS, REPO, _SNAP_A, proj_rebuilt.generation_id, q_vec, k=3
    )
    assert len(hits_rebuilt) == len(hits_orig)
    for h1, h2 in zip(hits_orig, hits_rebuilt):
        assert h1.fact_id == h2.fact_id
        assert h1.text == h2.text
        assert h1.score == pytest.approx(h2.score)


def test_rebuild_from_restored_structural_publication(tmp_path: Path) -> None:
    structural_dir = tmp_path / "structural"
    pub_store, _ = _publish_fixture(structural_dir, "A", _SNAP_A)
    vec_dir1 = tmp_path / "vec1"
    vec_store1 = VectorProjectionStore(vec_dir1)
    embedder = DeterministicFakeEmbedder()

    proj1 = vec_store1.build(pub_store, WS, REPO, _SNAP_A, embedder)
    q_vec = embedder.embed_query("auth user")
    hits1 = vec_store1.search(pub_store, WS, REPO, _SNAP_A, proj1.generation_id, q_vec, k=3)

    # Backup the structural root
    backup_dir = tmp_path / "backup"
    create_backup(structural_dir, backup_dir)

    # Rebuild structural store from records into new target directory
    restored_dir = tmp_path / "structural_restored"
    rebuild_from_records(backup_dir, restored_dir)

    # Build vector projection from restored structural store
    restored_pub = StructuralPublicationStore(restored_dir)
    vec_dir2 = tmp_path / "vec2"
    vec_store2 = VectorProjectionStore(vec_dir2)
    proj2 = vec_store2.build(restored_pub, WS, REPO, _SNAP_A, embedder)

    assert proj2.projection_sha256 == proj1.projection_sha256
    assert proj2.vectors_sha256 == proj1.vectors_sha256
    assert proj2.vector_count == proj1.vector_count

    hits2 = vec_store2.search(restored_pub, WS, REPO, _SNAP_A, proj2.generation_id, q_vec, k=3)
    assert len(hits2) == len(hits1)
    for h1, h2 in zip(hits1, hits2):
        assert h1.fact_id == h2.fact_id
        assert h1.text == h2.text
        assert h1.score == pytest.approx(h2.score)


def test_backup_and_rollback_restore_vectors_sqlite_byte_identical(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    pub_store, _ = _publish_fixture(state_root, "A", _SNAP_A)
    vec_store = VectorProjectionStore(state_root)
    embedder = DeterministicFakeEmbedder()
    vec_store.build(pub_store, WS, REPO, _SNAP_A, embedder)

    backup_dir = tmp_path / "backup"
    manifest_info = create_backup(state_root, backup_dir)
    assert "vectors" in manifest_info["stores"]
    assert (backup_dir / "vectors.sqlite").exists()
    backup_bytes = (backup_dir / "vectors.sqlite").read_bytes()

    # Mutate vectors.sqlite in state_root
    conn = sqlite3.connect(str(state_root / "vectors.sqlite"))
    try:
        conn.execute("DELETE FROM vector_rows")
        conn.commit()
    finally:
        conn.close()

    assert (state_root / "vectors.sqlite").read_bytes() != backup_bytes

    # Rollback
    rollback(backup_dir, state_root)

    # vectors.sqlite must be restored byte-identical
    restored_bytes = (state_root / "vectors.sqlite").read_bytes()
    assert restored_bytes == backup_bytes


def test_backup_without_vectors_sqlite_still_rebuilds(tmp_path: Path) -> None:
    state_root = tmp_path / "state_no_vec"
    _publish_fixture(state_root, "A", _SNAP_A)
    # No vectors.sqlite exists in state_root

    backup_dir = tmp_path / "backup"
    manifest_info = create_backup(state_root, backup_dir)
    assert "vectors" not in manifest_info["stores"]
    assert not (backup_dir / "vectors.sqlite").exists()

    target_root = tmp_path / "target_rebuilt"
    report = rebuild_from_records(backup_dir, target_root)
    assert "structural_publications" in report["stores"]
    assert "vectors" not in report["stores"]


def test_retired_or_unpublished_snapshot_refused(tmp_path: Path) -> None:
    structural_dir = tmp_path / "structural"
    facts_bytes, receipt_bytes, _insights, _raw = load_staged_fixture("A")
    store = StructuralPublicationStore(structural_dir)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=_SNAP_A,
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        manifest_files=[],
    )
    # Staged but NOT published
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    embedder = DeterministicFakeEmbedder()

    with pytest.raises(SnapshotNotPublishedError) as excinfo:
        vec_store.build(store, WS, REPO, _SNAP_A, embedder)
    assert excinfo.value.code == "snapshot_not_published"
    assert excinfo.value.status_code == 400

    with pytest.raises(SnapshotNotPublishedError):
        vec_store.search(store, WS, REPO, _SNAP_A, embedder.profile.generation_id, [1.0, 0.0, 0.0, 0.0])

    # Now publish and then retire
    store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=_SNAP_A)
    vec_store.build(store, WS, REPO, _SNAP_A, embedder)

    store.retire(workspace_id=WS, repository_id=REPO, snapshot_id=_SNAP_A)

    with pytest.raises(SnapshotNotPublishedError):
        vec_store.build(store, WS, REPO, _SNAP_A, embedder)

    with pytest.raises(SnapshotNotPublishedError):
        vec_store.search(store, WS, REPO, _SNAP_A, embedder.profile.generation_id, [1.0, 0.0, 0.0, 0.0])


def test_unbuilt_generation_refused(tmp_path: Path) -> None:
    pub_store, _ = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    vec_store = VectorProjectionStore(tmp_path / "vectors")
    embedder = DeterministicFakeEmbedder()

    vec_store.build(pub_store, WS, REPO, _SNAP_A, embedder)

    unbuilt_gen = "f" * 64
    with pytest.raises(KnowledgeError) as excinfo:
        vec_store.search(pub_store, WS, REPO, _SNAP_A, unbuilt_gen, [1.0, 0.0, 0.0, 0.0])
    assert excinfo.value.code == "vector_generation_mismatch"
    assert excinfo.value.status_code == 409


def test_cli_build_json_and_human(tmp_path: Path) -> None:
    pub_store, _ = _publish_fixture(tmp_path / "structural", "A", _SNAP_A)
    embedder = DeterministicFakeEmbedder()

    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(_routes_payload(embedder.profile)), encoding="utf-8")

    stdout = io.StringIO()
    snap_arg = f"{WS}:{REPO}:{_SNAP_A}"

    code = vectors_main(
        [
            "build",
            "--state-dir",
            str(tmp_path / "vectors"),
            "--structural-state-dir",
            str(tmp_path / "structural"),
            "--snapshot",
            snap_arg,
            "--routes",
            str(routes_file),
            "--json",
        ],
        embedder=embedder,
        stdout=stdout,
    )
    assert code == EXIT_OK
    payload = json.loads(stdout.getvalue())
    assert payload["namespace"] == f"{WS}:{REPO}@{_SNAP_A}"
    assert payload["generation_id"] == embedder.profile.generation_id
    assert payload["vector_count"] == 19
    assert len(payload["projection_sha256"]) == 64
    assert len(payload["vectors_sha256"]) == 64
    assert payload["route"] == "embed-gpu"

    # Test human output
    stdout_human = io.StringIO()
    code_human = vectors_main(
        [
            "build",
            "--state-dir",
            str(tmp_path / "vectors"),
            "--structural-state-dir",
            str(tmp_path / "structural"),
            "--snapshot",
            snap_arg,
            "--routes",
            str(routes_file),
        ],
        embedder=embedder,
        stdout=stdout_human,
    )
    assert code_human == EXIT_OK
    human_text = stdout_human.getvalue()
    assert f"{WS}:{REPO}@{_SNAP_A}" in human_text
    assert f"generation_id={embedder.profile.generation_id}" in human_text
    assert "vector_count=19" in human_text
    assert "route=embed-gpu" in human_text


def test_cli_errors_exit_2(tmp_path: Path) -> None:
    embedder = DeterministicFakeEmbedder()
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(_routes_payload(embedder.profile)), encoding="utf-8")

    stderr = io.StringIO()

    # 1. Invalid snapshot format
    code = vectors_main(
        [
            "build",
            "--state-dir",
            str(tmp_path / "vectors"),
            "--structural-state-dir",
            str(tmp_path / "structural"),
            "--snapshot",
            "not-valid-snapshot",
            "--routes",
            str(routes_file),
        ],
        stderr=stderr,
    )
    assert code == EXIT_ERROR
    assert "WS:REPO:SNAP" in stderr.getvalue()

    # 2. Invalid routes file
    stderr2 = io.StringIO()
    code2 = vectors_main(
        [
            "build",
            "--state-dir",
            str(tmp_path / "vectors"),
            "--structural-state-dir",
            str(tmp_path / "structural"),
            "--snapshot",
            f"{WS}:{REPO}:{_SNAP_A}",
            "--routes",
            str(tmp_path / "nonexistent.json"),
        ],
        stderr=stderr2,
    )
    assert code2 == EXIT_ERROR

    # 3. Unpublished snapshot
    stderr3 = io.StringIO()
    # Structural store exists but snapshot is not published
    StructuralPublicationStore(tmp_path / "empty_structural")
    code3 = vectors_main(
        [
            "build",
            "--state-dir",
            str(tmp_path / "vectors"),
            "--structural-state-dir",
            str(tmp_path / "empty_structural"),
            "--snapshot",
            f"{WS}:{REPO}:{_SNAP_A}",
            "--routes",
            str(routes_file),
        ],
        embedder=embedder,
        stderr=stderr3,
    )
    assert code3 == EXIT_ERROR
    assert "snapshot_not_published" in stderr3.getvalue()
