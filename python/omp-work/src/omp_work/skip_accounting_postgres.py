"""Host prerequisite check for the skip-accounting PostgreSQL alternate lane (OMP-617).

``check_prerequisites`` refuses (raises ``PrerequisiteError``, ``exit_code`` 2) unless the
host is a non-root Linux box with PostgreSQL 18 tooling, libseccomp, and working
unprivileged user/net/mount/pid namespaces. There is no fallback and no skip.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
import ctypes.util
import os
import re
import shutil
import subprocess
import sys
from typing import Any

__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "NAMESPACE_PROBES",
    "PG_MAJOR",
    "PrerequisiteError",
    "REQUIRED_TOOLS",
    "check_prerequisites",
]

PG_MAJOR = 18
COMMAND_TIMEOUT_SECONDS = 5.0
EXIT_CODE = 2

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
