"""Held-out reports expose failures without conflating timeout and abstain."""

from __future__ import annotations

import json

import httpx
import pytest

from omp_work import intake_support_eval as ev
from omp_work import intake_support_judge as judge
from omp_work.intake_support_corpus import judge_input, load_pairs
from omp_work.intake_support_judge import Completion


def test_mixed_report_keeps_timeout_and_false_support_denominators_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = judge.corpus_dir()
    pairs = [
        pair for pair in load_pairs(base / "inputs", base / "manifest.json")
        if pair.split == "heldout"
    ]
    negatives = [pair for pair in pairs if pair.gold != "supports"]
    positives = [pair for pair in pairs if pair.gold == "supports"]
    false_ids = {pair.id for pair in negatives[:2]}
    timeout_ids = {negatives[2].id, positives[1].id}
    abstain_ids = {negatives[3].id, positives[2].id, positives[3].id}
    verdicts = {pair.id: "neither" for pair in pairs}
    verdicts.update(dict.fromkeys(false_ids | {positives[0].id}, "supports"))
    verdicts.update(dict.fromkeys(timeout_ids, "timeout"))
    verdicts.update(dict.fromkeys(abstain_ids, "abstain"))
    pending = iter(pairs)
    attempted: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        pair = next(pending)
        attempted.append(pair.id)
        if verdicts[pair.id] == "timeout":
            raise httpx.ReadTimeout("judge timed out", request=request)
        return httpx.Response(200, json={
            "text": json.dumps({"verdict": verdicts[pair.id]}),
            "input_tokens": 3,
            "output_tokens": 2,
        })

    transport = httpx.MockTransport(respond)
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "https://judge.example/complete")

    def complete(prompt: str, model_id: str) -> Completion:
        return judge.complete_from_env(prompt, model_id, transport=transport)

    report = ev.build_report(split="heldout", max_usd=1, complete=complete)

    assert attempted == [pair.id for pair in pairs]
    assert report["n_pairs"] == len(pairs)
    assert report["n_negative"] == len(negatives)
    assert report["false_supports"] == 2
    assert report["false_support_rate"] == 2 / len(negatives)
    assert (report["wilson_low"], report["wilson_high"]) == ev.wilson(2, len(negatives))
    assert report["abstains"] == 3
    assert report["abstain_rate"] == 3 / len(pairs)
    assert (report["abstain_wilson_low"], report["abstain_wilson_high"]) == ev.wilson(
        3, len(pairs)
    )
    assert report["timeouts"] == 2
    assert report["timeout_rate"] == 2 / len(pairs)
    assert report["input_tokens"] is None
    assert report["output_tokens"] is None

    expected = []
    for pair in pairs:
        view = judge_input(pair)
        expected.append({
            "id": view["id"],
            "gold": pair.gold,
            "verdict": verdicts[pair.id],
            "claim": view["claim_text"],
            "passage": view["passage"],
            "context": view["context"],
        })
    assert report["per_pair"] == expected
    assert report["false_support_pairs"] == [row for row in expected if row["id"] in false_ids]
    assert report["timeout_pairs"] == [row for row in expected if row["id"] in timeout_ids]
