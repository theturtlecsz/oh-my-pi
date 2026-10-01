"""Tests for worktree reset and reuse (OMP-417-s07-s01)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omp_work.orchestrator.candidate_git import (
    CandidateGitError,
    git,
    reset_worktree,
)


def _init_repo(path: Path) -> str:
    """Initialize a git repo with user configs and an initial commit."""
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@omp.dev")
    (path / "file.txt").write_text("hello v1\n")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "commit 1")
    return git(path, "rev-parse", "HEAD").decode("ascii").strip()


def test_reset_worktree_missing_creates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    c1 = _init_repo(repo)

    worktrees_dir = tmp_path / "worktrees"
    wt = reset_worktree(repo, worktrees_dir, "wt-new", c1)

    assert wt == worktrees_dir / "wt-new"
    assert wt.is_dir()
    assert (wt / "file.txt").read_text() == "hello v1\n"
    assert git(wt, "rev-parse", "HEAD").decode("ascii").strip() == c1


def test_reset_worktree_dirty_registered_resets_and_cleans(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    c1 = _init_repo(repo)

    (repo / "file.txt").write_text("hello v2\n")
    (repo / "second.txt").write_text("second\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "commit 2")
    c2 = git(repo, "rev-parse", "HEAD").decode("ascii").strip()

    worktrees_dir = tmp_path / "worktrees"
    wt = reset_worktree(repo, worktrees_dir, "wt-reused", c1)
    assert git(wt, "rev-parse", "HEAD").decode("ascii").strip() == c1

    # Dirty the worktree: modify tracked, create untracked, ignored, and nested dir
    (wt / "file.txt").write_text("corrupted content\n")
    (wt / "untracked.bin").write_bytes(b"garbage")
    nested_dir = wt / "nested" / "deep"
    nested_dir.mkdir(parents=True)
    (nested_dir / "extra.txt").write_text("extra")

    # Add gitignore and ignored file
    (wt / ".gitignore").write_text("*.ignored\n")
    (wt / "temp.ignored").write_text("ignored file")

    reused = reset_worktree(repo, worktrees_dir, "wt-reused", c2)

    assert reused == wt
    assert git(wt, "rev-parse", "HEAD").decode("ascii").strip() == c2
    assert (wt / "file.txt").read_text() == "hello v2\n"
    assert (wt / "second.txt").read_text() == "second\n"
    assert not (wt / "untracked.bin").exists()
    assert not (wt / "nested").exists()
    assert not (wt / "temp.ignored").exists()
    assert not (wt / ".gitignore").exists()

    status = git(wt, "status", "--porcelain")
    assert status.strip() == b""


def test_reset_worktree_unregistered_dir_raises_conflict_and_kept(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    c1 = _init_repo(repo)

    worktrees_dir = tmp_path / "worktrees"
    unregistered = worktrees_dir / "wt-foreign"
    unregistered.mkdir(parents=True)
    marker = unregistered / "precious.txt"
    marker.write_text("do not touch")
    subdir = unregistered / "sub"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("keep intact")

    with pytest.raises(CandidateGitError) as exc_info:
        reset_worktree(repo, worktrees_dir, "wt-foreign", c1)

    assert exc_info.value.code == "worktree_conflict"
    assert unregistered.is_dir()
    assert marker.read_text() == "do not touch"
    assert (subdir / "nested.txt").read_text() == "keep intact"


def test_reset_worktree_bare_control_repo(tmp_path: Path) -> None:
    bare_repo = tmp_path / "bare.git"
    bare_repo.mkdir()
    git(bare_repo, "init", "--bare")
    git(bare_repo, "config", "user.name", "Test User")
    git(bare_repo, "config", "user.email", "test@omp.dev")

    # Create root commit in bare repo
    empty_tree = git(bare_repo, "write-tree").decode("ascii").strip()
    c1 = git(bare_repo, "commit-tree", empty_tree, "-m", "init").decode("ascii").strip()

    worktrees_dir = tmp_path / "worktrees"
    wt = reset_worktree(bare_repo, worktrees_dir, "wt-bare", c1)
    assert wt == worktrees_dir / "wt-bare"
    assert git(wt, "rev-parse", "HEAD").decode("ascii").strip() == c1

    # Dirty worktree
    (wt / "dirty.txt").write_text("dirty")
    reused = reset_worktree(bare_repo, worktrees_dir, "wt-bare", c1)
    assert reused == wt
    assert not (wt / "dirty.txt").exists()
    assert git(wt, "status", "--porcelain").strip() == b""
