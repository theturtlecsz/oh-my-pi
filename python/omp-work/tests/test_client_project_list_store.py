"""OMP-416: PostgreSQL integration for the client project list read.

Gated exactly as test_mission_status_store.py. Two projects exist in one
workspace; one is archived by direct SQL, and list_projects returns only the
other, ordered by name then key.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _workspace(service, workspace_id) -> None:
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )


def _insert_project(service, workspace_id, project_id, key: str, name: str) -> None:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_importer"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects(project_id, workspace_id, key, name, kind)"
            " VALUES (%s, %s, %s, %s, 'surface')",
            (project_id, workspace_id, key, name),
        )


def _archive(service, workspace_id, project_id) -> None:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_work.projects SET archived=true WHERE workspace_id=%s AND project_id=%s",
            (workspace_id, project_id),
        )


def test_list_projects_excludes_archived_and_orders_by_name_then_key(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    # Same name for both unarchived rows so the ordering falls through to key.
    live_b = uuid4()
    live_a = uuid4()
    archived = uuid4()
    _insert_project(service, workspace_id, live_b, "b-key", "Alpha")
    _insert_project(service, workspace_id, live_a, "a-key", "Alpha")
    _insert_project(service, workspace_id, archived, "z-key", "Zecret")
    _archive(service, workspace_id, archived)

    listed = store.list_projects(workspace_id, OWNER)

    assert listed["workspace_id"] == str(workspace_id)
    assert listed["projects"] == [
        {"project_id": str(live_a), "key": "a-key", "name": "Alpha", "kind": "surface"},
        {"project_id": str(live_b), "key": "b-key", "name": "Alpha", "kind": "surface"},
    ]
    assert str(archived) not in {row["project_id"] for row in listed["projects"]}
