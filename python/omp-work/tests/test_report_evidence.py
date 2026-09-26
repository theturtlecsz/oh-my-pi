from __future__ import annotations

import json
from pathlib import Path

import pytest

from omp_work.report_evidence import (
    ChatModelClassifier,
    Classification,
    Claim,
    ClaimPassagePair,
    ClassifierAnswerError,
    Completion,
    EvidenceMatrixError,
    Passage,
    SourceDoc,
    StubClassifier,
    annotate_report,
    auditor_view,
    build_evidence_matrix,
    classify_pairs,
    classify_sentence,
    extract_claims,
    load_matrix,
    parse_classifier_answer,
    record_usage,
    render_classifier_prompt,
    render_matrix_table,
    store_matrix,
)

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


def test_fixture_matrix_has_expected_statuses_with_stub() -> None:
    """A known supported/contradicted/unsupported draft yields those statuses."""
    classifier = StubClassifier()
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )

    supported = _claim_by_text(matrix, "40 milliseconds")
    contested = _claim_by_text(matrix, "10 milliseconds")
    unsupported = _claim_by_text(matrix, "12 queues")

    assert matrix.statuses[supported.id] == "supported"
    assert matrix.statuses[contested.id] == "contested"
    assert matrix.statuses[unsupported.id] == "unsupported"

    assert [p.passage_id for p in matrix.supporting_passages(supported.id)] == ["p-1"]
    assert [p.passage_id for p in matrix.contradicting_passages(contested.id)] == ["p-1"]
    assert matrix.supporting_passages(unsupported.id) == []
    assert matrix.contradicting_passages(unsupported.id) == []


def test_supported_claim_requires_stored_passage_text() -> None:
    """A supports label on a text-free passage is refused, not silently kept."""

    class SupportsEverything:
        usages: list = []

        def classify(self, pairs):
            return [
                Classification(
                    claim_id=claim.id, passage_id=passage.passage_id, label="supports"
                )
                for claim, passage in pairs
            ]

    blank = {
        "src-1": SourceDoc(
            source_id="src-1",
            passages=(
                Passage(
                    passage_id="p-blank",
                    source_id="src-1",
                    locator="p.1",
                    text="   ",
                ),
            ),
        )
    }
    with pytest.raises(EvidenceMatrixError, match="without at least one stored passage"):
        build_evidence_matrix(
            report_id="report-1",
            draft="The cache reduces p99 latency to 40 milliseconds [src-1].\n",
            sources=blank,
            classifier=SupportsEverything(),
        )


def test_matrix_is_stored_and_replays_with_links_and_flags(tmp_path: Path) -> None:
    """Round-trip preserves statuses/digest; render shows links and flags."""
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=StubClassifier()
    )
    path = tmp_path / "evidence-matrix.json"
    digest = store_matrix(path, matrix)
    assert digest == matrix.digest

    replayed = load_matrix(path)
    assert replayed.statuses == matrix.statuses
    assert replayed.digest == matrix.digest

    table = render_matrix_table(replayed)
    assert "[src-1 p.1](#p-1)" in table
    assert "**contested**" in table
    assert "**unsupported**" in table

    annotated = annotate_report(DRAFT, replayed)
    assert "[evidence: [src-1 p.1](#p-1)]" in annotated
    assert "**[CONTESTED]**" in annotated
    assert "**[UNSUPPORTED]**" in annotated


def test_tampered_matrix_digest_is_refused(tmp_path: Path) -> None:
    """A matrix edited after storage must not replay."""
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=StubClassifier()
    )
    path = tmp_path / "evidence-matrix.json"
    store_matrix(path, matrix)
    payload = json.loads(path.read_text())
    payload["statuses"][_claim_by_text(matrix, "40 milliseconds").id] = "unsupported"
    path.write_text(json.dumps(payload))

    with pytest.raises(EvidenceMatrixError, match="digest mismatch"):
        load_matrix(path)


