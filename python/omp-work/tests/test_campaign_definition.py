"""Campaign definition loading and scoring (OMP-590).

Covers a relative harness path and its cached digest, malformed definitions,
comparator boundaries, a missing metric, a non-completed execution, and a
mixed campaign verdict.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omp_work.jobs.campaign_definition import (
    Criterion,
    load_definition,
    score_campaign,
    score_metrics,
)
from omp_work.v1.models import ResearchDomain

_METRIC_128 = "m" * 128


def _definition(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "objective": "reduce latency",
        "domain": "engineering",
        "trials": [{"harness": "h.sh"}],
        "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
    }
    payload.update(overrides)
    return payload


def _write(directory: Path, payload: object, name: str = "campaign.json") -> Path:
    path = directory / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _harness(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_relative_path_digest_cached_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "defs"
    first_bytes = b"\x00\xffharness\n"
    second_bytes = b"other-harness"
    _harness(root, "bin/h.sh", first_bytes)
    _harness(root, "bin/other.sh", second_bytes)
    metric = _METRIC_128
    path = _write(
        root,
        {
            "objective": "reduce latency",
            "domain": "engineering",
            "trials": [{"harness": "bin/h.sh"}, {"harness": "bin/other.sh"}],
            "criteria": [
                {"metric": metric, "op": "gte", "threshold": 0.5},
                {"metric": "loss", "op": "lt", "threshold": 0},
                {"metric": "delta", "op": "eq", "threshold": -1},
            ],
        },
    )

    loaded = load_definition(path)

    assert loaded.objective == "reduce latency"
    assert loaded.domain == ResearchDomain.ENGINEERING
    assert isinstance(loaded.domain, ResearchDomain)
    assert loaded.timeout_seconds == 60
    assert loaded.criteria[0].metric == metric
    assert loaded.criteria[0].op == "gte"
    assert loaded.criteria[0].threshold == 0.5
    assert loaded.criteria[1].threshold == 0
    assert loaded.criteria[2].threshold == -1
    assert len(loaded.criteria[0].metric) == 128

    first, second = loaded.trials
    assert first.harness == (root / "bin" / "h.sh").resolve()
    assert second.harness == (root / "bin" / "other.sh").resolve()
    assert first.harness_bytes == first_bytes
    assert second.harness_bytes == second_bytes
    assert first.candidate_digest == hashlib.sha256(first_bytes).hexdigest()
    assert second.candidate_digest == hashlib.sha256(second_bytes).hexdigest()
    assert first.candidate_digest != second.candidate_digest

    (root / "bin" / "h.sh").write_bytes(b"mutated-after-load")
    assert first.harness_bytes == first_bytes
    assert first.candidate_digest == hashlib.sha256(first_bytes).hexdigest()

    reloaded = load_definition(path)
    assert reloaded.trials[0].harness_bytes == b"mutated-after-load"
    assert (
        reloaded.trials[0].candidate_digest
        == hashlib.sha256(b"mutated-after-load").hexdigest()
    )
    assert reloaded.trials[1].harness_bytes == second_bytes

    explicit = _write(
        root,
        _definition(timeout_seconds=45, domain="omp_harness"),
        name="explicit.json",
    )
    _harness(root, "h.sh", b"explicit")
    timed = load_definition(explicit)
    assert timed.timeout_seconds == 45
    assert timed.domain == ResearchDomain.OMP_HARNESS

    for domain in ResearchDomain:
        domain_path = _write(
            root, _definition(domain=domain.value), name=f"{domain.value}.json"
        )
        assert load_definition(domain_path).domain == domain


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(_definition(note="extra"), "unknown key note", id="unknown-root"),
        pytest.param(
            _definition(trials=[{"harness": "h.sh", "seed": 1}]),
            "unknown key seed",
            id="unknown-trial",
        ),
        pytest.param(
            _definition(
                criteria=[
                    {"metric": "score", "op": "gte", "threshold": 1, "unit": "ms"}
                ]
            ),
            "unknown key unit",
            id="unknown-criterion",
        ),
        pytest.param(
            _definition(objective=""),
            "objective must be a nonempty string",
            id="empty-objective",
        ),
        pytest.param(
            _definition(objective=1),
            "objective must be a nonempty string",
            id="objective-number",
        ),
        pytest.param(
            _definition(domain="biology"), "not a research domain", id="bad-domain"
        ),
        pytest.param(
            _definition(domain="Engineering"), "not a research domain", id="domain-case"
        ),
        pytest.param(
            _definition(trials=[]), "trials must be a nonempty list", id="empty-trials"
        ),
        pytest.param(
            _definition(criteria=[]),
            "criteria must be a nonempty list",
            id="empty-criteria",
        ),
        pytest.param(
            _definition(trials=[{"harness": ""}]),
            "harness must be a nonempty",
            id="empty-harness",
        ),
        pytest.param(
            _definition(trials=[{"harness": "missing.sh"}]),
            "cannot read harness",
            id="missing-harness",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "", "op": "gte", "threshold": 1}]),
            "metric must be a nonempty string",
            id="empty-metric",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "m" * 129, "op": "gte", "threshold": 1}]),
            "at most 128",
            id="metric-too-long",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "score", "op": "ge", "threshold": 1}]),
            "op must be one of",
            id="bad-op",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "score", "op": "GTE", "threshold": 1}]),
            "op must be one of",
            id="op-case",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "score", "op": "gte", "threshold": True}]),
            "threshold must be a finite number",
            id="threshold-true",
        ),
        pytest.param(
            _definition(
                criteria=[{"metric": "score", "op": "gte", "threshold": False}]
            ),
            "threshold must be a finite number",
            id="threshold-false",
        ),
        pytest.param(
            _definition(criteria=[{"metric": "score", "op": "gte", "threshold": "1"}]),
            "threshold must be a finite number",
            id="threshold-string",
        ),
        pytest.param(
            _definition(timeout_seconds=True),
            "timeout_seconds must be a positive integer",
            id="timeout-true",
        ),
        pytest.param(
            _definition(timeout_seconds=False),
            "timeout_seconds must be a positive integer",
            id="timeout-false",
        ),
        pytest.param(
            _definition(timeout_seconds=0),
            "timeout_seconds must be a positive integer",
            id="timeout-zero",
        ),
        pytest.param(
            _definition(timeout_seconds=-5),
            "timeout_seconds must be a positive integer",
            id="timeout-negative",
        ),
        pytest.param(
            _definition(timeout_seconds=1.5),
            "timeout_seconds must be a positive integer",
            id="timeout-float",
        ),
        pytest.param(
            _definition(timeout_seconds=None),
            "timeout_seconds must be a positive integer",
            id="timeout-null",
        ),
        pytest.param("{", "not valid JSON", id="broken-json"),
        pytest.param("[]", "definition must be an object", id="root-array"),
        pytest.param(
            '{"objective": "a", "objective": "b"}',
            "duplicate key objective",
            id="duplicate-key",
        ),
        pytest.param(
            '{"objective": "a", "domain": "engineering", "trials": [], '
            '"criteria": [], "threshold": NaN}',
            "non-finite number NaN",
            id="nan-token",
        ),
        pytest.param(
            '{"objective": "a", "domain": "engineering", "trials": [], '
            '"criteria": [], "threshold": Infinity}',
            "non-finite number Infinity",
            id="infinity-token",
        ),
        pytest.param(
            '{"objective": "a", "domain": "engineering", "trials": [], '
            '"criteria": [], "threshold": -Infinity}',
            "non-finite number -Infinity",
            id="negative-infinity-token",
        ),
        pytest.param(
            '{"objective": "reduce latency", "domain": "engineering", '
            '"trials": [{"harness": "h.sh"}], '
            '"criteria": [{"metric": "score", "op": "gte", "threshold": 1e309}]}',
            "threshold must be a finite number",
            id="threshold-overflow",
        ),
        pytest.param(
            {"domain": "engineering"}, "missing objective", id="missing-objective"
        ),
        pytest.param(
            _definition(trials=["h.sh"]),
            "trials\\[0\\] must be an object",
            id="trial-string",
        ),
        pytest.param(
            _definition(criteria=[["score", "gte", 1]]),
            "criteria\\[0\\] must be an object",
            id="criterion-list",
        ),
    ],
)
def test_malformed_input_rejected(tmp_path: Path, payload: object, match: str) -> None:
    _harness(tmp_path, "h.sh", b"#!/bin/sh\n")
    path = _write(tmp_path, payload)
    with pytest.raises((TypeError, ValueError), match=match):
        load_definition(path)


def test_comparator_boundaries() -> None:
    cases = (
        ("gte", 2, 1, True),
        ("gte", 1, 1, True),
        ("gte", 1.0, 1, True),
        ("gte", 0, 1, False),
        ("gt", 2, 1, True),
        ("gt", 1, 1, False),
        ("gt", 0, 1, False),
        ("lte", 0, 1, True),
        ("lte", 1, 1, True),
        ("lte", 2, 1, False),
        ("lt", 0, 1, True),
        ("lt", 1, 1, False),
        ("lt", 2, 1, False),
        ("eq", 1, 1, True),
        ("eq", 1.0, 1, True),
        ("eq", 2, 1, False),
        ("gte", -1, -1, True),
        ("gt", -1, -1, False),
        ("lte", -2, -1, True),
        ("lt", -0.5, -1, False),
        ("eq", -1, -1, True),
        ("eq", 0.5, 0.5, True),
        ("gt", 0.5, 0.5, False),
    )
    for op, value, threshold, supported in cases:
        outcome, reason = score_metrics(
            {"score": value},
            (Criterion("score", op, threshold),),
            "completed",
        )
        if supported:
            assert outcome == "supported", op
            assert reason == "passed 1 criterion"
        else:
            assert outcome == "refuted", op
            assert f"metric score failed comparison {op}" in reason
            assert "missing" not in reason
            assert "execution status" not in reason

    criteria = (
        Criterion("keep", "eq", 1),
        Criterion("drop", "gt", 1),
        Criterion("later", "eq", 1),
    )
    outcome, reason = score_metrics(
        {"keep": 1, "drop": 1, "later": 0}, criteria, "completed"
    )
    assert outcome == "refuted"
    assert "drop" in reason
    assert "gt" in reason
    assert "later" not in reason

    outcome, reason = score_metrics(
        {"keep": 1, "drop": 2, "later": 1}, criteria, "completed"
    )
    assert outcome == "supported"
    assert reason == "passed 3 criteria"

    outcome, reason = score_metrics(
        {"score": True}, (Criterion("score", "gte", 1),), "completed"
    )
    assert outcome == "refuted"
    assert reason == "metric score is not a finite number"
    outcome, reason = score_metrics(
        {"score": float("nan")},
        (Criterion("score", "eq", 0),),
        "completed",
    )
    assert outcome == "refuted"
    assert reason == "metric score is not a finite number"


def test_missing_metric_refutes() -> None:
    criteria = (
        Criterion("present", "gte", 1),
        Criterion("absent", "eq", 1),
        Criterion("later", "eq", 0),
    )
    outcome, reason = score_metrics({"present": 1, "other": 5}, criteria, "completed")
    assert outcome == "refuted"
    assert reason == "missing metric absent"

    outcome, reason = score_metrics(
        {}, (Criterion("absent", "gte", 0), Criterion("later", "eq", 1)), "completed"
    )
    assert outcome == "refuted"
    assert reason == "missing metric absent"


@pytest.mark.parametrize("status", ["crashed", "timed_out", "canceled", "unknown"])
def test_failed_execution_refutes(status: str) -> None:
    criteria = (Criterion("score", "gte", 0), Criterion("loss", "lt", 1))
    outcome, reason = score_metrics({"score": 1, "loss": 0}, criteria, status)
    assert outcome == "refuted"
    assert reason == f"execution status {status} is not completed"

    outcome, reason = score_metrics({}, criteria, status)
    assert outcome == "refuted"
    assert reason == f"execution status {status} is not completed"


def test_mixed_verdict() -> None:
    criteria = (Criterion("score", "gte", 1),)
    supported = score_metrics({"score": 1}, criteria, "completed")
    low = score_metrics({"score": 0}, criteria, "completed")
    crashed = score_metrics({"score": 1}, criteria, "crashed")
    assert supported == ("supported", "passed 1 criterion")

    outcome, reason = score_campaign([supported, low, crashed])
    assert outcome == "refuted"
    assert reason.startswith("trial 2 refuted:")
    assert "score" in reason
    assert "trial 3" not in reason

    outcome, reason = score_campaign([crashed, supported])
    assert outcome == "refuted"
    assert reason == "trial 1 refuted: execution status crashed is not completed"

    outcome, reason = score_campaign([supported, supported])
    assert outcome == "supported"
    assert reason == "passed 2 trials"

    outcome, reason = score_campaign([supported])
    assert outcome == "supported"
    assert reason == "passed 1 trial"

    outcome, reason = score_campaign(["supported", "refuted"])
    assert outcome == "refuted"
    assert reason == "trial 2 refuted"
