from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
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
from omp_work.intake_repository import (
    EvidenceLinkRecord,
    IntakeRevisionBody,
    append_revision,
    read_revision,
    revision_hashes,
)
from test_nsi2_repository import connection, locked, revision_body, write_repository
from test_workflow_service import service  # noqa: F401
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


# --- database integration: migration 0042 column metadata & legacy NULL ---


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_support_receipt_column_metadata(service) -> None:
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_type, udt_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'omp_work'
                  AND table_name = 'intake_claim_evidence'
                  AND column_name = 'support_receipt'
                """
            )
            row = cur.fetchone()
            assert row is not None
            data_type, udt_name, is_nullable, column_default = row
            assert data_type == "jsonb"
            assert udt_name == "jsonb"
            assert is_nullable == "YES"
            assert column_default is None


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_legacy_insert_leaves_receipt_sql_null(service, write_repository) -> None:
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        head = locked(cur, write_repository)
        revision = append_revision(cur, head, body, operation_id=uuid4())

        # Direct SQL query verifies support_receipt column value is SQL NULL
        cur.execute(
            """
            SELECT claim_id, artifact_id, start_byte, end_byte, support_receipt,
                   support_receipt IS NULL
            FROM omp_work.intake_claim_evidence
            WHERE workspace_id = %s AND run_id = %s AND revision = %s
            """,
            (workspace_id, "run-1", revision),
        )
        rows = cur.fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row[4] is None
            assert row[5] is True

        # Raw legacy insert without specifying support_receipt
        artifact_id = body["snapshot"]["artifacts"][0]["id"]
        cur.execute(
            """
            INSERT INTO omp_work.intake_claim_evidence
              (workspace_id, run_id, revision, claim_id, artifact_id, start_byte, end_byte, quote, relation, operation_id, nsi_version)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (workspace_id, "run-1", revision, "prior", artifact_id, 20, 25, "quote", "neither", uuid4(), "nsi/v1"),
        )
        cur.execute(
            """
            SELECT support_receipt, support_receipt IS NULL
            FROM omp_work.intake_claim_evidence
            WHERE workspace_id = %s AND run_id = %s AND revision = %s AND claim_id = 'prior' AND start_byte = 20
            """,
            (workspace_id, "run-1", revision),
        )
        legacy_row = cur.fetchone()
        assert legacy_row is not None
        assert legacy_row[0] is None
        assert legacy_row[1] is True

        # Read revision reconstructs evidence links with support_receipt=None
        view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=revision)
        assert len(view.body.evidence_links) > 0
        for link in view.body.evidence_links:
            assert link.support_receipt is None
            assert "support_receipt" not in link.model_dump()


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_append_read_preserves_receipts_by_claim_artifact_and_exact_span(service, write_repository) -> None:
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    body["assessments"] = []
    artifacts = body["snapshot"]["artifacts"]
    artifacts.append({**artifacts[0], "id": "other-source", "uri": "https://example.com/requirements-2"})
    body["artifact_bytes"]["other-source"] = body["artifact_bytes"][artifacts[0]["id"]]
    claim, prior = body["snapshot"]["claims"]
    claim["value"] = {"type": "null", "value": None}
    claim["evidence"] = []
    prior["evidence"] = []
    cases = [
        (claim, artifacts[0], 0, 5, "supports", "neither"),
        (claim, artifacts[0], 0, 6, "contradicts", "supports"),
        (claim, artifacts[0], 1, 5, "neither", "contradicts"),
        (claim, artifacts[1], 0, 5, "abstain", "supports"),
        (prior, artifacts[0], 0, 5, "timeout", "supports"),
        (prior, artifacts[1], 0, 5, "evidence-unavailable", "supports"),
    ]
    links = []
    for index, (owner, artifact, start, end, verdict, relation) in enumerate(cases):
        span = dict(artifact_id=artifact["id"], start_byte=start, end_byte=end,
                    quote="hello" if index % 2 == 0 else None)
        owner["evidence"].append({name: value for name, value in span.items() if value is not None})
        receipt = _receipt(verdict=verdict, source_span=dict(start=start, end=end))
        if index % 2:
            receipt.update(measurement="unknown", input_tokens=None, output_tokens=None, usage_id=None)
        links.append(dict(claim_id=owner["id"], **span, relation=relation, support_receipt=receipt))
    legacy_span = dict(artifact_id=artifacts[0]["id"], start_byte=5, end_byte=6, quote=None)
    unmatched_span = dict(artifact_id=artifacts[0]["id"], start_byte=6, end_byte=7, quote=None)
    claim["evidence"].extend([{name: value for name, value in span.items() if value is not None}
                              for span in (legacy_span, unmatched_span)])
    links.append(dict(claim_id=claim["id"], **legacy_span, relation="contradicts", support_receipt=None))
    # A caller link for a different span must neither leak into an unmatched span nor create evidence.
    body["evidence_links"] = links + [dict(
        claim_id=claim["id"], artifact_id=artifacts[0]["id"], start_byte=7, end_byte=8,
        relation="supports", support_receipt=_receipt(source_span=dict(start=7, end=8)),
    )]
    for owner in (claim, prior):
        owner["evidence"].sort(key=lambda span: (span["artifact_id"], span["start_byte"], span["end_byte"]))
    original = IntakeRevisionBody.model_validate(body)
    hashes = revision_hashes(original.snapshot, original.artifact_bytes)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        revision = append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
    # Context exit commits; a fresh readonly connection proves persistence, not an in-memory echo.
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=revision)
        expected = [EvidenceLinkRecord.model_validate(link).model_dump(mode="json") for link in links]
        expected.append(dict(claim_id=claim["id"], **unmatched_span, relation="neither"))
        key = lambda link: (link["claim_id"], link["artifact_id"], link["start_byte"], link["end_byte"])
        assert sorted(view.body.model_dump(mode="json", include={"evidence_links"})["evidence_links"], key=key) == sorted(expected, key=key)
        assert {link.support_receipt.verdict for link in view.body.evidence_links
                if link.support_receipt is not None} == {case[4] for case in cases}
        assert {item.id: item for item in view.snapshot.artifacts} == {
            item.id: item for item in original.snapshot.artifacts
        }
        assert {item.id: item for item in view.snapshot.claims} == {item.id: item for item in original.snapshot.claims}
        assert view.artifact_bytes == original.artifact_bytes
        assert view.body.binding == original.binding
        assert (view.claim_set_hash, view.source_hash) == hashes
        restored_claim = next(item for item in view.snapshot.claims if item.id == claim["id"])
        assert restored_claim.model_dump()["value"] == {"type": "null", "value": None}
        assert view.body.criteria[0].expected_result == original.criteria[0].expected_result
        cur.execute(
            "SELECT start_byte, relation, support_receipt IS NULL FROM omp_work.intake_claim_evidence "
            "WHERE workspace_id=%s AND run_id=%s AND revision=%s AND claim_id=%s AND start_byte IN (5,6) "
            "ORDER BY start_byte", (workspace_id, "run-1", revision, claim["id"]),
        )
        assert cur.fetchall() == [(5, "contradicts", True), (6, "neither", True)]


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
@pytest.mark.parametrize("bad", [
    {"verdict": "maybe"}, {"passage_sha256": "invalid"},
    {"measurement": "unknown", "input_tokens": 0},
])
def test_read_revision_rejects_malformed_stored_receipt(service, write_repository, bad) -> None:
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        revision = append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
    # Simulate corrupt historical data only in this disposable workspace and privileged transaction.
    with connection(service, workspace_id, "postgres") as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        result = conn.execute(
            "UPDATE omp_work.intake_claim_evidence SET support_receipt=%s "
            "WHERE workspace_id=%s AND run_id=%s AND revision=%s",
            (Jsonb(_receipt(**bad)), workspace_id, "run-1", revision),
        )
        assert result.rowcount == 1
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        with pytest.raises(ValidationError, match="support_receipt"):
            read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=revision)
