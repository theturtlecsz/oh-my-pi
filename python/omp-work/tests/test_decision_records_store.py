"""OMP-414: PostgreSQL integration tests for decision record persistence."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest

from test_workflow_service import _grant
from omp_work.v1.api_models import DecisionView
from omp_work.v1.models import CommandEnvelope, CreateDecisionCommand, OperationState
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _payload(decision_id: UUID, project_id: UUID, mission_id: str) -> dict[str, object]:
    return {
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
        "action_class": "publish_as_owner",
        "resume_state": "awaiting-publication",
    }


def _envelope(workspace_id: UUID, command: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


def _applied_events(service, workspace_id: UUID, event_type: str) -> int:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_audit.domain_events "
            "WHERE workspace_id = %s AND event_type = %s AND outcome = 'applied'",
            (workspace_id, event_type),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_decision_records_persist_answer_and_replay(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    project_id = uuid4()
    decision_id = uuid4()
    other_id = uuid4()

    assert store.decisions(workspace_id, actor_id)["decisions"] == []

    body = _payload(decision_id, project_id, "OMP-414")
    create_envelope = _envelope(
        workspace_id, {"type": "create_decision", "payload": body}
    )
    receipt, result = store.execute(
        create_envelope,
        actor_id=actor_id,
        actor_kind="owner",
        required_scope="work.mutate",
    )
    assert receipt.state == OperationState.APPLIED
    dumped = CreateDecisionCommand.model_validate(
        {"type": "create_decision", "payload": body}
    ).payload.model_dump(mode="json")
    assert result == {
        "type": "create_decision",
        "status": "pending",
        "decision": dumped,
    }

    with pytest.raises(WorkStoreError) as duplicate:
        store.execute(
            _envelope(workspace_id, {"type": "create_decision", "payload": body}),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.mutate",
        )
    assert duplicate.value.code == "revision_conflict"
    assert duplicate.value.diagnostics == ("decision_exists",)
    assert _applied_events(service, workspace_id, "create_decision") == 1

    other = _payload(other_id, project_id, "OMP-415")
    store.execute(
        _envelope(workspace_id, {"type": "create_decision", "payload": other}),
        actor_id=actor_id,
        actor_kind="owner",
        required_scope="work.mutate",
    )

    pending = store.decisions(
        workspace_id, actor_id, status="pending", mission_id="OMP-414"
    )["decisions"]
    assert len(pending) == 1
    view = DecisionView.model_validate(pending[0])
    assert view.status == "pending"
    assert view.answer is None
    assert view.answered_at is None
    assert view.mission_id == "OMP-414"
    assert view.action_class == "publish_as_owner"
    assert pending[0]["options"] == ["publish", "hold"]
    assert "resume_state" not in pending[0]
    assert [
        row["decision_id"]
        for row in store.decisions(workspace_id, actor_id)["decisions"]
    ] == [str(decision_id), str(other_id)]

    store2 = PostgresWorkStore(service.config)
    assert (
        store2.decisions(
            workspace_id, actor_id, status="pending", mission_id="OMP-414"
        )["decisions"]
        == pending
    )

    with pytest.raises(WorkStoreError) as unknown:
        store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {"decision_id": str(uuid4()), "answer": "publish"},
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
    assert unknown.value.code == "invalid_request"
    assert unknown.value.diagnostics == ("decision_not_found",)

    with pytest.raises(WorkStoreError) as not_an_option:
        store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {"decision_id": str(decision_id), "answer": "maybe"},
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
    assert not_an_option.value.code == "invalid_request"
    assert not_an_option.value.diagnostics == ("answer_not_an_option",)
    assert _applied_events(service, workspace_id, "answer_decision") == 0
    assert (
        store.decisions(workspace_id, actor_id, status="pending", mission_id="OMP-414")[
            "decisions"
        ]
        == pending
    )

    answer_envelope = _envelope(
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {"decision_id": str(decision_id), "answer": "publish"},
        },
    )
    answer_receipt, answer = store.execute(
        answer_envelope,
        actor_id=actor_id,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert answer_receipt.state == OperationState.APPLIED
    assert answer == {
        "type": "answer_decision",
        "decision_id": str(decision_id),
        "mission_id": "OMP-414",
        "answer": "publish",
        "resume_state": "awaiting-publication",
    }
    assert (
        store.decisions(workspace_id, actor_id, status="pending", mission_id="OMP-414")[
            "decisions"
        ]
        == []
    )
    still_pending = store.decisions(
        workspace_id, actor_id, status="pending", mission_id="OMP-415"
    )["decisions"]
    assert len(still_pending) == 1
    assert still_pending[0]["decision_id"] == str(other_id)

    answered = store.decisions(workspace_id, actor_id, mission_id="OMP-414")[
        "decisions"
    ]
    assert len(answered) == 1
    answered_view = DecisionView.model_validate(answered[0])
    assert answered_view.status == "answered"
    assert answered_view.answer == "publish"
    assert answered_view.answered_at is not None
    assert answered_view.mission_id == "OMP-414"

    replay_receipt, replay = store.execute(
        answer_envelope,
        actor_id=actor_id,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert replay_receipt.state == OperationState.REPLAYED
    assert replay == answer
    assert _applied_events(service, workspace_id, "answer_decision") == 1

    with pytest.raises(WorkStoreError) as second:
        store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {"decision_id": str(decision_id), "answer": "hold"},
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
    assert second.value.code == "revision_conflict"
    assert second.value.diagnostics == ("decision_already_answered",)
    assert _applied_events(service, workspace_id, "answer_decision") == 1
