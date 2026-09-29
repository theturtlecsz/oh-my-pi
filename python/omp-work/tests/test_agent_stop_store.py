"""OMP-405: PostgreSQL integration tests for agent stop persistence and idempotency."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest

from test_workflow_service import _grant
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.store import PostgresWorkStore

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _envelope(workspace_id: UUID, command_type: str, reason: str) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": command_type, "payload": {"reason": reason}},
        }
    )


def test_agent_stop_store_lifecycle_and_idempotence(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    grokbot_actor_id = uuid4()
    owner_actor_id = uuid4()

    # Fresh workspace: not stopped
    status = store.stop_status(workspace_id, owner_actor_id)
    assert status["stopped"] is False
    assert status["reason"] is None
    assert status["changed_at"] is None
    assert status["changed_by_actor_kind"] is None
    assert status["workspace_id"] == str(workspace_id)

    # Engage as actor_kind "grokbot" -> stopped with its reason, grokbot, changed_at set
    engage_envelope = _envelope(workspace_id, "engage_stop", "runaway loop detected")
    receipt, result = store.execute(
        engage_envelope,
        actor_id=grokbot_actor_id,
        actor_kind="grokbot",
        required_scope="work.stop",
    )
    assert receipt.state == OperationState.APPLIED
    assert result == {
        "type": "engage_stop",
        "stopped": True,
        "reason": "runaway loop detected",
    }

    status_after_engage = store.stop_status(workspace_id, grokbot_actor_id)
    assert status_after_engage["stopped"] is True
    assert status_after_engage["reason"] == "runaway loop detected"
    assert status_after_engage["changed_by_actor_kind"] == "grokbot"
    assert status_after_engage["changed_at"] is not None

    # Owner release -> not stopped
    release_envelope = _envelope(workspace_id, "release_stop", "operator resumed work")
    receipt, result = store.execute(
        release_envelope,
        actor_id=owner_actor_id,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert receipt.state == OperationState.APPLIED
    assert result == {
        "type": "release_stop",
        "stopped": False,
        "reason": "operator resumed work",
    }

    status_after_release = store.stop_status(workspace_id, owner_actor_id)
    assert status_after_release["stopped"] is False
    assert status_after_release["reason"] == "operator resumed work"
    assert status_after_release["changed_by_actor_kind"] == "owner"
    assert status_after_release["changed_at"] is not None

    # A second PostgresWorkStore from the same config reads the same state
    store2 = PostgresWorkStore(service.config)
    status2 = store2.stop_status(workspace_id, owner_actor_id)
    assert status2 == status_after_release

    # Replaying the engage envelope adds no event
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
        assert row is not None
        events_before = int(row[0])

    replay_receipt, replay_result = store.execute(
        engage_envelope,
        actor_id=grokbot_actor_id,
        actor_kind="grokbot",
        required_scope="work.stop",
    )
    assert replay_receipt.state == OperationState.REPLAYED
    assert replay_result == {
        "type": "engage_stop",
        "stopped": True,
        "reason": "runaway loop detected",
    }

    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
        assert row is not None
        events_after = int(row[0])

    assert events_after == events_before
    assert store.stop_status(workspace_id, owner_actor_id)["stopped"] is False
