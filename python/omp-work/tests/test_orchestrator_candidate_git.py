"""Tests for hardened candidate git operations (OMP-417-s04-s01)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from omp_work.orchestrator.candidate_git import (
    CandidateGitError,
    IntentLog,
    add_worktree,
    git,
    path_allowed,
)


class InMemoryIntentLog:
    """In-memory IntentLog implementation for testing."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def record_intent(self, key: str, action: str, target: Any) -> None:
        self.records[key] = {"action": action, "target": target, "done": False}

    def mark_done(self, key: str, ref: str) -> None:
        if key in self.records:
            self.records[key]["done"] = True
            self.records[key]["ref"] = ref

    def open(self, key: str) -> dict[str, Any] | None:
        record = self.records.get(key)
        if record is not None and not record.get("done", False):
            return record
        return None


def test_candidate_git_error_structure() -> None:
    err_default = CandidateGitError("git_failed")
    assert str(err_default) == "git_failed"
    assert err_default.code == "git_failed"
    assert err_default.violations == []

    err_violations = CandidateGitError("envelope_violation", ["outside allowed globs"])
    assert err_violations.code == "envelope_violation"
    assert err_violations.violations == ["outside allowed globs"]


def test_intent_log_protocol() -> None:
    log = InMemoryIntentLog()
    assert isinstance(log, IntentLog)
    log.record_intent("k1", "push", {"branch": "main"})
    assert log.open("k1") == {"action": "push", "target": {"branch": "main"}, "done": False}
    log.mark_done("k1", "refs/heads/main")
    assert log.open("k1") is None


def test_hooks_and_fsmonitor_write_no_marker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@omp.dev"], check=True)

    marker_hooks = tmp_path / "marker_hooks"
    evil_hooks_dir = tmp_path / "evil_hooks"
    evil_hooks_dir.mkdir(parents=True)
    for hook_name in ("pre-commit", "post-commit", "commit-msg"):
        hook_file = evil_hooks_dir / hook_name
        hook_file.write_text(f"#!/bin/sh\ntouch '{marker_hooks}'\nexit 0\n")
        hook_file.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(evil_hooks_dir)],
        check=True,
    )

    marker_fsmonitor = tmp_path / "marker_fsmonitor"
    evil_fsmonitor = tmp_path / "evil_fsmonitor.sh"
    evil_fsmonitor.write_text(f"#!/bin/sh\ntouch '{marker_fsmonitor}'\nexit 0\n")
    evil_fsmonitor.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(evil_fsmonitor)],
        check=True,
    )

    status_out = git(repo, "status")
    assert isinstance(status_out, bytes)

    commit_out = git(repo, "commit", "--allow-empty", "-m", "allow empty commit")
    assert isinstance(commit_out, bytes)

    assert not marker_hooks.exists(), "core.hooksPath hook should not have run"
    assert not marker_fsmonitor.exists(), "core.fsmonitor hook should not have run"


def test_parent_env_git_dir_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_a = tmp_path / "repo_a"
    subprocess.run(["git", "init", str(repo_a)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_a), "config", "user.name", "A"], check=True)
    subprocess.run(["git", "-C", str(repo_a), "config", "user.email", "a@omp.dev"], check=True)
    (repo_a / "file_a.txt").write_text("from repo a\n")
    subprocess.run(["git", "-C", str(repo_a), "add", "file_a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "repo a init"], check=True, capture_output=True)

    repo_b = tmp_path / "repo_b"
    subprocess.run(["git", "init", str(repo_b)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_b), "config", "user.name", "B"], check=True)
    subprocess.run(["git", "-C", str(repo_b), "config", "user.email", "b@omp.dev"], check=True)
    (repo_b / "file_b.txt").write_text("from repo b\n")
    subprocess.run(["git", "-C", str(repo_b), "add", "file_b.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_b), "commit", "-m", "repo b init"], check=True, capture_output=True)

    # Point parent env GIT_DIR to repo_b's .git
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))

    out = git(repo_a, "ls-files")
    assert b"file_a.txt" in out
    assert b"file_b.txt" not in out


def test_git_replace_does_not_change_ls_tree_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@omp.dev"], check=True)

    file_path = repo / "file.txt"
    file_path.write_text("v1 content\n")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c1"], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    base_tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"]).decode().strip()
    blob_v1 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", f"{base}:file.txt"]).decode().strip()

    file_path.write_text("v2 replaced content\n")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c2"], check=True, capture_output=True)
    c2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    second_tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"]).decode().strip()
    blob_v2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", f"{c2}:file.txt"]).decode().strip()

    # Replace base_tree with second_tree in repository replace refs
    subprocess.run(["git", "-C", str(repo), "replace", base_tree, second_tree], check=True, capture_output=True)

    # Standard git ls-tree would see replaced tree with blob_v2
    raw_ls = subprocess.check_output(["git", "-C", str(repo), "ls-tree", base]).decode()
    assert blob_v2 in raw_ls

    # Hardened git() has GIT_NO_REPLACE_OBJECTS=1 so it sees original base tree with blob_v1
    hardened_ls = git(repo, "ls-tree", base).decode()
    assert blob_v1 in hardened_ls
    assert blob_v2 not in hardened_ls


