"""NSI authority, taxonomy, claim, and status wire contracts reject invalid records."""

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


CLAIM_SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/omp_work/contracts/nsi/v1/claim.schema.json")
    .read_text()
)
CLAIM_VALIDATOR = Draft202012Validator(
    CLAIM_SCHEMA,
    registry=Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in (SCHEMA, TAXONOMY_SCHEMA)
    ),
)
CLAIM = {
    "id": "claim-1",
    "category": "non_functional_qualities",
    "subject": ENTITY,
    "predicate_id": "uptime_target",
    "value": {"type": "number", "value": 99.9},
    "origin": "model_proposal",
    "state": "proposed",
    "modality": "desired",
    "polarity": "positive",
    "truth": "unknown",
    "asserted_by": {"id": "model-1", "kind": "model"},
    "evidence": [],
    "premise_ids": [],
}
ARTIFACT = {
    "id": "artifact-1",
    "uri": "https://example.com/requirements",
    "content_hash": "a" * 64,
    "revision": "revision-1",
    "actor": {"id": "tool-1", "kind": "tool"},
    "media_type": "text/plain",
}
SPAN = {"artifact_id": "artifact-1", "start_byte": 0, "end_byte": 12}


def claim_definition_validator(definition):
    return CLAIM_VALIDATOR.evolve(
        schema={**CLAIM_SCHEMA, "$ref": f"#/$defs/{definition}"}
    )


@pytest.mark.parametrize(
    "definition,record",
    [("IntakeClaim", CLAIM), ("EvidenceArtifact", ARTIFACT), ("EvidenceSpan", SPAN)],
)
def test_claim_records_require_complete_closed_wire_shapes(definition, record):
    Draft202012Validator.check_schema(CLAIM_SCHEMA)
    validator = claim_definition_validator(definition)
    validator.validate(record)
    if definition == "IntakeClaim":
        CLAIM_VALIDATOR.validate(record)
    for field in record:
        incomplete = dict(record)
        del incomplete[field]
        with pytest.raises(ValidationError):
            validator.validate(incomplete)
    with pytest.raises(ValidationError):
        validator.validate({**record, "unexpected": True})


@pytest.mark.parametrize(
    "definition,values",
    [
        ("ClaimOrigin", ["owner_statement", "policy_rule", "repository_observation",
                         "tool_receipt", "model_proposal", "rule_derivation"]),
        ("ClaimState", ["proposed", "admitted", "rejected", "superseded"]),
        ("ClaimModality", ["observed", "desired", "required", "prohibited", "possible", "unknown"]),
        ("Polarity", ["positive", "negative"]),
    ],
)
def test_claim_enums_accept_exact_wire_vocabulary(definition, values):
    assert sorted(CLAIM_SCHEMA["$defs"][definition]["enum"]) == sorted(values)
    validator = claim_definition_validator(definition)
    for value in values:
        validator.validate(value)
    with pytest.raises(ValidationError):
        validator.validate("unsupported")


@pytest.mark.parametrize(
    "kind,valid,invalid",
    [("string", "", 1), ("integer", 1, True), ("integer", 0, 1.5),
     ("number", 1.5, True), ("boolean", False, 0), ("null", None, "null"),
     ("entity", ENTITY, {"id": "entity-1"})],
)
def test_typed_values_enforce_tagged_json_types_and_entity_refs(kind, valid, invalid):
    validator = claim_definition_validator("TypedValue")
    record = {"type": kind, "value": valid}
    validator.validate(record)
    CLAIM_VALIDATOR.validate({**CLAIM, "value": record})
    for bad in ({"type": kind, "value": invalid}, {"value": valid},
                {"type": kind}, {**record, "extra": True}):
        with pytest.raises(ValidationError):
            validator.validate(bad)


@pytest.mark.parametrize(
    "definition,record,field,value",
    [
        ("EvidenceArtifact", ARTIFACT, "id", ""),
        ("EvidenceArtifact", ARTIFACT, "uri", 1),
        ("EvidenceArtifact", ARTIFACT, "content_hash", "a" * 63),
        ("EvidenceArtifact", ARTIFACT, "content_hash", "A" * 64),
        ("EvidenceArtifact", ARTIFACT, "content_hash", "g" * 64),
        ("EvidenceArtifact", ARTIFACT, "revision", ""),
        ("EvidenceArtifact", ARTIFACT, "actor", {"id": "tool-1", "kind": "unknown"}),
        ("EvidenceArtifact", ARTIFACT, "media_type", ""),
        ("EvidenceSpan", SPAN, "artifact_id", ""),
        ("EvidenceSpan", SPAN, "start_byte", -1),
        ("EvidenceSpan", SPAN, "end_byte", -1),
        ("EvidenceSpan", SPAN, "start_byte", 0.5),
        ("EvidenceSpan", SPAN, "end_byte", True),
        ("EvidenceSpan", SPAN, "quote", ""),
        ("IntakeClaim", CLAIM, "category", "unknown"),
        ("IntakeClaim", CLAIM, "subject", {"id": "entity-1", "type": ""}),
        ("IntakeClaim", CLAIM, "truth", True),
        ("IntakeClaim", CLAIM, "truth", "unsupported"),
        ("IntakeClaim", CLAIM, "premise_ids", [1]),
        ("IntakeClaim", CLAIM, "evidence", [{**SPAN, "start_byte": -1}]),
        ("IntakeClaim", CLAIM, "admitted_by", {"id": "model-1", "kind": "model"}),
    ],
)
def test_claim_records_reject_invalid_evidence_and_cross_file_refs(definition, record, field, value):
    with pytest.raises(ValidationError):
        claim_definition_validator(definition).validate({**record, field: value})


