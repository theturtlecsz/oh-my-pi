"""Read-only and backup roles can read native job tables without write access.

``0001_omp_jobs_schema.sql`` grants ``omp_work_readonly`` SELECT on its tables
and relies on owner default privileges for later ones. A jobs file that
``omp_work_migrator`` runs creates its tables as the migrator, so those default
privileges do not fire and the tables ``0003`` added (workers, operations,
outbox) stay closed to that role. ``sql/jobs_grants.sql`` re-runs on every
``ops migrate`` and closes the gap for every existing table. This test applies
``0003`` the way the pre-OMP-453 applier did, checks the gap, then runs the
real migrate path and checks both read-only roles.
"""

from __future__ import annotations

import os
import secrets
import socket
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest

from omp_work.operations import database
from omp_work.operations.config import OperationsConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_MIGRATOR = "omp_work_migrator"
_NEW_TABLES = ("workers", "operations", "outbox")
_READERS = ("omp_work_readonly", "omp_work_backup")


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


def _apply_jobs_files_as_migrator(config: OperationsConfig) -> None:
    """Run every jobs file as the migrator login with no ``SET ROLE``.

    That is how the standalone applier ran before OMP-453, so the tables a file
    creates are owned by ``omp_work_migrator`` and never see the owner default
    privileges ``0001`` sets. Each file is recorded so the later migrate path
    sees a clean prefix instead of drift.
    """
    with psycopg.connect(**config.connection_kwargs(_MIGRATOR)) as conn:
        with conn.cursor() as cur:
            for ordinal, path in database.jobs_migrations():
                cur.execute(path.read_text())
                cur.execute(
                    "INSERT INTO omp_jobs.schema_migrations (ordinal, filename, sha256)"
                    " VALUES (%s, %s, %s)",
                    (ordinal, path.name, sha256(path.read_bytes()).hexdigest()),
                )


def _migrate(config: OperationsConfig) -> None:
    import argparse

    from omp_work.operations.cli import run as ops_run

    ops_run(
        argparse.Namespace(ops_command="migrate", target=None, lock_timeout=30),
        config,
    )


def _can_read(config: OperationsConfig, role: str, table: str) -> bool:
    with psycopg.connect(**config.connection_kwargs(role)) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT * FROM omp_jobs.{table}")
            except psycopg.errors.InsufficientPrivilege:
                return False
            return True


@pytest.fixture()
def migrated(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    from pg_native import native_postgres

    root = tmp_path_factory.mktemp("jobs-read-grants")
    config = _config(root)
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    with native_postgres(root, config.port):
        database.bootstrap(config)
        yield config


def test_ops_migrate_repairs_read_grants_for_migrator_owned_tables(migrated) -> None:
    config = migrated

    with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("DROP SCHEMA omp_jobs CASCADE")
    _apply_jobs_files_as_migrator(config)

    # Pre-OMP-453 state: the migrator owns the 0003 tables, so the 0001 owner
    # default privileges never granted omp_work_readonly SELECT on them.
    for table in _NEW_TABLES:
        assert not _can_read(config, "omp_work_readonly", table)

    _migrate(config)

    for role in _READERS:
        for table in _NEW_TABLES:
            assert _can_read(config, role, table), f"{role} cannot read omp_jobs.{table}"
        with psycopg.connect(**config.connection_kwargs(role), autocommit=True) as conn:
            for table in _NEW_TABLES:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f"INSERT INTO omp_jobs.{table} DEFAULT VALUES")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        f"UPDATE omp_jobs.{table} SET workspace_id = workspace_id"
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f"DELETE FROM omp_jobs.{table}")
