"""``omp-work ops migrate`` owns the omp_jobs migration set.

`ops migrate` applies and records the jobs files in the same transaction as the
WorkService ones, folds them into ``migration_set_sha256()``, and reports their
pending/drift through ``check_migrations``/``collect_health``. The standalone
``apply_jobs_migrations`` re-runs nothing once the inline path recorded them.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from omp_work.operations import database
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import (
    bootstrap,
    check,
    check_migrations,
    collect_health,
    jobs_migrations,
    migration_set_sha256,
    migrate,
)
from omp_work.operations.jobs_migrate import apply_jobs_migrations

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_JOBS_TABLES = (
    "jobs",
    "job_events",
    "leases",
    "reservations",
    "usage_events",
    "work_items",
    "admit_heartbeat",
    "schema_migrations",
    "workers",
    "operations",
    "outbox",
)


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


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory):
    from pg_native import native_postgres

    root = tmp_path_factory.mktemp("ops-migrate-jobs")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()
        yield SimpleNamespace(config=config, root=root)


def _jobs_rows(config: OperationsConfig) -> list[tuple[int, str, str]]:
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ordinal, filename, sha256 FROM omp_jobs.schema_migrations ORDER BY ordinal"
            )
            return [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]


def test_fresh_bootstrap_applies_and_hashes_jobs_migrations(migrated) -> None:
    config = migrated.config
    expected = [
        (ordinal, path.name, sha256(path.read_bytes()).hexdigest())
        for ordinal, path in jobs_migrations()
    ]
    assert expected, "test expects at least one jobs migration file"
    assert _jobs_rows(config) == expected

    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            for table in _JOBS_TABLES:
                cur.execute("SELECT to_regclass(%s)", (f"omp_jobs.{table}",))
                assert cur.fetchone()[0] is not None, table
            cur.execute(
                "SELECT migration_set_sha256 FROM omp_control.runtime_compatibility"
            )
            assert cur.fetchone()[0] == migration_set_sha256()

    result = check(config)
    assert result["pending"] == [] and result["drift"] == []
    report = collect_health(config)
    assert report.migration["pending"] == [] and report.migration["drift"] == []
    assert "MIGRATION_PENDING" not in report.alerts
    assert "MIGRATION_DRIFT" not in report.alerts


def test_jobs_file_bytes_change_migration_set_hash(
    migrated, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "operations"
    shutil.copytree(Path(str(database.files("omp_work.operations"))), copy)
    monkeypatch.setattr(database, "_operations_path", lambda part: copy / part)

    before = migration_set_sha256()
    target = next(copy.joinpath("jobs_migrations").glob("*.sql"))
    target.write_bytes(target.read_bytes() + b"\n-- edited\n")
    assert migration_set_sha256() != before


def test_tampered_recorded_jobs_sha_is_drift(migrated) -> None:
    config = migrated.config
    original = _jobs_rows(config)
    try:
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                "UPDATE omp_jobs.schema_migrations SET sha256=%s WHERE ordinal=%s",
                ("0" * 64, original[0][0]),
            )
        with pytest.raises(ValueError, match="migration_drift"):
            migrate(config)
        with psycopg.connect(**config.connection_kwargs("omp_work_migrator")) as conn:
            with pytest.raises(ValueError, match="migration_drift"):
                check_migrations(conn, allow_pending=True)
        report = collect_health(config)
        assert report.migration["drift"] == ["migration_drift"]
        assert "MIGRATION_DRIFT" in report.alerts
    finally:
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                "UPDATE omp_jobs.schema_migrations SET sha256=%s WHERE ordinal=%s",
                (original[0][2], original[0][0]),
            )
    assert _jobs_rows(config) == original


def test_backup_role_reads_every_jobs_table(migrated) -> None:
    with psycopg.connect(
        **migrated.config.connection_kwargs("omp_work_backup")
    ) as conn:
        with conn.cursor() as cur:
            for table in _JOBS_TABLES:
                cur.execute(f"SELECT count(*) FROM omp_jobs.{table}")
                assert cur.fetchone()[0] >= 0


def test_second_migrate_and_apply_jobs_migrations_are_noops(migrated) -> None:
    config = migrated.config
    before = _jobs_rows(config)
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_control.schema_migrations")
            work_count = cur.fetchone()[0]

    migrate(config)

    assert _jobs_rows(config) == before
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_control.schema_migrations")
            assert cur.fetchone()[0] == work_count

    applied = apply_jobs_migrations(config)
    assert applied["applied"] == []
    assert sorted(applied["skipped"]) == sorted(path.name for _, path in jobs_migrations())
    assert _jobs_rows(config) == before


def test_target_limits_workservice_ordinals_only(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pg_native import native_postgres

    root = tmp_path_factory.mktemp("ops-migrate-jobs-target")
    config = _config(root)
    with native_postgres(root, config.port):
        original = database.migrate
        monkeypatch.setattr(database, "validate_bundle", lambda **kw: None)
        monkeypatch.setattr(
            database, "migrate", lambda cfg, **kw: original(cfg, target=20, **kw)
        )
        bootstrap(config)
        monkeypatch.setattr(database, "migrate", original)

        expected = [
            (ordinal, path.name, sha256(path.read_bytes()).hexdigest())
            for ordinal, path in jobs_migrations()
        ]
        assert _jobs_rows(config) == expected
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(ordinal) FROM omp_control.schema_migrations")
                assert cur.fetchone()[0] == 20