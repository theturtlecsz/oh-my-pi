"""Held-out false-support and abstain report for the intake support judge (OMP-604-s07).

Scores only the held-out split. ``judge_link`` receives ``judge_input`` fields
only; gold is read after each call, to score. The report is refused, before any
``complete`` call, when the corpus seal or the tuning pin does not verify or when
no held-out pair has a non-supports gold. Abstentions never count as support.
The abstain rate is over all held-out pairs. No enforcement threshold is
reported: that is an owner decision.

CLI: ``python -m omp_work.intake_support_eval --split heldout --max-usd N --out PATH``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from omp_work import intake_support_judge as judge
from omp_work.intake_support_corpus import Pair, judge_input, load_pairs
from omp_work.intake_support_judge import Complete, Completion, IntakeJudgeRefusal
from omp_work.routing.policy import load_policy
from omp_work.routing.router import route_stage

Z = 1.96
DENOMINATOR_ZERO = "denominator_zero"


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval for ``k`` of ``n``; n must be positive."""
    center = (k + z * z / 2) / (n + z * z)
    half = z * math.sqrt(k * (n - k) / n + z * z / 4) / (n + z * z)
    return max(0.0, center - half), min(1.0, center + half)


class _Usage:
    """Sums settled token counts; one unknown call makes both totals unknown."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown = False

    def settle(self, completion: Completion) -> None:
        if completion.input_tokens is None or completion.output_tokens is None:
            self.unknown = True
        else:
            self.input_tokens += completion.input_tokens
            self.output_tokens += completion.output_tokens
        return None


def build_report(
    *, split: str, max_usd: Decimal | int | float, complete: Complete
) -> dict[str, object]:
    """Judge every pair of ``split`` and return the report dict."""
    if split != "heldout":
        raise ValueError("only the heldout split is scored")
    judge._sealed_pin()  # PIN_MISMATCH before any pair is loaded or judged
    base = judge.corpus_dir()
    try:
        pairs = load_pairs(base / "inputs", base / "manifest.json")
    except ValueError as exc:
        raise IntakeJudgeRefusal(judge.PIN_MISMATCH) from exc
    rows: list[Pair] = [p for p in pairs if p.split == split]
    negatives = [p for p in rows if p.gold != "supports"]
    if not negatives:
        raise IntakeJudgeRefusal(DENOMINATOR_ZERO)
    model_id = route_stage(load_policy(), "research").model
    usage = _Usage()
    false_supports = 0
    abstains = 0
    for pair in rows:
        view = judge_input(pair)
        receipt = judge.judge_link(
            claim_text=view["claim_text"],
            passage=view["passage"],
            context=view["context"],
            model_id=model_id,
            remaining_usd=max_usd,
            complete=complete,
            settle=usage.settle,
        )
        abstains += receipt.verdict == "abstain"
        false_supports += receipt.verdict == "supports" and pair.gold != "supports"
    n_neg = len(negatives)
    fs_low, fs_high = wilson(false_supports, n_neg)
    ab_low, ab_high = wilson(abstains, len(rows))
    return {
        "split": split,
        "model_id": model_id,
        "n_negative": n_neg,
        "false_supports": false_supports,
        "false_support_rate": false_supports / n_neg,
        "wilson_low": fs_low,
        "wilson_high": fs_high,
        "n_pairs": len(rows),
        "abstains": abstains,
        "abstain_rate": abstains / len(rows),
        "abstain_wilson_low": ab_low,
        "abstain_wilson_high": ab_high,
        "input_tokens": None if usage.unknown else usage.input_tokens,
        "output_tokens": None if usage.unknown else usage.output_tokens,
    }


def _positive_usd(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("not a number") from exc
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def main(argv: list[str] | None = None, *, complete: Complete = judge.complete_from_env) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_work.intake_support_eval")
    parser.add_argument("--split", choices=["heldout"], required=True)
    parser.add_argument("--max-usd", type=_positive_usd, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(split=args.split, max_usd=args.max_usd, complete=complete)
    except IntakeJudgeRefusal as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
