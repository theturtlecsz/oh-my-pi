"""Tests for hardened candidate git operations (OMP-417-s04-s01)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from omp_work.orchestrator.candidate_git import (
    CandidateGitError,
    IntentLog,
    _scan,
    add_worktree,
    audit_inputs,
    check_envelope,
    freeze,
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


def test_path_allowed_matches_verbatim_no_normalization() -> None:
    # Whitespace is part of the filename: no strip on path or pattern.
    assert not path_allowed(" README", ["README"])
    assert not path_allowed("README ", ["README"])
    assert not path_allowed("README", [" README"])
    assert path_allowed("README", ["README"])

    # Backslash is a literal filename byte, not a POSIX separator.
    assert not path_allowed("src\\a.py", ["src/*"])
    assert path_allowed("src\\a.py", ["src\\a.py"])

    # A trailing slash does not imply recursion; only explicit ** recurses.
    assert not path_allowed("README/x", ["README/"])
    assert path_allowed("README", ["README/"])
    assert path_allowed("README/x", ["README/**"])
    assert not path_allowed("README/x", ["README"])


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


def _make_control_repo_and_base(tmp_path: Path) -> tuple[Path, str]:
    ctrl_repo = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(ctrl_repo)], check=True, capture_output=True)

    seed_dir = tmp_path / "seed"
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.email", "seed@omp.dev"], check=True)

    (seed_dir / "app.py").write_text("print('hello world')\n")
    (seed_dir / "README.md").write_text("# Readme\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(seed_dir), "rev-parse", "HEAD"]).decode().strip()
    subprocess.run(["git", "-C", str(seed_dir), "push", str(ctrl_repo), f"{base}:refs/heads/main"], check=True, capture_output=True)
    return ctrl_repo, base


def test_check_envelope_allowed_change_and_scan_snapshot(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_allowed", base)

    # Modify allowed file
    (wt / "app.py").write_text("print('modified')\n")
    # Add new allowed file
    (wt / "src").mkdir()
    (wt / "src" / "helper.py").write_text("def helper(): pass\n")
    # Delete allowed file
    (wt / "README.md").unlink()

    violations = check_envelope(wt, base, ["app.py", "src/**", "README.md"], repository=ctrl_repo)
    assert violations == []

    # Also test _scan return value contract
    scan_violations, changed_map = _scan(wt, base, ["app.py", "src/**", "README.md"], repository=ctrl_repo)
    assert scan_violations == []
    assert changed_map["app.py"] == ("100644", b"print('modified')\n")
    assert changed_map["src/helper.py"] == ("100644", b"def helper(): pass\n")
    assert changed_map["README.md"] is None


def test_check_envelope_outside_allowed(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_outside", base)

    (wt / "app.py").write_text("print('modified')\n")
    (wt / "outside.txt").write_text("disallowed\n")

    violations = check_envelope(wt, base, ["src/**"], repository=ctrl_repo)
    assert "outside_allowed:app.py" in violations
    assert "outside_allowed:outside.txt" in violations


def test_check_envelope_refuses_src_git_symlink_to_outside(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_src_git", base)

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (wt / "src").mkdir()
    os.symlink(str(outside_dir), str(wt / "src" / ".git"))

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "git_metadata:src/.git" in violations
    assert "symlink_escape:src/.git" in violations


def test_check_envelope_refuses_root_git_dir_holding_escape(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_root_git_dir", base)

    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    (wt / ".git" / "escape").symlink_to("/etc/passwd")

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "git_metadata:.git" in violations
    assert "symlink_escape:.git/escape" in violations


def test_check_envelope_root_gitfile_plain_and_hardlinked(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_gitfile", base)

    # Plain gitfile -> no violation
    violations_plain = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "hardlink:.git" not in violations_plain
    assert violations_plain == []

    # Hardlinked elsewhere -> hardlink:.git
    other_link = tmp_path / "other_gitfile"
    os.link(wt / ".git", other_link)

    violations_hardlinked = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "hardlink:.git" in violations_hardlinked


def test_check_envelope_regular_file_hardlink(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_hardlink_reg", base)

    (wt / "file1.txt").write_text("hello\n")
    os.link(wt / "file1.txt", wt / "file2.txt")

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "hardlink:file1.txt" in violations
    assert "hardlink:file2.txt" in violations


def test_check_envelope_unsupported_fifo(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_unsupported", base)

    os.mkfifo(wt / "my_fifo")

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "unsupported:my_fifo" in violations


def test_check_envelope_symlink_escape_non_git(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_symlink_esc", base)

    (wt / "bad_link").symlink_to("/etc/passwd")
    (wt / "safe_link").symlink_to("app.py")

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert "symlink_escape:bad_link" in violations
    assert "symlink_escape:safe_link" not in violations


def test_check_envelope_live_checkout_changed(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)

    # Set up live checkout
    live_dir = tmp_path / "live_checkout"
    subprocess.run(["git", "clone", str(ctrl_repo), str(live_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live_dir), "config", "user.name", "Live"], check=True)
    subprocess.run(["git", "-C", str(live_dir), "config", "user.email", "live@omp.dev"], check=True)

    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_live", base)

    # Clean live checkout -> no violation
    clean_violations = check_envelope(wt, base, ["**"], repository=ctrl_repo, live_checkout=live_dir)
    assert clean_violations == []

    # 1. Tracked edit
    (live_dir / "app.py").write_text("print('live edit')\n")
    # 2. Untracked file
    (live_dir / "untracked.txt").write_text("untracked\n")
    # 3. Ignored file
    (live_dir / ".gitignore").write_text("ignored.txt\n")
    subprocess.run(["git", "-C", str(live_dir), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(live_dir), "commit", "-m", "ignore rule"], check=True, capture_output=True)
    (live_dir / "ignored.txt").write_text("ignored content\n")

    violations = check_envelope(wt, base, ["**"], repository=ctrl_repo, live_checkout=live_dir)
    assert any(v.startswith("live_checkout_changed:") and "app.py" in v for v in violations)
    assert any(v.startswith("live_checkout_changed:") and "untracked.txt" in v for v in violations)
    assert any(v.startswith("live_checkout_changed:") and "ignored.txt" in v for v in violations)


def test_check_envelope_attacker_git_replace_hiding_outside_txt(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_attacker", base)

    # Create attacker repository with git replace ref hiding outside.txt
    attacker_repo = tmp_path / "attacker.git"
    subprocess.run(["git", "clone", "--bare", str(ctrl_repo), str(attacker_repo)], check=True, capture_output=True)

    scratch = tmp_path / "attacker_scratch"
    subprocess.run(["git", "clone", str(attacker_repo), str(scratch)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(scratch), "config", "user.name", "Attacker"], check=True)
    subprocess.run(["git", "-C", str(scratch), "config", "user.email", "attacker@omp.dev"], check=True)
    (scratch / "outside.txt").write_text("attacker content\n")
    subprocess.run(["git", "-C", str(scratch), "add", "outside.txt"], check=True)
    subprocess.run(["git", "-C", str(scratch), "commit", "-m", "add outside"], check=True, capture_output=True)

    base_tree = subprocess.check_output(["git", "-C", str(scratch), "rev-parse", f"{base}^{{tree}}"]).decode().strip()
    replaced_tree = subprocess.check_output(["git", "-C", str(scratch), "rev-parse", "HEAD^{tree}"]).decode().strip()
    subprocess.run(["git", "-C", str(scratch), "push", "origin", "HEAD:refs/heads/outside"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(attacker_repo), "replace", base_tree, replaced_tree], check=True, capture_output=True)

    # Point worktree .git to attacker repo
    (wt / ".git").write_text(f"gitdir: {attacker_repo}\n")
    (wt / "outside.txt").write_text("attacker content\n")

    # check_envelope queries control_repo, ignoring attacker's git replace
    violations = check_envelope(wt, base, ["app.py"], repository=ctrl_repo)
    assert "outside_allowed:outside.txt" in violations


def test_check_envelope_refusal_leaves_control_repo_objects_unchanged(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_refuse", base)

    (wt / "forbidden.txt").write_text("disallowed\n")

    before_objects = git(ctrl_repo, "cat-file", "--batch-all-objects", "--batch-check")
    violations = check_envelope(wt, base, ["app.py"], repository=ctrl_repo)
    assert violations == ["outside_allowed:forbidden.txt"]
    after_objects = git(ctrl_repo, "cat-file", "--batch-all-objects", "--batch-check")

    assert before_objects == after_objects


def test_check_envelope_every_code_produced(tmp_path: Path) -> None:
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_codes", base)

    # 1. outside_allowed
    (wt / "outside.txt").write_text("outside\n")
    v_outside = check_envelope(wt, base, ["app.py"], repository=ctrl_repo)
    assert any(v.startswith("outside_allowed:") for v in v_outside)
    (wt / "outside.txt").unlink()

    # 2. git_metadata
    (wt / "src").mkdir()
    (wt / "src" / ".git").mkdir()
    v_meta = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert any(v.startswith("git_metadata:") for v in v_meta)
    (wt / "src" / ".git").rmdir()

    # 3. symlink_escape
    (wt / "escape_link").symlink_to("/etc/passwd")
    v_esc = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert any(v.startswith("symlink_escape:") for v in v_esc)
    (wt / "escape_link").unlink()

    # 4. hardlink
    (wt / "h1.txt").write_text("h\n")
    os.link(wt / "h1.txt", wt / "h2.txt")
    v_hard = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert any(v.startswith("hardlink:") for v in v_hard)
    (wt / "h1.txt").unlink()
    (wt / "h2.txt").unlink()

    # 5. unsupported
    os.mkfifo(wt / "my_fifo")
    v_unsup = check_envelope(wt, base, ["**"], repository=ctrl_repo)
    assert any(v.startswith("unsupported:") for v in v_unsup)
    (wt / "my_fifo").unlink()

    # 6. live_checkout_changed
    live_dir = tmp_path / "live_codes"
    subprocess.run(["git", "clone", str(ctrl_repo), str(live_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live_dir), "config", "user.name", "Live"], check=True)
    subprocess.run(["git", "-C", str(live_dir), "config", "user.email", "live@omp.dev"], check=True)
    (live_dir / "untracked.txt").write_text("new\n")
    v_live = check_envelope(wt, base, ["**"], repository=ctrl_repo, live_checkout=live_dir)
    assert any(v.startswith("live_checkout_changed:") for v in v_live)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_scan_unreadable_disallowed_file_fails_closed(tmp_path: Path) -> None:
    """An unreadable file must abort the scan, never be dropped into an approval."""
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_unreadable", base)

    (wt / "app.py").write_text("print('changed')\n")
    secret = wt / "secret.txt"
    secret.write_text("unreadable\n")
    secret.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            _scan(wt, base, ["app.py"], repository=ctrl_repo)
    finally:
        secret.chmod(0o644)


def test_scan_non_utf8_symlink_target_is_preserved(tmp_path: Path) -> None:
    """Non-UTF-8 link bytes round-trip instead of crashing the UTF-8 encode."""
    ctrl_repo, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl_repo, tmp_path / "worktrees", "wt_nonutf8", base)

    target_bytes = b"app.py\xff\xfe"
    os.symlink(target_bytes, os.fsencode(str(wt / "link")))

    violations = check_envelope(wt, base, ["link"], repository=ctrl_repo)
    assert violations == []

    _, changed_map = _scan(wt, base, ["link"], repository=ctrl_repo)
    assert changed_map["link"] == ("120000", target_bytes)


_CANDIDATE_AUTHOR = "Cand <cand@omp.dev>"


def _ls_map(repo: Path, rev: str) -> dict[str, tuple[str, str]]:
    out = git(repo, "ls-tree", "-r", "-z", rev)
    entries: dict[str, tuple[str, str]] = {}
    if not out:
        return entries
    for item in out.split(b"\0"):
        if not item:
            continue
        meta, path_b = item.split(b"\t", 1)
        mode_b, _type_b, sha_b = meta.split(b" ", 2)
        entries[path_b.decode("utf-8")] = (mode_b.decode("ascii"), sha_b.decode("ascii"))
    return entries


def _make_rich_base(tmp_path: Path) -> tuple[Path, str]:
    """Base tree with a file to chmod and a directory an inside symlink can live in."""
    ctrl_repo = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(ctrl_repo)], check=True, capture_output=True)

    seed_dir = tmp_path / "seed"
    subprocess.run(["git", "init", str(seed_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_dir), "config", "user.email", "seed@omp.dev"], check=True)

    (seed_dir / "app.py").write_text("print('hello world')\n")
    (seed_dir / "README.md").write_text("# Readme\n")
    (seed_dir / "script.sh").write_text("#!/bin/sh\necho hi\n")
    (seed_dir / "dir").mkdir()
    (seed_dir / "dir" / "inside.txt").write_text("inside\n")
    subprocess.run(["git", "-C", str(seed_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(seed_dir), "commit", "-m", "initial commit"],
        check=True,
        capture_output=True,
    )
    base = subprocess.check_output(["git", "-C", str(seed_dir), "rev-parse", "HEAD"]).decode().strip()
    subprocess.run(
        ["git", "-C", str(seed_dir), "push", str(ctrl_repo), f"{base}:refs/heads/main"],
        check=True,
        capture_output=True,
    )
    return ctrl_repo, base


def _freeze(ctrl: Path, wt: Path, base: str, message: str, *, repository: Path | None = None) -> str:
    return freeze(
        ctrl,
        wt,
        base,
        ["**"],
        repository=ctrl if repository is None else repository,
        live_checkout=None,
        message=message,
        author=_CANDIDATE_AUTHOR,
    )


def test_freeze_twice_same_sha_and_tree_is_base_plus_edits(tmp_path: Path) -> None:
    """Two freezes of the same edits are one sha, and the tree is base plus those edits."""
    ctrl, base = _make_rich_base(tmp_path)
    wt = add_worktree(ctrl, tmp_path / "worktrees", "wt_freeze", base)

    gpg_marker = tmp_path / "gpg-marker"
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text(f"#!/bin/sh\ntouch '{gpg_marker}'\nexit 1\n")
    fake_gpg.chmod(0o755)
    subprocess.run(["git", "-C", str(ctrl), "config", "commit.gpgsign", "true"], check=True)
    subprocess.run(["git", "-C", str(ctrl), "config", "gpg.program", str(fake_gpg)], check=True)

    changed = b"print('changed')\n"
    (wt / "app.py").write_bytes(changed)
    (wt / "README.md").unlink()
    (wt / "script.sh").chmod(0o755)
    (wt / "dir" / "link").symlink_to("inside.txt")

    refs_before = git(ctrl, "show-ref")
    sha1 = _freeze(ctrl, wt, base, "candidate")
    sha2 = _freeze(ctrl, wt, base, "candidate")
    assert sha1 == sha2
    assert not gpg_marker.exists()

    expected = _ls_map(ctrl, base)
    app_sha = git(ctrl, "hash-object", "--no-filters", "--stdin", input=changed).decode().strip()
    expected["app.py"] = ("100644", app_sha)
    del expected["README.md"]
    _mode, script_sha = expected["script.sh"]
    assert _mode == "100644"
    expected["script.sh"] = ("100755", script_sha)
    link_sha = git(ctrl, "hash-object", "--no-filters", "--stdin", input=b"inside.txt").decode().strip()
    expected["dir/link"] = ("120000", link_sha)
    assert _ls_map(ctrl, sha1) == expected

    assert git(ctrl, "rev-parse", f"{sha1}^").decode().strip() == base
    assert git(ctrl, "show-ref") == refs_before
    head = subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"]).decode().strip()
    assert head == base
    assert not (wt / "README.md").exists()
    assert (wt / "dir" / "link").is_symlink()
    assert os.readlink(wt / "dir" / "link") == "inside.txt"

    base_date = git(ctrl, "log", "-1", "--format=%cI", base).decode().strip()
    ident = git(ctrl, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%aI%n%cI%n%s", sha1).decode().splitlines()
    assert ident == [
        "Cand",
        "cand@omp.dev",
        "Cand",
        "cand@omp.dev",
        base_date,
        base_date,
        "candidate",
    ]


def test_freeze_violation_leaves_control_repo_objects_unchanged(tmp_path: Path) -> None:
    ctrl, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl, tmp_path / "worktrees", "wt_freeze_refuse", base)
    (wt / "app.py").write_text("print('would have been allowed')\n")
    (wt / "forbidden.txt").write_text("nope\n")
    (wt / "bad_link").symlink_to("/etc/passwd")

    live = tmp_path / "live"
    subprocess.run(["git", "clone", str(ctrl), str(live)], check=True, capture_output=True)
    (live / "untracked.txt").write_text("dirty\n")

    before = git(ctrl, "cat-file", "--batch-all-objects", "--batch-check")
    refs_before = git(ctrl, "show-ref")
    with pytest.raises(CandidateGitError) as exc:
        freeze(
            ctrl,
            wt,
            base,
            ["app.py"],
            repository=ctrl,
            live_checkout=live,
            message="refused",
            author=_CANDIDATE_AUTHOR,
        )
    assert exc.value.code == "envelope_violation"
    assert "outside_allowed:forbidden.txt" in exc.value.violations
    assert "symlink_escape:bad_link" in exc.value.violations
    assert any(
        v.startswith("live_checkout_changed:") and "untracked.txt" in v for v in exc.value.violations
    )
    assert git(ctrl, "cat-file", "--batch-all-objects", "--batch-check") == before
    assert git(ctrl, "show-ref") == refs_before


def test_freeze_attacker_gitfile_writes_raw_bytes_and_no_marker(tmp_path: Path) -> None:
    """A hostile gitfile's hooks, fsmonitor, and clean filter must not run or rewrite blobs."""
    ctrl, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl, tmp_path / "worktrees", "wt_evil", base)

    marker = tmp_path / "marker"
    scripts = tmp_path / "evil-scripts"
    hooks_path = tmp_path / "evil-hooks"
    scripts.mkdir()
    hooks_path.mkdir()
    clean = scripts / "clean.sh"
    clean.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'FILTERED\\n'\n")
    clean.chmod(0o755)
    fsmonitor = scripts / "fsmonitor.sh"
    fsmonitor.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    fsmonitor.chmod(0o755)
    for hook_name in ("pre-commit", "pre-push"):
        hook = hooks_path / hook_name
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
        hook.chmod(0o755)

    attacker = tmp_path / "attacker"
    subprocess.run(["git", "init", str(attacker)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(attacker), "config", "core.hooksPath", str(hooks_path)], check=True)
    subprocess.run(["git", "-C", str(attacker), "config", "core.fsmonitor", str(fsmonitor)], check=True)
    subprocess.run(["git", "-C", str(attacker), "config", "filter.evil.clean", str(clean)], check=True)
    git_dir = attacker / ".git"
    for hook_name in ("pre-commit", "pre-push"):
        hook = git_dir / "hooks" / hook_name
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
        hook.chmod(0o755)

    (wt / ".git").write_text(f"gitdir: {git_dir}\n")
    raw = b"print('raw candidate bytes')\n"
    (wt / "app.py").write_bytes(raw)
    attributes = b"* filter=evil\n"
    (wt / ".gitattributes").write_bytes(attributes)

    # The trap is armed: hashing through the attacker gitdir rewrites the blob.
    canary = subprocess.run(
        ["git", "-C", str(wt), "hash-object", "--path", "app.py", "--stdin"],
        input=raw,
        capture_output=True,
        check=False,
    )
    assert canary.returncode == 0, canary.stderr
    assert marker.exists()
    filtered = subprocess.check_output(
        ["git", "-C", str(wt), "hash-object", "--stdin"],
        input=b"FILTERED\n",
    ).strip()
    assert canary.stdout.strip() == filtered
    marker.unlink()

    commit = _freeze(ctrl, wt, base, "raw")
    audited = audit_inputs(ctrl, base, commit)
    assert not marker.exists()
    assert git(ctrl, "cat-file", "blob", f"{commit}:app.py") == raw
    assert git(ctrl, "cat-file", "blob", f"{commit}:.gitattributes") == attributes
    assert audited["changed_paths"] == [".gitattributes", "app.py"]
    assert audited["candidate_tree_sha"] == git(ctrl, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    assert not marker.exists()


def test_audit_inputs_stable_and_one_byte_changes_digest(tmp_path: Path) -> None:
    ctrl, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl, tmp_path / "worktrees", "wt_audit", base)
    (wt / "app.py").write_text("print('v1')\n")
    (wt / "README.md").unlink()
    (wt / "extra.txt").write_text("extra\n")

    commit = _freeze(ctrl, wt, base, "audit")
    first = audit_inputs(ctrl, base, commit)
    second = audit_inputs(ctrl, base, commit)
    assert first == second
    assert first["changed_paths"] == ["README.md", "app.py", "extra.txt"]
    assert first["candidate_tree_sha"] == git(
        ctrl, "rev-parse", "--verify", f"{commit}^{{tree}}"
    ).decode().strip()
    diff = git(
        ctrl,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        base,
        commit,
    )
    assert first["diff_sha256"] == hashlib.sha256(diff).hexdigest()
    assert len(first["diff_sha256"]) == 64

    (wt / "app.py").write_text("print('v2')\n")
    commit_byte = _freeze(ctrl, wt, base, "audit")
    changed = audit_inputs(ctrl, base, commit_byte)
    assert changed["diff_sha256"] != first["diff_sha256"]
    assert changed["changed_paths"] == first["changed_paths"]
    assert changed["candidate_tree_sha"] == git(
        ctrl, "rev-parse", "--verify", f"{commit_byte}^{{tree}}"
    ).decode().strip()
    assert changed["candidate_tree_sha"] != first["candidate_tree_sha"]


def test_freeze_repository_mismatch(tmp_path: Path) -> None:
    ctrl, base = _make_control_repo_and_base(tmp_path)
    wt = add_worktree(ctrl, tmp_path / "worktrees", "wt_mismatch", base)
    (wt / "app.py").write_text("print('x')\n")

    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", str(other)], check=True, capture_output=True)
    before = git(ctrl, "cat-file", "--batch-all-objects", "--batch-check")
    with pytest.raises(CandidateGitError) as exc:
        freeze(
            ctrl,
            wt,
            base,
            ["**"],
            repository=other,
            live_checkout=None,
            message="nope",
            author=_CANDIDATE_AUTHOR,
        )
    assert exc.value.code == "repository_mismatch"
    assert exc.value.violations == []
    assert git(ctrl, "cat-file", "--batch-all-objects", "--batch-check") == before

    link = tmp_path / "ctrl-link"
    link.symlink_to(ctrl)
    via_link = _freeze(ctrl, wt, base, "via link", repository=link)
    via_path = _freeze(ctrl, wt, base, "via link")
    assert via_link == via_path

