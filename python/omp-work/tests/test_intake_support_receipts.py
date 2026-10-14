from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omp_work.contracts.nsi.v1.models import (
    ActorRef,
    Binding,
    EntityRef,
    IntakeClaim,
    IntakeOntology,
    IntakeSnapshot,
    OwnerRef,
    TypedNullValue,
)
from omp_work.intake_repository import EvidenceLinkRecord, IntakeRevisionBody
from omp_work.intake_support import (
    ClaimProjection,
    SupportReceipt,
    claim_supported,
    render_claim_text,
    status_from_receipts,
    verify_receipt,
)
from omp_work.report_evidence import ClaimStatus, Label
from omp_work.v1.canonical import text_sha256

H = "a" * 64


def _receipt(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "verdict": "supports",
        "source_span": {"start": 0, "end": 5},
        "source_revision": "rev-1",
        "model_version": "2026-01",
        "prompt_version": "p1",
        "rule_version": "r1",
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


@pytest.mark.parametrize("verdict", ["abstain", "timeout", "evidence-unavailable"])
def test_pending_verdicts_stored_and_block_support(verdict: str) -> None:
    pending = _parse(verdict=verdict, measurement="unknown", input_tokens=None, output_tokens=None)
    assert pending.verdict == verdict
    assert claim_supported([_parse()]) is True
    assert claim_supported([_parse(), pending]) is False
    assert claim_supported([pending]) is False


def test_no_supports_verdict_is_not_supported() -> None:
    assert claim_supported([]) is False
    assert claim_supported([_parse(verdict="neither")]) is False


@pytest.mark.parametrize("field", ["source_revision", "model_version", "prompt_version", "rule_version"])
def test_binding_fields_required_nonempty(field: str) -> None:
    with pytest.raises(ValidationError):
        _parse(**{field: ""})
    base = _receipt()
    del base[field]
    with pytest.raises(ValidationError):
        SupportReceipt.model_validate_json(json.dumps(base))


@pytest.mark.parametrize(
    "span", [{"start": -1, "end": 2}, {"start": 3, "end": 2}, {"start": True, "end": 2}, {"start": 0}]
)
def test_bad_source_span_rejected(span: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _parse(source_span=span)


def _claim() -> ClaimProjection:
    return {
        "subject_id": "café",
        "predicate_id": "p1",
        "predicate_title": "位置 🧭",
        "value": {"type": "null", "value": None},
        "modality": "observed",
        "polarity": "positive",
    }


def _receipt_kwargs(receipt: SupportReceipt, claim_text: str, quote: str) -> dict[str, object]:
    return {
        "claim_text": claim_text,
        "quote": quote,
        "model_id": receipt.model_id,
        "model_version": receipt.model_version,
        "prompt_version": receipt.prompt_version,
        "rule_version": receipt.rule_version,
        "source_span": receipt.source_span,
        "source_revision": receipt.source_revision,
    }


def test_claim_supported_mix_supports_and_contradicts_is_contested_not_supported() -> None:
    supports = _parse(verdict="supports")
    contradicts = _parse(verdict="contradicts")
    # Supports plus contradicts is contested, so claim_supported must be False.
    assert claim_supported([supports, contradicts]) is False
    assert claim_supported([("supports", supports), ("contradicts", contradicts)]) is False


def test_claim_supported_caller_label_disagreement_blocks_support() -> None:
    supports = _parse(verdict="supports")
    # Disagreement between caller label and judge verdict blocks support
    assert claim_supported([("neither", supports)]) is False
    assert claim_supported([("contradicts", supports)]) is False
    assert claim_supported([("supports", supports)]) is True
    assert claim_supported([("supports", None)]) is False


@pytest.mark.parametrize(
    "value, rendered_value",
    [
        ({"type": "null", "value": None}, '{"type":"null","value":null}'),
        (
            {"value": {"type": "place", "id": "東京"}, "type": "entity"},
            '{"type":"entity","value":{"id":"東京","type":"place"}}',
        ),
    ],
)
def test_claim_rendering_is_canonical_and_hashes_unicode_bytes(
    value: dict[str, object], rendered_value: str,
) -> None:
    claim = {**_claim(), "value": value, "incidental_metadata": "ignored"}
    expected = (
        '{"modality":"observed","polarity":"positive","predicate_id":"p1",'
        '"predicate_title":"位置 🧭","subject_id":"café","value":' + rendered_value + '}'
    )
    rendered = render_claim_text(claim)
    assert rendered == expected
    # Reversing all mapping keys must leave the judge's text/hash unchanged.
    claim["value"] = dict(reversed(list(value.items())))
    assert render_claim_text(dict(reversed(list(claim.items())))) == expected
    assert text_sha256(rendered) == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    receipt = _parse(
        claim_text_sha256=text_sha256(rendered),
        passage_sha256=text_sha256("証拠 café 🧭"),
    )
    assert verify_receipt(receipt, **_receipt_kwargs(receipt, rendered, "証拠 café 🧭"))


@pytest.mark.parametrize(
    "field, changed",
    [
        ("subject_id", "subject-2"),
        ("predicate_id", "predicate-2"),
        ("predicate_title", "different title"),
        ("value", {"type": "entity", "value": {"id": "東京", "type": "place"}}),
        ("modality", "required"),
        ("polarity", "negative"),
    ],
)
def test_each_claim_semantic_change_invalidates_receipt(field: str, changed: object) -> None:
    claim = _claim()
    text = render_claim_text(claim)
    receipt = _parse(claim_text_sha256=text_sha256(text), passage_sha256=text_sha256("quote"))
    changed_claim = {**claim, field: changed}
    kwargs = _receipt_kwargs(receipt, render_claim_text(changed_claim), "quote")
    assert not verify_receipt(receipt, **kwargs)


def test_render_requires_caller_predicate_title() -> None:
    claim = dict(_claim())
    del claim["predicate_title"]
    with pytest.raises(KeyError, match="predicate_title"):
        render_claim_text(claim)


@pytest.mark.parametrize(
    "field, tampered",
    [
        ("claim_text_sha256", "b" * 64),
        ("passage_sha256", "b" * 64),
        ("source_span", {"start": 1, "end": 5}),
        ("source_revision", "rev-2"),
        ("model_id", "judge-2"),
        ("model_version", "2026-02"),
        ("prompt_version", "p2"),
        ("rule_version", "r2"),
    ],
)
def test_tampered_receipt_binding_fails_verification(field: str, tampered: object) -> None:
    claim_text = "claim café"
    quote = "quote 東京"
    base_receipt = _parse(
        claim_text_sha256=text_sha256(claim_text),
        passage_sha256=text_sha256(quote),
    )
    over = {
        "claim_text_sha256": text_sha256(claim_text),
        "passage_sha256": text_sha256(quote),
    }
    over[field] = tampered
    tampered_receipt = _parse(**over)
    expected_kwargs = _receipt_kwargs(base_receipt, claim_text, quote)
    assert verify_receipt(base_receipt, **expected_kwargs)
    assert not verify_receipt(tampered_receipt, **expected_kwargs)


@pytest.mark.parametrize(
    "kwarg, changed_val",
    [
        ("claim_text", "claim cafe\u0301"),
        ("quote", "quote 東京\n"),
        ("source_span", {"start": 0, "end": 4}),
        ("source_revision", "rev-other"),
        ("model_id", "Judge-1"),
        ("model_version", "2026-02"),
        ("prompt_version", "p_other"),
        ("rule_version", "r_other"),
    ],
)
def test_changed_expected_binding_fails_verification(kwarg: str, changed_val: object) -> None:
    claim_text = "claim café"
    quote = "quote 東京"
    receipt = _parse(
        claim_text_sha256=text_sha256(claim_text),
        passage_sha256=text_sha256(quote),
    )
    kwargs = _receipt_kwargs(receipt, claim_text, quote)
    assert verify_receipt(receipt, **kwargs)
    kwargs[kwarg] = changed_val
    assert not verify_receipt(receipt, **kwargs)


def test_verify_receipt_accepts_span_formats_and_positional_calls() -> None:
    claim_text = "claim café"
    quote = "quote 東京"
    receipt = _parse(
        claim_text_sha256=text_sha256(claim_text),
        passage_sha256=text_sha256(quote),
    )
    # SourceSpan instance
    assert verify_receipt(
        receipt,
        claim_text,
        quote,
        "judge-1",
        "2026-01",
        "p1",
        "r1",
        receipt.source_span,
        "rev-1",
    )
    # tuple span
    assert verify_receipt(
        receipt,
        claim_text,
        quote,
        "judge-1",
        "2026-01",
        "p1",
        "r1",
        (0, 5),
        "rev-1",
    )
    # dict span
    assert verify_receipt(
        receipt,
        claim_text,
        quote,
        "judge-1",
        "2026-01",
        "p1",
        "r1",
        {"start": 0, "end": 5},
        "rev-1",
    )
    # positional call with criterion 4 ordering
    assert verify_receipt(
        receipt,
        claim_text,
        quote,
        (0, 5),
        "rev-1",
        "judge-1",
        "2026-01",
        "p1",
        "r1",
    )


@pytest.mark.parametrize(
    "pairs, expected",
    [
        ([], "unsupported"),
        ([("supports", None), ("contradicts", None)], "unsupported"),
        ([("supports", "supports")], "supported"),
        ([("neither", "supports")], "unsupported"),
        ([("supports", "neither")], "unsupported"),
        ([("supports", "supports"), ("contradicts", "contradicts")], "contested"),
        ([("neither", "supports"), ("supports", "contradicts")], "contested"),
        ([(None, "supports")], "supported"),
        ([("supports", "supports"), ("supports", None)], "unsupported"),
        ([("contradicts", "contradicts"), ("supports", None)], "contested"),
    ],
)
def test_status_derivation(
    pairs: list[tuple[Label | None, str | None]], expected: ClaimStatus,
) -> None:
    receipts = [(relation, _parse(verdict=verdict) if verdict is not None else None) for relation, verdict in pairs]
    assert status_from_receipts(receipts) == expected


@pytest.mark.parametrize("verdict", ["abstain", "timeout", "evidence-unavailable"])
def test_pending_receipt_blocks_support_but_preserves_contradiction(verdict: str) -> None:
    pending = _parse(verdict=verdict, measurement="unknown", input_tokens=None, output_tokens=None)
    assert status_from_receipts([(None, pending)]) == "unsupported"
    assert status_from_receipts([("supports", _parse()), (None, pending)]) == "unsupported"
    assert status_from_receipts([
        ("supports", _parse()), (None, pending), ("contradicts", _parse(verdict="contradicts")),
    ]) == "contested"


# --- storage envelope: EvidenceLinkRecord.support_receipt ---


def _typed_null_claim() -> IntakeClaim:
    return IntakeClaim(
        id="claim-1",
        category="non_functional_qualities",
        subject=EntityRef(id="entity-1", type="component"),
        predicate_id="uptime_target",
        value=TypedNullValue(type="null", value=None),
        origin="owner_statement",
        state="admitted",
        modality="desired",
        polarity="positive",
        truth="true",
        asserted_by=ActorRef(id="owner-1", kind="owner"),
        admitted_by=OwnerRef(id="owner-1", kind="owner"),
        evidence=[],
        premise_ids=[],
    )


def _link(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "claim_id": "c1", "artifact_id": "a1", "start_byte": 0, "end_byte": 5,
        "quote": None, "relation": "neither",
    }
    base.update(over)
    return base


def _body(links: list[EvidenceLinkRecord], claims: list[IntakeClaim] | None = None) -> IntakeRevisionBody:
    if claims is None:
        claims = [_typed_null_claim()]
    return IntakeRevisionBody(
        workspace_id=uuid4(),
        snapshot=IntakeSnapshot(
            run_id="r", revision=0, state="collecting", artifacts=[], entities=[], claims=claims,
            ontology=IntakeOntology(predicates=[], dimensions=[]),
        ),
        binding=Binding(
            run_id="r", revision=0, contract_hash=H, claim_set_hash=H,
            rule_bundle_hash=H, schema_hash=H, source_hash=H,
        ),
        artifact_bytes={}, evidence_links=links,
    )


@pytest.mark.parametrize("explicit_none", [False, True])
def test_receipt_free_link_dump_keeps_previous_shape(explicit_none: bool) -> None:
    extra = {"support_receipt": None} if explicit_none else {}
    link = EvidenceLinkRecord.model_validate(_link(**extra))
    expected = _link()
    assert link.model_dump() == expected
    assert link.model_dump()["quote"] is None
    assert "support_receipt" not in link.model_dump()

    assert json.loads(link.model_dump_json()) == expected
    assert json.loads(link.model_dump_json())["quote"] is None
    assert "support_receipt" not in json.loads(link.model_dump_json())

    unset_dump = link.model_dump(mode="json", exclude_unset=True)
    assert unset_dump["quote"] is None
    assert "support_receipt" not in unset_dump

    unset_json = json.loads(link.model_dump_json(exclude_unset=True))
    assert unset_json["quote"] is None
    assert "support_receipt" not in unset_json

    body = _body([link])
    dumped_body = body.model_dump(mode="json")
    assert dumped_body["evidence_links"][0] == expected
    assert dumped_body["evidence_links"][0]["quote"] is None
    assert "support_receipt" not in dumped_body["evidence_links"][0]
    assert dumped_body["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}

    json_body = json.loads(body.model_dump_json())
    assert json_body["evidence_links"][0] == expected
    assert json_body["evidence_links"][0]["quote"] is None
    assert "support_receipt" not in json_body["evidence_links"][0]
    assert json_body["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}

    unset_body = body.model_dump(mode="json", exclude_unset=True)
    assert unset_body["evidence_links"][0]["quote"] is None
    assert "support_receipt" not in unset_body["evidence_links"][0]
    assert unset_body["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}

    unset_json_body = json.loads(body.model_dump_json(exclude_unset=True))
    assert unset_json_body["evidence_links"][0]["quote"] is None
    assert "support_receipt" not in unset_json_body["evidence_links"][0]
    assert unset_json_body["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}


def test_populated_receipt_round_trips_with_unknown_nulls() -> None:
    receipt = _receipt(measurement="unknown", input_tokens=None, output_tokens=None, usage_id=None)
    link = EvidenceLinkRecord.model_validate_json(
        json.dumps(_link(relation="supports", quote="hello", support_receipt=receipt))
    )
    dumped = json.loads(link.model_dump_json())
    assert dumped["support_receipt"] == receipt
    assert dumped["support_receipt"]["input_tokens"] is None
    assert dumped["support_receipt"]["output_tokens"] is None
    assert dumped["support_receipt"]["usage_id"] is None
    assert EvidenceLinkRecord.model_validate_json(json.dumps(dumped)) == link

    dumped_py = link.model_dump(mode="json")
    assert dumped_py["support_receipt"] == receipt
    assert dumped_py["support_receipt"]["input_tokens"] is None
    assert dumped_py["support_receipt"]["output_tokens"] is None
    assert dumped_py["support_receipt"]["usage_id"] is None

    dumped_unset = link.model_dump(mode="json", exclude_unset=True)
    assert dumped_unset["support_receipt"] == receipt
    assert dumped_unset["support_receipt"]["input_tokens"] is None
    assert dumped_unset["support_receipt"]["output_tokens"] is None
    assert dumped_unset["support_receipt"]["usage_id"] is None

    body = _body([link])
    nested = json.loads(body.model_dump_json())["evidence_links"][0]
    assert nested["support_receipt"] == receipt
    assert nested["support_receipt"]["input_tokens"] is None
    assert nested["support_receipt"]["output_tokens"] is None
    assert nested["support_receipt"]["usage_id"] is None

    nested_py = body.model_dump(mode="json")["evidence_links"][0]
    assert nested_py["support_receipt"] == receipt
    assert nested_py["support_receipt"]["input_tokens"] is None
    assert nested_py["support_receipt"]["output_tokens"] is None
    assert nested_py["support_receipt"]["usage_id"] is None

    nested_unset = body.model_dump(mode="json", exclude_unset=True)["evidence_links"][0]
    assert nested_unset["support_receipt"] == receipt
    assert nested_unset["support_receipt"]["input_tokens"] is None
    assert nested_unset["support_receipt"]["output_tokens"] is None
    assert nested_unset["support_receipt"]["usage_id"] is None

    nested_unset_json = json.loads(body.model_dump_json(exclude_unset=True))["evidence_links"][0]
    assert nested_unset_json["support_receipt"] == receipt
    assert nested_unset_json["support_receipt"]["input_tokens"] is None
    assert nested_unset_json["support_receipt"]["output_tokens"] is None
    assert nested_unset_json["support_receipt"]["usage_id"] is None

    # Verify claim typed-null value remains preserved alongside populated receipt
    assert body.model_dump(mode="json")["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}
    assert body.model_dump(mode="json", exclude_unset=True)["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}
    assert json.loads(body.model_dump_json())["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}
    assert json.loads(body.model_dump_json(exclude_unset=True))["snapshot"]["claims"][0]["value"] == {"type": "null", "value": None}

    assert IntakeRevisionBody.model_validate_json(body.model_dump_json()) == body


@pytest.mark.parametrize("bad", [
    {"verdict": "maybe"},
    {"passage_sha256": "A" * 64},
    {"measurement": "measured", "input_tokens": None},
    {"measurement": "unknown"},
    {"source_span": {"start": 5, "end": 1}},
])
def test_link_rejects_malformed_receipt(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceLinkRecord.model_validate_json(
            json.dumps(_link(relation="supports", support_receipt=_receipt(**bad)))
        )
    with pytest.raises(ValidationError):
        EvidenceLinkRecord.model_validate(
            _link(relation="supports", support_receipt=_receipt(**bad))
        )
