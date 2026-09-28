"""Docker argv for the Harbor worker container.

``run`` executes ``[docker, *args]`` and raises ``DockerError`` when the status
is non-zero. Container ids come from ``docker ps -q`` filtered by the Compose
project and service labels. Patches are applied with ``git apply`` on stdin.
``export_bundle`` commits the worker tree under a fixed identity and copies
out a v2 or v3 ``HEAD`` bundle. ``rpc_command`` is the ``docker exec`` argv
that records the omp pid before exec. ``pid_killer`` SIGKILLs that pid.
"""

from __future__ import annotations

import shlex
import subprocess  # nosec B404 - docker and git are argv lists; shell is only `sh -c` inside the container
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

_GIT_NAME = "omp-harbor"
_GIT_EMAIL = "omp-harbor@localhost"
_COMMIT_MESSAGE = "harbor worker tree"
_BUNDLE_IN_CONTAINER = "/tmp/worker-repo.bundle"
_OMP_PID_SCRIPT = 'echo $$ >/tmp/omp.pid; exec omp "$@"'
_KILL_SCRIPT = 'kill -KILL "$(cat /tmp/omp.pid)"'
_BUNDLE_MAGICS = (b"# v2 git bundle", b"# v3 git bundle")


class DockerError(RuntimeError):
    """A docker argv exited non-zero, or a bundle was not a v2/v3 git bundle."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run(docker: str, args: Sequence[str], input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run ``[docker, *args]``. Raise ``DockerError`` when the exit status is non-zero."""

    try:
        completed = subprocess.run(
            [docker, *args],
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise DockerError(f"docker failed to start: {exc}") from exc
    if completed.returncode != 0:
        command = args[0] if args else "docker"
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        message = f"docker {command} exited {completed.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise DockerError(
            message,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def compose_container(project: str, service: str, *, docker: str) -> str:
    """Return the one container id labeled with this Compose project and service."""

    completed = run(
        docker,
        [
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
    )
    ids = [line.strip() for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ids) != 1:
        raise DockerError(f"expected one {service} container in project {project}, found {len(ids)}")
    return ids[0]


def apply_patch(worker: str, patch: bytes, workdir: str, *, docker: str) -> None:
    """``git apply`` ``patch`` inside ``workdir`` on ``worker``."""

    run(docker, ["exec", "-i", worker, "git", "-C", workdir, "apply", "-"], input=patch)


def export_bundle(worker: str, workdir: str, dest: str | Path, *, docker: str) -> None:
    """Commit ``workdir``, bundle ``HEAD``, and copy it to ``dest``.

    A failed copy or bytes that are not a ``# v2`` or ``# v3`` git bundle raise
    ``DockerError`` and leave ``dest`` absent.
    """

    destination = Path(dest)
    run(docker, ["exec", worker, "sh", "-c", _bundle_script(workdir)])
    try:
        run(docker, ["cp", f"{worker}:{_BUNDLE_IN_CONTAINER}", str(destination)])
    except DockerError:
        _remove_file(destination)
        raise
    if not destination.is_file() or not _is_git_bundle(destination):
        _remove_file(destination)
        raise DockerError("worker bundle is not a v2 or v3 git bundle")


def rpc_command(worker: str, omp_args: Sequence[str], *, docker: str) -> list[str]:
    """``docker exec`` argv that writes ``/tmp/omp.pid`` and replaces itself with omp."""

    return [docker, "exec", "-i", worker, "sh", "-c", _OMP_PID_SCRIPT, "sh", *omp_args]


def pid_killer(worker: str, *, docker: str) -> Callable[[subprocess.Popen[Any]], None]:
    """Return a callable that SIGKILLs the pid recorded for ``worker``."""

    def kill(_process: subprocess.Popen[Any]) -> None:
        run(docker, ["exec", worker, "sh", "-c", _KILL_SCRIPT])

    return kill


def read_file(worker: str, path: str, *, docker: str) -> bytes:
    """Read ``path`` from ``worker``."""

    return run(docker, ["exec", worker, "cat", "--", path]).stdout


def write_file(worker: str, path: str, data: bytes, *, docker: str) -> None:
    """Write ``data`` to ``path`` on ``worker``, creating parent directories."""

    script = f"mkdir -p -- {shlex.quote(_posix_parent(path))} && cat > {shlex.quote(path)}"
    run(docker, ["exec", "-i", worker, "sh", "-c", script], input=data)


def _posix_parent(path: str) -> str:
    if "/" not in path:
        return "."
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def _bundle_script(workdir: str) -> str:
    name = shlex.quote(_GIT_NAME)
    email = shlex.quote(_GIT_EMAIL)
    message = shlex.quote(_COMMIT_MESSAGE)
    directory = shlex.quote(workdir)
    bundle = shlex.quote(_BUNDLE_IN_CONTAINER)
    return (
        f"cd -- {directory} && "
        "git add -A && "
        f"git -c user.name={name} -c user.email={email} -c commit.gpgsign=false "
        f"commit --allow-empty -m {message} && "
        f"git bundle create {bundle} HEAD"
    )


def _is_git_bundle(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(len(_BUNDLE_MAGICS[1]) + 1)
    for magic in _BUNDLE_MAGICS:
        if not header.startswith(magic):
            continue
        if len(header) == len(magic):
            return True
        return header[len(magic) : len(magic) + 1] in (b"\n", b"\r")
    return False


def _remove_file(path: Path) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink()
