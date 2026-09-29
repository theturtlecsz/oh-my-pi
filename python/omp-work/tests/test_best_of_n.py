"""OMP-420-s06: Best-of-N candidates scored by a real harness."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.research_campaign import run_campaign
from omp_work.routing.best_of_n import fact_coverage_harness, run_best_of_n


def test_stub_harness_selects_true_maximum() -> None:
    scores = {"low": "METRIC quality=0.2\n", "high": "METRIC quality=0.9\n", "mid": "METRIC quality=0.5\n"}
    scored, winner = run_best_of_n(["low", "high", "mid"], lambda text: scores[text], metric="quality")
    assert [item.score for item in scored] == [0.2, 0.9, 0.5]
    assert [item.n for item in scored] == [1, 2, 3]
    assert winner.text == "high"
    assert winner.n == 2
    assert winner.metrics == {"quality": 0.9}


def test_missing_metric_raises() -> None:
    with pytest.raises(ValueError, match=r"candidate 1 'a'.*missing metric 'quality'"):
        run_best_of_n(["a", "b"], lambda text: "METRIC other=1.0\n", metric="quality")


def test_run_campaign_reports_custom_generate_and_harness() -> None:
    def generate(question: str, facts: list[str], n: int) -> list[str]:
        assert question == "q"
        assert facts == ["f"]
        assert n == 2
        return ["alpha", "beta"]

    def harness(text: str) -> str:
        return "METRIC fact_coverage=0.25\n" if text == "alpha" else "METRIC fact_coverage=0.75\n"

    result = run_campaign(
        campaign_id="c",
        question="q",
        retrieved_facts=["f"],
        generate=generate,
        harness=harness,
    )
    assert [candidate.n for candidate in result.candidates] == [1, 2]
    assert [candidate.text for candidate in result.candidates] == ["alpha", "beta"]
    assert [candidate.score for candidate in result.candidates] == [0.25, 0.75]
    assert [candidate.metrics for candidate in result.candidates] == [
        {"fact_coverage": 0.25},
        {"fact_coverage": 0.75},
    ]
    assert result.eval_winner_n == 2
    assert result.revision == "Revised once from N2: beta [adapted]"


def test_defaults_score_candidates_by_fact_coverage() -> None:
    facts = ["fact-alpha", "fact-beta"]
    result = run_campaign(campaign_id="c", question="q", retrieved_facts=facts)
    assert len(result.candidates) == 2
    assert all(candidate.score == candidate.metrics["fact_coverage"] for candidate in result.candidates)
    assert result.candidates[0].score == 0.5
    assert result.candidates[1].score == 0.5
    assert result.eval_winner_n == 1
    assert result.candidates[0].tokens == 8000 // 4


def test_fact_coverage_harness_zero_facts() -> None:
    assert fact_coverage_harness([])("anything") == "METRIC fact_coverage=0\n"
    assert fact_coverage_harness(["a", "b"])("only a here") == "METRIC fact_coverage=0.5\n"
