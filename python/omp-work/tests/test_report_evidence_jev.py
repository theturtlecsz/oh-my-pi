from __future__ import annotations

import json

import pytest

from omp_work.report_evidence import (
    JEV_DEFAULT_BASE_URL,
    JEV_STATE_CHAR_CAP,
    JevClassifier,
    Passage,
    Classification,
    ClassifierAnswerError,
    Claim,
    SourceDoc,
    _jev_batch_body,
    build_evidence_matrix,
    classify_pairs,
    render_matrix_table,
    annotate_report,
    truncate_passage,
    truncate_state,
)

from jev_stub import JevStub, probabilities

DRAFT = (
    "The cache reduces p99 latency to 40 milliseconds [src-1].\n"
    "The cache reduces p99 latency to 10 milliseconds [src-1].\n"
    "The scheduler processes 12 queues per cycle [src-1].\n"
)

SOURCES = {
    "src-1": SourceDoc(
        source_id="src-1",
        passages=(
            Passage(
                passage_id="p-1",
                source_id="src-1",
                locator="p.1",
                text="The cache reduces p99 latency to 40 milliseconds at peak load.",
            ),
            Passage(
                passage_id="p-2",
                source_id="src-1",
                locator="p.2",
                text="The scheduler keeps a bounded work queue.",
            ),
        ),
    )
}


def _claim_by_text(matrix, needle: str) -> Claim:
    matches = [c for c in matrix.claims if needle in c.text]
    assert len(matches) == 1, f"expected one claim containing {needle!r}"
    return matches[0]


def _jev_matrix(labels: list[str] | None = None, **kwargs):
    stub = JevStub(labels=labels, **kwargs)
    classifier = JevClassifier(transport=stub.transport, batch_size=4)
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )
    return stub, classifier, matrix


def test_jev_classifier_drives_the_same_matrix_statuses() -> None:
    """The Jev classifier fills the matrix and one usage entry per batch."""
    stub, classifier, matrix = _jev_matrix()

    assert len(matrix.classifications) > 0
    for classification in matrix.classifications:
        assert classification.label in ("supports", "contradicts", "neither")
    assert sum(u.pair_count for u in classifier.usages) == len(matrix.classifications)
    assert all(u.model.startswith("jev-latest") for u in classifier.usages)


def test_jev_label_per_pair_controls_status() -> None:
    """Distinct per-pair labels from the stub yield distinct claim statuses."""
    # One pair per claim here: top_k=1, so each claim answers exactly one pair.
    stub = JevStub(labels=["supports", "contradicts", "neither"])
    classifier = JevClassifier(transport=stub.transport, batch_size=4)
    matrix = build_evidence_matrix(
        report_id="report-1",
        draft=DRAFT,
        sources=SOURCES,
        classifier=classifier,
        top_k=1,
    )

    supported = _claim_by_text(matrix, "40 milliseconds")
    contested = _claim_by_text(matrix, "10 milliseconds")
    unsupported = _claim_by_text(matrix, "12 queues")

    assert matrix.statuses[supported.id] == "supported"
    assert matrix.statuses[contested.id] == "contested"
    assert matrix.statuses[unsupported.id] == "unsupported"


def test_jev_probabilities_are_stored_but_never_rendered() -> None:
    """Jev routing probabilities persist on the matrix and stay out of the report body."""
    stub, classifier, matrix = _jev_matrix(labels=["supports"] * 4)
    stored = matrix.to_dict()
    assert all(item["probabilities"] for item in stored["classifications"])

    table = render_matrix_table(matrix)
    annotated = annotate_report(DRAFT, matrix)
    for body in (table, annotated):
        assert "0.9" not in body
        assert "probabilities" not in body


def test_jev_off_list_option_is_refused() -> None:
    """An answer outside {supports, contradicts, neither} never becomes a label."""
    stub = JevStub(mode="off-list")
    classifier = JevClassifier(transport=stub.transport, batch_size=4)
    with pytest.raises(ClassifierAnswerError, match="off-list option"):
        build_evidence_matrix(
            report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
        )