@pytest.mark.parametrize(
    "origin,required_kind",
    [("model_proposal", "model"), ("owner_statement", "owner"),
     ("rule_derivation", "rule_engine"), ("policy_rule", None),
     ("repository_observation", None), ("tool_receipt", None)],
)
def test_claim_origin_constrains_asserting_actor(origin, required_kind):
    for kind in ("owner", "service", "model", "tool", "rule_engine"):
        record = {**CLAIM, "origin": origin, "asserted_by": {"id": "actor-1", "kind": kind}}
        if required_kind is not None and kind != required_kind:
            with pytest.raises(ValidationError):
                CLAIM_VALIDATOR.validate(record)
        else:
            CLAIM_VALIDATOR.validate(record)


@pytest.mark.parametrize("state", ["proposed", "admitted", "rejected", "superseded"])
def test_admission_requires_authority_and_rule_engine_evidence(state):
    record = {**CLAIM, "state": state}
    if state == "admitted":
        with pytest.raises(ValidationError):
            CLAIM_VALIDATOR.validate(record)
    else:
        CLAIM_VALIDATOR.validate(record)
    CLAIM_VALIDATOR.validate({**record, "admitted_by": {"id": "owner-1", "kind": "owner"}})
    rule_record = {**record, "admitted_by": {"id": "engine-1", "kind": "rule_engine"}}
    if state == "admitted":
        with pytest.raises(ValidationError):
            CLAIM_VALIDATOR.validate(rule_record)
    else:
        CLAIM_VALIDATOR.validate(rule_record)
    CLAIM_VALIDATOR.validate({**rule_record, "evidence": [SPAN]})
    CLAIM_VALIDATOR.validate({**rule_record, "evidence": [{**SPAN, "quote": "Requirements"}]})


STATUS_SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/omp_work/contracts/nsi/v1/status.schema.json")
    .read_text()
)
STATUS_VALIDATOR = Draft202012Validator(
    STATUS_SCHEMA,
    registry=Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in (SCHEMA, TAXONOMY_SCHEMA, CLAIM_SCHEMA)
    ),
)
SHA256 = "a" * 64
RECEIPT = {
    "id": "receipt-1",
    "run_id": "run-1",
    "revision": 0,
    "contract_hash": SHA256,
    "claim_set_hash": SHA256,
    "rule_bundle_hash": SHA256,
    "schema_hash": SHA256,
    "source_hash": SHA256,
    "rule_id": "rule-1",
    "premise_ids": ["premise-1"],
    "conclusion_ids": ["conclusion-1"],
    "derived_by": {"id": "engine-1", "kind": "rule_engine"},
    "created_at": "2026-10-10T00:00:00Z",
}
GAP = {
    "id": "gap-1",
    "dimension_id": "dimension-1",
    "premise_id": "premise-1",
    "reason": "Required premise is unknown.",
    "materiality": "material",
    "waiver_policy": "owner_with_reason",
}
CONFLICT = {
    "id": "conflict-1",
    "claim_ids": ["claim-1", "claim-2"],
    "predicate_id": "uptime_target",
    "reason": "Admitted claims contradict.",
}
QUESTION = {
    "id": "question-1",
    "question": "What uptime is required?",
    "gap_ids": ["gap-1"],
    "conflict_ids": [],
    "category": "non_functional_qualities",
}
DECISION = {
    "id": "decision-1",
    "run_id": "run-1",
    "revision": 0,
    "actor": {"id": "owner-1", "kind": "owner"},
    "question_id": "question-1",
    "answer": {"type": "string", "value": "99.9"},
    "reason": "Owner confirmed the target.",
    "created_at": "2026-10-10T00:00:00Z",
}
INTAKE_RUN_STATES = [
    "collecting", "reasoning", "needs_spec", "conflicted", "ready_for_ratification",
    "ratified", "published", "stale", "invalid", "superseded",
]


@pytest.mark.parametrize(
    "definition,record",
    [("DerivationReceipt", RECEIPT), ("Gap", GAP), ("Conflict", CONFLICT),
     ("ClarificationQuestion", QUESTION), ("OwnerDecision", DECISION)],
)
def test_status_root_and_definitions_require_closed_complete_records(definition, record):
    Draft202012Validator.check_schema(STATUS_SCHEMA)
    definition_validator = STATUS_VALIDATOR.evolve(
        schema={**STATUS_SCHEMA, "oneOf": [{"$ref": f"#/$defs/{definition}"}]}
    )
    for validator in (STATUS_VALIDATOR, definition_validator):
        validator.validate(record)
        for field in record:
            incomplete = dict(record)
            del incomplete[field]
            with pytest.raises(ValidationError):
                validator.validate(incomplete)
        with pytest.raises(ValidationError):
            validator.validate({**record, "unexpected": True})


