from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work.knowledge_contracts import (
    CodeSnapshotManifest,
    EnolaFact,
    IngestionJob,
    IngestionReceipt,
    KnowledgeHit,
    KnowledgeQuery,
    ManifestFile,
    ProviderRoute,
    RepositoryIdentity,
    SourceRef,
)
from pydantic import ValidationError


def _get_fixtures_path() -> Path:
    # Look for fixtures/knowledge_vectors.json relative to repository root
    cand = Path(__file__).resolve().parents[3] / "fixtures" / "knowledge_vectors.json"
    if cand.is_file():
        return cand
    cand_local = Path("fixtures/knowledge_vectors.json").resolve()
    if cand_local.is_file():
        return cand_local
    raise FileNotFoundError("Could not find fixtures/knowledge_vectors.json")


def test_every_fixture_vector_recomputes() -> None:
    fixture_path = _get_fixtures_path()
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    # Test code snapshot vectors
    for item in data.get("code_snapshots", []):
        manifest = CodeSnapshotManifest(
            workspace_id=UUID(item["workspace_id"]),
            repository_id=UUID(item["repository_id"]),
            base_commit=item["base_commit"],
            files=item["files"],
        )
        assert manifest.snapshot_id() == item["snapshot_id"], (
            f"Snapshot mismatch in {item['name']}"
        )

        # Order must not change snapshot_id
        reversed_files = list(reversed(item["files"]))
        manifest_rev = CodeSnapshotManifest(
            workspace_id=UUID(item["workspace_id"]),
            repository_id=UUID(item["repository_id"]),
            base_commit=item["base_commit"],
            files=reversed_files,
        )
        assert manifest_rev.snapshot_id() == item["snapshot_id"], (
            f"Order changed snapshot in {item['name']}"
        )

    # Test child fact vectors
    for item in data.get("child_facts", []):
        fact = EnolaFact(
            fact_id=item["fact_id"],
            kind=item["kind"],
            name=item.get("name_fact", item.get("fact_name", item.get("name"))),
            file=item["file"],
            line=item["line"],
        )
        assert fact.child_fact_id(item["snap"]) == item["child_fact_id"], (
            f"Child fact mismatch in {item['name']}"
        )


def test_snapshot_id_changes_with_bytes_kind_path_base_commit() -> None:
    ws = uuid4()
    repo = uuid4()
    base_commit = "0" * 40
    f1 = ManifestFile(path="src/a.py", kind="base", sha256="1" * 64, size=100)
    f2 = ManifestFile(path="src/b.py", kind="modified", sha256="2" * 64, size=200)

    base_manifest = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f1, f2],
    )
    base_snap = base_manifest.snapshot_id()

    # 1. Order of files does NOT change snapshot_id
    reordered_manifest = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f2, f1],
    )
    assert reordered_manifest.snapshot_id() == base_snap

    # 2. bytes (sha256) changes snapshot_id
    f1_diff_sha = ManifestFile(path="src/a.py", kind="base", sha256="f" * 64, size=100)
    manifest_diff_sha = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f1_diff_sha, f2],
    )
    assert manifest_diff_sha.snapshot_id() != base_snap

    # 3. bytes (size) changes snapshot_id
    f1_diff_size = ManifestFile(path="src/a.py", kind="base", sha256="1" * 64, size=999)
    manifest_diff_size = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f1_diff_size, f2],
    )
    assert manifest_diff_size.snapshot_id() != base_snap

    # 4. kind changes snapshot_id
    f1_diff_kind = ManifestFile(
        path="src/a.py", kind="untracked", sha256="1" * 64, size=100
    )
    manifest_diff_kind = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f1_diff_kind, f2],
    )
    assert manifest_diff_kind.snapshot_id() != base_snap

    # 5. path changes snapshot_id
    f1_diff_path = ManifestFile(
        path="src/diff.py", kind="base", sha256="1" * 64, size=100
    )
    manifest_diff_path = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit=base_commit,
        files=[f1_diff_path, f2],
    )
    assert manifest_diff_path.snapshot_id() != base_snap

    # 6. base_commit changes snapshot_id
    manifest_diff_commit = CodeSnapshotManifest(
        workspace_id=ws,
        repository_id=repo,
        base_commit="a" * 40,
        files=[f1, f2],
    )
    assert manifest_diff_commit.snapshot_id() != base_snap


