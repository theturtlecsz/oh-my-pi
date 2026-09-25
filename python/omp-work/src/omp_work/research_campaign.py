"""Bounded research campaign: baseline→candidate→eval→one revision→stop; Best-of-N=2."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Candidate:
    n: int
    text: str
    score: float
    tokens: int


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


def run_campaign(
    *,
    campaign_id: str,
    question: str,
    retrieved_facts: list[str],
    parent_budget_tokens: int = 8000,
) -> CampaignResult:
    phases: list[str] = []
    phases.append("baseline")
    baseline = f"Baseline answer for: {question} | facts={len(retrieved_facts)}"
    phases.append("candidate")
    half = max(1, parent_budget_tokens // 4)
    if retrieved_facts:
        c1 = Candidate(n=1, text=f"N1: {retrieved_facts[0]}", score=0.7, tokens=half)
    else:
        c1 = Candidate(n=1, text=baseline + " | N1: lean on fact0", score=0.55, tokens=half)
    if len(retrieved_facts) > 1:
        c2 = Candidate(n=2, text=f"N2: {retrieved_facts[1]}", score=0.65, tokens=half)
    else:
        c2 = Candidate(n=2, text=f"N2: alternate — {question}", score=0.4, tokens=half)
    candidates = [c1, c2]
    spent = c1.tokens + c2.tokens
    if spent > parent_budget_tokens:
        raise ValueError("BoN exceeded parent budget")
    phases.append("eval")
    winner = max(candidates, key=lambda c: c.score)
    phases.append("revision")
    revision = f"Revised once from N{winner.n}: {winner.text} [adapted]"
    spent += half // 2
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
