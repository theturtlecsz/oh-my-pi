"""OMP-423-s03: read_project.open_missions is the project's live missions.

Mission state is the latest applied mission event (v1/missions.open_missions),
not omp_work.project_missions, which only what submit_mission writes reaches. A
mission whose latest snapshot is terminal (completed, failed, abandoned) drops
out of the list.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from omp_work import contract_sha256
from omp_work.operations.capabilities import CLIENT_SCOPES
from omp_work.v1.store import PostgresWorkStore
from test_mission_status_store import _draft, _project
from test_workflow_service import OWNER, _command

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_CLIENT_TOKEN = "open-missions-client-token"

_OPEN_MISSION_KEYS = {
    "mission_id",
    "objective",
    "status",
    "priority",
    "kind",
    "revision",
    "created_at",
}


def _submit(service, workspace_id, project_id, mission_id, objective):
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
    return body["result"]["mission"]


def _abandon(service, workspace_id, mission_id):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "abandoned",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body


def _block(service, workspace_id, mission_id):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": "blocked",
                "cause_kind": "policy_rule",
                "policy_rule_id": "rule.open_missions",
            },
        },
    )
    assert status == 200, body


def _client_capability(service, workspace_ids) -> None:
    path = service.capabilities / "client.json"
    path.write_text(
        json.dumps(
            {
                "token": _CLIENT_TOKEN,
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(workspace_id) for workspace_id in workspace_ids],
                "scopes": list(CLIENT_SCOPES),
            }
        )
    )
    path.chmod(0o600)


def _client_status(service, workspace_id, project_id):
    return service.client.get(
        f"/v1/workspaces/{workspace_id}/client/projects/{project_id}/status",
        headers={
            "Authorization": f"Bearer {_CLIENT_TOKEN}",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )


def test_open_missions_tracks_latest_event_and_the_project(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_a, project_a = _project(service)
    workspace_b, project_b = _project(service)

    live_a = uuid4()
    abandoned_a = uuid4()
    _submit(service, workspace_a, project_a, abandoned_a, "Abandoned objective")
    # created_at is clock_timestamp(); a submit after it orders the live mission
    # after the abandoned one deterministically without sleeping.
    _submit(service, workspace_a, project_a, live_a, "Live objective A")
    _abandon(service, workspace_a, abandoned_a)

    live_b = uuid4()
    _submit(service, workspace_b, project_b, live_b, "Live objective B")

    view_a = store.read_project(workspace_a, OWNER, project_a)
    assert [mission["mission_id"] for mission in view_a["open_missions"]] == [
        str(live_a)
    ]
    entry = view_a["open_missions"][0]
    assert set(entry) == _OPEN_MISSION_KEYS
    assert entry["mission_id"] == str(live_a)
    assert entry["objective"] == "Live objective A"
    assert entry["status"] == "awaiting_confirmation"
    assert entry["priority"] == 2
    assert entry["kind"] == "engineering.execute"
    assert entry["revision"] == 1
    assert entry["created_at"]

    # project_missions is unchanged; only open_missions follows the events.
    assert [mission["mission_id"] for mission in view_a["missions"]] == []

    view_b = store.read_project(workspace_b, OWNER, project_b)
    assert [mission["mission_id"] for mission in view_b["open_missions"]] == [
        str(live_b)
    ]

    # A newer status event wins over the submit snapshot.
    _block(service, workspace_a, live_a)
    refreshed = store.read_project(workspace_a, OWNER, project_a)
    assert [mission["mission_id"] for mission in refreshed["open_missions"]] == [
        str(live_a)
    ]
    assert refreshed["open_missions"][0]["status"] == "blocked"

    _client_capability(service, (workspace_a, workspace_b))
    response = _client_status(service, workspace_a, project_a)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operation"] == "project.status"
    assert body["result"]["open_missions"] == refreshed["open_missions"]


def test_open_missions_is_empty_without_events(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id, project_id = _project(service)
    view = store.read_project(workspace_id, OWNER, project_id)
    assert view["open_missions"] == []
