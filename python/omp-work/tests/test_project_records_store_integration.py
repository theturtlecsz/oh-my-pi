"""OMP-559-s01: PostgreSQL integration tests for project records, missions, and history RLS."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from omp_work.v1.store import PostgresWorkStore
from test_project_store import _workspace
from test_workflow_service import OWNER

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def test_project_records_store_rls_isolation(service) -> None:
    store = PostgresWorkStore(service.config)
    ws1 = uuid4()
    ws2 = uuid4()
    _workspace(service, ws1)
    _workspace(service, ws2)

    p1 = store.ensure_project(ws1, OWNER, "proj-1", "Project 1", "surface")
    m1 = uuid4()
    store.link_mission(ws1, OWNER, p1, m1, "Mission 1", "draft")
    store.append_history(ws1, OWNER, p1, "event-1", "History 1", mission_id=m1)

    p2 = store.ensure_project(ws2, OWNER, "proj-2", "Project 2", "surface")
    m2 = uuid4()
    store.link_mission(ws2, OWNER, p2, m2, "Mission 2", "draft")
    store.append_history(ws2, OWNER, p2, "event-2", "History 2", mission_id=m2)

    workspaces = [
        (ws1, p1, ws2, p2),
        (ws2, p2, ws1, p1),
    ]

    for own_ws, own_project, other_ws, other_project in workspaces:
        with store._transaction(own_ws, OWNER) as cur:
            cur.execute("SELECT current_user")
            user_row = cur.fetchone()
            assert user_row is not None
            assert user_row["current_user"] == "omp_work_app"

            for table in (
                "omp_work.project_records",
                "omp_work.project_missions",
                "omp_work.project_history",
            ):
                cur.execute(f"SELECT workspace_id, project_id FROM {table}")
                rows = cur.fetchall()
                row_pairs = {(r["workspace_id"], r["project_id"]) for r in rows}
                assert (own_ws, own_project) in row_pairs
                assert (other_ws, other_project) not in row_pairs
                assert all(r["workspace_id"] == own_ws for r in rows)
                assert all(r["workspace_id"] != other_ws for r in rows)
                assert row_pairs == {(own_ws, own_project)}
