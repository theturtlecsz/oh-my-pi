"""Alarm and digest dispatcher sending domain event alerts to Grokbot (OMP-406)."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.alarm_classify import (
    AlarmState,
    Alert,
    build_digest,
    classify,
    _normalize_day,
    _to_utc_date,
)

__all__ = [
    "AlarmStateMissing",
    "init_alarms",
    "run_alarms",
    "run_digest",
]


class AlarmStateMissing(Exception):
    """Raised when the alarm state file does not exist."""


def _save_state(state_path: Path, state: AlarmState) -> None:
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{state_path.name}.", dir=state_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(state.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _fetch_events(client: Any, after: int, limit: int = 500) -> Any:
    try:
        return client.events(after=after, limit=limit)
    except TypeError:
        return client.events(after_sequence=after, limit=limit)


def _unpack_page(page: Any) -> tuple[Any, bool]:
    if isinstance(page, dict):
        events = page.get("events", ())
        has_more = bool(page.get("has_more", False))
    else:
        events = getattr(page, "events", ())
        has_more = bool(getattr(page, "has_more", False))
    return events, has_more


def _unpack_watermark(page: Any) -> int:
    if isinstance(page, dict):
        wm = page.get("watermark_sequence", 0)
    else:
        wm = getattr(page, "watermark_sequence", 0)
    return int(wm if wm is not None else 0)


def init_alarms(client: Any, state_path: str | Path) -> AlarmState:
    """Initialize alarm state at the current watermark sequence.

    State is written atomically with mode 0600.
    If the state file already exists, FileExistsError is raised.
    """
    path = Path(state_path)
    if path.exists():
        raise FileExistsError(f"Alarm state file already exists: {path}")

    page = _fetch_events(client, after=0, limit=1)
    watermark = _unpack_watermark(page)
    state = AlarmState(after_sequence=watermark)

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(state.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except (AttributeError, OSError):
            if path.exists():
                raise FileExistsError(f"Alarm state file already exists: {path}")
            os.replace(temporary, path)
        else:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    except FileExistsError:
        raise
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

    return state


def run_alarms(
    client: Any,
    state_path: str | Path,
    send: Callable[[str, dict[str, Any]], Any],
) -> int:
    """Run incremental alarm dispatch from the saved state sequence.

    Pages from after_sequence (limit 500) until not has_more.
    For each page: classifies events, sends each alert, then saves state atomically.
    If send raises an error, it propagates with that page unsaved.
    Returns the total number of alerts sent.
    """
    path = Path(state_path)
    if not path.exists():
        raise AlarmStateMissing(f"Alarm state file missing: {path}")

    raw = path.read_text(encoding="utf-8")
    state = AlarmState.from_json(raw)

    alerts_sent = 0

    while True:
        page = _fetch_events(client, after=state.after_sequence, limit=500)
        events, has_more = _unpack_page(page)

        if not events:
            break

        alerts, _rest, updated_state = classify(events, state)
        for alert in alerts:
            send(alert.idempotency_key, alert.body())
            alerts_sent += 1

        _save_state(path, updated_state)
        state = updated_state

        if not has_more:
            break

    return alerts_sent


def run_digest(
    client: Any,
    workspace_id: UUID | str,
    day: date | datetime | str,
    send: Callable[[str, dict[str, Any]], Any],
) -> dict[str, Any]:
    """Build and send a daily operational digest over all events from sequence 0.

    Events are processed with a fresh state. Digest includes only rest events,
    with that day's alert count. Sent with key f"digest:{workspace_id}:{day}".
    Returns the digest body.
    """
    state = AlarmState()
    all_alerts: list[Alert] = []
    all_rest: list[Any] = []
    after_sequence = 0

    while True:
        page = _fetch_events(client, after=after_sequence, limit=500)
        events, has_more = _unpack_page(page)

        if not events:
            break

        alerts, rest, state = classify(events, state)
        all_alerts.extend(alerts)
        all_rest.extend(rest)
        after_sequence = state.after_sequence

        if not has_more:
            break

    target_date, _day_str = _normalize_day(day)
    day_alert_count = sum(
        1 for alert in all_alerts
        if _to_utc_date(alert.occurred_at) == target_date
    )

    body = build_digest(
        workspace_id=workspace_id,
        day=day,
        rest=all_rest,
        alert_count=day_alert_count,
    )

    key = f"digest:{workspace_id}:{day}"
    send(key, body)
    return body
