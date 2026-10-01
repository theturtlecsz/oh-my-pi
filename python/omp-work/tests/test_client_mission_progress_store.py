"""OMP-425-s03: client project status lists contract mission progress."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from omp_work.operations.capabilities import CLIENT_SCOPES
from test_mission_status_store import _draft, _project
from test_workflow_service import _command, _owner_headers, _seed_project

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _client_headers(service, workspace_id) -> dict[str, str]:
    token = "client-token"
    path = service.capabilities / "client.json"
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(workspace_id)],
                "scopes": list(CLIENT_SCOPES),
            }
        )
    )
    path.chmod(0o600)
    return _owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"}


def _submit(service, workspace_id, project_id, objective: str):
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, objective=objective),
            },
        },
    )
    assert status == 200, body
    return mission_id


def _get(service, workspace_id, path: str, headers: dict[str, str]) -> dict:
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/client/{path}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_client_project_status_lists_mission_progress(service) -> None:
    workspace_id, project_a = _project(service)
    project_b = uuid4()
    project_empty = uuid4()
    _seed_project(service, workspace_id, project_b, "Project B")
    _seed_project(service, workspace_id, project_empty, "Empty")

    mission_a1 = _submit(service, workspace_id, project_a, "Alpha one")
    mission_a2 = _submit(service, workspace_id, project_a, "Alpha two")
    mission_b = _submit(service, workspace_id, project_b, "Beta only")

    headers = _client_headers(service, workspace_id)
    progress = _get(service, workspace_id, f"projects/{project_a}/status", headers)[
        "mission_progress"
    ]
    assert [row["mission_id"] for row in progress] == sorted(
        (str(mission_a1), str(mission_a2))
    )
    assert str(mission_b) not in {row["mission_id"] for row in progress}

    before = {row["mission_id"]: row for row in progress}
    for mission_id, row in before.items():
        mission = _get(service, workspace_id, f"missions/{mission_id}", headers)
        assert row["status"] == mission["status"]
        assert row["revision"] == mission["revision"]
        assert row["objective"] == mission["objective"]
        assert row["updated_at"]

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_a1),
                "target_status": "abandoned",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body

    again = _get(service, workspace_id, f"projects/{project_a}/status", headers)[
        "mission_progress"
    ]
    updated = next(row for row in again if row["mission_id"] == str(mission_a1))
    mission = _get(service, workspace_id, f"missions/{mission_a1}", headers)
    assert updated["status"] == mission["status"] == "abandoned"
    assert updated["revision"] == mission["revision"]
    assert before[str(mission_a1)]["status"] != updated["status"]

    empty = _get(service, workspace_id, f"projects/{project_empty}/status", headers)
    assert empty["mission_progress"] == []
