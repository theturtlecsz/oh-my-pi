"""Local research runner on the egress worker jail (OMP-315 R05).

:class:`LocalJailBackend` runs a request in the existing ``worker`` jail
(``run_sandboxed`` with ``root="worker"``). The working directory is the
caller's workdir, mounted read-write at ``/work``. Named files are read-only
binds. The worker environment is only ``PATH``, ``LANG``, ``TERM``, and
``TZ``. GPU, instrument, and remote backends are refused before any process
starts. A recorded manifest is the backend name, kernel, machine, and the
sha256 of each executable the request names under ``/usr/bin`` or ``/bin``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import run_sandboxed
from omp_work.v1.canonical import sha256

__all__ = [
    "Backend",
    "BackendIncompatible",
    "LocalJailBackend",
    "RemoteRunner",
    "RunRequest",
    "RunResult",
    "check_environment",
    "resolve_backend",
]

_ALLOWED_ENV = frozenset({"PATH", "LANG", "TERM", "TZ"})
_BIN_DIRS = ("/usr/bin", "/bin")
_OUTPUT_LIMIT = 1 << 20
_LOCAL_NAME = "local-jail.v1"
_LOCAL_CAPABILITIES = frozenset({"cpu"})

RunStatus = Literal["completed", "crashed", "timed_out", "canceled"]

_IDENTITY = Identity(
    workspace_id="research-runner",
    project_id="local-jail",
    mission_id=None,
    worker_id="local-jail",
    stage="repository",
)


class BackendIncompatible(Exception):
    """Raised before any process when this machine cannot run the request.

    ``code`` is ``backend_incompatible`` or ``missing_dependency:<name>``.
    """

    code: str

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RunRequest:
    """One sandbox run. ``env`` may name only ``PATH``, ``LANG``, ``TERM``, and ``TZ``."""

    argv: tuple[str, ...]
    workdir: str
    ro_files: tuple[str, ...]
    env: dict[str, str]
    timeout: float | None
    requires: tuple[str, ...]
    capabilities: frozenset[str]

    def __init__(
        self,
        argv: Sequence[str],
        workdir: str | Path,
        ro_files: Sequence[str | Path] = (),
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        requires: Sequence[str] = (),
        capabilities: Iterable[str] | None = None,
    ) -> None:
        supplied = {} if env is None else env
        for name in supplied:
            if name not in _ALLOWED_ENV:
                raise ValueError("env_not_allowed")
        self.argv = tuple(str(item) for item in argv)
        self.workdir = str(workdir)
        self.ro_files = tuple(str(item) for item in ro_files)
        self.env = {name: str(value) for name, value in supplied.items()}
        self.timeout = timeout
        self.requires = tuple(str(item) for item in requires)
        self.capabilities = (
            _LOCAL_CAPABILITIES if capabilities is None else frozenset(str(item) for item in capabilities)
        )


@dataclass(frozen=True)
class RunResult:
    """The outcome of one :meth:`Backend.run`. ``output`` is at most 1 MiB."""

    status: RunStatus
    exit_code: int
    output: bytes
    duration: float
    manifest: dict[str, object]
    manifest_sha256: str


@runtime_checkable
class Backend(Protocol):
    """A machine that can run a :class:`RunRequest`."""

    name: str
    capabilities: frozenset[str]

    def run(self, request: RunRequest, cancel: threading.Event | None = None) -> RunResult:
        """Run ``request``. ``cancel``, when set, stops the run."""


@runtime_checkable
class RemoteRunner(Protocol):
    """A remote machine with the same run contract as :class:`Backend`.

    No remote backend is implemented. :func:`resolve_backend` refuses
    ``remote`` with ``backend_incompatible`` before any process starts.
    """

    name: str
    capabilities: frozenset[str]

    def run(self, request: RunRequest, cancel: threading.Event | None = None) -> RunResult:
        """Run ``request`` on the remote machine."""


def _resolve_tool(name: str) -> Path | None:
    """An executable named ``name`` in ``/usr/bin`` or ``/bin``, or None."""
    if name == "" or "/" in name or name in {".", ".."}:
        return None
    for directory in _BIN_DIRS:
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_OUTPUT_LIMIT)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _executable_sha256(name: str) -> str | None:
    path = _resolve_tool(name)
    if path is None:
        return None
    return _file_sha256(path)


def _manifest_executables(request: RunRequest) -> dict[str, str]:
    names: list[str] = []
    if request.argv:
        names.append(Path(request.argv[0]).name)
    names.extend(request.requires)
    found: dict[str, str] = {}
    for name in names:
        if name in found or "/" in name:
            continue
        digest = _executable_sha256(name)
        if digest is not None:
            found[name] = digest
    return found


def _environment_manifest(request: RunRequest) -> dict[str, object]:
    uname = os.uname()
    return {
        "backend": _LOCAL_NAME,
        "kernel": uname.release,
        "machine": uname.machine,
        "executables": _manifest_executables(request),
    }


def check_environment(manifest: Mapping[str, object]) -> list[str]:
    """Keys of ``manifest`` that differ from this machine.

    An executable whose sha256 differs is reported as ``executables:<name>``.
    Comparison order is backend, kernel, machine, then executable names.
    """
    uname = os.uname()
    diffs: list[str] = []
    if manifest.get("backend") != _LOCAL_NAME:
        diffs.append("backend")
    if manifest.get("kernel") != uname.release:
        diffs.append("kernel")
    if manifest.get("machine") != uname.machine:
        diffs.append("machine")
    recorded = manifest.get("executables")
    if not isinstance(recorded, Mapping):
        diffs.append("executables")
        return diffs
    for name in sorted(str(item) for item in recorded):
        if recorded.get(name) != _executable_sha256(name):
            diffs.append(f"executables:{name}")
    return diffs


def _reject_incompatible(request: RunRequest) -> None:
    if not request.capabilities <= _LOCAL_CAPABILITIES:
        raise BackendIncompatible("backend_incompatible")
    for name in request.requires:
        if _resolve_tool(name) is None:
            raise BackendIncompatible(f"missing_dependency:{name}")


def _result(
    status: RunStatus,
    exit_code: int,
    output: bytearray,
    started: float,
    manifest: dict[str, object],
) -> RunResult:
    return RunResult(
        status=status,
        exit_code=exit_code,
        output=bytes(output[:_OUTPUT_LIMIT]),
        duration=time.monotonic() - started,
        manifest=manifest,
        manifest_sha256=sha256(manifest),
    )


class LocalJailBackend:
    """CPU runner. The worker jail is the containment boundary."""

    name = _LOCAL_NAME
    capabilities = _LOCAL_CAPABILITIES

    def run(self, request: RunRequest, cancel: threading.Event | None = None) -> RunResult:
        """Run ``request`` in the worker jail. Refusals happen before a process."""
        _reject_incompatible(request)
        manifest = _environment_manifest(request)
        output = bytearray()
        started = time.monotonic()
        try:
            code = run_sandboxed(
                request.argv,
                _IDENTITY,
                MemoryRecorder(),
                None,
                dict(request.env),
                request.timeout,
                root="worker",
                ro_binds=request.ro_files,
                worktree=str(Path(request.workdir).resolve()),
                cancel=cancel,
                output=output,
            )
        except subprocess.TimeoutExpired:
            return _result("timed_out", -9, output, started, manifest)
        if code == -9 and cancel is not None and cancel.is_set():
            status: RunStatus = "canceled"
        elif code == 0:
            status = "completed"
        else:
            status = "crashed"
        return _result(status, code, output, started, manifest)


def resolve_backend(name: str) -> Backend:
    """The named backend. ``remote`` is refused before any process starts."""
    if name in {_LOCAL_NAME, "local-jail"}:
        return LocalJailBackend()
    raise BackendIncompatible("backend_incompatible")
