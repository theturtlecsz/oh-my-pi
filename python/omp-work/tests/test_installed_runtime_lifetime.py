"""Installed-runtime test servers must die with their pytest parent."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from installed_runtime_support import (
    InstalledRelease,
    _free_port,
    _run,
)
from installed_runtime_support import (
    installed_release as installed_release,  # noqa: PLC0414 -- pytest fixture re-export
)
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority


def _process_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _child_pids(parent_pid: int) -> list[int]:
    task_children = Path(f"/proc/{parent_pid}/task/{parent_pid}/children")
    if task_children.exists():
        try:
            children = [int(p) for p in task_children.read_text().split()]
            if children:
                return children
        except (FileNotFoundError, ProcessLookupError):
            pass
    children = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat_content = (entry / "stat").read_text()
                ppid = int(stat_content.rsplit(")", 1)[1].split()[1])
                if ppid == parent_pid:
                    children.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
                continue
    return children


_CHILD_RUNNER = """
import sys, time, json, os
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from installed_runtime_support import _process, _health

service_command = json.loads(sys.argv[2])
candidate = Path(sys.argv[3])
env = json.loads(sys.argv[4])
service_log = Path(sys.argv[5])
base_url = sys.argv[6]
ready_file = Path(sys.argv[7])

def child_pids(parent_pid):
    task_children = Path(f"/proc/{parent_pid}/task/{parent_pid}/children")
    if task_children.exists():
        try:
            res = [int(p) for p in task_children.read_text().split()]
            if res:
                return res
        except (FileNotFoundError, ProcessLookupError):
            pass
    res = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat_content = (entry / "stat").read_text()
                ppid = int(stat_content.rsplit(")", 1)[1].split()[1])
                if ppid == parent_pid:
                    res.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
                continue
    return res

with _process(service_command, candidate, env, service_log) as service:
    _health(base_url, service, service_log)
    bun_pid = service.pid
    children = child_pids(bun_pid)
    deadline = time.monotonic() + 10
    while not children and time.monotonic() < deadline:
        time.sleep(0.05)
        children = child_pids(bun_pid)
    python_pid = children[0] if children else -1
    ready_file.write_text(json.dumps({"bun_pid": bun_pid, "python_pid": python_pid}))
    while True:
        time.sleep(1)
"""


def test_sigkill_of_pytest_parent_terminates_installed_runtime_servers(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Installed runtime servers started by _process must die when their pytest parent dies."""
    release = installed_release
    state = tmp_path / "runtime"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    pg_port, http_port = _free_port(), _free_port()
    service_args = ("--service", "--postgres-port", str(pg_port))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def run_service(*arguments: str) -> str:
        return _run(
            release.command(state, candidate, *service_args, *arguments), candidate, env
        )

    with native_postgres(tmp_path / "postgres", pg_port):
        identity = json.loads(run_service("ops", "credentials", "init"))
        run_service("ops", "bootstrap")
        config = OperationsConfig(
            config_dir=state / "config/omp/work-ledger",
            state_dir=state / "state/omp/work-ledger",
            data_dir=state / "data/omp/work-ledger",
            port=pg_port,
        )
        seed_authority(
            config.connection_kwargs("postgres"),
            UUID(identity["workspace_id"]),
            UUID(identity["owner_id"]),
        )
        base_url = f"http://127.0.0.1:{http_port}"
        run_service(
            "ops",
            "capabilities",
            "init",
            "--workspace-id",
            identity["workspace_id"],
            "--owner-id",
            identity["owner_id"],
            "--base-url",
            base_url,
        )

        service_command = release.command(
            state,
            candidate,
            *service_args,
            "serve",
            "--port",
            str(http_port),
            "--capabilities-dir",
            str(config.config_dir / "capabilities"),
        )
        service_log = tmp_path / "service.stderr"
        ready_file = tmp_path / "ready.json"

        test_dir = str(Path(__file__).parent)
        parent_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_RUNNER,
                test_dir,
                json.dumps(service_command),
                str(candidate),
                json.dumps(env),
                str(service_log),
                base_url,
                str(ready_file),
            ],
            env={**os.environ, "PYTHONPATH": test_dir},
        )
        bun_pid = -1
        python_pid = -1
        try:
            deadline = time.monotonic() + 30
            while not ready_file.exists():
                assert parent_proc.poll() is None, (
                    f"parent runner exited prematurely: {service_log.read_text()[-2000:]}"
                )
                assert time.monotonic() < deadline, "parent runner never reported ready"
                time.sleep(0.1)

            pids = json.loads(ready_file.read_text())
            bun_pid = pids["bun_pid"]
            python_pid = pids["python_pid"]
            assert bun_pid > 0 and python_pid > 0, f"invalid server pids: {pids}"
            assert _process_alive(bun_pid), f"bun process {bun_pid} is not alive"
            assert _process_alive(python_pid), (
                f"python server process {python_pid} is not alive"
            )

            # SIGKILL the parent process simulating an abrupt test abort / flood budget kill
            os.kill(parent_proc.pid, signal.SIGKILL)
            parent_proc.wait(timeout=10)

            # Both server processes must terminate within a few seconds
            deadline = time.monotonic() + 10
            while (
                _process_alive(bun_pid) or _process_alive(python_pid)
            ) and time.monotonic() < deadline:
                time.sleep(0.1)

            assert not _process_alive(bun_pid), (
                f"bun launcher process {bun_pid} survived SIGKILL of parent process"
            )
            assert not _process_alive(python_pid), (
                f"python server process {python_pid} survived SIGKILL of parent process"
            )
        finally:
            if parent_proc.poll() is None:
                parent_proc.kill()
                parent_proc.wait(timeout=10)
            if bun_pid > 0 and _process_alive(bun_pid):
                os.kill(bun_pid, signal.SIGKILL)
            if python_pid > 0 and _process_alive(python_pid):
                os.kill(python_pid, signal.SIGKILL)
