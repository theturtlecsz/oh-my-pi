"""Pure alarm classification and digest builder over domain events (OMP-406)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
from typing import Any, Iterable
from uuid import UUID

from omp_work.v1.api_models import DomainEventView
from omp_work.v1.models import OWNER_APPROVAL_COMMAND_TYPES, OWNER_APPROVAL_REFUSED_EVENT

__all__ = [
    "ALERT_KINDS",
    "REPEATED_FAILURE_THRESHOLD",
    "Alert",
    "AlarmState",
    "classify",
    "build_digest",
]

ALERT_KINDS: frozenset[str] = frozenset(
    {
        "cost_threshold",
        "owner_approval_attempt",
        "safety_check_failed",
        "budget_exceeded",
        "repeated_failure",
        "credential_appeared",
    }
)

REPEATED_FAILURE_THRESHOLD: int = 3


def _normalize_aggregate(agg: Any) -> UUID | str:
    if isinstance(agg, UUID):
        return agg
    if isinstance(agg, str):
        try:
            return UUID(agg)
        except ValueError:
            return agg
    return agg


class _FailureDict(dict):
    """Dictionary supporting transparent UUID and string-UUID key lookups."""

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(_normalize_aggregate(key))

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(_normalize_aggregate(key), value)

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(_normalize_aggregate(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(_normalize_aggregate(key), default)

    def pop(self, key: Any, *args: Any) -> Any:
        return super().pop(_normalize_aggregate(key), *args)


@dataclass(frozen=True)
class Alert:
    """Frozen operational alert generated from a domain event."""

    kind: str
    workspace_id: UUID
    summary: str
    related_sequences: tuple[int, ...]
    event_id: UUID
    sequence: int
    event_sha256: str
    event_type: str
    aggregate_id: UUID
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.kind not in ALERT_KINDS:
            raise ValueError(f"Unknown alert kind: {self.kind!r}")
        if len(self.summary) > 200:
            raise ValueError("summary must not exceed 200 characters")
        if isinstance(self.workspace_id, str):
            object.__setattr__(self, "workspace_id", UUID(self.workspace_id))
        if isinstance(self.event_id, str):
            object.__setattr__(self, "event_id", UUID(self.event_id))
        if isinstance(self.aggregate_id, str):
            object.__setattr__(self, "aggregate_id", UUID(self.aggregate_id))
        if not isinstance(self.related_sequences, tuple):
            object.__setattr__(
                self, "related_sequences", tuple(self.related_sequences)
            )

    @property
    def idempotency_key(self) -> str:
        return f"{self.event_id}:{self.kind}"

    def body(self) -> dict[str, Any]:
        return {
            "type": "alert",
            "kind": self.kind,
            "workspace_id": str(self.workspace_id),
            "summary": self.summary,
            "related_sequences": list(self.related_sequences),
            "idempotency_key": self.idempotency_key,
            "event": {
                "event_id": str(self.event_id),
                "sequence": self.sequence,
                "event_sha256": self.event_sha256,
                "event_type": self.event_type,
                "aggregate_id": str(self.aggregate_id),
                "occurred_at": (
                    self.occurred_at.isoformat()
                    if hasattr(self.occurred_at, "isoformat")
                    else str(self.occurred_at)
                ),
            },
        }


@dataclass
class AlarmState:
    """State for incremental alarm classification."""

    after_sequence: int = 0
    failures: dict[Any, list[int]] = field(default_factory=_FailureDict)

    def __post_init__(self) -> None:
        raw = self.failures if self.failures is not None else {}
        self.failures = _FailureDict(
            {
                _normalize_aggregate(k): [int(seq) for seq in v]
                for k, v in raw.items()
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "after_sequence": self.after_sequence,
                "failures": {
                    str(k): list(v) for k, v in self.failures.items()
                },
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, data: str | bytes | dict[str, Any]) -> AlarmState:
        if isinstance(data, (str, bytes)):
            payload = json.loads(data)
        elif isinstance(data, dict):
            payload = data
        else:
            raise TypeError(
                f"Expected str, bytes, or dict, got {type(data).__name__}"
            )
        after_sequence = int(payload.get("after_sequence", 0))
        failures_raw = payload.get("failures", {})
        failures = {
            _normalize_aggregate(k): [int(x) for x in v]
            for k, v in failures_raw.items()
        }
        return cls(after_sequence=after_sequence, failures=failures)


def _extract_summary(event: DomainEventView, kind: str) -> str:
    payload = event.payload or {}
    if payload.get("subject"):
        return str(payload["subject"])[:200]
    if payload.get("reason"):
        return str(payload["reason"])[:200]
    if payload.get("code"):
        return f"{kind}: {payload['code']}"[:200]
    if payload.get("detail"):
        return str(payload["detail"])[:200]
    return f"{kind}: {event.event_type}"[:200]


def classify(
    events: Iterable[DomainEventView],
    state: AlarmState | None = None,
) -> tuple[list[Alert], list[DomainEventView], AlarmState]:
    """Classify domain events into alerts, rest events, and updated state.

    Events must have strictly ascending sequences greater than state.after_sequence.
    The input state is not mutated.
    """
    if state is None:
        state = AlarmState()

    alerts: list[Alert] = []
    rest: list[DomainEventView] = []

    new_failures = _FailureDict(
        {k: list(v) for k, v in state.failures.items()}
    )
    current_after_sequence = state.after_sequence

    for event in events:
        if event.sequence <= current_after_sequence:
            raise ValueError(
                f"sequence {event.sequence} <= after_sequence {current_after_sequence}"
            )
        current_after_sequence = event.sequence

        matched_alert: Alert | None = None

        # 1. record_alarm_signal gives kind payload["signal"]
        if event.event_type == "record_alarm_signal":
            kind = event.payload.get("signal")
            if not kind:
                raise ValueError(
                    f"record_alarm_signal missing 'signal' in payload: {event.payload}"
                )
            summary = _extract_summary(event, kind)
            matched_alert = Alert(
                kind=kind,
                workspace_id=event.workspace_id,
                summary=summary,
                related_sequences=(event.sequence,),
                event_id=event.event_id,
                sequence=event.sequence,
                event_sha256=event.event_sha256,
                event_type=event.event_type,
                aggregate_id=event.aggregate_id,
                occurred_at=event.occurred_at,
            )

        # 2. owner-approval types or the refused-attempt event give owner_approval_attempt
        elif (
            event.event_type in OWNER_APPROVAL_COMMAND_TYPES
            or event.event_type == OWNER_APPROVAL_REFUSED_EVENT
            or (event.payload or {}).get("command_type") in OWNER_APPROVAL_COMMAND_TYPES
            or (event.payload or {}).get("type") in OWNER_APPROVAL_COMMAND_TYPES
        ):
            kind = "owner_approval_attempt"
            summary = _extract_summary(event, kind)
            matched_alert = Alert(
                kind=kind,
                workspace_id=event.workspace_id,
                summary=summary,
                related_sequences=(event.sequence,),
                event_id=event.event_id,
                sequence=event.sequence,
                event_sha256=event.event_sha256,
                event_type=event.event_type,
                aggregate_id=event.aggregate_id,
                occurred_at=event.occurred_at,
            )

        # 3. other refused outcome gives safety_check_failed
        elif event.outcome == "refused":
            kind = "safety_check_failed"
            summary = _extract_summary(event, kind)
            matched_alert = Alert(
                kind=kind,
                workspace_id=event.workspace_id,
                summary=summary,
                related_sequences=(event.sequence,),
                event_id=event.event_id,
                sequence=event.sequence,
                event_sha256=event.event_sha256,
                event_type=event.event_type,
                aggregate_id=event.aggregate_id,
                occurred_at=event.occurred_at,
            )

        # 4. else rest
        else:
            rest.append(event)

        if matched_alert is not None:
            alerts.append(matched_alert)

        # Failure tracking: refused, or safety_check_failed signal
        is_failure = (
            event.outcome == "refused"
            or event.event_type == OWNER_APPROVAL_REFUSED_EVENT
            or (
                event.event_type == "record_alarm_signal"
                and (event.payload or {}).get("signal") == "safety_check_failed"
            )
        )

        if is_failure:
            agg = _normalize_aggregate(event.aggregate_id)
            if agg not in new_failures:
                new_failures[agg] = []
            new_failures[agg].append(event.sequence)

            if len(new_failures[agg]) == REPEATED_FAILURE_THRESHOLD:
                repeated_alert = Alert(
                    kind="repeated_failure",
                    workspace_id=event.workspace_id,
                    summary=f"Repeated failure on aggregate {event.aggregate_id}"[:200],
                    related_sequences=tuple(new_failures[agg][:3]),
                    event_id=event.event_id,
                    sequence=event.sequence,
                    event_sha256=event.event_sha256,
                    event_type=event.event_type,
                    aggregate_id=event.aggregate_id,
                    occurred_at=event.occurred_at,
                )
                alerts.append(repeated_alert)

    new_state = AlarmState(
        after_sequence=current_after_sequence,
        failures=new_failures,
    )
    return alerts, rest, new_state


def _to_utc_date(dt: Any) -> date:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).date()
        return dt.date()
    if isinstance(dt, date):
        return dt
    raise TypeError(f"Cannot extract date from {type(dt).__name__}: {dt!r}")


def _normalize_day(day: Any) -> tuple[date, str]:
    if isinstance(day, str):
        d = date.fromisoformat(day)
        return d, day
    if isinstance(day, datetime):
        d = _to_utc_date(day)
        return d, d.isoformat()
    if isinstance(day, date):
        return day, day.isoformat()
    raise TypeError(f"Invalid day type: {type(day).__name__}: {day!r}")


def _extract_work_ids(event: DomainEventView) -> set[str]:
    ids: set[str] = set()
    if event.aggregate_type == "work_item":
        ids.add(str(event.aggregate_id))
    payload = event.payload or {}
    if payload.get("work_id"):
        ids.add(str(payload["work_id"]))
    if isinstance(payload.get("work_ids"), (list, tuple)):
        for wid in payload["work_ids"]:
            if wid:
                ids.add(str(wid))
    if isinstance(payload.get("items"), (list, tuple)):
        for item in payload["items"]:
            if isinstance(item, dict) and item.get("work_id"):
                ids.add(str(item["work_id"]))
            elif isinstance(item, (str, UUID)):
                ids.add(str(item))
    return ids


def build_digest(
    workspace_id: UUID | str,
    day: date | datetime | str,
    rest: Iterable[DomainEventView],
    alert_count: int | Iterable[Any],
) -> dict[str, Any]:
    """Build a daily operational digest over rest events of the specified UTC day."""
    if isinstance(alert_count, (list, tuple)):
        actual_alert_count = len(alert_count)
    else:
        actual_alert_count = int(alert_count)

    target_date, day_str = _normalize_day(day)
    day_events = [
        event for event in rest
        if _to_utc_date(event.occurred_at) == target_date
    ]
    day_events.sort(key=lambda e: e.sequence)

    if not day_events:
        return {
            "type": "digest",
            "workspace_id": str(workspace_id),
            "day": day_str,
            "event_count": 0,
            "by_event_type": {},
            "by_outcome": {},
            "work_items": 0,
            "first_sequence": None,
            "last_sequence": None,
            "last_event_sha256": None,
            "alerts": actual_alert_count,
        }

    by_event_type: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    work_item_ids: set[str] = set()

    for event in day_events:
        by_event_type[event.event_type] = (
            by_event_type.get(event.event_type, 0) + 1
        )
        by_outcome[event.outcome] = by_outcome.get(event.outcome, 0) + 1
        work_item_ids.update(_extract_work_ids(event))

    return {
        "type": "digest",
        "workspace_id": str(workspace_id),
        "day": day_str,
        "event_count": len(day_events),
        "by_event_type": by_event_type,
        "by_outcome": by_outcome,
        "work_items": len(work_item_ids),
        "first_sequence": day_events[0].sequence,
        "last_sequence": day_events[-1].sequence,
        "last_event_sha256": day_events[-1].event_sha256,
        "alerts": actual_alert_count,
    }
