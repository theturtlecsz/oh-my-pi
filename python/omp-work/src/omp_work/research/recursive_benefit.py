"""Longitudinal benefit of recursive confirmation (OMP-319-s04).

``summarize_benefit`` reads s03 confirmation comparisons and the native job
usage ids that already pay for them. It does not score tasks again and it does
not write a ledger.

Quality stays on its own axis. USD, compute seconds, tokens, and owner minutes
stay on theirs. Nothing here turns quality or owner minutes into money, and
there is no productivity multiplier.

On each cost axis the window benefit is baseline operating cost, minus
candidate operating cost, minus attributable research, evaluation, maintenance,
and interventions. Those four ownership costs are charged once for the window,
not once per task. Missing numbers stay unknown and block a complete claim.
Negative and non-finite costs are refused.

A second copy of the same observation is a replay: it does not add savings or
another fresh task. Identity clashes and overlapping claims are refused.
Unsuccessful attempts stay in the failure count and in the operating cost.
Old evaluator versions are separate series and are not pooled into one claim.
A window that still mixes those versions does not receive the one-time costs;
charge them by summarizing each evaluator on its own.
Adoption uses the caller's preregistered bounds. A favorable self-score, or any
unknown cost, cannot make the window eligible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from omp_work.research.recursive_transfer import ComparisonReport, DomainComparison, TaskComparison
from omp_work.research.recursive_transfer_execution import ArmExecution, TaskExecution
from omp_work.v1.canonical import sha256

__all__ = [
    "ArmLink",
    "AxisBenefit",
    "BenefitAxes",
    "BenefitReport",
    "BenefitSummary",
    "BoundObservation",
    "ConfirmationComparison",
    "CostAxes",
    "CostBounds",
    "DomainBenefit",
    "ObservationWindow",
    "OwnershipCosts",
    "QualityBound",
    "SeriesBenefit",
    "summarize_benefit",
]

_AXES = ("usd", "compute_seconds", "tokens", "owner_minutes")
_MONEY_AXES = ("usd",)
_QUANTITY_AXES = ("compute_seconds", "owner_minutes")
_NATIVE_ID_FIELDS = ("workspace_id", "work_id", "job_id", "request_id")
ArmName = Literal["baseline", "candidate"]


def _reject(reason: str) -> ValueError:
    return ValueError(reason)


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _reject("timestamp_must_be_aware")
    if value.utcoffset() != timedelta(0):
        raise _reject("timestamp_must_be_utc")
    return value


def _parse_usd(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (Decimal, str, int)):
        raise _reject("usd_must_be_decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError):
        raise _reject("nonfinite_cost") from None
    if not parsed.is_finite():
        raise _reject("nonfinite_cost")
    if parsed < 0:
        raise _reject("negative_cost")
    return parsed


def _parse_quantity(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _reject("nonfinite_cost")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _reject("nonfinite_cost")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            raise _reject("nonfinite_cost") from None
    elif isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise _reject("nonfinite_cost") from None
    else:
        raise _reject("nonfinite_cost")
    if not parsed.is_finite():
        raise _reject("nonfinite_cost")
    if parsed < 0:
        raise _reject("negative_cost")
    return parsed


def _parse_tokens(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject("nonfinite_cost")
    if value < 0:
        raise _reject("negative_cost")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _reject("identity_mismatch")
    return value


def _zero(axis: str) -> Decimal | int:
    return 0 if axis == "tokens" else Decimal(0)


@dataclass(frozen=True)
class CostAxes:
    """One attributable cost, with None meaning the axis was not measured."""

    usd: Decimal | None
    compute_seconds: Decimal | None
    tokens: int | None
    owner_minutes: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "usd", _parse_usd(self.usd))
        object.__setattr__(self, "compute_seconds", _parse_quantity(self.compute_seconds))
        object.__setattr__(self, "tokens", _parse_tokens(self.tokens))
        object.__setattr__(self, "owner_minutes", _parse_quantity(self.owner_minutes))

    def get(self, axis: str) -> Decimal | int | None:
        return getattr(self, axis)


@dataclass(frozen=True)
class OwnershipCosts:
    """Research, evaluation, maintenance, and interventions for one window.

    Each category is charged once for that window. A missing axis stays unknown.
    """

    research: CostAxes
    evaluation: CostAxes
    maintenance: CostAxes
    interventions: CostAxes

    def __post_init__(self) -> None:
        for name in ("research", "evaluation", "maintenance", "interventions"):
            if not isinstance(getattr(self, name), CostAxes):
                raise _reject("identity_mismatch")


@dataclass(frozen=True)
class QualityBound:
    """Preregistered quality gate. Deltas are already oriented so larger is better."""

    minimum_delta: float
    minimum_independent_tasks: int = 1

    def __post_init__(self) -> None:
        delta = self.minimum_delta
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
            raise _reject("nonfinite_cost")
        count = self.minimum_independent_tasks
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise _reject("identity_mismatch")
        object.__setattr__(self, "minimum_delta", float(delta))


@dataclass(frozen=True)
class CostBounds:
    """Preregistered minimum net savings on each cost axis."""

    usd: Decimal
    compute_seconds: Decimal
    tokens: int
    owner_minutes: Decimal

    def __post_init__(self) -> None:
        usd = _parse_usd(self.usd)
        compute = _parse_quantity(self.compute_seconds)
        tokens = _parse_tokens(self.tokens)
        owner = _parse_quantity(self.owner_minutes)
        if usd is None or compute is None or tokens is None or owner is None:
            raise _reject("identity_mismatch")
        object.__setattr__(self, "usd", usd)
        object.__setattr__(self, "compute_seconds", compute)
        object.__setattr__(self, "tokens", tokens)
        object.__setattr__(self, "owner_minutes", owner)

    def get(self, axis: str) -> Decimal | int:
        return getattr(self, axis)


@dataclass(frozen=True)
class ObservationWindow:
    """Half-open ``[start, end)`` window and the bounds the caller preregistered."""

    start: datetime
    end: datetime
    quality_bound: QualityBound
    cost_bounds: CostBounds

    def __post_init__(self) -> None:
        start = _aware_utc(self.start)
        end = _aware_utc(self.end)
        if start >= end:
            raise _reject("identity_mismatch")
        if not isinstance(self.quality_bound, QualityBound) or not isinstance(self.cost_bounds, CostBounds):
            raise _reject("identity_mismatch")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


@dataclass(frozen=True)
class ArmLink:
    """Method, release, receipt, and native usage ids for one comparison arm."""

    method_version: str
    release_version: str
    receipt_digest: str
    usage_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_version", _text(self.method_version))
        object.__setattr__(self, "release_version", _text(self.release_version))
        object.__setattr__(self, "receipt_digest", _text(self.receipt_digest))
        if isinstance(self.usage_ids, (str, bytes)) or not isinstance(self.usage_ids, Sequence):
            raise _reject("identity_mismatch")
        ids: list[str] = []
        for usage_id in self.usage_ids:
            text = _text(usage_id)
            if text not in ids:
                ids.append(text)
        object.__setattr__(self, "usage_ids", tuple(ids))


@dataclass(frozen=True)
class ConfirmationComparison:
    """One s03 confirmation task bound to versions, receipts, time, and usage ids."""

    domain: str
    task_id: str
    evaluator_version: str
    timestamp: datetime
    baseline: ArmLink
    candidate: ArmLink
    result: TaskExecution
    self_scored: bool = False
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _text(self.domain))
        object.__setattr__(self, "task_id", _text(self.task_id))
        object.__setattr__(self, "evaluator_version", _text(self.evaluator_version))
        object.__setattr__(self, "timestamp", _aware_utc(self.timestamp))
        if not isinstance(self.baseline, ArmLink) or not isinstance(self.candidate, ArmLink):
            raise _reject("identity_mismatch")
        if not isinstance(self.result, TaskExecution):
            raise _reject("identity_mismatch")
        if not isinstance(self.self_scored, bool):
            raise _reject("identity_mismatch")
        if self.content_sha256 is not None:
            object.__setattr__(self, "content_sha256", _text(self.content_sha256))
        if self.baseline.receipt_digest == self.candidate.receipt_digest:
            raise _reject("overlap")
        if set(self.baseline.usage_ids) & set(self.candidate.usage_ids):
            raise _reject("overlap")


@dataclass(frozen=True)
class BoundObservation:
    """One arm attempt bound to the identities a benefit claim may use."""

    method_version: str
    release_version: str
    evaluator_version: str
    domain: str
    task_id: str
    receipt_digest: str
    timestamp: datetime
    arm: ArmName
    usage_ids: tuple[str, ...]
    successful: bool
    self_scored: bool
    failures: tuple[str, ...]
    content_sha256: str | None = None


@dataclass(frozen=True)
class AxisBenefit:
    """One cost axis. ``net`` is unknown when any input on the axis is missing."""

    baseline_operating: Decimal | int | None
    candidate_operating: Decimal | int | None
    research: Decimal | int | None
    evaluation: Decimal | int | None
    maintenance: Decimal | int | None
    interventions: Decimal | int | None
    net: Decimal | int | None
    one_time_included: bool


@dataclass(frozen=True)
class BenefitAxes:
    usd: AxisBenefit
    compute_seconds: AxisBenefit
    tokens: AxisBenefit
    owner_minutes: AxisBenefit

    def get(self, axis: str) -> AxisBenefit:
        return getattr(self, axis)


@dataclass(frozen=True)
class BenefitSummary:
    """Quality and the four cost axes for one domain or one evaluator window."""

    quality_deltas: tuple[float, ...]
    eligible_deltas: tuple[float, ...]
    axes: BenefitAxes
    failures: int
    failure_reasons: tuple[str, ...]
    fresh_independent_tasks: int
    complete: bool
    adoption_eligible: bool
    self_scored: bool
    observations: tuple[BoundObservation, ...]


@dataclass(frozen=True)
class DomainBenefit:
    domain: str
    summary: BenefitSummary


@dataclass(frozen=True)
class SeriesBenefit:
    """Evidence that shares one evaluator version. Other evaluators are not added in."""

    evaluator_version: str
    protocol_sha256: str | None
    domains: tuple[DomainBenefit, ...]
    cumulative: BenefitSummary


@dataclass(frozen=True)
class BenefitReport:
    """Per-series window projections. Series are not added together."""

    series: tuple[SeriesBenefit, ...]
    replayed: int
    window_start: datetime
    window_end: datetime

    @property
    def cumulative(self) -> BenefitSummary | None:
        """The window claim when every observation shares one evaluator version."""
        if len(self.series) != 1:
            return None
        return self.series[0].cumulative

    @property
    def adoption_eligible(self) -> bool:
        cumulative = self.cumulative
        return bool(cumulative and cumulative.adoption_eligible)


@dataclass(frozen=True)
class _Usage:
    usage_id: str
    usd: Decimal | None
    compute_seconds: Decimal | None
    tokens: int | None
    owner_minutes: Decimal | None
    fields: Mapping[str, object]
    recorded_at: datetime | None

    def get(self, axis: str) -> Decimal | int | None:
        return getattr(self, axis)


@dataclass(frozen=True)
class _Arm:
    method_version: str
    release_version: str
    receipt_digest: str
    usage_ids: tuple[str, ...]
    usd: Decimal | None
    compute_seconds: Decimal | None
    tokens: int | None
    owner_minutes: Decimal | None

    def get(self, axis: str) -> Decimal | int | None:
        return getattr(self, axis)


@dataclass(frozen=True)
class _Comparison:
    domain: str
    task_id: str
    content_sha256: str | None
    evaluator_version: str
    protocol_sha256: str | None
    timestamp: datetime
    baseline: _Arm
    candidate: _Arm
    quality_delta: float | None
    eligible: bool
    unsuccessful: bool
    self_scored: bool
    failures: tuple[str, ...]

    def identity(self) -> tuple[object, ...]:
        return (
            self.evaluator_version,
            self.domain,
            self.task_id,
            self.content_sha256,
            self.timestamp,
            self.baseline.method_version,
            self.candidate.method_version,
            self.baseline.release_version,
            self.candidate.release_version,
            self.baseline.receipt_digest,
            self.candidate.receipt_digest,
            self.baseline.usage_ids,
            self.candidate.usage_ids,
            self.quality_delta,
            self.eligible,
            self.unsuccessful,
            self.self_scored,
            self.failures,
        )


def _native_usage_id(payload: Mapping[str, object], usage_id: str) -> None:
    """When the caller supplies the native identity, the usage id must be its digest."""
    if not any(key in payload for key in ("workspace_id", "work_id", "request_id")):
        return
    if not all(key in payload for key in _NATIVE_ID_FIELDS):
        raise _reject("identity_mismatch")
    job_id = payload["job_id"]
    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        raise _reject("identity_mismatch")
    if not isinstance(payload["request_id"], str) or not payload["request_id"]:
        raise _reject("identity_mismatch")
    if payload["workspace_id"] in ("", None) or payload["work_id"] in ("", None):
        raise _reject("identity_mismatch")
    expected = sha256(
        {
            "workspace_id": str(payload["workspace_id"]),
            "work_id": str(payload["work_id"]),
            "job_id": job_id,
            "request_id": payload["request_id"],
        }
    )
    if expected != usage_id:
        raise _reject("identity_mismatch")


def _parse_event(payload: Mapping[str, object]) -> _Usage:
    if not isinstance(payload, Mapping):
        raise _reject("identity_mismatch")
    usage_id = _text(payload.get("usage_id"))
    _native_usage_id(payload, usage_id)
    measurement = payload.get("measurement")
    token_fields = ("input_tokens", "output_tokens", "cache_tokens")
    if measurement == "unknown":
        if any(payload.get(name) is not None for name in token_fields) or payload.get("tokens") is not None:
            raise _reject("identity_mismatch")
        tokens: int | None = None
    elif any(name in payload for name in token_fields):
        parts = []
        for name in token_fields:
            part = _parse_tokens(payload.get(name))
            if part is None:
                raise _reject("identity_mismatch")
            parts.append(part)
        tokens = sum(parts)
        if "tokens" in payload and _parse_tokens(payload.get("tokens")) != tokens:
            raise _reject("identity_mismatch")
    else:
        tokens = _parse_tokens(payload.get("tokens"))
    recorded_at = payload.get("recorded_at")
    if recorded_at is not None:
        recorded_at = _aware_utc(recorded_at)
    fields = {
        key: payload[key]
        for key in (
            "domain",
            "task_id",
            "content_sha256",
            "arm",
            "method_version",
            "release_version",
            "evaluator_version",
            "receipt_digest",
            "job_id",
        )
        if key in payload
    }
    return _Usage(
        usage_id,
        _parse_usd(payload.get("price_usd", payload.get("usd"))),
        _parse_quantity(payload.get("compute_seconds")),
        tokens,
        _parse_quantity(payload.get("owner_minutes")),
        fields,
        recorded_at,
    )


def _index_events(usage_events: object) -> tuple[dict[str, _Usage], int]:
    if isinstance(usage_events, Mapping):
        sequence: Sequence[object] = (usage_events,)
    elif isinstance(usage_events, (str, bytes)) or not isinstance(usage_events, Sequence):
        raise _reject("identity_mismatch")
    else:
        sequence = usage_events
    indexed: dict[str, _Usage] = {}
    replayed = 0
    for item in sequence:
        if not isinstance(item, Mapping):
            raise _reject("identity_mismatch")
        event = _parse_event(item)
        previous = indexed.get(event.usage_id)
        if previous is None:
            indexed[event.usage_id] = event
            continue
        if previous != event:
            raise _reject("identity_mismatch")
        replayed += 1
    return indexed, replayed


def _costs(usage_ids: Sequence[str], events: Mapping[str, _Usage]) -> dict[str, Decimal | int | None]:
    totals: dict[str, Decimal | int | None] = {axis: _zero(axis) for axis in _AXES}
    if not usage_ids:
        return {axis: None for axis in _AXES}
    for usage_id in usage_ids:
        event = events.get(usage_id)
        if event is None:
            raise _reject("identity_mismatch")
        for axis in _AXES:
            value = event.get(axis)
            current = totals[axis]
            if value is None or current is None:
                totals[axis] = None
            else:
                totals[axis] = current + value
    return totals


def _agree(event: _Usage, expected: Mapping[str, object]) -> None:
    for key, value in expected.items():
        if key not in event.fields:
            continue
        if event.fields[key] != value:
            raise _reject("identity_mismatch")
    if event.recorded_at is not None and event.recorded_at != expected["timestamp"]:
        raise _reject("identity_mismatch")


def _check_execution_arm(arm: ArmExecution, link: ArmLink) -> None:
    if arm.source_sha256 is not None and arm.source_sha256 != link.method_version:
        raise _reject("identity_mismatch")
    if arm.bundle_sha256 is not None and arm.bundle_sha256 != link.release_version:
        raise _reject("identity_mismatch")
    if arm.receipt_sha256 is not None and arm.receipt_sha256 != link.receipt_digest:
        raise _reject("identity_mismatch")


def _measured(execution: TaskExecution) -> tuple[bool, float | None]:
    delta = execution.delta
    if delta is None:
        return False, None
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
        raise _reject("identity_mismatch")
    finished = execution.ok and execution.status == "compared" and not execution.failures
    if not finished:
        return False, None
    return True, float(delta)


def _as_decimal(value: Decimal | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise _reject("identity_mismatch")
    return value


def _as_int(value: Decimal | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject("identity_mismatch")
    return value


def _arm_from_costs(
    method_version: str,
    release_version: str,
    receipt_digest: str,
    usage_ids: tuple[str, ...],
    costs: Mapping[str, Decimal | int | None],
) -> _Arm:
    return _Arm(
        method_version,
        release_version,
        receipt_digest,
        usage_ids,
        _as_decimal(costs["usd"]),
        _as_decimal(costs["compute_seconds"]),
        _as_int(costs["tokens"]),
        _as_decimal(costs["owner_minutes"]),
    )


def _arm_from_link(
    link: ArmLink,
    events: Mapping[str, _Usage],
    expected: Mapping[str, object],
) -> _Arm:
    for usage_id in link.usage_ids:
        event = events.get(usage_id)
        if event is None:
            raise _reject("identity_mismatch")
        _agree(event, expected)
    return _arm_from_costs(
        link.method_version,
        link.release_version,
        link.receipt_digest,
        link.usage_ids,
        _costs(link.usage_ids, events),
    )


def _from_bound(comparison: ConfirmationComparison, events: Mapping[str, _Usage], window: ObservationWindow) -> _Comparison:
    execution = comparison.result
    if execution.phase != "confirmation":
        raise _reject("identity_mismatch")
    if execution.domain != comparison.domain or execution.task_id != comparison.task_id:
        raise _reject("identity_mismatch")
    content = comparison.content_sha256 if comparison.content_sha256 is not None else execution.content_sha256
    if (
        comparison.content_sha256 is not None
        and execution.content_sha256 is not None
        and comparison.content_sha256 != execution.content_sha256
    ):
        raise _reject("identity_mismatch")
    if not window.contains(comparison.timestamp):
        raise _reject("outside_window")
    _check_execution_arm(execution.baseline, comparison.baseline)
    _check_execution_arm(execution.candidate, comparison.candidate)
    measured, delta = _measured(execution)

    def expected(link: ArmLink, arm_name: ArmName, execution_arm: ArmExecution) -> dict[str, object]:
        fields: dict[str, object] = {
            "domain": comparison.domain,
            "task_id": comparison.task_id,
            "evaluator_version": comparison.evaluator_version,
            "timestamp": comparison.timestamp,
            "content_sha256": content,
            "arm": arm_name,
            "method_version": link.method_version,
            "release_version": link.release_version,
            "receipt_digest": link.receipt_digest,
        }
        if execution_arm.job_id is not None:
            fields["job_id"] = execution_arm.job_id
        return fields

    baseline = _arm_from_link(
        comparison.baseline, events, expected(comparison.baseline, "baseline", execution.baseline),
    )
    candidate = _arm_from_link(
        comparison.candidate, events, expected(comparison.candidate, "candidate", execution.candidate),
    )
    return _Comparison(
        comparison.domain,
        comparison.task_id,
        content,
        comparison.evaluator_version,
        execution.protocol_sha256,
        comparison.timestamp,
        baseline,
        candidate,
        delta,
        measured and delta is not None and delta > 0,
        not measured,
        comparison.self_scored,
        tuple(execution.failures),
    )


def _event_matches(event: _Usage, execution: TaskExecution) -> bool:
    fields = event.fields
    if fields.get("domain") != execution.domain or fields.get("task_id") != execution.task_id:
        return False
    if fields.get("arm") not in ("baseline", "candidate"):
        return False
    if "content_sha256" in fields and fields["content_sha256"] != execution.content_sha256:
        return False
    return True


def _require_event_text(event: _Usage, key: str) -> str:
    if key not in event.fields:
        raise _reject("identity_mismatch")
    return _text(event.fields[key])


def _arm_from_events(events: Sequence[_Usage], execution: ArmExecution) -> _Arm:
    if not events:
        raise _reject("identity_mismatch")
    method_version = _require_event_text(events[0], "method_version")
    release_version = _require_event_text(events[0], "release_version")
    receipt_digest = _require_event_text(events[0], "receipt_digest")
    if execution.source_sha256 is not None and execution.source_sha256 != method_version:
        raise _reject("identity_mismatch")
    if execution.bundle_sha256 is not None and execution.bundle_sha256 != release_version:
        raise _reject("identity_mismatch")
    if execution.receipt_sha256 is not None and execution.receipt_sha256 != receipt_digest:
        raise _reject("identity_mismatch")
    usage_ids: list[str] = []
    for event in events:
        if _require_event_text(event, "method_version") != method_version:
            raise _reject("identity_mismatch")
        if _require_event_text(event, "release_version") != release_version:
            raise _reject("identity_mismatch")
        if _require_event_text(event, "receipt_digest") != receipt_digest:
            raise _reject("identity_mismatch")
        if execution.job_id is not None and event.fields.get("job_id") != execution.job_id:
            raise _reject("identity_mismatch")
        if event.usage_id not in usage_ids:
            usage_ids.append(event.usage_id)
    return _arm_from_costs(
        method_version,
        release_version,
        receipt_digest,
        tuple(usage_ids),
        _costs(usage_ids, {event.usage_id: event for event in events}),
    )


def _from_execution(
    execution: TaskExecution,
    arm_events: Mapping[str, Sequence[_Usage]],
    *,
    transfer_eligible: bool,
) -> _Comparison:
    if execution.phase != "confirmation" or not execution.domain or not execution.task_id:
        raise _reject("identity_mismatch")
    baseline_events = arm_events.get("baseline", ())
    candidate_events = arm_events.get("candidate", ())
    timestamp = baseline_events[0].recorded_at if baseline_events else None
    if timestamp is None:
        raise _reject("identity_mismatch")
    for event in (*baseline_events, *candidate_events):
        if event.recorded_at != timestamp:
            raise _reject("identity_mismatch")
    evaluator_version = _require_event_text(baseline_events[0], "evaluator_version")
    for event in (*baseline_events, *candidate_events):
        if _require_event_text(event, "evaluator_version") != evaluator_version:
            raise _reject("identity_mismatch")
    measured, delta = _measured(execution)
    self_scored = getattr(execution, "self_scored", False)
    if not isinstance(self_scored, bool):
        raise _reject("identity_mismatch")
    return _Comparison(
        execution.domain,
        execution.task_id,
        execution.content_sha256,
        evaluator_version,
        execution.protocol_sha256,
        timestamp,
        _arm_from_events(baseline_events, execution.baseline),
        _arm_from_events(candidate_events, execution.candidate),
        delta,
        transfer_eligible and measured and delta is not None and delta > 0,
        not measured,
        self_scored,
        tuple(execution.failures),
    )


def _iter_executions(value: object) -> list[tuple[TaskExecution, bool]]:
    if isinstance(value, ComparisonReport):
        eligible = {
            id(execution)
            for group in value.transfer
            for task in group.tasks
            for execution in task.executions
        }
        rows: list[tuple[TaskExecution, bool]] = []
        for group in value.domains:
            if group.phase != "confirmation":
                continue
            for task in group.tasks:
                for execution in task.executions:
                    rows.append((execution, id(execution) in eligible))
        return rows
    if isinstance(value, DomainComparison):
        if value.phase != "confirmation":
            raise _reject("identity_mismatch")
        group_eligible = value.status == "compared" and not value.keep_both
        return [
            (execution, group_eligible and _measured(execution)[0] and (execution.delta or 0) > 0)
            for task in value.tasks
            for execution in task.executions
        ]
    if isinstance(value, TaskComparison):
        group_eligible = value.status == "compared" and not value.keep_both
        return [(execution, group_eligible) for execution in value.executions]
    if isinstance(value, TaskExecution):
        measured, delta = _measured(value)
        return [(value, measured and delta is not None and delta > 0)]
    raise _reject("identity_mismatch")


def _comparisons_from_s03(
    items: Sequence[object],
    events: Mapping[str, _Usage],
    window: ObservationWindow,
) -> list[_Comparison]:
    executions: list[tuple[TaskExecution, bool]] = []
    for item in items:
        executions.extend(_iter_executions(item))
    assigned: dict[int, dict[str, list[_Usage]]] = {}
    used: set[str] = set()
    for event in events.values():
        matches = [execution for execution, _eligible in executions if _event_matches(event, execution)]
        if not matches:
            continue
        if len(matches) != 1:
            raise _reject("identity_mismatch")
        execution = matches[0]
        arm = str(event.fields["arm"])
        assigned.setdefault(id(execution), {"baseline": [], "candidate": []})[arm].append(event)
        used.add(event.usage_id)
    if used != set(events):
        raise _reject("identity_mismatch")
    rows: list[_Comparison] = []
    for execution, transfer_eligible in executions:
        row = _from_execution(execution, assigned.get(id(execution), {}), transfer_eligible=transfer_eligible)
        if not window.contains(row.timestamp):
            raise _reject("outside_window")
        if row.baseline.receipt_digest == row.candidate.receipt_digest:
            raise _reject("overlap")
        if set(row.baseline.usage_ids) & set(row.candidate.usage_ids):
            raise _reject("overlap")
        rows.append(row)
    return rows


def _normalize(
    confirmation_results: object,
    events: Mapping[str, _Usage],
    window: ObservationWindow,
) -> list[_Comparison]:
    if isinstance(confirmation_results, (ComparisonReport, DomainComparison, TaskComparison, TaskExecution)):
        items: Sequence[object] = (confirmation_results,)
        bound = False
    elif isinstance(confirmation_results, ConfirmationComparison):
        items = (confirmation_results,)
        bound = True
    elif isinstance(confirmation_results, (str, bytes, Mapping)) or not isinstance(confirmation_results, Sequence):
        raise _reject("identity_mismatch")
    else:
        items = tuple(confirmation_results)
        if any(isinstance(item, ConfirmationComparison) for item in items) and any(
            not isinstance(item, ConfirmationComparison) for item in items
        ):
            raise _reject("identity_mismatch")
        bound = bool(items) and isinstance(items[0], ConfirmationComparison)
    if bound:
        rows = [_from_bound(item, events, window) for item in items if isinstance(item, ConfirmationComparison)]
        referenced = {usage_id for row in rows for arm in (row.baseline, row.candidate) for usage_id in arm.usage_ids}
        if referenced != set(events):
            raise _reject("identity_mismatch")
        return rows
    return _comparisons_from_s03(items, events, window)


def _drop_replays(rows: Sequence[_Comparison]) -> tuple[list[_Comparison], int]:
    seen: set[tuple[object, ...]] = set()
    unique: list[_Comparison] = []
    replayed = 0
    for row in rows:
        key = row.identity()
        if key in seen:
            replayed += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique, replayed


def _reject_overlap(rows: Sequence[_Comparison]) -> None:
    receipts: set[str] = set()
    usages: set[str] = set()
    contents: dict[str, tuple[str, str]] = {}
    slots: dict[tuple[object, ...], str] = {}
    for row in rows:
        if row.content_sha256 is not None:
            owner = (row.domain, row.task_id)
            previous_owner = contents.get(row.content_sha256)
            if previous_owner is not None and previous_owner != owner:
                raise _reject("overlap")
            contents[row.content_sha256] = owner
        for arm_name, arm in (("baseline", row.baseline), ("candidate", row.candidate)):
            if arm.receipt_digest in receipts:
                raise _reject("overlap")
            receipts.add(arm.receipt_digest)
            slot = (
                row.evaluator_version,
                row.domain,
                row.task_id,
                row.content_sha256,
                arm_name,
                row.timestamp,
            )
            previous = slots.get(slot)
            if previous is not None and previous != arm.receipt_digest:
                raise _reject("overlap")
            slots[slot] = arm.receipt_digest
            for usage_id in arm.usage_ids:
                if usage_id in usages:
                    raise _reject("overlap")
                usages.add(usage_id)


def _observe(row: _Comparison, arm_name: ArmName, arm: _Arm) -> BoundObservation:
    return BoundObservation(
        arm.method_version,
        arm.release_version,
        row.evaluator_version,
        row.domain,
        row.task_id,
        arm.receipt_digest,
        row.timestamp,
        arm_name,
        arm.usage_ids,
        not row.unsuccessful,
        row.self_scored,
        row.failures,
        row.content_sha256,
    )


def _sum_axis(axis: str, rows: Sequence[_Comparison], arm_name: ArmName) -> Decimal | int | None:
    if not rows:
        return None
    total: Decimal | int | None = _zero(axis)
    for row in rows:
        value = getattr(row, arm_name).get(axis)
        if value is None or total is None:
            total = None
        else:
            total = total + value
    return total


def _axis_benefit(
    axis: str,
    rows: Sequence[_Comparison],
    ownership: OwnershipCosts | None,
) -> AxisBenefit:
    baseline = _sum_axis(axis, rows, "baseline")
    candidate = _sum_axis(axis, rows, "candidate")
    if ownership is None:
        net = None if baseline is None or candidate is None else baseline - candidate
        return AxisBenefit(baseline, candidate, None, None, None, None, net, False)
    research = ownership.research.get(axis)
    evaluation = ownership.evaluation.get(axis)
    maintenance = ownership.maintenance.get(axis)
    interventions = ownership.interventions.get(axis)
    parts = (baseline, candidate, research, evaluation, maintenance, interventions)
    net = None if any(part is None for part in parts) else (
        baseline - candidate - research - evaluation - maintenance - interventions
    )
    return AxisBenefit(
        baseline, candidate, research, evaluation, maintenance, interventions, net, True,
    )


def _axes(rows: Sequence[_Comparison], ownership: OwnershipCosts | None) -> BenefitAxes:
    built = {axis: _axis_benefit(axis, rows, ownership) for axis in _AXES}
    return BenefitAxes(
        built["usd"], built["compute_seconds"], built["tokens"], built["owner_minutes"],
    )


def _summary(
    rows: Sequence[_Comparison],
    ownership: OwnershipCosts | None,
    window: ObservationWindow,
) -> BenefitSummary:
    observations = tuple(
        observation
        for row in rows
        for observation in (_observe(row, "baseline", row.baseline), _observe(row, "candidate", row.candidate))
    )
    quality = tuple(row.quality_delta for row in rows if row.quality_delta is not None)
    eligible = tuple(row.quality_delta for row in rows if row.eligible and row.quality_delta is not None)
    reasons = tuple(dict.fromkeys(reason for row in rows if row.unsuccessful for reason in row.failures))
    identities: list[tuple[str, str, str | None]] = []
    for row in rows:
        identity = (row.domain, row.task_id, row.content_sha256)
        if identity not in identities:
            identities.append(identity)
    axes = _axes(rows, ownership)
    self_scored = any(row.self_scored for row in rows)
    complete = ownership is not None and all(axes.get(axis).net is not None for axis in _AXES)
    bound = window.quality_bound
    costs = window.cost_bounds
    adoption = (
        complete
        and not self_scored
        and len(identities) >= bound.minimum_independent_tasks
        and bool(eligible)
        and all(delta >= bound.minimum_delta for delta in quality)
        and all(delta >= bound.minimum_delta for delta in eligible)
        and all(axes.get(axis).net is not None and axes.get(axis).net >= costs.get(axis) for axis in _AXES)
    )
    return BenefitSummary(
        quality,
        eligible,
        axes,
        sum(1 for row in rows if row.unsuccessful),
        reasons,
        len(identities),
        complete,
        adoption,
        self_scored,
        observations,
    )


def _series(
    rows: Sequence[_Comparison],
    ownership: OwnershipCosts | None,
    window: ObservationWindow,
) -> SeriesBenefit:
    protocols = {row.protocol_sha256 for row in rows}
    if len(protocols) != 1:
        raise _reject("identity_mismatch")
    protocol = next(iter(protocols))
    grouped: dict[str, list[_Comparison]] = {}
    for row in rows:
        grouped.setdefault(row.domain, []).append(row)
    domains = tuple(
        DomainBenefit(domain, _summary(domain_rows, None, window))
        for domain, domain_rows in grouped.items()
    )
    # One-time costs are subtracted once for the whole series, never once per task or domain.
    cumulative = _summary(rows, ownership, window)
    return SeriesBenefit(rows[0].evaluator_version, protocol, domains, cumulative)


def summarize_benefit(
    confirmation_results: object,
    usage_events: object,
    ownership_costs: object,
    observation_window: object,
) -> BenefitReport:
    """Project one observation window from s03 confirmations and native usage ids.

    ``confirmation_results`` is an s03 ``ComparisonReport`` (confirmation phase
    only; transfer membership decides eligibility), a confirmation
    ``DomainComparison`` / ``TaskComparison`` / ``TaskExecution``, or a
    ``ConfirmationComparison`` that binds those executions to versions and usage
    ids. Development-phase rows in a report are not confirmation evidence.

    ``usage_events`` are native job usage rows already identified by ``usage_id``.
    When ``workspace_id``, ``work_id``, ``job_id``, and ``request_id`` are
    present, ``usage_id`` must be the native digest of those fields. This
    function does not record usage.
    """
    if not isinstance(observation_window, ObservationWindow):
        raise _reject("identity_mismatch")
    if not isinstance(ownership_costs, OwnershipCosts):
        raise _reject("identity_mismatch")
    events, event_replays = _index_events(usage_events)
    rows = _normalize(confirmation_results, events, observation_window)
    unique, comparison_replays = _drop_replays(rows)
    _reject_overlap(unique)
    by_evaluator: dict[str, list[_Comparison]] = {}
    for row in unique:
        by_evaluator.setdefault(row.evaluator_version, []).append(row)
    # A window that mixes evaluator versions does not attribute one-time costs to
    # each series. Splitting the call by evaluator charges those costs once.
    attributable = ownership_costs if len(by_evaluator) == 1 else None
    series = tuple(
        _series(grouped, attributable, observation_window)
        for grouped in by_evaluator.values()
    )
    return BenefitReport(
        series,
        event_replays + comparison_replays,
        observation_window.start,
        observation_window.end,
    )
