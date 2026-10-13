"""Tests for verifier-side candidate preparation (OMP-417).

Each test builds a real bare control repository with a pinned base and candidate
commit, adds a detached worktree at the candidate, and drives
``prepare_candidate`` and ``VerifyTask``. The contracts covered are: the frozen
snapshot's bytes, the envelope refusals (outside change/delete/rename), digest
and HEAD mismatches, dirty tracked/index/untracked/ignored state, bad task
input, unsafe links and export-attribute transforms, and cleanup on every exit.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import uuid

import pytest
from pydantic import ValidationError

from omp_work.orchestrator.candidate_git import (
    CandidateGitError,
    add_worktree,
    audit_inputs,
    git,
)
from omp_work.orchestrator.verify_candidate import (
    TRANSPORT_PATHS,
    VerifyTask,
    prepare_candidate,
)

_KEY = "/run/secrets/verifier.key"


def _git_env() -> dict[str, str]:
    return {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        env={**_git_env(), "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )


def _write_base(seed: Path) -> None:
    (seed / "app.py").write_text("print('base')\n", encoding="utf-8")
    (seed / "README.md").write_text("# Readme\n", encoding="utf-8")
    (seed / "keep.txt").write_text("keep\n", encoding="utf-8")
    (seed / "removed.txt").write_text("removed\n", encoding="utf-8")
    (seed / "dir").mkdir()
    (seed / "dir" / "nested.txt").write_text("nested\n", encoding="utf-8")
    script = seed / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)


def _default_candidate(seed: Path) -> None:
    (seed / "app.py").write_text("print('cand')\n", encoding="utf-8")
    (seed / "added.txt").write_text("added\n", encoding="utf-8")
    (seed / "removed.txt").unlink()


def _world(
    tmp_path: Path,
    build: Callable[[Path], None] = _default_candidate,
    *,
    name: str = "wt",
) -> tuple[Path, str, str, Path]:
    """Bare control repo with base/candidate commits and a detached candidate worktree."""
    bare = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.name", "t")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "commit.gpgsign", "false")
    _write_base(seed)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "base")
    base = _git(seed, "rev-parse", "HEAD").stdout.decode().strip()
    _git(seed, "push", str(bare), "HEAD:refs/heads/main")

    build(seed)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "candidate")
    cand = _git(seed, "rev-parse", "HEAD").stdout.decode().strip()
    _git(seed, "push", str(bare), "HEAD:refs/heads/cand")

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    wt = add_worktree(bare, worktrees, name, cand)
    return bare, base, cand, wt


def _task(
    bare: Path,
    base: str,
    cand: str,
    *,
    allowed: tuple[str, ...] = ("app.py", "added.txt", "removed.txt"),
    digest: str | None = None,
    attempt_id: str | None = None,
    candidate_commit: str | None = None,
    test_command: tuple[str, ...] = ("/usr/bin/true",),
    **extra: Any,
) -> dict[str, Any]:
    computed = digest if digest is not None else str(audit_inputs(bare, base, cand)["diff_sha256"])
    task: dict[str, Any] = {
        "candidate_sha256": computed,
        "attempt_id": attempt_id if attempt_id is not None else str(uuid.uuid4()),
        "verifier_id": "verifier",
        "key_path": _KEY,
        "test_command": list(test_command),
        "criteria": [{"statement": "feature works", "observable_outcome": "app prints cand"}],
        "candidate_commit": candidate_commit if candidate_commit is not None else cand,
        "base_commit": base,
        "allowed_paths": list(allowed),
        "timeout_seconds": 30.0,
    }
    task.update(extra)
    return task


def _write_transport(wt: Path, *names: str) -> None:
    omp = wt / ".omp"
    omp.mkdir(exist_ok=True)
    for name in names:
        (omp / Path(name).name).write_text(f"transport {name}\n", encoding="utf-8")


def test_prepare_yields_frozen_snapshot_bytes(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    _write_transport(wt, "verify-task.json")

    with prepare_candidate(_task(bare, base, cand), wt) as snapshot:
        assert snapshot.is_dir()
        assert snapshot != wt
        assert (snapshot / "app.py").read_bytes() == b"print('cand')\n"
        assert (snapshot / "added.txt").read_bytes() == b"added\n"
        assert (snapshot / "dir" / "nested.txt").read_bytes() == b"nested\n"
        # The archive preserves the executable mode bit.
        assert os.access(snapshot / "script.sh", os.X_OK)
        assert not (snapshot / "removed.txt").exists()
        # The archive carries the commit's tree, not the untracked transport.
        assert not (snapshot / ".omp").exists()
        # The command is never executed by preparation.
        assert not (snapshot / "ran.txt").exists()
        # The snapshot is an independent copy, not the worktree.
        (snapshot / "app.py").write_text("mutated\n", encoding="utf-8")
        assert (wt / "app.py").read_text(encoding="utf-8") == "print('cand')\n"

    assert not snapshot.exists()


def test_prepare_rejects_outside_change(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / "outside.txt").write_text("outside\n", encoding="utf-8")

    bare, base, cand, wt = _world(tmp_path, build)
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "outside_allowed:outside.txt" in exc.value.violations


def test_prepare_rejects_outside_delete(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / "keep.txt").unlink()

    bare, base, cand, wt = _world(tmp_path, build)
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "outside_allowed:keep.txt" in exc.value.violations


def test_prepare_rejects_outside_rename(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / "keep.txt").rename(seed / "moved.txt")

    bare, base, cand, wt = _world(tmp_path, build)
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "outside_allowed:keep.txt" in exc.value.violations
    assert "outside_allowed:moved.txt" in exc.value.violations


def test_prepare_rejects_digest_mismatch(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    observed = str(audit_inputs(bare, base, cand)["diff_sha256"])
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, digest="0" * 64), wt):
            pass
    assert exc.value.code == "digest_mismatch"
    assert exc.value.violations == [observed]


def test_prepare_rejects_head_mismatch(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    # A worktree at the base commit while the task pins the candidate commit.
    other = add_worktree(bare, tmp_path / "worktrees", "at-base", base)
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), other):
            pass
    assert exc.value.code == "head_mismatch"
    assert exc.value.violations == [base]


def test_prepare_rejects_dirty_tracked_and_staged(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)

    (wt / "app.py").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "dirty_worktree"
    assert "tracked_change:app.py" in exc.value.violations

    # Staged-only dirt: the index differs from the commit while the tree does not.
    _git(wt, "checkout", "--", "app.py")
    (wt / "README.md").write_text("staged\n", encoding="utf-8")
    _git(wt, "add", "README.md")
    _git(wt, "restore", "--source=HEAD", "--worktree", "README.md")
    assert git(wt, "--no-optional-locks", "status", "--porcelain").decode().startswith("M")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "dirty_worktree"
    assert "tracked_change:README.md" in exc.value.violations


def test_prepare_rejects_untracked_and_ignored(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / ".gitignore").write_text("*.log\n", encoding="utf-8")

    bare, base, cand, wt = _world(tmp_path, build)
    allowed = ("app.py", "added.txt", "removed.txt", ".gitignore")

    (wt / "stray.txt").write_text("stray\n", encoding="utf-8")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt):
            pass
    assert exc.value.code == "dirty_worktree"
    assert "unexpected:stray.txt" in exc.value.violations

    (wt / "stray.txt").unlink()
    (wt / "debug.log").write_text("ignored\n", encoding="utf-8")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt):
            pass
    assert exc.value.code == "dirty_worktree"
    assert "unexpected:debug.log" in exc.value.violations


def test_prepare_rejects_committed_transport(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / ".omp").mkdir()
        (seed / ".omp" / "verify-task.json").write_text("{}\n", encoding="utf-8")

    bare, base, cand, wt = _world(tmp_path, build)
    allowed = ("app.py", "added.txt", "removed.txt", ".omp/verify-task.json")
    # The committed transport is untracked in the worktree, so also clear it there.
    (wt / ".omp" / "verify-task.json").unlink()
    (wt / ".omp").rmdir()

    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt):
            pass
    assert exc.value.code == "committed_transport"
    assert exc.value.violations == [".omp/verify-task.json"]


def test_prepare_allows_all_transport_files(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    _write_transport(wt, *TRANSPORT_PATHS)
    with prepare_candidate(_task(bare, base, cand), wt) as snapshot:
        assert snapshot.is_dir()


def test_prepare_rejects_transport_symlink_escape(tmp_path: Path) -> None:
    """An untracked transport file that is an escaping link is refused, never exported."""
    bare, base, cand, wt = _world(tmp_path)
    omp = wt / ".omp"
    omp.mkdir()
    (omp / "verdict.json").symlink_to("/etc/passwd")

    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "symlink_escape:.omp/verdict.json" in exc.value.violations


@pytest.mark.parametrize(
    "criteria",
    [
        [],
        [{"statement": "contradictory prose is ignored by preparation"}],
        [{"statement": "x", "oracle": "automated_test", "nested": {"k": [1, 2, 3]}}],
    ],
)
def test_prepare_criteria_prose_is_not_an_oracle(tmp_path: Path, criteria: list[dict[str, Any]]) -> None:
    """Criteria are carried typed data; their prose never changes or gates preparation."""
    bare, base, cand, wt = _world(tmp_path)
    task = _task(bare, base, cand, criteria=criteria)
    with prepare_candidate(task, wt) as snapshot:
        assert sorted(p.name for p in snapshot.iterdir()) == [
            "README.md",
            "added.txt",
            "app.py",
            "dir",
            "keep.txt",
            "script.sh",
        ]
        assert not (snapshot / ".omp").exists()


@pytest.mark.parametrize("target", ["escape-abs", "escape-rel"])
def test_prepare_rejects_escaping_link(tmp_path: Path, target: str) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        if target == "escape-abs":
            (seed / "escape").symlink_to("/etc/passwd")
        else:
            (seed / "escape").symlink_to("../outside")

    bare, base, cand, wt = _world(tmp_path, build)
    allowed = ("app.py", "added.txt", "removed.txt", "escape")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "symlink_escape:escape" in exc.value.violations


def test_prepare_allows_inside_symlink(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / "link").symlink_to("app.py")

    bare, base, cand, wt = _world(tmp_path, build)
    allowed = ("app.py", "added.txt", "removed.txt", "link")
    with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt) as snapshot:
        assert (snapshot / "link").is_symlink()
        assert (snapshot / "link").read_text(encoding="utf-8") == "print('cand')\n"


def test_prepare_rejects_export_attribute_transform(tmp_path: Path) -> None:
    def build(seed: Path) -> None:
        _default_candidate(seed)
        (seed / ".gitattributes").write_text(
            "doc.md export-subst\nkeep.txt export-ignore\n",
            encoding="utf-8",
        )
        (seed / "doc.md").write_text("commit $Format:%H$\n", encoding="utf-8")

    bare, base, cand, wt = _world(tmp_path, build)
    allowed = ("app.py", "added.txt", "removed.txt", ".gitattributes", "doc.md", "keep.txt")
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(_task(bare, base, cand, allowed=allowed), wt):
            pass
    assert exc.value.code == "envelope_violation"
    assert "outside_allowed:keep.txt" in exc.value.violations
    assert "outside_allowed:doc.md" in exc.value.violations


def test_prepare_cleans_snapshot_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, base, cand, wt = _world(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))

    def leftovers() -> set[Path]:
        return set(private.glob("omp-verify-snapshot-*"))

    # Exception raised inside the body still removes the snapshot.
    holder: dict[str, Path] = {}
    with pytest.raises(RuntimeError, match="boom"):
        with prepare_candidate(_task(bare, base, cand), wt) as snapshot:
            holder["snapshot"] = snapshot
            raise RuntimeError("boom")
    assert holder["snapshot"].parent == private
    assert not holder["snapshot"].exists()
    assert leftovers() == set()

    # A validation failure creates no snapshot at all.
    with pytest.raises(CandidateGitError):
        with prepare_candidate(_task(bare, base, cand, digest="0" * 64), wt):
            pass
    assert leftovers() == set()


_VALID = {
    "candidate_sha256": "a" * 64,
    "attempt_id": "123e4567-e89b-12d3-a456-426614174000",
    "verifier_id": "verifier",
    "key_path": "/run/secrets/verifier.key",
    "test_command": ["/usr/bin/truth"],
    "criteria": [{"statement": "works"}],
    "candidate_commit": "b" * 40,
    "base_commit": "c" * 40,
    "allowed_paths": ["src/**"],
    "timeout_seconds": 30.0,
}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_sha256", "A" * 64),
        ("candidate_sha256", "a" * 63),
        ("candidate_commit", "b" * 39),
        ("candidate_commit", "B" * 40),
        ("base_commit", "c" * 41),
        ("attempt_id", "not-a-uuid"),
        ("attempt_id", "123E4567-E89B-12D3-A456-426614174000"),
        ("verifier_id", "worker"),
        ("key_path", "relative/key"),
        ("test_command", []),
        ("test_command", "true"),
        ("criteria", "statement"),
        ("criteria", [1]),
        ("allowed_paths", "src/**"),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1.0),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", "30"),
    ],
)
def test_verify_task_rejects_bad_input(field: str, value: Any) -> None:
    bad = dict(_VALID)
    bad[field] = value
    with pytest.raises(ValidationError):
        VerifyTask.model_validate(bad)


def test_verify_task_rejects_missing_and_extra_fields() -> None:
    missing = dict(_VALID)
    del missing["allowed_paths"]
    with pytest.raises(ValidationError):
        VerifyTask.model_validate(missing)

    with pytest.raises(ValidationError):
        VerifyTask.model_validate({**_VALID, "unexpected": True})


def test_verify_task_accepts_valid_and_is_frozen() -> None:
    task = VerifyTask.model_validate(_VALID)
    assert task.verifier_id == "verifier"
    assert task.timeout_seconds == 30.0
    with pytest.raises(ValidationError):
        task.candidate_sha256 = "0" * 64


def test_prepare_rejects_non_mapping_task(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(object(), wt):  # type: ignore[arg-type]
            pass
    assert exc.value.code == "invalid_task"


def test_prepare_rejects_invalid_task_document(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    bad = _task(bare, base, cand)
    bad["verifier_id"] = "worker"
    with pytest.raises(CandidateGitError) as exc:
        with prepare_candidate(bad, wt):
            pass
    assert exc.value.code == "invalid_task"
    assert any(v.startswith("verifier_id:") for v in exc.value.violations)


def test_prepare_accepts_valid_task_instance(tmp_path: Path) -> None:
    bare, base, cand, wt = _world(tmp_path)
    task = VerifyTask.model_validate(_task(bare, base, cand))
    with prepare_candidate(task, wt) as snapshot:
        assert (snapshot / "added.txt").read_bytes() == b"added\n"


def test_transport_paths_are_the_three_orchestrator_files() -> None:
    """Only the three orchestrator transport files are exempt from the dirty check."""
    assert set(TRANSPORT_PATHS) == {
        ".omp/verify-task.json",
        ".omp/verdict.json",
        ".omp/audit-report.txt",
    }
