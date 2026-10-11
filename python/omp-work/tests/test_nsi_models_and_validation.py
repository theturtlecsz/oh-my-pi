"""Runtime NSI contracts reject malformed wire data and preserve semantic authority."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from omp_work.contracts.nsi.v1 import models
from omp_work.contracts.nsi.v1 import (
    closure_hash, content_hash, contract_hash, load_schema, premise_truth,
    validate_instance, validate_unknown_semantics, validate_waiver,
)
from test_nsi_schemas import (
    ARTIFACT, BINDING, CLAIM, CONFLICT, CONTRACT, CONTRACT_RECORDS, DECISION,
    DIMENSION, ENTITY, GAP, PREDICATE, QUESTION, RECEIPT, WAIVER,
)

RECORDS = [(name, record) for name, record in CONTRACT_RECORDS] + [
    ("IntakeClaim", CLAIM), ("PredicateDefinition", PREDICATE),
    ("RequirementDimensionDefinition", DIMENSION), ("DerivationReceipt", RECEIPT),
    ("Gap", GAP), ("Conflict", CONFLICT), ("OwnerDecision", DECISION),
    ("ClarificationQuestion", QUESTION),
]


@pytest.mark.parametrize("name,record", RECORDS)
def test_runtime_records_roundtrip_schema_wire_shapes_and_reject_missing_fields(name, record):
    cls = getattr(models, name)
    model = cls.model_validate(record)
    assert model.model_dump(exclude_unset=True) == record
    validate_instance(model.schema_name, model)
    for field in record:
        missing = dict(record)
        del missing[field]
        with pytest.raises(ValidationError):
            cls.model_validate(missing)
    with pytest.raises(ValidationError):
        cls.model_validate({**record, "extra": True})


def snapshot():
    return models.IntakeSnapshot(
        run_id="run-1", revision=0, state="collecting", artifacts=[ARTIFACT],
        entities=[ENTITY], claims=[CLAIM],
        ontology={"predicates": [PREDICATE], "dimensions": [DIMENSION]},
    )


def test_snapshot_ontology_preserve_typed_records_and_validate_nested_claims():
    model = snapshot()
    assert isinstance(model.ontology, models.IntakeOntology)
    assert isinstance(model.claims[0], models.IntakeClaim)
    wire = model.model_dump(exclude_unset=True)
    wire["claims"][0]["origin"] = "markdown"
    with pytest.raises(ValidationError):
        models.IntakeSnapshot.model_validate(wire)
    with pytest.raises(ValidationError):
        models.IntakeSnapshot.model_validate({**model.model_dump(), "revision": -1})


@pytest.mark.parametrize("field,value", [
    ("origin", "markdown"), ("modality", "certain"), ("truth", False),
    ("asserted_by", {"id": "owner-1", "kind": "owner"}),
    ("value", {"type": "boolean", "value": "false"}),
    ("value", {"type": "integer", "value": True}),
    ("value", {"type": "number", "value": float("nan")}),
    ("state", "published"),
])
def test_claim_model_and_schema_reject_invalid_types_origins_modalities_and_authority(field, value):
    record = {**CLAIM, field: value}
    with pytest.raises(ValidationError):
        models.IntakeClaim.model_validate(record)
    with pytest.raises(ValueError):
        validate_instance("claim", record)


@pytest.mark.parametrize("value", ["0", True, 0.0])
def test_bound_model_rejects_revision_coercion(value):
    with pytest.raises(ValidationError):
        models.Binding.model_validate({**BINDING, "revision": value})
    with pytest.raises(ValueError):
        validate_instance("contract#/$defs/Binding", {**BINDING, "revision": value})


def test_schema_resolution_is_offline_closed_and_checks_format_and_bounds():
    validate_instance("claim.schema.json", CLAIM)
    with pytest.raises(ValueError):
        load_schema("../v1/schema.json")
    with pytest.raises(ValueError):
        validate_instance("claim#/$defs/Missing", CLAIM)
    with pytest.raises(ValueError):
        validate_instance("claim#/$defs/EvidenceArtifact", {**ARTIFACT, "uri": "invalid"})
    with pytest.raises(ValueError):
        validate_instance("status#/$defs/Conflict", {**CONFLICT, "claim_ids": ["c", "c"]})


@pytest.mark.parametrize("hash_function", [content_hash, closure_hash, contract_hash])
def test_hashes_ignore_clock_layout_and_key_order_but_bind_semantic_changes(hash_function):
    original = {"claims": [CLAIM], "source_hash": "a" * 64, "created_at": "today"}
    reordered = dict(reversed(list(original.items())))
    reordered.update(created_at="tomorrow", layout={"x": 12}, presentation="Markdown")
    assert hash_function(original) == hash_function(reordered)
    changed = deepcopy(original)
    changed["claims"][0]["truth"] = "false"
    assert hash_function(original) != hash_function(changed)
    assert hash_function(original) != hash_function({**original, "source_hash": "b" * 64})
    assert hash_function(original) != hash_function({**original, "revision": 1})


def test_hashes_preserve_evidence_digests_and_canonicalize_set_order():
    second = {**CLAIM, "id": "claim-2"}
    assert closure_hash({"claims": [CLAIM, second]}) == closure_hash({"claims": [second, CLAIM]})
    assert content_hash({"artifacts": [ARTIFACT]}) != content_hash(
        {"artifacts": [{**ARTIFACT, "content_hash": "b" * 64}]}
    )
    assert contract_hash(models.IntakeContract.model_validate(CONTRACT)) == contract_hash(CONTRACT)
    assert content_hash(snapshot()) == content_hash(snapshot().model_dump(exclude_unset=True))
    assert len({content_hash(CLAIM), closure_hash(CLAIM), contract_hash(CLAIM)}) == 3
    assert closure_hash(BINDING) != closure_hash({**BINDING, "contract_hash": "b" * 64})
    assert content_hash({"text": "intent"}) == content_hash({"text": "intent", "contentHash": "a" * 64})
    with pytest.raises(ValueError):
        content_hash({"value": float("inf")})


@pytest.mark.parametrize("truth", ["true", "false", "unknown"])
def test_unknown_semantics_distinguish_admitted_truth_proposals_and_absence(truth):
    claim = models.IntakeClaim.model_validate({
        **CLAIM, "truth": truth, "state": "admitted",
        "admitted_by": {"id": "owner-1", "kind": "owner"},
    })
    premises = {claim.id: claim}
    assert premise_truth(premises, claim.id) == truth
    assert premise_truth(premises, "missing") == "unknown"
    assert premise_truth({CLAIM["id"]: models.IntakeClaim.model_validate(CLAIM)}, CLAIM["id"]) == "unknown"
    validate_unknown_semantics(premises, {claim.id: truth, "missing": "unknown"})
    with pytest.raises(ValueError):
        validate_unknown_semantics(premises, {"missing": "false"})
    with pytest.raises(ValueError):
        validate_unknown_semantics(premises, {claim.id: False})


def test_unknown_modality_and_null_value_are_not_boolean_false():
    claim = models.IntakeClaim.model_validate({
        **CLAIM, "modality": "unknown", "truth": "unknown", "value": {"type": "null", "value": None},
    })
    assert claim.truth == "unknown"
    assert claim.value.value is None
    with pytest.raises(ValidationError):
        models.IntakeClaim.model_validate({key: value for key, value in CLAIM.items() if key != "truth"})


def test_owner_waiver_applies_only_to_exact_permitted_gap_revision():
    gap = models.Gap.model_validate({**GAP, "dimension_id": WAIVER["dimension_id"],
                                   "premise_id": WAIVER["premise_id"], "materiality": "material",
                                   "waiver_policy": "owner_with_reason"})
    waiver = models.WaiverDefinition.model_validate(WAIVER)
    validate_waiver(waiver, gap, run_id=waiver.run_id, revision=waiver.revision)
    for patch in ({"waiver_policy": "never"}, {"materiality": "critical", "waiver_policy": "never"},
                  {"premise_id": "another"}, {"dimension_id": "another"}, {"materiality": "minor"}):
        changed = models.Gap.model_validate({**gap.model_dump(), **patch})
        with pytest.raises(ValueError):
            validate_waiver(waiver, changed, run_id=waiver.run_id, revision=waiver.revision)
    for run_id, revision in (("other", waiver.revision), (waiver.run_id, waiver.revision + 1)):
        with pytest.raises(ValueError):
            validate_waiver(waiver, gap, run_id=run_id, revision=revision)
    for patch in ({"reason": "   "}, {"decision_id": ""}, {"waiver_policy": "never"}):
        changed = models.WaiverDefinition.model_validate({**WAIVER, **patch})
        with pytest.raises(ValueError):
            validate_waiver(changed, gap, run_id=waiver.run_id, revision=waiver.revision)
    with pytest.raises(ValidationError):
        models.WaiverDefinition.model_validate({**WAIVER, "granted_by": {"id": "model", "kind": "model"}})
