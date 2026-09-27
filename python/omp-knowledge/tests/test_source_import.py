"""Resumable source import of one retained Enola snapshot (OMP-312-s03)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from omp_knowledge.source_import import (
    FACTS_NAME,
    MANIFEST_NAME,
    RECEIPT_NAME,
    ImportResult,
    SourceImportError,
    import_source,
    main,
)
from omp_work.knowledge_publication import (
    SnapshotInvisibleError,
    StructuralPublicationStore,
)
from omp_work.knowledge_structural import EnolaStructuralError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAP_A = "a" * 64
SNAP_B = "b" * 64


def _git_repo(path: Path, origin: str, filename: str, content: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    return path


def _copy_fixture(name: str, dest: Path) -> Path:
    dest.mkdir(parents=True)
    for filename in (FACTS_NAME, RECEIPT_NAME):
        shutil.copyfile(FIXTURES / name / filename, dest / filename)
    return dest


def _names(store: StructuralPublicationStore, workspace, repository, snapshot: str) -> set[str]:
    result = store.query(
        workspace_id=workspace,
        repository_id=repository,
        snapshot_id=snapshot,
        text="*",
        limit=100,
    )
    return {hit.name for hit in result.hits}


def _row_count(state: Path) -> int:
    import sqlite3

    db = state / "structural-publications.sqlite"
    if not db.exists():
        return 0
    connection = sqlite3.connect(db)
    try:
        return int(
            connection.execute("SELECT count(*) FROM structural_snapshots").fetchone()[0]
        )
    finally:
        connection.close()


def test_fixtures_publish_isolated_by_repository(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repo_a = uuid4()
    repo_b = uuid4()
    checkout_a = _git_repo(
        tmp_path / "repo-a",
        "https://example.invalid/omp/repo-a.git",
        "a.txt",
        "alpha\n",
    )
    checkout_b = _git_repo(
        tmp_path / "repo-b",
        "git@example.invalid:omp/repo-b.git",
        "b.txt",
        "beta\n",
    )

    imported_a = import_source(
        state,
        workspace_id=workspace,
        repository_id=repo_a,
        snapshot_id=SNAP_A,
        checkout=checkout_a,
        enola_dir=FIXTURES / "A",
    )
    imported_b = import_source(
        state,
        workspace_id=workspace,
        repository_id=repo_b,
        snapshot_id=SNAP_B,
        checkout=checkout_b,
        enola_dir=FIXTURES / "B",
    )

    assert imported_a.publication.state == "published"
    assert imported_b.publication.state == "published"
    assert imported_a.publication.repository_id == repo_a
    assert imported_b.publication.repository_id == repo_b
    assert len(imported_a.facts_sha256) == 64
    assert len(imported_a.receipt_sha256) == 64
    assert len(imported_a.manifest_sha256) == 64

    retained_a = state / "sources" / str(repo_a) / SNAP_A
    assert (retained_a / FACTS_NAME).read_bytes() == (FIXTURES / "A" / FACTS_NAME).read_bytes()
    assert (retained_a / RECEIPT_NAME).read_bytes() == (FIXTURES / "A" / RECEIPT_NAME).read_bytes()
    manifest = json.loads((retained_a / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["repository_id"] == str(repo_a)
    assert manifest["workspace_id"] == str(workspace)
    assert imported_a.manifest_sha256 == hashlib.sha256(
        (retained_a / MANIFEST_NAME).read_bytes()
    ).hexdigest()

    inodes = {
        name: (retained_a / name).stat().st_ino
        for name in (FACTS_NAME, RECEIPT_NAME, MANIFEST_NAME)
    }
    again = import_source(
        state,
        workspace_id=workspace,
        repository_id=repo_a,
        snapshot_id=SNAP_A,
        checkout=checkout_a,
        enola_dir=FIXTURES / "A",
    )
    assert again == imported_a
    assert {
        name: (retained_a / name).stat().st_ino
        for name in (FACTS_NAME, RECEIPT_NAME, MANIFEST_NAME)
    } == inodes

    store = StructuralPublicationStore(state)
    names_a = _names(store, workspace, repo_a, SNAP_A)
    names_b = _names(store, workspace, repo_b, SNAP_B)
    assert "ts/src.gammaOnlyA" in names_a
    assert "py/pkg/beta.beta_only_b" not in names_a
    assert "py/pkg/beta.beta_only_b" in names_b
    assert "py/pkg/gamma.gamma_only_b" in names_b
    assert "ts/src.gammaOnlyA" not in names_b
    with pytest.raises(SnapshotInvisibleError):
        store.query(
            workspace_id=workspace,
            repository_id=repo_a,
            snapshot_id=SNAP_B,
            text="*",
        )
    with pytest.raises(SnapshotInvisibleError):
        store.query(
            workspace_id=workspace,
            repository_id=repo_b,
            snapshot_id=SNAP_A,
            text="*",
        )
    assert _row_count(state) == 2
    assert isinstance(again, ImportResult)


def test_parallel_imports_into_one_state_root_both_publish(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repo_a = uuid4()
    repo_b = uuid4()
    checkout_a = _git_repo(
        tmp_path / "repo-a", "https://example.invalid/omp/parallel-a.git", "a.txt", "a\n"
    )
    checkout_b = _git_repo(
        tmp_path / "repo-b", "https://example.invalid/omp/parallel-b.git", "b.txt", "b\n"
    )

    def _import(repo, snapshot: str, checkout: Path, fixture: str) -> ImportResult:
        return import_source(
            state,
            workspace_id=workspace,
            repository_id=repo,
            snapshot_id=snapshot,
            checkout=checkout,
            enola_dir=FIXTURES / fixture,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_import, repo_a, SNAP_A, checkout_a, "A")
        future_b = pool.submit(_import, repo_b, SNAP_B, checkout_b, "B")
        imported_a = future_a.result()
        imported_b = future_b.result()

    assert imported_a.publication.state == "published"
    assert imported_b.publication.state == "published"
    store = StructuralPublicationStore(state)
    assert store.is_published(
        workspace_id=workspace, repository_id=repo_a, snapshot_id=SNAP_A
    )
    assert store.is_published(
        workspace_id=workspace, repository_id=repo_b, snapshot_id=SNAP_B
    )
    assert "ts/src.gammaOnlyA" in _names(store, workspace, repo_a, SNAP_A)
    assert "py/pkg/gamma.gamma_only_b" in _names(store, workspace, repo_b, SNAP_B)
    assert _row_count(state) == 2


def test_parallel_same_snapshot_publishes_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/same.git", "s.txt", "s\n"
    )

    def _import() -> ImportResult:
        return import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=FIXTURES / "A",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (future.result() for future in (pool.submit(_import), pool.submit(_import)))

    assert first.publication.state == "published"
    assert second.publication.state == "published"
    assert first.facts_sha256 == second.facts_sha256
    assert first.publication.graph_sha256 == second.publication.graph_sha256
    assert _row_count(state) == 1
    store = StructuralPublicationStore(state)
    assert "ts/src.gammaOnlyA" in _names(store, workspace, repository, SNAP_A)


def test_crash_between_stage_and_publish_stays_invisible_until_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/crash.git", "c.txt", "c\n"
    )
    calls = {"n": 0}
    real = StructuralPublicationStore.publish

    def boom(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash between stage and publish")
        return real(self, **kwargs)

    monkeypatch.setattr(StructuralPublicationStore, "publish", boom)
    with pytest.raises(RuntimeError, match="crash between stage and publish"):
        import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=FIXTURES / "A",
        )

    store = StructuralPublicationStore(state)
    assert store.is_published(
        workspace_id=workspace, repository_id=repository, snapshot_id=SNAP_A
    ) is False
    with pytest.raises(SnapshotInvisibleError):
        store.query(
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            text="*",
        )
    assert _row_count(state) == 1

    published = import_source(
        state,
        workspace_id=workspace,
        repository_id=repository,
        snapshot_id=SNAP_A,
        checkout=checkout,
        enola_dir=FIXTURES / "A",
    )
    assert published.publication.state == "published"
    assert calls["n"] == 2
    assert _row_count(state) == 1
    assert "ts/src.gammaOnlyA" in _names(store, workspace, repository, SNAP_A)


def test_crash_during_retain_rerun_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/retain-crash.git", "c.txt", "c\n"
    )
    real_replace = os.replace
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("crash during retain")
        return real_replace(src, dst)

    # The patch raises on the second replace only, so the re-run below can finish
    # the files the crash skipped.
    monkeypatch.setattr("omp_knowledge.source_import.os.replace", boom)
    with pytest.raises(OSError, match="crash during retain"):
        import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=FIXTURES / "A",
        )
    assert _row_count(state) == 0

    published = import_source(
        state,
        workspace_id=workspace,
        repository_id=repository,
        snapshot_id=SNAP_A,
        checkout=checkout,
        enola_dir=FIXTURES / "A",
    )
    assert published.publication.state == "published"
    assert _row_count(state) == 1
    store = StructuralPublicationStore(state)
    assert "ts/src.alphaOnlyA" in _names(store, workspace, repository, SNAP_A)


def test_missing_or_corrupt_receipt_publishes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/refuse.git", "r.txt", "r\n"
    )
    missing = _copy_fixture("A", tmp_path / "missing")
    (missing / RECEIPT_NAME).unlink()

    with pytest.raises(EnolaStructuralError) as missing_exc:
        import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=missing,
        )
    assert missing_exc.value.code == "missing_receipt"
    assert not (state / "structural-publications.sqlite").exists()
    assert not (state / "sources").exists()

    corrupt = _copy_fixture("A", tmp_path / "corrupt")
    (corrupt / RECEIPT_NAME).write_bytes(b"{")
    with pytest.raises(EnolaStructuralError) as corrupt_exc:
        import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=corrupt,
        )
    assert corrupt_exc.value.code == "corrupt_receipt"
    assert not (state / "structural-publications.sqlite").exists()
    assert _row_count(state) == 0


def test_differing_retained_bytes_refuse_and_publish_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/conflict.git", "k.txt", "k\n"
    )
    enola = _copy_fixture("A", tmp_path / "enola")
    imported = import_source(
        state,
        workspace_id=workspace,
        repository_id=repository,
        snapshot_id=SNAP_A,
        checkout=checkout,
        enola_dir=enola,
    )
    graph = imported.publication.graph_sha256
    facts = (enola / FACTS_NAME).read_bytes()
    (enola / FACTS_NAME).write_bytes(facts + b"\n")

    with pytest.raises(SourceImportError) as exc:
        import_source(
            state,
            workspace_id=workspace,
            repository_id=repository,
            snapshot_id=SNAP_A,
            checkout=checkout,
            enola_dir=enola,
        )
    assert exc.value.code == "snapshot_conflict"
    assert _row_count(state) == 1
    store = StructuralPublicationStore(state)
    active = store.active_snapshots(workspace_id=workspace, repository_id=repository)
    assert len(active) == 1
    assert active[0].state == "published"
    assert active[0].graph_sha256 == graph
    assert "ts/src.gammaOnlyA" in _names(store, workspace, repository, SNAP_A)
    retained_facts = state / "sources" / str(repository) / SNAP_A / FACTS_NAME
    assert retained_facts.read_bytes() == facts


def test_cli_success_and_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/cli.git", "cli.txt", "cli\n"
    )
    argv = [
        "--state-root",
        str(state),
        "--workspace",
        str(workspace),
        "--repository-id",
        str(repository),
        "--snapshot-id",
        SNAP_A,
        "--checkout",
        str(checkout),
        "--enola-dir",
        str(FIXTURES / "A"),
        "--json",
    ]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["publication"]["state"] == "published"
    assert payload["publication"]["snapshot_id"] == SNAP_A
    assert len(payload["facts_sha256"]) == 64

    missing = _copy_fixture("B", tmp_path / "missing")
    (missing / RECEIPT_NAME).unlink()
    refused = main(
        [
            "--state-root",
            str(tmp_path / "refused"),
            "--workspace",
            str(workspace),
            "--repository-id",
            str(repository),
            "--snapshot-id",
            SNAP_B,
            "--checkout",
            str(checkout),
            "--enola-dir",
            str(missing),
        ]
    )
    assert refused == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "receipt" in captured.err.lower()
    assert not (tmp_path / "refused" / "structural-publications.sqlite").exists()


def test_module_entrypoint_exits_zero(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = uuid4()
    repository = uuid4()
    checkout = _git_repo(
        tmp_path / "repo", "https://example.invalid/omp/mod.git", "m.txt", "m\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_knowledge.source_import",
            "--state-root",
            str(state),
            "--workspace",
            str(workspace),
            "--repository-id",
            str(repository),
            "--snapshot-id",
            SNAP_B,
            "--checkout",
            str(checkout),
            "--enola-dir",
            str(FIXTURES / "B"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"published {SNAP_B} ")
    store = StructuralPublicationStore(state)
    assert "py/pkg/beta.beta_only_b" in _names(store, workspace, repository, SNAP_B)
