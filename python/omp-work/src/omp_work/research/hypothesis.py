"""Pure hypothesis evolution and recorded search selections (R11)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

HypothesisKind = Literal["draft", "challenge", "combine"]
Outcome = Literal["A", "B", "tie", "inconclusive"]
Order = tuple[str, str]
ComparisonKey = tuple[tuple[str, str], tuple[Order, ...], tuple[Outcome, ...]]


@dataclass(frozen=True)
class GenerationContext:
    question: str
    frozen_sha: str


@dataclass(frozen=True)
class Hypothesis:
    id: str
    text: str
    question: str
    context_sha: str
    kind: HypothesisKind


def generation_context(
    question: str, frozen_sha: str, sibling_texts: Sequence[str] = ()
) -> GenerationContext:
    """Keep generation context independent of sibling hypotheses."""
    if sibling_texts:
        raise ValueError("leaked_context")
    return GenerationContext(question, frozen_sha)


def generate(context: GenerationContext, drafts: Iterable[str]) -> list[Hypothesis]:
    return [
        Hypothesis(uuid4().hex, text, context.question, context.frozen_sha, "draft")
        for text in drafts
    ]


def challenge(hypothesis: Hypothesis, assumption: str) -> Hypothesis:
    return Hypothesis(
        uuid4().hex,
        f"{hypothesis.text}\nChallenge assumption: {assumption}",
        hypothesis.question,
        hypothesis.context_sha,
        "challenge",
    )


def combine(a: Hypothesis, b: Hypothesis) -> Hypothesis:
    if a.question != b.question:
        raise ValueError("incompatible")
    return Hypothesis(
        uuid4().hex, f"{a.text}\n{b.text}", a.question, a.context_sha, "combine"
    )


def dedupe(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    seen: set[str] = set()
    result: list[Hypothesis] = []
    for hypothesis in hypotheses:
        normalized = " ".join(hypothesis.text.casefold().split())
        if normalized not in seen:
            seen.add(normalized)
            result.append(hypothesis)
    return result


@dataclass(frozen=True)
class SelectionRecord:
    """Store supplied comparisons without judging or certifying hypotheses."""

    hypothesis_ids: tuple[str, str]
    orders: tuple[Order, ...]
    outcomes: tuple[Outcome, ...]
    model_identity: str
    tournament_id: str | None = None
    label: Literal["search aid, not a validity certificate"] = field(
        default="search aid, not a validity certificate", init=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_ids", tuple(self.hypothesis_ids))
        object.__setattr__(self, "orders", tuple(tuple(order) for order in self.orders))
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        if len(self.orders) != len(self.outcomes):
            raise ValueError("one outcome per order required")
        if any(outcome not in {"A", "B", "tie", "inconclusive"} for outcome in self.outcomes):
            raise ValueError("invalid outcome")


def order_effect(selection: SelectionRecord) -> bool:
    return len(set(selection.outcomes)) > 1


def comparison_key(selection: SelectionRecord) -> ComparisonKey:
    return selection.hypothesis_ids, selection.orders, selection.outcomes


def ranking(selection: SelectionRecord) -> Outcome:
    """Preserve unanimous outcomes; conflicting or absent outcomes are inconclusive."""
    if selection.outcomes and not order_effect(selection):
        return selection.outcomes[0]
    return "inconclusive"


@dataclass(frozen=True)
class ComparativeRecord:
    variables: tuple[str, ...]
    ablation: bool
    claim: str
    causal: Literal["uncertain", "attributed"]
    status: Literal["unchallenged", "challenged"]


def comparative(
    variables: Sequence[str], ablation: bool, claim: str = ""
) -> ComparativeRecord:
    causal = "attributed" if len(variables) == 1 and ablation else "uncertain"
    status = "challenged" if not ablation and "caused by " in claim.casefold() else "unchallenged"
    return ComparativeRecord(tuple(variables), ablation, claim, causal, status)


def decide(kind: HypothesisKind, metric_delta: float, infra: bool) -> Literal["repair", "refine", "pivot"]:
    if infra:
        return "repair"
    if metric_delta > 0 and kind == "draft":
        return "refine"
    return "pivot"
