"""Hardened git runner and candidate operations (OMP-417)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import fnmatch
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CandidateGitError",
    "IntentLog",
    "add_worktree",
    "check_envelope",
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


def check_envelope(
    worktree: Path | str,
    base: str,
    allowed_paths: Iterable[str],
    *,
    repository: Path | str,
    live_checkout: Path | str | None = None,
) -> list[str]:
    """Check candidate worktree against envelope constraints (OMP-417-s04-s02)."""
    violations, _ = _scan(
        worktree,
        base,
        allowed_paths,
        repository=repository,
        live_checkout=live_checkout,
    )
    return violations


def _scan(
    worktree: Path | str,
    base: str,
    allowed_paths: Iterable[str],
    *,
    repository: Path | str,
    live_checkout: Path | str | None = None,
) -> tuple[list[str], dict[str, tuple[str, bytes] | None]]:
    """Scan candidate worktree for envelope violations and compute changed file snapshot."""
    wt = Path(worktree).resolve()
    repo = Path(repository).resolve()

    violations: list[str] = []

    # 1. Base ls-tree from bare control repo
    base_out = git(repo, "ls-tree", "-r", "-z", "--full-tree", str(base))
    base_entries: dict[str, tuple[str, str]] = {}
    if base_out:
        for item in base_out.split(b"\0"):
            if not item:
                continue
            meta, path_b = item.split(b"\t", 1)
            mode_b, _type_b, sha_b = meta.split(b" ", 2)
            base_entries[path_b.decode("utf-8", errors="surrogateescape")] = (
                mode_b.decode("ascii"),
                sha_b.decode("ascii"),
            )

    # 2. Live checkout check
    if live_checkout is not None:
        live = Path(live_checkout).resolve()
        live_out = git(
            live,
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=traditional",
        )
        if live_out:
            for entry in live_out.split(b"\0"):
                if entry:
                    violations.append(
                        f"live_checkout_changed:{entry.decode('utf-8', errors='surrogateescape')}"
                    )

    # 3. Walk worktree without pruning
    wt_files: dict[str, tuple[str, str, bytes]] = {}
    seen_paths: set[str] = set()

    for root, dirs, files in os.walk(wt, followlinks=False):
        for d in list(dirs):
            full_d = Path(root) / d
            rel_d = full_d.relative_to(wt).as_posix()
            d_parts = _segments(rel_d)

            if os.path.islink(full_d):
                try:
                    target = full_d.resolve()
                    if not target.is_relative_to(wt):
                        violations.append(f"symlink_escape:{rel_d}")
                except (OSError, RuntimeError):
                    violations.append(f"symlink_escape:{rel_d}")

                if any(part == ".git" for part in d_parts):
                    violations.append(f"git_metadata:{rel_d}")
                else:
                    try:
                        link_text = os.readlink(full_d)
                        content_bytes = link_text.encode("utf-8")
                        mode = "120000"
                        sha = git(
                            repo,
                            "hash-object",
                            "--no-filters",
                            "--stdin",
                            input=content_bytes,
                        ).decode("ascii").strip()
                        wt_files[rel_d] = (mode, sha, content_bytes)
                        seen_paths.add(rel_d)
                    except OSError:
                        pass
            else:
                if any(part == ".git" for part in d_parts):
                    violations.append(f"git_metadata:{rel_d}")

        for f in files:
            full_f = Path(root) / f
            rel_f = full_f.relative_to(wt).as_posix()
            f_parts = _segments(rel_f)

            try:
                st = os.lstat(full_f)
            except OSError:
                continue

            if stat.S_ISLNK(st.st_mode):
                try:
                    target = full_f.resolve()
                    if not target.is_relative_to(wt):
                        violations.append(f"symlink_escape:{rel_f}")
                except (OSError, RuntimeError):
                    violations.append(f"symlink_escape:{rel_f}")

                if any(part == ".git" for part in f_parts):
                    violations.append(f"git_metadata:{rel_f}")
                else:
                    try:
                        link_text = os.readlink(full_f)
                        content_bytes = link_text.encode("utf-8")
                        mode = "120000"
                        sha = git(
                            repo,
                            "hash-object",
                            "--no-filters",
                            "--stdin",
                            input=content_bytes,
                        ).decode("ascii").strip()
                        wt_files[rel_f] = (mode, sha, content_bytes)
                        seen_paths.add(rel_f)
                    except OSError:
                        pass

            elif (
                stat.S_ISFIFO(st.st_mode)
                or stat.S_ISSOCK(st.st_mode)
                or stat.S_ISCHR(st.st_mode)
                or stat.S_ISBLK(st.st_mode)
            ):
                violations.append(f"unsupported:{rel_f}")
                if any(part == ".git" for part in f_parts):
                    violations.append(f"git_metadata:{rel_f}")

            elif stat.S_ISREG(st.st_mode):
                is_root_gitfile = (rel_f == ".git")
                if st.st_nlink > 1:
                    violations.append(f"hardlink:{rel_f}")

                if any(part == ".git" for part in f_parts):
                    if not is_root_gitfile:
                        violations.append(f"git_metadata:{rel_f}")
                else:
                    try:
                        content_bytes = full_f.read_bytes()
                        mode = "100755" if bool(st.st_mode & 0o111) else "100644"
                        sha = git(
                            repo,
                            "hash-object",
                            "--no-filters",
                            "--stdin",
                            input=content_bytes,
                        ).decode("ascii").strip()
                        wt_files[rel_f] = (mode, sha, content_bytes)
                        seen_paths.add(rel_f)
                    except OSError:
                        pass

    # 4. Check changed paths relative to base
    changed_map: dict[str, tuple[str, bytes] | None] = {}

    for p, (mode, sha, content_bytes) in wt_files.items():
        if p not in base_entries:
            changed_map[p] = (mode, content_bytes)
            if not path_allowed(p, allowed_paths):
                violations.append(f"outside_allowed:{p}")
        else:
            base_mode, base_sha = base_entries[p]
            if mode != base_mode or sha != base_sha:
                changed_map[p] = (mode, content_bytes)
                if not path_allowed(p, allowed_paths):
                    violations.append(f"outside_allowed:{p}")

    for p in base_entries:
        if p not in seen_paths:
            changed_map[p] = None
            if not path_allowed(p, allowed_paths):
                violations.append(f"outside_allowed:{p}")

    sorted_violations = sorted(set(violations))
    return sorted_violations, changed_map

