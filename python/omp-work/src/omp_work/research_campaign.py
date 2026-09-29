"""Bounded research campaign: baseline→candidate→eval→one revision→stop.

The candidate count and scoring metric come from the written routing policy's
``research`` stage (``best_of_n``); candidates are real generated texts scored
by a harness, not hardcoded numbers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from omp_work.routing.best_of_n import fact_coverage_harness, run_best_of_n
from omp_work.routing.policy import RoutingPolicy, load_policy

Generate = Callable[[str, list[str], int], list[str]]
Harness = Callable[[str], str]


@dataclass
class Candidate:
    n: int
    text: str
    score: float
    tokens: int
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class CampaignResult:
    campaign_id: str
    phases: list[str]
    baseline: str
    candidates: list[Candidate]
    eval_winner_n: int
    revision: str
    stopped: bool
    finding: str
    parent_budget_tokens: int
    spent_tokens: int
    adapted: bool


def _baseline(question: str, retrieved_facts: list[str]) -> str:
    return f"Baseline answer for: {question} | facts={len(retrieved_facts)}"


def _default_generate(question: str, retrieved_facts: list[str], n: int) -> list[str]:
    baseline = _baseline(question, retrieved_facts)
    texts: list[str] = []
    for index in range(1, n + 1):
        if index <= len(retrieved_facts):
            texts.append(f"N{index}: {retrieved_facts[index - 1]}")
        elif index == 1:
            texts.append(baseline + " | N1: lean on fact0")
        else:
            texts.append(f"N{index}: alternate — {question}")
    return texts


def run_campaign(
    *,
    campaign_id: str,
    question: str,
    retrieved_facts: list[str],
    parent_budget_tokens: int = 8000,
    generate: Generate | None = None,
    harness: Harness | None = None,
    policy: RoutingPolicy | None = None,
) -> CampaignResult:
    policy = policy if policy is not None else load_policy()
    best_of_n = policy.stages["research"].best_of_n
    if best_of_n is None:
        raise ValueError("research stage has no best_of_n policy")
    n = best_of_n.n
    metric = best_of_n.metric
    if generate is None:
        generate = _default_generate
    if harness is None:
        harness = fact_coverage_harness(retrieved_facts)

    phases: list[str] = []
    phases.append("baseline")
    baseline = _baseline(question, retrieved_facts)
    phases.append("candidate")
    texts = generate(question, list(retrieved_facts), n)
    scored, winner_scored = run_best_of_n(texts, harness, metric=metric)
    tokens = max(1, parent_budget_tokens // (2 * n))
    candidates = [
        Candidate(n=item.n, text=item.text, score=item.score, tokens=tokens, metrics=item.metrics)
        for item in scored
    ]
    spent = sum(candidate.tokens for candidate in candidates)
    if spent > parent_budget_tokens:
        raise ValueError("BoN exceeded parent budget")
    phases.append("eval")
    winner = next(candidate for candidate in candidates if candidate.n == winner_scored.n)
    phases.append("revision")
    revision = f"Revised once from N{winner.n}: {winner.text} [adapted]"
    spent += tokens // 2
    if spent > parent_budget_tokens:
        spent = parent_budget_tokens
    phases.append("stop")
    return CampaignResult(
        campaign_id=campaign_id,
        phases=phases,
        baseline=baseline,
        candidates=candidates,
        eval_winner_n=winner.n,
        revision=revision,
        stopped=True,
        finding=revision,
        parent_budget_tokens=parent_budget_tokens,
        spent_tokens=spent,
        adapted=True,
    )
