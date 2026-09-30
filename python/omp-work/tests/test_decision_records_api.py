"""OMP-414: decision records HTTP API lifecycle."""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work.v1.server import create_app
from test_workflow_service import _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _envelope(
    workspace_id: UUID, command: dict[str, object], operation_id: UUID | None = None
) -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }


def _post(
    client: TestClient,
    workspace_id: UUID,
    command: dict[str, object],
    operation_id: UUID | None = None,
) -> tuple[int, dict[str, object], dict[str, object]]:
    envelope = _envelope(workspace_id, command, operation_id)
    response = client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id),
        json=envelope,
    )
    return response.status_code, response.json(), envelope


def _post_envelope(
    client: TestClient,
    workspace_id: UUID,
    envelope: dict[str, object],
) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id),
        json=envelope,
    )
    return response.status_code, response.json()


def _list(client: TestClient, workspace_id: UUID, **params: object) -> dict[str, object]:
    response = client.get(
        f"/v1/workspaces/{workspace_id}/decisions",
        params=params,
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_decision_records_api_lifecycle(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    project_id = uuid4()
    decision_id = uuid4()
    mission_id = str(uuid4())
    resume_state = json.dumps({"step": 3})

    payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": mission_id,
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
        "resume_state": resume_state,
    }

    # 1. create_decision with every payload field (mission_id M, 2 options, risk_of_each_choice, evidence_refs, default_if_any, resume_state, action_class None) -> 200; result decision_id/project_id/mission_id equal sent.
    create_status, create_body, _ = _post(
        service.client,
        workspace_id,
        {"type": "create_decision", "payload": payload},
    )
    assert create_status == 200, create_body
    create_result = create_body["result"]
    assert create_result["decision_id"] == str(decision_id)
    assert create_result["project_id"] == str(project_id)
    assert create_result["mission_id"] == mission_id

    # 2. GET /v1/workspaces/{ws}/decisions?status=pending: one row, every sent DecisionView field equal; with &mission_id=M: same row.
    pending_page = _list(service.client, workspace_id, status="pending")
    pending = pending_page["decisions"]
    assert len(pending) == 1, pending
    row = pending[0]
    assert row["decision_id"] == str(decision_id)
    assert row["project_id"] == str(project_id)
    assert row["mission_id"] == mission_id
    assert row["status"] == "pending"
    assert row["question"] == payload["question"]
    assert row["why_it_matters"] == payload["why_it_matters"]
    assert row["risk_of_delay"] == payload["risk_of_delay"]
    assert row["options"] == payload["options"]
    assert row["evidence_refs"] == payload["evidence_refs"]
    assert row["default_if_any"] == payload["default_if_any"]
    assert row["risk_of_each_choice"] == payload["risk_of_each_choice"]
    assert row["action_class"] == payload["action_class"]
    assert row["answer"] is None
    assert row["answered_at"] is None

    filtered_pending = _list(
        service.client, workspace_id, status="pending", mission_id=mission_id
    )["decisions"]
    assert filtered_pending == [row]

    # 3. client2 = TestClient(create_app(service.config, capabilities_dir=service.capabilities)); still pending.
    client2 = TestClient(create_app(service.config, capabilities_dir=service.capabilities))
    client2_pending = _list(
        client2, workspace_id, status="pending", mission_id=mission_id
    )["decisions"]
    assert client2_pending == [row]

    # 4. client2 answer_decision (owner) -> 200; result answer; json.loads(resume_state) == {"step": 3}.
    answer_command = {
        "type": "answer_decision",
        "payload": {"decision_id": str(decision_id), "answer": "publish"},
    }
    answer_status, answer_body, answer_envelope = _post(
        client2, workspace_id, answer_command
    )
    assert answer_status == 200, answer_body
    answer_result = answer_body["result"]
    assert answer_result["answer"] == "publish"
    assert json.loads(answer_result["resume_state"]) == {"step": 3}

    # 5. Re-POST identical answer envelope -> 200, result == step 4 result, receipt state "replayed"; pending&mission_id=M empty; status=answered: one row, answer set.
    replay_status, replay_body = _post_envelope(
        client2, workspace_id, answer_envelope
    )
    assert replay_status == 200, replay_body
    assert replay_body["result"] == answer_result
    assert replay_body["receipt"]["state"] == "replayed"
    assert (
        _list(client2, workspace_id, status="pending", mission_id=mission_id)["decisions"]
        == []
    )
    answered = _list(client2, workspace_id, status="answered")["decisions"]
    assert len(answered) == 1, answered
    assert answered[0]["decision_id"] == str(decision_id)
    assert answered[0]["answer"] == "publish"

    # 6. create_decision minus why_it_matters -> 400, error.code invalid_request; its id absent from unfiltered GET.
    bad_decision_id = uuid4()
    bad_payload = dict(payload)
    bad_payload["decision_id"] = str(bad_decision_id)
    bad_payload.pop("why_it_matters")
    bad_status, bad_body, _ = _post(
        client2,
        workspace_id,
        {"type": "create_decision", "payload": bad_payload},
    )
    assert bad_status == 400, bad_body
    assert bad_body["error"]["code"] == "invalid_request"
    unfiltered = _list(client2, workspace_id)["decisions"]
    assert not any(d["decision_id"] == str(bad_decision_id) for d in unfiltered)
    assert unfiltered == answered
