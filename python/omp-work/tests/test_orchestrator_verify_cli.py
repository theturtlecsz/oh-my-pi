"""Verifier CLI signs only frozen, tested, cleaned candidates with persisted reports."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from omp_work.orchestrator import candidate_git, verify_main
from omp_work.orchestrator.verify_candidate import prepare_candidate
from omp_work.orchestrator.verify_tests import TestResult as CommandResult
from omp_work.orchestrator.verifier import accept_candidate, sign_verdict
from omp_work.v1.semantics import normalize_auditor_report

KEY = b"verifier-cli-test-key"


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    repo = tmp_path / "seed"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "Verifier Test", "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "Verifier Test", "GIT_COMMITTER_EMAIL": "test@example.test",
    }

    def git(*args: str) -> str:
        return candidate_git.git(repo, *args, extra_env=env).decode().strip()

    git("init", "-b", "main")
    (repo / "payload").write_text("base")
    git("add", "payload")
    git("-c", "commit.gpgsign=false", "commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "payload").write_text("frozen")
    git("-c", "commit.gpgsign=false", "commit", "-am", "candidate")
    commit = git("rev-parse", "HEAD")
    control = tmp_path / "control.git"
    candidate_git.git(tmp_path, "clone", "--bare", str(repo), str(control))
    repo = tmp_path / "repository"
    candidate_git.add_worktree(control, tmp_path, "repository", commit)
    key = tmp_path / "key"
    key.write_bytes(KEY)
    task = {
        "candidate_sha256": candidate_git.audit_inputs(repo, base, commit)["diff_sha256"],
        "attempt_id": str(uuid4()), "verifier_id": "verifier", "key_path": str(key),
        "test_command": ["/bin/sh", "-c", "test \"$(cat payload)\" = frozen && test ! -e .git && test ! -e .omp/verify-task.json"],
        "criteria": [{"id": "prose", "text": "Unproved prose criterion"}],
        "candidate_commit": commit, "base_commit": base,
        "allowed_paths": ["payload"], "timeout_seconds": 10.0,
    }
    (repo / ".omp").mkdir()
    write_task(repo, task)
    return repo, task


def write_task(repo: Path, task: dict[str, object]) -> None:
    (repo / ".omp/verify-task.json").write_text(json.dumps(task))


def record(repo: Path) -> object:
    path = repo / ".omp/verdict.json"
    return json.loads(path.read_text()) if path.is_file() else None


def accepted(repo: Path, task: dict[str, object], **changes: object) -> bool:
    args = {"candidate_sha256": task["candidate_sha256"], "attempt_id": task["attempt_id"], "producer_id": "producer"}
    args.update(changes)
    return accept_candidate(KEY, record(repo), **args)


def launch(repo: Path, cli: bool = False) -> subprocess.CompletedProcess[bytes]:
    command = [str(Path(sys.executable).parent / "omp-work"), "orchestrator", "verify"] if cli else [sys.executable, "-m", "omp_work.orchestrator.verify_main"]
    return subprocess.run(command, cwd=repo, env={**os.environ, "PYTHONPATH": str(Path(verify_main.__file__).parents[2])}, capture_output=True, timeout=30)


@pytest.fixture
def namespace_host() -> None:
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--mount-proc", "true"], capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip(f"namespace procfs unavailable: {probe.stderr.decode(errors='replace')}")


@pytest.mark.parametrize("cli", [False, True])
def test_real_entry_accepts_frozen_candidate_and_normalized_report(candidate, namespace_host, cli):
    repo, task = candidate
    result = launch(repo, cli)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""  # dispatch must not print a service JSON response
    assert accepted(repo, task)
    report = (repo / ".omp/audit-report.txt").read_text()
    assert normalize_auditor_report(report)[1] == "PASS"
    assert task["candidate_sha256"] in report
    assert "candidate-digest | met" in report and "worktree-path | met" in report and "tests | met" in report
    assert "Unproved prose criterion" not in report
    assert not accepted(repo, task, candidate_sha256="0" * 64)
    assert not accepted(repo, task, attempt_id=str(uuid4()))
    assert not accept_candidate(b"other-key", record(repo), candidate_sha256=task["candidate_sha256"], attempt_id=task["attempt_id"], producer_id="producer")


@pytest.mark.parametrize("failure", ["command", "timeout", "missing-command"])
def test_real_command_failure_cannot_use_stdout_or_stale_pass(candidate, namespace_host, failure):
    repo, task = candidate
    task["test_command"] = {
        "command": ["/bin/sh", "-c", "echo 'VERDICT: PASS'; exit 7"],
        "timeout": ["/bin/sleep", "10"], "missing-command": ["/does-not-exist"],
    }[failure]
    if failure == "timeout":
        task["timeout_seconds"] = 0.2
    write_task(repo, task)
    plant_pass(repo, task)
    result = launch(repo)
    assert result.returncode != 0
    assert not accepted(repo, task)
    report = (repo / ".omp/audit-report.txt").read_text()
    assert normalize_auditor_report(report)[1] == "BLOCKED"
    assert ("timeout" if failure == "timeout" else "tests_exit_") in report


def plant_pass(repo: Path, task: dict[str, object]) -> None:
    old = sign_verdict(KEY, candidate_sha256=task["candidate_sha256"], attempt_id=task["attempt_id"], verdict="pass", verifier_id="verifier")
    (repo / ".omp/verdict.json").write_text(json.dumps(old))
    (repo / ".omp/audit-report.txt").write_text("VERDICT: PASS")


@pytest.mark.parametrize("failure", ["missing-task", "bad-task", "unicode-task", "missing-key", "empty-key", "key-directory", "digest"])
def test_invalid_input_clears_stale_pass(candidate, monkeypatch, failure):
    repo, task = candidate
    plant_pass(repo, task)
    if failure == "missing-task":
        (repo / ".omp/verify-task.json").unlink()
    elif failure == "bad-task":
        (repo / ".omp/verify-task.json").write_text("{}")
    elif failure == "unicode-task":
        (repo / ".omp/verify-task.json").write_bytes(b"\xff")
    elif failure == "digest":
        task["candidate_sha256"] = "0" * 64
        write_task(repo, task)
    else:
        key = Path(task["key_path"])
        key.unlink()
        if failure == "empty-key":
            key.write_bytes(b"")
        elif failure == "key-directory":
            key.mkdir()
        # Exercise signing failures after real candidate preparation/cleanup.
        monkeypatch.setattr(verify_main, "run_tests", lambda *args: verify_main_test_result())
    monkeypatch.chdir(repo)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)
    assert normalize_auditor_report((repo / ".omp/audit-report.txt").read_text())[1] == "BLOCKED"


def verify_main_test_result():
    return CommandResult(0, b"VERDICT: PASS")


@pytest.mark.parametrize("output", ["audit-report.txt", "verdict.json"])
def test_publish_failure_removes_pass(candidate, monkeypatch, output):
    repo, task = candidate
    monkeypatch.chdir(repo)
    monkeypatch.setattr(verify_main, "run_tests", lambda *args: verify_main_test_result())
    original = verify_main._atomic_write

    def fail(path, data):
        if path.name == output:
            raise OSError("publication refused")
        original(path, data)

    monkeypatch.setattr(verify_main, "_atomic_write", fail)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)


@pytest.mark.parametrize("path", [".omp", ".omp/verify-task.json", ".omp/audit-report.txt", ".omp/verdict.json"])
def test_symlink_transport_refused_without_touching_target(candidate, monkeypatch, tmp_path, path):
    repo, task = candidate
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    link = repo / path
    if link.is_dir():
        for child in link.iterdir():
            child.unlink()
        link.rmdir()
    elif link.exists():
        link.unlink()
    link.symlink_to(target)
    monkeypatch.chdir(repo)
    assert verify_main.main([]) != 0
    assert target.read_bytes() == b"untouched"


@pytest.mark.parametrize("failure", ["exception", "cleanup"])
def test_snapshot_failure_never_signs(candidate, monkeypatch, failure):
    repo, task = candidate
    snapshots = []

    @contextmanager
    def prepare(task, cwd):
        with prepare_candidate(task, cwd) as snapshot:
            snapshots.append(snapshot)
            yield snapshot
        if failure == "cleanup":
            raise OSError("cleanup failed")

    def run(*args):
        if failure == "exception":
            raise RuntimeError("unexpected test fault")
        return verify_main_test_result()

    monkeypatch.chdir(repo)
    monkeypatch.setattr(verify_main, "prepare_candidate", prepare)
    monkeypatch.setattr(verify_main, "run_tests", run)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)
    assert all(not snapshot.exists() for snapshot in snapshots)


def test_committed_transport_refused(candidate, monkeypatch):
    repo, task = candidate
    candidate_git.git(repo, "add", ".omp/verify-task.json")
    candidate_git.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false", "commit", "-m", "committed transport")
    task["candidate_commit"] = candidate_git.git(repo, "rev-parse", "HEAD").decode().strip()
    task["candidate_sha256"] = candidate_git.audit_inputs(repo, task["base_commit"], task["candidate_commit"])["diff_sha256"]
    task["allowed_paths"] = ["payload", ".omp"]
    write_task(repo, task)
    monkeypatch.chdir(repo)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)
    assert "committed_transport" in (repo / ".omp/audit-report.txt").read_text()


@pytest.mark.parametrize("cli", [False, True])
def test_entry_missing_task_removes_stale_pass_without_loading_config(candidate, cli):
    repo, task = candidate
    plant_pass(repo, task)
    (repo / ".omp/verify-task.json").unlink()
    result = launch(repo, cli)
    assert result.returncode != 0
    assert result.stdout == b""
    assert not accepted(repo, task)
    assert "task_missing" in (repo / ".omp/audit-report.txt").read_text()


def test_success_cleans_snapshot_before_signing_and_publishes_report_first(candidate, monkeypatch):
    repo, task = candidate
    snapshots = []
    publications = []
    original_sign = verify_main._sign
    original_write = verify_main._atomic_write

    def run(command, snapshot, timeout):
        snapshots.append(snapshot)
        # Real frozen bytes and command execution; namespace isolation is tested above.
        result = subprocess.run(command, cwd=snapshot, capture_output=True, timeout=timeout)
        return CommandResult(result.returncode, result.stdout)

    def sign(task):
        assert snapshots and all(not snapshot.exists() for snapshot in snapshots)
        return original_sign(task)

    def publish(path, data):
        if path.name == "verdict.json":
            assert normalize_auditor_report((repo / ".omp/audit-report.txt").read_text())[1] == "PASS"
        original_write(path, data)
        publications.append(path.name)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(verify_main, "run_tests", run)
    monkeypatch.setattr(verify_main, "_sign", sign)
    monkeypatch.setattr(verify_main, "_atomic_write", publish)
    assert verify_main.main([]) == 0
    assert accepted(repo, task)
    assert publications == ["audit-report.txt", "verdict.json"]
    assert not accepted(repo, task, attempt_id=str(uuid4()))
    assert not accepted(repo, task, candidate_sha256="0" * 64)
    assert not accept_candidate(b"other", record(repo), candidate_sha256=task["candidate_sha256"], attempt_id=task["attempt_id"], producer_id="producer")


def test_exception_after_verdict_rename_removes_partial_pass(candidate, monkeypatch):
    repo, task = candidate
    original = verify_main._atomic_write

    def publish(path, data):
        original(path, data)
        if path.name == "verdict.json":
            raise RuntimeError("fault after rename")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(verify_main, "run_tests", lambda *args: verify_main_test_result())
    monkeypatch.setattr(verify_main, "_atomic_write", publish)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)
    assert normalize_auditor_report((repo / ".omp/audit-report.txt").read_text())[1] == "BLOCKED"


@pytest.mark.parametrize("failure", ["command", "timeout"])
def test_command_failure_after_preflight_discards_stale_pass(candidate, monkeypatch, failure):
    repo, task = candidate
    task["test_command"] = ["/bin/sh", "-c", "echo 'VERDICT: PASS'; exit 7"] if failure == "command" else ["/bin/sleep", "10"]
    task["timeout_seconds"] = 0.1
    write_task(repo, task)
    plant_pass(repo, task)

    def run(command, snapshot, timeout):
        result = subprocess.run(command, cwd=snapshot, capture_output=True, timeout=timeout)
        return CommandResult(result.returncode, result.stdout)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(verify_main, "run_tests", run)
    assert verify_main.main([]) != 0
    assert not accepted(repo, task)
    report = (repo / ".omp/audit-report.txt").read_text()
    assert normalize_auditor_report(report)[1] == "BLOCKED"
    assert ("tests_exit_7" if failure == "command" else "timeout") in report
