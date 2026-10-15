"""Host prerequisite check for the skip-accounting PostgreSQL alternate lane (OMP-617).

``check_prerequisites`` refuses (raises ``PrerequisiteError``, ``exit_code`` 2) unless the
host is a non-root Linux box with PostgreSQL 18 tooling, libseccomp, and working
unprivileged user/net/mount/pid namespaces. There is no fallback and no skip.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "NAMESPACE_PROBES",
    "PG_MAJOR",
    "PROBE_NAME",
    "REQUIRED_TOOLS",
    "ROOT_PREFIX",
    "STORAGE_MODE",
    "OwnedTemp",
    "PrerequisiteError",
    "check_prerequisites",
]



PG_MAJOR = 18
COMMAND_TIMEOUT_SECONDS = 5.0
EXIT_CODE = 2

ROOT_PREFIX = "omp-skip-pg-"
STORAGE_MODE = 0o755
PROBE_NAME = ".omp-storage-probe"


REQUIRED_TOOLS: tuple[str, ...] = (
    "initdb",
    "postgres",
    "pg_isready",
    "git",
    "ssh-keygen",
    "openssl",
    "visudo",
    "unshare",
    "setpriv",
    "ip",
    "gcc",
)
VERSIONED_TOOLS: tuple[str, ...] = ("initdb", "postgres", "pg_isready")

NAMESPACE_PROBES: tuple[tuple[str, ...], ...] = (
    ("unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"),
    ("unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--mount-proc", "true"),
    (
        "unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork",
        "unshare", "--pid", "--fork", "--mount-proc", "true",
    ),
)  # fmt: skip

_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)")


class PrerequisiteError(RuntimeError):
    """A host prerequisite is missing or broken. ``cause`` names which one."""

    def __init__(
        self,
        cause: str,
        message: str,
        *,
        argv: Sequence[str] | None = None,
        status: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(f"{cause}: {message}")
        self.cause = cause
        self.detail = message
        self.argv = tuple(argv) if argv is not None else None
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = EXIT_CODE


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _run(
    argv: Sequence[str],
    cause_prefix: str,
    run: Callable[..., Any],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run argv (no shell, bounded); raise a named cause on launch failure, timeout, or nonzero exit."""
    try:
        proc = run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PrerequisiteError(
            f"{cause_prefix}_timeout",
            f"{' '.join(argv)} timed out after {timeout}s",
            argv=argv,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        ) from exc
    except OSError as exc:
        raise PrerequisiteError(
            f"{cause_prefix}_launch_failed",
            f"{' '.join(argv)} could not start: {exc}",
            argv=argv,
            stderr=str(exc),
        ) from exc
    if proc.returncode != 0:
        stdout, stderr = _text(proc.stdout), _text(proc.stderr)
        raise PrerequisiteError(
            f"{cause_prefix}_failed",
            f"{' '.join(argv)} exited {proc.returncode}: {stderr.strip() or stdout.strip()}",
            argv=argv,
            status=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return proc


def check_prerequisites(
    *,
    platform: str | None = None,
    geteuid: Callable[[], int] | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., Any] | None = None,
    find_library: Callable[[str], str | None] | None = None,
    cdll: Callable[[str], Any] | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> None:
    """Raise ``PrerequisiteError`` unless every prerequisite holds. Returns None on success."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        raise PrerequisiteError("not_linux", f"requires Linux, found {platform!r}")
    euid = (geteuid or os.geteuid)()
    if euid == 0:
        raise PrerequisiteError("running_as_root", "must not run as root (euid 0)")

    which = which or shutil.which
    run = run or subprocess.run
    for tool in REQUIRED_TOOLS:
        if not which(tool):
            raise PrerequisiteError(f"missing_tool_{tool}", f"{tool} not found on PATH")

    for tool in VERSIONED_TOOLS:
        argv = (tool, "--version")
        proc = _run(argv, f"version_{tool}", run, timeout)
        stdout, stderr = _text(proc.stdout), _text(proc.stderr)
        match = _VERSION_RE.search(stdout)
        if match is None:
            raise PrerequisiteError(
                f"version_{tool}_unparseable",
                f"{tool} --version output not recognised: {stdout.strip()!r}",
                argv=argv,
                status=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if int(match.group(1)) != PG_MAJOR:
            raise PrerequisiteError(
                f"version_{tool}_wrong_major",
                f"{tool} major {match.group(1)}, need {PG_MAJOR}",
                argv=argv,
                status=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

    try:
        name = (find_library or ctypes.util.find_library)("seccomp")
    except Exception as exc:  # noqa: BLE001 - discovery failure is a named cause
        raise PrerequisiteError("libseccomp_discovery_failed", f"find_library raised: {exc}") from exc
    if not name:
        raise PrerequisiteError("libseccomp_missing", "libseccomp not found by ctypes.util.find_library")
    try:
        (cdll or ctypes.CDLL)(name)
    except OSError as exc:
        raise PrerequisiteError("libseccomp_load_failed", f"could not load {name}: {exc}", stderr=str(exc)) from exc

    for index, argv in enumerate(NAMESPACE_PROBES, start=1):
        _run(argv, f"namespace_probe_{index}", run, timeout)


def _stat_mode(path: Path) -> int:
    """Mode bits of ``path``. The default ancestor-searchability probe."""
    return path.stat().st_mode


def _resolve_path(path: Path) -> Path:
    """Resolve symlinks in ``path``."""
    return path.resolve()


def _storage_probe(
    directory: Path,
    *,
    write_bytes: Callable[[int, bytes], int] = os.write,
    remove_path: Callable[[Path], None] = os.remove,
) -> None:
    """Create, write, then remove ``PROBE_NAME`` inside ``directory`` to prove it stores."""
    probe = directory / PROBE_NAME
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        write_bytes(fd, b"omp-storage-probe\n")
    finally:
        os.close(fd)
    remove_path(probe)


def _remove_tree(path: Path) -> None:
    """Remove an owned root. A seam so rollback failures can be exercised."""
    shutil.rmtree(path)


def _operation_error(operation: str, path: Path, exc: OSError) -> PrerequisiteError:
    return PrerequisiteError(
        f"owned_temp_{operation}_failed",
        f"{operation} {path}: {exc}",
        stderr=str(exc),
    )


def _child_error(
    child: Any, operation: str, root: Path, exc: OSError | subprocess.TimeoutExpired
) -> PrerequisiteError:
    pid = getattr(child, "pid", None)
    label = f"child {child!r}" if pid is None else f"child pid {pid}"
    return PrerequisiteError(
        f"owned_temp_child_{operation}_failed",
        f"{operation} {label} while cleaning {root}: {exc}; storage retained",
        stderr=str(exc),
    )


def _end_child(child: Any, root: Path) -> None:
    """Terminate if live, then reap with bounded waits, escalating to kill on timeout."""
    wait_op = "wait"
    try:
        wait_op = "poll"
        if child.poll() is None:
            wait_op = "terminate"
            child.terminate()
        wait_op = "wait"
        child.wait(timeout=COMMAND_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError as exc:
        raise _child_error(child, wait_op, root, exc) from exc
    try:
        child.kill()
    except OSError as exc:
        raise _child_error(child, "kill", root, exc) from exc
    try:
        child.wait(timeout=COMMAND_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _child_error(child, "wait_after_kill", root, exc) from exc


def _require_searchable(root: Path, searchable: Callable[[Path], int]) -> None:
    """Raise unless every resolved ancestor of ``root`` grants other-search (o+x).

    Ancestors are never chmod'd; a private ancestor is reported with its mode so the
    caller fixes the host instead of this code silently widening permissions.
    """
    for ancestor in root.parents:
        try:
            mode = searchable(ancestor)
        except OSError as exc:
            raise PrerequisiteError(
                "owned_temp_ancestor_stat_failed",
                f"stat ancestor {ancestor}: {exc}",
                stderr=str(exc),
            ) from exc
        if not mode & stat.S_IXOTH:
            raise PrerequisiteError(
                "owned_temp_ancestor_not_searchable",
                f"ancestor {ancestor} is not other-searchable (mode 0o{stat.S_IMODE(mode):o})",
            )


def _rollback(path: Path, rmtree: Callable[[Path], None]) -> OSError | None:
    """Remove an allocated root during creation failure; return the error, if any."""
    try:
        rmtree(path)
    except OSError as exc:
        return exc
    return None


# Factory authority lives outside handle fields: construction, reinitialization and
# copying cannot register a root or restore a consumed entry. Paths are immutable.
_OWNED_ROOTS: WeakKeyDictionary[OwnedTemp, Path] = WeakKeyDictionary()


class OwnedTemp:
    """One storage root, owned only by the :meth:`create` that allocated it.

    ``tmpdir`` is exposed as ``TMPDIR`` through :meth:`env`; ``basetemp`` is the storage
    a later pytest invocation passes as ``--basetemp``. Factory alone grants ownership
    bound to the allocated root.
    """

    def __init__(
        self,
        root: Path,
        tmpdir: Path,
        basetemp: Path,
    ) -> None:
        self._root = Path(root)
        self._tmpdir = Path(tmpdir)
        self._basetemp = Path(basetemp)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def tmpdir(self) -> Path:
        return self._tmpdir

    @property
    def basetemp(self) -> Path:
        return self._basetemp

    @property
    def is_owned(self) -> bool:
        return self in _OWNED_ROOTS

    def __repr__(self) -> str:
        return f"OwnedTemp(root={self._root!r}, tmpdir={self._tmpdir!r}, basetemp={self._basetemp!r})"

    def __copy__(self) -> OwnedTemp:
        return OwnedTemp(self._root, self._tmpdir, self._basetemp)

    def __deepcopy__(self, memo: dict[int, Any]) -> OwnedTemp:
        return OwnedTemp(self._root, self._tmpdir, self._basetemp)

    @classmethod
    def create(
        cls,
        base: str | os.PathLike[str] = "/tmp",
        *,
        searchable: Callable[[Path], int] | None = None,
        resolve: Callable[[Path], Path] = _resolve_path,
        mkdtemp: Callable[..., str] = tempfile.mkdtemp,
        chmod: Callable[[os.PathLike[str], int], None] = os.chmod,
        mkdir: Callable[[os.PathLike[str], int], None] = os.mkdir,
        probe: Callable[[Path], None] = _storage_probe,
        rmtree: Callable[[Path], None] = _remove_tree,
    ) -> OwnedTemp:
        """Allocate and prepare owned storage, or raise ``PrerequisiteError``.

        The unique root is created under ``base``, chmod'd to 0755, and verified to sit
        below other-searchable ancestors. ``tmpdir`` and ``basetemp`` are created under it
        and a storage probe proves it writable. Any failure after allocation removes the
        root again, preserving the original cause and surfacing a rollback failure.
        """
        resolve_searchable = _stat_mode if searchable is None else searchable
        base_path = Path(base)
        try:
            raw_root_str = mkdtemp(prefix=ROOT_PREFIX, dir=str(base_path))
        except OSError as exc:
            raise _operation_error("mkdtemp", base_path, exc) from exc

        raw_root = Path(raw_root_str)
        try:
            try:
                root = resolve(raw_root)
            except OSError as exc:
                raise _operation_error("resolve", raw_root, exc) from exc
            try:
                chmod(root, STORAGE_MODE)
            except OSError as exc:
                raise _operation_error("chmod", root, exc) from exc
            _require_searchable(root, resolve_searchable)
            tmpdir = root / "tmp"
            basetemp = root / "basetemp"
            for target in (tmpdir, basetemp):
                try:
                    mkdir(target, STORAGE_MODE)
                except OSError as exc:
                    raise _operation_error("mkdir", target, exc) from exc
            try:
                probe(root)
            except OSError as exc:
                raise _operation_error("probe", root, exc) from exc
        except BaseException as exc:
            failure = _rollback(raw_root, rmtree)
            if failure is not None:
                raise PrerequisiteError(
                    "owned_temp_rollback_failed",
                    f"{exc}; rollback of {raw_root} failed: {failure}",
                    stderr=str(failure),
                ) from exc
            raise
        owned = cls(root=root, tmpdir=tmpdir, basetemp=basetemp)
        _OWNED_ROOTS[owned] = root
        return owned

    def cleanup(
        self,
        children: Sequence[Any] = (),
        *,
        rmtree: Callable[[Path], None] = _remove_tree,
    ) -> None:
        """Remove the allocated root once; unowned or already-consumed handles do nothing.

        ``children`` are Popen-like direct children using this storage. Each is terminated
        if live (killed after ``COMMAND_TIMEOUT_SECONDS``) and reaped, exited ones too.
        Any child failure raises ``PrerequisiteError`` and retains storage and authority;
        removal happens only after every child has ended.

        Authority is the factory allocation, never this handle's path fields. A root that
        is already absent counts as removed. Real removal errors raise
        ``PrerequisiteError`` and leave authority intact for a retry.
        """
        root = _OWNED_ROOTS.get(self)
        if root is None:
            return
        first_failure: PrerequisiteError | None = None
        for child in children:
            try:
                _end_child(child, root)
            except PrerequisiteError as exc:
                if first_failure is None:
                    first_failure = exc
        if first_failure is not None:
            raise first_failure
        try:
            rmtree(root)
        except OSError as exc:
            try:
                root.lstat()
            except FileNotFoundError:
                del _OWNED_ROOTS[self]
                return
            except OSError:
                # Inspection failure does not establish absence. Preserve removal's
                # original OS error, including its exception chain.
                pass
            raise _operation_error("remove", root, exc) from exc
        del _OWNED_ROOTS[self]

    def env(self) -> dict[str, str]:
        """Environment exposing this root's ``TMPDIR``; merge into a spawned child's env."""
        return {"TMPDIR": str(self._tmpdir)}
