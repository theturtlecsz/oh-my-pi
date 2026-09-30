"""OMP-426-s07: answer_mission_draft wired into PostgresWorkStore."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.v1.canonical import sha256, text_sha256
from test_workflow_service import OWNER, _command, _create, _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = {
    "usd": "10.00",
    "tokens": 1000,
    "wall_clock_seconds": 3600,
    "max_subagents": 4,
}

_ITEM_BUDGET = {
    "usd": "2.00",
    "tokens": 100,
    "wall_clock_seconds": 60,
    "max_subagents": 1,
}


def _project(service, provenance: dict | None = None) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Mission project",
                json.dumps(provenance or {}),
            ),
        )
    return workspace_id, project_id


def _grant_agent(service, workspace_id: UUID) -> tuple[str, UUID]:
    agent_file = service.capabilities / "agent.json"
    agent_id = uuid4()
    token = "agent-token"
    if agent_file.exists():
        data = json.loads(agent_file.read_text())
        if str(workspace_id) not in data["workspaces"]:
            data["workspaces"].append(str(workspace_id))
            agent_file.write_text(json.dumps(data))
            agent_file.chmod(0o600)
        return token, UUID(data["actor_id"])
    agent_file.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(agent_id),
                "actor_kind": "agent",
                "workspaces": [str(workspace_id)],
                "scopes": [
                    "work.read",
                    "work.mutate",
                    "work.approve",
                    "work.execute",
                ],
            }
        )
    )
    agent_file.chmod(0o600)
    return token, agent_id


def _instruction(**overrides: object) -> dict:
    instr = {
        "text": "Confirm this mission draft.",
        "provenance": {
            "channel": "owner-chat",
            "message_ref": "msg-426-1",
            "received_at": "2026-09-30T10:00:00+00:00",
        },
    }
    instr.update(overrides)
    return instr


def _intake(
    *,
    goal: str = "Bound memory growth",
    criteria: tuple[tuple[str, str, str | None], ...] = (
        ("ac-1", "RSS <= 256MB", "automated_test"),
        ("ac-2", "Latency <= 5ms", "automated_test"),
    ),
) -> dict:
    text = "Bound the memory growth of the cache eviction path."
    return {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {"id": "goal-1", "statement": goal, "source_span_ids": []},
        "acceptance_criteria": [
            {
                "id": criterion_id,
                "statement": outcome,
                "source_span_ids": [],
                "observable_outcome": outcome,
                "oracle": oracle,
            }
            for criterion_id, outcome, oracle in criteria
        ],
    }


def _scope(project_id: UUID, **overrides) -> dict:
    scope = {
        "project_id": str(project_id),
        "repositories": ["repo-a", "repo-b"],
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    scope.update(overrides)
    return scope


def _draft_mission_intake(
    service,
    workspace_id: UUID,
    mission_id: UUID,
    project_id: UUID,
    *,
    base_revision: int | None = None,
    intake: dict | None = None,
    scope: dict | None = None,
    instruction: dict | None = None,
    token: str = "owner-token",
) -> tuple[int, dict]:
    payload: dict = {
        "mission_id": str(mission_id),
        "base_revision": base_revision,
        "intake": _intake() if intake is None else intake,
        "scope": _scope(project_id) if scope is None else scope,
    }
    if instruction is not None:
        payload["instruction"] = instruction
    return _command(
        service,
        workspace_id,
        {"type": "draft_mission_intake", "payload": payload},
        token=token,
    )


def _answer_mission_draft(
    service,
    workspace_id: UUID,
    mission_id: UUID,
    decision_id: UUID,
    revision: int,
    answer: dict,
    *,
    instruction: dict | None = None,
    token: str = "owner-token",
) -> tuple[int, dict]:
    payload: dict = {
        "mission_id": str(mission_id),
        "decision_id": str(decision_id),
        "revision": revision,
        "answer": answer,
        "instruction": _instruction() if instruction is None else instruction,
    }
    return _command(
        service,
        workspace_id,
        {"type": "answer_mission_draft", "payload": payload},
        token=token,
    )


def _seed_budget(
    service, workspace_id: UUID, *, work_id: UUID, revision_id: UUID, budget: dict
) -> None:
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(OWNER),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _link(service, workspace_id: UUID, mission_id: UUID, work_id: UUID) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
    )


def _get_mission(client, workspace_id: UUID, mission_id: UUID):
    return client.get(
        f"/v1/workspaces/{workspace_id}/missions/{mission_id}",
        headers=_owner_headers(workspace_id),
    )


def _get_decisions(client, workspace_id: UUID, status: str = "pending"):
    return client.get(
        f"/v1/workspaces/{workspace_id}/decisions?status={status}",
        headers=_owner_headers(workspace_id),
    )


def test_confirm_approves_mission_with_decision_basis_and_allows_link(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "confirm"},
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "approved"
    assert result["next_decision_id"] is None
    mission = result["mission"]
    assert mission["status"] == "approved"
    assert mission["revision"] == 1
    assert mission["approved_scope"]["basis_kind"] == "decision"
    assert mission["approved_scope"]["basis_id"] == str(decision_id)

    item = _create(service, workspace_id, "Test Item")
    _seed_budget(
        service,
        workspace_id,
        work_id=UUID(item["work_id"]),
        revision_id=UUID(item["revision_id"]),
        budget=_ITEM_BUDGET,
    )
    link_status, link_body = _link(service, workspace_id, mission_id, UUID(item["work_id"]))
    assert link_status == 200, link_body
    assert link_body["result"]["type"] == "link_mission_work"
    assert any(link["work_id"] == item["work_id"] for link in link_body["result"]["mission"]["links"])


def test_reject_abandons_mission(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "reject"},
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "rejected"
    assert result["mission"]["status"] == "abandoned"

    read = _get_mission(service.client, workspace_id, mission_id)
    assert read.status_code == 200
    assert read.json()["status"] == "abandoned"


def test_edited_draft_approves_at_revision_plus_one_with_owner_criteria(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    owner_draft = {
        "project_id": str(project_id),
        "objective": "Owner modified objective",
        "acceptance_criteria": ["Custom owner AC 1", "Custom owner AC 2"],
        "repositories": ["repo-custom"],
        "requested_capabilities": ["read", "write"],
        "approval_classes": ["tier-2"],
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "edited_draft", "draft": owner_draft},
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "approved"
    assert result["mission"]["revision"] == 2
    assert result["mission"]["status"] == "approved"
    assert result["mission"]["objective"] == "Owner modified objective"
    assert result["mission"]["acceptance_criteria"] == ["Custom owner AC 1", "Custom owner AC 2"]
    assert result["mission"]["approved_scope"]["revision"] == 2
    assert result["mission"]["approved_scope"]["basis_kind"] == "decision"
    assert result["mission"]["approved_scope"]["basis_id"] == str(decision_id)


def test_note_answers_decision_as_note_leaves_awaiting_confirmation_and_creates_new_pending_decision(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "note", "text": "Please bound cache size instead."},
    )
    assert status == 200, body
    result = body["result"]
    assert result["outcome"] == "noted"
    next_decision_id = result["next_decision_id"]
    assert next_decision_id is not None
    assert UUID(next_decision_id) != decision_id

    mission = result["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["approved_scope"] is None
    assert mission["revision"] == 2
    assert any(ref == f"owner_note:{decision_id}" for ref in mission["context_refs"])

    answered_resp = _get_decisions(service.client, workspace_id, status="answered")
    assert answered_resp.status_code == 200
    answered = answered_resp.json()["decisions"]
    matching_answered = [d for d in answered if d["decision_id"] == str(decision_id)]
    assert len(matching_answered) == 1
    assert matching_answered[0]["answer"] == "note"
    assert matching_answered[0]["status"] == "answered"

    pending_resp = _get_decisions(service.client, workspace_id, status="pending")
    assert pending_resp.status_code == 200
    pending = pending_resp.json()["decisions"]
    matching_pending = [d for d in pending if d["decision_id"] == str(next_decision_id)]
    assert len(matching_pending) == 1
    assert matching_pending[0]["status"] == "pending"
    assert matching_pending[0]["evidence_refs"] == [f"mission:{mission_id}@2"]


def test_get_operation_returns_instruction_and_provenance(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    instruction = _instruction(
        text="Owner confirms this intake revision.",
        provenance={
            "channel": "slack-direct",
            "message_ref": "slack-msg-789",
            "received_at": "2026-09-30T10:15:00+00:00",
        },
    )
    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "confirm"},
        instruction=instruction,
    )
    assert status == 200, body
    operation_id = body["receipt"]["operation_id"]

    op_resp = service.client.get(
        f"/v1/operations/{operation_id}",
        headers=_owner_headers(workspace_id),
    )
    assert op_resp.status_code == 200
    op_body = op_resp.json()
    assert op_body["command_type"] == "answer_mission_draft"
    op_result = op_body["result"]
    assert op_result["instruction"]["text"] == "Owner confirms this intake revision."
    assert op_result["instruction"]["provenance"]["channel"] == "slack-direct"
    assert op_result["instruction"]["provenance"]["message_ref"] == "slack-msg-789"
    assert op_result["instruction"]["provenance"]["received_at"] == "2026-09-30T10:15:00Z"


def test_agent_kind_answer_is_forbidden(service) -> None:
    workspace_id, project_id = _project(service)
    agent_token, _ = _grant_agent(service, workspace_id)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "confirm"},
        token=agent_token,
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden"


def test_stale_revision_returns_revision_conflict(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _answer_mission_draft(
        service,
        workspace_id,
        mission_id,
        decision_id,
        99,
        {"kind": "option", "option": "confirm"},
    )
    assert status == 409
    assert body["error"]["code"] == "revision_conflict"


def test_answer_decision_on_intake_decision_refuses_invalid_request(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft_status, draft_body = _draft_mission_intake(
        service, workspace_id, mission_id, project_id
    )
    assert draft_status == 200, draft_body
    decision_id = UUID(draft_body["result"]["decision_id"])

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(decision_id),
                "answer": "confirm",
            },
        },
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "answer_with_answer_mission_draft" in body["error"]["diagnostics"]
