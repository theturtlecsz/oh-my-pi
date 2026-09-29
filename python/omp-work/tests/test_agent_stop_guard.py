"""OMP-405: while an agent stop is engaged, the ledger refuses agent work.

Unit layer (no database): ``allowed_while_stopped`` is the pure predicate the
store consults — stop control stays usable so the owner can always release, a
grant's pause/stop/cancel transitions stay usable so a live agent can halt, and
everything else (here ``create_work_batch`` and a resume to ``active``) is
refused.

PostgreSQL layer (``OMP_WORK_POSTGRES_INTEGRATION=1``): an engaged stop refuses
a new ``create_work_batch`` with 409 ``agent_stop_engaged`` writing neither item
nor idempotency row; a replay of an operation applied before the stop still
returns its stored receipt; and after the owner releases, the same new command
applies.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.v1.agent_stop import allowed_while_stopped
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.store import PostgresWorkStore

from test_workflow_service import _batch, _command, _grant

pytest_plugins = ["test_workflow_service"]


def _envelope(workspace_id: UUID, command: dict, operation_id=None) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(operation_id or uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


def _stop_command(command_type: str, reason: str) -> dict:
    return {"type": command_type, "payload": {"reason": reason}}


def _set_execution_state(target_state: str) -> dict:
    return {
        "type": "set_execution_state",
        "payload": {
            "grant_id": str(uuid4()),
            "expected_grant_version": 1,
            "target_state": target_state,
            "judge_sha256": "0" * 64,
        },
    }


def _batch_command(title: str) -> dict:
    return _batch([{"client_ref": "root", "title": title}])


# --- unit: the predicate, no database -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        _stop_command("engage_stop", "runaway loop"),
        _stop_command("release_stop", "all clear"),
        _set_execution_state("paused"),
        _set_execution_state("stopped"),
        _set_execution_state("canceled"),
    ],
)
def test_predicate_allows_only_stop_control_and_grant_teardown(command: dict) -> None:
    assert allowed_while_stopped(_envelope(uuid4(), command).command) is True


@pytest.mark.parametrize(
    "command",
    [_batch_command("work"), _set_execution_state("active")],
)
def test_predicate_refuses_agent_work(command: dict) -> None:
    assert allowed_while_stopped(_envelope(uuid4(), command).command) is False


# --- PostgreSQL: the guard inside the command transaction -----------------


def _owner_actor_id(service) -> UUID:
    owner = service.capabilities / "owner.json"
    return UUID(json.loads(owner.read_text())["actor_id"])


def _count_rows(service, table: str, workspace_id: UUID) -> int:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            f"SELECT count(*) FROM {table} WHERE workspace_id = %s", (workspace_id,)
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_stopped_workspace_refuses_new_work_and_replays_before_release(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    owner = _owner_actor_id(service)
    grokbot = uuid4()

    # An operation applied before the stop — its receipt must survive the stop.
    pre_stop = _envelope(workspace_id, _batch_command("pre-stop work"))
    pre_receipt, pre_result = store.execute(
        pre_stop, actor_id=owner, actor_kind="owner", required_scope="work.mutate"
    )
    assert pre_receipt.state == OperationState.APPLIED

    # Engage the stop.
    engage = _envelope(workspace_id, _stop_command("engage_stop", "runaway loop"))
    engage_receipt, _ = store.execute(
        engage, actor_id=grokbot, actor_kind="grokbot", required_scope="work.stop"
    )
    assert engage_receipt.state == OperationState.APPLIED
    stop_state = store.stop_status(workspace_id, owner)
    assert stop_state["stopped"] is True

    items_before = _count_rows(service, "omp_work.work_items", workspace_id)
    idem_before = _count_rows(service, "omp_control.idempotent_commands", workspace_id)

    # A new agent command is refused 409 without writing an item or idempotency row.
    status, body = _command(
        service,
        workspace_id,
        _batch_command("blocked work"),
    )
    assert status == 409, body
    assert body["error"]["code"] == "agent_stop_engaged"
    assert body["error"]["diagnostics"] == [
        f"agent stop engaged at {stop_state['changed_at']}: runaway loop",
        "only the owner can release it: omp-work stop release",
    ]
    assert _count_rows(service, "omp_work.work_items", workspace_id) == items_before
    assert (
        _count_rows(service, "omp_control.idempotent_commands", workspace_id)
        == idem_before
    )

    # Replaying the pre-stop operation returns its stored receipt, not a refusal.
    replay_receipt, replay_result = store.execute(
        pre_stop, actor_id=owner, actor_kind="owner", required_scope="work.mutate"
    )
    assert replay_receipt.state == OperationState.REPLAYED
    assert replay_result == pre_result

    # Owner releases; the same new command now applies.
    release = _envelope(
        workspace_id, _stop_command("release_stop", "operator resumed work")
    )
    release_receipt, _ = store.execute(
        release, actor_id=owner, actor_kind="owner", required_scope="work.approve"
    )
    assert release_receipt.state == OperationState.APPLIED
    assert store.stop_status(workspace_id, owner)["stopped"] is False

    status, body = _command(service, workspace_id, _batch_command("new work"))
    assert status == 200, body
    assert body["result"]["items"][0]["state"] == "BACKLOG"
    assert _count_rows(service, "omp_work.work_items", workspace_id) == items_before + 1
