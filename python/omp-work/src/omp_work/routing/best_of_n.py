"""Best-of-N selection scored by a harness's ``METRIC`` output (OMP-420)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from omp_work.jobs.research_trial import parse_metric_lines


@dataclass(frozen=True)
class Scored:
    n: int
    text: str
    metrics: dict[str, float]
    score: float


def run_best_of_n(
    candidates: Sequence[str],
    harness: Callable[[str], str],
    *,
    metric: str,
) -> tuple[list[Scored], Scored]:
    """Score every candidate with ``harness`` and return ``(scored, winner)``."""
    scored: list[Scored] = []
    for index, text in enumerate(candidates, start=1):
        metrics = parse_metric_lines(harness(text))
        if metric not in metrics:
            raise ValueError(
                f"candidate {index} {text!r} harness output missing metric {metric!r}"
            )
        scored.append(Scored(n=index, text=text, metrics=metrics, score=metrics[metric]))
    winner = max(scored, key=lambda item: (item.score, -item.n))
    return scored, winner


def fact_coverage_harness(facts: Sequence[str]) -> Callable[[str], str]:
    """Build a harness emitting the share of ``facts`` found verbatim in the text."""
    known = tuple(facts)

    def harness(text: str) -> str:
        if not known:
            return "METRIC fact_coverage=0\n"
        hits = sum(1 for fact in known if fact in text)
        return f"METRIC fact_coverage={hits / len(known)}\n"

    return harness
