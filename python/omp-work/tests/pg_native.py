"""Shared native PostgreSQL launcher for integration tests.

The docker daemon is unavailable in this environment; tests run against the
host's PostgreSQL 18 binaries instead. Trust auth keeps the per-role password
secrets valid without extra setup.
"""
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Generator
from pathlib import Path


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
        ["pg_ctl", "-D", str(data_dir), "-l", str(root / "postgres.log"), "-w", "-o", f"-p {port} -k {sock_dir} -c listen_addresses=127.0.0.1", "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield
    finally:
        subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False, capture_output=True)
