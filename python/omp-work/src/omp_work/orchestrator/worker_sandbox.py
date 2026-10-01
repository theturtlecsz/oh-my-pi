"""Worker sandbox for orchestrator locks 1-3 (OMP-417).

:func:`run_worker` runs ``argv`` in the ``worker`` root profile: the research
jail (tmpfs ``pivot_root``, read-only ``/usr`` ``/bin`` ``/lib``, a fresh
``/proc``, and its own user, PID, and network namespaces) plus one read-write
bind of a linked worktree at ``/work`` and any read-only files the caller
names. The process environment is PATH, LANG, TERM, TZ, OMP_TASK_TOKEN, and
OMP_WORK_URL. The only loopback service is the WorkService relay.

The worktree must be a linked worktree (a ``.git`` file whose gitdir lives
under ``worktrees/``) strictly inside ``OMP_WORKTREES_DIR``. The live checkout
and any path outside that directory are refused before a sandbox starts.
``identity`` names the namespace identity and is ``worker`` or ``verifier``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import run_sandboxed

__all__ = ["WorkerSandboxRefused", "run_worker"]

_IDENTITIES = frozenset({"worker", "verifier"})
_PATH = "/usr/bin:/bin"


class WorkerSandboxRefused(RuntimeError):
    """Raised when a worker launch is refused before any sandbox process starts.

    ``code`` is ``identity_not_allowed``, ``worktrees_dir_unconfigured``,
    ``worktree_not_allowed``, ``live_checkout``, ``ro_bind_not_allowed``, or
    ``workservice_not_allowed``.
    """

    code: str

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _worktrees_dir() -> Path:
    raw = os.environ.get("OMP_WORKTREES_DIR", "").strip()
    if raw == "":
        raise WorkerSandboxRefused("worktrees_dir_unconfigured")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise WorkerSandboxRefused("worktrees_dir_unconfigured")
    return path.resolve()


def _linked_worktree(worktree: str | Path, root: Path) -> Path:
    """The worktree path, or a refusal if it is not a linked worktree inside ``root``."""
    resolved = Path(worktree).resolve()
    if not resolved.is_dir():
        raise WorkerSandboxRefused("worktree_not_allowed")
    try:
        resolved.relative_to(root)
    except ValueError:
        raise WorkerSandboxRefused("worktree_not_allowed") from None
    if resolved == root:
        raise WorkerSandboxRefused("worktree_not_allowed")
    git = resolved / ".git"
    if git.is_dir():
        raise WorkerSandboxRefused("live_checkout")
    if not git.is_file():
        raise WorkerSandboxRefused("worktree_not_allowed")
    try:
        text = git.read_text(encoding="utf-8")
    except OSError:
        raise WorkerSandboxRefused("worktree_not_allowed") from None
    if not text.startswith("gitdir:"):
        raise WorkerSandboxRefused("worktree_not_allowed")
    gitdir = Path(text.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = resolved / gitdir
    gitdir = gitdir.resolve()
    if "worktrees" not in gitdir.parts or not gitdir.is_dir():
        raise WorkerSandboxRefused("worktree_not_allowed")
    return resolved


def _ro_bind(path: str | Path) -> str:
    """One caller-named file, kept at the path the caller wrote."""
    raw = Path(path)
    if not raw.is_absolute() or raw.is_dir() or not raw.is_file():
        raise WorkerSandboxRefused("ro_bind_not_allowed")
    return str(raw)


def _workservice(value: object) -> tuple[str, int]:
    """``(host, port)``, ``host:port``, or an ``http://host:port`` URL."""
    if isinstance(value, tuple) and len(value) == 2:
        host, port = value
        if (
            isinstance(host, str)
            and host != ""
            and isinstance(port, int)
            and not isinstance(port, bool)
        ):
            return host, port
        raise WorkerSandboxRefused("workservice_not_allowed")
    if isinstance(value, str) and "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname and parsed.port:
            return parsed.hostname, parsed.port
        raise WorkerSandboxRefused("workservice_not_allowed")
    if isinstance(value, str) and value.count(":") == 1 and not value.startswith("/"):
        host, port_text = value.split(":", 1)
        if host != "" and port_text.isdecimal():
            return host, int(port_text)
    raise WorkerSandboxRefused("workservice_not_allowed")


def run_worker(
    argv: Sequence[str],
    *,
    worktree: str | Path,
    token: str,
    workservice_socket: object,
    identity: str,
    ro_binds: Sequence[str | Path] = (),
    timeout: float | int | None,
) -> int:
    """Run ``argv`` in the worker jail. Returns the process exit status.

    Refuses, before any sandbox process, a worktree that is the live checkout
    or that lies outside ``OMP_WORKTREES_DIR``. ``token`` is ``OMP_TASK_TOKEN``.
    ``workservice_socket`` is the host WorkService the existing relay exposes
    on loopback inside the jail as ``OMP_WORK_URL``.
    """
    if identity not in _IDENTITIES:
        raise WorkerSandboxRefused("identity_not_allowed")
    root = _worktrees_dir()
    linked = _linked_worktree(worktree, root)
    binds = tuple(_ro_bind(item) for item in ro_binds)
    host, port = _workservice(workservice_socket)
    env = {
        "PATH": _PATH,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TZ": "UTC",
        "OMP_TASK_TOKEN": token,
        "OMP_WORK_URL": f"http://127.0.0.1:{port}",
    }
    egress = Identity(
        workspace_id="worker",
        project_id=identity,
        mission_id=identity,
        worker_id=identity,
        stage="repository",
    )
    return run_sandboxed(
        list(argv),
        egress,
        MemoryRecorder(),
        None,
        env,
        timeout,
        workservice=(host, port),
        root="worker",
        ro_binds=binds,
        worktree=linked,
    )
