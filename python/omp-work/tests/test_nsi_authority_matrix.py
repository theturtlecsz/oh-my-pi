"""Markdown and other non-records cannot become NSI canonical state.

Wire dicts, exported models, and mixes of the two share one strict boundary.
"""

import json
from copy import deepcopy
from decimal import Decimal
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from omp_work.contracts.nsi.v1 import authority, models
from omp_work.contracts.nsi.v1.authority import *
from test_nsi_schemas import (
    ARTIFACT,
    ASSESSMENT,
    BINDING,
    CLAIM,
    CONFLICT,
    CONTRACT,
    CONTRACT_RECORDS,
    CRITERION,
    DECISION,
    DIMENSION,
    ENTITY,
    FORMALIZATION,
    GAP,
    MUTATION,
    PREDICATE,
    PUBLICATION,
    QUESTION,
    RATIFICATION,
    RATIFIED_BINDING,
    READINESS,
    RECEIPT,
    REQUIREMENT,
    RULE,
    SPAN,
    WAIVER,
)

TRUSTED = {"admitted": True}
MARKDOWN = "# Intake\nPublish"
PROSE = "Publish the intake as canonical state."

RECORDS = [
    RULE,
    MUTATION,
    WAIVER,
    ENTITY,
    PREDICATE,
    DIMENSION,
    CLAIM,
    ARTIFACT,
    SPAN,
    RECEIPT,
    GAP,
    CONFLICT,
    QUESTION,
    DECISION,
    REQUIREMENT,
    CRITERION,
    FORMALIZATION,
    BINDING,
    RATIFIED_BINDING,
    CONTRACT,
    ASSESSMENT,
    RATIFICATION,
    READINESS,
    PUBLICATION,
]
ID_LESS = [record for _name, record in CONTRACT_RECORDS if "id" not in record]
TYPED_RECORDS = [
    (models.AuthorityMatrixRule, RULE),
    (models.MutationEnvelope, MUTATION),
    (models.WaiverDefinition, WAIVER),
    (models.EntityRef, ENTITY),
    (models.PredicateDefinition, PREDICATE),
    (models.RequirementDimensionDefinition, DIMENSION),
    (models.IntakeClaim, CLAIM),
    (models.EvidenceArtifact, ARTIFACT),
    (models.EvidenceSpan, SPAN),
    (models.DerivationReceipt, RECEIPT),
    (models.Gap, GAP),
    (models.Conflict, CONFLICT),
    (models.ClarificationQuestion, QUESTION),
    (models.OwnerDecision, DECISION),
    (models.RequirementClause, REQUIREMENT),
    (models.AcceptanceCriterion, CRITERION),
    (models.Formalization, FORMALIZATION),
    (models.Binding, BINDING),
    (models.RatifiedFormalBinding, RATIFIED_BINDING),
    (models.IntakeContract, CONTRACT),
    (models.IntakeReadinessAssessment, ASSESSMENT),
    (models.RatificationReceipt, RATIFICATION),
    (models.IntakeReadinessReceipt, READINESS),
    (models.IntakePublicationReceipt, PUBLICATION),
]
DECLARED_TEXT = [
    (models.RequirementClause, {**REQUIREMENT, "text": MARKDOWN}),
    (models.IntakeClaim, {**CLAIM, "value": {"type": "string", "value": MARKDOWN}}),
    (models.IntakeClaim, {**CLAIM, "premise_ids": [MARKDOWN]}),
    (
        models.OwnerDecision,
        {**DECISION, "reason": PROSE, "answer": {"type": "string", "value": MARKDOWN}},
    ),
    (models.EvidenceSpan, {**SPAN, "quote": MARKDOWN}),
    (models.ClarificationQuestion, {**QUESTION, "question": MARKDOWN}),
    (models.Gap, {**GAP, "reason": MARKDOWN}),
    (models.WaiverDefinition, {**WAIVER, "reason": MARKDOWN}),
    (models.EvidenceArtifact, {**ARTIFACT, "media_type": "text/markdown"}),
    (models.TypedStringValue, {"type": "string", "value": MARKDOWN}),
    (models.Applicability, {"entity_types": [MARKDOWN], "predicate_ids": ["publish"]}),
]


