"""OMP-562: history can reference only a mission in the same workspace."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from omp_work.v1.store import PostgresWorkStore
from test_project_store import _insert_project, _workspace
from test_workflow_service import OWNER

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.mark.parametrize("reference", ["missing", "other_workspace", "local", "none"])
def test_history_mission_reference_requires_workspace_mission(service, reference) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, project_id, "history")

    if reference == "other_workspace":
        other_workspace, other_project = uuid4(), uuid4()
        _workspace(service, other_workspace)
        _insert_project(service, other_workspace, other_project, "other")
        store.link_mission(other_workspace, OWNER, other_project, mission_id, "Other", "draft")
    elif reference == "local":
        store.link_mission(workspace_id, OWNER, project_id, mission_id, "Local", "draft")
    elif reference == "none":
        mission_id = None

    if reference in ("missing", "other_workspace"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            store.append_history(workspace_id, OWNER, project_id, "test", "History", mission_id=mission_id)
        assert store.read_project(workspace_id, OWNER, project_id)["history"] == []
    else:
        store.append_history(workspace_id, OWNER, project_id, "test", "History", mission_id=mission_id)
        history = store.read_project(workspace_id, OWNER, project_id)["history"]
        assert [row["mission_id"] for row in history] == [str(mission_id) if mission_id else None]


def test_referenced_mission_cannot_be_deleted(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, project_id, "history")
    store.link_mission(workspace_id, OWNER, project_id, mission_id, "Local", "draft")
    store.append_history(workspace_id, OWNER, project_id, "test", "History", mission_id=mission_id)

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                "DELETE FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
                (workspace_id, mission_id),
            )
    history = store.read_project(workspace_id, OWNER, project_id)["history"]
    assert [row["mission_id"] for row in history] == [str(mission_id)]
