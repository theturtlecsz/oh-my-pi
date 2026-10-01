"""OMP-425-s04: client stop pauses autonomous work and changes no mission.

A client principal POSTs ``client/stop``. OMP-405's gate then refuses agent
work (``create_work_batch`` and ``set_mission_status``) with 409
``agent_stop_engaged``. The two missions and the pending decision stay as they
were. After the owner releases the stop, the refused batch applies and the
missions are still unchanged.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from omp_work.operations.capabilities import CLIENT_SCOPES
from test_mission_status_store import _draft, _events, _project
from test_workflow_service import _batch, _command, _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_CLIENT_TOKEN = "client-stop-token"
_STOP_REASON = "owner asked to stop"


def _client_headers(service, workspace_id) -> dict[str, str]:
    path = service.capabilities / "client.json"
    path.write_text(
        json.dumps(
            {
                "token": _CLIENT_TOKEN,
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(workspace_id)],
                "scopes": list(CLIENT_SCOPES),
            }
        )
    )
    path.chmod(0o600)
    return _owner_headers(workspace_id) | {"Authorization": f"Bearer {_CLIENT_TOKEN}"}


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
    return mission_id, body["result"]["mission"]


def _approve(service, workspace_id, mission_id):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def _create_pending_decision(service, workspace_id, project_id, mission_id):
    decision_id = uuid4()
    payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": str(mission_id),
        "question": "Publish the intake externally?",
        "why_it_matters": "The mission cannot continue without a ruling.",
        "risk_of_delay": "The window closes and the mission stalls.",
        "options": ["publish", "hold"],
        "evidence_refs": ["receipt:abc"],
        "default_if_any": "hold",
        "risk_of_each_choice": {
            "publish": "Irreversible external exposure.",
            "hold": "Missed deadline.",
        },
        "action_class": None,
        "resume_state": json.dumps({"step": 3}),
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "create_decision", "payload": payload},
    )
    assert status == 200, body
    assert body["result"]["decision_id"] == str(decision_id)
    assert body["result"]["project_id"] == str(project_id)
    assert body["result"]["mission_id"] == str(mission_id)
    return decision_id


def _client_json(service, workspace_id, path: str, headers: dict[str, str]) -> dict:
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/client/{path}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mission_core(mission: dict) -> dict:
    return {
        "status": mission["status"],
        "revision": mission["revision"],
        "transitions": mission["transitions"],
    }


def _decision_row(service, workspace_id, decision_id) -> dict:
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/decisions",
        params={"status": "pending"},
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200, response.text
    rows = [
        row
        for row in response.json()["decisions"]
        if row["decision_id"] == str(decision_id)
    ]
    assert len(rows) == 1, response.text
    return rows[0]


def _assert_frozen(service, workspace_id, headers, m1, m2, decision_id, snap) -> None:
    read_m1 = _client_json(service, workspace_id, f"missions/{m1}", headers)["result"]
    read_m2 = _client_json(service, workspace_id, f"missions/{m2}", headers)["result"]
    assert _mission_core(read_m1) == snap["m1"]
    assert _mission_core(read_m2) == snap["m2"]
    assert read_m1 == snap["m1_result"]
    assert read_m2 == snap["m2_result"]
    assert _events(service, workspace_id, m1) == snap["events_m1"]
    assert _events(service, workspace_id, m2) == snap["events_m2"]
    row = _decision_row(service, workspace_id, decision_id)
    assert row == snap["decision"]
    assert row["status"] == "pending"
    assert row["answer"] is None


def test_client_stop_pauses_autonomous_work_and_changes_no_mission(service) -> None:
    workspace_id, project_id = _project(service)
    _grant(service, workspace_id)

    m1, _submitted_m1 = _submit(service, workspace_id, project_id, "Approved mission")
    approved = _approve(service, workspace_id, m1)
    assert approved["status"] == "approved"

    m2, submitted_m2 = _submit(service, workspace_id, project_id, "Draft mission")
    assert submitted_m2["status"] == "awaiting_confirmation"

    decision_id = _create_pending_decision(service, workspace_id, project_id, m2)
    headers = _client_headers(service, workspace_id)

    read_m1 = _client_json(service, workspace_id, f"missions/{m1}", headers)
    read_m2 = _client_json(service, workspace_id, f"missions/{m2}", headers)
    assert read_m1["operation"] == "mission.status"
    assert read_m2["operation"] == "mission.status"
    snap = {
        "m1": _mission_core(read_m1["result"]),
        "m2": _mission_core(read_m2["result"]),
        "m1_result": read_m1["result"],
        "m2_result": read_m2["result"],
        "events_m1": _events(service, workspace_id, m1),
        "events_m2": _events(service, workspace_id, m2),
        "decision": _decision_row(service, workspace_id, decision_id),
    }
    assert snap["m1"]["status"] == "approved"
    assert snap["m2"]["status"] == "awaiting_confirmation"
    assert snap["decision"]["status"] == "pending"
    assert snap["decision"]["answer"] is None

    request_id = uuid4()
    stopped = service.client.post(
        f"/v1/workspaces/{workspace_id}/client/stop",
        headers=headers,
        json={"request_id": str(request_id), "payload": {"reason": _STOP_REASON}},
    )
    assert stopped.status_code == 200, stopped.text
    stopped_body = stopped.json()
    assert stopped_body["outcome"] == "applied"
    assert stopped_body["result"]["stopped"] is True

    stop_read = _client_json(service, workspace_id, "stop", headers)
    assert stop_read["operation"] == "stop.status"
    assert stop_read["state"] == "stopped"
    assert stop_read["result"]["stopped"] is True

    batch = _batch([{"client_ref": "root", "title": "blocked while stopped"}])
    status, body = _command(service, workspace_id, batch)
    assert status == 409, body
    assert body["error"]["code"] == "agent_stop_engaged"

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(m1),
                "target_status": "running",
                "cause_kind": "principal",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "agent_stop_engaged"

    _assert_frozen(service, workspace_id, headers, m1, m2, decision_id, snap)

    status, body = _command(
        service,
        workspace_id,
        {"type": "release_stop", "payload": {"reason": "operator resumed work"}},
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"

    status, body = _command(service, workspace_id, batch)
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["items"][0]["state"] == "BACKLOG"

    _assert_frozen(service, workspace_id, headers, m1, m2, decision_id, snap)
