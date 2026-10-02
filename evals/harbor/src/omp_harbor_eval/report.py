"""Harbor trial report: ingest sealed evidence into trial records."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
from typing import Any

from .evidence import EvidenceError, load_evidence
from .fixtures import Fixture
from .grader import _resolve_fixture, grade
from .run_plan import model_price, usd


def ingest(
    evidence_dir: str | Path,
    manifest_sha256: str,
    fixture_arg: str | Path | Fixture,
    *,
    run_id: str,
    nonce: str,
    experiment: str,
    model: str,
    attempt: int,
    repetition: int = 1,
    catalog: Mapping[str, Any] | str | Path | None = None,
    out: str | Path,
    fixtures_root: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest one sealed graded trial and append a record to out."""
    if "/" in model:
        provider, model_id = model.split("/", 1)
    else:
        provider, model_id = "", model

    try:
        price = model_price(catalog, provider, model_id)
    except Exception as exc:
        sys.stderr.write(f"unpriced or unknown model {model}: {exc}\n")
        raise SystemExit(1)

    if isinstance(fixture_arg, Fixture):
        fixture = fixture_arg
    else:
        fixture = _resolve_fixture(str(fixture_arg), str(fixtures_root) if fixtures_root else None)

    grade_result = grade(
        evidence_dir,
        manifest_sha256,
        fixture,
        run_id=run_id,
        nonce=nonce,
    )
    grade_status = grade_result["status"]

    try:
        evidence = load_evidence(evidence_dir, manifest_sha256)
    except EvidenceError:
        evidence = None

    plan_id: str | None = None
    arm: str | None = None
    status: str = "invalid_evidence"

    if evidence is not None and "arm.json" in evidence.files:
        try:
            arm_doc = evidence.read_json("arm.json")
        except EvidenceError:
            arm_doc = None

        if isinstance(arm_doc, dict):
            raw_plan_id = arm_doc.get("plan_id")
            raw_arm = arm_doc.get("arm")
            if (
                isinstance(raw_plan_id, str)
                and raw_plan_id
                and isinstance(raw_arm, str)
                and raw_arm
            ):
                plan_id = raw_plan_id
                arm = raw_arm
                status = grade_status

    inbound_rows: list[dict[str, Any]] = []
    if evidence is not None and "rpc-transcript.jsonl" in evidence.files:
        try:
            for row in evidence.read_jsonl("rpc-transcript.jsonl"):
                if isinstance(row, dict) and row.get("direction") == "in":
                    inbound_rows.append(row)
        except EvidenceError:
            inbound_rows = []

    turns = 0
    for row in inbound_rows:
        frame = row.get("frame")
        if isinstance(frame, dict) and frame.get("type") == "turn_start":
            turns += 1

    cost_basis = "exact" if price.exact else "upper_bound"
    tokens: dict[str, int] | None = None
    cost_usd: str | None = None

    last_agent_end_frame: dict[str, Any] | None = None
    for row in inbound_rows:
        frame = row.get("frame")
        if isinstance(frame, dict) and frame.get("type") == "agent_end":
            last_agent_end_frame = frame

    if last_agent_end_frame is not None:
        telemetry = last_agent_end_frame.get("telemetry")
        if isinstance(telemetry, dict):
            usage = telemetry.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("inputTokens")
                out_tok = usage.get("outputTokens")
                if (
                    isinstance(inp, int)
                    and not isinstance(inp, bool)
                    and isinstance(out_tok, int)
                    and not isinstance(out_tok, bool)
                ):
                    cached = usage.get("cachedInputTokens", 0)
                    cached_int = int(cached) if (isinstance(cached, int) and not isinstance(cached, bool)) else 0
                    tokens = {
                        "input": inp,
                        "cached": cached_int,
                        "output": out_tok,
                    }
                    cost_usd = str(usd(price, inp, cached_int, out_tok))

    interventions: int | None = None
    if evidence is not None and "ui-answers.jsonl" in evidence.files:
        try:
            interventions = len(evidence.read_jsonl("ui-answers.jsonl"))
        except EvidenceError:
            interventions = None

    timestamps: list[float] = []
    for row in inbound_rows:
        frame = row.get("frame")
        if isinstance(frame, dict) and frame.get("type") in ("message_start", "message_end"):
            msg = frame.get("message")
            if isinstance(msg, dict) and "timestamp" in msg:
                ts = msg["timestamp"]
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    timestamps.append(float(ts))

    latency_s: float | None = None
    if len(timestamps) >= 2:
        span_s = (max(timestamps) - min(timestamps)) / 1000.0
        if span_s >= 2.0:
            latency_s = span_s

    record: dict[str, Any] = {
        "experiment": experiment,
        "plan_id": plan_id,
        "arm": arm,
        "model": model,
        "task_id": fixture.id,
        "repetition": repetition,
        "attempt": attempt,
        "status": status,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "cost_basis": cost_basis,
        "turns": turns,
        "interventions": interventions,
        "latency_s": latency_s,
    }

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.report")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--evidence", required=True)
    ingest_parser.add_argument("--manifest-sha256", required=True)
    ingest_parser.add_argument("--fixture", required=True)
    ingest_parser.add_argument("--fixtures-root")
    ingest_parser.add_argument("--run-id", required=True)
    ingest_parser.add_argument("--nonce", required=True)
    ingest_parser.add_argument("--experiment", required=True)
    ingest_parser.add_argument("--model", required=True)
    ingest_parser.add_argument("--attempt", type=int, required=True)
    ingest_parser.add_argument("--repetition", type=int, default=1)
    ingest_parser.add_argument("--catalog", default=None)
    ingest_parser.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        try:
            ingest(
                args.evidence,
                args.manifest_sha256,
                args.fixture,
                fixtures_root=args.fixtures_root,
                run_id=args.run_id,
                nonce=args.nonce,
                experiment=args.experiment,
                model=args.model,
                attempt=args.attempt,
                repetition=args.repetition,
                catalog=args.catalog,
                out=args.out,
            )
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
