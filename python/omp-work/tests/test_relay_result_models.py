"""OMP-416-s01-s07: the relay_owner_intent result model and its provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from omp_work.v1.api_models import CommandResult, RelayOwnerIntentResult


def _relay_result(
    *, intent: str = "pause", owner_authored: bool = True
) -> dict[str, object]:
    return {
        "type": "relay_owner_intent",
        "intent": intent,
        "mission": None,
        "decision_id": None,
        "answer": None,
        "relay": {
            "instruction": {
                "text": "Pause the mission.",
                "source_message_ref": "msg-ref-1",
                "owner_authored": owner_authored,
                "received_at": datetime.now(UTC),
            },
            "relayed_by": uuid4(),
            "signed": False,
        },
    }


def test_relay_owner_intent_result_parses_with_contract_key_order() -> None:
    payload = _relay_result()
    parsed = TypeAdapter(CommandResult).validate_python(payload)
    assert isinstance(parsed, RelayOwnerIntentResult)
    assert list(parsed.model_dump(mode="json").keys()) == [
        "type",
        "intent",
        "mission",
        "decision_id",
        "answer",
        "relay",
    ]


def test_relay_provenance_refuses_non_owner_authored_instruction() -> None:
    payload = _relay_result(owner_authored=False)
    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_python(payload)


def test_relay_owner_intent_result_refuses_unknown_intent() -> None:
    payload = _relay_result(intent="explode")
    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_python(payload)
