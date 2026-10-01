"""Hardened git runner and candidate operations (OMP-417)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import fnmatch
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CandidateGitError",
    "IntentLog",
    "add_worktree",
    "git",
    "path_allowed",
]


class CandidateGitError(RuntimeError):
    """Raised when candidate git operations fail or are refused."""

    def __init__(self, code: str, violations: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.violations: list[str] = list(violations) if violations is not None else []


@runtime_checkable
class IntentLog(Protocol):
    """Protocol for recording intent before external push/merge side effects."""

    def record_intent(
        self,
        key: str,
        action: str,
        target: Any,
    ) -> None: ...

    def mark_done(
        self,
        key: str,
        ref: str,
    ) -> None: ...

    def open(
        self,
        key: str,
    ) -> dict[str, Any] | None: ...


def git(
    repo: Path | str,
    *args: str,
    input: bytes | str | None = None,
    index_file: Path | str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> bytes:
    """Run a hardened git command isolated from system/user configs, hooks, and fsmonitor."""
    cmd = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        *args,
    ]

    with tempfile.TemporaryDirectory(prefix="omp-git-home-") as tmp_home:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "HOME": tmp_home,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if index_file is not None:
            env["GIT_INDEX_FILE"] = str(index_file)
        if extra_env:
            env.update(extra_env)

        input_bytes: bytes | None
        if isinstance(input, str):
            input_bytes = input.encode("utf-8")
        else:
            input_bytes = input

        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        if proc.returncode != 0:
            raise CandidateGitError("git_failed")

        return proc.stdout


def add_worktree(
    control_repo: Path | str,
    worktrees_dir: Path | str,
    name: str,
    base: str,
) -> Path:
    """Create a detached worktree from control_repo at worktrees_dir / name."""
    ctrl = Path(control_repo)
    dest = Path(worktrees_dir) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(ctrl, "worktree", "add", "--detach", str(dest), str(base))
    return dest


def _match_segments(path_segments: list[str], pat_segments: list[str]) -> bool:
    n = len(path_segments)
    m = len(pat_segments)
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[n][m] = True

    for j in range(m - 1, -1, -1):
        if pat_segments[j] == "**":
            dp[n][j] = dp[n][j + 1]
        else:
            break

    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if pat_segments[j] == "**":
                dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
            else:
                if fnmatch.fnmatchcase(path_segments[i], pat_segments[j]):
                    dp[i][j] = dp[i + 1][j + 1]
                else:
                    dp[i][j] = False

    return dp[0][0]


def _segments(value: str) -> list[str]:
    """Split a POSIX path or glob on ``/``, dropping empty and ``.`` segments."""
    return [s for s in value.split("/") if s and s != "."]


def path_allowed(path: str | Path, allowed_paths: Iterable[str]) -> bool:
    """Check if path matches any segment glob pattern in allowed_paths.

    The path is matched verbatim: whitespace and backslashes are preserved, so
    ``" README"``/``"README "`` do not match ``README`` and a literal POSIX
    filename ``src\\a.py`` does not match ``src/*``. Globs match per segment:
    ``*``, ``?`` and ``[..]`` stay within one segment, and only an explicit
    ``**`` segment matches zero or more segments. Nothing matches by prefix,
    and a trailing slash does not imply recursion.
    """
    path_segments = _segments(str(path))

    for pattern in allowed_paths:
        if _match_segments(path_segments, _segments(pattern)):
            return True
    return False
