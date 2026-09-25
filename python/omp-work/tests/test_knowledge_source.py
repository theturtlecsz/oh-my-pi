from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.knowledge_contracts import CodeSnapshotManifest, RepositoryIdentity
from omp_work.knowledge_source import (
    KnowledgeSourceError,
    capture_manifest,
    normalize_remote_url,
    resolve_repository_identity,
)
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

OWNER = uuid4()


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


@pytest.fixture(scope="module")
def pg_config(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("knowledge-service")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()
        yield config


@contextmanager
def _transaction(
    config: OperationsConfig, workspace_id: UUID, actor_id: UUID = OWNER
):
    with psycopg.connect(
        **config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL search_path = pg_catalog")
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                    (str(workspace_id), str(actor_id)),
                )
                yield cur


def _ensure_workspace(
    config: OperationsConfig, workspace_id: UUID, actor_id: UUID = OWNER
) -> None:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    seed_authority(
        config.connection_kwargs("postgres"), workspace_id, actor_id
    )


def _insert_repository(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    key: str,
    name: str,
    url: str,
    archived: bool = False,
    provenance: dict | None = None,
    created_at: datetime | None = None,
) -> None:
    with psycopg.connect(
        **config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            cur.execute(
                """
                INSERT INTO omp_work.repositories(
                    repository_id, workspace_id, key, name, url, archived, provenance, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    repository_id,
                    workspace_id,
                    key,
                    name,
                    url,
                    archived,
                    json.dumps(provenance or {"source": "test"}),
                    created_at or datetime.now(timezone.utc),
                ),
            )


def _create_git_repo(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
    )
    (path / "file1.txt").write_text("file 1 content\n")
    (path / "file2.txt").write_text("file 2 content\n")
    (path / ".gitignore").write_text("*.log\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True)
    return path


def test_normalize_remote_url():
    cases = [
        ("git@github.com:foo/bar.git", "ssh://git@github.com/foo/bar"),
        ("git@github.com:foo/bar", "ssh://git@github.com/foo/bar"),
        ("ssh://git@github.com:22/foo/bar.git", "ssh://git@github.com/foo/bar"),
        ("ssh://git@github.com:22/foo/bar.git/", "ssh://git@github.com/foo/bar"),
        ("https://github.com:443/foo/bar.git", "https://github.com/foo/bar"),
        ("https://github.com/foo/bar.git/", "https://github.com/foo/bar"),
        ("https://github.com/foo/bar/", "https://github.com/foo/bar"),
        ("https://github.com/foo/bar", "https://github.com/foo/bar"),
        ("HTTPS://GITHUB.COM:443/Foo/Bar.git", "https://github.com/Foo/Bar"),
        ("  git@GITHUB.COM:Foo/Bar.git/  ", "ssh://git@github.com/Foo/Bar"),
        ("https://github.com:8080/foo/bar.git", "https://github.com:8080/foo/bar"),
        ("ssh://git@github.com:2222/foo/bar.git", "ssh://git@github.com:2222/foo/bar"),
    ]
    for raw, expected in cases:
        assert normalize_remote_url(raw) == expected


def test_checkout_invalid_conditions(tmp_path: Path, pg_config: OperationsConfig):
    ws_id = uuid4()
    _ensure_workspace(pg_config, ws_id)

    # 1. Non-git directory
    non_git = tmp_path / "non_git"
    non_git.mkdir()
    with _transaction(pg_config, ws_id) as cur:
        with pytest.raises(KnowledgeSourceError) as exc_info:
            resolve_repository_identity(cur, ws_id, non_git)
        assert exc_info.value.code == "checkout_invalid"

    # 2. Subdirectory of checkout
    repo_dir = _create_git_repo(tmp_path / "valid_repo", "https://example.com/repo.git")
    sub_dir = repo_dir / "subdir"
    sub_dir.mkdir()
    with _transaction(pg_config, ws_id) as cur:
        with pytest.raises(KnowledgeSourceError) as exc_info:
            resolve_repository_identity(cur, ws_id, sub_dir)
        assert exc_info.value.code == "checkout_invalid"

    # 3. No origin remote
    no_remote = tmp_path / "no_remote"
    no_remote.mkdir()
    subprocess.run(["git", "init"], cwd=no_remote, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=no_remote, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=no_remote, check=True)
    (no_remote / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=no_remote, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=no_remote, check=True)
    with _transaction(pg_config, ws_id) as cur:
        with pytest.raises(KnowledgeSourceError) as exc_info:
            resolve_repository_identity(cur, ws_id, no_remote)
        assert exc_info.value.code == "checkout_invalid"


def test_resolve_identity_worktree_sibling_and_url_forms(
    tmp_path: Path, pg_config: OperationsConfig
):
    ws_id = uuid4()
    _ensure_workspace(pg_config, ws_id)

    # Repository with HTTPS URL in DB, origin has .git
    repo_id = uuid4()
    _insert_repository(
        pg_config,
        workspace_id=ws_id,
        repository_id=repo_id,
        key="REPO-1",
        name="Test Repo",
        url="https://example.com/test-repo",
    )

    checkout_root = _create_git_repo(
        tmp_path / "main_checkout", "https://example.com/test-repo.git"
    )

    # Sibling worktree
    sibling_root = tmp_path / "sibling_worktree"
    subprocess.run(
        ["git", "worktree", "add", str(sibling_root), "HEAD"],
        cwd=checkout_root,
        check=True,
        capture_output=True,
    )

    with _transaction(pg_config, ws_id) as cur:
        id_main = resolve_repository_identity(cur, ws_id, checkout_root)
        id_sibling = resolve_repository_identity(cur, ws_id, sibling_root)

    assert id_main.repository_id == repo_id
    assert id_sibling.repository_id == repo_id
    assert id_main.canonical_remote_url == "https://example.com/test-repo"
    assert id_sibling.canonical_remote_url == "https://example.com/test-repo"
    assert id_main.root_commits == id_sibling.root_commits

    # SCP URL form matching:
    # DB has ssh://git@example.com/scp-repo, checkout has git@example.com:scp-repo.git
    repo_id_scp = uuid4()
    _insert_repository(
        pg_config,
        workspace_id=ws_id,
        repository_id=repo_id_scp,
        key="REPO-SCP",
        name="SCP Repo",
        url="ssh://git@example.com/scp-repo",
    )
    checkout_scp = _create_git_repo(
        tmp_path / "scp_checkout", "git@example.com:scp-repo.git"
    )
    with _transaction(pg_config, ws_id) as cur:
        id_scp = resolve_repository_identity(cur, ws_id, checkout_scp)
    assert id_scp.repository_id == repo_id_scp
    assert id_scp.canonical_remote_url == "ssh://git@example.com/scp-repo"


def test_archived_ambiguous_unresolved_and_rls(
    tmp_path: Path, pg_config: OperationsConfig
):
    ws_a = uuid4()
    ws_b = uuid4()
    _ensure_workspace(pg_config, ws_a)
    _ensure_workspace(pg_config, ws_b)

    repo_url = "https://example.com/target-repo"
    checkout = _create_git_repo(tmp_path / "target_checkout", repo_url)

    # 1. No rows in ws_a -> repository_unresolved, row count unchanged
    with _transaction(pg_config, ws_a) as cur:
        cur.execute("SELECT count(*) FROM omp_work.repositories;")
        count_before = cur.fetchone()["count"]

        with pytest.raises(KnowledgeSourceError) as exc:
            resolve_repository_identity(cur, ws_a, checkout)
        assert exc.value.code == "repository_unresolved"

        cur.execute("SELECT count(*) FROM omp_work.repositories;")
        count_after = cur.fetchone()["count"]
        assert count_after == count_before

    # 2. Only archived row in ws_a -> repository_unresolved
    repo_archived = uuid4()
    _insert_repository(
        pg_config,
        workspace_id=ws_a,
        repository_id=repo_archived,
        key="REPO-ARCH",
        name="Archived Repo",
        url=repo_url,
        archived=True,
    )
    with _transaction(pg_config, ws_a) as cur:
        with pytest.raises(KnowledgeSourceError) as exc:
            resolve_repository_identity(cur, ws_a, checkout)
        assert exc.value.code == "repository_unresolved"

    # 3. Two matching rows in ws_a -> repository_ambiguous
    repo_active_1 = uuid4()
    repo_active_2 = uuid4()
    _insert_repository(
        pg_config,
        workspace_id=ws_a,
        repository_id=repo_active_1,
        key="REPO-ACT-1",
        name="Active 1",
        url=repo_url,
    )
    _insert_repository(
        pg_config,
        workspace_id=ws_a,
        repository_id=repo_active_2,
        key="REPO-ACT-2",
        name="Active 2",
        url=f"{repo_url}.git",
    )
    with _transaction(pg_config, ws_a) as cur:
        with pytest.raises(KnowledgeSourceError) as exc:
            resolve_repository_identity(cur, ws_a, checkout)
        assert exc.value.code == "repository_ambiguous"

    # 4. RLS isolation: ws_b queries checkout; ws_a's rows must be invisible to ws_b
    with _transaction(pg_config, ws_b) as cur:
        with pytest.raises(KnowledgeSourceError) as exc:
            resolve_repository_identity(cur, ws_b, checkout)
        assert exc.value.code == "repository_unresolved"


def test_capture_manifest_snapshot_id_invariants(tmp_path: Path):
    ws_id = uuid4()
    repo_id = uuid4()

    main_root = _create_git_repo(
        tmp_path / "manifest_main", "https://example.com/manifest-test.git"
    )
    sibling_root = tmp_path / "manifest_sibling"
    subprocess.run(
        ["git", "worktree", "add", str(sibling_root), "HEAD"],
        cwd=main_root,
        check=True,
        capture_output=True,
    )

    identity = RepositoryIdentity(
        workspace_id=ws_id,
        repository_id=repo_id,
        canonical_remote_url="https://example.com/manifest-test",
        root_commits=("a" * 40,),
        verified_at=datetime.now(timezone.utc),
    )

    # 1. Clean worktree at same HEAD -> same snapshot_id
    m_main = capture_manifest(identity, main_root)
    m_sibling = capture_manifest(identity, sibling_root)
    clean_snap = m_main.snapshot_id()
    assert clean_snap == m_sibling.snapshot_id()
    assert all(f.kind == "base" for f in m_main.files)

    # 2. Ignored file does not change snapshot_id
    (sibling_root / "temp.log").write_text("debug log output")
    m_ignored = capture_manifest(identity, sibling_root)
    assert m_ignored.snapshot_id() == clean_snap
    assert not any(f.path == "temp.log" for f in m_ignored.files)
    (sibling_root / "temp.log").unlink()

    # 3. Tracked edit changes snapshot_id
    (sibling_root / "file1.txt").write_text("modified file 1 content\n")
    m_modified = capture_manifest(identity, sibling_root)
    assert m_modified.snapshot_id() != clean_snap
    mod_entry = next(f for f in m_modified.files if f.path == "file1.txt")
    assert mod_entry.kind == "modified"
    # Revert edit
    subprocess.run(["git", "checkout", "--", "."], cwd=sibling_root, check=True)
    assert capture_manifest(identity, sibling_root).snapshot_id() == clean_snap

    # 4. New untracked file changes snapshot_id
    (sibling_root / "untracked.txt").write_text("brand new file")
    m_untracked = capture_manifest(identity, sibling_root)
    assert m_untracked.snapshot_id() != clean_snap
    untracked_entry = next(f for f in m_untracked.files if f.path == "untracked.txt")
    assert untracked_entry.kind == "untracked"
    # Remove untracked file
    (sibling_root / "untracked.txt").unlink()
    assert capture_manifest(identity, sibling_root).snapshot_id() == clean_snap

    # 5. Deletion changes snapshot_id
    (sibling_root / "file2.txt").unlink()
    m_deleted = capture_manifest(identity, sibling_root)
    assert m_deleted.snapshot_id() != clean_snap
    del_entry = next(f for f in m_deleted.files if f.path == "file2.txt")
    assert del_entry.kind == "deleted"
    assert del_entry.sha256 is None
    assert del_entry.size is None


def test_resolve_identity_multiple_root_commits(
    tmp_path: Path, pg_config: OperationsConfig
):
    ws_id = uuid4()
    _ensure_workspace(pg_config, ws_id)
    repo_id = uuid4()
    _insert_repository(
        pg_config,
        workspace_id=ws_id,
        repository_id=repo_id,
        key="REPO-MULTI-ROOT",
        name="Multi Root Repo",
        url="https://example.com/multi-root",
    )
    repo_dir = tmp_path / "multi_root_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/multi-root.git"],
        cwd=repo_dir,
        check=True,
    )
    (repo_dir / "r1.txt").write_text("root 1\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "commit 1"], cwd=repo_dir, check=True)
    # create unrelated branch
    subprocess.run(
        ["git", "checkout", "--orphan", "branch2"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "rm", "-rf", "."],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    (repo_dir / "r2.txt").write_text("root 2\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=repo_dir, check=True)
    # merge with unrelated histories
    subprocess.run(
        ["git", "merge", "--no-ff", "--allow-unrelated-histories", "master", "-m", "merge roots"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    with _transaction(pg_config, ws_id) as cur:
        ident = resolve_repository_identity(cur, ws_id, repo_dir)
    assert len(ident.root_commits) == 2
    assert ident.repository_id == repo_id