def test_jev_missing_pair_answer_is_refused() -> None:
    """A response that omits one pair's choice answer is a typed refusal."""
    stub = JevStub(mode="missing-answer")
    classifier = JevClassifier(transport=stub.transport, batch_size=4)
    with pytest.raises(ClassifierAnswerError, match="omitted choice answer"):
        build_evidence_matrix(
            report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
        )


def test_jev_malformed_and_http_errors_are_refused() -> None:
    """Non-JSON bodies and non-2xx responses surface as ClassifierAnswerError."""
    for mode, match in (("malformed", "not JSON"), ("500", "HTTP 500")):
        classifier = JevClassifier(transport=JevStub(mode=mode).transport)
        with pytest.raises(ClassifierAnswerError, match=match):
            build_evidence_matrix(
                report_id="report-1",
                draft=DRAFT,
                sources=SOURCES,
                classifier=classifier,
            )


def test_jev_truncates_state_with_marker_and_cap() -> None:
    """A pair body longer than the cap is head-truncated with the marker."""
    long_text = "word " * 3000
    stub = JevStub()
    classifier = JevClassifier(transport=stub.transport, batch_size=4)
    long_source = {
        "src-1": SourceDoc(
            source_id="src-1",
            passages=(
                Passage(
                    passage_id="p-long",
                    source_id="src-1",
                    locator="p.1",
                    text=long_text,
                ),
            ),
        )
    }
    build_evidence_matrix(
        report_id="report-1",
        draft="The cache reduces p99 latency to 40 milliseconds [src-1].\n",
        sources=long_source,
        classifier=classifier,
    )

    sent_state = stub.payloads[0]["state"]
    assert "PASSAGE:" in sent_state
    assert "…[truncated " in sent_state
    assert sent_state.endswith(" chars]")

    # Rebuild the untruncated state to check the cap arithmetic exactly.
    full_state = _jev_batch_body(
        [
            (
                Claim(
                    id="c-1",
                    text="The cache reduces p99 latency to 40 milliseconds [src-1].",
                    char_start=0,
                    char_end=1,
                    material=True,
                ),
                Passage(
                    passage_id="p-long",
                    source_id="src-1",
                    locator="p.1",
                    text=long_text,
                ),
            )
        ]
    )["state"]
    assert len(full_state) > JEV_STATE_CHAR_CAP
    assert sent_state == (
        f"{full_state[:JEV_STATE_CHAR_CAP]}"
        f"…[truncated {len(full_state) - JEV_STATE_CHAR_CAP} chars]"
    )


def test_truncate_state_under_cap_is_unchanged() -> None:
    """A state within the cap is sent verbatim with no marker or truncation flag."""
    short = "x" * JEV_STATE_CHAR_CAP
    assert truncate_state(short) == (short, False)
    truncated, flag = truncate_state(short + "yz")
    assert flag is True
    assert truncated.startswith(short)
    assert truncated.endswith("…[truncated 2 chars]")


def test_jev_classifier_satisfies_the_protocol_seam() -> None:
    """The Jev classifier works through classify_pairs and the classifier protocol."""
    claim = Claim(
        id="c-1",
        text="The cache reduces p99 latency to 40 milliseconds [src-1].",
        char_start=0,
        char_end=1,
        material=True,
    )
    passage = Passage(
        passage_id="p-1", source_id="src-1", locator="p.1", text="text"
    )
    classifier = JevClassifier(transport=JevStub(labels=["supports"]).transport)
    results = classify_pairs([(claim, passage)], classifier)
    assert results == [
        Classification(
            claim_id="c-1",
            passage_id="p-1",
            label="supports",
            probabilities=probabilities("supports"),
        )
    ]


