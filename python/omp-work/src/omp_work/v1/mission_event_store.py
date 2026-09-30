"""OMP-415: mission event store — record findings and project mission events."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from omp_work.mission_events import derive_mission_events
from .api_models import EventSubscriptionView, FindingView
from .missions import latest_mission
from .models import (
    AdvanceEventCursorCommand,
    CommandEnvelope,
    DeleteEventSubscriptionCommand,
    PutEventSubscriptionCommand,
    RecordFindingCommand,
)
from .store_shared import WorkStoreError

__all__ = [
    "DOMAIN_EVENTS_WINDOW_QUERY",
    "advance_event_cursor",
    "delete_event_subscription",
    "event_subscriptions",
    "execute_event_subscriptions",
    "execute_mission_events",
    "list_subscriptions",
    "mission_events",
    "mission_for_work",
    "put_event_subscription",
    "record_finding",
]

DOMAIN_EVENTS_WINDOW_QUERY = (
    "WITH watermark AS ("
    " SELECT COALESCE(MAX(sequence), 0) AS wm"
    " FROM omp_audit.domain_events"
    " WHERE workspace_id = %s"
    ") "
    "SELECT "
    " w.wm AS watermark_sequence, "
    " e.event_id, "
    " e.sequence, "
    " e.workspace_id, "
    " e.aggregate_type, "
    " e.aggregate_id, "
    " e.aggregate_version, "
    " e.actor_id, "
    " e.actor_kind, "
    " e.capability_id, "
    " e.request_id, "
    " e.correlation_id, "
    " e.operation_id, "
    " e.causation_id, "
    " e.event_type, "
    " e.outcome, "
    " e.payload, "
    " e.payload_sha256, "
    " e.previous_event_sha256, "
    " e.event_sha256, "
    " e.occurred_at "
    "FROM watermark w "
    "LEFT JOIN LATERAL ("
    " SELECT * "
    " FROM omp_audit.domain_events "
    " WHERE workspace_id = %s "
    "   AND sequence > %s "
    "   AND sequence <= w.wm "
    " ORDER BY sequence ASC "
    " LIMIT %s"
    ") e ON true "
    "ORDER BY e.sequence ASC NULLS LAST"
)


def record_finding(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID | None = None,
    actor_kind: str | None = None,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, RecordFindingCommand):
        raise TypeError("record_finding dispatched with a non-record_finding command")
    payload = command.payload
    workspace_id = envelope.workspace_id

    mission = latest_mission(cur, workspace_id, payload.mission_id)
    if mission is None:
        raise WorkStoreError("invalid_request", ("mission_not_found",))

    cur.execute(
        "SELECT 1 FROM omp_audit.domain_events"
        " WHERE workspace_id = %s"
        " AND event_type = 'record_finding'"
        " AND outcome = 'applied'"
        " AND (payload->'finding'->>'finding_id' = %s OR payload @> %s)"
        " LIMIT 1",
        (
            workspace_id,
            str(payload.finding_id),
            json.dumps({"finding": {"finding_id": str(payload.finding_id)}}),
        ),
    )
    if cur.fetchone() is not None:
        raise WorkStoreError("revision_conflict", ("finding_exists",))

    finding_view = FindingView(
        finding_id=payload.finding_id,
        mission_id=payload.mission_id,
        severity=payload.severity,
        title=payload.title,
        evidence_refs=payload.evidence_refs,
    )
    return {
        "type": "record_finding",
        "finding": finding_view.model_dump(mode="json"),
    }


def mission_for_work(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    work_id: UUID | str,
    sequence: int,
) -> UUID | None:
    if not isinstance(work_id, UUID):
        try:
            work_id = UUID(str(work_id))
        except (ValueError, TypeError):
            return None
    cur.execute(
        "SELECT aggregate_id FROM omp_audit.domain_events"
        " WHERE workspace_id = %s"
        " AND aggregate_type = 'mission'"
        " AND event_type = 'link_mission_work'"
        " AND outcome = 'applied'"
        " AND sequence < %s"
        " AND payload @> %s"
        " ORDER BY sequence DESC LIMIT 1",
        (
            workspace_id,
            sequence,
            json.dumps({"mission": {"links": [{"work_id": str(work_id)}]}}),
        ),
    )
    row = cur.fetchone()
    if row is None:
        return None
    agg = row["aggregate_id"] if isinstance(row, dict) else row[0]
    return UUID(str(agg)) if not isinstance(agg, UUID) else agg


def _clean_event(r: dict[str, Any]) -> dict[str, Any]:
    event = {k: r[k] for k in r if k != "watermark_sequence"}
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            event["payload"] = json.loads(payload)
        except (ValueError, TypeError):
            pass
    return event


def _read_mission_events_config(
    config_dir: Path | None,
) -> tuple[str, Decimal]:
    finding_threshold = "high"
    budget_fraction = Decimal("0.8")
    if config_dir is not None:
        config_file = config_dir / "mission-events.json"
        try:
            if config_file.is_file():
                data = json.loads(config_file.read_text())
                if isinstance(data, dict):
                    if (
                        "finding_severity_threshold" in data
                        and data["finding_severity_threshold"] is not None
                    ):
                        finding_threshold = str(data["finding_severity_threshold"])
                    if (
                        "budget_usd_fraction" in data
                        and data["budget_usd_fraction"] is not None
                    ):
                        budget_fraction = Decimal(str(data["budget_usd_fraction"]))
        except (OSError, json.JSONDecodeError, InvalidOperation, ValueError):
            pass
    return finding_threshold, budget_fraction


def execute_mission_events(
    cur: psycopg.Cursor[dict[str, object]],
    config_dir: Path | None,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    after: int = 0,
    limit: int = 500,
    mission_id: UUID | None = None,
) -> dict[str, object]:
    finding_threshold, budget_fraction = _read_mission_events_config(config_dir)
    cur.execute(
        DOMAIN_EVENTS_WINDOW_QUERY,
        (workspace_id, workspace_id, after, limit + 1),
    )
    rows = cur.fetchall()
    watermark_sequence = int(rows[0]["watermark_sequence"]) if rows else 0
    if not rows or rows[0]["event_id"] is None:
        return {
            "events": (),
            "watermark_sequence": watermark_sequence,
            "next_after_sequence": after,
            "has_more": False,
        }
    has_more = len(rows) > limit
    page_rows = rows[:limit] if has_more else rows
    next_after_sequence = int(page_rows[-1]["sequence"])

    events = tuple(_clean_event(r) for r in page_rows)
    derived = derive_mission_events(
        events,
        mission_for_work=lambda work_id, seq: mission_for_work(
            cur, workspace_id, work_id, seq
        ),
        finding_threshold=finding_threshold,
        budget_fraction=budget_fraction,
    )
    if mission_id is not None:
        target_mid = str(mission_id)
        derived = [ev for ev in derived if str(ev["mission_id"]) == target_mid]

    return {
        "events": tuple(derived),
        "watermark_sequence": watermark_sequence,
        "next_after_sequence": next_after_sequence,
        "has_more": has_more,
    }


def mission_events(
    target: Any,
    *args: Any,
    after: int = 0,
    limit: int = 500,
    mission_id: UUID | None = None,
    config_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    if not 1 <= limit <= 500:
        raise WorkStoreError("invalid_request", ("limit must be between 1 and 500",))
    if after < 0:
        raise WorkStoreError("invalid_request", ("after must be non-negative",))

    if hasattr(target, "_transaction"):
        ws = args[0]
        actor = args[1] if len(args) > 1 else ws
        cd = getattr(target, "_config", None)
        cfg_dir = getattr(cd, "config_dir", None) if cd is not None else config_dir
        try:
            with target._transaction(ws, actor) as cur:
                return execute_mission_events(
                    cur,
                    cfg_dir,
                    ws,
                    actor,
                    after=after,
                    limit=limit,
                    mission_id=mission_id,
                )
        except (psycopg.Error, ValueError) as err:
            raise WorkStoreError("unavailable") from err

    if hasattr(target, "execute"):
        cur = target
        cfg_dir = args[0] if len(args) > 2 else config_dir
        ws = args[1] if len(args) > 2 else args[0]
        actor = args[2] if len(args) > 2 else (args[1] if len(args) > 1 else ws)
        return execute_mission_events(
            cur,
            cfg_dir,
            ws,
            actor,
            after=after,
            limit=limit,
            mission_id=mission_id,
        )

    ws = target
    actor = args[0] if args else ws
    raise WorkStoreError("unavailable")


class _SubscriptionDict(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _domain_watermark(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS wm"
        " FROM omp_audit.domain_events"
        " WHERE workspace_id = %s",
        (workspace_id,),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row["wm"])
    return int(row[0])


def _load_subscriptions(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
) -> dict[str, dict[str, object]]:
    cur.execute(
        "SELECT event_type, payload"
        " FROM omp_audit.domain_events"
        " WHERE workspace_id = %s"
        "   AND outcome = 'applied'"
        "   AND event_type IN ("
        "     'put_event_subscription',"
        "     'delete_event_subscription',"
        "     'advance_event_cursor'"
        "   )"
        " ORDER BY sequence ASC",
        (workspace_id,),
    )
    records: dict[str, dict[str, object]] = {}
    for row in cur.fetchall():
        payload = row["payload"] if isinstance(row, dict) else row[1]
        if isinstance(payload, str):
            try:
                body = json.loads(payload)
            except (ValueError, TypeError):
                continue
        elif isinstance(payload, dict):
            body = payload
        else:
            continue
        sub = body.get("subscription")
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get("subscription_id"))
        records[sub_id] = sub
    return records


def put_event_subscription(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID | None = None,
    actor_kind: str | None = None,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, PutEventSubscriptionCommand):
        raise TypeError(
            "put_event_subscription dispatched with a non-put_event_subscription command"
        )
    payload = command.payload
    workspace_id = envelope.workspace_id
    sub_id = payload.subscription_id

    records = _load_subscriptions(cur, workspace_id)
    existing = records.get(str(sub_id))

    if existing is not None:
        if existing.get("deleted") is True:
            raise WorkStoreError("revision_conflict", ("subscription_deleted",))
        if payload.client_id is not None and str(payload.client_id) != str(
            existing.get("client_id")
        ):
            raise WorkStoreError("invalid_request", ("client_id_immutable",))
        resolved_client_id = UUID(str(existing["client_id"]))
        cursor_sequence = int(existing["cursor_sequence"])
    else:
        resolved_client_id = (
            payload.client_id if payload.client_id is not None else actor_id
        )
        if resolved_client_id is None:
            raise WorkStoreError("invalid_request", ("missing_client_id",))
        if not isinstance(resolved_client_id, UUID):
            resolved_client_id = UUID(str(resolved_client_id))
        cursor_sequence = _domain_watermark(cur, workspace_id)

    sub_view = EventSubscriptionView(
        subscription_id=sub_id,
        client_id=resolved_client_id,
        push_url=payload.push_url,
        event_types=payload.event_types,
        cursor_sequence=cursor_sequence,
        deleted=False,
    )
    return {
        "type": "put_event_subscription",
        "subscription": sub_view.model_dump(mode="json"),
    }


def delete_event_subscription(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID | None = None,
    actor_kind: str | None = None,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, DeleteEventSubscriptionCommand):
        raise TypeError(
            "delete_event_subscription dispatched with a non-delete_event_subscription command"
        )
    payload = command.payload
    workspace_id = envelope.workspace_id
    sub_id = payload.subscription_id

    records = _load_subscriptions(cur, workspace_id)
    existing = records.get(str(sub_id))
    if existing is None:
        raise WorkStoreError("invalid_request", ("subscription_not_found",))
    if existing.get("deleted") is True:
        raise WorkStoreError("revision_conflict", ("subscription_deleted",))

    sub_view = EventSubscriptionView(
        subscription_id=sub_id,
        client_id=UUID(str(existing["client_id"])),
        push_url=existing.get("push_url"),
        event_types=tuple(existing["event_types"]),
        cursor_sequence=int(existing["cursor_sequence"]),
        deleted=True,
    )
    return {
        "type": "delete_event_subscription",
        "subscription": sub_view.model_dump(mode="json"),
    }


def advance_event_cursor(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID | None = None,
    actor_kind: str | None = None,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, AdvanceEventCursorCommand):
        raise TypeError(
            "advance_event_cursor dispatched with a non-advance_event_cursor command"
        )
    payload = command.payload
    workspace_id = envelope.workspace_id
    sub_id = payload.subscription_id

    records = _load_subscriptions(cur, workspace_id)
    existing = records.get(str(sub_id))
    if existing is None:
        raise WorkStoreError("invalid_request", ("subscription_not_found",))
    if existing.get("deleted") is True:
        raise WorkStoreError("revision_conflict", ("subscription_deleted",))

    current_cursor = int(existing["cursor_sequence"])
    after_seq = payload.after_sequence
    watermark = _domain_watermark(cur, workspace_id)

    if after_seq < current_cursor:
        raise WorkStoreError("revision_conflict", ("cursor_regression",))
    if after_seq > watermark:
        raise WorkStoreError("invalid_request", ("cursor_ahead",))

    sub_view = EventSubscriptionView(
        subscription_id=sub_id,
        client_id=UUID(str(existing["client_id"])),
        push_url=existing.get("push_url"),
        event_types=tuple(existing["event_types"]),
        cursor_sequence=after_seq,
        deleted=False,
    )
    return {
        "type": "advance_event_cursor",
        "subscription": sub_view.model_dump(mode="json"),
    }


def list_subscriptions(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    client_id: UUID | None = None,
) -> list[dict[str, object]]:
    records = _load_subscriptions(cur, workspace_id)
    result: list[dict[str, object]] = []
    target_client_id = str(client_id) if client_id is not None else None
    for sub in records.values():
        if sub.get("deleted") is True:
            continue
        if target_client_id is not None and str(sub.get("client_id")) != target_client_id:
            continue
        result.append(_SubscriptionDict(sub))
    return result


def execute_event_subscriptions(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    client_id: UUID | None = None,
) -> dict[str, object]:
    subs = list_subscriptions(cur, workspace_id, client_id=client_id)
    return {
        "subscriptions": tuple(subs),
    }


def event_subscriptions(
    target: Any,
    *args: Any,
    client_id: UUID | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    if hasattr(target, "_transaction"):
        ws = args[0]
        actor = args[1] if len(args) > 1 else ws
        try:
            with target._transaction(ws, actor) as cur:
                return execute_event_subscriptions(
                    cur,
                    ws,
                    client_id=client_id,
                )
        except (psycopg.Error, ValueError) as err:
            raise WorkStoreError("unavailable") from err

    if hasattr(target, "execute"):
        cur = target
        ws = args[0]
        return execute_event_subscriptions(
            cur,
            ws,
            client_id=client_id,
        )

    ws = target
    raise WorkStoreError("unavailable")
