"""NSI-2 lost-response recovery and bounded mission intake coexistence."""
from __future__ import annotations

import json
import os
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest

from omp_work.v1.canonical import sha256, text_sha256
from test_mission_intake_store import _intake, _scope
from test_project_store import _workspace
from test_workflow_service import (
    OWNER, _command, _grant, _owner_headers, _seed_project, service,  # noqa: F401
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def intake_capability(service, workspace_id, *, kind="agent", scopes=("work.mutate", "work.read")):
    actor_id = uuid4()
    token = str(actor_id)
    capability = service.capabilities / f"{actor_id}.json"
    capability.write_text(json.dumps(dict(
        token=token, actor_id=str(actor_id), actor_kind=kind,
        workspaces=[str(workspace_id)], scopes=list(scopes),
    )))
    capability.chmod(0o600)
    return actor_id, token


@pytest.fixture
def mission(service):
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _grant(service, workspace_id)
    _workspace(service, workspace_id)
    _seed_project(service, workspace_id, project_id)
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id, mission_id, project_id, objective, status)"
            " VALUES (%s, %s, %s, 'Intake', 'draft')",
            (workspace_id, mission_id, project_id),
        )
    return workspace_id, project_id, mission_id


def start_intake(service, workspace_id, mission_id):
    return _command(service, workspace_id, {
        "type": "start_intake",
        "payload": {
            "run_id": "run-1", "expected_revision": 0,
            "mission_id": str(mission_id),
            "ontology": {"predicates": [], "dimensions": []}, "entities": [],
        },
    })


def test_ac2_nsi2_lost_response_reconciliation_and_replay(service, mission):
    workspace_id, _, mission_id = mission
    status, body = start_intake(service, workspace_id, mission_id)
    assert status == 200, body
    operation_id = uuid4()
    source = "Owner requires bounded memory growth."
    envelope = {
        "api_version": "work.omp.dev/v1", "workspace_id": str(workspace_id),
        "operation_id": str(operation_id), "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {
            "type": "register_intake_evidence",
            "payload": {
                "run_id": "run-1", "expected_revision": 1,
                "artifact": {
                    "id": "source", "uri": "https://example.test/requirements",
                    "content_hash": text_sha256(source), "revision": "1",
                    "actor": {"id": str(OWNER), "kind": "owner"},
                    "media_type": "text/plain",
                },
                "source": {"text": source, "sha256": text_sha256(source)},
            },
        },
    }
    headers = _owner_headers(workspace_id)
    # CommandResponse validation runs only after service.execute commits.
    with patch("omp_work.v1.server.CommandResponse.model_validate",
               side_effect=RuntimeError("lost response after commit")) as validate:
        lost = service.client.post("/v1/commands", headers=headers, json=envelope)
    assert validate.call_count == 1
    assert lost.status_code == 503
    error = lost.json()["error"]
    assert error["code"] == "unavailable"
    assert any("outcome unknown, reconcile by operation_id" in diagnostic
               for diagnostic in error["diagnostics"])

    reconciled = service.client.get(f"/v1/operations/{operation_id}", headers=headers)
    assert reconciled.status_code == 200, reconciled.json()
    stored = reconciled.json()
    assert stored["receipt"]["state"] == "applied"
    assert stored["result"] == {
        "type": "register_intake_evidence", "run_id": "run-1",
        "expected_revision": 1, "revision": 2,
    }
    assert stored["receipt"]["result_sha256"] == sha256(stored["result"])

    replay = service.client.post("/v1/commands", headers=headers, json=envelope)
    assert replay.status_code == 200, replay.json()
    replayed = replay.json()
    assert replayed["receipt"]["state"] == "replayed"
    assert replayed["result"] == stored["result"]
    assert replayed["receipt"]["result_sha256"] == stored["receipt"]["result_sha256"]

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM omp_work.intake_revisions WHERE workspace_id=%s AND run_id='run-1'",
            (workspace_id,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, operation_id),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id='run-1'",
            (workspace_id,),
        ).fetchone() == (1,)


def test_ac3_bounded_mission_intake_coexistence(service, mission):
    workspace_id, project_id, mission_id = mission
    _, token = intake_capability(
        service, workspace_id, kind="client", scopes=("work.client", "work.mutate", "work.read"),
    )
    response = service.client.post(
        f"/v1/workspaces/{workspace_id}/client/mission-intake",
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
        json={
            "request_id": str(uuid4()),
            "payload": {
                "mission_id": str(mission_id), "base_revision": None,
                "intake": _intake(), "scope": _scope(project_id),
            },
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["contract"] == "client.omp.dev/v1"
    assert body["outcome"] == "applied"
    assert body["operation"] == "mission.intake"
    assert body["result"]["type"] == "draft_mission_intake"
    status, body = start_intake(service, workspace_id, mission_id)
    assert status == 200, body
