"""Run verifier tests against a frozen snapshot in the worker jail."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from pathlib import Path

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import run_sandboxed

_IDENTITY = Identity(
    workspace_id="worker",
    project_id="verifier",
    mission_id="verifier",
    worker_id="verifier",
    stage="repository",
)


@dataclass(frozen=True)
class TestResult:
    """Exit status and at most 1 MiB of combined test output."""

    exit_code: int
    output: bytes


def run_tests(
    command: Sequence[str], snapshot: Path, timeout_seconds: int | float
) -> TestResult:
    """Run argv with a closed environment and a read-only snapshot.

    Invalid input raises ValueError before sandbox setup. SandboxUnavailable
    and subprocess.TimeoutExpired propagate without an unsandboxed retry.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or (isinstance(timeout_seconds, float) and not math.isfinite(timeout_seconds))
    ):
        raise ValueError("timeout_seconds must be a finite positive number")
    if (
        isinstance(command, (str, bytes))
        or not isinstance(command, Sequence)
        or not command
        or any(not isinstance(arg, str) or not arg for arg in command)
    ):
        raise ValueError("command must be a non-empty sequence of non-empty strings")
    if (
        not isinstance(snapshot, Path)
        or not snapshot.is_absolute()
        or not snapshot.is_dir()
        or snapshot.resolve() == Path(snapshot.anchor)
        or (snapshot / ".git").exists()
        or (snapshot / ".git").is_symlink()
    ):
        raise ValueError("snapshot must be an absolute non-root directory without .git")

    # run_sandboxed bounds this buffer to 1 MiB while draining worker output.
    output = bytearray()
    exit_code = run_sandboxed(
        list(command),
        _IDENTITY,
        MemoryRecorder(),
        "/work",
        {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "dumb", "TZ": "UTC"},
        timeout_seconds,
        root="worker",
        worktree=snapshot,
        output=output,
        worktree_readonly=True,
    )
    return TestResult(exit_code=exit_code, output=bytes(output))
