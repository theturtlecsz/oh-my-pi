"""Pure Gate 3 counter following docs/qualification-counting-rules.md.

Consumes internal, verified read projections only. No payload-key inference,
backend qualification, freeze creation, persistence or scientific acceptance.
"""
from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime

from omp_work.qualification_records import (
    ExecutionAttempt, Exclusion, FrozenProtocol, Report, ScopeEvent, Snapshot,
    frozen_run_boundary, frozen_run_missing_proof, scope_boundary, unavailable_report,
)

DIMENSIONS = ("repository", "task-family", "independent-campaign", "recovery", "cancellation")
GATE = "within-run/100-execution-attempt"


@dataclass(frozen=True)
class StratumCoverage:
    dimension: str
    value: str | None
    status: str
    allocation: int | None
    proof_record_ids: tuple[str, ...]
    execution_attempt_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class AttemptGateReport(Report):
    target: int = 100
    execution_attempt_ids: tuple[str, ...] = ()
    queued_record_ids: tuple[str, ...] = ()
    queued_job_ids: tuple[str, ...] = ()
    queued_count: int = 0
    execution_outcomes: tuple[tuple[str, int], ...] = ()
    scientific_outcomes: tuple[tuple[str | None, int], ...] = ()
    coverage: tuple[StratumCoverage, ...] = ()
    coverage_met: bool = False
    target_reached: bool = False
    retained_candidates: tuple[ExecutionAttempt, ...] = ()
    scope_end: datetime | None = None
    scope_endings: tuple[Exclusion, ...] = ()
    protocol: FrozenProtocol | None = None


def attempt_gate_report(snapshot: Snapshot) -> AttemptGateReport:
    """Count starts, not successes, leases, compute jobs or worker registrations.

    Repeated delivery retains its evidence but cannot create a second execution.
    All candidate resources (including exclusions and unknowns) remain intact;
    do not sum delivery snapshots as if they were independent consumption.
    """
    protocol = snapshot.protocol
    missing = frozen_run_missing_proof(protocol) if protocol else ()
    source_ids = {
        record.record_id for record in (*snapshot.receipts, *snapshot.observations)
    } | set(snapshot.acceptance_record_ids)
    if protocol and not set(protocol.proof_record_ids) <= source_ids:
        missing += ("unresolved freeze evidence",)
    if protocol is None or missing:
        base = unavailable_report(snapshot, GATE)
        return AttemptGateReport(
            **{field.name: getattr(base, field.name) for field in fields(Report)
               if field.name != "missing_proof"},
            missing_proof=missing or base.missing_proof,
            retained_candidates=snapshot.attempts, protocol=protocol,
            coverage=tuple(_missing_stratum(dimension) for dimension in DIMENSIONS),
        )

    # Scope change ends this run even if later records revert to its old identity.
    events = (*snapshot.scope_events, *(ScopeEvent(
        row.record_id, row.protocol_sha256, row.started_at or row.recorded_at, row.scope,
    ) for row in snapshot.attempts))
    drift = tuple(event for event in events if (
        event.protocol_sha256 == protocol.protocol_sha256
        and protocol.run_start <= event.observed_at < protocol.run_end
        and scope_boundary(protocol.scope, event.scope)
    ))
    scope_end = min((event.observed_at for event in drift), default=protocol.run_end)
    endings = tuple(Exclusion(event.record_id, "frozen scope drift: " + ", ".join(
        scope_boundary(protocol.scope, event.scope),
    )) for event in drift if event.observed_at == scope_end)

    exclusions = list(snapshot.exclusions)
    counted: list[ExecutionAttempt] = []
    seen: set[str] = set()
    seen_starts: set[str] = set()
    queued: list[ExecutionAttempt] = []
    for row in sorted(snapshot.attempts, key=lambda row: (
        row.started_at or row.recorded_at, row.record_id,
    )):
        reason = frozen_run_boundary(
            protocol, row.protocol_sha256, row.started_at or row.recorded_at, row.scope,
        )
        if reason is None and (row.started_at or row.recorded_at) >= scope_end:
            reason = "frozen run ended at scope drift"
        if (reason is None and row.started_at is None and row.execution_outcome == "queued"
                and row.job_id and row.job_evidence_id in source_ids):
            queued.append(row)
        if reason is None:
            reason = _execution_exclusion(row, protocol, source_ids)
        if reason is None and row.started_at is None:
            reason = "queued but never started"
        if reason is None and row.execution_attempt_id in seen:
            reason = "duplicate delivery, polling or reconciliation of execution attempt"
        if reason is None and row.start_evidence_id in seen_starts:
            reason = "same start evidence is not a genuine retry"
        if reason:
            exclusions.append(Exclusion(row.record_id, reason))
            continue
        # _execution_exclusion has required a stable execution and distinct start.
        assert row.execution_attempt_id is not None and row.start_evidence_id is not None
        seen.add(row.execution_attempt_id)
        seen_starts.add(row.start_evidence_id)
        counted.append(row)

    coverage: list[StratumCoverage] = []
    for dimension in DIMENSIONS:
        strata = [stratum for stratum in protocol.strata if stratum.dimension == dimension]
        if not strata:
            coverage.append(_missing_stratum(dimension))
        for stratum in strata:
            registered = bool(stratum.proof_record_ids) and set(stratum.proof_record_ids) <= source_ids
            matches = [(row, evidence) for row in counted for evidence in row.strata
                       if evidence.dimension == dimension and evidence.value == stratum.value
                       and evidence.evidence_ids and set(evidence.evidence_ids) <= source_ids]
            attempt_ids = tuple(dict.fromkeys(row.execution_attempt_id for row, _ in matches))
            coverage.append(StratumCoverage(
                dimension, stratum.value, "preregistered" if registered else "not preregistered",
                stratum.allocation if registered else None, stratum.proof_record_ids,
                attempt_ids if registered else (),
                tuple(dict.fromkeys(eid for _, evidence in matches for eid in evidence.evidence_ids))
                if registered else (),
            ))
    registered = all(row.status == "preregistered" for row in coverage)
    coverage_met = registered and all(
        row.allocation is not None and len(row.execution_attempt_ids) >= row.allocation
        for row in coverage
    )
    contributing = list(protocol.proof_record_ids)
    for row in counted:
        contributing.extend((row.record_id, row.trial_evidence_id, row.job_evidence_id, row.start_evidence_id))
        contributing.extend(eid for resource in row.resources for eid in resource.evidence_ids)
        for declaration in protocol.experiments:
            if declaration.experiment_id == row.experiment_id:
                contributing.extend(declaration.proof_record_ids)
        for backend in protocol.backends:
            if backend.backend_id == row.backend_id:
                contributing.extend(backend.proof_record_ids)
    for stratum in coverage:
        if stratum.status == "preregistered":
            contributing.extend(stratum.proof_record_ids)
            contributing.extend(stratum.evidence_ids)
    queued_jobs = tuple(dict.fromkeys(row.job_id for row in queued))
    return AttemptGateReport(
        GATE, "available", len(counted), tuple(dict.fromkeys(contributing)), tuple(exclusions),
        "preregistered" if registered else "not preregistered", protocol.allocations if registered else (),
        tuple(f"preregistered stratum: {row.dimension}" for row in coverage
              if row.status != "preregistered"),
        execution_attempt_ids=tuple(row.execution_attempt_id for row in counted),
        queued_record_ids=tuple(row.record_id for row in queued), queued_job_ids=queued_jobs,
        queued_count=len(queued_jobs),
        execution_outcomes=tuple(Counter(row.execution_outcome for row in counted).items()),
        scientific_outcomes=tuple(Counter(row.scientific_outcome for row in counted).items()),
        coverage=tuple(coverage), coverage_met=coverage_met,
        target_reached=len(counted) >= 100 and coverage_met,
        retained_candidates=snapshot.attempts, scope_end=scope_end, scope_endings=endings,
        protocol=protocol,
    )