def _ontology():
    return {"predicates": [PREDICATE], "dimensions": [DIMENSION]}


def _snapshot():
    return {
        "run_id": "run-1",
        "revision": 0,
        "state": "collecting",
        "artifacts": [ARTIFACT],
        "entities": [ENTITY],
        "claims": [CLAIM],
        "ontology": _ontology(),
    }


def _empty_snapshot():
    return {
        "run_id": "run-1",
        "revision": 0,
        "state": "collecting",
        "artifacts": [],
        "entities": [],
        "claims": [],
        "ontology": {"predicates": [], "dimensions": []},
    }


def _controls():
    return [
        _snapshot(),
        CONTRACT,
        _ontology(),
        CLAIM,
        [CLAIM, GAP, CONTRACT, RATIFICATION],
        [],
        {field: [] for field in CONTRACT},
        _empty_snapshot(),
    ]


def _admit(content, context=TRUSTED):
    return refuse_markdown_canonical_state(content, context=context)


def test_authority_module_and_package_export_the_gate():
    assert authority.__all__ == ["AuthorityRefusal", "refuse_markdown_canonical_state"]
    assert AuthorityRefusal is authority.AuthorityRefusal
    assert refuse_markdown_canonical_state is authority.refuse_markdown_canonical_state

    import omp_work.contracts.nsi.v1 as pkg

    exported = {name: getattr(pkg, name) for name in pkg.__all__}
    assert exported["authority"] is authority
    assert exported["AuthorityRefusal"] is AuthorityRefusal
    assert exported["refuse_markdown_canonical_state"] is refuse_markdown_canonical_state


@pytest.mark.parametrize("content", RECORDS)
def test_valid_wire_records_keep_identity(content):
    original = deepcopy(content)
    assert _admit(content) is content
    assert content == original


def test_id_less_receipts_keep_identity():
    assert {id(record) for record in ID_LESS} <= {id(record) for record in RECORDS}
    assert all("id" not in record for record in ID_LESS)
    for record in ID_LESS:
        assert _admit(record) is record


def test_valid_snapshot_contract_ontology_and_lists_keep_identity():
    snap = _snapshot()
    claim = snap["claims"][0]
    ontology = _ontology()
    predicate = ontology["predicates"][0]
    records = [CLAIM, CONTRACT, RATIFICATION, ontology]
    empty = []
    for content, nested in (
        (snap, claim),
        (CONTRACT, CONTRACT["requirements"][0]),
        (ontology, predicate),
        (records, records[0]),
        (empty, None),
        ({field: [] for field in CONTRACT}, None),
        (_empty_snapshot(), None),
    ):
        original = deepcopy(content)
        assert _admit(content) is content
        assert content == original
        if nested is not None:
            assert _admit(content) is content


def test_nested_wire_objects_keep_identity():
    snap = _snapshot()
    assert _admit(snap)["claims"][0] is snap["claims"][0]
    assert _admit(snap)["ontology"]["predicates"][0] is PREDICATE
    records = [CLAIM, GAP]
    assert _admit(records)[0] is CLAIM
    assert _admit(records)[1] is GAP


@pytest.mark.parametrize("content", RECORDS)
def test_omitted_none_and_empty_context_refuse_valid_content(content):
    with pytest.raises(AuthorityRefusal):
        refuse_markdown_canonical_state(content)
    with pytest.raises(AuthorityRefusal):
        refuse_markdown_canonical_state(content, context=None)
    with pytest.raises(AuthorityRefusal):
        refuse_markdown_canonical_state(content, context={})
    with pytest.raises(AuthorityRefusal):
        refuse_markdown_canonical_state(content, context=MappingProxyType({}))


def test_untrusted_context_refuses_snapshot_contract_and_lists():
    for content in _controls():
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content)
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content, context=None)
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content, context={})


