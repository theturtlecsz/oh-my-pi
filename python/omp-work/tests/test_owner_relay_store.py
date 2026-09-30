"""OMP-416-s04: relay_owner_intent on PostgreSQL.

pause, resume, request_cancellation, and change_priority apply. The domain
event keeps the instruction text verbatim, its provenance, and relayed_by.
A stale priority revision is revision_conflict. Cancelling an abandoned
mission is mission_transition_refused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.v1.models import CommandEnvelope, RelayedInstruction
from omp_work.v1.owner_signature import NAMESPACE, relay_signature_message
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from test_mission_status_store import _draft, _events, _get, _project
from test_workflow_service import _command

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_TEXT = 'Hold here.\nKeep this exact instruction: "pause now".'
_SOURCE = "thread:1712345678.000100"
_RECEIVED_AT = "2026-09-30T12:34:56+00:00"


def _instruction(text: str = _TEXT) -> dict[str, object]:
    return {
        "text": text,
        "source_message_ref": _SOURCE,
        "owner_authored": True,
        "received_at": _RECEIVED_AT,
    }


def _envelope(workspace_id, command: dict[str, object]) -> CommandEnvelope:
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


def _relay(intent: str, mission_id, text: str, **fields: object) -> dict[str, object]:
    return {
        "type": "relay_owner_intent",
        "payload": {
            "intent": intent,
            "mission_id": str(mission_id),
            "instruction": _instruction(text),
            **fields,
        },
    }


def _apply(store, workspace_id, actor_id, command: dict[str, object]):
    return store.execute(
        _envelope(workspace_id, command),
        actor_id=actor_id,
        actor_kind="client",
        required_scope="work.client",
    )


def _latest_event(service, workspace_id, mission_id) -> dict[str, object]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT event_type, payload FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND aggregate_id=%s AND aggregate_type='mission'"
            " AND outcome='applied' ORDER BY sequence DESC LIMIT 1",
            (workspace_id, mission_id),
        )
        row = cur.fetchone()
    assert row is not None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {"event_type": row["event_type"], "payload": payload}


def _approved_mission(service):
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id),
            },
        },
    )
    assert status == 200, body
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
    return workspace_id, mission_id


def _generate_key(tmp_path: Path) -> Path:
    key = tmp_path / "owner"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def test_relay_intents_apply_and_record_provenance(service) -> None:
    workspace_id, mission_id = _approved_mission(service)
    store = PostgresWorkStore(service.config)
    actor_id = uuid4()

    pause_text = _TEXT
    _receipt, paused = _apply(
        store,
        workspace_id,
        actor_id,
        _relay("pause", mission_id, pause_text),
    )
    assert paused["type"] == "relay_owner_intent"
    assert paused["intent"] == "pause"
    assert paused["mission"]["status"] == "paused"
    transition = paused["mission"]["transitions"][-1]
    assert transition["to_status"] == "paused"
    assert transition["cause_kind"] == "principal"
    assert transition["cause_id"] == str(actor_id)
    assert paused["relay"]["relayed_by"] == str(actor_id)
    assert paused["relay"]["signed"] is False
    assert paused["relay"]["instruction"]["text"] == pause_text

    event = _latest_event(service, workspace_id, mission_id)
    assert event["event_type"] == "set_mission_status"
    instruction = event["payload"]["relay"]["instruction"]
    expected = RelayedInstruction.model_validate(_instruction(pause_text)).model_dump(
        mode="json"
    )
    assert instruction == expected
    assert instruction["text"] == pause_text
    assert event["payload"]["relay"]["relayed_by"] == str(actor_id)
    assert event["payload"]["relay"]["signed"] is False
    assert event["payload"]["mission"]["status"] == "paused"
    assert _get_status(service, workspace_id, mission_id) == "paused"


def test_status_priority_and_refused_transitions(service) -> None:
    workspace_id, mission_id = _approved_mission(service)
    store = PostgresWorkStore(service.config)
    actor_id = uuid4()
    _apply(store, workspace_id, actor_id, _relay("pause", mission_id, "Pause it."))

    _receipt, resumed = _apply(
        store, workspace_id, actor_id, _relay("resume", mission_id, "Resume it.")
    )
    assert resumed["mission"]["status"] == "running"
    assert _latest_event(service, workspace_id, mission_id)["event_type"] == (
        "set_mission_status"
    )
    assert (
        _latest_event(service, workspace_id, mission_id)["payload"]["relay"][
            "instruction"
        ]["text"]
        == "Resume it."
    )
    assert _get_status(service, workspace_id, mission_id) == "running"

    _receipt, revised = _apply(
        store,
        workspace_id,
        actor_id,
        _relay("change_priority", mission_id, "Make it urgent.", revision=1, priority=0),
    )
    assert revised["intent"] == "change_priority"
    assert revised["mission"]["priority"] == 0
    assert revised["mission"]["revision"] == 2
    assert revised["mission"]["status"] == "running"
    priority_event = _latest_event(service, workspace_id, mission_id)
    assert priority_event["event_type"] == "revise_mission"
    assert priority_event["payload"]["relay"]["instruction"]["text"] == "Make it urgent."
    assert priority_event["payload"]["relay"]["relayed_by"] == str(actor_id)
    assert priority_event["payload"]["mission"]["priority"] == 0
    assert _events(service, workspace_id, mission_id) == 5

    with pytest.raises(WorkStoreError) as stale:
        _apply(
            store,
            workspace_id,
            actor_id,
            _relay(
                "change_priority",
                mission_id,
                "Stale priority.",
                revision=1,
                priority=3,
            ),
        )
    assert stale.value.code == "revision_conflict"
    assert _events(service, workspace_id, mission_id) == 5

    _receipt, cancelled = _apply(
        store,
        workspace_id,
        actor_id,
        _relay("request_cancellation", mission_id, "Cancel it."),
    )
    assert cancelled["mission"]["status"] == "abandoned"
    cancel_event = _latest_event(service, workspace_id, mission_id)
    assert cancel_event["event_type"] == "set_mission_status"
    assert cancel_event["payload"]["relay"]["instruction"]["text"] == "Cancel it."
    assert cancel_event["payload"]["mission"]["status"] == "abandoned"
    assert _get_status(service, workspace_id, mission_id) == "abandoned"

    with pytest.raises(WorkStoreError) as refused:
        _apply(
            store,
            workspace_id,
            actor_id,
            _relay("request_cancellation", mission_id, "Cancel again."),
        )
    assert refused.value.code == "mission_transition_refused"
    assert _events(service, workspace_id, mission_id) == 6


def test_signature_and_unimplemented_intents(service, tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not installed")
    workspace_id, mission_id = _approved_mission(service)
    store = PostgresWorkStore(service.config)
    actor_id = uuid4()
    key = _generate_key(tmp_path)
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    (service.config.config_dir / "owner_allowed_signers").write_text(
        f"owner {Path(f'{key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    before = _events(service, workspace_id, mission_id)

    unsigned = _envelope(
        workspace_id, _relay("pause", mission_id, "Signed pause.")
    )
    message = relay_signature_message(workspace_id, unsigned.command.payload)
    signature = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")
    bad = unsigned.model_copy(
        update={
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "command": unsigned.command.model_copy(
                update={
                    "payload": unsigned.command.payload.model_copy(
                        update={"owner_signature": "not-a-signature"}
                    )
                }
            ),
        }
    )
    with pytest.raises(WorkStoreError) as invalid:
        store.execute(
            bad,
            actor_id=actor_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert invalid.value.code == "approval_required"
    assert invalid.value.diagnostics == ("owner_signature_invalid",)
    assert _events(service, workspace_id, mission_id) == before

    signed_envelope = unsigned.model_copy(
        update={
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "command": unsigned.command.model_copy(
                update={
                    "payload": unsigned.command.payload.model_copy(
                        update={"owner_signature": signature}
                    )
                }
            ),
        }
    )
    assert (
        relay_signature_message(workspace_id, signed_envelope.command.payload)
        == message
    )
    _receipt, applied = store.execute(
        signed_envelope,
        actor_id=actor_id,
        actor_kind="client",
        required_scope="work.client",
    )
    assert applied["mission"]["status"] == "paused"
    assert applied["relay"]["signed"] is True
    assert applied["relay"]["relayed_by"] == str(actor_id)
    assert applied["relay"]["instruction"]["text"] == "Signed pause."

    other = _envelope(
        workspace_id,
        {
            "type": "relay_owner_intent",
            "payload": {
                "intent": "answer_decision",
                "instruction": _instruction("Answer it."),
                "decision_id": str(uuid4()),
                "answer": "approve",
            },
        },
    )
    with pytest.raises(WorkStoreError) as unavailable:
        store.execute(
            other,
            actor_id=actor_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert unavailable.value.code == "unavailable"
    assert _events(service, workspace_id, mission_id) == before + 1


def _get_status(service, workspace_id, mission_id) -> str:
    response = _get(service.client, workspace_id, mission_id)
    assert response.status_code == 200, response.json()
    return str(response.json()["status"])
