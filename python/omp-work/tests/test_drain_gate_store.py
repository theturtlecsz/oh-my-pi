"""OMP-519-s01: drain gate in-flight mission inspection."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from omp_work.operations.config import OperationsConfig
from omp_work.operations.drain import in_flight
from test_mission_status_store import _draft, _project
from test_workflow_service import OWNER, _command

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_DRAIN_KEYS = {
    "mission_id",
    "project_id",
    "objective",
    "status",
    "created_at",
}


def _setup_credentials(config: OperationsConfig, workspace_id: UUID) -> None:
    ws_path = config.secret_path("workspace-id")
    ws_path.write_text(str(workspace_id))
    ws_path.chmod(0o600)
    actor_path = config.secret_path("operator-actor-id")
    actor_path.write_text(str(OWNER))
    actor_path.chmod(0o600)


def _submit(service, workspace_id, project_id, mission_id, objective: str = "Test mission"):
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


def _approve(service, workspace_id, mission_id, revision: int = 1):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": revision,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def _set_status(service, workspace_id, mission_id, target_status: str):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_mission_status",
            "payload": {
                "mission_id": str(mission_id),
                "target_status": target_status,
                "cause_kind": "principal",
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def test_drain_gate_in_flight_lifecycle(service) -> None:
    workspace_id, project_id = _project(service)
    _setup_credentials(service.config, workspace_id)

    # 1. No missions: []
    assert in_flight(service.config) == []

    # 2. One mission approved and set running: listed once with its mission_id
    m1 = uuid4()
    _submit(service, workspace_id, project_id, m1, "Objective 1")
    _approve(service, workspace_id, m1)
    _set_status(service, workspace_id, m1, "running")

    running = in_flight(service.config)
    assert len(running) == 1
    entry = running[0]
    assert set(entry.keys()) == _DRAIN_KEYS
    assert entry["mission_id"] == str(m1)
    assert entry["project_id"] == str(project_id)
    assert entry["objective"] == "Objective 1"
    assert entry["status"] == "running"
    assert entry["created_at"]

    # 3. That mission then paused: []
    _set_status(service, workspace_id, m1, "paused")
    assert in_flight(service.config) == []

    # 4. Two missions, one running and one paused: only the running one
    m2 = uuid4()
    _submit(service, workspace_id, project_id, m2, "Objective 2")
    _approve(service, workspace_id, m2)
    _set_status(service, workspace_id, m2, "running")

    running_after = in_flight(service.config)
    assert len(running_after) == 1
    entry2 = running_after[0]
    assert set(entry2.keys()) == _DRAIN_KEYS
    assert entry2["mission_id"] == str(m2)
    assert entry2["project_id"] == str(project_id)
    assert entry2["objective"] == "Objective 2"
    assert entry2["status"] == "running"
    assert entry2["created_at"]


def test_drain_gate_ordering_and_isolation(service) -> None:
    workspace_a, project_a = _project(service)
    workspace_b, project_b = _project(service)
    _setup_credentials(service.config, workspace_a)

    # Create two running missions in workspace_a
    m1 = uuid4()
    _submit(service, workspace_a, project_a, m1, "Mission A1")
    _approve(service, workspace_a, m1)
    _set_status(service, workspace_a, m1, "running")

    m2 = uuid4()
    _submit(service, workspace_a, project_a, m2, "Mission A2")
    _approve(service, workspace_a, m2)
    _set_status(service, workspace_a, m2, "running")

    # Create a running mission in workspace_b
    mb = uuid4()
    _submit(service, workspace_b, project_b, mb, "Mission B")
    _approve(service, workspace_b, mb)
    _set_status(service, workspace_b, mb, "running")

    # workspace_a only sees its own running missions in order
    running = in_flight(service.config)
    assert [m["mission_id"] for m in running] == [str(m1), str(m2)]
