"""ops migrate adopts omp_jobs objects the pre-#189 standalone applier created.

That applier connected as omp_work_migrator and ran DDL with no SET ROLE, so
the tables were owned by omp_work_migrator. migrate reads
omp_jobs.schema_migrations as omp_work_owner and used to fail with
permission denied. It now reassigns those tables and sequences first.

The standalone applier itself now runs DDL as omp_work_owner, so a fresh
apply does not recreate the bad ownership.
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest

from omp_work.operations.cli import run as ops_run
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import jobs_migrations
from omp_work.operations.jobs_migrate import apply_jobs_migrations, jobs_migrations as applier_files

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

# Tables 0001 creates. The arch-dev failure was these eight, owned by the migrator.
_PRE_189_TABLES = {
    "admit_heartbeat",
    "job_events",
    "jobs",
    "leases",
    "reservations",
    "schema_migrations",
    "usage_events",
    "work_items",
}
_OWNER = "omp_work_owner"
_MIGRATOR = "omp_work_migrator"


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


def _expected_jobs_rows() -> list[tuple[int, str, str]]:
    return [
        (ordinal, path.name, sha256(path.read_bytes()).hexdigest())
        for ordinal, path in jobs_migrations()
    ]


def _jobs_rows(config: OperationsConfig) -> list[tuple[int, str, str]]:
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ordinal, filename, sha256 FROM omp_jobs.schema_migrations ORDER BY ordinal"
            )
            return [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]


def _relations(config: OperationsConfig) -> list[tuple[str, str, str]]:
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname, c.relkind::text, pg_get_userbyid(c.relowner)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'omp_jobs'
                  AND c.relkind IN ('r', 'p', 'S', 'i')
                ORDER BY c.relname
                """
            )
            return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def _drop_jobs_schema(config: OperationsConfig) -> None:
    with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS omp_jobs CASCADE")


def _ops_migrate(config: OperationsConfig) -> None:
    ops_run(
        argparse.Namespace(ops_command="migrate", target=None, lock_timeout=30),
        config,
    )


def _apply_pre_189_standalone(config: OperationsConfig) -> None:
    """The pre-#189 standalone applier: migrator login, no SET ROLE, 0001 only.

    Later jobs files stay unrecorded so ops migrate has to read the
    migrator-owned schema_migrations table and apply them.
    A sequence created in the same session is owned by the migrator too.
    """
    path = applier_files()[0]
    assert path.name == "0001_omp_jobs_schema.sql"
    ordinal, name, digest = _expected_jobs_rows()[0]
    assert name == path.name
    with psycopg.connect(**config.connection_kwargs(_MIGRATOR)) as conn:
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute(
                """
                INSERT INTO omp_jobs.schema_migrations (ordinal, filename, sha256)
                VALUES (%s, %s, %s)
                """,
                (ordinal, name, digest),
            )
            cur.execute("CREATE SEQUENCE omp_jobs.migrator_owned_seq")


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory):
    from pg_native import native_postgres

    root = tmp_path_factory.mktemp("ops-migrate-jobs-owner")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        try:
            from omp_work.operations.database import bootstrap

            bootstrap(config)
        finally:
            monkeypatch.undo()
        yield config


def test_standalone_applier_creates_jobs_owned_by_owner(migrated) -> None:
    _drop_jobs_schema(migrated)
    applied = apply_jobs_migrations(migrated)
    assert [row["filename"] for row in applied["applied"]] == [
        path.name for _, path in jobs_migrations()
    ]

    relations = _relations(migrated)
    tables = {name: owner for name, kind, owner in relations if kind in {"r", "p"}}
    sequences = {name: owner for name, kind, owner in relations if kind == "S"}
    indexes = {name: owner for name, kind, owner in relations if kind == "i"}
    assert _PRE_189_TABLES <= set(tables)
    assert set(tables.values()) == {_OWNER}
    assert not sequences or set(sequences.values()) == {_OWNER}
    assert not indexes or set(indexes.values()) == {_OWNER}
    assert _jobs_rows(migrated) == _expected_jobs_rows()

    again = apply_jobs_migrations(migrated)
    assert again["applied"] == []
    assert _jobs_rows(migrated) == _expected_jobs_rows()


def test_ops_migrate_adopts_schema_created_by_pre_189_applier(migrated) -> None:
    _drop_jobs_schema(migrated)
    _apply_pre_189_standalone(migrated)

    before = _relations(migrated)
    tables = {name: owner for name, kind, owner in before if kind in {"r", "p"}}
    sequences = {name: owner for name, kind, owner in before if kind == "S"}
    assert set(tables) == _PRE_189_TABLES
    assert set(tables.values()) == {_MIGRATOR}
    assert sequences["migrator_owned_seq"] == _MIGRATOR
    assert _jobs_rows(migrated) == _expected_jobs_rows()[:1]

    _ops_migrate(migrated)

    after = _relations(migrated)
    tables = {name: owner for name, kind, owner in after if kind in {"r", "p"}}
    sequences = {name: owner for name, kind, owner in after if kind == "S"}
    indexes = {name: owner for name, kind, owner in after if kind == "i"}
    assert _PRE_189_TABLES <= set(tables)
    assert {"workers", "operations", "outbox"} <= set(tables)
    assert set(tables.values()) == {_OWNER}
    assert sequences["migrator_owned_seq"] == _OWNER
    assert set(sequences.values()) == {_OWNER}
    assert set(indexes.values()) == {_OWNER}
    recorded = _jobs_rows(migrated)
    assert recorded == _expected_jobs_rows()

    _ops_migrate(migrated)
    assert _jobs_rows(migrated) == recorded
    assert _relations(migrated) == after
