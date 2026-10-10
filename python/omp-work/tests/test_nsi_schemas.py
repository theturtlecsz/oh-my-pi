"""NSI authority and taxonomy wire contracts reject invalid records."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource


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


TAXONOMY_SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/omp_work/contracts/nsi/v1/taxonomy.schema.json")
    .read_text()
)
TAXONOMY_VALIDATOR = Draft202012Validator(
    TAXONOMY_SCHEMA,
    registry=Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA)),
)
ENTITY = {"id": "entity-1", "type": "requirement"}
PREDICATE = {
    "id": "depends_on",
    "title": "Depends on",
    "subject_types": ["requirement"],
    "object_types": ["service"],
    "cardinality": "many",
    "exclusivity": False,
    "transitivity": True,
    "authority": ["owner", "rule_engine"],
}
DIMENSION = {
    "id": "availability",
    "title": "Availability",
    "category": "non_functional_qualities",
    "applicability": {"entity_types": [], "predicate_ids": []},
    "required_premises": ["uptime_target"],
    "materiality": "material",
    "waiver_policy": "owner_with_reason",
}


@pytest.mark.parametrize(
    "definition,record",
    [("EntityRef", ENTITY), ("PredicateDefinition", PREDICATE),
     ("RequirementDimensionDefinition", DIMENSION)],
)
def test_taxonomy_root_and_definitions_require_closed_complete_records(definition, record):
    Draft202012Validator.check_schema(TAXONOMY_SCHEMA)
    definition_validator = TAXONOMY_VALIDATOR.evolve(
        schema={**TAXONOMY_SCHEMA, "oneOf": [{"$ref": f"#/$defs/{definition}"}]}
    )
    for validator in (TAXONOMY_VALIDATOR, definition_validator):
        validator.validate(record)
        for field in record:
            incomplete = dict(record)
            del incomplete[field]
            with pytest.raises(ValidationError):
                validator.validate(incomplete)
        with pytest.raises(ValidationError):
            validator.validate({**record, "unexpected": True})


def test_intake_categories_accept_exactly_the_eleven_supported_categories():
    categories = [
        "functional_scope_and_behavior", "domain_and_data_model",
        "interaction_and_ux_flow", "non_functional_qualities",
        "integrations_and_external_dependencies", "edge_cases_and_failure_handling",
        "constraints_and_tradeoffs", "terminology", "completion_signals",
        "placeholders_and_ambiguities", "scope_boundaries",
    ]
    assert sorted(TAXONOMY_SCHEMA["$defs"]["IntakeCategory"]["enum"]) == sorted(categories)
    validator = TAXONOMY_VALIDATOR.evolve(
        schema={**TAXONOMY_SCHEMA, "oneOf": [{"$ref": "#/$defs/IntakeCategory"}]}
    )
    for category in categories:
        validator.validate(category)
        TAXONOMY_VALIDATOR.validate({**DIMENSION, "category": category})
    with pytest.raises(ValidationError):
        validator.validate("unknown")


@pytest.mark.parametrize(
    "record,field,value",
    [
        (ENTITY, "id", ""),
        (ENTITY, "type", ""),
        (PREDICATE, "subject_types", []),
        (PREDICATE, "subject_types", ["requirement", "requirement"]),
        (PREDICATE, "subject_types", [1]),
        (PREDICATE, "object_types", []),
        (PREDICATE, "object_types", ["service", "service"]),
        (PREDICATE, "object_types", [1]),
        (PREDICATE, "cardinality", "unknown"),
        (PREDICATE, "exclusivity", 1),
        (PREDICATE, "transitivity", "true"),
        (PREDICATE, "authority", []),
        (PREDICATE, "authority", ["unknown"]),
        (DIMENSION, "category", "unknown"),
        (DIMENSION, "applicability", {"entity_types": []}),
        (DIMENSION, "applicability", {"predicate_ids": []}),
        (DIMENSION, "applicability", {"entity_types": [], "predicate_ids": [], "extra": True}),
        (DIMENSION, "applicability", {"entity_types": [1], "predicate_ids": []}),
        (DIMENSION, "applicability", {"entity_types": [], "predicate_ids": [1]}),
        (DIMENSION, "required_premises", []),
        (DIMENSION, "required_premises", [1]),
        (DIMENSION, "materiality", "unknown"),
        (DIMENSION, "waiver_policy", "unknown"),
    ],
)
def test_taxonomy_rejects_invalid_types_bounds_and_authority_refs(record, field, value):
    with pytest.raises(ValidationError):
        TAXONOMY_VALIDATOR.validate({**record, field: value})


@pytest.mark.parametrize("materiality", ["critical", "material", "minor"])
def test_critical_dimensions_cannot_be_waived(materiality):
    TAXONOMY_VALIDATOR.validate({**DIMENSION, "materiality": materiality, "waiver_policy": "never"})
    record = {**DIMENSION, "materiality": materiality}
    if materiality == "critical":
        with pytest.raises(ValidationError):
            TAXONOMY_VALIDATOR.validate(record)
    else:
        TAXONOMY_VALIDATOR.validate(record)


def test_predicates_accept_single_cardinality_and_shared_actor_kinds():
    TAXONOMY_VALIDATOR.validate({
        **PREDICATE, "cardinality": "one", "exclusivity": True, "transitivity": False,
        "authority": ["owner", "service", "model", "tool", "rule_engine"],
    })