@pytest.mark.parametrize("context", [[], ["trusted"], "", "trusted", 0, False, b"trusted", TRUSTED])
def test_only_a_nonempty_mapping_is_trusted(context):
    if isinstance(context, dict) and context:
        assert _admit(CLAIM, context) is CLAIM
    else:
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(CLAIM, context=context)


def test_mapping_proxy_is_trusted_without_actor_authorization():
    context = MappingProxyType({"actor": {"id": "model-1", "kind": "model"}, "operation": "publish"})
    assert _admit(CLAIM, context) is CLAIM
    assert _admit(PUBLICATION, RULE) is PUBLICATION
    with pytest.raises(AuthorityRefusal):
        _admit(MARKDOWN, context)


def test_context_is_keyword_only():
    with pytest.raises(TypeError):
        refuse_markdown_canonical_state(CLAIM, TRUSTED)


@pytest.mark.parametrize(
    "content",
    [
        MARKDOWN,
        PROSE,
        "",
        "   ",
        b"# Intake\nPublish",
        bytearray(b"# Intake"),
        json.dumps(CLAIM),
        json.dumps([CLAIM]),
        json.dumps(MARKDOWN),
        {"markdown": "# Intake"},
        {"claims": [MARKDOWN]},
        {"source": MARKDOWN},
        {"document": PROSE},
        {"source": MARKDOWN, "document": PROSE},
        [{"markdown": "# Intake"}],
        [CLAIM, MARKDOWN],
        [CLAIM, PROSE],
        [MARKDOWN, CLAIM],
        [{"markdown": "# Intake"}, CLAIM],
        [CLAIM, {"markdown": "# Intake"}],
    ],
)
def test_markdown_prose_bytes_json_and_wrappers_are_refused(content):
    with pytest.raises(AuthorityRefusal) as caught:
        _admit(content)
    assert not isinstance(caught.value, ValidationError)
    assert isinstance(caught.value, ValueError)


def test_prose_inside_a_snapshot_claim_collection_is_refused():
    snap = _snapshot()
    snap["claims"] = [MARKDOWN]
    with pytest.raises(AuthorityRefusal):
        _admit(snap)
    snap = _snapshot()
    snap["claims"] = [CLAIM, PROSE]
    with pytest.raises(AuthorityRefusal):
        _admit(snap)


@pytest.mark.parametrize(
    "content",
    [
        {**CLAIM, "id": "claim-1", "truth": True},
        {**CLAIM, "id": "claim-1", "value": {"type": "boolean", "value": "false"}},
        {**CLAIM, "id": "claim-1", "value": {"type": "integer", "value": True}},
        {**CLAIM, "id": "claim-1", "subject": {"id": "entity-1", "type": ""}},
        {**CLAIM, "id": "claim-1", "evidence": [{**SPAN, "start_byte": -1}]},
        {**CLAIM, "markdown": MARKDOWN},
        {**ARTIFACT, "id": "artifact-1", "content_hash": "A" * 64},
        {**ARTIFACT, "id": "artifact-1", "uri": "not a uri"},
        {**ENTITY, "id": "entity-1", "type": 1},
        {**CONFLICT, "id": "conflict-1", "claim_ids": ["claim-1"]},
        {**CONFLICT, "id": "conflict-1", "claim_ids": ["claim-1", 2]},
        {**RECEIPT, "id": "receipt-1", "revision": "0"},
        {**RECEIPT, "id": "receipt-1", "revision": -1},
        {**REQUIREMENT, "id": "requirement-1", "text": ""},
        {**REQUIREMENT, "id": "requirement-1", "claim_ids": [1]},
        {**BINDING, "revision": True},
        {**BINDING, "revision": 0.0},
        {**BINDING, "source_hash": "g" * 64},
        {**GAP, "id": "gap-1", "materiality": "critical", "waiver_policy": "owner_with_reason"},
        {**SPAN, "artifact_id": "artifact-1", "quote": ""},
        {**DECISION, "id": "decision-1", "answer": {"type": "string"}},
        {"id": "claim-1", "markdown": MARKDOWN},
    ],
)
def test_malformed_nested_fields_are_refused_despite_ids(content):
    with pytest.raises(AuthorityRefusal):
        _admit(content)


