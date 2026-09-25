"""OMP-309 / FK-4: versioned structural graph with candidate snapshot isolation.

The suite defends the five FK-4 acceptance contracts:

1. two candidates coexist — publishing candidate B leaves candidate A's
   structural query results byte-identical;
2. staged, aborted and partially-ingested snapshots are never visible, and the
   previously published snapshot keeps answering;
3. extraction and these tests run with no knowledge engine present, and the
   structural input an alternative engine consumes is documented and stable;
4. every published fact carries repository identity, snapshot id, source /
   config / processing versions and file provenance;
5. a per-snapshot coverage manifest lists extracted and skipped files with
   reasons.

The Enola fact and receipt shapes below are the real ``facts.jsonl`` /
``receipt.json`` shapes produced by Enola 0.4.18 (``format_version: 1``), with
the candidate A/B corpus trimmed to the facts each assertion needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from omp_work.knowledge_contracts import ManifestFile, validate_manifest_path
from omp_work.knowledge_namespace import (
    fact_identity_string,
    fact_node_id,
    namespace_digest,
    snapshot_namespace,
    structural_fact_id,
    validate_snapshot_id,
)
from omp_work.knowledge_publication import (
    SnapshotConflictError,
    SnapshotInvisibleError,
    StructuralPublicationStore,
)
from omp_work.knowledge_structural import (
    STRUCTURAL_PROJECTION_VERSION,
    CoverageEntry,
    DuplicateFactIdentityError,
    EnolaStructuralError,
    build_coverage_manifest,
    parse_enola_facts,
    parse_enola_receipt,
    project_structural_snapshot,
)
from omp_work.v1.canonical import canonical_json

WS = uuid5(NAMESPACE_URL, "omp-test/fk4-workspace")
REPO = uuid5(NAMESPACE_URL, "omp-test/fk4-repository")
ENOLA_VERSION = "0.4.18"
CONFIG_HASH = "f8233549c49910b52327defe5523379e05b55de00b039017b649b3c0545ee96"

PY_ALPHA = "950270693f0b0c8255a1dbb940c4247d"
PY_BETA = "558e71fa82a3af4251c74e44b5c171d1"
PY_PKG = "6aba79c9ad781b7872bdfbd62c6e86a8"

# Candidate A declares py/pkg/gamma.py::gamma_only_a; candidate B replaces that
# file's only symbol with gamma_only_b and adds beta_only_b to beta.py. Both
# candidates keep the shared py/pkg/*.normalize symbols and their identity.
FACTS_A: list[dict[str, object]] = [
    {
        "kind": "module",
        "name": "py/pkg",
        "file": "py/pkg",
        "repo": "enola-fixture",
        "props": {"language": "python"},
        "id": PY_PKG,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/alpha.normalize",
        "file": "py/pkg/alpha.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "relations": [{"kind": "declares", "target": "py/pkg", "target_id": PY_PKG}],
        "id": PY_ALPHA,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/beta.normalize",
        "file": "py/pkg/beta.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "relations": [{"kind": "declares", "target": "py/pkg", "target_id": PY_PKG}],
        "id": PY_BETA,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/main.run",
        "file": "py/pkg/main.py",
        "line": 5,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "relations": [
            {
                "kind": "calls",
                "target": "py/pkg/alpha.normalize",
                "target_id": PY_ALPHA,
            },
            {"kind": "calls", "target": "py/pkg/beta.normalize", "target_id": PY_BETA},
        ],
        "id": "03f36f1044ad75664b8908f77db31782",
    },
    {
        "kind": "symbol",
        "name": "py/pkg/gamma.gamma_only_a",
        "file": "py/pkg/gamma.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "id": "aaaa1111222233334444555566667777",
    },
]

FACTS_B: list[dict[str, object]] = [
    {
        "kind": "module",
        "name": "py/pkg",
        "file": "py/pkg",
        "repo": "enola-fixture",
        "props": {"language": "python"},
        "id": PY_PKG,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/alpha.normalize",
        "file": "py/pkg/alpha.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "relations": [{"kind": "declares", "target": "py/pkg", "target_id": PY_PKG}],
        "id": PY_ALPHA,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/beta.normalize",
        "file": "py/pkg/beta.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "relations": [{"kind": "declares", "target": "py/pkg", "target_id": PY_PKG}],
        "id": PY_BETA,
    },
    {
        "kind": "symbol",
        "name": "py/pkg/beta.beta_only_b",
        "file": "py/pkg/beta.py",
        "line": 5,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "id": "fc14f7d46ff88c128294af29b2d32bb9",
    },
    {
        "kind": "symbol",
        "name": "py/pkg/gamma.gamma_only_b",
        "file": "py/pkg/gamma.py",
        "line": 1,
        "repo": "enola-fixture",
        "props": {"language": "python", "symbol_kind": "function"},
        "id": "f128a1fe125ded086d16045f5a72dceb",
    },
]

QUALITY: dict[str, object] = {
    "files_seen": 10,
    "files_parsed": 7,
    "files_skipped": 2,
    "dirs_skipped": 1,
    "skipped_sample": [
        ".git/ (glob: **/.git/**)",
        "py/tests/test_alpha.py (glob: **/test_*.py)",
    ],
    "parse_errors": 0,
    "census": {
        "files_walked": 12,
        "parsed": 7,
        "excluded_by_ignore": 2,
        "excluded_by_kind": 1,
        "excluded_kinds": {".mts": 1},
    },
}


def _snapshot_id(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _facts_bytes(rows: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n" for row in rows
    )


def _receipt_bytes(
    *,
    enola_version: str | None = ENOLA_VERSION,
    config_hash: str | None = CONFIG_HASH,
    quality: dict[str, object] | None = None,
) -> bytes:
    receipt: dict[str, object] = {
        "snapshot_id": "sha256:" + "0" * 64,
        "format_version": 1,
        "extractor_version": "v265",
        "generated_at": "2026-09-12T12:54:41Z",
        "fact_count": 19,
        "quality": QUALITY if quality is None else quality,
    }
    if enola_version is not None:
        receipt["enola_version"] = enola_version
    if config_hash is not None:
        receipt["config_hash"] = f"sha256:{config_hash}"
    return json.dumps(receipt).encode("utf-8")


def _manifest_file(path: str, kind: str = "base") -> ManifestFile:
    if kind == "deleted":
        return ManifestFile(path=path, kind=kind, sha256=None, size=None)
    return ManifestFile(path=path, kind=kind, sha256="a" * 64, size=10)


_SNAPSHOT_MANIFEST = [
    _manifest_file("py/pkg/alpha.py"),
    _manifest_file("py/pkg/beta.py"),
    _manifest_file("py/pkg/gamma.py"),
    _manifest_file("py/pkg/main.py"),
    _manifest_file("py/pkg/old.py", kind="deleted"),
    _manifest_file("py/pkg/other.py"),
    _manifest_file("py/tests/test_alpha.py"),
    _manifest_file("ts/src/extra.mts"),
]


def _project(
    rows: list[dict[str, object]],
    *,
    snapshot_id: str,
    repository_id: UUID = REPO,
    receipt: bytes | None = None,
):
    return project_structural_snapshot(
        workspace_id=WS,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts_bytes=_facts_bytes(rows),
        receipt_bytes=_receipt_bytes() if receipt is None else receipt,
    )


def _store_with_published(
    state_dir: Path,
    rows: list[dict[str, object]],
    *,
    snapshot_id: str,
    repository_id: UUID = REPO,
    manifest_files: list[ManifestFile] | None = None,
) -> StructuralPublicationStore:
    store = StructuralPublicationStore(state_dir)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts_bytes=_facts_bytes(rows),
        receipt_bytes=_receipt_bytes(),
        manifest_files=manifest_files if manifest_files is not None else [],
    )
    store.publish(workspace_id=WS, repository_id=repository_id, snapshot_id=snapshot_id)
    return store


# --------------------------------------------------------------------------
# acceptance 1: two candidates coexist; B never rewrites A
# --------------------------------------------------------------------------


def test_publishing_candidate_b_leaves_candidate_a_results_byte_identical(
    tmp_path: Path,
) -> None:
    snap_a = _snapshot_id("candidate-a")
    snap_b = _snapshot_id("candidate-b")

    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap_a)
    before = store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_a)
    assert before.namespace == snapshot_namespace(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap_a
    )
    assert before.total_matched > 0

    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap_b,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    # Staging B alone must not disturb A.
    assert (
        store.query(
            workspace_id=WS, repository_id=REPO, snapshot_id=snap_a
        ).result_sha256()
        == before.result_sha256()
    )

    store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=snap_b)
    after = store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_a)

    # Byte-identical results: every hit, id and hash is unchanged by B.
    assert after.result_sha256() == before.result_sha256()
    assert after.model_dump(mode="json") == before.model_dump(mode="json")

    # Both candidates remain retained active snapshots of the repository.
    active = store.active_snapshots(workspace_id=WS, repository_id=REPO)
    assert sorted(publication.snapshot_id for publication in active) == sorted(
        [snap_a, snap_b]
    )
    assert all(publication.state == "published" for publication in active)
    assert all(publication.published_at is not None for publication in active)

    # B answers from its own namespace with its own (different) content.
    candidate_b = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap_b, text="gamma"
    )
    candidate_a = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap_a, text="gamma"
    )
    assert [hit.name for hit in candidate_a.hits] == ["py/pkg/gamma.gamma_only_a"]
    assert [hit.name for hit in candidate_b.hits] == ["py/pkg/gamma.gamma_only_b"]
    assert candidate_a.namespace != candidate_b.namespace


def test_same_fact_in_two_candidates_shares_identity_but_not_node_id(
    tmp_path: Path,
) -> None:
    snap_a = _snapshot_id("candidate-a")
    snap_b = _snapshot_id("candidate-b")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap_a)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap_b,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=snap_b)

    hit_a = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap_a, text="beta.normalize"
    ).hits[0]
    hit_b = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap_b, text="beta.normalize"
    ).hits[0]

    # The fact identity is stable across snapshots, so the node id must be
    # namespace-bound: otherwise candidate B would overwrite candidate A.
    assert hit_a.structural_fact_id == hit_b.structural_fact_id
    assert hit_a.node_id != hit_b.node_id
    assert hit_a.snapshot_id == snap_a
    assert hit_b.snapshot_id == snap_b


# --------------------------------------------------------------------------
# acceptance 2: partial, staged and aborted snapshots are never visible
# --------------------------------------------------------------------------


def test_staged_snapshot_is_invisible_until_published(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-a")
    store = StructuralPublicationStore(tmp_path)
    staged = store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_A),
        receipt_bytes=_receipt_bytes(),
        manifest_files=_SNAPSHOT_MANIFEST,
    )
    assert staged.state == "staged"
    assert staged.published_at is None
    assert (
        store.is_published(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
        is False
    )
    assert store.active_snapshots(workspace_id=WS, repository_id=REPO) == ()

    with pytest.raises(SnapshotInvisibleError) as excinfo:
        store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    assert excinfo.value.code == "snapshot_not_visible"
    with pytest.raises(SnapshotInvisibleError):
        store.load(workspace_id=WS, repository_id=REPO, snapshot_id=snap)

    published = store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    assert published.state == "published"
    assert published.published_at is not None
    assert (
        store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap).total_matched
        > 0
    )


def test_aborted_ingest_leaves_previous_published_snapshot_unchanged(
    tmp_path: Path,
) -> None:
    snap_a = _snapshot_id("candidate-a")
    snap_b = _snapshot_id("candidate-b")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap_a)
    before = store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_a)

    # Candidate B is projected and staged, then the ingest aborts (crash mid-way).
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap_b,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    aborted = store.abort(workspace_id=WS, repository_id=REPO, snapshot_id=snap_b)
    assert aborted.state == "aborted"

    assert (
        store.query(
            workspace_id=WS, repository_id=REPO, snapshot_id=snap_a
        ).result_sha256()
        == before.result_sha256()
    )
    with pytest.raises(SnapshotInvisibleError):
        store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_b)
    assert [
        publication.snapshot_id
        for publication in store.active_snapshots(workspace_id=WS, repository_id=REPO)
    ] == [snap_a]


def test_aborted_snapshot_can_be_restaged_and_published(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-b")
    store = StructuralPublicationStore(tmp_path)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    store.abort(workspace_id=WS, repository_id=REPO, snapshot_id=snap)

    restaged = store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    assert restaged.state == "staged"
    store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    assert store.is_published(workspace_id=WS, repository_id=REPO, snapshot_id=snap)


def test_same_snapshot_id_with_different_content_is_refused(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-a")
    store = StructuralPublicationStore(tmp_path)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_A),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    with pytest.raises(SnapshotConflictError) as excinfo:
        store.stage_enola_snapshot(
            workspace_id=WS,
            repository_id=REPO,
            snapshot_id=snap,
            facts_bytes=_facts_bytes(FACTS_B),
            receipt_bytes=_receipt_bytes(),
            manifest_files=[],
        )
    assert excinfo.value.code == "snapshot_conflict"

    # Idempotent re-stage of identical content must not un-publish a snapshot.
    store.publish(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    again = store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_A),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    assert again.state == "published"
    assert store.is_published(workspace_id=WS, repository_id=REPO, snapshot_id=snap)


def test_published_snapshot_survives_store_reopen(tmp_path: Path) -> None:
    snap_a = _snapshot_id("candidate-a")
    snap_b = _snapshot_id("candidate-b")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap_a)
    before = store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_a)

    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=REPO,
        snapshot_id=snap_b,
        facts_bytes=_facts_bytes(FACTS_B),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    # Simulate the process dying before candidate B is published.
    reopened = StructuralPublicationStore(tmp_path)
    assert [
        publication.snapshot_id
        for publication in reopened.active_snapshots(
            workspace_id=WS, repository_id=REPO
        )
    ] == [snap_a]
    with pytest.raises(SnapshotInvisibleError):
        reopened.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap_b)
    assert (
        reopened.query(
            workspace_id=WS, repository_id=REPO, snapshot_id=snap_a
        ).result_sha256()
        == before.result_sha256()
    )


def test_retired_snapshot_leaves_queries(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-a")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap)
    retired = store.retire(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    assert retired.state == "retired"
    assert store.active_snapshots(workspace_id=WS, repository_id=REPO) == ()
    with pytest.raises(SnapshotInvisibleError):
        store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap)


# --------------------------------------------------------------------------
# acceptance 3: extraction is engine-neutral and runs with no engine present
# --------------------------------------------------------------------------


def test_extraction_and_staging_run_with_engine_imports_blocked() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    program = """
import sys
import uuid

