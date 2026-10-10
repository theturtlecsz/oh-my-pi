"""Read-only and backup roles can read native job tables without write access."""

import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from omp_work.operations.jobs_migrate import jobs_migrations
from installed_runtime_support import ReservedPort
from pg_native import native_postgres


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
@pytest.mark.parametrize("creator", ["omp_work_migrator", "omp_work_owner"])
@pytest.mark.parametrize("reader", ["omp_work_readonly", "omp_work_backup"])
def test_native_job_tables_allow_reads_and_reject_writes(
    tmp_path: Path, creator: str, reader: str
) -> None:
    with ReservedPort() as reserve, native_postgres(
        tmp_path, reserve.port, reserve=reserve
    ):
        connection = {
            "host": "127.0.0.1",
            "port": reserve.port,
            "dbname": "postgres",
        }
        with psycopg.connect(**connection, user="postgres") as admin:
            admin.execute("CREATE ROLE omp_work_owner")
            for role in (
                "omp_work_migrator", "omp_work_app", "omp_work_readonly", "omp_work_backup"
            ):
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            admin.execute("GRANT omp_work_owner TO omp_work_migrator")
            admin.execute("GRANT CREATE ON DATABASE postgres TO omp_work_owner")
            admin.execute("CREATE SCHEMA omp_control AUTHORIZATION omp_work_owner")
            admin.execute("GRANT ALL ON SCHEMA omp_control TO omp_work_migrator")
            admin.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(creator)))
            for migration in jobs_migrations():
                admin.execute(migration.read_text())

        with psycopg.connect(**connection, user=reader, autocommit=True) as client:
            for table in ("workers", "operations", "outbox"):
                relation = sql.Identifier("omp_jobs", table)
                assert client.execute(
                    sql.SQL("SELECT * FROM {}").format(relation)
                ).fetchall() == []
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    client.execute(
                        sql.SQL("UPDATE {} SET workspace_id = workspace_id").format(relation)
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    client.execute(sql.SQL("DELETE FROM {}").format(relation))
