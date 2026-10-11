"""NSI-2 intake write commands reject incomplete bodies and every result
survives HTTP response serialization (E0165).

Command-envelope validation and result serialization only.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omp_work.v1 import api_models, models
from omp_work.v1.canonical import text_sha256
from test_nsi_schemas import (
    ARTIFACT,
    CLAIM,
    CONFLICT,
    CRITERION,
    DECISION,
    DIMENSION,
    ENTITY,
    GAP,
    PREDICATE,
    QUESTION,
    REQUIREMENT,
)

ID = "11111111-1111-4111-8111-111111111111"
SOURCE = "Owner: café\n"
URI = "https://example.test/a"
HASH = "a" * 64
OTHER_HASH = "b" * 64

ARTIFACT = {**ARTIFACT, "uri": URI, "content_hash": HASH}
SPAN = {"artifact_id": ARTIFACT["id"], "start_byte": 0, "end_byte": 5, "quote": "Owner"}
CLAIM = {**CLAIM, "evidence": [SPAN]}
LINKS = [{"span": SPAN, "relation": "supports"}]
CONFLICT = {**CONFLICT, "claim_ids": ["claim-1", "claim-2"]}
INSTRUCTION = {
    "text": "Require 99.9% availability.",
    "provenance": {
        "channel": "test-owner",
        "message_ref": "message-1",
        "received_at": "2026-10-11T00:00:00Z",
    },
}
DECISION_RECORD = {
    "decision": {**DECISION, "revision": 1},
    "claim_ids": [CLAIM["id"]],
    "instruction": INSTRUCTION,
    "superseded_decision_id": None,
    "verified_relay_operation_id": None,
}
ISSUE_RECORD = {"issue": CONFLICT, "gaps": [GAP["id"]]}
CRITERION_LINK = {"criterion": CRITERION, "requirements": [REQUIREMENT]}
ONTOLOGY = {"predicates": [PREDICATE], "dimensions": [DIMENSION]}
BASE = {"run_id": "run-1", "expected_revision": 1}

PAYLOADS: dict[str, dict[str, object]] = {
    "start_intake": {
        **BASE,
        "expected_revision": 0,
        "mission_id": ID,
        "ontology": ONTOLOGY,
        "entities": [ENTITY],
    },
    "register_intake_evidence": {
        **BASE,
        "artifact": ARTIFACT,
        "source": {"text": SOURCE, "sha256": text_sha256(SOURCE)},
    },
    "record_intake_claim": {**BASE, "claim": CLAIM, "evidence_links": LINKS},
    "record_intake_decision": {**BASE, **DECISION_RECORD},
    "supersede_intake_claim": {
        **BASE,
        "old_claim_id": "claim-1",
        "replacement_claim_id": "claim-2",
        "decision_id": "decision-1",
    },
    "record_intake_issue": {**BASE, **ISSUE_RECORD},
    "link_intake_criterion": {**BASE, **CRITERION_LINK},
    "assess_intake": BASE,
}

FOLLOW_ON = [
    "register_intake_evidence",
    "record_intake_claim",
    "record_intake_decision",
    "supersede_intake_claim",
    "record_intake_issue",
    "link_intake_criterion",
]
FORBIDDEN_PAYLOAD_FIELDS = ("operation_id", "request_id", "correlation_id", "request_sha256")


def envelope(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": ID,
        "operation_id": ID,
        "request_id": ID,
        "correlation_id": ID,
        "command": {"type": kind, "payload": payload},
    }


@pytest.mark.parametrize("kind,payload", PAYLOADS.items())
def test_every_intake_command_accepts_a_complete_body(kind, payload):
    parsed = models.CommandEnvelope.model_validate(envelope(kind, payload))
    assert parsed.command.type == kind
    assert parsed.command.payload.run_id == "run-1"


@pytest.mark.parametrize("kind,payload", PAYLOADS.items())
@pytest.mark.parametrize("omission", ["run_id", "expected_revision", "unknown_key"])
def test_every_intake_command_rejects_incomplete_bodies(kind, payload, omission):
    if omission == "unknown_key":
        broken = {**payload, "unexpected": ID}
    else:
        broken = {key: value for key, value in payload.items() if key != omission}
    with pytest.raises(ValidationError):
        models.CommandEnvelope.model_validate(envelope(kind, broken))


def test_only_start_intake_begins_at_revision_zero():
    with pytest.raises(ValidationError):
        models.CommandEnvelope.model_validate(
            envelope("start_intake", {**PAYLOADS["start_intake"], "expected_revision": 1})
        )
    for kind in FOLLOW_ON + ["assess_intake"]:
        with pytest.raises(ValidationError):
            models.CommandEnvelope.model_validate(
                envelope(kind, {**PAYLOADS[kind], "expected_revision": 0})
            )


@pytest.mark.parametrize(
    "kind,field,value",
    [
        (
            "register_intake_evidence",
            "artifact",
            {**ARTIFACT, "uri": "x"},
        ),
        (
            "register_intake_evidence",
            "artifact",
            {**ARTIFACT, "content_hash": "zz"},
        ),
        (
            "record_intake_claim",
            "evidence_links",
            [{"span": SPAN, "relation": "maybe"}],
        ),
        (
            "record_intake_claim",
            "evidence_links",
            [{"span": {**SPAN, "start_byte": 5, "end_byte": 5}, "relation": "neither"}],
        ),
        (
            "record_intake_issue",
            "issue",
            {**CONFLICT, "claim_ids": ["claim-1"]},
        ),
        (
            "link_intake_criterion",
            "criterion",
            {**CRITERION, "requirement_ids": []},
        ),
        ("assess_intake", "verdict", "PASS"),
    ],
)
def test_nested_intake_records_reject_invalid_values(kind, field, value):
    with pytest.raises(ValidationError):
        models.CommandEnvelope.model_validate(
            envelope(kind, {**PAYLOADS[kind], field: value})
        )


@pytest.mark.parametrize("kind", PAYLOADS)
def test_intake_payloads_carry_no_operation_metadata(kind):
    payload_cls = type(models.CommandEnvelope.model_validate(envelope(kind, PAYLOADS[kind])).command.payload)
    assert not set(payload_cls.model_fields) & set(FORBIDDEN_PAYLOAD_FIELDS)


def test_intake_source_binds_text_sha256():
    parsed = models.CommandEnvelope.model_validate(
        envelope("register_intake_evidence", PAYLOADS["register_intake_evidence"])
    )
    assert parsed.command.payload.source.sha256 == text_sha256(SOURCE)
    with pytest.raises(ValidationError):
        models.CommandEnvelope.model_validate(
            envelope(
                "register_intake_evidence",
                {
                    **PAYLOADS["register_intake_evidence"],
                    "source": {"text": SOURCE, "sha256": OTHER_HASH},
                },
            )
        )


def valid_result(kind: str) -> dict[str, object]:
    if kind == "start_intake":
        return {"type": kind, "run_id": "run-1", "expected_revision": 0, "revision": 1}
    if kind == "assess_intake":
        return {"type": kind, "run_id": "run-1", "revision": 2, "assessed_revision": 2}
    return {"type": kind, "run_id": "run-1", "expected_revision": 1, "revision": 2}


RECEIPT = {
    "operation_id": ID,
    "request_id": ID,
    "state": "applied",
    "request_sha256": HASH,
    "result_sha256": OTHER_HASH,
}


@pytest.mark.parametrize("kind", PAYLOADS)
def test_every_intake_result_serializes_through_response_and_stored_operation(kind):
    """E0165: an applied intake write must survive HTTP response serialization."""
    result = valid_result(kind)
    response = api_models.CommandResponse.model_validate({"receipt": RECEIPT, "result": result})
    reparsed = api_models.CommandResponse.model_validate_json(response.model_dump_json())
    assert response.result.type == kind == reparsed.result.type

    stored = api_models.StoredOperationView.model_validate(
        {
            "receipt": RECEIPT,
            "command_type": kind,
            "request_id": ID,
            "correlation_id": ID,
            "result": result,
        }
    )
    restored = api_models.StoredOperationView.model_validate_json(stored.model_dump_json())
    assert stored.result.type == kind == restored.result.type


def test_follow_on_intake_results_require_an_advancing_revision():
    for kind in FOLLOW_ON:
        with pytest.raises(ValidationError):
            api_models.CommandResponse.model_validate(
                {
                    "receipt": RECEIPT,
                    "result": {
                        "type": kind,
                        "run_id": "run-1",
                        "expected_revision": 0,
                        "revision": 1,
                    },
                }
            )
    with pytest.raises(ValidationError):
        api_models.CommandResponse.model_validate(
            {
                "receipt": RECEIPT,
                "result": {
                    "type": "start_intake",
                    "run_id": "run-1",
                    "expected_revision": 1,
                    "revision": 2,
                },
            }
        )


def test_assess_intake_result_binds_assessed_revision_without_verdict():
    accepted = api_models.CommandResponse.model_validate(
        {"receipt": RECEIPT, "result": valid_result("assess_intake")}
    )
    wire = json.loads(accepted.model_dump_json())
    assert wire["result"]["revision"] == 2 == wire["result"]["assessed_revision"]
    assert "verdict" not in wire["result"]

    with pytest.raises(ValidationError):
        api_models.CommandResponse.model_validate(
            {
                "receipt": RECEIPT,
                "result": {
                    "type": "assess_intake",
                    "run_id": "run-1",
                    "revision": 3,
                    "assessed_revision": 2,
                },
            }
        )