class _BlockEngines:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "cognee" or fullname.startswith("cognee."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, _BlockEngines())

from omp_work.knowledge_namespace import structural_fact_id
from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.knowledge_structural import STRUCTURAL_PROJECTION_VERSION

workspace_id = uuid.uuid5(uuid.NAMESPACE_URL, "omp-test/fk4-engineless-workspace")
repository_id = uuid.uuid5(uuid.NAMESPACE_URL, "omp-test/fk4-engineless-repository")
snapshot_id = "c" * 64
facts = '{"kind":"symbol","name":"a.b","file":"src/a.py","repo":"r","id":"' + "a" * 32 + '"}'
receipt = '{"enola_version":"0.4.18","config_hash":"sha256:' + "b" * 64 + '"}'
store = StructuralPublicationStore(":memory:")
store.stage_enola_snapshot(
    workspace_id=workspace_id,
    repository_id=repository_id,
    snapshot_id=snapshot_id,
    facts_bytes=(facts + chr(10)).encode(),
    receipt_bytes=receipt.encode(),
    manifest_files=(),
)
store.publish(
    workspace_id=workspace_id,
    repository_id=repository_id,
    snapshot_id=snapshot_id,
)
result = store.query(
    workspace_id=workspace_id,
    repository_id=repository_id,
    snapshot_id=snapshot_id,
)
assert result.total_matched == 1, result
assert result.hits[0].processing_version == STRUCTURAL_PROJECTION_VERSION
assert structural_fact_id(
    repository_id=repository_id,
    enola_repo="r",
    kind="symbol",
    name="a.b",
    file="src/a.py",
) == result.hits[0].structural_fact_id
print("engine-absent-ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "engine-absent-ok" in completed.stdout


def test_projection_round_trips_as_documented_engine_input() -> None:
    snap = _snapshot_id("candidate-a")
    projection = _project(FACTS_A, snapshot_id=snap)

    payload = projection.model_dump(mode="json")
    # The documented structural input an alternative engine consumes.
    assert set(payload) == {
        "workspace_id",
        "repository_id",
        "snapshot_id",
        "namespace",
        "source_version",
        "config_version",
        "processing_version",
        "nodes",
        "edges",
        "raw_fact_count",
        "graph_sha256",
    }
    assert set(payload["nodes"][0]) == {
        "structural_fact_id",
        "namespace",
        "node_id",
        "kind",
        "name",
        "file",
        "enola_repository",
        "line",
        "properties",
        "observations",
        "provenance",
    }
    assert set(payload["edges"][0]) == {"source_fact_id", "target_fact_id", "kind"}

    reloaded = type(projection).model_validate_json(canonical_json(payload))
    assert reloaded == projection


def test_projection_is_order_independent_and_deterministic() -> None:
    snap = _snapshot_id("candidate-a")
    first = _project(FACTS_A, snapshot_id=snap)
    reordered = _project(list(reversed(FACTS_A)), snapshot_id=snap)

    # Two engines reading the same facts in any order must agree on the graph.
    assert reordered.graph_sha256 == first.graph_sha256
    assert [node.structural_fact_id for node in reordered.nodes] == [
        node.structural_fact_id for node in first.nodes
    ]
    assert reordered.edges == first.edges

    # A byte-different snapshot projects to a byte-different graph.
    assert _project(FACTS_B, snapshot_id=_snapshot_id("candidate-b")).graph_sha256 != (
        first.graph_sha256
    )


def test_raw_observations_are_kept_separate_from_the_projected_node() -> None:
    snap = _snapshot_id("candidate-a")
    duplicated = [FACTS_A[1], FACTS_A[1], FACTS_A[2]]
    projection = _project(duplicated, snapshot_id=snap)

    assert projection.raw_fact_count == 3
    assert len(projection.nodes) == 2
    alpha = projection.nodes[0]
    assert alpha.name == "py/pkg/alpha.normalize"
    assert [observation.raw_index for observation in alpha.observations] == [0, 1]
    assert [observation.raw_fact_id for observation in alpha.observations] == [
        PY_ALPHA,
        PY_ALPHA,
    ]
    assert projection.edges == ()


def test_one_raw_id_with_two_identities_is_refused() -> None:
    conflicting = [FACTS_A[1], dict(FACTS_A[1], file="py/pkg/zed.py")]
    with pytest.raises(DuplicateFactIdentityError) as excinfo:
        _project(conflicting, snapshot_id=_snapshot_id("candidate-a"))
    assert excinfo.value.code == "duplicate_fact_identity"
    assert excinfo.value.raw_fact_id == PY_ALPHA


def test_missing_engine_version_metadata_fails_closed() -> None:
    with pytest.raises(EnolaStructuralError) as excinfo:
        _project(FACTS_A, snapshot_id=_snapshot_id("candidate-a"), receipt=b"{}")
    assert excinfo.value.code == "corrupt_receipt"

    # No Git metadata is present in this receipt, and none is invented: the
    # projection carries versions only, never a claimed commit or tree id.
    projection = _project(FACTS_A, snapshot_id=_snapshot_id("candidate-a"))
    assert "commit" not in projection.model_dump(mode="json")
    assert projection.source_version == ENOLA_VERSION


@pytest.mark.parametrize("bad", ["", "nothex", "A" * 64, "1" * 63, "1" * 65])
def test_snapshot_id_must_be_64_lower_hex(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_snapshot_id(bad)
    with pytest.raises(ValueError):
        snapshot_namespace(workspace_id=WS, repository_id=REPO, snapshot_id=bad)


def test_projector_version_must_be_one_this_code_applied(tmp_path: Path) -> None:
    # A caller cannot stamp a snapshot with a projection this code never ran.
    with pytest.raises(EnolaStructuralError) as excinfo:
        project_structural_snapshot(
            workspace_id=WS,
            repository_id=REPO,
            snapshot_id=_snapshot_id("candidate-a"),
            facts_bytes=_facts_bytes(FACTS_A),
            receipt_bytes=_receipt_bytes(),
            processing_version="structural-projection/v99",
        )
    assert excinfo.value.code == "unsupported_projection_version"

    projection = _project(FACTS_A, snapshot_id=_snapshot_id("candidate-a"))
    coverage = build_coverage_manifest(projection, manifest_files=_SNAPSHOT_MANIFEST)
    forged = projection.model_copy(
        update={"processing_version": "structural-projection/v99"}
    )
    store = StructuralPublicationStore(tmp_path)
    with pytest.raises(EnolaStructuralError) as excinfo:
        store.stage(projection=forged, coverage=coverage)
    assert excinfo.value.code == "unsupported_projection_version"


# --------------------------------------------------------------------------
# acceptance 4: identity, versions and file provenance on every published fact
# --------------------------------------------------------------------------


def test_every_published_fact_carries_full_provenance(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-a")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap)
    result = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap, text="*", limit=50
    )

    assert result.total_matched == len(FACTS_A)
    for hit in result.hits:
        assert hit.workspace_id == WS
        assert hit.repository_id == REPO
        assert hit.snapshot_id == snap
        assert hit.source_version == ENOLA_VERSION
        assert hit.config_version == CONFIG_HASH
        assert hit.processing_version == STRUCTURAL_PROJECTION_VERSION
        assert validate_manifest_path(hit.file) == hit.file

    gamma = next(
        hit for hit in result.hits if hit.kind == "symbol" and "gamma" in hit.name
    )
    # File provenance: the source file and its line, not just the symbol name.
    assert gamma.file == "py/pkg/gamma.py"
    assert gamma.line == 1

    # The engine-derived config hash is stored without its `sha256:` prefix and
    # the applied projector version is our own, never the engine's.
    assert gamma.config_version == CONFIG_HASH
    assert gamma.processing_version != gamma.source_version


def test_query_reports_total_matched_and_caps_returned_hits(tmp_path: Path) -> None:
    snap = _snapshot_id("candidate-a")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap)
    capped = store.query(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap, text="py/pkg", limit=2
    )
    # The consumer must be able to tell a truncated page from a complete one.
    assert capped.total_matched == 5
    assert len(capped.hits) == 2
    assert [hit.structural_fact_id for hit in capped.hits] == [
        node.structural_fact_id
        for node in _project(FACTS_A, snapshot_id=snap).nodes
        if "py/pkg" in node.name.lower() or "py/pkg" in node.kind.lower()
    ][:2]

    with pytest.raises(ValueError):
        store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap, limit=0)


