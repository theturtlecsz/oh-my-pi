"""Shared native PostgreSQL launcher for integration tests.

The docker daemon is unavailable in this environment; tests run against the
host's PostgreSQL 18 binaries instead. Trust auth keeps the per-role password
secrets valid without extra setup.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg


@contextlib.contextmanager
def native_postgres(root: Path, port: int) -> Generator[None]:
    data_dir = root / "pgdata"
    sock_dir = root / "pgsock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["initdb", "-D", str(data_dir), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(data_dir),
            "-l",
            str(root / "postgres.log"),
            "-w",
            "-o",
            f"-p {port} -k {sock_dir} -c listen_addresses=127.0.0.1",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield
    finally:
        subprocess.run(
            ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
            check=False,
            capture_output=True,
        )


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
