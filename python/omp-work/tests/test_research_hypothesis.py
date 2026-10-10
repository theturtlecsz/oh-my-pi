"""R11 contracts for isolated drafts, evolution, and non-certifying selections."""

import ast
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from omp_work.research import hypothesis as module
from omp_work.research.hypothesis import (
    SelectionRecord,
    challenge,
    combine,
    comparative,
    comparison_key,
    decide,
    dedupe,
    generate,
    generation_context,
    order_effect,
    ranking,
)


def test_generation_rejects_sibling_context_and_keeps_drafts_isolated():
    with pytest.raises(ValueError, match="^leaked_context$"):
        generation_context("Why?", "frozen", sibling_texts=("Sibling claim",))

    context = generation_context("Why?", "frozen")
    assert asdict(context) == {"question": "Why?", "frozen_sha": "frozen"}
    first, second = generate(context, ("First claim", "Sibling claim"))
    assert first.text == "First claim"
    assert second.text == "Sibling claim"
    assert second.text not in first.text
    assert first.text not in second.text
    assert first.id != second.id
    assert (first.question, first.context_sha, first.kind) == ("Why?", "frozen", "draft")
    assert generate(context, ()) == []


def test_challenge_and_combine_create_distinct_evolved_hypotheses():
    first, second = generate(generation_context("Why?", "frozen"), ("Claim A", "Claim B"))
    challenged = challenge(first, "Measurement is reliable")
    combined = combine(first, second)
    assert challenged.kind == "challenge"
    assert "Measurement is reliable" in challenged.text
    assert first.text in challenged.text
    assert combined.kind == "combine"
    assert first.text in combined.text and second.text in combined.text
    assert len({first.id, second.id, challenged.id, combined.id}) == 4
    assert challenged.question == combined.question == first.question
    assert challenged.context_sha == combined.context_sha == first.context_sha
    unrelated = generate(generation_context("Other question?", "frozen"), ("Claim C",))[0]
    with pytest.raises(ValueError, match="^incompatible$"):
        combine(first, unrelated)


def test_dedupe_retains_earliest_text_after_casefold_and_whitespace_collapse():
    drafts = generate(
        generation_context("Why?", "frozen"),
        ("Same claim.", "same  claim.", "Different claim.", "\tSAME\nclaim. "),
    )
    assert dedupe(drafts) == [drafts[0], drafts[2]]


@pytest.mark.parametrize("outcome", ["A", "B", "tie", "inconclusive"])
def test_unanimous_ranking_preserves_outcome_without_certifying(outcome):
    selection = SelectionRecord(
        ("h1", "h2"), (("h1", "h2"), ("h2", "h1")), (outcome, outcome), "judge"
    )
    assert ranking(selection) == outcome
    assert not order_effect(selection)
    assert selection.label == "search aid, not a validity certificate"


def test_order_disagreement_survives_identity_and_tournament_changes():
    selection = SelectionRecord(
        ("h1", "h2"), (("h1", "h2"), ("h2", "h1")), ("A", "B"), "model-1"
    )
    assert order_effect(selection)
    assert ranking(selection) == "inconclusive"
    assert comparison_key(selection) == (
        ("h1", "h2"), (("h1", "h2"), ("h2", "h1")), ("A", "B")
    )
    assert comparison_key(selection) == comparison_key(
        replace(selection, model_identity="model-2", tournament_id="tournament-2")
    )
    assert comparison_key(selection) != comparison_key(replace(selection, outcomes=("B", "A")))


@pytest.mark.parametrize(
    "outcomes, error",
    [(("A",), "one outcome per order required"), (("A", "winner"), "invalid outcome")],
)
def test_selection_rejects_missing_or_invalid_outcomes(outcomes, error):
    with pytest.raises(ValueError, match=error):
        SelectionRecord(("h1", "h2"), (("h1", "h2"), ("h2", "h1")), outcomes, "judge")


@pytest.mark.parametrize(
    "variables, ablation, causal",
    [
        (("X", "Y"), True, "uncertain"),
        (("X",), False, "uncertain"),
        (("X",), True, "attributed"),
        ((), True, "uncertain"),
    ],
)
def test_attribution_requires_one_variable_and_ablation(variables, ablation, causal):
    assert comparative(variables, ablation).causal == causal


def test_unsupported_causal_claim_is_challenged():
    result = comparative(("X",), False, claim="caused by X")
    assert result.causal == "uncertain"
    assert result.status == "challenged"
    assert comparative(("X",), True, claim="caused by X").status == "unchallenged"
    assert comparative(("X",), False, claim="associated with X").status == "unchallenged"


@pytest.mark.parametrize(
    "kind, delta, infra, decision",
    [
        ("draft", 0.1, True, "repair"),
        ("combine", -0.1, True, "repair"),
        ("draft", 0.1, False, "refine"),
        ("draft", -0.1, False, "pivot"),
        ("draft", 0.0, False, "pivot"),
        ("challenge", 0.1, False, "pivot"),
        ("combine", 0.1, False, "pivot"),
    ],
)
def test_decision_prioritizes_infra_then_positive_draft_delta(kind, delta, infra, decision):
    assert decide(kind, delta, infra) == decision


def test_module_has_no_intake_import():
    tree = ast.parse(Path(module.__file__).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
            imports.extend(alias.name for alias in node.names)
    assert all("intake" not in name.split(".") for name in imports)
