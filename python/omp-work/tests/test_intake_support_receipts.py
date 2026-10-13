from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omp_work.intake_support import SupportReceipt

H = "a" * 64


def _receipt(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "verdict": "supports",
        "passage_sha256": H,
        "claim_text_sha256": H,
        "context_sha256": H,
        "prompt_sha256": H,
        "model_id": "judge-1",
        "measurement": "measured",
        "input_tokens": 10,
        "output_tokens": 2,
        "usage_id": "u1",
    }
    base.update(over)
    return base


def _parse(**over: object) -> SupportReceipt:
    return SupportReceipt.model_validate_json(json.dumps(_receipt(**over)))


def test_bad_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        _parse(verdict="maybe")


@pytest.mark.parametrize("field", ["passage_sha256", "claim_text_sha256", "context_sha256", "prompt_sha256"])
@pytest.mark.parametrize("bad", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_malformed_hash_rejected(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        _parse(**{field: bad})


def test_empty_model_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _parse(model_id="")


@pytest.mark.parametrize(
    "counts",
    [
        {"input_tokens": None, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": None},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": -1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": False},
        {"input_tokens": 1.0, "output_tokens": 1},
        {"input_tokens": "1", "output_tokens": 1},
    ],
)
def test_measured_bad_counts_rejected(counts: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _parse(measurement="measured", **counts)


@pytest.mark.parametrize(
    "counts",
    [
        {"input_tokens": 0, "output_tokens": None},
        {"input_tokens": None, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 3, "output_tokens": None},
    ],
)
def test_unknown_with_any_count_rejected(counts: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _parse(measurement="unknown", **counts)


def test_measured_zero_survives_json() -> None:
    r = _parse(input_tokens=0, output_tokens=0)
    assert (r.input_tokens, r.output_tokens) == (0, 0)


def test_unknown_nulls_survive_json() -> None:
    r = _parse(measurement="unknown", input_tokens=None, output_tokens=None, usage_id=None)
    assert r.input_tokens is None and r.output_tokens is None and r.usage_id is None
    assert r.model_dump(mode="json")["input_tokens"] is None
