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
        "0003_native_research_jobs.sql",
        "0004_native_workspace_check.sql",
    ]
    for p in files:
        assert isinstance(p, Path)
        assert p.is_file()
        assert p.parent == jobs_migrations_dir()


def test_main_migrations_ledger_intact_and_isolated_from_jobs_migrations() -> None:
    main_migs = database.migrations()
    assert len(main_migs) == 38
    assert [ordinal for ordinal, _ in main_migs] == list(range(1, 39))

    jobs_names = {p.name for p in jobs_migrations()}
    assert jobs_names == {
        "0001_omp_jobs_schema.sql",
        "0002_shadow_provenance.sql",
        "0003_native_research_jobs.sql",
        "0004_native_workspace_check.sql",
    }

    main_names = {path.name for _, path in main_migs}
    assert jobs_names.isdisjoint(main_names)
    for name in jobs_names:
        assert name not in main_names


def test_migration_set_sha256_matches_work_proxy() -> None:
    assert migration_set_sha256_work() == database.migration_set_sha256()


def test_applied_0030_does_not_carry_project_lookup_indexes() -> None:
    """0030 is already applied. The lookup indexes live only in 0037."""
    root = Path(database.__file__).resolve().parent / "migrations"
    names = {
        "project_missions_workspace_project_idx": "omp_work.project_missions (workspace_id, project_id)",
        "project_history_workspace_project_idx": "omp_work.project_history (workspace_id, project_id)",
    }
    applied = (root / "0030_project_records.sql").read_text()
    added = (root / "0037_project_lookup_indexes.sql").read_text()
    for name, target in names.items():
        assert name not in applied
        assert f"CREATE INDEX {name}\n  ON {target};" in added
    holders = sorted(
        path.name
        for path in root.glob("*.sql")
        if any(name in path.read_text() for name in names)
    )
    assert holders == ["0037_project_lookup_indexes.sql"]