def test_repository_identity_is_retained_per_publication(tmp_path: Path) -> None:
    other_repo = uuid5(NAMESPACE_URL, "omp-test/fk4-other-repository")
    snap = _snapshot_id("candidate-a")
    store = _store_with_published(tmp_path, FACTS_A, snapshot_id=snap)

    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=other_repo,
        snapshot_id=snap,
        facts_bytes=_facts_bytes(FACTS_A),
        receipt_bytes=_receipt_bytes(),
        manifest_files=[],
    )
    store.publish(workspace_id=WS, repository_id=other_repo, snapshot_id=snap)

    original = store.query(workspace_id=WS, repository_id=REPO, snapshot_id=snap)
    other = store.query(workspace_id=WS, repository_id=other_repo, snapshot_id=snap)

    # Repository identity is never rewritten: the retained publication keeps the
    # repository it was captured under, and the namespace stays disjoint.
    assert all(hit.repository_id == REPO for hit in original.hits)
    assert all(hit.repository_id == other_repo for hit in other.hits)
    assert original.namespace != other.namespace
    assert all(hit.repository_id != UUID(int=0) for hit in original.hits)


# --------------------------------------------------------------------------
# acceptance 5: coverage manifest per snapshot
# --------------------------------------------------------------------------


def test_coverage_manifest_lists_extracted_and_skipped_with_reasons() -> None:
    snap = _snapshot_id("candidate-a")
    projection = _project(FACTS_A, snapshot_id=snap)
    receipt = parse_enola_receipt(_receipt_bytes())
    coverage = build_coverage_manifest(
        projection, manifest_files=_SNAPSHOT_MANIFEST, quality=receipt.quality
    )

    by_path = {entry.path: entry for entry in coverage.files}
    assert by_path["py/pkg/alpha.py"] == CoverageEntry(
        path="py/pkg/alpha.py", state="extracted", reason="extracted", fact_count=1
    )
    assert by_path["py/pkg/old.py"].state == "skipped"
    assert by_path["py/pkg/old.py"].reason == "deleted"
    assert by_path["py/tests/test_alpha.py"].reason == "excluded: glob: **/test_*.py"
    assert by_path["ts/src/extra.mts"].reason == "excluded_kind:.mts"
    assert by_path["py/pkg/other.py"].reason == "no_facts_emitted"

    # A fact-bearing path absent from the file manifest is still reported, so
    # coverage can never claim a snapshot is fully covered by omission.
    assert by_path["py/pkg"].state == "extracted"

    assert coverage.extracted_files == sum(
        1 for entry in coverage.files if entry.state == "extracted"
    )
    assert coverage.skipped_files == sum(
        1 for entry in coverage.files if entry.state == "skipped"
    )
    assert coverage.extracted_files + coverage.skipped_files == len(coverage.files)
    assert coverage.snapshot_id == snap
    assert coverage.repository_id == REPO
    assert coverage.raw_fact_count == projection.raw_fact_count
    assert [entry.path for entry in coverage.files] == sorted(
        by_path, key=lambda path: path.encode("utf-8")
    )
    assert all(
        entry.reason in {"extracted", "deleted", "no_facts_emitted"}
        or entry.reason.startswith(("excluded: ", "excluded_kind:"))
        for entry in coverage.files
    )


