"""Bring up one fixture for an interactive omp session, then read the service back.

``run_interactive_env`` loads the task, starts workservice and the worker, applies
the seed patch and then the solution, points omp at the scripted model, and
waits. After ``stop`` (or Ctrl-C) it writes ``service-readback.json`` from
``ExecProbe.read`` and copies the worker session directory. The WorkService is
only read.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import signal
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from .docker_ops import DockerError, apply_patch, compose_container, run, write_file
from .netns import ExecProbe, model_port, start_model_sidecar, stop_container
from .scripted_model import models_yml
from .task_env import TaskEnv, load_task

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"
_BEARER = "OMP_HARBOR_BEARER"
_WORKSPACE = "OMP_HARBOR_WORKSPACE_ID"


def run_interactive_env(
    fixture_id: str,
    out: str | Path,
    *,
    fixtures_root: str | Path,
    docker: str,
    stop: Callable[[], None] | None = None,
) -> int:
    """Start fixture ``fixture_id`` and return 0, or 2 when the task or credentials are refused.

    A ``ValueError`` from ``load_task`` returns 2 before any docker argv.
    ``OMP_HARBOR_BEARER`` and ``OMP_HARBOR_WORKSPACE_ID`` are required the same way.
    """

    environment = Path(fixtures_root) / fixture_id / "environment"
    try:
        task = load_task(environment)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    bearer = os.environ.get(_BEARER)
    workspace_id = os.environ.get(_WORKSPACE)
    if not bearer or not workspace_id:
        print(f"{_BEARER} and {_WORKSPACE} are required", file=sys.stderr)
        return 2

    out_dir = Path(out)
    session_dir = out_dir / "session"
    out_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    compose_file = environment / "docker-compose.yaml"
    project = f"omp-harbor-interactive-{fixture_id}"
    staging = Path(tempfile.mkdtemp(prefix="omp-harbor-interactive-"))
    sidecar: str | None = None
    try:
        run(
            docker,
            ["compose", "-f", str(compose_file), "-p", project, "up", "-d", "workservice", "worker"],
        )
        ws = compose_container(project, "workservice", docker=docker)
        worker = compose_container(project, "worker", docker=docker)
        _apply_tree(task, worker, docker)
        write_file(
            worker,
            f"{task.home}/.omp/agent/models.yml",
            models_yml(task.model_url).encode(),
            docker=docker,
        )
        sidecar = start_model_sidecar(
            ws,
            task.workservice_image,
            task.fixture.scenario.model_script,
            model_port(task.model_url),
            staging,
            docker=docker,
        )
        _print_banner(task, worker, session_dir)
        if stop is not None:
            stop()
        else:
            try:
                signal.pause()
            except KeyboardInterrupt:
                pass
        readback = ExecProbe(task.workservice_url, bearer, workspace_id, worker, docker=docker).read()
        (out_dir / "service-readback.json").write_text(
            json.dumps(readback, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _copy_sessions(docker, worker, task.home, session_dir)
        return 0
    finally:
        if sidecar is not None:
            with contextlib.suppress(DockerError):
                stop_container(sidecar, docker)
        with contextlib.suppress(DockerError):
            run(docker, ["compose", "-f", str(compose_file), "-p", project, "down", "-v"])
        shutil.rmtree(staging, ignore_errors=True)


def _apply_tree(task: TaskEnv, worker: str, docker: str) -> None:
    directory = task.fixture.directory
    for name in (task.fixture.seed_patch, task.fixture.solution_patch):
        apply_patch(worker, (directory / name).read_bytes(), task.working_dir, docker=docker)


def _print_banner(task: TaskEnv, worker: str, session_dir: Path) -> None:
    key = task.fixture.scenario.command.split()[-1]
    print(f"key: {key}", flush=True)
    print(
        "omp: docker exec -it "
        f"{worker} omp --model scripted/scripted --session-dir {task.home}/omp-sessions",
        flush=True,
    )
    print(f"session dir: {session_dir}", flush=True)


def _copy_sessions(docker: str, worker: str, home: str, dest: Path) -> None:
    """``docker cp`` the session tree. A file-only cp falls back to tar of the same tree."""

    remote = f"{home}/omp-sessions"
    try:
        run(docker, ["cp", f"{worker}:{remote}/.", str(dest)])
    except DockerError:
        _copy_sessions_tar(docker, worker, remote, dest)


def _copy_sessions_tar(docker: str, worker: str, remote: str, dest: Path) -> None:
    try:
        archived = run(docker, ["exec", worker, "tar", "-C", remote, "-cf", "-", "."])
    except DockerError:
        return
    if not archived.stdout:
        return
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
        archive.extractall(dest, filter="data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.interactive_env")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixtures-root")
    args = parser.parse_args(argv)
    root = args.fixtures_root if args.fixtures_root is not None else FIXTURES_ROOT
    return run_interactive_env(args.fixture, args.out, fixtures_root=root, docker="docker")


if __name__ == "__main__":
    raise SystemExit(main())
