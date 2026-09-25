"""Tests for jobs_migrations file enumeration and ledger separation."""
from __future__ import annotations

from pathlib import Path

from omp_work.operations import database
from omp_work.operations.jobs_migrate import (
    jobs_migrations,
    jobs_migrations_dir,
    migration_set_sha256_work,
)


def test_jobs_migrations_returns_expected_files_in_ordinal_order() -> None:
    files = jobs_migrations()
    assert [p.name for p in files] == [
        "0001_omp_jobs_schema.sql",
        "0002_shadow_provenance.sql",
    ]
    for p in files:
        assert isinstance(p, Path)
        assert p.is_file()
        assert p.parent == jobs_migrations_dir()


def test_main_migrations_ledger_intact_and_isolated_from_jobs_migrations() -> None:
    main_migs = database.migrations()
    assert len(main_migs) == 23
    assert [ordinal for ordinal, _ in main_migs] == list(range(1, 24))

    jobs_names = {p.name for p in jobs_migrations()}
    assert jobs_names == {"0001_omp_jobs_schema.sql", "0002_shadow_provenance.sql"}

    main_names = {path.name for _, path in main_migs}
    assert jobs_names.isdisjoint(main_names)
    for name in jobs_names:
        assert name not in main_names


def test_migration_set_sha256_matches_work_proxy() -> None:
    assert migration_set_sha256_work() == database.migration_set_sha256()
