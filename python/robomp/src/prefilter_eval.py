"""Evaluation harness for robomp issue prefiltering using Jev typed decisions."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from robomp.config import Settings
from robomp.db import Database
from robomp.jev_client import JevClient
from robomp.prefilter import run_prefilter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PrefilterEvalMetrics:
    total_issues: int = 0
    skip_session_count: int = 0
    skip_session_share: float = 0.0
    confident_bucket_total: int = 0
    confident_bucket_correct: int = 0
    confident_bucket_accuracy: float = 0.0
    overall_accuracy: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    cost_per_1000: float = 0.0
    unparseable_count: int = 0
    unparseable_rate: float = 0.0
    off_list_count: int = 0
    off_list_rate: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("latencies_ms", None)
        return data


def _calculate_percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(math.ceil(percentile * len(sorted_values))) - 1
    idx = max(0, min(idx, len(sorted_values) - 1))
    return float(sorted_values[idx])


def load_issues_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load issue records from a JSONL file."""
    issues: list[dict[str, Any]] = []
    p = Path(path)
    if not p.is_file():
        return issues
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
                if isinstance(item, dict):
                    issues.append(item)
            except json.JSONDecodeError:
                continue
    return issues


async def evaluate_prefilter(
    issues: Sequence[Mapping[str, Any]],
    *,
    client: JevClient | None = None,
    settings: Settings | None = None,
    db: Database | None = None,
    threshold: float = 0.90,
) -> PrefilterEvalMetrics:
    """Evaluate prefilter decisions against ground truth labels."""
    metrics = PrefilterEvalMetrics(
        total_issues=len(issues),
        confident_bucket_accuracy=1.0 if not issues else 0.0,
        overall_accuracy=1.0 if not issues else 0.0,
    )
    if not issues:
        return metrics

    target_db = db or Database(":memory:")
    target_settings = settings or Settings.model_construct(
        github_webhook_secret=SecretStr("test-secret"),
        bot_login="robomp-bot",
        git_author_email="robomp-bot@example.invalid",
        gh_proxy_url="http://gh-proxy.invalid:8081",
        gh_proxy_hmac_key=SecretStr("test-hmac"),
        jev_enabled=True,
        prefilter_enabled=True,
        prefilter_threshold=threshold,
    )

    total_tokens = 0
    for issue in issues:
        repo = issue.get("repo", "")
        number = issue.get("number", 0)
        key = issue.get("key") or f"{repo}#{number}"
        title = issue.get("title", "")
        body = issue.get("body", "")
        ground_truth = issue.get("label") or issue.get("primary_label")

        start = time.monotonic()
        result = await run_prefilter(
            settings=target_settings,
            db=target_db,
            client=client,
            key=key,
            title=title,
            body=body,
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0
        metrics.latencies_ms.append(elapsed_ms)

        state_len = len(f"{title}\n\n{body}" if body else title)
        # Approximate input tokens (4 chars per token roughly) plus question overhead
        tokens = math.ceil(state_len / 4.0) + 120
        total_tokens += tokens

        if result.route == "answered":
            metrics.skip_session_count += 1
            metrics.confident_bucket_total += 1
            if result.label is not None and ground_truth is not None and result.label == ground_truth:
                metrics.confident_bucket_correct += 1

    # Inspect db jev_calls for outcomes if any were recorded
    cursor = target_db._conn.execute("SELECT outcome, latency_ms FROM jev_calls")
    db_calls = cursor.fetchall()
    if db_calls:
        for row in db_calls:
            outcome = row[0]
            if outcome == "malformed":
                metrics.unparseable_count += 1
            elif outcome == "off_list":
                metrics.off_list_count += 1

    total = metrics.total_issues
    if total > 0:
        metrics.skip_session_share = metrics.skip_session_count / total
        call_count = len(db_calls) if db_calls else total
        metrics.unparseable_rate = metrics.unparseable_count / call_count
        metrics.off_list_rate = metrics.off_list_count / call_count

    if metrics.confident_bucket_total > 0:
        metrics.confident_bucket_accuracy = metrics.confident_bucket_correct / metrics.confident_bucket_total
    else:
        # If no issues were answered, confident bucket accuracy is vacuously 1.0
        metrics.confident_bucket_accuracy = 1.0

    # Overall accuracy: answered issues are scored by prefilter accuracy;
    # session issues proceed to full human/session triage which reaches ground truth.
    session_count = total - metrics.skip_session_count
    if total > 0:
        metrics.overall_accuracy = (metrics.confident_bucket_correct + session_count) / total
    else:
        metrics.overall_accuracy = 1.0

    sorted_latencies = sorted(metrics.latencies_ms)
    metrics.p50_latency_ms = _calculate_percentile(sorted_latencies, 0.50)
    metrics.p95_latency_ms = _calculate_percentile(sorted_latencies, 0.95)

    # Cost per 1000 calls: input tokens * $0.042 / 1M tokens
    if total > 0:
        avg_tokens_per_call = total_tokens / total
        metrics.cost_per_1000 = (avg_tokens_per_call * 1000.0 * 0.042) / 1_000_000.0

    return metrics


async def _async_main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate robomp Jev prefilter against issue set")
    parser.add_argument("--issues", help="Path to issues JSONL file")
    parser.add_argument("--db", help="Path to robomp sqlite database")
    parser.add_argument("--threshold", type=float, default=0.90, help="Prefilter threshold (default 0.90)")
    parser.add_argument(
        "--jev-url", default=os.getenv("ROBOMP_JEV_BASE_URL", "https://api.typesafe.ai"), help="Jev base URL"
    )
    parser.add_argument("--api-key", default=os.getenv("TYPESAFE_API_KEY"), help="Typesafe API key")
    parser.add_argument("--out", help="Write results JSON to file")
    parser.add_argument("--json", action="store_true", help="Print results JSON to stdout")

    args = parser.parse_args()

    issues: list[dict[str, Any]] = []
    if args.issues:
        issues = load_issues_jsonl(args.issues)
    elif args.db:
        db = Database(args.db)
        cursor = db._conn.execute("SELECT repo, number, title, body, labels_json FROM issue_index")
        for row in cursor.fetchall():
            try:
                labels = json.loads(row[4]) if row[4] else []
            except Exception:
                labels = []
            issues.append(
                {
                    "key": f"{row[0]}#{row[1]}",
                    "repo": row[0],
                    "number": row[1],
                    "title": row[2],
                    "body": row[3],
                    "labels": labels,
                    "label": labels[0] if labels else None,
                }
            )

    client: JevClient | None = None
    if args.api_key:
        client = JevClient(
            base_url=args.jev_url,
            api_key=SecretStr(args.api_key),
        )

    metrics = await evaluate_prefilter(
        issues,
        client=client,
        threshold=args.threshold,
    )

    data = metrics.to_dict()
    output_json = json.dumps(data, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_json, encoding="utf-8")

    if args.json or not args.out:
        print(output_json)


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