def test_coverage_manifest_is_retained_with_the_published_snapshot(
    tmp_path: Path,
) -> None:
    snap = _snapshot_id("candidate-a")
    store = _store_with_published(
        tmp_path, FACTS_A, snapshot_id=snap, manifest_files=_SNAPSHOT_MANIFEST
    )
    _projection, coverage = store.load(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap
    )
    assert coverage.snapshot_id == snap
    assert coverage.skipped_files >= 1
    assert any(entry.reason == "deleted" for entry in coverage.skipped())


# --------------------------------------------------------------------------
# namespace adapter unit contracts
# --------------------------------------------------------------------------


def test_fact_identity_includes_the_file() -> None:
    snap = _snapshot_id("candidate-a")
    base = {
        "repository_id": REPO,
        "enola_repo": "enola-fixture",
        "kind": "symbol",
        "name": "ts/src.normalize",
    }
    in_alpha = structural_fact_id(**base, file="ts/src/alpha.ts")
    in_beta = structural_fact_id(**base, file="ts/src/beta.ts")
    assert in_alpha != in_beta
    assert fact_identity_string(**base, file="ts/src/alpha.ts") != fact_identity_string(
        **base, file="ts/src/beta.ts"
    )

    # Same identity, different namespace ⇒ different node id and digest.
    namespace = snapshot_namespace(
        workspace_id=WS, repository_id=REPO, snapshot_id=snap
    )
    other = snapshot_namespace(
        workspace_id=WS, repository_id=REPO, snapshot_id=_snapshot_id("candidate-b")
    )
    assert fact_node_id(namespace=namespace, structural_fact_id=in_alpha) != (
        fact_node_id(namespace=other, structural_fact_id=in_alpha)
    )
    assert namespace_digest(namespace=namespace, structural_fact_id=in_alpha) != (
        namespace_digest(namespace=other, structural_fact_id=in_alpha)
    )