def test_malformed_nested_snapshot_and_list_are_refused_despite_ids():
    snap = _snapshot()
    snap["claims"] = [{**CLAIM, "id": "claim-1", "modality": "certain"}]
    with pytest.raises(AuthorityRefusal):
        _admit(snap)
    snap = _snapshot()
    snap["revision"] = -1
    with pytest.raises(AuthorityRefusal):
        _admit(snap)
    mixed = [CLAIM, {**GAP, "id": "gap-1", "reason": ""}]
    with pytest.raises(AuthorityRefusal):
        _admit(mixed)
    assert mixed[0] is CLAIM


def test_mixed_records_keep_identity_and_non_json_values_stay_uncoerced():
    typed_claim = models.IntakeClaim.model_validate(CLAIM)
    snap = _snapshot()
    snap["claims"] = [typed_claim]
    assert _admit(snap) is snap
    assert snap["claims"][0] is typed_claim

    decimal_claim = {**CLAIM, "value": {"type": "number", "value": Decimal("1.25")}}
    with pytest.raises(AuthorityRefusal):
        _admit(decimal_claim)
    snap = _snapshot()
    snap["claims"] = [decimal_claim]
    original = deepcopy(snap)
    with pytest.raises(AuthorityRefusal):
        _admit(snap)
    assert snap == original
    assert isinstance(snap["claims"][0]["value"]["value"], Decimal)

    number = {"type": "number", "value": Decimal("1.25")}
    with pytest.raises(AuthorityRefusal):
        _admit(number)
    assert isinstance(number["value"], Decimal)

    subject = models.EntityRef.model_validate(ENTITY)
    claim = {**CLAIM, "subject": subject}
    assert _admit(claim) is claim
    assert claim["subject"] is subject

    requirement = models.RequirementClause.model_validate(CONTRACT["requirements"][0])
    contract = {**CONTRACT, "requirements": [requirement]}
    assert _admit(contract) is contract
    assert contract["requirements"][0] is requirement

    predicate = models.PredicateDefinition.model_validate(PREDICATE)
    ontology = {"predicates": [predicate], "dimensions": [DIMENSION]}
    assert _admit(ontology) is ontology
    assert ontology["predicates"][0] is predicate


@pytest.mark.parametrize(
    "content",
    [
        {**REQUIREMENT, "text": MARKDOWN},
        {**CLAIM, "value": {"type": "string", "value": MARKDOWN}},
        {**CLAIM, "premise_ids": [MARKDOWN]},
        {
            **DECISION,
            "reason": PROSE,
            "answer": {"type": "string", "value": MARKDOWN},
        },
        {**SPAN, "quote": MARKDOWN},
        {**QUESTION, "question": MARKDOWN},
        {**GAP, "reason": MARKDOWN},
        {**WAIVER, "reason": MARKDOWN},
        {**ARTIFACT, "media_type": "text/markdown"},
        {"type": "string", "value": MARKDOWN},
        {"entity_types": [MARKDOWN], "predicate_ids": ["publish"]},
    ],
)
def test_declared_text_and_value_fields_remain_data(content):
    original = deepcopy(content)
    assert _admit(content) is content
    assert content == original


def test_refusal_does_not_mutate_controls_or_bypasses():
    for content in (CLAIM, CONTRACT, [CLAIM, GAP], _snapshot()):
        original = deepcopy(content)
        assert _admit(content) is content
        assert content == original
    bypass = [CLAIM, MARKDOWN]
    original = deepcopy(bypass)
    with pytest.raises(AuthorityRefusal):
        _admit(bypass)
    assert bypass == original
    wrapped = {"claims": [MARKDOWN]}
    with pytest.raises(AuthorityRefusal):
        _admit(wrapped)
    assert wrapped == {"claims": [MARKDOWN]}