def test_jev_request_body_is_typed_choice_over_the_labels() -> None:
    """The wire body is a Jev choice over exactly the three labels, text-only state."""
    stub = JevStub()
    classifier = JevClassifier(transport=stub.transport, base_url=JEV_DEFAULT_BASE_URL)
    classifier.classify(
        [
            (
                Claim(
                    id="c-1",
                    text="The cache reduces p99 latency to 40 milliseconds [src-1].",
                    char_start=0,
                    char_end=1,
                    material=True,
                ),
                Passage(
                    passage_id="p-1",
                    source_id="src-1",
                    locator="p.1",
                    text="The cache reduces p99 latency to 40 milliseconds.",
                    metadata={"doi": "10.1/secret", "venue": "Hidden Venue"},
                ),
            )
        ]
    )

    body = stub.payloads[0]
    assert body["model"]
    assert "CLAIM:" in body["state"]
    assert "PASSAGE:" in body["state"]
    # Passage metadata never reaches the request, and citations are stripped.
    serialized = json.dumps(body)
    assert "10.1/secret" not in serialized
    assert "Hidden Venue" not in serialized
    assert "src-1" not in body["state"]

    question = next(iter(body["questions"].values()))
    assert question["type"] == "choice"
    assert question["options"] == ["supports", "contradicts", "neither"]


def test_jev_usage_records_one_entry_per_batch() -> None:
    """Each Jev call records one usage entry carrying its pair count."""
    stub = JevStub()
    classifier = JevClassifier(transport=stub.transport, batch_size=2)
    build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )
    total_pairs = sum(usage.pair_count for usage in classifier.usages)
    assert total_pairs == len(classifier.usages) * 2
    assert all(usage.model.startswith("jev-latest") for usage in classifier.usages)


def test_jev_tied_probabilities_resolve_to_neither() -> None:
    """A tie between supports and contradicts is least committal: neither."""
    claim = Claim(
        id="c-1",
        text="The cache reduces p99 latency to 40 milliseconds [src-1].",
        char_start=0,
        char_end=1,
        material=True,
    )
    passage = Passage(
        passage_id="p-1", source_id="src-1", locator="p.1", text="text"
    )
    stub = JevStub(
        answers={
            "pair-1": {
                "probabilities": {"supports": 0.5, "contradicts": 0.5, "neither": 0.0}
            }
        }
    )
    classifier = JevClassifier(transport=stub.transport)
    (result,) = classifier.classify([(claim, passage)])
    assert result.label == "neither"
    assert result.probabilities == {
        "supports": 0.5,
        "contradicts": 0.5,
        "neither": 0.0,
    }


def test_jev_answer_keyed_by_instructions_is_accepted() -> None:
    """A response that keys the choice by the instructions text still resolves."""
    claim = Claim(
        id="c-1",
        text="The cache reduces p99 latency to 40 milliseconds [src-1].",
        char_start=0,
        char_end=1,
        material=True,
    )
    passage = Passage(
        passage_id="p-1", source_id="src-1", locator="p.1", text="text"
    )
    body = _jev_batch_body([(claim, passage)])
    instructions = body["questions"]["pair-1"]["instructions"]
    stub = JevStub(
        answers={instructions: {"probabilities": probabilities("contradicts")}}
    )
    classifier = JevClassifier(transport=stub.transport)
    (result,) = classifier.classify([(claim, passage)])
    assert result.label == "contradicts"


def test_jev_batch_preserves_every_passage_under_partitioning() -> None:
    """Eight 2400-char passages partition across batches without cutting off any passage."""
    stub = JevStub(labels=["supports"] * 8)
    classifier = JevClassifier(transport=stub.transport, batch_size=8)
    claims = [
        Claim(
            id=f"c-{i}",
            text=f"Claim {i} asserts cache performance [src-1].",
            char_start=0,
            char_end=20,
            material=True,
        )
        for i in range(8)
    ]
    passages = [
        Passage(
            passage_id=f"p-{i}",
            source_id="src-1",
            locator=f"p.{i}",
            text=f"PASSAGE-{i}-START " + ("benchmark-evidence " * 120) + f"PASSAGE-{i}-END",
        )
        for i in range(8)
    ]
    pairs = list(zip(claims, passages, strict=True))
    for _, p in pairs:
        assert 2000 < len(p.text) < 2500

    results = classifier.classify(pairs)

    assert len(results) == 8
    assert all(r.label == "supports" for r in results)
    # 8 * ~2300 chars > 8000 cap, so multiple batches were sent.
    assert len(stub.payloads) > 1

    assert len(stub.payloads) == 3
    assert "PASSAGE-0-START" in stub.payloads[0]["state"]
    assert "PASSAGE-2-END" in stub.payloads[0]["state"]
    assert "PASSAGE-3-START" in stub.payloads[1]["state"]
    assert "PASSAGE-5-END" in stub.payloads[1]["state"]
    assert "PASSAGE-6-START" in stub.payloads[2]["state"]
    assert "PASSAGE-7-END" in stub.payloads[2]["state"]

    for payload in stub.payloads:
        sent_state = payload["state"]
        # None of the individual passages exceeded 8000 chars, so no truncation marker.
        assert "…[truncated" not in sent_state
        question_names = list(payload["questions"].keys())
        for q_name in question_names:
            pair_num = int(q_name.split("-")[1])
            assert f"{pair_num}. CLAIM:" in sent_state


