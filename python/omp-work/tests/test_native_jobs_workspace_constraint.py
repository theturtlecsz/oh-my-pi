"""Native jobs require a workspace and kind on every database write."""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import CheckViolation

from omp_work.jobs.store import NativeJobStore
from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from test_native_jobs_workers import _insert_native_job

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.mark.parametrize(
    ("missing_workspace", "missing_kind"),
    [(True, False), (False, True), (True, True)],
)
def test_native_insert_rejects_missing_workspace_or_kind(
    native_jobs, missing_workspace: bool, missing_kind: bool
) -> None:
    job_id = str(uuid4())
    with pytest.raises(CheckViolation) as refused:
        _insert_native_job(
            native_jobs,
            job_id=job_id,
            workspace_id=None if missing_workspace else native_jobs.workspace_id,
            kind=None if missing_kind else "compute",
        )
    assert refused.value.diag.constraint_name == "jobs_native_workspace_check"
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("omp_work_app")) as conn:
        assert conn.execute(
            "SELECT job_id FROM omp_jobs.jobs WHERE job_id=%s", (job_id,)
        ).fetchone() is None


@pytest.mark.parametrize("column", ["workspace_id", "kind"])
def test_native_update_cannot_remove_workspace_or_kind(native_jobs, column: str) -> None:
    job_id = str(uuid4())
    _insert_native_job(native_jobs, job_id=job_id)
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        before = conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s", (job_id,)
        ).fetchone()
        with pytest.raises(CheckViolation) as refused:
            conn.execute(
                sql.SQL("UPDATE omp_jobs.jobs SET {}=NULL WHERE job_id=%s").format(
                    sql.Identifier(column)
                ),
                (job_id,),
            )
        assert refused.value.diag.constraint_name == "jobs_native_workspace_check"
        assert conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s", (job_id,)
        ).fetchone() == before


@pytest.mark.parametrize("kind", ["compute", "model"])
def test_valid_native_rows_remain_visible_to_workspace_queries(native_jobs, kind: str) -> None:
    job_id = str(uuid4())
    _insert_native_job(native_jobs, job_id=job_id, kind=kind)
    store = NativeJobStore(native_jobs.service.config)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        row = store.job_view(cur, native_jobs.workspace_id, job_id)
        assert row is not None
        assert row["job_id"] == job_id
        assert row["workspace_id"] == str(native_jobs.workspace_id)
        assert row["kind"] == kind
        assert row["source"] == "native"
        assert store.job_view(cur, uuid4(), job_id) is None


def test_flood_import_without_workspace_or_kind_remains_valid(native_jobs) -> None:
    job_id = str(uuid4())
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source) VALUES(%s,'backlog','flood_import')",
            (job_id,),
        )
        conn.execute("UPDATE omp_jobs.jobs SET status='in_flight' WHERE job_id=%s", (job_id,))
        assert conn.execute(
            "SELECT source,workspace_id,kind,status FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone() == ("flood_import", None, None, "in_flight")
