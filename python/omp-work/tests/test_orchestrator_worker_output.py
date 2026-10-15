"""OMP-607: run_worker forwards ``output`` to the jail's bounded capture.

Captured stdout is returned for success and failure exits, a caller buffer's
earlier bytes stay, and without ``output`` the worker's stdout is inherited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omp_work.orchestrator.worker_sandbox import run_worker
from test_orchestrator_worker_sandbox import (
    _TOKEN,
    _WorkServiceStub,
    _git,
    _unshare_probe_fails,
)

pytestmark = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "worktrees"
    root.mkdir()
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    linked = root / "job"
    _git(repo, "worktree", "add", "--quiet", str(linked))
    service = _WorkServiceStub()
    service.start()
    try:
        yield linked, service
    finally:
        service.close()


def _run(linked: Path, service: _WorkServiceStub, script: str, **kwargs: object) -> int:
    return run_worker(
        ["/bin/sh", "-c", script],
        worktree=linked,
        token=_TOKEN,
        workservice_socket=("127.0.0.1", service.port),
        identity="worker",
        timeout=30,
        **kwargs,
    )


@pytest.mark.parametrize("status", [0, 1])
def test_stdout_captured_for_exit_status(job, status: int, capfd: pytest.CaptureFixture[str]) -> None:
    linked, service = job
    output = bytearray()
    assert _run(linked, service, f"echo delivered-{status}; exit {status}", output=output) == status
    assert b"delivered-%d" % status in bytes(output)
    assert "delivered-" not in capfd.readouterr().out


def test_caller_buffer_retained(job) -> None:
    linked, service = job
    output = bytearray(b"prior|")
    assert _run(linked, service, "echo appended", output=output) == 0
    assert bytes(output).startswith(b"prior|")
    assert b"appended" in bytes(output)


def test_omitted_output_inherits_stdout(job, capfd: pytest.CaptureFixture[str]) -> None:
    linked, service = job
    assert _run(linked, service, "echo inherited-line") == 0
    assert "inherited-line" in capfd.readouterr().out
