"""Deterministic three-gate counter report as plain JSON-ready data.

Pure projection of ``ordinary_gate_reports`` and ``attempt_gate_report``. It
reads nothing, writes nothing and decides nothing: counts grant no acceptance
or qualification, and no target allocation is invented.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta

from omp_work.qualification_attempts import AttemptGateReport, attempt_gate_report
from omp_work.qualification_ordinary import (
    DISTINCT_TASKS,
    STREAK_LENGTH,
    SUSTAINED_DURATION,
    OrdinaryGateReport,
    ordinary_gate_reports,
)
from omp_work.qualification_records import (
    Exclusion,
    FrozenProtocol,
    Gate,
    Observation,
    Receipt,
    Snapshot,
)


def build_report(snapshot: Snapshot) -> dict[str, object]:
    ordinary = ordinary_gate_reports(snapshot)
    attempts = attempt_gate_report(snapshot)
    observations = _observations(snapshot, ordinary.observations)
    consecutive = _ordinary_gate(ordinary.consecutive, snapshot, observations)
    consecutive["target"] = {"accepted_trials": STREAK_LENGTH}
    sustained = _ordinary_gate(ordinary.sustained, snapshot, observations)
    sustained["target"] = {
        "duration_seconds": SUSTAINED_DURATION.total_seconds(),
        "distinct_tasks": DISTINCT_TASKS,
    }
    return {
        "grants_acceptance": False,
        "frozen": _frozen(snapshot.protocol),
        "gates": {
            "consecutive": consecutive,
            "sustained": sustained,
            "attempts": _attempt_gate(attempts, snapshot),
        },
        "observations": list(observations.values()),
    }


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _frozen(protocol: FrozenProtocol | None) -> dict[str, object] | None:
    if protocol is None:
        return None
    return {
        "protocol_sha256": protocol.protocol_sha256,
        "release_baseline_sha256": protocol.release_baseline_sha256,
        "scope": _plain(protocol.scope),
        "run_start": protocol.run_start.isoformat(),
        "run_end": protocol.run_end.isoformat(),
        "run_duration_seconds": (protocol.run_end - protocol.run_start).total_seconds(),
        "task_order": list(protocol.task_order),
        "proof_record_ids": list(protocol.proof_record_ids),
        "qualified_backend_proof_ids": list(protocol.qualified_backend_proof_ids),
    }


def _exclusions(reported: Iterable[Exclusion], snapshot: Snapshot) -> list[dict[str, str]]:
    """Report order first, then snapshot exclusions the gate path dropped."""
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for item in (*reported, *snapshot.exclusions):
        merged.setdefault(
            (item.record_id, item.reason), {"record_id": item.record_id, "reason": item.reason},
        )
    return list(merged.values())


def _strata(status: str, allocations: tuple[tuple[str, int], ...]) -> dict[str, object]:
    return {
        "status": status,
        "allocations": [{"name": name, "allocation": value} for name, value in allocations],
    }


def _observations(snapshot: Snapshot, shared: Iterable[object]) -> dict[str, dict[str, object]]:
    sources: dict[str, Receipt | Observation] = {
        record.record_id: record for record in (*snapshot.receipts, *snapshot.observations)
    }
    merged: dict[str, dict[str, object]] = {}
    for item in shared:
        entry = merged.get(item.record_id)  # type: ignore[attr-defined]
        if entry is None:
            entry = merged[item.record_id] = {  # type: ignore[attr-defined]
                "record_id": item.record_id, "task_ids": [], "uses": [], "attachments": [],
                "source": _source(sources.get(item.record_id)),  # type: ignore[attr-defined]
            }
        _extend(entry["task_ids"], [item.task_id])  # type: ignore[attr-defined]
        _extend(entry["uses"], list(item.uses))  # type: ignore[attr-defined]
        _extend(entry["attachments"], [_plain(a) for a in item.attachments])  # type: ignore[attr-defined]
    return merged


def _extend(target: object, items: list[object]) -> None:
    assert isinstance(target, list)
    for item in items:
        if item not in target:
            target.append(item)


def _source(record: Receipt | Observation | None) -> dict[str, object] | None:
    if record is None:
        return None
    data = _plain(record)
    assert isinstance(data, dict)
    data["payload"] = json.loads(str(data.pop("payload_json")))
    data["kind"] = "observation" if isinstance(record, Observation) else "receipt"
    return data


def _uses(observations: dict[str, dict[str, object]], gate: Gate) -> list[str]:
    return [
        record_id for record_id, entry in observations.items()
        if gate in entry["uses"]  # type: ignore[operator]
    ]


def _ordinary_gate(
    report: OrdinaryGateReport, snapshot: Snapshot, observations: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        "gate": report.gate,
        "status": report.status,
        "count": report.count,
        "total_instances": report.total_instances,
        "outcomes": _plain(report.outcomes),
        "categories": _plain(report.categories),
        "contributing_record_ids": list(report.contributing_source_ids),
        "observation_ids": _uses(observations, report.gate),
        "exclusions": _exclusions(report.exclusions, snapshot),
        "missing_proof": list(report.missing_proof),
        "resets": _plain(report.resets),
        "scope_end": _plain(report.scope_end),
        "elapsed_seconds": _plain(report.elapsed),
        "duration_met": report.duration_met,
        "coverage": _plain(report.coverage),
        "run_failures": list(report.run_failures),
        "unresolved_failure_ids": list(report.unresolved_failure_ids),
        "unplanned_repairs": list(report.unplanned_repairs),
        "release_qualified": report.release_qualified,
        "strata": _strata(report.strata_status, report.allocations),
    }


def _counts(items: Iterable[tuple[str | None, int]]) -> list[dict[str, object]]:
    return [
        {"outcome": outcome, "count": count}
        for outcome, count in sorted(items, key=lambda item: (item[0] is None, item[0] or ""))
    ]


def _attempt_gate(report: AttemptGateReport, snapshot: Snapshot) -> dict[str, object]:
    return {
        "gate": report.gate,
        "status": report.status,
        "count": report.count,
        "target": {"execution_attempts": report.target},
        "contributing_record_ids": list(report.contributing_record_ids),
        "exclusions": _exclusions(report.exclusions, snapshot),
        "missing_proof": list(report.missing_proof),
        "strata": _strata(report.strata_status, report.allocations),
        "execution_attempt_ids": list(report.execution_attempt_ids),
        "queued": {
            "record_ids": list(report.queued_record_ids),
            "job_ids": list(report.queued_job_ids),
            "count": report.queued_count,
        },
        "execution_outcomes": _counts(report.execution_outcomes),
        "scientific_outcomes": _counts(report.scientific_outcomes),
        "coverage": _plain(report.coverage),
        "coverage_met": report.coverage_met,
        "target_reached": report.target_reached,
        "scope_end": _plain(report.scope_end),
        "scope_endings": _plain(report.scope_endings),
        "retained_candidates": sorted(
            (_plain(row) for row in report.retained_candidates),  # type: ignore[misc]
            key=lambda row: row["record_id"],
        ),
    }
