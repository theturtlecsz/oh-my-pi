"""Markdown and other non-records cannot become NSI canonical state."""

import json
from copy import deepcopy
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


def test_typed_models_are_refused_until_the_next_slice():
    claim = models.IntakeClaim.model_validate(CLAIM)
    snap = models.IntakeSnapshot.model_validate(_snapshot())
    contract = models.IntakeContract.model_validate(CONTRACT)
    ontology = models.IntakeOntology.model_validate(_ontology())
    for typed in (claim, snap, contract, ontology, [claim], [models.Gap.model_validate(GAP)]):
        with pytest.raises(AuthorityRefusal):
            _admit(typed)
    wire = claim.model_dump(exclude_unset=True)
    assert _admit(wire) is wire


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
