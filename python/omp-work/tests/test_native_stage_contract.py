from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omp_work.v1.models import (
    CommandEnvelope,
    ReserveStageLaunchPayload,
    StageLaunchRole,
)


def _payload() -> dict[str, object]:
    digest = "a" * 64
    return {
        "work_id": uuid4(),
        "role": "implement",
        "request_sha256": digest,
        "tool_call_id": "native-1",
        "task_sha256": digest,
        "prepared_context_sha256": digest,
        "requested_selector": "openai-codex/gpt-5.6-luna:high",
        "requested_provider": "openai-codex",
        "requested_model": "gpt-5.6-luna",
        "requested_api": "openai-codex-responses",
        "requested_effort": "high",
        "requested_wire_model": "gpt-5.6-luna",
    }


def test_stage_reservation_carries_route_and_prepared_context_identity() -> None:
    payload = ReserveStageLaunchPayload.model_validate(_payload())
    assert payload.role is StageLaunchRole.IMPLEMENT
    assert len(payload.prepared_context_sha256) == 64
    assert payload.requested_selector.endswith(":high")


def test_stage_reservation_rejects_unhashed_context() -> None:
    body = _payload()
    body["prepared_context_sha256"] = "context"
    with pytest.raises(ValidationError):
        ReserveStageLaunchPayload.model_validate(body)


def test_stage_command_envelope_discriminates_native_reservation() -> None:
    payload = _payload()
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": uuid4(),
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {"type": "reserve_stage_launch", "payload": payload},
        }
    )
    assert envelope.command.type == "reserve_stage_launch"
    assert envelope.command.payload.role.value == "implement"