def test_intake_run_state_accepts_exactly_the_ten_lifecycle_states():
    assert STATUS_SCHEMA["$defs"]["IntakeRunState"]["enum"] == INTAKE_RUN_STATES
    validator = STATUS_VALIDATOR.evolve(
        schema={**STATUS_SCHEMA, "oneOf": [{"$ref": "#/$defs/IntakeRunState"}]}
    )
    for state in INTAKE_RUN_STATES:
        validator.validate(state)
    with pytest.raises(ValidationError):
        validator.validate("unknown")
    with pytest.raises(ValidationError):
        STATUS_VALIDATOR.validate("collecting")


@pytest.mark.parametrize(
    "field",
    ["contract_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "source_hash"],
)
@pytest.mark.parametrize("value", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, ""])
def test_receipt_hashes_are_lowercase_sha256(field, value):
    with pytest.raises(ValidationError):
        STATUS_VALIDATOR.validate({**RECEIPT, field: value})


@pytest.mark.parametrize(
    "record,field,value",
    [
        (RECEIPT, "revision", -1),
        (RECEIPT, "revision", 0.5),
        (RECEIPT, "premise_ids", [1]),
        (RECEIPT, "conclusion_ids", [1]),
        (RECEIPT, "derived_by", {"id": "engine-1", "kind": "owner"}),
        (RECEIPT, "derived_by", {"id": "", "kind": "rule_engine"}),
        (RECEIPT, "derived_by", {"id": "engine-1", "kind": "rule_engine", "extra": True}),
        (GAP, "reason", ""),
        (GAP, "materiality", "unknown"),
        (GAP, "waiver_policy", "unknown"),
        (CONFLICT, "claim_ids", ["claim-1"]),
        (CONFLICT, "claim_ids", []),
        (CONFLICT, "claim_ids", ["claim-1", "claim-1"]),
        (CONFLICT, "claim_ids", ["claim-1", 2]),
        (QUESTION, "question", ""),
        (QUESTION, "gap_ids", [1]),
        (QUESTION, "conflict_ids", [1]),
        (QUESTION, "category", "unknown"),
        (DECISION, "revision", -1),
        (DECISION, "revision", 0.5),
        (DECISION, "actor", {"id": "engine-1", "kind": "rule_engine"}),
        (DECISION, "actor", {"id": "", "kind": "owner"}),
        (DECISION, "actor", {"id": "owner-1", "kind": "owner", "extra": True}),
        (DECISION, "answer", {"type": "string"}),
        (DECISION, "answer", {"type": "entity", "value": {"id": "entity-1"}}),
        (DECISION, "reason", 1),
        (DECISION, "created_at", 1),
    ],
)
def test_status_records_reject_invalid_bounds_hashes_and_cross_file_refs(record, field, value):
    with pytest.raises(ValidationError):
        STATUS_VALIDATOR.validate({**record, field: value})


@pytest.mark.parametrize("materiality", ["critical", "material", "minor"])
def test_critical_gaps_cannot_be_waived(materiality):
    STATUS_VALIDATOR.validate({**GAP, "materiality": materiality, "waiver_policy": "never"})
    record = {**GAP, "materiality": materiality, "waiver_policy": "owner_with_reason"}
    if materiality == "critical":
        with pytest.raises(ValidationError):
            STATUS_VALIDATOR.validate(record)
    else:
        STATUS_VALIDATOR.validate(record)


def test_status_records_accept_empty_lists_plain_timestamps_and_typed_answers():
    STATUS_VALIDATOR.validate({
        **RECEIPT,
        "contract_hash": "0123456789abcdef" * 4,
        "claim_set_hash": "fedcba9876543210" * 4,
        "premise_ids": [],
        "conclusion_ids": [],
        "created_at": "not-a-timestamp",
    })
    STATUS_VALIDATOR.validate({**QUESTION, "gap_ids": [], "conflict_ids": []})
    STATUS_VALIDATOR.validate({**CONFLICT, "claim_ids": ["claim-1", "claim-2", "claim-3"], "reason": ""})
    STATUS_VALIDATOR.validate({**DECISION, "reason": "", "created_at": "not-a-timestamp"})
    for category in TAXONOMY_SCHEMA["$defs"]["IntakeCategory"]["enum"]:
        STATUS_VALIDATOR.validate({**QUESTION, "category": category})
    for answer in (
        {"type": "string", "value": ""},
        {"type": "integer", "value": 0},
        {"type": "number", "value": 1.5},
        {"type": "boolean", "value": False},
        {"type": "null", "value": None},
        {"type": "entity", "value": {"id": "entity-1", "type": "requirement"}},
    ):
        STATUS_VALIDATOR.validate({**DECISION, "answer": answer})
