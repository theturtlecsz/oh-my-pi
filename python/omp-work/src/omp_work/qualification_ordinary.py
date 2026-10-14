"""Pure ordinary-work gate reports for the 20-trial streak and the 72-hour run.

The input is an already projected snapshot. This module does not decode ledger
payloads, freeze a protocol, start a trial, or write anything. Counting rules
are the r2 quotations in ``docs/qualification-counting-rules.md``.

A missing acceptance-bound freeze or release baseline leaves both gates not
started, with null counts. Empty preregistered allocations stay empty and are
reported as not preregistered. Measured counts never qualify a release.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from omp_work.qualification_records import (
    Exclusion,
    FrozenProtocol,
    Gate,
    ScopeIdentity,
    Snapshot,
    scope_boundary,
)

CONSECUTIVE_GATE: Gate = "20-consecutive-accepted"
SUSTAINED_GATE: Gate = "72h/30-distinct-task"
SHARED_USES: tuple[Gate, ...] = (CONSECUTIVE_GATE, SUSTAINED_GATE)

STREAK_LENGTH = 20
DISTINCT_TASKS = 30
SUSTAINED_DURATION = timedelta(hours=72)
REPOSITORIES: tuple[str, ...] = ("oh-my-pi", "media-discovery")

# r2 resets the streak for these identity changes. Configuration, workload, and
# acceptance criteria end the same scope but are not streak-reset causes.
STREAK_RESET_FIELDS = frozenset({"recipe", "release", "evaluator"})
SCOPE_FIELDS = ("release", "configuration", "recipe", "evaluator", "workload", "criteria")

OrdinaryOutcome = Literal["accepted", "failed", "blocked", "cancelled", "incomplete"]
OUTCOMES: tuple[OrdinaryOutcome, ...] = (
    "accepted", "failed", "blocked", "cancelled", "incomplete",
)
AttachmentKind = Literal["retry", "review_fix", "resumption"]
Integrity = Literal[
    "lost accepted record",
    "duplicate effect",
    "unauthorized admission",
    "false acceptance",
]
Category = Literal[
    "negative science",
    "candidate debugging",
    "infrastructure failure",
    "planned recovery",
    "planned fault injection",
]
CATEGORIES: tuple[Category, ...] = (
    "negative science",
    "candidate debugging",
    "infrastructure failure",
    "planned recovery",
    "planned fault injection",
)
# S4.3 allows normal review/remediation. Reconstructing grants, patching the
# runtime, and manually reconstructing state are unplanned workflow repair.
ORDINARY_REVIEW = "normal review remediation"
GateStatus = Literal["not started", "incomplete", "failed", "count reached"]
StrataStatus = Literal["not preregistered", "preregistered"]

_ATTACHMENT_REASONS = {
    "retry": "attached retry; not a separate task",
    "review_fix": "attached review fix; not a separate task",
    "resumption": "attached resumption; not a separate task",
}


@dataclass(frozen=True)
class Attachment:
    """Retry, review fix, or resumption. It stays on its task and is not counted again."""
    record_id: str
    kind: AttachmentKind


@dataclass(frozen=True)
class OrdinaryTask:
    """One declared ordinary task instance, in the caller's projected form.

    ``started_at`` is not a chronology or duration source. Order comes from the
    frozen task list. The sustained interval comes from the frozen run bounds.
    """
    task_id: str
    record_id: str
    repository: str
    family: str
    outcome: OrdinaryOutcome
    scope: ScopeIdentity
    attachments: tuple[Attachment, ...] = ()
    repairs: tuple[str, ...] = ()
    regression_id: str | None = None
    integrity: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    started_at: datetime | None = None


@dataclass(frozen=True)
class OrdinarySnapshot:
    """In-memory projection. Not a serialized writer format and not a freeze."""
    protocol: FrozenProtocol | None = None
    tasks: tuple[OrdinaryTask, ...] = ()
    extra_exclusions: tuple[Exclusion, ...] = ()


@dataclass(frozen=True)
class OutcomeTotals:
    accepted: int = 0
    failed: int = 0
    blocked: int = 0
    cancelled: int = 0
    incomplete: int = 0

    @property
    def instances(self) -> int:
        return self.accepted + self.failed + self.blocked + self.cancelled + self.incomplete


@dataclass(frozen=True)
class CategoryTotals:
    negative_science: int = 0
    candidate_debugging: int = 0
    infrastructure_failure: int = 0
    planned_recovery: int = 0
    planned_fault_injection: int = 0


@dataclass(frozen=True)
class Coverage:
    repositories: tuple[str, ...]
    missing_repositories: tuple[str, ...]
    families: tuple[str, ...]
    multiple_families: bool


@dataclass(frozen=True)
class Reset:
    cause: str
    task_id: str
    record_id: str


@dataclass(frozen=True)
class ScopeEnd:
    """The first frozen-identity change. Later tasks are not a new run."""
    task_id: str
    record_id: str
    fields: tuple[str, ...]
    outcome: str


@dataclass(frozen=True)
class SharedObservation:
    """One task observation. ``uses`` names every gate the same evidence serves."""
    task_id: str
    record_id: str
    uses: tuple[Gate, ...]
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class OrdinaryGateReport:
    gate: Gate
    status: GateStatus
    count: int | None
    total_instances: int | None
    outcomes: OutcomeTotals | None
    contributing_source_ids: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    resets: tuple[Reset, ...]
    scope_end: ScopeEnd | None
    elapsed: timedelta | None
    duration_met: bool | None
    coverage: Coverage | None
    run_failures: tuple[str, ...]
    categories: CategoryTotals | None
    unresolved_failure_ids: tuple[str, ...]
    unplanned_repairs: tuple[str, ...]
    release_qualified: bool
    strata_status: StrataStatus
    allocations: tuple[tuple[str, int], ...]
    missing_proof: tuple[str, ...]


@dataclass(frozen=True)
class OrdinaryGateReports:
    consecutive: OrdinaryGateReport
    sustained: OrdinaryGateReport
    observations: tuple[SharedObservation, ...]


def ordinary_gate_reports(snapshot: OrdinarySnapshot | Snapshot) -> OrdinaryGateReports:
    """Report the 20-trial gate and the 72-hour/30-task gate from one snapshot.

    The two reports share one scope walk, one observation per task, and one
    exclusion list. A task whose release, configuration, recipe, evaluator,
    workload, and acceptance criteria match the frozen scope is a single
    observation with both uses named.
    """
    projected = _as_ordinary(snapshot)
    protocol = projected.protocol
    if protocol is None or not protocol.protocol_sha256 or not protocol.release_baseline_sha256:
        missing = _missing_start(protocol)
        exclusions = tuple(
            Exclusion(record_id, "missing O9 freeze or release baseline")
            for record_id in _candidate_ids(projected)
        )
        observations = tuple(
            SharedObservation(task.task_id, task.record_id, (), task.attachments)
            for task in _unique_tasks(projected.tasks)[0].values()
        )
        report = _stopped("not started", exclusions, missing, ())
        return OrdinaryGateReports(
            _report(CONSECUTIVE_GATE, report),
            _report(SUSTAINED_GATE, report),
            observations,
        )

    walk = _walk(projected, protocol)
    exclusions = tuple(walk.exclusions)
    strata, allocations, missing = _strata(protocol)
    coverage = _coverage(walk.repositories, walk.families)
    outcomes = walk.outcomes
    categories = walk.categories
    base = _Measured(
        exclusions=exclusions,
        resets=tuple(walk.resets),
        scope_end=walk.scope_end,
        elapsed=walk.elapsed,
        duration_met=walk.elapsed >= SUSTAINED_DURATION,
        coverage=coverage,
        outcomes=outcomes,
        total_instances=outcomes.instances,
        contributing_source_ids=tuple(walk.contributing),
        run_failures=tuple(walk.run_failures),
        categories=categories,
        unresolved_failure_ids=tuple(walk.unresolved),
        unplanned_repairs=tuple(walk.unplanned_repairs),
        strata_status=strata,
        allocations=allocations,
        missing_proof=missing,
    )
    return OrdinaryGateReports(
        _finish(CONSECUTIVE_GATE, base, walk.streak),
        _finish(SUSTAINED_GATE, base, outcomes.instances),
        tuple(walk.observations),
    )


@dataclass(frozen=True)
class _Measured:
    exclusions: tuple[Exclusion, ...]
    resets: tuple[Reset, ...]
    scope_end: ScopeEnd | None
    elapsed: timedelta
    duration_met: bool
    coverage: Coverage
    outcomes: OutcomeTotals
    total_instances: int
    contributing_source_ids: tuple[str, ...]
    run_failures: tuple[str, ...]
    categories: CategoryTotals
    unresolved_failure_ids: tuple[str, ...]
    unplanned_repairs: tuple[str, ...]
    strata_status: StrataStatus
    allocations: tuple[tuple[str, int], ...]
    missing_proof: tuple[str, ...]


@dataclass
class _Walk:
    exclusions: list[Exclusion]
    observations: list[SharedObservation]
    resets: list[Reset]
    contributing: list[str]
    run_failures: list[str]
    unplanned_repairs: list[str]
    unresolved: list[str]
    repositories: list[str]
    families: list[str]
    outcomes: OutcomeTotals
    categories: CategoryTotals
    streak: int = 0
    scope_end: ScopeEnd | None = None
    elapsed: timedelta = timedelta(0)


def _walk(snapshot: OrdinarySnapshot, protocol: FrozenProtocol) -> _Walk:
    by_id, exclusions = _unique_tasks(snapshot.tasks)
    walk = _Walk(exclusions=exclusions, observations=[], resets=[], contributing=[],
                 run_failures=[], unplanned_repairs=[], unresolved=[], repositories=[],
                 families=[], outcomes=OutcomeTotals(), categories=CategoryTotals(),
                 elapsed=protocol.run_end - protocol.run_start)
    ordered: set[str] = set()
    for task_id in protocol.task_order:
        if task_id in ordered:
            _add(walk.exclusions, Exclusion(task_id, "duplicate frozen task order entry"))
            continue
        ordered.add(task_id)
        task = by_id.get(task_id)
        if task is None:
            # A hole is not a success and not a failed trial. Do not join the
            # accepts on either side, and do not label the id as a failure.
            _add(walk.exclusions, Exclusion(task_id, "frozen task absent from snapshot"))
            walk.streak = 0
            continue
        if walk.scope_end is not None:
            _add(walk.exclusions, Exclusion(
                task.record_id or task.task_id,
                f"after scope end; not restarted; outcome {task.outcome}",
            ))
            continue
        mismatches = scope_boundary(protocol.scope, task.scope)
        if mismatches:
            _end_scope(walk, task, mismatches)
            continue
        if task.outcome not in OUTCOMES:
            _add(walk.exclusions, Exclusion(task.record_id or task.task_id, "unknown outcome"))
            walk.streak = 0
            continue
        _count_task(walk, task)

    for task in by_id.values():
        if task.task_id not in ordered:
            _add(walk.exclusions, Exclusion(
                task.record_id or task.task_id, "not in frozen task order",
            ))
    for extra in snapshot.extra_exclusions:
        _add(walk.exclusions, extra)
    return walk


def _count_task(walk: _Walk, task: OrdinaryTask) -> None:
    """Count one in-scope instance. Attachments are already excluded."""
    walk.observations.append(SharedObservation(
        task.task_id, task.record_id, SHARED_USES, task.attachments,
    ))
    walk.contributing.append(task.record_id)
    walk.outcomes = _add_outcome(walk.outcomes, task.outcome)
    walk.categories = _add_categories(walk.categories, task.categories)
    if task.repository and task.repository not in walk.repositories:
        walk.repositories.append(task.repository)
    if task.family and task.family not in walk.families:
        walk.families.append(task.family)

    unplanned = [repair for repair in task.repairs if repair != ORDINARY_REVIEW]
    if unplanned:
        for repair in unplanned:
            if repair not in walk.unplanned_repairs:
                walk.unplanned_repairs.append(repair)
        _reset(walk, "unplanned workflow repair", task)
        _remember(walk.run_failures, "unplanned workflow repair")
    for flag in task.integrity:
        _remember(walk.run_failures, flag)
    if task.outcome != "accepted":
        _reset(walk, "counted non-accepted", task)
        if task.outcome == "failed" and not task.regression_id:
            walk.unresolved.append(task.task_id)
        return
    if unplanned or task.integrity:
        # A labeled acceptance is not a streak advance through repair or a
        # lost, duplicate, unauthorized, or false acceptance.
        if not unplanned:
            for flag in task.integrity:
                _reset(walk, flag, task)
        return
    if walk.unresolved:
        return
    walk.streak += 1


def _end_scope(walk: _Walk, task: OrdinaryTask, mismatches: tuple[str, ...]) -> None:
    for field in mismatches:
        if field in STREAK_RESET_FIELDS:
            _reset(walk, field, task)
    for repair in task.repairs:
        if repair != ORDINARY_REVIEW:
            if repair not in walk.unplanned_repairs:
                walk.unplanned_repairs.append(repair)
            _reset(walk, "unplanned workflow repair", task)
            _remember(walk.run_failures, "unplanned workflow repair")
    for flag in task.integrity:
        _remember(walk.run_failures, flag)
    walk.scope_end = ScopeEnd(task.task_id, task.record_id, mismatches, task.outcome)
    walk.observations.append(SharedObservation(task.task_id, task.record_id, (), task.attachments))
    _add(walk.exclusions, Exclusion(
        task.record_id or task.task_id,
        "scope ended: " + ", ".join(mismatches) + f"; outcome {task.outcome} retained",
    ))


def _reset(walk: _Walk, cause: str, task: OrdinaryTask) -> None:
    walk.streak = 0
    reset = Reset(cause, task.task_id, task.record_id)
    if reset not in walk.resets:
        walk.resets.append(reset)


def _finish(gate: Gate, measured: _Measured, count: int) -> OrdinaryGateReport:
    open_scope = measured.scope_end is None
    if gate == CONSECUTIVE_GATE:
        reached = (
            open_scope
            and count >= STREAK_LENGTH
            and not measured.unresolved_failure_ids
        )
        status: GateStatus = "count reached" if reached else "incomplete"
        run_failures: tuple[str, ...] = ()
    else:
        coverage = measured.coverage
        reached = (
            open_scope
            and not measured.run_failures
            and measured.duration_met
            and count >= DISTINCT_TASKS
            and not coverage.missing_repositories
            and coverage.multiple_families
        )
        if measured.run_failures:
            status = "failed"
        elif reached:
            status = "count reached"
        else:
            status = "incomplete"
        run_failures = measured.run_failures
    return OrdinaryGateReport(
        gate=gate,
        status=status,
        count=count,
        total_instances=measured.total_instances,
        outcomes=measured.outcomes,
        contributing_source_ids=measured.contributing_source_ids,
        exclusions=measured.exclusions,
        resets=measured.resets,
        scope_end=measured.scope_end,
        elapsed=measured.elapsed,
        duration_met=measured.duration_met,
        coverage=measured.coverage,
        run_failures=run_failures,
        categories=measured.categories,
        unresolved_failure_ids=measured.unresolved_failure_ids,
        unplanned_repairs=measured.unplanned_repairs,
        release_qualified=False,
        strata_status=measured.strata_status,
        allocations=measured.allocations,
        missing_proof=measured.missing_proof,
    )


def _stopped(status: GateStatus, exclusions: tuple[Exclusion, ...], missing: tuple[str, ...],
            allocations: tuple[tuple[str, int], ...]) -> OrdinaryGateReport:
    return OrdinaryGateReport(
        gate=CONSECUTIVE_GATE,
        status=status,
        count=None,
        total_instances=None,
        outcomes=None,
        contributing_source_ids=(),
        exclusions=exclusions,
        resets=(),
        scope_end=None,
        elapsed=None,
        duration_met=None,
        coverage=None,
        run_failures=(),
        categories=None,
        unresolved_failure_ids=(),
        unplanned_repairs=(),
        release_qualified=False,
        strata_status="not preregistered",
        allocations=allocations,
        missing_proof=missing,
    )


def _report(gate: Gate, template: OrdinaryGateReport) -> OrdinaryGateReport:
    if template.gate == gate:
        return template
    return OrdinaryGateReport(**{**template.__dict__, "gate": gate})


def _as_ordinary(snapshot: OrdinarySnapshot | Snapshot) -> OrdinarySnapshot:
    if isinstance(snapshot, OrdinarySnapshot):
        return snapshot
    extra = list(snapshot.exclusions)
    for record in (*snapshot.receipts, *snapshot.observations):
        extra.append(Exclusion(record.record_id, "not an ordinary task instance"))
    for record_id in snapshot.acceptance_record_ids:
        extra.append(Exclusion(record_id, "not an ordinary task instance"))
    return OrdinarySnapshot(protocol=snapshot.protocol, extra_exclusions=tuple(extra))


def _missing_start(protocol: FrozenProtocol | None) -> tuple[str, ...]:
    missing: list[str] = []
    if protocol is None or not protocol.protocol_sha256:
        missing.append("acceptance-bound O9 freeze")
    if protocol is None or not protocol.release_baseline_sha256:
        missing.append("release baseline")
    missing.append("preregistered strata")
    return tuple(missing)


def _strata(protocol: FrozenProtocol) -> tuple[StrataStatus, tuple[tuple[str, int], ...], tuple[str, ...]]:
    if not protocol.allocations:
        return "not preregistered", (), ("preregistered strata",)
    return "preregistered", protocol.allocations, ()


def _coverage(repositories: list[str], families: list[str]) -> Coverage:
    present = tuple(repositories)
    missing = tuple(name for name in REPOSITORIES if name not in present)
    seen = tuple(families)
    return Coverage(present, missing, seen, len(seen) >= 2)


def _unique_tasks(tasks: Iterable[OrdinaryTask]) -> tuple[dict[str, OrdinaryTask], list[Exclusion]]:
    by_id: dict[str, OrdinaryTask] = {}
    exclusions: list[Exclusion] = []
    seen_records: set[str] = set()
    for task in tasks:
        identity = task.record_id or task.task_id or "<missing>"
        if not task.task_id:
            _add(exclusions, Exclusion(identity, "missing task id"))
            continue
        if task.task_id in by_id:
            _add(exclusions, Exclusion(identity, "duplicate task instance"))
            _exclude_attachments(exclusions, task, seen_records)
            continue
        if task.record_id and task.record_id in seen_records:
            _add(exclusions, Exclusion(task.record_id, "duplicate record id"))
            _exclude_attachments(exclusions, task, seen_records)
            continue
        by_id[task.task_id] = task
        if task.record_id:
            seen_records.add(task.record_id)
        _exclude_attachments(exclusions, task, seen_records)
    return by_id, exclusions


def _exclude_attachments(exclusions: list[Exclusion], task: OrdinaryTask, seen_records: set[str]) -> None:
    for attachment in task.attachments:
        reason = _ATTACHMENT_REASONS.get(attachment.kind, "attached activity; not a separate task")
        identity = attachment.record_id or "<missing>"
        if identity in seen_records:
            _add(exclusions, Exclusion(identity, f"duplicate record id; {reason}"))
            continue
        seen_records.add(identity)
        _add(exclusions, Exclusion(identity, reason))


def _candidate_ids(snapshot: OrdinarySnapshot) -> tuple[str, ...]:
    seen: list[str] = []
    for task in snapshot.tasks:
        _remember(seen, task.record_id or task.task_id or "<missing>")
        for attachment in task.attachments:
            _remember(seen, attachment.record_id or "<missing>")
    for extra in snapshot.extra_exclusions:
        _remember(seen, extra.record_id)
    return tuple(seen)


def _add_outcome(totals: OutcomeTotals, outcome: str) -> OutcomeTotals:
    values = {name: getattr(totals, name) for name in OUTCOMES}
    values[outcome] = values[outcome] + 1
    return OutcomeTotals(**values)


def _add_categories(totals: CategoryTotals, categories: tuple[str, ...]) -> CategoryTotals:
    values = {
        "negative science": totals.negative_science,
        "candidate debugging": totals.candidate_debugging,
        "infrastructure failure": totals.infrastructure_failure,
        "planned recovery": totals.planned_recovery,
        "planned fault injection": totals.planned_fault_injection,
    }
    for category in dict.fromkeys(categories):
        if category in values:
            values[category] += 1
    return CategoryTotals(
        values["negative science"],
        values["candidate debugging"],
        values["infrastructure failure"],
        values["planned recovery"],
        values["planned fault injection"],
    )


def _add(items: list[Exclusion], exclusion: Exclusion) -> None:
    if all(existing.record_id != exclusion.record_id for existing in items):
        items.append(exclusion)


def _remember(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
