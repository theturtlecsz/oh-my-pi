"""Derive mission events from applied domain events. No I/O.

One applied domain event yields at most one mission event. The id is
``uuid5(uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events"), f"{event_id}:{type}")``.
Payload prose is not copied: evidence refs cite ids only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

__all__ = ["derive_mission_events"]

_MISSION_EVENTS_NS = uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events")

_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

_NOTIFIED_STATUSES = frozenset({"blocked", "completed", "failed"})


def derive_mission_events(
    events: Iterable[Any],
    *,
    mission_for_work: Callable[[Any, Any], UUID | None],
    finding_threshold: str = "high",
    budget_fraction: Decimal = Decimal("0.8"),
) -> list[dict[str, Any]]:
    """Project applied domain events into mission events.

    ``mission_for_work(work_id, sequence)`` resolves the mission for a
    ``complete_work`` aggregate. Rows whose ``outcome`` is not ``applied``
    are skipped. ``finding_threshold`` ranks ``low < medium < high < critical``.
    A budget alert fires when one link crosses ``budget_fraction`` of
    ``mission.budget.usd``.
    """
    if not isinstance(finding_threshold, str):
        raise ValueError(f"unknown finding_threshold: {finding_threshold!r}")
    threshold_rank = _SEVERITY_RANK.get(finding_threshold.strip().lower())
    if threshold_rank is None:
        raise ValueError(f"unknown finding_threshold: {finding_threshold!r}")
    fraction = (
        budget_fraction
        if isinstance(budget_fraction, Decimal)
        else Decimal(str(budget_fraction))
    )
    if not fraction.is_finite():
        raise ValueError(f"budget_fraction must be finite: {budget_fraction!r}")

    derived: list[dict[str, Any]] = []
    for event in events:
        if _field(event, "outcome") != "applied":
            continue
        row = _derive_applied(
            event,
            mission_for_work,
            finding_threshold,
            threshold_rank,
            fraction,
        )
        if row is not None:
            derived.append(row)
    return derived


def _derive_applied(
    event: Any,
    mission_for_work: Callable[[Any, Any], UUID | None],
    finding_threshold: str,
    threshold_rank: int,
    fraction: Decimal,
) -> dict[str, Any] | None:
    event_type = _event_type(event)
    if event_type == "set_mission_status":
        return _from_status(event)
    if event_type == "complete_work":
        return _from_complete(event, mission_for_work)
    if event_type == "create_decision":
        return _from_decision(event)
    if event_type == "link_mission_work":
        return _from_link(event, fraction)
    if event_type == "record_finding":
        return _from_finding(event, finding_threshold, threshold_rank)
    return None


def _from_status(event: Any) -> dict[str, Any] | None:
    mission = _field(_payload(event), "mission")
    transitions = _items(_field(mission, "transitions"))
    if not transitions:
        return None
    rule = _status_rule(transitions[-1])
    if rule is None:
        return None
    mission_id = _uuid_text(_field(mission, "mission_id"))
    if mission_id is None:
        return None
    type_, trigger = rule
    return _row(event, mission_id, type_, trigger, _refs(_event_id_text(event)))


def _status_rule(transition: Any) -> tuple[str, str] | None:
    origin = _status_text(_field(transition, "from_status"))
    target = _status_text(_field(transition, "to_status"))
    if target is None:
        return None
    if origin == "approved" and target == "running":
        return ("mission.started", "status:approved->running")
    if target in _NOTIFIED_STATUSES:
        return (f"mission.{target}", f"status:{target}")
    if target == "abandoned":
        return None
    return ("mission.progressed", "stage_change")


def _from_complete(
    event: Any,
    mission_for_work: Callable[[Any, Any], UUID | None],
) -> dict[str, Any] | None:
    work_id = _field(event, "aggregate_id")
    mission_id = _uuid_text(mission_for_work(work_id, _field(event, "sequence")))
    if mission_id is None:
        return None
    evidence = _refs(_event_id_text(event))
    if work_id is not None:
        evidence.append({"kind": "work_item", "ref": _id_text(work_id)})
    evidence.extend(_evidence_entries(_payload(event)))
    return _row(event, mission_id, "mission.progressed", "operation_completed", evidence)


def _from_decision(event: Any) -> dict[str, Any] | None:
    decision = _field(_payload(event), "decision")
    mission_id = _uuid_text(_field(decision, "mission_id"))
    if mission_id is None:
        return None
    evidence = _refs(_event_id_text(event))
    decision_id = _field(decision, "decision_id")
    if decision_id is not None:
        evidence.append({"kind": "decision", "ref": _id_text(decision_id)})
    evidence.extend(_evidence_entries(decision))
    return _row(event, mission_id, "decision.required", "decision_created", evidence)


def _from_link(event: Any, fraction: Decimal) -> dict[str, Any] | None:
    mission = _field(_payload(event), "mission")
    budget = _field(mission, "budget")
    if budget is None:
        return None
    limit_usd = _decimal(_field(budget, "usd"))
    after = _decimal(_field(_field(mission, "drawn"), "usd"))
    links = _items(_field(mission, "links"))
    if limit_usd is None or after is None or not links:
        return None
    last = links[-1]
    delta = _decimal(_field(_field(last, "budget"), "usd"))
    if delta is None:
        return None
    before = after - delta
    limit = fraction * limit_usd
    if not (before < limit <= after):
        return None
    mission_id = _uuid_text(_field(mission, "mission_id"))
    if mission_id is None:
        return None
    evidence = _refs(_event_id_text(event))
    work_id = _field(last, "work_id")
    if work_id is not None:
        evidence.append({"kind": "work_item", "ref": _id_text(work_id)})
    evidence.extend(_evidence_entries(_payload(event)))
    return _row(event, mission_id, "budget.threshold_reached", f"budget_usd>={fraction}", evidence)


def _from_finding(event: Any, finding_threshold: str, threshold_rank: int) -> dict[str, Any] | None:
    payload = _payload(event)
    finding = _field(payload, "finding")
    severity = _severity(finding, payload)
    rank = _SEVERITY_RANK.get(severity or "")
    if rank is None or rank < threshold_rank:
        return None
    mission_id = (
        _uuid_text(_field(finding, "mission_id"))
        or _uuid_text(_field(payload, "mission_id"))
        or _uuid_text(_field(_field(payload, "mission"), "mission_id"))
    )
    if mission_id is None:
        return None
    evidence = _refs(_event_id_text(event))
    finding_id = _finding_id(finding, payload)
    if finding_id is not None:
        evidence.append({"kind": "finding", "ref": finding_id})
    if _has(finding, "evidence_refs"):
        evidence.extend(_evidence_entries(finding))
    else:
        evidence.extend(_evidence_entries(payload))
    return _row(event, mission_id, "important_finding", f"finding_severity>={finding_threshold}", evidence)


def _row(
    event: Any,
    mission_id: str,
    type_: str,
    trigger: str,
    evidence_refs: list[dict[str, str]],
) -> dict[str, Any] | None:
    event_id = _event_id_text(event)
    if event_id is None:
        return None
    return {
        "mission_event_id": str(uuid5(_MISSION_EVENTS_NS, f"{event_id}:{type_}")),
        "sequence": _field(event, "sequence"),
        "mission_id": mission_id,
        "type": type_,
        "trigger": trigger,
        "occurred_at": _field(event, "occurred_at"),
        "source_event_id": event_id,
        "evidence_refs": evidence_refs,
    }


def _refs(event_id: str | None) -> list[dict[str, str]]:
    if event_id is None:
        return []
    return [{"kind": "domain_event", "ref": event_id}]


def _evidence_entries(obj: Any) -> list[dict[str, str]]:
    raw = _field(obj, "evidence_refs")
    if raw is None or isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    return [{"kind": "evidence", "ref": item} for item in raw if isinstance(item, str)]


def _finding_id(finding: Any, payload: Any) -> str | None:
    for source, keys in ((finding, ("finding_id", "id")), (payload, ("finding_id",))):
        if source is None:
            continue
        for key in keys:
            if not _has(source, key):
                continue
            found = _scalar_id(_field(source, key))
            if found is not None:
                return found
    return None


def _scalar_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if value is None or isinstance(value, (Mapping, bytes, Sequence)):
        return None
    return _id_text(value)


def _severity(finding: Any, payload: Any) -> str | None:
    if _has(finding, "severity"):
        raw = _field(finding, "severity")
    else:
        raw = _field(payload, "severity")
    if not isinstance(raw, str):
        return None
    return raw.strip().lower()


def _payload(event: Any) -> Any:
    payload = _field(event, "payload")
    return {} if payload is None else payload


def _event_type(event: Any) -> str | None:
    raw = _field(event, "event_type")
    if raw is None:
        raw = _field(event, "type")
    return raw if isinstance(raw, str) else None


def _event_id_text(event: Any) -> str | None:
    raw = _field(event, "event_id")
    if raw is None:
        return None
    return _id_text(raw)


def _id_text(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _uuid_text(value: Any) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


def _status_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower()
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw.strip().lower()
    return str(value).strip().lower()


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def _items(value: Any) -> Sequence[Any] | None:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return None
    if isinstance(value, Sequence):
        return value
    return None


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has(obj: Any, key: str) -> bool:
    if obj is None:
        return False
    if isinstance(obj, Mapping):
        return key in obj
    return hasattr(obj, key)
