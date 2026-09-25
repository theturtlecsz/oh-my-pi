"""Tests for JevClient (async httpx client for Jev typed decisions)."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from robomp.jev_client import JevClient
from tests.jev_stub import JevStub

if TYPE_CHECKING:
    from robomp.db import Database


async def test_jev_client_ok_response(db: Database) -> None:
    stub = JevStub(
        mode="ok",
        server_id="srv-42",
        primary_probs={"bug": 0.8, "invalid": 0.2},
    )
    client = JevClient(
        base_url="https://api.typesafe.ai",
        api_key=SecretStr("secret-key-123"),
        db=db,
        transport=stub.transport,
    )

    questions = {
        "primary_type": {
            "type": "choice",
            "instructions": "Classify",
            "options": ["bug", "invalid"],
        }
    }
    result = await client.decide("Test issue", questions)

    assert result is not None
    assert result.server_id == "srv-42"
    assert "primary_type" in result.answers
    assert len(stub.requests) == 1

    req = stub.requests[0]
    assert req.headers["authorization"] == "Bearer secret-key-123"
    req_id = req.headers["x-request-id"]
    # Check valid UUID
    uuid.UUID(req_id)

    # Check jev_calls table
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].request_id == req_id
    assert calls[0].server_id == "srv-42"
    assert calls[0].outcome == "ok"
    assert calls[0].attempt == 1
    assert calls[0].truncated is False
    assert calls[0].state_chars == len("Test issue")


async def test_jev_client_truncation_over_8000(db: Database) -> None:
    stub = JevStub(mode="ok")
    client = JevClient(db=db, transport=stub.transport)

    long_state = "A" * 8500
    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    result = await client.decide(long_state, questions)

    assert result is not None
    assert len(stub.payloads) == 1
    sent_state = stub.payloads[0]["state"]
    assert sent_state.startswith("A" * 8000)
    assert sent_state.endswith("…[truncated 500 chars]")

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].truncated is True
    assert calls[0].state_chars == 8500


async def test_jev_client_no_truncation_under_8000(db: Database) -> None:
    stub = JevStub(mode="ok")
    client = JevClient(db=db, transport=stub.transport)

    state = "Short state"
    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    result = await client.decide(state, questions)

    assert result is not None
    sent_state = stub.payloads[0]["state"]
    assert sent_state == state

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].truncated is False
    assert calls[0].state_chars == len(state)


async def test_jev_client_timeout_no_retry(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    stub = JevStub(mode="delay")
    client = JevClient(db=db, transport=stub.transport)

    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    with caplog.at_level(logging.WARNING):
        result = await client.decide("state", questions)

    assert result is None
    # Must NOT retry after timeout
    assert len(stub.requests) == 1

    # Exactly one warning logged
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "timed out" in warnings[0].message.lower()

    # jev_calls has one timeout row
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "timeout"
    assert calls[0].attempt == 1


async def test_jev_client_500_retries_once_and_fails(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    stub = JevStub(mode="500")
    client = JevClient(db=db, transport=stub.transport)

    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    with caplog.at_level(logging.WARNING):
        result = await client.decide("state", questions)

    assert result is None
    # 5xx must retry once within budget
    assert len(stub.requests) == 2

    # Exactly one warning logged for the call
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    # jev_calls has two rows with distinct request_ids
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 2
    assert calls[0].attempt == 1
    assert calls[0].outcome == "500"
    assert calls[1].attempt == 2
    assert calls[1].outcome == "500"
    assert calls[0].request_id != calls[1].request_id


async def test_jev_client_500_retry_succeeds(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    stub = JevStub(sequence=["500", "ok"])
    client = JevClient(db=db, transport=stub.transport)

    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    with caplog.at_level(logging.WARNING):
        result = await client.decide("state", questions)

    assert result is not None
    assert len(stub.requests) == 2

    # No warning when retry succeeds
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0

    # jev_calls has two rows: 500 then ok
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 2
    assert calls[0].outcome == "500"
    assert calls[0].attempt == 1
    assert calls[1].outcome == "ok"
    assert calls[1].attempt == 2


async def test_jev_client_malformed_json(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    stub = JevStub(mode="malformed")
    client = JevClient(db=db, transport=stub.transport)

    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}
    with caplog.at_level(logging.WARNING):
        result = await client.decide("state", questions)

    assert result is None
    assert len(stub.requests) == 1

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "malformed" in warnings[0].message.lower()

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "malformed"


async def test_jev_client_off_list(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    stub = JevStub(mode="off-list")
    client = JevClient(db=db, transport=stub.transport)

    questions = {
        "primary_type": {
            "type": "choice",
            "instructions": "Classify",
            "options": ["bug", "invalid"],
        }
    }
    with caplog.at_level(logging.WARNING):
        result = await client.decide("state", questions)

    assert result is None
    assert len(stub.requests) == 1

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "off-list" in warnings[0].message.lower()

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "off_list"


async def test_jev_client_circuit_breaker(db: Database) -> None:
    stub = JevStub(mode="500")
    client = JevClient(
        db=db,
        transport=stub.transport,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=60.0,
    )

    questions = {"q": {"type": "choice", "instructions": "?", "options": ["bug"]}}

    # Call 1 fails (2 attempts to stub)
    res1 = await client.decide("state 1", questions)
    assert res1 is None
    assert client.is_breaker_open is False
    assert len(stub.requests) == 2

    # Call 2 fails (2 attempts to stub)
    res2 = await client.decide("state 2", questions)
    assert res2 is None
    assert client.is_breaker_open is True
    assert len(stub.requests) == 4

    # Call 3: circuit breaker is open! No request sent.
    res3 = await client.decide("state 3", questions)
    assert res3 is None
    assert len(stub.requests) == 4  # Still 4, no new request
    # No new row in db for call 3
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 4


async def test_jev_client_choice_and_yesno_helpers(db: Database) -> None:
    stub = JevStub(
        mode="ok",
        answers={
            "choice": {"probabilities": {"bug": 0.9, "invalid": 0.1}},
            "statement": {"probability": 0.85},
        },
    )
    client = JevClient(db=db, transport=stub.transport)

    choice_res = await client.choice("What kind?", ["bug", "invalid"], "state")
    assert choice_res == {"bug": 0.9, "invalid": 0.1}

    yesno_res = await client.yesno("Is it broken?", "state")
    assert yesno_res == 0.85
