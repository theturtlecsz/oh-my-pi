"""OMP-417-s04-s07: the owner signature is stored on the answer_decision event.

The command result stays the AnswerDecisionResult dump. The domain event
payload is that dump plus owner_signature when a signature was given.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.v1.canonical import sha256
from omp_work.v1.decision_records import answer_decision, find_decision
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import _grant

pytest_plugins = ["test_workflow_service"]

_TARGET = "ab" * 32
_EXPIRES = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PROJECT = UUID("00000000-0000-7000-8000-000000000417")
_TEXT = "Approve this target."
_SOURCE = "thread:1712345678.000100"
_RECEIVED_AT = "2026-09-30T12:34:56+00:00"


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, query: str, params: object = None) -> None:
        return None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object]:
        return {"now": _NOW}


def _decision(
    decision_id: UUID,
    *,
    action_class: str | None,
    target_sha256: str | None,
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(_PROJECT),
        "mission_id": "OMP-417",
        "question": "Apply this target?",
        "why_it_matters": "The approval must name the target it covers.",
        "risk_of_delay": "Pipeline halts until answered.",
        "options": ["approve", "reject"],
        "evidence_refs": ["receipt:answer-event"],
        "default_if_any": "reject",
        "risk_of_each_choice": {
            "approve": "The named target becomes approved.",
            "reject": "Work stalls.",
        },
        "action_class": action_class,
        "target_sha256": target_sha256,
        "resume_state": "target-approved",
    }


def _row(decision: dict[str, object]) -> dict[str, object]:
    return {
        "event_type": "create_decision",
        "payload": {
            "type": "create_decision",
            "status": "pending",
            "decision": decision,
        },
        "occurred_at": _NOW,
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


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _signers(tmp_path: Path, key: Path) -> Path:
    path = tmp_path / "owner_allowed_signers"
    public = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
    path.write_text(f"owner {public}\n", encoding="utf-8")
    return path


def _values(value: object) -> list[object]:
    if isinstance(value, dict):
        found: list[object] = []
        for item in value.values():
            found.extend(_values(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_values(item))
        return found
    return [value]


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")
def test_answer_decision_event_carries_signature_result_does_not(tmp_path: Path) -> None:
    workspace_id = uuid4()
    signed_id = uuid4()
    unsigned_id = uuid4()
    key = _generate_key(tmp_path, "owner")
    signers = _signers(tmp_path, key)
    signed_decision = _decision(
        signed_id, action_class="contract_hash", target_sha256=_TARGET
    )
    unsigned_decision = _decision(unsigned_id, action_class=None, target_sha256=None)
    cursor = _Cursor([_row(signed_decision), _row(unsigned_decision)])
    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=signed_id,
        action_class="contract_hash",
        answer="approve",
        target_sha256=_TARGET,
        expires_at=_EXPIRES,
    )
    signature = _sign(key, message)
    result, event = answer_decision(
        cursor,
        _envelope(
            workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": str(signed_id),
                    "answer": "approve",
                    "owner_signature": signature,
                    "expires_at": _EXPIRES.isoformat(),
                },
            },
        ),
        signers,
    )
    assert "owner_signature" not in result
    assert event["owner_signature"] == signature
    without_signature = {
        field: value for field, value in event.items() if field != "owner_signature"
    }
    assert without_signature == result

    unsigned_result, unsigned_event = answer_decision(
        cursor,
        _envelope(
            workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": str(unsigned_id),
                    "answer": "approve",
                },
            },
        ),
        signers,
    )
    assert unsigned_event == unsigned_result
    assert "owner_signature" not in unsigned_result


def _create_body(decision_id: UUID, project_id: UUID) -> dict[str, object]:
    body = _decision(decision_id, action_class="contract_hash", target_sha256=_TARGET)
    body["project_id"] = str(project_id)
    return body


def _instruction() -> dict[str, object]:
    return {
        "text": _TEXT,
        "source_message_ref": _SOURCE,
        "owner_authored": True,
        "received_at": _RECEIVED_AT,
    }


def _applied_answers(service, workspace_id: UUID) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT payload FROM omp_audit.domain_events "
            "WHERE workspace_id = %s AND event_type = 'answer_decision' "
            "AND outcome = 'applied' ORDER BY sequence ASC",
            (workspace_id,),
        )
        rows = cur.fetchall()
    events: list[dict[str, object]] = []
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert isinstance(payload, dict)
        events.append(payload)
    return events


def _answer_event(events: list[dict[str, object]], decision_id: UUID) -> dict[str, object]:
    matched = [event for event in events if event.get("decision_id") == str(decision_id)]
    assert len(matched) == 1
    return matched[0]


def _view(store: PostgresWorkStore, workspace_id: UUID, actor_id: UUID, decision_id: UUID):
    with store._transaction(workspace_id, actor_id) as cur:
        view = find_decision(cur, workspace_id, decision_id)
    assert view is not None
    return view


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)
def test_signed_answer_records_signature_on_domain_event(service, tmp_path: Path) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    project_id = uuid4()
    direct_id = uuid4()
    relay_id = uuid4()
    key = _generate_key(tmp_path, "owner")
    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        f"owner {Path(f'{key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )

    def signature_for(decision_id: UUID) -> str:
        message = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
            target_sha256=_TARGET,
            expires_at=_EXPIRES,
        )
        return _sign(key, message)

    try:
        for decision_id in (direct_id, relay_id):
            receipt, _created = store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "create_decision",
                        "payload": _create_body(decision_id, project_id),
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.mutate",
            )
            assert receipt.state == OperationState.APPLIED

        direct_before = set(_view(store, workspace_id, actor_id, direct_id))
        relay_before = set(_view(store, workspace_id, actor_id, relay_id))
        direct_signature = signature_for(direct_id)
        direct_envelope = _envelope(
            workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": str(direct_id),
                    "answer": "approve",
                    "owner_signature": direct_signature,
                    "expires_at": _EXPIRES.isoformat(),
                },
            },
        )
        direct_receipt, direct_result = store.execute(
            direct_envelope,
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert direct_receipt.state == OperationState.APPLIED
        assert "owner_signature" not in direct_result
        assert direct_signature not in _values(direct_result)
        assert direct_receipt.result_sha256 == sha256(direct_result)

        relay_signature = signature_for(relay_id)
        relay_envelope = _envelope(
            workspace_id,
            {
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "answer_decision",
                    "decision_id": str(relay_id),
                    "answer": "approve",
                    "owner_signature": relay_signature,
                    "expires_at": _EXPIRES.isoformat(),
                    "instruction": _instruction(),
                },
            },
        )
        relay_receipt, relay_result = store.execute(
            relay_envelope,
            actor_id=actor_id,
            actor_kind="client",
            required_scope="work.client",
        )
        assert relay_receipt.state == OperationState.APPLIED
        assert relay_result["type"] == "relay_owner_intent"
        assert "owner_signature" not in relay_result
        assert relay_signature not in _values(relay_result)
        assert relay_receipt.result_sha256 == sha256(relay_result)

        events = _applied_answers(service, workspace_id)
        direct_event = _answer_event(events, direct_id)
        relay_event = _answer_event(events, relay_id)
        assert direct_event["owner_signature"] == direct_signature
        assert direct_event == {**direct_result, "owner_signature": direct_signature}
        assert sha256(direct_event) != direct_receipt.result_sha256
        assert relay_event["owner_signature"] == relay_signature
        assert relay_event["type"] == "answer_decision"
        assert relay_event["answer"] == "approve"

        direct_after = _view(store, workspace_id, actor_id, direct_id)
        relay_after = _view(store, workspace_id, actor_id, relay_id)
        assert set(direct_after) == direct_before
        assert set(relay_after) == relay_before
        assert "owner_signature" not in direct_after
        assert "owner_signature" not in relay_after
        assert direct_after["status"] == "answered"
        assert relay_after["status"] == "answered"

        replay_receipt, replay = store.execute(
            direct_envelope,
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert replay_receipt.state == OperationState.REPLAYED
        assert replay == direct_result
        assert replay_receipt.result_sha256 == direct_receipt.result_sha256
        assert len(_applied_answers(service, workspace_id)) == 2
    finally:
        if signers_path.exists():
            signers_path.unlink()
