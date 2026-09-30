"""OMP-426: mission intake confirmation — contract closure and owner-only answer.

Without a database: drafting is ``work.mutate`` and answering is ``work.approve``
and owner-only. An answer without an instruction, an option other than
confirm or reject, and a scope that carries an objective are HTTP 400.
Automation is refused before the store. The owner's answer reaches the store.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import omp_work
from omp_work import contract_sha256, load_contract
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    MissionDraft,
    MissionIntakeScope,
    OperationReceipt,
    OperationState,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
MISSION = UUID("00000000-0000-7000-8000-000000000201")
DECISION = UUID("00000000-0000-7000-8000-000000000202")
PROJECT = UUID("00000000-0000-7000-8000-000000000203")
ACTOR = UUID("00000000-0000-7000-8000-000000000204")


def _instruction() -> dict[str, object]:
    return {
        "text": "Confirm this mission.",
        "provenance": {
            "channel": "owner-chat",
            "message_ref": "msg-426-1",
            "received_at": "2026-09-30T12:00:00+00:00",
        },
    }


def _intake() -> dict[str, object]:
    text = "Confirm the mission scope before planning."
    return {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {
            "id": "goal-1",
            "statement": "Confirm mission scope",
            "source_span_ids": [],
        },
    }


def _scope(**overrides: object) -> dict[str, object]:
    scope: dict[str, object] = {
        "project_id": str(PROJECT),
        "risk_policy": "risk-policy",
        "approval_policy": "approval-policy",
        "effort_policy": "effort-policy",
    }
    scope.update(overrides)
    return scope


def _draft_command(scope: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "draft_mission_intake",
        "payload": {
            "mission_id": str(MISSION),
            "base_revision": 1,
            "intake": _intake(),
            "scope": _scope() if scope is None else scope,
        },
    }


def _answer_command(
    answer: dict[str, object] | None = None,
    *,
    instruction: dict[str, object] | None = None,
    include_instruction: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_id": str(DECISION),
        "mission_id": str(MISSION),
        "revision": 1,
        "answer": answer
        or {"kind": "option", "option": "confirm"},
    }
    if include_instruction:
        payload["instruction"] = _instruction() if instruction is None else instruction
    return {"type": "answer_mission_draft", "payload": payload}


def _mission_view() -> dict[str, object]:
    return {
        "project_id": str(PROJECT),
        "objective": "Ship the intake confirmation",
        "risk_policy": "risk-policy",
        "approval_policy": "approval-policy",
        "effort_policy": "effort-policy",
        "mission_id": str(MISSION),
        "created_by": str(ACTOR),
        "created_at": "2026-09-30T12:00:00+00:00",
        "revision": 1,
        "status": "awaiting_confirmation",
        "drawn": {"usd": "0", "tokens": 0, "wall_clock_seconds": 0},
    }


class _RecordingStore:
    """Fake WorkStore: records the execute call and echoes a valid result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        payload = envelope.command.payload
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
                "payload": payload,
            }
        )
        receipt = OperationReceipt(
            operation_id=envelope.operation_id,
            request_id=envelope.request_id,
            state=OperationState.APPLIED,
            request_sha256="0" * 64,
            result_sha256="1" * 64,
        )
        if envelope.command.type == "draft_mission_intake":
            result: dict[str, object] = {
                "type": "draft_mission_intake",
                "mission_id": str(payload.mission_id),
                "outcome": "awaiting_owner",
                "questions": [],
            }
        else:
            answer = payload.answer
            if answer.kind == "note":
                outcome = "noted"
            elif answer.kind == "option" and answer.option == "reject":
                outcome = "rejected"
            else:
                outcome = "approved"
            result = {
                "type": "answer_mission_draft",
                "decision_id": str(payload.decision_id),
                "mission_id": str(payload.mission_id),
                "outcome": outcome,
                "mission": _mission_view(),
                "instruction": payload.instruction.model_dump(mode="json"),
            }
        return receipt, result


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_kind, scopes in (
        ("owner", "owner", ["work.mutate", "work.approve"]),
        ("automation", "automation", ["work.mutate", "work.approve"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(ACTOR),
                    "actor_kind": actor_kind,
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _client(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities_dir(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def _post(client: TestClient, token: str, command: dict[str, object]):
    return client.post(
        "/v1/commands",
        headers={
            "Authorization": f"Bearer {token}",
            "X-OMP-Contract-SHA256": contract_sha256(),
            "X-OMP-Workspace-ID": str(WORKSPACE),
        },
        json={
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        },
    )


def test_contract_closures_name_the_mission_intake_commands() -> None:
    contract = load_contract()
    for command in ("draft_mission_intake", "answer_mission_draft"):
        assert command in omp_work._COMMAND_TYPES
        assert command in contract.command_types


def test_mission_intake_scope_mapping() -> None:
    assert WorkService._scopes["draft_mission_intake"] == "work.mutate"
    assert WorkService._scopes["answer_mission_draft"] == "work.approve"


def test_scope_is_mission_draft_minus_confirmed_fields() -> None:
    excluded = {"objective", "acceptance_criteria", "constraints"}
    assert set(MissionIntakeScope.model_fields) == set(MissionDraft.model_fields) - excluded
    scope_schema = MissionIntakeScope.model_json_schema()["properties"]
    draft_schema = MissionDraft.model_json_schema()["properties"]
    for name in MissionIntakeScope.model_fields:
        assert scope_schema[name] == draft_schema[name]


def test_invalid_answer_and_scope_are_400(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    missing_instruction = _post(
        client,
        "owner-token",
        _answer_command(include_instruction=False),
    )
    assert missing_instruction.status_code == 400
    assert missing_instruction.json()["error"]["code"] == "invalid_request"

    maybe = _post(
        client,
        "owner-token",
        _answer_command({"kind": "option", "option": "maybe"}),
    )
    assert maybe.status_code == 400
    assert maybe.json()["error"]["code"] == "invalid_request"

    with_objective = _post(
        client,
        "owner-token",
        _draft_command(_scope(objective="Ship it")),
    )
    assert with_objective.status_code == 400
    assert with_objective.json()["error"]["code"] == "invalid_request"
    assert store.calls == []


def test_automation_answer_is_forbidden_and_owner_answer_reaches_store(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)

    drafted = _post(client, "automation-token", _draft_command())
    assert drafted.status_code == 200
    assert drafted.json()["result"]["outcome"] == "awaiting_owner"
    assert store.calls[-1]["command_type"] == "draft_mission_intake"
    assert store.calls[-1]["required_scope"] == "work.mutate"
    assert store.calls[-1]["actor_kind"] == "automation"

    refused = _post(client, "automation-token", _answer_command())
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert [call["command_type"] for call in store.calls] == ["draft_mission_intake"]

    answered = _post(client, "owner-token", _answer_command())
    assert answered.status_code == 200
    body = answered.json()["result"]
    assert body["type"] == "answer_mission_draft"
    assert body["outcome"] == "approved"
    assert body["instruction"]["provenance"]["message_ref"] == "msg-426-1"
    assert store.calls[-1]["command_type"] == "answer_mission_draft"
    assert store.calls[-1]["actor_kind"] == "owner"
    assert store.calls[-1]["required_scope"] == "work.approve"
    assert store.calls[-1]["payload"].answer.kind == "option"
    assert store.calls[-1]["payload"].answer.option == "confirm"
    assert store.calls[-1]["payload"].instruction.text == "Confirm this mission."