def test_path_allowed_rows() -> None:
    # "README does not allow README/x"
    assert not path_allowed("README/x", ["README"])
    assert path_allowed("README", ["README"])

    # "src/* allows src/a.py but not src/a/b.py"
    assert path_allowed("src/a.py", ["src/*"])
    assert not path_allowed("src/a/b.py", ["src/*"])

    # "src/** allows both"
    assert path_allowed("src/a.py", ["src/**"])
    assert path_allowed("src/a/b.py", ["src/**"])

    # Disallowed outside root
    assert not path_allowed("other/a.py", ["src/**"])
    assert not path_allowed("src_extra/a.py", ["src/*"])
    assert not path_allowed("src_extra/a.py", ["src/**"])

    # Multi-segment matching with ** in the middle
    assert path_allowed("src/pkg/sub/test.py", ["src/**/test.py"])
    assert path_allowed("src/test.py", ["src/**/test.py"])
    assert not path_allowed("src/pkg/sub/other.py", ["src/**/test.py"])

    # Segment character classes [..]
    assert path_allowed("src/1/app.py", ["src/[0-9]/*.py"])
    assert not path_allowed("src/10/app.py", ["src/[0-9]/*.py"])
    assert not path_allowed("src/a/app.py", ["src/[0-9]/*.py"])

    # Single character ?
    assert path_allowed("src/a1/app.py", ["src/?1/*.py"])
    assert not path_allowed("src/ab1/app.py", ["src/?1/*.py"])

    # Empty allowed paths
    assert not path_allowed("any/path.py", [])


def test_add_worktree_checks_out_base_and_post_checkout_hook_writes_no_marker(tmp_path: Path) -> None:
    ctrl_repo = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(ctrl_repo)], check=True, capture_output=True)

    seed_dir = tmp_path / "seed"
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.email", "seed@omp.dev"], check=True)

    (seed_dir / "app.py").write_text("print('hello world')\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(seed_dir), "rev-parse", "HEAD"]).decode().strip()
    subprocess.run(["git", "-C", str(seed_dir), "push", str(ctrl_repo), f"{base}:refs/heads/main"], check=True, capture_output=True)

    # Plant evil post-checkout hook in control_repo hooks dir
    marker_ctrl_hook = tmp_path / "marker_ctrl_hook"
    hooks_dir = ctrl_repo / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    post_checkout_hook = hooks_dir / "post-checkout"
    post_checkout_hook.write_text(f"#!/bin/sh\ntouch '{marker_ctrl_hook}'\nexit 0\n")
    post_checkout_hook.chmod(0o755)

    # Also plant core.hooksPath in control_repo config
    evil_hooks_dir = tmp_path / "ctrl_evil_hooks"
    evil_hooks_dir.mkdir(parents=True)
    evil_hook = evil_hooks_dir / "post-checkout"
    evil_hook.write_text(f"#!/bin/sh\ntouch '{marker_ctrl_hook}'\nexit 0\n")
    evil_hook.chmod(0o755)
    subprocess.run(["git", "-C", str(ctrl_repo), "config", "core.hooksPath", str(evil_hooks_dir)], check=True)

    worktrees_dir = tmp_path / "worktrees"
    wt = add_worktree(ctrl_repo, worktrees_dir, "worker-test", base)

    assert wt.exists()
    assert (wt / "app.py").read_text() == "print('hello world')\n"
    wt_head = git(wt, "rev-parse", "HEAD").decode().strip()
    assert wt_head == base

    assert not marker_ctrl_hook.exists(), "post-checkout hook in control repo should not have run"


def test_git_input_and_index_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    # Test stdin input hashing
    sha = git(repo, "hash-object", "--stdin", input="test payload\n").decode().strip()
    assert sha

    # Test index_file parameter
    custom_index = tmp_path / "custom.index"
    empty_tree = git(repo, "mktree", input="").decode().strip()
    git(repo, "read-tree", empty_tree, index_file=custom_index)
    assert custom_index.exists()


def test_git_failure_raises_candidate_git_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    with pytest.raises(CandidateGitError) as exc_info:
        git(repo, "invalid-git-command-12345")
    assert exc_info.value.code == "git_failed"