def test_ambiguous_relation_name_is_not_linked() -> None:
    snap = _snapshot_id("candidate-a")
    rows = [
        {
            "kind": "symbol",
            "name": "pkg.run",
            "file": "pkg/run.py",
            "repo": "enola-fixture",
            "relations": [{"kind": "calls", "target": "pkg.normalize"}],
            "id": "0" * 32,
        },
        {
            "kind": "symbol",
            "name": "pkg.normalize",
            "file": "pkg/alpha.py",
            "repo": "enola-fixture",
            "id": "1" * 32,
        },
        {
            "kind": "symbol",
            "name": "pkg.normalize",
            "file": "pkg/beta.py",
            "repo": "enola-fixture",
            "id": "2" * 32,
        },
    ]
    projection = _project(rows, snapshot_id=snap)
    # The name exists in two files, so the relation is ambiguous: no edge is
    # invented rather than picking an arbitrary target.
    assert projection.edges == ()
    assert len(projection.nodes) == 3


def test_parse_enola_facts_rejects_corrupt_rows() -> None:
    with pytest.raises(EnolaStructuralError) as excinfo:
        parse_enola_facts(b'{"kind":"symbol"}\n')
    assert excinfo.value.code == "corrupt_facts"
    with pytest.raises(EnolaStructuralError):
        parse_enola_facts(b"not json\n")
    with pytest.raises(EnolaStructuralError):
        parse_enola_facts(b"")
