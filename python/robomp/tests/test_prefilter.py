"""Tests for robomp prefilter (Jev typed-decision issue pre-gate)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from robomp.config import Settings
from robomp.jev_client import JevClient
from robomp.prefilter import PrefilterResult, run_prefilter
from tests.jev_stub import JevStub

if TYPE_CHECKING:
    from robomp.db import Database


def _make_settings(**kwargs: object) -> Settings:
    defaults = {
        "ROBOMP_GH_PROXY_URL": "http://gh-proxy.invalid:8081",
        "ROBOMP_GH_PROXY_HMAC_KEY": "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "GITHUB_WEBHOOK_SECRET": "test-webhook-secret",
        "ROBOMP_BOT_LOGIN": "robomp-bot",
        "ROBOMP_GIT_AUTHOR_EMAIL": "robomp-bot@example.invalid",
        "ROBOMP_JEV_ENABLED": "true",
        "ROBOMP_PREFILTER": "true",
        "ROBOMP_PREFILTER_THRESHOLD": "0.90",
    }
    defaults.update(kwargs)
    return Settings.model_construct(
        github_webhook_secret=SecretStr(str(defaults["GITHUB_WEBHOOK_SECRET"])),
        bot_login=str(defaults["ROBOMP_BOT_LOGIN"]),
        git_author_email=str(defaults["ROBOMP_GIT_AUTHOR_EMAIL"]),
        gh_proxy_url=str(defaults["ROBOMP_GH_PROXY_URL"]),
        gh_proxy_hmac_key=SecretStr(str(defaults["ROBOMP_GH_PROXY_HMAC_KEY"])),
        jev_enabled=str(defaults["ROBOMP_JEV_ENABLED"]).lower() in ("true", "1", "yes"),
        prefilter_enabled=str(defaults["ROBOMP_PREFILTER"]).lower() in ("true", "1", "yes"),
        prefilter_threshold=float(defaults["ROBOMP_PREFILTER_THRESHOLD"]),
    )


async def test_prefilter_invalid_095_routes_to_answered_invalid(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(
        mode="ok",
        primary_probs={"invalid": 0.95, "bug": 0.05},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#1",
        title="Spam issue title",
        body="Some spam body text",
    )

    assert result == PrefilterResult(route="answered", label="invalid")
    row = db.get_issue_prefilter("octo/widget#1")
    assert row is not None
    assert row.route == "answered"
    assert row.label == "invalid"
    assert row.request_id is not None
    probs = json.loads(row.probabilities_json)
    assert probs["primary_type"]["invalid"] == 0.95


async def test_prefilter_invalid_085_routes_to_session(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(
        mode="ok",
        primary_probs={"invalid": 0.85, "bug": 0.15},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#2",
        title="Ambiguous issue",
        body="Might be invalid or a bug",
    )

    assert result == PrefilterResult(route="session", label=None)
    row = db.get_issue_prefilter("octo/widget#2")
    assert row is not None
    assert row.route == "session"
    assert row.label is None
    probs = json.loads(row.probabilities_json)
    assert probs["primary_type"]["invalid"] == 0.85


async def test_prefilter_batch_audit_092_routes_to_answered_batch_audit(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(
        mode="ok",
        primary_probs={"bug": 0.80, "invalid": 0.20},
        batch_audit_prob=0.92,
    )
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#3",
        title="Automated scanner report",
        body="Security audit finding across 100 repos",
    )

    assert result == PrefilterResult(route="answered", label="batch-audit")
    row = db.get_issue_prefilter("octo/widget#3")
    assert row is not None
    assert row.route == "answered"
    assert row.label == "batch-audit"
    probs = json.loads(row.probabilities_json)
    assert probs["batch_audit"] == 0.92


async def test_prefilter_question_high_confidence(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(
        mode="ok",
        primary_probs={"question": 0.94, "bug": 0.06},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#4",
        title="How to configure X?",
        body="Can someone explain how to use X?",
    )

    assert result == PrefilterResult(route="answered", label="question")
    row = db.get_issue_prefilter("octo/widget#4")
    assert row is not None
    assert row.route == "answered"
    assert row.label == "question"


async def test_prefilter_bug_high_confidence_routes_to_session(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(
        mode="ok",
        primary_probs={"bug": 0.99, "invalid": 0.01},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#5",
        title="Null pointer crash on startup",
        body="Here is the stack trace and repro steps",
    )

    # Bugs always route to session
    assert result == PrefilterResult(route="session", label=None)
    row = db.get_issue_prefilter("octo/widget#5")
    assert row is not None
    assert row.route == "session"
    assert row.label is None


async def test_prefilter_timeout_routes_to_session(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    settings = _make_settings()
    stub = JevStub(mode="delay")
    client = JevClient(db=db, transport=stub.transport)

    with caplog.at_level(logging.WARNING):
        result = await run_prefilter(
            settings=settings,
            db=db,
            client=client,
            key="octo/widget#6",
            title="Title",
            body="Body",
        )

    assert result == PrefilterResult(route="session", label=None)
    # Exactly one warning logged
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    # jev_calls has timeout row
    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "timeout"

    # issue_prefilter has session row
    row = db.get_issue_prefilter("octo/widget#6")
    assert row is not None
    assert row.route == "session"


async def test_prefilter_500_routes_to_session(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    settings = _make_settings()
    stub = JevStub(mode="500")
    client = JevClient(db=db, transport=stub.transport)

    with caplog.at_level(logging.WARNING):
        result = await run_prefilter(
            settings=settings,
            db=db,
            client=client,
            key="octo/widget#7",
            title="Title",
            body="Body",
        )

    assert result == PrefilterResult(route="session", label=None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 2
    assert calls[0].outcome == "500"
    assert calls[1].outcome == "500"

    row = db.get_issue_prefilter("octo/widget#7")
    assert row is not None
    assert row.route == "session"


async def test_prefilter_malformed_routes_to_session(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    settings = _make_settings()
    stub = JevStub(mode="malformed")
    client = JevClient(db=db, transport=stub.transport)

    with caplog.at_level(logging.WARNING):
        result = await run_prefilter(
            settings=settings,
            db=db,
            client=client,
            key="octo/widget#8",
            title="Title",
            body="Body",
        )

    assert result == PrefilterResult(route="session", label=None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "malformed"

    row = db.get_issue_prefilter("octo/widget#8")
    assert row is not None
    assert row.route == "session"


async def test_prefilter_off_list_routes_to_session(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    settings = _make_settings()
    stub = JevStub(mode="off-list")
    client = JevClient(db=db, transport=stub.transport)

    with caplog.at_level(logging.WARNING):
        result = await run_prefilter(
            settings=settings,
            db=db,
            client=client,
            key="octo/widget#9",
            title="Title",
            body="Body",
        )

    assert result == PrefilterResult(route="session", label=None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    calls = db.get_jev_calls("robomp_prefilter")
    assert len(calls) == 1
    assert calls[0].outcome == "off_list"

    row = db.get_issue_prefilter("octo/widget#9")
    assert row is not None
    assert row.route == "session"


async def test_prefilter_flags_off_no_request(db: Database) -> None:
    settings = _make_settings(ROBOMP_JEV_ENABLED="false", ROBOMP_PREFILTER="false")
    stub = JevStub(mode="ok")
    client = JevClient(db=db, transport=stub.transport)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#10",
        title="Title",
        body="Body",
    )

    assert result == PrefilterResult(route="session", label=None)
    assert len(stub.requests) == 0
    row = db.get_issue_prefilter("octo/widget#10")
    assert row is not None
    assert row.route == "session"
    assert row.request_id is None


async def test_prefilter_client_none_no_request(db: Database) -> None:
    settings = _make_settings()

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=None,
        key="octo/widget#11",
        title="Title",
        body="Body",
    )

    assert result == PrefilterResult(route="session", label=None)
    row = db.get_issue_prefilter("octo/widget#11")
    assert row is not None
    assert row.route == "session"
    assert row.request_id is None


async def test_prefilter_open_breaker_no_request(db: Database) -> None:
    settings = _make_settings()
    stub = JevStub(mode="500")
    client = JevClient(
        db=db,
        transport=stub.transport,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=60.0,
    )

    # Trip the breaker with 2 failing calls
    await client.decide("s1", {"q": {"type": "choice", "options": ["bug"], "instructions": "?"}})
    await client.decide("s2", {"q": {"type": "choice", "options": ["bug"], "instructions": "?"}})
    assert client.is_breaker_open is True
    requests_before = len(stub.requests)

    result = await run_prefilter(
        settings=settings,
        db=db,
        client=client,
        key="octo/widget#12",
        title="Title",
        body="Body",
    )

    assert result == PrefilterResult(route="session", label=None)
    # No new requests sent
    assert len(stub.requests) == requests_before
    row = db.get_issue_prefilter("octo/widget#12")
    assert row is not None
    assert row.route == "session"
