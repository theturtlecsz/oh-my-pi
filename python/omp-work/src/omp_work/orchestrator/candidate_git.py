"""Hardened git runner and candidate operations (OMP-417)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import fnmatch
import hashlib
import os
from pathlib import Path
import stat
import subprocess  # nosec B404 - git argv lists only; executable is the literal "git", no shell
import tempfile
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CandidateGitError",
    "IntentLog",
    "add_worktree",
    "audit_inputs",
    "check_envelope",
    "freeze",
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
    input: bytes | str | None = None,  # pylint: disable=redefined-builtin
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

        proc = subprocess.run(  # nosec B603 - argv starts with the literal "git", no shell; remaining args and stdin are data, not a shell command
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


def _raise_walk_error(error: OSError) -> None:
    """Fail closed: an unreadable directory must abort the scan, not vanish."""
    raise error


def _resolves_inside(path: Path, root: Path) -> bool:
    """Return True only when ``path`` resolves to a location inside ``root``.

    Broken links and resolve failures count as escaping (fail closed).
    """
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _snapshot_blob(repo: Path, content_bytes: bytes) -> str:
    """Hash content in the control repo without writing the blob (``-w`` omitted)."""
    return git(repo, "hash-object", "--no-filters", "--stdin", input=content_bytes).decode("ascii").strip()


def _record_symlink(
    full: Path,
    rel: str,
    *,
    wt: Path,
    repo: Path,
    in_git_metadata: bool,
    violations: list[str],
    wt_files: dict[str, tuple[str, str, bytes]],
    seen_paths: set[str],
) -> None:
    """Record one symlink: escape check, git-metadata check, else 120000 blob.

    ``os.readlink`` text is re-encoded with :func:`os.fsencode`, which round-trips
    the exact filesystem bytes (surrogateescape), so non-UTF-8 targets neither
    crash the scan nor get rewritten. Read errors propagate (fail closed).
    """
    if not _resolves_inside(full, wt):
        violations.append(f"symlink_escape:{rel}")
    if in_git_metadata:
        violations.append(f"git_metadata:{rel}")
        return
    content_bytes = os.fsencode(os.readlink(full))
    wt_files[rel] = ("120000", _snapshot_blob(repo, content_bytes), content_bytes)
    seen_paths.add(rel)


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

    for root, dirs, files in os.walk(wt, followlinks=False, onerror=_raise_walk_error):
        for d in list(dirs):
            full_d = Path(root) / d
            rel_d = full_d.relative_to(wt).as_posix()

            if os.path.islink(full_d):
                _record_symlink(
                    full_d,
                    rel_d,
                    wt=wt,
                    repo=repo,
                    in_git_metadata=any(part == ".git" for part in _segments(rel_d)),
                    violations=violations,
                    wt_files=wt_files,
                    seen_paths=seen_paths,
                )
            elif any(part == ".git" for part in _segments(rel_d)):
                violations.append(f"git_metadata:{rel_d}")

        for f in files:
            full_f = Path(root) / f
            rel_f = full_f.relative_to(wt).as_posix()
            f_parts = _segments(rel_f)

            st = os.lstat(full_f)

            if stat.S_ISLNK(st.st_mode):
                _record_symlink(
                    full_f,
                    rel_f,
                    wt=wt,
                    repo=repo,
                    in_git_metadata=any(part == ".git" for part in f_parts),
                    violations=violations,
                    wt_files=wt_files,
                    seen_paths=seen_paths,
                )

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
                is_root_gitfile = rel_f == ".git"
                if st.st_nlink > 1:
                    violations.append(f"hardlink:{rel_f}")

                if any(part == ".git" for part in f_parts):
                    if not is_root_gitfile:
                        violations.append(f"git_metadata:{rel_f}")
                else:
                    content_bytes = full_f.read_bytes()
                    mode = "100755" if bool(st.st_mode & 0o111) else "100644"
                    wt_files[rel_f] = (mode, _snapshot_blob(repo, content_bytes), content_bytes)
                    seen_paths.add(rel_f)

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


def _split_author(author: str) -> tuple[str, str]:
    """Split ``Name <email>`` into the name and email ident fields."""
    name, sep, rest = author.rpartition(" <")
    if sep == "" or not rest.endswith(">"):
        raise CandidateGitError("git_failed")
    email = rest[:-1]
    if name == "" or email == "":
        raise CandidateGitError("git_failed")
    return name, email


def _write_scanned_blob(repo: Path, content: bytes) -> str:
    """Store ``content`` with ``hash-object -w --no-filters`` and require the scanned id."""
    scanned_id = _snapshot_blob(repo, content)
    written_id = git(
        repo,
        "hash-object",
        "-w",
        "--no-filters",
        "--stdin",
        input=content,
    ).decode("ascii").strip()
    if written_id != scanned_id:
        raise CandidateGitError("blob_mismatch")
    return written_id


def _index_tree(
    repo: Path,
    base: str,
    changed: Mapping[str, tuple[str, bytes] | None],
) -> str:
    """Build ``base`` plus ``changed`` in a temporary index and return the tree id.

    ``--force-remove`` refuses to run in a bare repository. Its work tree is an
    empty directory, never the candidate checkout: that checkout's gitfile may
    name an attacker gitdir. Deletions run before additions so directory-to-file
    and file-to-directory replacements do not conflict with stale index entries.
    """
    with tempfile.TemporaryDirectory(prefix="omp-candidate-index-") as tmp:
        index_path = Path(tmp) / "index"
        empty_worktree = Path(tmp) / "empty"
        empty_worktree.mkdir()
        git(repo, "read-tree", base, index_file=index_path)
        deletions = [p for p in sorted(changed) if changed[p] is None]
        additions = [p for p in sorted(changed) if changed[p] is not None]

        for path in deletions:
            git(
                repo,
                "update-index",
                "--force-remove",
                "--",
                path,
                index_file=index_path,
                extra_env={"GIT_WORK_TREE": str(empty_worktree)},
            )
        for path in additions:
            entry = changed[path]
            assert entry is not None
            mode, content = entry
            blob = _write_scanned_blob(repo, content)
            git(
                repo,
                "update-index",
                "--add",
                "--replace",
                "--cacheinfo",
                f"{mode},{blob},{path}",
                index_file=index_path,
            )
        return git(repo, "write-tree", index_file=index_path).decode("ascii").strip()


def freeze(
    control_repo: Path | str,
    worktree: Path | str,
    base: str,
    allowed_paths: Iterable[str],
    *,
    repository: Path | str,
    live_checkout: Path | str | None,
    message: str,
    author: str,
) -> str:
    """Commit the scanned checkout onto ``base`` and return the new commit sha.

    ``repository`` must be the same directory as ``control_repo``. One scan
    decides the envelope; any violation writes nothing. Blobs are the scanned
    bytes (``hash-object -w --no-filters --stdin``), parent is ``base``, and
    author and committer are both ``author`` with the base committer date.
    No ref moves, and no git command runs in the candidate worktree.
    """
    if os.path.realpath(control_repo) != os.path.realpath(repository):
        raise CandidateGitError("repository_mismatch")

    violations, changed = _scan(
        worktree,
        base,
        allowed_paths,
        repository=repository,
        live_checkout=live_checkout,
    )
    if violations:
        raise CandidateGitError("envelope_violation", violations)

    repo = Path(control_repo)
    tree = _index_tree(repo, base, changed)
    name, email = _split_author(author)
    date = git(repo, "log", "-1", "--format=%cI", base).decode("ascii").strip()
    commit = git(
        repo,
        "-c",
        "commit.gpgSign=false",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        message,
        extra_env={
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": date,
        },
    )
    return commit.decode("ascii").strip()


def audit_inputs(control_repo: Path | str, base: str, commit: str) -> dict[str, Any]:
    """Return the stable diff digest, tree sha, and sorted paths for ``commit``."""
    diff_bytes = git(
        control_repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        base,
        commit,
    )
    names = git(
        control_repo,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base,
        commit,
    )
    changed_paths = sorted(
        part.decode("utf-8", errors="surrogateescape")
        for part in names.split(b"\0")
        if part
    )
    tree = git(control_repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode("ascii").strip()
    return {
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "candidate_tree_sha": tree,
        "changed_paths": changed_paths,
    }