def _unchanged(content):
    original = deepcopy(content)
    assert _admit(content) is content
    assert content == original
    return content


@pytest.mark.parametrize("model_type,content", TYPED_RECORDS)
def test_typed_records_keep_identity(model_type, content):
    _unchanged(model_type.model_validate(content))


def test_typed_snapshot_contract_ontology_and_mixed_lists_keep_identity():
    snap = models.IntakeSnapshot.model_validate(_snapshot())
    contract = models.IntakeContract.model_validate(CONTRACT)
    ontology = models.IntakeOntology.model_validate(_ontology())
    claim = snap.claims[0]
    claims = snap.claims
    snap_ontology = snap.ontology
    snap_predicates = snap_ontology.predicates
    snap_predicate = snap_predicates[0]
    predicate = ontology.predicates[0]
    requirement = contract.requirements[0]
    gap = models.Gap.model_validate(GAP)
    typed_list = [claim, gap, CONTRACT]
    mixed = [CLAIM, ontology]

    assert _unchanged(snap) is snap
    assert snap.claims is claims
    assert snap.claims[0] is claim
    assert snap.ontology is snap_ontology
    assert snap.ontology.predicates is snap_predicates
    assert snap.ontology.predicates[0] is snap_predicate
    assert _unchanged(contract) is contract
    assert contract.requirements[0] is requirement
    assert _unchanged(ontology) is ontology
    assert ontology.predicates[0] is predicate
    assert _unchanged(typed_list) is typed_list
    assert typed_list[0] is claim
    assert typed_list[1] is gap
    assert typed_list[2] is CONTRACT
    assert _unchanged(mixed) is mixed
    assert mixed[0] is CLAIM
    assert mixed[1] is ontology

    wire = claim.model_dump(exclude_unset=True)
    assert _admit(wire) is wire
    assert _admit(claim) is claim


@pytest.mark.parametrize("model_type,content", DECLARED_TEXT)
def test_typed_declared_text_and_value_fields_remain_data(model_type, content):
    model = model_type.model_validate(content)
    assert _admit(model) is model
    assert model.model_dump(exclude_unset=True) == content


def test_typed_id_less_assessment_and_receipts_keep_identity():
    typed = {id(record): model_type for model_type, record in TYPED_RECORDS}
    seen = set()
    for record in ID_LESS:
        model = _unchanged(typed[id(record)].model_validate(record))
        assert "id" not in type(model).model_fields
        seen.add(type(model))
    assert models.IntakeReadinessAssessment in seen
    assert models.RatificationReceipt in seen
    assert models.IntakeReadinessReceipt in seen
    assert models.IntakePublicationReceipt in seen


def test_omitted_none_and_empty_context_refuse_typed_controls():
    controls = [
        models.IntakeSnapshot.model_validate(_snapshot()),
        models.IntakeContract.model_validate(CONTRACT),
        models.IntakeOntology.model_validate(_ontology()),
        models.IntakeReadinessAssessment.model_validate(ASSESSMENT),
        models.RatificationReceipt.model_validate(RATIFICATION),
        models.IntakeReadinessReceipt.model_validate(READINESS),
        models.IntakeClaim.model_validate(CLAIM),
        models.RequirementClause.model_validate({**REQUIREMENT, "text": MARKDOWN}),
    ]
    for content in controls:
        original = deepcopy(content)
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content)
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content, context=None)
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content, context={})
        with pytest.raises(AuthorityRefusal):
            refuse_markdown_canonical_state(content, context=MappingProxyType({}))
        assert content == original


def test_appended_markdown_in_snapshot_ontology_is_refused():
    ontology = models.IntakeOntology(predicates=[], dimensions=[])
    snap = _empty_snapshot()
    snap["ontology"] = ontology
    assert _admit(snap) is snap
    assert snap["ontology"] is ontology
    assert ontology.predicates == []

    ontology.predicates.append(MARKDOWN)
    assert models.IntakeOntology.model_validate(ontology) is ontology
    with pytest.raises(AuthorityRefusal):
        _admit(snap)
    assert snap["ontology"] is ontology
    assert ontology.predicates == [MARKDOWN]


