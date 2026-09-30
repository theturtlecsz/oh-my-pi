"""OMP-416-s01-s04: client contract schema, decisions, operations, and relay envelopes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from omp_work import _READS, load_contract
from omp_work.v1.models import CommandEnvelope
from pydantic import ValidationError


def _contract_file(name: str) -> Path:
    return Path(str(files("omp_work").joinpath(f"contracts/v1/{name}")))


def test_client_contract_operations_and_0019_correspondence() -> None:
    contract = load_contract()
    client_contract = contract.client_contract
    assert client_contract.version == "client.omp.dev/v1"
    assert len(client_contract.operations) == 17

    expected_operations = (
        (
            "project.list",
            "GET",
            "/v1/workspaces/{workspace_id}/client/projects",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "project.context",
            "GET",
            "/v1/workspaces/{workspace_id}/client/projects/{project_id}/context",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "project.status",
            "GET",
            "/v1/workspaces/{workspace_id}/client/projects/{project_id}/status",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "project.decisions",
            "GET",
            "/v1/workspaces/{workspace_id}/client/projects/{project_id}/decisions",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "mission.status",
            "GET",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "evidence.inspect",
            "GET",
            "/v1/workspaces/{workspace_id}/client/evidence/{receipt_id}",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "stop.status",
            "GET",
            "/v1/workspaces/{workspace_id}/client/stop",
            None,
            ("work.read", "work.client"),
            None,
            "ClientResponse",
        ),
        (
            "mission.submit",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions",
            "submit_mission",
            ("work.client", "work.mutate"),
            "SubmitMissionPayload",
            "ClientResponse",
        ),
        (
            "mission.intake",
            "POST",
            "/v1/workspaces/{workspace_id}/client/mission-intake",
            "draft_mission_intake",
            ("work.client", "work.mutate"),
            "DraftMissionIntakePayload",
            "ClientResponse",
        ),
        (
            "stop.engage",
            "POST",
            "/v1/workspaces/{workspace_id}/client/stop",
            "engage_stop",
            ("work.stop",),
            "StopReasonPayload",
            "ClientResponse",
        ),
        (
            "mission.pause",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/pause",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "mission.resume",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/resume",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "mission.cancel",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/cancel",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "mission.reprioritise",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/priority",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "mission.scope.confirm",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/confirm",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "mission.scope.edit",
            "POST",
            "/v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/edit",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
        (
            "decision.answer",
            "POST",
            "/v1/workspaces/{workspace_id}/client/decisions/{decision_id}/answer",
            "relay_owner_intent",
            ("work.client",),
            "RelayOwnerIntentPayload",
            "ClientResponse",
        ),
    )

    actual_rows = tuple(
        (
            op.name,
            op.method,
            op.path,
            op.command,
            op.scope,
            op.request,
            op.response,
        )
        for op in client_contract.operations
    )
    assert actual_rows == expected_operations

    d0019_path = _contract_file("decisions/0019-client-contract.md")
    assert d0019_path.is_file()
    d0019_text = d0019_path.read_text(encoding="utf-8")
    d0019_lines = d0019_text.splitlines()

    for op in client_contract.operations:
        # Each row's name, method, and path share a 0019 line
        matching = [
            line
            for line in d0019_lines
            if op.name in line and op.method in line and op.path in line
        ]
        assert (
            len(matching) >= 1
        ), f"operation {op.name} ({op.method} {op.path}) not found on a single 0019 line"

        if op.method == "GET":
            get_read = f"GET {op.path}"
            assert get_read in contract.reads
            assert get_read in _READS

    manifest = json.loads(_contract_file("manifest.json").read_text(encoding="utf-8"))
    assert "decisions/0019-client-contract.md" in manifest["paths"]

    required_0019_names = (
        "decision_signature_message",
        "action_class",
        "broaden_scope",
        "not_designated_controller",
        "owner-controller.json",
        "detail=true",
    )
    for name in required_0019_names:
        assert name in d0019_text, f"0019 must name {name}"


def test_no_forbidden_strings_and_operation_naming_rules() -> None:
    contract = load_contract()

    for filename in (
        "contract.json",
        "schema.json",
        "api-schema.json",
        "decisions/0019-client-contract.md",
    ):
        text = _contract_file(filename).read_text(encoding="utf-8")
        assert not re.search(
            r"grok", text, re.IGNORECASE
        ), f"forbidden /grok/i found in {filename}"

    forbidden_op_substrings = ("queue", "set_now", "cancel_work")
    for op in contract.client_contract.operations:
        for forbidden in forbidden_op_substrings:
            assert (
                forbidden not in op.name
            ), f"operation name '{op.name}' contains forbidden substring '{forbidden}'"


def test_command_envelope_relay_owner_intent() -> None:
    valid_instruction = {
        "text": "Please proceed with this action.",
        "source_message_ref": "msg-ref-100",
        "owner_authored": True,
        "received_at": datetime.now(UTC),
    }

    minimal_payloads = [
        {
            "intent": "pause",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
        },
        {
            "intent": "resume",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
        },
        {
            "intent": "request_cancellation",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
        },
        {
            "intent": "change_priority",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
            "revision": 1,
            "priority": 2,
        },
        {
            "intent": "confirm_scope",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
            "decision_id": uuid4(),
            "revision": 1,
        },
        {
            "intent": "edit_scope",
            "instruction": valid_instruction,
            "mission_id": uuid4(),
            "decision_id": uuid4(),
            "revision": 1,
            "draft": {
                "project_id": uuid4(),
                "objective": "Build the client contract",
                "risk_policy": "rp-1",
                "approval_policy": "ap-1",
                "effort_policy": "ep-1",
            },
        },
        {
            "intent": "answer_decision",
            "instruction": valid_instruction,
            "decision_id": uuid4(),
            "answer": "approved",
        },
    ]

    for payload in minimal_payloads:
        env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={"type": "relay_owner_intent", "payload": payload},
        )
        assert env.command.type == "relay_owner_intent"
        assert env.command.payload.intent == payload["intent"]

    # Refuses no instruction
    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "pause",
                    "mission_id": uuid4(),
                },
            },
        )

    # Refuses owner_authored false
    bad_instruction = {**valid_instruction, "owner_authored": False}
    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "pause",
                    "instruction": bad_instruction,
                    "mission_id": uuid4(),
                },
            },
        )

    # Refuses unused field
    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "pause",
                    "instruction": valid_instruction,
                    "mission_id": uuid4(),
                    "priority": 1,
                },
            },
        )

    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "answer_decision",
                    "instruction": valid_instruction,
                    "decision_id": uuid4(),
                    "answer": "approved",
                    "mission_id": uuid4(),
                },
            },
        )

    # Refuses missing field
    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "pause",
                    "instruction": valid_instruction,
                },
            },
        )

    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "change_priority",
                    "instruction": valid_instruction,
                    "mission_id": uuid4(),
                    "revision": 1,
                },
            },
        )

    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "confirm_scope",
                    "instruction": valid_instruction,
                    "mission_id": uuid4(),
                    "decision_id": uuid4(),
                },
            },
        )

    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "edit_scope",
                    "instruction": valid_instruction,
                    "mission_id": uuid4(),
                    "decision_id": uuid4(),
                    "revision": 1,
                },
            },
        )

    with pytest.raises(ValidationError):
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=uuid4(),
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command={
                "type": "relay_owner_intent",
                "payload": {
                    "intent": "answer_decision",
                    "instruction": valid_instruction,
                    "decision_id": uuid4(),
                },
            },
        )