def test_probabilities_stay_out_of_report_body() -> None:
    """Routing-hint probabilities persist on the matrix but never render."""

    def complete(prompt: str) -> Completion:
        count = prompt.count("CLAIM:")
        answers = [
            {
                "pair": index,
                "label": "neither",
                "probabilities": {
                    "supports": 0.97,
                    "contradicts": 0.01,
                    "neither": 0.02,
                },
            }
            for index in range(1, count + 1)
        ]
        return Completion(
            text=json.dumps({"answers": answers}),
            input_tokens=10 * count,
            output_tokens=5 * count,
            model="fake-chat",
        )

    classifier = ChatModelClassifier(complete=complete, batch_size=2)
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )

    stored = matrix.to_dict()
    assert any(
        item["probabilities"] is not None for item in stored["classifications"]
    )
    assert classifier.usages[0].pair_count == 2

    table = render_matrix_table(matrix)
    annotated = annotate_report(DRAFT, matrix)
    for body in (table, annotated):
        assert "0.97" not in body
        assert "0.02" not in body
        assert "probabilities" not in body


def test_classifier_calls_land_in_usage_accounting(tmp_path: Path) -> None:
    """Every classifier call is recorded as a ledger usage event."""
    classifier = StubClassifier()
    build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )
    assert len(classifier.usages) == 1
    assert classifier.usages[0].pair_count > 0

    ledger = tmp_path / "ledger.json"
    events = record_usage(ledger, classifier.usages, conversation_id="report-1")
    assert len(events) == 1
    assert events[0]["kind"] == "claim_support_classify"

    written = json.loads(ledger.read_text())
    assert written["events"][0]["kind"] == "claim_support_classify"
    assert written["events"][0]["pair_count"] == classifier.usages[0].pair_count


def test_auditor_view_flags_unlinked_and_note_separates_metadata() -> None:
    """The auditor sees contested/unsupported ids and the separation note."""
    classifier = StubClassifier()
    matrix = build_evidence_matrix(
        report_id="report-1", draft=DRAFT, sources=SOURCES, classifier=classifier
    )
    view = auditor_view(matrix)

    assert view["matrix_digest"] == matrix.digest
    assert view["contested"] == [_claim_by_text(matrix, "10 milliseconds").id]
    assert view["unsupported"] == [_claim_by_text(matrix, "12 queues").id]
    assert view["unlinked"] == []
    assert "metadata checks" in view["semantic_support_note"]


def test_excluded_sentences_carry_a_reason() -> None:
    """Heading and question sentences are excluded with a recorded reason."""
    claims = extract_claims("# Findings\n\nWhich cache is best?\n")
    assert [c.material for c in claims] == [False, False]
    assert claims[0].excluded_reason == "heading"
    assert claims[1].excluded_reason == "question"
    assert classify_sentence("# Findings") == (False, "heading")


def test_unrequested_classifier_answer_is_refused() -> None:
    """The classifier seam refuses answers outside the requested pair set."""

    class OffSet:
        usages: list = []

        def classify(self, pairs: list[ClaimPassagePair]) -> list[Classification]:
            return [
                Classification(
                    claim_id="c-999", passage_id="p-999", label="supports"
                )
            ]

    claim = Claim(
        id="c-1", text="a", char_start=0, char_end=1, material=True
    )
    passage = Passage(
        passage_id="p-1", source_id="src-1", locator="p.1", text="b"
    )

    with pytest.raises(EvidenceMatrixError, match="unrequested pair"):
        classify_pairs([(claim, passage)], OffSet())


def test_prompt_covers_every_pair_and_omission_is_refused() -> None:
    """A strict answer must cover each pair exactly once."""
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
    pairs = [(claim, passage)]

    prompt = render_classifier_prompt(pairs)
    assert "CLAIM: The cache reduces p99 latency to 40 milliseconds ." in prompt
    assert "[src-1]" not in prompt

    with pytest.raises(ClassifierAnswerError, match="omitted pairs"):
        parse_classifier_answer('{"answers": []}', pairs)
