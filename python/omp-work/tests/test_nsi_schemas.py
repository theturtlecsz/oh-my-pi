"""NSI authority wire contract accepts valid records and rejects authority gaps."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/omp_work/contracts/nsi/v1/authority.schema.json")
    .read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA)
RULE = {"actor_kind": "owner", "allowed_operations": ["propose", "approve"]}
MUTATION = {
    "operation_id": "operation-1",
    "run_id": "run-1",
    "expected_revision": 0,
    "actor": {"id": "model-1", "kind": "model"},
    "operation": "propose",
    "payload": {},
}
WAIVER = {
    "id": "waiver-1",
    "run_id": "run-1",
    "revision": 0,
    "premise_id": "premise-1",
    "dimension_id": "dimension-1",
    "materiality": "material",
    "waiver_policy": "owner_with_reason",
    "granted_by": {"id": "owner-1", "kind": "owner"},
    "reason": "Owner accepts this gap.",
    "decision_id": "decision-1",
    "created_at": "2026-10-10T00:00:00Z",
}


@pytest.mark.parametrize("record", [RULE, MUTATION, WAIVER])
def test_authority_root_accepts_records_and_rejects_missing_or_extra_fields(record):
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(record)
    for field in record:
        incomplete = dict(record)
        del incomplete[field]
        with pytest.raises(ValidationError):
            VALIDATOR.validate(incomplete)
    with pytest.raises(ValidationError):
        VALIDATOR.validate({**record, "unexpected": True})


@pytest.mark.parametrize("kind", ["owner", "service", "model", "tool", "rule_engine"])
def test_only_models_are_restricted_to_proposals(kind):
    record = deepcopy(MUTATION)
    record["actor"]["kind"] = kind
    VALIDATOR.validate(record)
    record["operation"] = "approve"
    if kind == "model":
        with pytest.raises(ValidationError):
            VALIDATOR.validate(record)
    else:
        VALIDATOR.validate(record)


@pytest.mark.parametrize(
    "record,field,value",
    [
        (RULE, "actor_kind", "unknown"),
        (RULE, "allowed_operations", []),
        (RULE, "allowed_operations", ["propose", "propose"]),
        (RULE, "allowed_operations", [1]),
        (MUTATION, "expected_revision", -1),
        (MUTATION, "expected_revision", 0.5),
        (MUTATION, "actor", {"id": "", "kind": "model"}),
        (MUTATION, "actor", {"id": "x", "kind": "unknown"}),
        (MUTATION, "actor", {"id": "x", "kind": "model", "extra": True}),
        (MUTATION, "payload", []),
        (MUTATION, "payload", {"extra": True}),
        (WAIVER, "revision", -1),
        (WAIVER, "revision", 0.5),
        (WAIVER, "materiality", "critical"),
        (WAIVER, "waiver_policy", "unknown"),
        (WAIVER, "granted_by", {"id": "service-1", "kind": "service"}),
        (WAIVER, "granted_by", {"id": "", "kind": "owner"}),
        (WAIVER, "granted_by", {"id": "x", "kind": "owner", "extra": True}),
        (WAIVER, "reason", ""),
    ],
)
def test_authority_root_rejects_invalid_authority_records(record, field, value):
    with pytest.raises(ValidationError):
        VALIDATOR.validate({**record, field: value})


def test_waiver_accepts_minor_materiality_and_never_policy():
    VALIDATOR.validate({**WAIVER, "materiality": "minor", "waiver_policy": "never"})


@pytest.mark.parametrize("definition,kind", [("OwnerRef", "owner"), ("RuleEngineRef", "rule_engine")])
def test_specialized_actor_refs_reject_wrong_kind_empty_id_and_open_fields(definition, kind):
    validator = Draft202012Validator({**SCHEMA, "oneOf": [{"$ref": f"#/$defs/{definition}"}]})
    validator.validate({"id": "actor-1", "kind": kind})
    for invalid in (
        {"id": "actor-1", "kind": "model"},
        {"id": "", "kind": kind},
        {"id": "actor-1"},
        {"kind": kind},
        {"id": "actor-1", "kind": kind, "extra": True},
    ):
        with pytest.raises(ValidationError):
            validator.validate(invalid)
