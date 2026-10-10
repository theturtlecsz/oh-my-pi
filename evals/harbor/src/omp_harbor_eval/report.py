"""Harbor trial report: ingest sealed evidence and summarize trial records."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
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


_WILSON_Z = 1.959964
_KNOWN_STATUSES = ("pass", "fail", "harness_defect", "invalid_evidence")
_MD_COLUMNS = (
    "experiment",
    "arm",
    "model",
    "repetitions",
    "undecided",
    "repetitions_final_pass",
    "ac_success",
    "first_pass_rate",
    "cost_per_success",
    "success_per_minute",
)


def _cap_unit(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _rate(successes: int, total: int) -> float | None:
    if total == 0:
        return None
    return _cap_unit(successes / total)


def _wilson(successes: int, total: int) -> tuple[float, float]:
    z = _WILSON_Z
    p = _cap_unit(successes / total)
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    radicand = p * (1.0 - p) / total + z2 / (4.0 * total * total)
    if radicand < 0.0:
        radicand = 0.0
    margin = z * math.sqrt(radicand) / denom
    return _cap_unit(center - margin), _cap_unit(center + margin)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _parse_cost(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("cost_usd must be a decimal string or null")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError("cost_usd is not a decimal") from exc
    if not parsed.is_finite():
        raise ValueError("cost_usd is not a finite decimal")
    return parsed


def _require_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_no}: record is not an object")
        _validate_record(row, path, line_no)
        records.append(row)
    _reject_duplicates(records)
    return records


def _validate_record(row: Mapping[str, Any], path: Path, line_no: int) -> None:
    def fail(message: str) -> ValueError:
        return ValueError(f"{path}:{line_no}: {message}")

    experiment = row.get("experiment")
    model = row.get("model")
    task_id = row.get("task_id")
    arm = row.get("arm")
    status = row.get("status")
    if not isinstance(experiment, str) or not experiment:
        raise fail("experiment must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise fail("model must be a non-empty string")
    if not isinstance(task_id, str) or not task_id:
        raise fail("task_id must be a non-empty string")
    if arm is not None and not isinstance(arm, str):
        raise fail("arm must be a string or null")
    if not isinstance(status, str) or not status:
        raise fail("status must be a non-empty string")
    try:
        _require_int(row, "repetition")
        _require_int(row, "attempt")
    except ValueError as exc:
        raise fail(str(exc)) from exc
    tokens = row.get("tokens")
    if tokens is not None and not isinstance(tokens, dict):
        raise fail("tokens must be an object or null")
    try:
        _parse_cost(row.get("cost_usd"))
    except ValueError as exc:
        raise fail(str(exc)) from exc
    for field in ("turns", "interventions"):
        value = row.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise fail(f"{field} must be an integer or null")
    latency = row.get("latency_s")
    if latency is not None and (isinstance(latency, bool) or not isinstance(latency, (int, float))):
        raise fail("latency_s must be a number or null")


def _reject_duplicates(records: list[dict[str, Any]]) -> None:
    seen: set[tuple[Any, ...]] = set()
    for rec in records:
        key = (
            rec["experiment"],
            rec.get("arm"),
            rec["model"],
            rec["task_id"],
            rec["repetition"],
            rec["attempt"],
        )
        if key in seen:
            experiment, arm, model, task_id, repetition, attempt = key
            sys.stderr.write(
                "duplicate record: "
                f"experiment={experiment} arm={arm} model={model} "
                f"task_id={task_id} repetition={repetition} attempt={attempt}\n"
            )
            raise SystemExit(1)
        seen.add(key)


def _status_outcome(status: str) -> int | None:
    if status == "pass":
        return 1
    if status == "fail":
        return 0
    return None


def _first_outcome(records: list[dict[str, Any]]) -> int | None:
    for rec in records:
        if rec["attempt"] == 1:
            return _status_outcome(rec["status"])
    return None


def _final_outcome(records: list[dict[str, Any]]) -> int | None:
    # The highest pass or fail decides. A later harness_defect or
    # invalid_evidence does not replace that outcome.
    chosen: tuple[int, int] | None = None
    for rec in records:
        outcome = _status_outcome(rec["status"])
        if outcome is None:
            continue
        if chosen is None or rec["attempt"] > chosen[0]:
            chosen = (rec["attempt"], outcome)
    if chosen is None:
        return None
    return chosen[1]


def _task_flag(outcomes: list[int]) -> int | None:
    if not outcomes:
        return None
    if all(outcome == 1 for outcome in outcomes):
        return 1
    return 0


def _status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in _KNOWN_STATUSES}
    for rec in records:
        status = rec["status"]
        counts[status] = counts.get(status, 0) + 1
    extras = sorted(name for name in counts if name not in _KNOWN_STATUSES)
    ordered = {name: counts[name] for name in _KNOWN_STATUSES}
    for name in extras:
        ordered[name] = counts[name]
    return ordered


def _sum_ints(values: list[int]) -> int | None:
    if not values:
        return None
    return sum(values)


def _token_field(records: list[dict[str, Any]], field: str) -> int | None:
    values: list[int] = []
    for rec in records:
        tokens = rec.get("tokens")
        if not isinstance(tokens, dict):
            continue
        value = tokens.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        values.append(value)
    return _sum_ints(values)


def _sum_field_ints(records: list[dict[str, Any]], field: str) -> int | None:
    values: list[int] = []
    for rec in records:
        value = rec.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        values.append(value)
    return _sum_ints(values)


def _sum_latency(records: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for rec in records:
        value = rec.get("latency_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    if not values:
        return None
    return sum(values)


def _sum_cost(records: list[dict[str, Any]]) -> Decimal | None:
    values: list[Decimal] = []
    for rec in records:
        parsed = _parse_cost(rec.get("cost_usd"))
        if parsed is None:
            continue
        values.append(parsed)
    if not values:
        return None
    return sum(values, Decimal(0))


def _derived_rates(
    input_tokens: int | None,
    cost: Decimal | None,
    successes: int,
    latency_s: float | None,
) -> tuple[str | None, float | None]:
    # Both rates stay null when no input tokens were summed.
    if input_tokens is None:
        return None, None
    per_success: str | None = None
    if cost is not None and successes != 0:
        per_success = _decimal_text(cost / Decimal(successes))
    per_minute: float | None = None
    if latency_s is not None and latency_s != 0:
        per_minute = successes / (latency_s / 60.0)
    return per_success, per_minute


def _summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for rec in records:
        buckets.setdefault((rec["task_id"], rec["repetition"]), []).append(rec)
    repetition_keys = sorted(buckets, key=lambda item: (item[0], item[1]))

    by_task: dict[str, list[tuple[int | None, int | None]]] = {}
    final_pass = 0
    final_decidable = 0
    first_pass = 0
    first_decidable = 0
    for task_id, _repetition in repetition_keys:
        reps = buckets[(task_id, _repetition)]
        first = _first_outcome(reps)
        final = _final_outcome(reps)
        by_task.setdefault(task_id, []).append((first, final))
        if final is not None:
            final_decidable += 1
            if final == 1:
                final_pass += 1
        if first is not None:
            first_decidable += 1
            if first == 1:
                first_pass += 1

    tasks: list[dict[str, Any]] = []
    task_first_counted = 0
    task_first_passing = 0
    task_final_counted = 0
    task_final_passing = 0
    for task_id in sorted(by_task):
        outcomes = by_task[task_id]
        first_flag = _task_flag([item[0] for item in outcomes if item[0] is not None])
        final_flag = _task_flag([item[1] for item in outcomes if item[1] is not None])
        tasks.append(
            {
                "task_id": task_id,
                "repetitions": len(outcomes),
                "first_pass": first_flag,
                "final_pass": final_flag,
            }
        )
        if first_flag is not None:
            task_first_counted += 1
            if first_flag == 1:
                task_first_passing += 1
        if final_flag is not None:
            task_final_counted += 1
            if final_flag == 1:
                task_final_passing += 1

    # Null-token records stay out of every sum. `unknown` counts them.
    known = [rec for rec in records if rec.get("tokens") is not None]
    unknown = len(records) - len(known)
    input_tokens = _token_field(known, "input")
    cached_tokens = _token_field(known, "cached")
    output_tokens = _token_field(known, "output")
    cost = _sum_cost(known)
    latency_s = _sum_latency(known)
    ac_success = _rate(final_pass, final_decidable)
    if ac_success is None:
        wilson_low: float | None = None
        wilson_high: float | None = None
    else:
        wilson_low, wilson_high = _wilson(final_pass, final_decidable)
    cost_per_success, success_per_minute = _derived_rates(
        input_tokens,
        cost,
        final_pass,
        latency_s,
    )
    return {
        "records": len(records),
        "status_counts": _status_counts(records),
        "repetitions": len(repetition_keys),
        "undecided": len(repetition_keys) - final_decidable,
        "repetitions_final_pass": final_pass,
        "repetitions_final_decidable": final_decidable,
        "repetitions_first_pass": first_pass,
        "repetitions_first_decidable": first_decidable,
        "ac_success": ac_success,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "first_pass_rate": _rate(first_pass, first_decidable),
        "tasks": tasks,
        "task_rates": {
            "first": {
                "counted": task_first_counted,
                "passing": task_first_passing,
                "rate": _rate(task_first_passing, task_first_counted),
            },
            "final": {
                "counted": task_final_counted,
                "passing": task_final_passing,
                "rate": _rate(task_final_passing, task_final_counted),
            },
        },
        "sums": {
            "input": input_tokens,
            "cached": cached_tokens,
            "output": output_tokens,
            "cost_usd": None if cost is None else _decimal_text(cost),
            "turns": _sum_field_ints(known, "turns"),
            "interventions": _sum_field_ints(known, "interventions"),
            "latency_s": latency_s,
        },
        "unknown": unknown,
        "cost_per_success": cost_per_success,
        "success_per_minute": success_per_minute,
    }


def _sum_optional(values: list[Any]) -> Any:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _totals(groups: list[dict[str, Any]]) -> dict[str, Any]:
    if not groups:
        return _summarize_records([])
    status_keys = list(_KNOWN_STATUSES)
    seen = set(status_keys)
    for group in groups:
        for name in group["status_counts"]:
            if name not in seen:
                status_keys.append(name)
                seen.add(name)
    extras = sorted(name for name in status_keys if name not in _KNOWN_STATUSES)
    ordered_status = [*_KNOWN_STATUSES, *extras]
    status_counts = {
        name: sum(group["status_counts"].get(name, 0) for group in groups)
        for name in ordered_status
    }
    final_pass = sum(group["repetitions_final_pass"] for group in groups)
    final_decidable = sum(group["repetitions_final_decidable"] for group in groups)
    first_pass = sum(group["repetitions_first_pass"] for group in groups)
    first_decidable = sum(group["repetitions_first_decidable"] for group in groups)
    repetitions = sum(group["repetitions"] for group in groups)
    task_first_counted = sum(group["task_rates"]["first"]["counted"] for group in groups)
    task_first_passing = sum(group["task_rates"]["first"]["passing"] for group in groups)
    task_final_counted = sum(group["task_rates"]["final"]["counted"] for group in groups)
    task_final_passing = sum(group["task_rates"]["final"]["passing"] for group in groups)
    tasks: list[dict[str, Any]] = []
    for group in groups:
        for task in group["tasks"]:
            tasks.append(
                {
                    "experiment": group["experiment"],
                    "arm": group["arm"],
                    "model": group["model"],
                    "task_id": task["task_id"],
                    "repetitions": task["repetitions"],
                    "first_pass": task["first_pass"],
                    "final_pass": task["final_pass"],
                }
            )
    input_tokens = _sum_optional([group["sums"]["input"] for group in groups])
    cost_values = [
        Decimal(group["sums"]["cost_usd"])
        for group in groups
        if group["sums"]["cost_usd"] is not None
    ]
    cost = sum(cost_values, Decimal(0)) if cost_values else None
    latency_s = _sum_optional([group["sums"]["latency_s"] for group in groups])
    ac_success = _rate(final_pass, final_decidable)
    if ac_success is None:
        wilson_low: float | None = None
        wilson_high: float | None = None
    else:
        wilson_low, wilson_high = _wilson(final_pass, final_decidable)
    cost_per_success, success_per_minute = _derived_rates(
        input_tokens,
        cost,
        final_pass,
        latency_s,
    )
    return {
        "records": sum(group["records"] for group in groups),
        "status_counts": status_counts,
        "repetitions": repetitions,
        "undecided": sum(group["undecided"] for group in groups),
        "repetitions_final_pass": final_pass,
        "repetitions_final_decidable": final_decidable,
        "repetitions_first_pass": first_pass,
        "repetitions_first_decidable": first_decidable,
        "ac_success": ac_success,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "first_pass_rate": _rate(first_pass, first_decidable),
        "tasks": tasks,
        "task_rates": {
            "first": {
                "counted": task_first_counted,
                "passing": task_first_passing,
                "rate": _rate(task_first_passing, task_first_counted),
            },
            "final": {
                "counted": task_final_counted,
                "passing": task_final_passing,
                "rate": _rate(task_final_passing, task_final_counted),
            },
        },
        "sums": {
            "input": input_tokens,
            "cached": _sum_optional([group["sums"]["cached"] for group in groups]),
            "output": _sum_optional([group["sums"]["output"] for group in groups]),
            "cost_usd": None if cost is None else _decimal_text(cost),
            "turns": _sum_optional([group["sums"]["turns"] for group in groups]),
            "interventions": _sum_optional([group["sums"]["interventions"] for group in groups]),
            "latency_s": latency_s,
        },
        "unknown": sum(group["unknown"] for group in groups),
        "cost_per_success": cost_per_success,
        "success_per_minute": success_per_minute,
    }


def _identity_sort(key: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    parts: list[tuple[int, str]] = []
    for value in key:
        if value is None:
            parts.append((0, ""))
        else:
            parts.append((1, str(value)))
    return tuple(parts)


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = format(value, ".6g")
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _markdown(groups: list[dict[str, Any]]) -> str:
    header = "| " + " | ".join(_MD_COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in _MD_COLUMNS) + " |"
    lines = [header, separator]
    for group in groups:
        cells = [_markdown_cell(group[column]) for column in _MD_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def summarize(
    records: str | Path,
    *,
    out_json: str | Path,
    out_md: str | Path,
) -> dict[str, Any]:
    """Read JSONL trial records and write one summary row per group."""
    loaded = _load_records(Path(records))
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for rec in loaded:
        key = (rec["experiment"], rec.get("arm"), rec["model"])
        grouped.setdefault(key, []).append(rec)
    groups: list[dict[str, Any]] = []
    for key in sorted(grouped, key=_identity_sort):
        experiment, arm, model = key
        row = {"experiment": experiment, "arm": arm, "model": model}
        row.update(_summarize_records(grouped[key]))
        groups.append(row)
    payload = {"groups": groups, "totals": _totals(groups)}
    json_path = Path(out_json)
    md_path = Path(out_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown(groups), encoding="utf-8")
    return payload


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

    summary = sub.add_parser("summarize")
    summary.add_argument("--records", required=True)
    summary.add_argument("--out-json", required=True)
    summary.add_argument("--out-md", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "summarize":
        try:
            summarize(args.records, out_json=args.out_json, out_md=args.out_md)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
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