def test_model_copy_markdown_inside_typed_snapshot_is_refused():
    snap = models.IntakeSnapshot.model_validate(_snapshot())
    assert _admit(snap) is snap
    bad_claim = snap.claims[0].model_copy(update={"markdown": MARKDOWN})
    bad_snap = snap.model_copy(update={"claims": [bad_claim]})
    assert "markdown" not in bad_claim.model_dump()
    assert models.IntakeSnapshot.model_validate(bad_snap) is bad_snap
    with pytest.raises(AuthorityRefusal):
        _admit(bad_snap)
    assert bad_snap.claims[0] is bad_claim
    assert bad_claim.__dict__["markdown"] == MARKDOWN
    assert "markdown" not in snap.claims[0].__dict__


def test_unknown_root_fields_on_typed_records_are_refused():
    claim = models.IntakeClaim.model_validate(CLAIM).model_copy(update={"markdown": MARKDOWN})
    snap = models.IntakeSnapshot.model_validate(_snapshot()).model_copy(update={"markdown": MARKDOWN})
    for model in (claim, snap):
        assert "markdown" not in model.model_dump()
        with pytest.raises(AuthorityRefusal):
            _admit(model)
        assert model.__dict__["markdown"] == MARKDOWN


def test_model_construct_and_model_copy_malformed_fields_are_refused():
    raw = _empty_snapshot()
    raw["revision"] = -1
    constructed = models.IntakeSnapshot.model_construct(**raw)
    copied = models.IntakeClaim.model_validate(CLAIM).model_copy(update={"modality": "certain"})
    explicit_null = models.IntakeClaim.model_validate(CLAIM).model_copy(update={"admitted_by": None})
    span = models.EvidenceSpan.model_validate(SPAN)
    span.__dict__["quote"] = ""
    premises = models.IntakeClaim.model_construct(**{**CLAIM, "premise_ids": (MARKDOWN,)})
    value = models.IntakeClaim.model_validate(CLAIM).model_copy(
        update={"value": models.TypedIntegerValue.model_construct(type="integer", value=True)}
    )
    bad_gap = models.Gap.model_construct(**{**GAP, "reason": ""})
    mixed = [CLAIM, bad_gap]

    assert constructed.revision == -1
    assert "quote" not in span.model_dump(exclude_unset=True)
    assert value.value.value is True

    for content in (constructed, copied, explicit_null, span, premises, value, mixed):
        with pytest.raises(AuthorityRefusal):
            _admit(content)

    assert constructed.revision == -1
    assert constructed.ontology == {"predicates": [], "dimensions": []}
    assert copied.modality == "certain"
    assert explicit_null.admitted_by is None
    assert span.quote == ""
    assert premises.premise_ids == (MARKDOWN,)
    assert value.value.value is True
    assert mixed[0] is CLAIM
    assert mixed[1] is bad_gap
    assert bad_gap.reason == ""


def test_nested_unknown_fields_in_mixed_lists_are_refused():
    predicate = models.PredicateDefinition.model_validate(PREDICATE).model_copy(
        update={"markdown": MARKDOWN}
    )
    ontology = models.IntakeOntology(predicates=[], dimensions=[])
    ontology.predicates.append(predicate)
    span = models.EvidenceSpan.model_validate(SPAN).model_copy(update={"note": MARKDOWN})
    claim = {**CLAIM, "evidence": [span]}
    gap = models.Gap.model_validate(GAP)
    lists = (
        [CLAIM, ontology],
        [gap, claim],
        [ontology, _ontology(), claim],
    )
    for mixed in lists:
        with pytest.raises(AuthorityRefusal):
            _admit(mixed)
    assert ontology.predicates[0] is predicate
    assert predicate.__dict__["markdown"] == MARKDOWN
    assert "markdown" not in predicate.model_dump()
    assert claim["evidence"][0] is span
    assert span.__dict__["note"] == MARKDOWN
    assert lists[1][0] is gap


