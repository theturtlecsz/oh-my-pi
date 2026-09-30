"""Owner-slice harness: measure the R2 Jev claim-support classifier.

Runs one Jev choice call and one chat-classifier call per hand-labeled
(claim, passage) pair, then writes the measurement report. This is the owner
slice: it needs the owner's ``TYPESAFE_API_KEY`` and a comparison chat model, so
it is run on the owner's machine, never in the implementation worktree. The
implementer slice over a stub Jev server is
``python/omp-work/tests/test_report_evidence_jev.py``.

Usage::

    TYPESAFE_API_KEY=... JEV_CLAIM_SUPPORT_CHAT_CMD='...' \
    uv run --project python/omp-work python docs/reports/jev-claim-support/run.py \
      --labels ~/.omp/jev-claim-support-labels.jsonl \
      --out docs/reports/jev-claim-support-measurement-report.md \
      --pairs-per-report 24

``JEV_CLAIM_SUPPORT_CHAT_CMD`` reads the rendered classifier prompt on stdin and
writes ``{"text": ..., "input_tokens": ..., "output_tokens": ..., "model": ...}``
on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from omp_work.report_evidence import (
    ChatModelClassifier,
    Claim,
    Completion,
    JevClassifier,
    Passage,
)

# §5 review's Jev primitive table: input $0.042/MTok, output free.
JEV_INPUT_USD_PER_MTOK = 0.042

LABELS = ("supports", "contradicts", "neither")


@dataclass(frozen=True)
class LabeledPair:
    claim: Claim
    passage: Passage
    label: str


@dataclass(frozen=True)
class LabelScore:
    precision: float
    recall: float


@dataclass(frozen=True)
class Score:
    accuracy: float
    per_label: dict[str, LabelScore]


def load_labels(path: Path) -> list[LabeledPair]:
    """Read the owner's JSONL hand labels; one object per pair."""
    pairs: list[LabeledPair] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        label = item["label"]
        if label not in LABELS:
            raise ValueError(f"line {line_number}: off-list label {label!r}")
        index = len(pairs) + 1
        pairs.append(
            LabeledPair(
                claim=Claim(
                    id=f"c-{index}",
                    text=item["claim"],
                    char_start=0,
                    char_end=0,
                    material=True,
                ),
                passage=Passage(
                    passage_id=f"p-{index}",
                    source_id=item.get("source_id", "s"),
                    locator=item.get("locator", ""),
                    text=item["passage"],
                ),
                label=label,
            )
        )
    if not pairs:
        raise ValueError("no hand-labeled pairs found")
    return pairs


def chat_completer(command: str):
    """A ``complete(prompt) -> Completion`` backed by an external command."""

    def complete(prompt: str) -> Completion:
        result = subprocess.run(
            command,
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"chat command exited {result.returncode}")
        payload = json.loads(result.stdout)
        return Completion(
            text=payload["text"],
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            model=payload.get("model", "chat-model"),
        )

    return complete


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]


def _score(pairs: list[LabeledPair], labels: list[str]) -> Score:
    """Accuracy and per-label precision/recall against the hand labels."""
    correct = sum(1 for pair, label in zip(pairs, labels) if pair.label == label)
    per_label: dict[str, LabelScore] = {}
    for label in LABELS:
        tp = sum(1 for p, got in zip(pairs, labels) if p.label == label and got == label)
        fp = sum(1 for p, got in zip(pairs, labels) if p.label != label and got == label)
        fn = sum(1 for p, got in zip(pairs, labels) if p.label == label and got != label)
        per_label[label] = LabelScore(
            precision=tp / (tp + fp) if tp + fp else 0.0,
            recall=tp / (tp + fn) if tp + fn else 0.0,
        )
    return Score(
        accuracy=correct / len(pairs) if pairs else 0.0,
        per_label=per_label,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pairs-per-report", type=int, default=24)
    parser.add_argument("--base-url", default="https://api.typesafe.ai")
    parser.add_argument("--chat-cmd", default=os.environ.get("JEV_CLAIM_SUPPORT_CHAT_CMD"))
    args = parser.parse_args(argv)

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is not set", file=sys.stderr)
        return 2
    if not args.chat_cmd:
        print("JEV_CLAIM_SUPPORT_CHAT_CMD is not set", file=sys.stderr)
        return 2

    pairs = load_labels(args.labels)
    claim_passages = [(pair.claim, pair.passage) for pair in pairs]
    hand = [pair.label for pair in pairs]

    failures = 0
    latencies: list[float] = []

    # Jev side: one choice call per pair (batch_size=1), for per-pair latency.
    jev_labels: list[str] = []
    jev = JevClassifier(api_key=api_key, base_url=args.base_url, batch_size=1)
    for pair in pairs:
        started = time.perf_counter()
        try:
            (result,) = jev.classify([(pair.claim, pair.passage)])
            jev_labels.append(result.label)
        except Exception:  # noqa: BLE001 - a failed pair is reported, not retried
            jev_labels.append("error")
            failures += 1
        latencies.append((time.perf_counter() - started) * 1000)

    # Chat side: the shipped batched classifier over the same pairs.
    chat_labels: list[str] = []
    chat = ChatModelClassifier(complete=chat_completer(args.chat_cmd), batch_size=8)
    try:
        chat_labels = [c.label for c in chat.classify(claim_passages)]
    except Exception:  # noqa: BLE001
        chat_labels = ["error"] * len(pairs)

    jev_score = _score(pairs, jev_labels)
    chat_score = _score(pairs, chat_labels)
    agreement = (
        sum(1 for a, b in zip(jev_labels, chat_labels) if a == b) / len(pairs)
        if pairs
        else 0.0
    )

    jev_input_tokens = sum(u.input_tokens for u in jev.usages)
    cost_per_pair = (jev_input_tokens / len(pairs)) * JEV_INPUT_USD_PER_MTOK / 1e6

    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    lines = [
        "# R2-J Jev claim-support classifier — measurement",
        "",
        f"Hand-labeled pairs: {len(pairs)} · failures: {failures}",
        "",
        "| Metric | Chat classifier | Jev |",
        "| --- | --- | --- |",
        f"| Hand-label accuracy | {pct(chat_score.accuracy)} | {pct(jev_score.accuracy)} |",
    ]
    for label in LABELS:
        chat_label = chat_score.per_label[label]
        jev_label = jev_score.per_label[label]
        lines.append(
            f"| {label} precision / recall | "
            f"{pct(chat_label.precision)} / {pct(chat_label.recall)} | "
            f"{pct(jev_label.precision)} / {pct(jev_label.recall)} |"
        )
    lines += [
        f"| Pairwise agreement (Jev vs chat) | — | {pct(agreement)} |",
        f"| Input tokens per pair | — | {jev_input_tokens / len(pairs):.1f} |",
        f"| p50 / p95 latency (ms) | — | {percentile(latencies, 0.5):.0f} / {percentile(latencies, 0.95):.0f} |",
        f"| Cost per pair ($) | — | {cost_per_pair:.8f} |",
        f"| Cost per report ($, {args.pairs_per_report} pairs) | — | {cost_per_pair * args.pairs_per_report:.6f} |",
        f"| Transport / parse failure share | — | {pct(failures / len(pairs))} |",
        "",
        "Probabilities are routing hints only: scored labels are the choice argmax, "
        "and no probability enters the report body or a decision.",
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
