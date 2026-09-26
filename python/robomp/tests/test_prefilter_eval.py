"""Tests for robomp prefilter_eval module."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from robomp.db import Database
from robomp.jev_client import JevClient
from robomp.prefilter_eval import (
    evaluate_prefilter,
    load_issues_jsonl,
)
from tests.jev_stub import JevStub


def test_load_issues_jsonl() -> None:
    with TemporaryDirectory() as tmpdir:
        issues_file = Path(tmpdir) / "issues.jsonl"
        records = [
            {"key": "repo#1", "title": "Crash on startup", "body": "Stack trace...", "label": "bug"},
            {"key": "repo#2", "title": "How to use feature", "body": "Question...", "label": "question"},
        ]
        issues_file.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        loaded = load_issues_jsonl(issues_file)
        assert len(loaded) == 2
        assert loaded[0]["key"] == "repo#1"
        assert loaded[1]["label"] == "question"


async def test_evaluate_prefilter_empty_list() -> None:
    metrics = await evaluate_prefilter([])
    assert metrics.total_issues == 0
    assert metrics.skip_session_share == 0.0
    assert metrics.confident_bucket_accuracy == 1.0


async def test_evaluate_prefilter_confident_correct(db: Database) -> None:
    stub = JevStub(
        mode="ok",
        primary_probs={"invalid": 0.95, "bug": 0.05},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    issues = [
        {"key": "repo#1", "title": "Spam issue", "body": "junk", "label": "invalid"},
        {"key": "repo#2", "title": "Another spam", "body": "junk", "label": "invalid"},
    ]

    metrics = await evaluate_prefilter(issues, client=client, db=db, threshold=0.90)

    assert metrics.total_issues == 2
    assert metrics.skip_session_count == 2
    assert metrics.skip_session_share == 1.0
    assert metrics.confident_bucket_total == 2
    assert metrics.confident_bucket_correct == 2
    assert metrics.confident_bucket_accuracy == 1.0
    assert metrics.p50_latency_ms >= 0.0


async def test_evaluate_prefilter_mixed_and_low_confidence(db: Database) -> None:
    # First issue: confident invalid (0.95)
    # Second issue: low confidence (0.80), routes to session
    stub = JevStub(
        mode="ok",
        primary_probs={"invalid": 0.95, "bug": 0.05},
        batch_audit_prob=0.01,
    )
    client = JevClient(db=db, transport=stub.transport)

    issues = [
        {"key": "repo#1", "title": "Spam issue", "body": "junk", "label": "invalid"},
    ]
    metrics = await evaluate_prefilter(issues, client=client, db=db, threshold=0.90)
    assert metrics.skip_session_share == 1.0

    # Low confidence issue
    stub_low = JevStub(
        mode="ok",
        primary_probs={"invalid": 0.80, "bug": 0.20},
        batch_audit_prob=0.01,
    )
    client_low = JevClient(db=db, transport=stub_low.transport)
    metrics_low = await evaluate_prefilter(
        [{"key": "repo#2", "title": "Maybe bug", "body": "details", "label": "bug"}],
        client=client_low,
        db=db,
        threshold=0.90,
    )
    assert metrics_low.skip_session_count == 0
    assert metrics_low.skip_session_share == 0.0
    assert metrics_low.confident_bucket_total == 0
    # Overall accuracy counts session-routed issues as 1.0
    assert metrics_low.overall_accuracy == 1.0


async def test_evaluate_prefilter_malformed_and_off_list(db: Database) -> None:
    stub_malformed = JevStub(mode="malformed")
    client = JevClient(db=db, transport=stub_malformed.transport)

    issues = [{"key": "repo#1", "title": "Bug", "body": "desc", "label": "bug"}]
    metrics = await evaluate_prefilter(issues, client=client, db=db, threshold=0.90)

    assert metrics.skip_session_count == 0
    assert metrics.unparseable_count == 1
    assert metrics.unparseable_rate == 1.0