def _missing_stratum(dimension: str) -> StratumCoverage:
    return StratumCoverage(dimension, None, "not preregistered", None, (), (), ())


def _execution_exclusion(
    row: ExecutionAttempt, protocol: FrozenProtocol, source_ids: set[str],
) -> str | None:
    if row.replacement_for:
        return "attempt swapped in after run start for " + row.replacement_for
    if row.kind != "experiment-execution":
        return "generic research job or non-execution activity"
    declaration = next((item for item in protocol.experiments if (
        item.experiment_id == row.experiment_id and item.trial_id == row.trial_id
        and item.task_id == row.task_id and item.task_id in protocol.task_order
        and item.proof_record_ids and set(item.proof_record_ids) <= source_ids
    )), None)
    if declaration is None:
        return "experiment/trial/task not declared in frozen run"
    if not row.job_id or not row.trial_evidence_id or not row.job_evidence_id or not {
        row.trial_evidence_id, row.job_evidence_id,
    } <= source_ids:
        return "missing linked trial/job evidence"
    if not any(backend.backend_id == row.backend_id
               and row.experiment_id in backend.experiment_ids
               and backend.proof_record_ids
               and set(backend.proof_record_ids) <= source_ids
               and set(backend.proof_record_ids) <= set(protocol.qualified_backend_proof_ids)
               for backend in protocol.backends):
        return "backend unavailable, unqualified or not applicable to experiment"
    if row.started_at is None:
        if row.execution_outcome == "queued" and row.start_evidence_id is None:
            return None
        return "missing actual start evidence"
    if not row.execution_attempt_id:
        return "missing stable execution-attempt id"
    if not row.start_evidence_id or row.start_evidence_id not in source_ids:
        return "missing actual start evidence"
    return None
