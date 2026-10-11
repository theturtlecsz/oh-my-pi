"""Verifier launch validates metadata before granting read-only Git mounts."""

import os
from pathlib import Path
import subprocess

import pytest

from omp_work.orchestrator import worker_sandbox


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "jobs"
    root.mkdir()
    job = root / "job"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    for args in (
        ("init", "-q"),
        ("commit", "-q", "--allow-empty", "-m", "init"),
        ("worktree", "add", "--quiet", "-b", "job", str(job)),
    ):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    gitdir = Path((job / ".git").read_text().split(":", 1)[1].strip())
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", spy)

    def run(**options):
        return worker_sandbox.run_worker(
            ["/usr/bin/true"], worktree=job, token="task-token",
            workservice_socket=("127.0.0.1", 8000), timeout=5,
            **{"identity": "verifier", **options},
        )

    return repo, job, gitdir, calls, run


@pytest.mark.parametrize("relative_gitdir", [False, True])
def test_audit_launch_mounts_resolved_git_metadata(launch, relative_gitdir) -> None:
    repo, job, gitdir, calls, run = launch
    audit = job / ".omp"
    audit.mkdir()
    if relative_gitdir:
        (job / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, job)}\n")
        (gitdir / "commondir").write_text("../..\n")

    assert run() == 0
    args, kwargs = calls[0]
    assert kwargs["ro_dirs"] == ((repo / ".git").resolve(), gitdir)
    assert kwargs["worktree_readonly"] is True
    assert kwargs["audit_dir"] == audit
    assert "verifier_runtime" not in kwargs
    assert set(args[4]) == {"PATH", "LANG", "TERM", "TZ", "OMP_TASK_TOKEN", "OMP_WORK_URL"}


def test_no_audit_keeps_original_mount_options_and_environment(launch) -> None:
    _repo, _job, _gitdir, calls, run = launch
    assert run() == 0
    args, kwargs = calls[0]
    assert {"worktree_readonly", "ro_dirs", "audit_dir", "verifier_runtime"}.isdisjoint(kwargs)
    assert set(args[4]) == {"PATH", "LANG", "TERM", "TZ", "OMP_TASK_TOKEN", "OMP_WORK_URL"}


@pytest.mark.parametrize("audit", [False, True])
@pytest.mark.parametrize("invalid", ["outside", "parent", "empty", "missing", "root"])
def test_invalid_commondir_refused_even_without_audit(launch, audit, invalid) -> None:
    _repo, job, gitdir, calls, run = launch
    if audit:
        (job / ".omp").mkdir()
    common = gitdir / "commondir"
    if invalid == "missing":
        common.unlink()
    else:
        common.write_text({
            "outside": "/not-allowed\n", "parent": f"{gitdir.parent}\n",
            "empty": " \n", "root": "/\n",
        }[invalid])
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        run()
    assert exc.value.code == "commondir_not_allowed"
    assert calls == []
    assert not (job / ".omp" / "setup_ok").exists()


@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "file"])
def test_invalid_audit_refused_before_sandbox_setup(launch, kind) -> None:
    repo, job, _gitdir, calls, run = launch
    audit = job / ".omp"
    if kind == "file":
        audit.write_text("not a directory")
    else:
        audit.symlink_to(repo if kind == "symlink" else repo / "missing", target_is_directory=True)
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        run()
    assert exc.value.code == "audit_dir_not_allowed"
    assert calls == []
    assert not (audit / "setup_ok").exists()


def test_worker_does_not_validate_or_mount_verifier_paths(launch) -> None:
    _repo, job, gitdir, calls, run = launch
    (gitdir / "commondir").unlink()
    (job / ".omp").write_text("worker file")
    assert run(identity="worker") == 0
    assert {"worktree_readonly", "ro_dirs", "audit_dir"}.isdisjoint(calls[0][1])


@pytest.mark.parametrize("field", ["model_proxy", "model_token", "manifest_path"])
def test_verifier_model_fields_refused_before_path_validation(launch, field) -> None:
    _repo, job, _gitdir, calls, run = launch
    (job / ".git").unlink()
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        run(**{field: "forbidden"})
    assert exc.value.code == "identity_not_allowed"
    assert calls == []
