"""Contract tests for the deterministic fleet context compiler (OMP-311 / FK-5).

Each test defends an externally observable bundle contract:

- shuffled input yields byte-identical text and hashes;
- non-current items never render and each becomes an exclusion;
- the rendered text always fits the token budget and mandatory overflow raises;
- a request JSON round trip recompiles to identical bytes;
- the token counts are content-addressed and the layout is a stable prefix with
  the identity trailer last.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest
from omp_knowledge.context.compiler import BudgetInsufficientError, compile_bundle
from omp_knowledge.context.models import (
    CompileRequest,
    ContextItem,
    Section,
    StageIdentity,
    Status,
)
from omp_work.v1.canonical import sha256


class WordCounter:
    """Deterministic fake tokenizer: one token per whitespace-separated word."""

    profile = "test-words-v1"

    def count(self, texts: Sequence[str]) -> list[int]:
        return [len(text.split()) for text in texts]


def _identity() -> StageIdentity:
    return StageIdentity(
        work_id="11111111-1111-1111-1111-111111111111",
        work_key="OMP-311",
        revision_id="22222222-2222-2222-2222-222222222222",
        stage="implement",
        attempt_id="33333333-3333-3333-3333-333333333333",
        candidate_id="44444444-4444-4444-4444-444444444444",
    )


def _item(
    *,
    section: Section,
    source: str,
    ref: str,
    text: str,
    status: Status = "current",
    mandatory: bool = False,
    score: float | None = None,
    detail: str = "",
) -> ContextItem:
    return ContextItem(
        section=section,
        source=source,
        ref=ref,
        text=text,
        status=status,
        mandatory=mandatory,
        score=score,
        detail=detail,
    )


def _request(
    items: Sequence[ContextItem],
    *,
    budget: int = 1_000_000,
    encoding: str = "utf-8",
) -> CompileRequest:
    return CompileRequest(
        identity=_identity(),
        token_budget=budget,
        encoding=encoding,
        items=tuple(items),
    )


def test_shuffled_items_produce_byte_identical_bundle() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="the exact revision", mandatory=True),
        _item(section="structural", source="enola", ref="fact-a", text="alpha symbol", score=0.9),
        _item(section="structural", source="enola", ref="fact-b", text="beta symbol", score=0.4),
        _item(section="procedural", source="lesson", ref="proc-1", text="how to do the thing", score=0.7),
        _item(section="semantic", source="retrieval", ref="hit-1", text="semantic hit text", score=0.2),
        _item(section="semantic", source="retrieval", ref="hit-2", text="stale prior hit", status="stale"),
        _item(section="semantic", source="retrieval", ref="hit-3", text="denied repo hit", status="denied"),
        _item(section="procedural", source="lesson", ref="proc-2", text="withdrawn lesson", status="withdrawn"),
        _item(section="structural", source="enola", ref="fact-c", text="missing snapshot fact", status="missing"),
    ]

    baseline = compile_bundle(_request(items), counter)
    shuffled = list(items)
    random.Random(20260926).shuffle(shuffled)
    recompiled = compile_bundle(_request(shuffled), counter)

    assert recompiled.text == baseline.text
    assert recompiled.sha256 == baseline.sha256
    assert recompiled.section_sha256 == baseline.section_sha256
    assert [item.ref for item in recompiled.included] == [item.ref for item in baseline.included]
    assert [exclusion.ref for exclusion in recompiled.exclusions] == [
        exclusion.ref for exclusion in baseline.exclusions
    ]


def test_non_current_items_are_excluded_and_never_render() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="CURRENT-EXACT-TEXT", mandatory=True),
        _item(section="exact", source="work", ref="stale-rev", text="STALE-TEXT", status="stale", detail="other revision"),
        _item(section="structural", source="enola", ref="denied-fact", text="DENIED-TEXT", status="denied", detail="repo not permitted"),
        _item(section="procedural", source="lesson", ref="withdrawn-proc", text="WITHDRAWN-TEXT", status="withdrawn", detail="retired by FK-6"),
        _item(section="semantic", source="retrieval", ref="missing-hit", text="MISSING-TEXT", status="missing", detail="snapshot unpublished"),
    ]

    bundle = compile_bundle(_request(items), counter)

    for withheld in ("STALE-TEXT", "DENIED-TEXT", "WITHDRAWN-TEXT", "MISSING-TEXT"):
        assert withheld not in bundle.text

    assert [item.ref for item in bundle.included] == ["revision"]
    assert {
        (exclusion.ref, exclusion.reason, exclusion.detail) for exclusion in bundle.exclusions
    } == {
        ("stale-rev", "stale", "other revision"),
        ("denied-fact", "denied", "repo not permitted"),
        ("withdrawn-proc", "withdrawn", "retired by FK-6"),
        ("missing-hit", "missing", "snapshot unpublished"),
    }


def test_budget_trims_optional_items_and_stays_within_budget() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="mandatory acceptance criteria", mandatory=True),
        _item(section="structural", source="enola", ref="fact-a", text="alpha symbol with a longer description here", score=0.9),
        _item(section="structural", source="enola", ref="fact-b", text="beta symbol with another longer description", score=0.5),
        _item(section="semantic", source="retrieval", ref="hit-1", text="semantic hit that is also fairly long", score=0.1),
    ]
    generous = compile_bundle(_request(items, budget=1_000_000), counter)
    budget = generous.tokens - 1

    bundle = compile_bundle(_request(items, budget=budget), counter)

    assert bundle.tokens <= budget
    assert counter.count([bundle.text])[0] == bundle.tokens
    assert len(bundle.included) < len(items)
    assert any(exclusion.reason == "budget" for exclusion in bundle.exclusions)
    # Mandatory items are never dropped to satisfy the budget.
    assert any(item.ref == "revision" for item in bundle.included)


def test_mandatory_overflow_raises_budget_insufficient() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="mandatory acceptance criteria", mandatory=True),
    ]

    with pytest.raises(BudgetInsufficientError) as excinfo:
        compile_bundle(_request(items, budget=1), counter)

    assert excinfo.value.budget == 1
    assert excinfo.value.required > 1


def test_json_round_trip_recompiles_to_same_bytes() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="exact revision text", mandatory=True),
        _item(section="structural", source="enola", ref="fact-a", text="alpha symbol", score=0.9),
        _item(section="semantic", source="retrieval", ref="hit-1", text="stale prior hit", status="stale"),
    ]
    request = _request(items, budget=500, encoding="cl100k_base")

    baseline = compile_bundle(request, counter)
    restored = CompileRequest.model_validate_json(request.model_dump_json())
    recompiled = compile_bundle(restored, counter)

    assert restored == request
    assert recompiled.text == baseline.text
    assert recompiled.sha256 == baseline.sha256
    assert recompiled.section_sha256 == baseline.section_sha256
    assert recompiled.exclusions == baseline.exclusions


def test_counts_are_content_addressed_and_profile_is_recorded() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="exact revision text", mandatory=True),
        _item(section="structural", source="enola", ref="fact-a", text="alpha symbol", score=0.9),
    ]

    bundle = compile_bundle(_request(items), counter)

    assert bundle.counter_profile == counter.profile
    assert set(bundle.counts) == {sha256(item.text) for item in bundle.included}
    assert all(bundle.counts[sha256(item.text)] == len(item.text.split()) for item in bundle.included)


def test_layout_is_stable_prefix_with_identity_trailer() -> None:
    counter = WordCounter()
    items = [
        _item(section="exact", source="work", ref="revision", text="exact revision text", mandatory=True),
        _item(section="semantic", source="retrieval", ref="hit-1", text="semantic hit text", score=0.3),
    ]

    bundle = compile_bundle(_request(items), counter)
    lines = bundle.text.split("\n")

    assert lines[0] == "FLEET CONTEXT v1"
    section_headers = [f"## {section}" for section in ("exact", "structural", "procedural", "semantic")]
    positions = [bundle.text.index(header) for header in section_headers]
    assert positions == sorted(positions)
    assert bundle.text.rindex("--- identity ---") > positions[-1]
    assert f"stage: {_identity().stage}" in bundle.text
    assert f"counter_profile: {counter.profile}" in bundle.text
    assert bundle.sha256 == sha256(bundle.text)


def test_section_hashes_isolate_sections() -> None:
    counter = WordCounter()
    base = [
        _item(section="exact", source="work", ref="revision", text="exact revision text", mandatory=True),
    ]
    extended = base + [
        _item(section="semantic", source="retrieval", ref="hit-1", text="semantic hit text", score=0.3),
    ]

    base_bundle = compile_bundle(_request(base), counter)
    extended_bundle = compile_bundle(_request(extended), counter)

    assert extended_bundle.section_sha256["exact"] == base_bundle.section_sha256["exact"]
    assert extended_bundle.section_sha256["semantic"] != base_bundle.section_sha256["semantic"]
    assert set(extended_bundle.section_sha256) == {"exact", "structural", "procedural", "semantic"}