def test_pydantic_extras_and_private_storage_are_refused():
    extra_claim = models.IntakeClaim.model_validate(CLAIM)
    object.__setattr__(extra_claim, "__pydantic_extra__", {"markdown": MARKDOWN})
    private_claim = models.IntakeClaim.model_validate(CLAIM)
    object.__setattr__(private_claim, "__pydantic_private__", {"markdown": MARKDOWN})
    nested = models.IntakeClaim.model_validate(CLAIM)
    object.__setattr__(nested, "__pydantic_extra__", {"markdown": MARKDOWN})
    mixed = [GAP, nested]
    for content in (extra_claim, private_claim, mixed):
        with pytest.raises(AuthorityRefusal):
            _admit(content)
    assert extra_claim.__pydantic_extra__["markdown"] == MARKDOWN
    assert private_claim.__pydantic_private__["markdown"] == MARKDOWN
    assert mixed[0] is GAP
    assert mixed[1] is nested
    assert nested.__pydantic_extra__["markdown"] == MARKDOWN
    assert "markdown" not in extra_claim.model_dump()
    assert "markdown" not in private_claim.model_dump()


def test_subclass_of_an_exported_record_is_refused():
    class ClaimAlias(models.IntakeClaim):
        pass

    alias = ClaimAlias.model_validate(deepcopy(CLAIM))
    with pytest.raises(AuthorityRefusal):
        _admit(alias)
    assert alias.id == CLAIM["id"]
    assert type(alias) is ClaimAlias


@pytest.mark.parametrize(
    "model_type,wire,wire_model_type",
    [
        (models.OwnerRef, {"id": "model-1", "kind": "model"}, models.ActorRef),
        (models.RatificationReceipt, BINDING, models.Binding),
        (models.TypedIntegerValue, {"type": "string", "value": MARKDOWN}, models.TypedStringValue),
    ],
)
@pytest.mark.parametrize("location", ["root", "mixed_list"])
def test_malformed_concrete_models_cannot_pass_as_other_records(model_type, wire, wire_model_type, location):
    # These bytes are valid for a different record, but not for the stored class.
    _unchanged(wire)
    _unchanged(wire_model_type.model_validate(wire))
    bad = model_type.model_construct(**deepcopy(wire))
    if location == "root":
        content = bad
    else:
        content = [CLAIM, bad]
    original = deepcopy(content)
    with pytest.raises(AuthorityRefusal):
        _admit(content)
    assert content == original
    assert bad.__dict__ == wire
    if location == "mixed_list":
        assert content[1] is bad


@pytest.mark.parametrize(
    "parent_type,wire,field,bad_type,bad_wire",
    [
        (models.EvidenceArtifact, ARTIFACT, "actor", models.OwnerRef, {"id": "model-1", "kind": "model"}),
        (models.IntakeClaim, CLAIM, "value", models.TypedIntegerValue, {"type": "string", "value": MARKDOWN}),
    ],
)
@pytest.mark.parametrize("location", ["wire_parent", "typed_parent", "mixed_list"])
def test_parent_cannot_reinterpret_malformed_nested_model(parent_type, wire, field, bad_type, bad_wire, location):
    _unchanged({**wire, field: bad_wire})
    bad = bad_type.model_construct(**bad_wire)
    if location == "typed_parent":
        parent = parent_type.model_validate(wire).model_copy(update={field: bad})
    else:
        parent = {**wire, field: bad}
    content = [GAP, parent] if location == "mixed_list" else parent
    original = deepcopy(content)
    with pytest.raises(AuthorityRefusal):
        _admit(content)
    assert content == original
    if location == "typed_parent":
        assert getattr(parent, field) is bad
    else:
        assert parent[field] is bad
    if location == "mixed_list":
        assert content[1] is parent
    assert bad.__dict__ == bad_wire
