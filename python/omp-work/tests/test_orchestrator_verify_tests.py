"""Verifier input refusals, sandbox isolation, output, and failure contracts."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.orchestrator import verify_tests


def test_result_is_immutable() -> None:
    result = verify_tests.TestResult(exit_code=1, output=b"failed")
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 0
    with pytest.raises(FrozenInstanceError):
        result.output = b"passed"


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Mock:
    runner = Mock()
    monkeypatch.setattr(verify_tests, "run_sandboxed", runner)
    return runner


@pytest.mark.parametrize(
    "timeout", [0, -1, -0.5, float("nan"), float("inf"), -float("inf"), "1", None, True, False]
)
def test_invalid_timeout_refused_before_execution(
    tmp_path: Path, sandbox: Mock, timeout: object
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        verify_tests.run_tests(["pytest"], tmp_path, timeout)
    sandbox.assert_not_called()


@pytest.mark.parametrize(
    "command", [[], (), "pytest", b"pytest", None, 1, [1], ["pytest", ""], [""], {"pytest"}]
)
def test_invalid_argv_refused_before_execution(
    tmp_path: Path, sandbox: Mock, command: object
) -> None:
    with pytest.raises(ValueError, match="command"):
        verify_tests.run_tests(command, tmp_path, 1)
    sandbox.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["missing", "relative", "root", "root_alias", "file", "string", "git_dir", "git_file", "git_link"]
)
def test_invalid_snapshot_refused_before_execution(
    tmp_path: Path, sandbox: Mock, kind: str
) -> None:
    snapshot = tmp_path
    if kind == "missing":
        snapshot = tmp_path / "missing"
    elif kind == "relative":
        snapshot = Path(".")
    elif kind == "root":
        snapshot = Path(tmp_path.anchor)
    elif kind == "root_alias":
        snapshot = tmp_path.joinpath(*([".."] * len(tmp_path.parts[1:])))
    elif kind == "file":
        snapshot = tmp_path / "file"
        snapshot.write_bytes(b"not a directory")
    elif kind == "string":
        snapshot = str(tmp_path)
    elif kind == "git_dir":
        (tmp_path / ".git").mkdir()
    elif kind == "git_file":
        (tmp_path / ".git").write_text("gitdir: /producer/control.git")
    elif kind == "git_link":
        (tmp_path / ".git").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="snapshot"):
        verify_tests.run_tests(["pytest"], snapshot, 1)
    sandbox.assert_not_called()


@pytest.mark.parametrize("timeout", [1, 0.25])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_runs_argv_in_readonly_worker_with_closed_environment_and_captured_output(
    tmp_path: Path, sandbox: Mock, monkeypatch: pytest.MonkeyPatch,
    timeout: int | float, exit_code: int,
) -> None:
    monkeypatch.setenv("VERIFIER_SIGNING_KEY", "must-not-enter-jail")
    captured = b"stdout\n\xffstderr\n"

    def run(*args: object, **kwargs: object) -> int:
        output = kwargs["output"]
        assert isinstance(output, bytearray)
        output.extend(captured)
        return exit_code

    sandbox.side_effect = run
    command = ("pytest", "tests/test_feature.py")
    result = verify_tests.run_tests(command, tmp_path, timeout)
    args, kwargs = sandbox.call_args
    assert args[0] == list(command)
    assert isinstance(args[0], list)
    assert args[1] == Identity("worker", "verifier", "verifier", "verifier", "repository")
    assert isinstance(args[2], MemoryRecorder)
    assert args[3:] == (
        "/work",
        {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "dumb", "TZ": "UTC"},
        timeout,
    )
    assert kwargs == {
        "root": "worker", "worktree": tmp_path,
        "output": bytearray(captured), "worktree_readonly": True,
    }
    assert sandbox.call_count == 1
    assert result.exit_code == exit_code
    assert result.output == captured
    assert isinstance(result.output, bytes)


@pytest.mark.parametrize(
    "error", [SandboxUnavailable("setup failed"), subprocess.TimeoutExpired(["pytest"], 1)]
)
def test_sandbox_failure_propagates_without_retry(
    tmp_path: Path, sandbox: Mock, error: Exception
) -> None:
    sandbox.side_effect = error
    with pytest.raises(type(error)) as raised:
        verify_tests.run_tests(["pytest"], tmp_path, 1)
    assert raised.value is error
    assert sandbox.call_count == 1