def test_jev_overlong_passage_isolated_and_truncated() -> None:
    """An overlong passage is isolated to its own batch and truncated; subsequent pairs remain intact."""
    stub = JevStub(labels=["supports", "supports"])
    classifier = JevClassifier(transport=stub.transport, batch_size=8)
    overlong = Passage(
        passage_id="p-long",
        source_id="src-1",
        locator="p.1",
        text="PASSAGE-0-START " + ("giant-text " * 1500) + "PASSAGE-0-END",
    )
    normal = Passage(
        passage_id="p-short",
        source_id="src-1",
        locator="p.2",
        text="PASSAGE-1-START short text PASSAGE-1-END",
    )
    claim1 = Claim(id="c-1", text="Claim 1.", char_start=0, char_end=5, material=True)
    claim2 = Claim(id="c-2", text="Claim 2.", char_start=0, char_end=5, material=True)

    results = classifier.classify([(claim1, overlong), (claim2, normal)])

    assert len(results) == 2
    assert len(stub.payloads) == 2

    # Batch 1: only pair 1, truncated with marker
    p1 = stub.payloads[0]
    assert list(p1["questions"].keys()) == ["pair-1"]
    assert "…[truncated " in p1["state"]
    assert "PASSAGE-0-START" in p1["state"]

    # Batch 2: pair 2, completely intact with no truncation marker
    p2 = stub.payloads[1]
    assert list(p2["questions"].keys()) == ["pair-1"]
    assert "…[truncated" not in p2["state"]
    assert "PASSAGE-1-START short text PASSAGE-1-END" in p2["state"]


def test_measurement_harness_requires_at_least_100_pairs(tmp_path) -> None:
    """The measurement harness requires >= 100 hand-labeled pairs per spec."""
    import sys
    sys.path.insert(0, "docs/reports/jev-claim-support")
    from run import load_labels, update_report

    label_file = tmp_path / "few_labels.jsonl"
    lines = [
        json.dumps(
            {
                "claim": f"Claim {i}",
                "passage": f"Passage {i}",
                "label": "supports",
            }
        )
        for i in range(50)
    ]
    label_file.write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(ValueError, match="acceptance measurement requires >= 100"):
        load_labels(label_file)


def test_measurement_harness_update_report_replaces_pending_results() -> None:
    """update_report replaces the pending Results table while preserving the report structure."""
    import sys
    sys.path.insert(0, "docs/reports/jev-claim-support")
    from run import update_report

    skeleton = (
        "# Title\n\n"
        "## Results\n\n"
        "| Metric | Chat classifier | Jev |\n"
        "| --- | --- | --- |\n"
        "| Hand-label accuracy | pending | pending |\n\n"
        "## What is not measured\n\n"
        "Nothing.\n"
    )
    new_results = "Measured 100 pairs.\n\n| Metric | Chat classifier | Jev |\n| --- | --- | --- |\n| Hand-label accuracy | 92.0% | 94.0% |"
    updated = update_report(skeleton, new_results)
    assert "pending" not in updated
    assert "Measured 100 pairs." in updated
    assert "## What is not measured" in updated


