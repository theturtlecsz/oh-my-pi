"""Shared native PostgreSQL launcher for integration tests.

The docker daemon is unavailable in this environment; tests run against the
host's PostgreSQL 18 binaries instead. Trust auth keeps the per-role password
secrets valid without extra setup.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg

_SUPERVISOR = Path(__file__).with_name("pg_supervisor.py")


def _wait_until_ready(process: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"postgres supervisor exited with {process.returncode}:\n"
                f"{log.read_text(errors='replace')[-4000:]}"
            )
        if (
            subprocess.run(
                ["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres"],
                capture_output=True,
            ).returncode
            == 0
        ):
            return
        time.sleep(0.1)
    raise RuntimeError(f"postgres on port {port} did not become ready:\n{log.read_text(errors='replace')[-4000:]}")


@contextlib.contextmanager
def native_postgres(root: Path, port: int) -> Generator[None]:
    data_dir = root / "pgdata"
    root.mkdir(parents=True, exist_ok=True)
    log = root / "postgres.log"
    subprocess.run(
        ["initdb", "-D", str(data_dir), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    # The supervisor runs postgres in this process's group and stops it when
    # this pipe closes, so a SIGKILLed pytest cannot leave the server behind.
    supervisor = subprocess.Popen(
        [sys.executable, str(_SUPERVISOR), str(data_dir), str(port), str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_ready(supervisor, port, log)
        yield
    finally:
        if supervisor.stdin:
            supervisor.stdin.close()
        try:
            supervisor.wait(timeout=15)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait(timeout=10)


def seed_authority(dsn_kwargs: dict, workspace_id: UUID, actor_id: UUID) -> None:
    """Test-only: mark a workspace Work-authoritative without exercising the cutover path."""
    epoch_id = uuid4()
    with psycopg.connect(**dsn_kwargs, autocommit=True) as connection:
        connection.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(actor_id)),
        )
        if connection.execute(
            "SELECT 1 FROM omp_control.workspace_authority WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone():
            return
        connection.execute(
            "INSERT INTO omp_control.cutover_epochs(epoch_id,workspace_id,state,candidate_manifest,candidate_manifest_sha256) VALUES(%s,%s,'sealed',%s::jsonb,%s)",
            (epoch_id, workspace_id, json.dumps({"seeded": True}), "0" * 64),
        )
        connection.execute(
            "INSERT INTO omp_control.workspace_authority(workspace_id,epoch_id) VALUES(%s,%s)",
            (workspace_id, epoch_id),
        )
