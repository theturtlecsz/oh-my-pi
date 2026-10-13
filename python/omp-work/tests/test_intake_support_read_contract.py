"""Read contract for ``IntakeRecordView.support_judgments`` (OMP-604).

A consumer reading an intake record sees ``support_judgments`` as an array: absent
input from a legacy writer reads as ``[]``, a populated judgment round-trips through
JSON, and malformed judgments (bad verdict, bad hash, non-strict types) are refused
both by the pydantic model and by the generated ``api-schema.json``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from omp_work.v1.api_models import IntakeRecordView
from pydantic import ValidationError

HASH = "a" * 64


def _record() -> dict[str, Any]:
    return {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "mission_id": "00000000-0000-0000-0000-000000000002",
        "run_id": "run-1",
        "revision": 1,
        "snapshot": {
            "run_id": "run-1",
            "revision": 0,
            "state": "collecting",
            "artifacts": [],
            "entities": [],
            "claims": [],
            "ontology": {"predicates": [], "dimensions": []},
        },
        "evidence_links": [],
        "criterion_links": [],
        "decisions": [],
        "issues": [],
        "traces": [],
        "assessments": [],
        "provenance": {"run_id": "run-1", "revision": 1, "source_sha256": HASH},
    }


def _judgment(**overrides: Any) -> dict[str, Any]:
    return {
        "claim_id": "claim-1",
        "artifact_id": "artifact-1",
        "model_id": "judge-1",
        "source_revision": "revision-1",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "rule_version": "rule-v1",
        "context_sha256": "c" * 64,
        "start_byte": 0,
        "end_byte": 12,
        "caller_relation": "supports",
        "verdict": "neither",
        "passage_sha256": HASH,
        "claim_text_sha256": "b" * 64,
        "disagreement": True,
        **overrides,
    }


def _schema_validator() -> Draft202012Validator:
    text = (resources.files("omp_work") / "contracts/v1/api-schema.json").read_text()
    model = json.loads(text)["models"]["IntakeRecordView"]
    return Draft202012Validator(model)


BAD_JUDGMENTS = {
    "verdict": _judgment(verdict="maybe"),
    "caller_relation": _judgment(caller_relation="refutes"),
    "short_hash": _judgment(passage_sha256="abc"),
    "upper_hash": _judgment(claim_text_sha256="A" * 64),
    "negative_offset": _judgment(start_byte=-1),
    "string_offset": _judgment(end_byte="12"),
    "string_disagreement": _judgment(disagreement="true"),
    "empty_model": _judgment(model_id=""),
    "empty_source_revision": _judgment(source_revision=""),
    "empty_model_version": _judgment(model_version=""),
    "empty_prompt_version": _judgment(prompt_version=""),
    "empty_rule_version": _judgment(rule_version=""),
    "bad_context_hash": _judgment(context_sha256="abc"),
    "pending_caller_label": _judgment(caller_relation="abstain"),
}


def test_omitted_support_judgments_read_as_empty_array() -> None:
    view = IntakeRecordView.model_validate(_record())

    assert view.support_judgments == ()
    assert json.loads(view.model_dump_json())["support_judgments"] == []
    _schema_validator().validate(_record())


def test_populated_support_judgments_round_trip_and_match_schema() -> None:
    payload = {**_record(), "support_judgments": [_judgment(), _judgment(verdict="supports", disagreement=False)]}

    view = IntakeRecordView.model_validate(payload)
    wire = json.loads(view.model_dump_json())

    assert wire["support_judgments"] == payload["support_judgments"]
    assert IntakeRecordView.model_validate_json(view.model_dump_json()) == view
    _schema_validator().validate(wire)


@pytest.mark.parametrize("bad", BAD_JUDGMENTS.values(), ids=BAD_JUDGMENTS.keys())
def test_malformed_judgment_rejected_by_model_and_schema(bad: dict[str, Any]) -> None:
    payload = {**deepcopy(_record()), "support_judgments": [bad]}

    with pytest.raises(ValidationError):
        IntakeRecordView.model_validate(payload)
    assert not _schema_validator().is_valid(payload)


@pytest.mark.parametrize(
    ("caller_relation", "verdict"),
    [
        ("supports", "supports"),
        ("supports", "contradicts"),
        ("supports", "neither"),
        ("supports", "abstain"),
        ("supports", "timeout"),
        ("supports", "evidence-unavailable"),
        ("contradicts", "contradicts"),
        ("neither", "neither"),
        ("contradicts", "neither"),
        ("neither", "contradicts"),
    ],
)
def test_six_verdicts_preserve_judge_state_and_disagreement(caller_relation: str, verdict: str) -> None:
    judgment = _judgment(caller_relation=caller_relation, verdict=verdict, disagreement=verdict != caller_relation)
    view = IntakeRecordView.model_validate({**_record(), "support_judgments": [judgment]})
    wire = json.loads(view.model_dump_json())

    assert wire["support_judgments"][0] == judgment
    _schema_validator().validate(wire)
    assert (view.support_judgments[0].verdict == "supports") == (verdict == "supports")
    with pytest.raises(ValidationError, match="disagreement"):
        IntakeRecordView.model_validate({
            **_record(), "support_judgments": [{**judgment, "disagreement": not judgment["disagreement"]}]
        })


@pytest.mark.parametrize("binding", ["source_revision", "model_version", "prompt_version", "rule_version", "context_sha256"])
def test_missing_receipt_binding_refused(binding: str) -> None:
    judgment = _judgment()
    del judgment[binding]
    payload = {**_record(), "support_judgments": [judgment]}
    with pytest.raises(ValidationError):
        IntakeRecordView.model_validate(payload)
    assert not _schema_validator().is_valid(payload)


def test_reversed_source_span_refused() -> None:
    with pytest.raises(ValidationError, match="source span end precedes start"):
        IntakeRecordView.model_validate({**_record(), "support_judgments": [_judgment(start_byte=13)]})
