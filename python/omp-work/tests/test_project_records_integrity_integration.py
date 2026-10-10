"""OMP-559-s02: populated 0029→0030 upgrade backfills one record per project, idempotently."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import omp_work.operations.database as database
import psycopg
import pytest
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres
from test_project_store import _insert_project
from test_workflow_service import OWNER, _config

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

RECORD_ROWS = (
    "SELECT workspace_id, project_id, purpose, updated_at::text"
    " FROM omp_work.project_records ORDER BY workspace_id, project_id"
)


@pytest.fixture(scope="module")
def upgrade_env(tmp_path_factory: pytest.TempPathFactory):
    """Isolated database bootstrapped only to 0029, with projects seeded pre-0030."""
    root = tmp_path_factory.mktemp("project-records-upgrade")
    config = _config(root)
    with native_postgres(root, config.port):
        original_migrate = database.migrate

        def migrate_only_to_0029(config_arg, **kwargs) -> None:
            original_migrate(config_arg, target=29)

        guard = pytest.MonkeyPatch()
        guard.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        guard.setattr("omp_work.operations.database.migrate", migrate_only_to_0029)
        try:
            database.bootstrap(config)
        finally:
            guard.undo()

        seeded: dict[UUID, list[UUID]] = {uuid4(): [uuid4(), uuid4()], uuid4(): [uuid4()]}
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            for workspace_id, project_ids in seeded.items():
                conn.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s)",
                    (workspace_id,),
                )
                for project_id in project_ids:
                    conn.execute(
                        "INSERT INTO omp_work.projects"
                        " (project_id, workspace_id, key, name, kind)"
                        " VALUES (%s, %s, %s, %s, 'surface')",
                        (project_id, workspace_id, str(project_id), f"Project {project_id}"),
                    )
        yield SimpleNamespace(config=config, seeded=seeded)


def test_populated_0029_to_0030_upgrade(upgrade_env, monkeypatch: pytest.MonkeyPatch) -> None:
    config = upgrade_env.config
    seeded: dict[UUID, list[UUID]] = upgrade_env.seeded
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    old_projects = [
        (workspace_id, project_id)
        for workspace_id, project_ids in seeded.items()
        for project_id in project_ids
    ]

    # The 0029→0030 step runs as migrator→owner with no workspace/actor claims;
    # the backfill must still see every project past FORCE RLS on omp_work.projects.
    database.migrate(config, target=30)

    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        for workspace_id, project_id in old_projects:
            row = conn.execute(
                "SELECT count(*) FROM omp_work.project_records"
                " WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            ).fetchone()
            assert row is not None and row[0] == 1
        total = conn.execute("SELECT count(*) FROM omp_work.project_records").fetchone()
        assert total is not None and total[0] == len(old_projects)
        rls = {
            relname: (enabled, forced)
            for relname, enabled, forced in conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'omp_work'"
                " AND c.relname IN ('projects', 'project_records')"
            ).fetchall()
        }
    assert rls == {"projects": (True, True), "project_records": (True, True)}

    database.migrate(config)  # 0031 → latest

    # At the latest schema an importer insert with workspace/OWNER claims is
    # seeded exactly one record by the AFTER INSERT trigger.
    importer_workspace = next(iter(seeded))
    imported_project = uuid4()
    _insert_project(upgrade_env, importer_workspace, imported_project, "imported")
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        row = conn.execute(
            "SELECT count(*) FROM omp_work.project_records"
            " WHERE workspace_id=%s AND project_id=%s",
            (importer_workspace, imported_project),
        ).fetchone()
        assert row is not None and row[0] == 1
        before = conn.execute(RECORD_ROWS).fetchall()
    assert len(before) == len(old_projects) + 1

    store = PostgresWorkStore(config)
    for workspace_id, project_id in [*old_projects, (importer_workspace, imported_project)]:
        view = store.read_project(workspace_id, OWNER, project_id)
        assert view["project_id"] == str(project_id)

    expected = {workspace_id: len(project_ids) for workspace_id, project_ids in seeded.items()}
    expected[importer_workspace] += 1
    for workspace_id, projects in expected.items():
        assert store.count_missing_records(workspace_id, OWNER) == (projects, 0)

    database.migrate(config)  # re-run: nothing pending, records untouched
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        after = conn.execute(RECORD_ROWS).fetchall()
        duplicates = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM omp_work.project_records"
            " GROUP BY workspace_id, project_id HAVING count(*) > 1) AS dupes"
        ).fetchone()
    assert after == before
    assert duplicates is not None and duplicates[0] == 0