def test_code_snapshot_manifest_unique_files() -> None:
    ws = uuid4()
    repo = uuid4()
    f1 = ManifestFile(path="src/dup.py", kind="base", sha256="1" * 64, size=10)
    f2 = ManifestFile(path="src/dup.py", kind="modified", sha256="2" * 64, size=20)

    with pytest.raises(ValidationError, match="unique"):
        CodeSnapshotManifest(
            workspace_id=ws,
            repository_id=repo,
            base_commit="0" * 40,
            files=[f1, f2],
        )


def test_same_name_and_kind_in_two_files_distinct_child_fact_id() -> None:
    snap = "a" * 64
    fact1 = EnolaFact(
        fact_id="1" * 32,
        kind="class",
        name="Router",
        file="src/a/router.py",
        line=10,
    )
    fact2 = EnolaFact(
        fact_id="1" * 32,  # same fact_id
        kind="class",
        name="Router",
        file="src/b/router.py",  # different file
        line=10,
    )
    cid1 = fact1.child_fact_id(snap)
    cid2 = fact2.child_fact_id(snap)
    assert cid1 != cid2
    assert len(cid1) == 64
    assert len(cid2) == 64


def test_rejected_extra_fields() -> None:
    ws = uuid4()
    repo = uuid4()
    now = datetime.now(timezone.utc)

    # RepositoryIdentity
    with pytest.raises(ValidationError):
        RepositoryIdentity(
            workspace_id=ws,
            repository_id=repo,
            canonical_remote_url="https://example.com/repo.git",
            root_commits=("a" * 40,),
            verified_at=now,
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # SourceRef
    with pytest.raises(ValidationError):
        SourceRef(
            repository_id=repo,
            snapshot_id="a" * 64,
            path="src/index.ts",
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # ManifestFile
    with pytest.raises(ValidationError):
        ManifestFile(
            path="src/index.ts",
            kind="base",
            sha256="a" * 64,
            size=10,
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # CodeSnapshotManifest
    with pytest.raises(ValidationError):
        CodeSnapshotManifest(
            workspace_id=ws,
            repository_id=repo,
            base_commit="a" * 40,
            files=[],
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # EnolaFact
    with pytest.raises(ValidationError):
        EnolaFact(
            fact_id="a" * 32,
            kind="func",
            name="foo",
            file="a.py",
            line=1,
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # IngestionJob
    with pytest.raises(ValidationError):
        IngestionJob(
            job_id=uuid4(),
            workspace_id=ws,
            repository_id=repo,
            snapshot_id="a" * 64,
            idempotency_key="key-1",
            request_sha256="b" * 64,
            state="queued",
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # IngestionReceipt
    with pytest.raises(ValidationError):
        IngestionReceipt(
            receipt_id=uuid4(),
            job_id=uuid4(),
            snapshot_id="a" * 64,
            extraction_state="no_lesson",
            fact_count=0,
            graph_sha256="c" * 64,
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # KnowledgeQuery
    with pytest.raises(ValidationError):
        KnowledgeQuery(
            workspace_id=ws,
            repository_id=repo,
            snapshot_id="a" * 64,
            text="search",
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # KnowledgeHit
    with pytest.raises(ValidationError):
        KnowledgeHit(
            child_fact_id="a" * 64,
            source=SourceRef(repository_id=repo, snapshot_id="a" * 64, path="a.py"),
            kind="func",
            name="bar",
            corrected=False,
            extra_field="rejected",  # type: ignore[call-arg]
        )

    # ProviderRoute
    with pytest.raises(ValidationError):
        ProviderRoute(
            provider="test-provider",
            provider_version="1.0.0",
            operations=("ingest",),
            extra_field="rejected",  # type: ignore[call-arg]
        )


def test_rejected_bad_hex() -> None:
    repo = uuid4()
    ws = uuid4()

    # snapshot_id must be 64 lowercase hex
    for bad_snap in ["", "nothex", "A" * 64, "1" * 63, "1" * 65]:
        with pytest.raises(ValidationError):
            SourceRef(repository_id=repo, snapshot_id=bad_snap, path="a.py")
        with pytest.raises(ValidationError):
            IngestionJob(
                job_id=uuid4(),
                workspace_id=ws,
                repository_id=repo,
                snapshot_id=bad_snap,
                idempotency_key="k",
                request_sha256="1" * 64,
                state="queued",
            )
        with pytest.raises(ValidationError):
            KnowledgeQuery(
                workspace_id=ws,
                repository_id=repo,
                snapshot_id=bad_snap,
                text="q",
            )

    # fact_id must be 32 lowercase hex
    for bad_fid in ["", "nothex", "B" * 32, "1" * 31, "1" * 33]:
        with pytest.raises(ValidationError):
            EnolaFact(fact_id=bad_fid, kind="func", name="foo", file="a.py", line=1)
        with pytest.raises(ValidationError):
            SourceRef(
                repository_id=repo,
                snapshot_id="a" * 64,
                path="a.py",
                fact_id=bad_fid,
            )

    # child_fact_id method raises ValueError on bad snap hex
    fact = EnolaFact(fact_id="a" * 32, kind="func", name="foo", file="a.py", line=1)
    for bad_snap in ["", "short", "Z" * 64, "1" * 63]:
        with pytest.raises(ValueError, match="snap"):
            fact.child_fact_id(bad_snap)

    # root_commits must be hex commit shas
    with pytest.raises(ValidationError):
        RepositoryIdentity(
            workspace_id=ws,
            repository_id=repo,
            canonical_remote_url="https://example.com/repo.git",
            root_commits=("not-a-hex-sha",),
            verified_at=datetime.now(timezone.utc),
        )

    # base_commit must be hex
    with pytest.raises(ValidationError):
        CodeSnapshotManifest(
            workspace_id=ws,
            repository_id=repo,
            base_commit="invalid-commit",
            files=[],
        )


def test_rejected_deleted_with_hash() -> None:
    # Deleted with sha256 -> rejected
    with pytest.raises(ValidationError, match="deleted file must have sha256=None"):
        ManifestFile(path="old.py", kind="deleted", sha256="a" * 64, size=None)

    # Deleted with size -> rejected
    with pytest.raises(ValidationError, match="deleted file must have sha256=None"):
        ManifestFile(path="old.py", kind="deleted", sha256=None, size=100)

    # Deleted with both -> rejected
    with pytest.raises(ValidationError, match="deleted file must have sha256=None"):
        ManifestFile(path="old.py", kind="deleted", sha256="a" * 64, size=100)

    # Non-deleted without sha256 or size -> rejected
    with pytest.raises(ValidationError, match="must have both sha256 and size"):
        ManifestFile(path="live.py", kind="base", sha256=None, size=100)

    with pytest.raises(ValidationError, match="must have both sha256 and size"):
        ManifestFile(path="live.py", kind="modified", sha256="a" * 64, size=None)

    with pytest.raises(ValidationError, match="must have both sha256 and size"):
        ManifestFile(path="live.py", kind="untracked", sha256=None, size=None)

    # Deleted with None, None is valid
    valid_deleted = ManifestFile(path="old.py", kind="deleted", sha256=None, size=None)
    assert valid_deleted.kind == "deleted"
    assert valid_deleted.sha256 is None
    assert valid_deleted.size is None


def test_rejected_failed_without_error_code() -> None:
    ws = uuid4()
    repo = uuid4()

    # failed without error_code -> rejected
    with pytest.raises(
        ValidationError, match="error_code must be provided when state is 'failed'"
    ):
        IngestionJob(
            job_id=uuid4(),
            workspace_id=ws,
            repository_id=repo,
            snapshot_id="a" * 64,
            idempotency_key="key",
            request_sha256="b" * 64,
            state="failed",
            error_code=None,
        )

    with pytest.raises(
        ValidationError, match="error_code must be provided when state is 'failed'"
    ):
        IngestionJob(
            job_id=uuid4(),
            workspace_id=ws,
            repository_id=repo,
            snapshot_id="a" * 64,
            idempotency_key="key",
            request_sha256="b" * 64,
            state="failed",
            error_code="",
        )

    # non-failed with error_code -> rejected
    for state in ["queued", "running", "succeeded", "cancelled"]:
        with pytest.raises(ValidationError, match="error_code must be None"):
            IngestionJob(
                job_id=uuid4(),
                workspace_id=ws,
                repository_id=repo,
                snapshot_id="a" * 64,
                idempotency_key="key",
                request_sha256="b" * 64,
                state=state,  # type: ignore[arg-type]
                error_code="UNEXPECTED_ERROR",
            )

    # failed with error_code -> valid
    job = IngestionJob(
        job_id=uuid4(),
        workspace_id=ws,
        repository_id=repo,
        snapshot_id="a" * 64,
        idempotency_key="key",
        request_sha256="b" * 64,
        state="failed",
        error_code="TIMEOUT",
    )
    assert job.state == "failed"
    assert job.error_code == "TIMEOUT"


def test_rejected_no_lesson_with_facts() -> None:
    snap = "a" * 64
    jid = uuid4()

    # no_lesson with fact_count > 0 -> rejected
    with pytest.raises(
        ValidationError, match="fact_count 0 requires extraction_state='no_lesson'"
    ):
        IngestionReceipt(
            receipt_id=uuid4(),
            job_id=jid,
            snapshot_id=snap,
            extraction_state="extracted",
            fact_count=0,
            graph_sha256="c" * 64,
        )

    with pytest.raises(ValidationError, match="requires extraction_state='extracted'"):
        IngestionReceipt(
            receipt_id=uuid4(),
            job_id=jid,
            snapshot_id=snap,
            extraction_state="no_lesson",
            fact_count=1,
            graph_sha256="c" * 64,
        )

    # valid no_lesson
    r1 = IngestionReceipt(
        receipt_id=uuid4(),
        job_id=jid,
        snapshot_id=snap,
        extraction_state="no_lesson",
        fact_count=0,
        graph_sha256="c" * 64,
    )
    assert r1.extraction_state == "no_lesson"
    assert r1.fact_count == 0

    # valid extracted
    r2 = IngestionReceipt(
        receipt_id=uuid4(),
        job_id=jid,
        snapshot_id=snap,
        extraction_state="extracted",
        fact_count=42,
        graph_sha256="c" * 64,
    )
    assert r2.extraction_state == "extracted"
    assert r2.fact_count == 42


def test_manifest_file_path_validation() -> None:
    bad_paths = [
        "/absolute/path.py",
        "../traversal.py",
        "foo/../../bar.py",
        "./relative.py",
        "foo//bar.py",
        "",
        "   ",
        "control\x00char.py",
    ]
    for p in bad_paths:
        with pytest.raises(ValidationError):
            ManifestFile(path=p, kind="base", sha256="a" * 64, size=10)


def test_knowledge_query_limit_bounds() -> None:
    ws = uuid4()
    repo = uuid4()
    snap = "a" * 64

    # limit 1..50
    with pytest.raises(ValidationError):
        KnowledgeQuery(
            workspace_id=ws,
            repository_id=repo,
            snapshot_id=snap,
            text="hi",
            limit=0,
        )
    with pytest.raises(ValidationError):
        KnowledgeQuery(
            workspace_id=ws,
            repository_id=repo,
            snapshot_id=snap,
            text="hi",
            limit=51,
        )

    q1 = KnowledgeQuery(
        workspace_id=ws,
        repository_id=repo,
        snapshot_id=snap,
        text="hi",
        limit=1,
    )
    assert q1.limit == 1
    q50 = KnowledgeQuery(
        workspace_id=ws,
        repository_id=repo,
        snapshot_id=snap,
        text="hi",
        limit=50,
    )
    assert q50.limit == 50


def test_provider_route_operations_subset() -> None:
    # Valid subset
    route = ProviderRoute(
        provider="model-provider",
        provider_version="2.1.0",
        operations=["lookup", "query"],
    )
    assert route.operations == ("lookup", "query")

    # Empty operations is a subset of allowed operations
    route_empty = ProviderRoute(
        provider="model-provider",
        provider_version="2.1.0",
        operations=[],
    )
    assert route_empty.operations == ()

    # Invalid operation outside allowed operations
    with pytest.raises(ValidationError, match="unsupported operation"):
        ProviderRoute(
            provider="model-provider",
            provider_version="2.1.0",
            operations=["ingest", "drop_table"],  # type: ignore[list-item]
        )


def test_shorthand_aliases_compatibility() -> None:
    ws = uuid4()
    repo = uuid4()
    snap = "f" * 64

    # Constructed with shorthand vs full names
    m1 = CodeSnapshotManifest(ws=ws, repo=repo, base_commit="0" * 40, files=[])
    assert m1.ws == ws
    assert m1.workspace_id == ws
    assert m1.repo == repo
    assert m1.repository_id == repo

    s1 = SourceRef(repo=repo, snap=snap, path="src/main.py")
    assert s1.repo == repo
    assert s1.repository_id == repo
    assert s1.snap == snap
    assert s1.snapshot_id == snap

    q1 = KnowledgeQuery(ws=ws, repo=repo, snap=snap, text="test")
    assert q1.ws == ws
    assert q1.repo == repo
    assert q1.snap == snap
