"""Tests for alarm and digest dispatch module (OMP-406)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omp_work.alarm_dispatch import (
    AlarmStateMissing,
    init_alarms,
    run_alarms,
    run_digest,
)
from omp_work.v1.api_models import DomainEventsPage, DomainEventView


def make_event(
    *,
    sequence: int = 1,
    event_type: str = "create_work_batch",
    outcome: str = "applied",
    payload: dict | None = None,
    aggregate_id: UUID | None = None,
    aggregate_type: str = "work_item",
    workspace_id: UUID | None = None,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
    event_sha256: str = "a" * 64,
) -> DomainEventView:
    return DomainEventView(
        event_id=event_id or uuid4(),
        sequence=sequence,
        workspace_id=workspace_id or uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id or uuid4(),
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="agent",
        capability_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        operation_id=uuid4(),
        causation_id=uuid4(),
        event_type=event_type,
        outcome=outcome,
        payload=payload if payload is not None else {},
        payload_sha256="b" * 64,
        previous_event_sha256=None,
        event_sha256=event_sha256,
        occurred_at=occurred_at or datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
    )


class FakeWorkClient:
    def __init__(
        self,
        events: list[DomainEventView] | None = None,
        page_size: int = 500,
    ) -> None:
        self.all_events = list(events) if events else []
        self.page_size = page_size
        self.calls: list[dict[str, Any]] = []

    def events(
        self,
        after_sequence: int = 0,
        limit: int = 500,
        *,
        after: int | None = None,
    ) -> DomainEventsPage:
        effective_after = after if after is not None else after_sequence
        self.calls.append({"after": effective_after, "limit": limit})
        filtered = [e for e in self.all_events if e.sequence > effective_after]
        effective_limit = min(limit, self.page_size)
        page_events = tuple(filtered[:effective_limit])
        has_more = len(filtered) > len(page_events)
        watermark = max((e.sequence for e in self.all_events), default=0)
        next_after = page_events[-1].sequence if page_events else effective_after
        return DomainEventsPage(
            events=page_events,
            watermark_sequence=watermark,
            next_after_sequence=next_after,
            has_more=has_more,
        )


def test_run_before_init_raises(tmp_path: Path) -> None:
    state_path = tmp_path / "alarm_state.json"
    client = FakeWorkClient()
    sent: list[tuple[str, dict[str, Any]]] = []

    with pytest.raises(AlarmStateMissing):
        run_alarms(client, state_path, lambda k, b: sent.append((k, b)))


def test_init_alarms_creates_state_with_mode_600_and_rejects_existing(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "alarm_state.json"
    ev1 = make_event(sequence=1)
    ev2 = make_event(sequence=2)
    client = FakeWorkClient([ev1, ev2])

    state = init_alarms(client, state_path)
    assert state.after_sequence == 2
    assert state_path.is_file()

    mode = state_path.stat().st_mode & 0o777
    assert mode == 0o600

    # Existing file must raise FileExistsError
    with pytest.raises(FileExistsError):
        init_alarms(client, state_path)


def test_old_events_send_nothing_new_ones_alert_in_sequence_order_and_second_run_sends_zero(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "sub" / "alarm_state.json"
    ws_id = uuid4()

    # Pre-existing events (watermark = 2)
    ev1 = make_event(sequence=1, workspace_id=ws_id, event_type="create_work_batch")
    ev2 = make_event(
        sequence=2,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "safety_check_failed", "reason": "pre-existing"},
    )
    client = FakeWorkClient([ev1, ev2])

    init_alarms(client, state_path)

    sent: list[tuple[str, dict[str, Any]]] = []
    count_init_run = run_alarms(client, state_path, lambda k, b: sent.append((k, b)))
    assert count_init_run == 0
    assert len(sent) == 0

    # Add new events: one cost_threshold, one rest event, one budget_exceeded
    ev3 = make_event(
        sequence=3,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "cost_threshold", "reason": "cost high"},
    )
    ev4 = make_event(sequence=4, workspace_id=ws_id, event_type="revise_work")
    ev5 = make_event(
        sequence=5,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "budget_exceeded", "reason": "budget gone"},
    )
    client.all_events.extend([ev3, ev4, ev5])

    count_new_run = run_alarms(client, state_path, lambda k, b: sent.append((k, b)))
    assert count_new_run == 2
    assert len(sent) == 2

    # Alerts sent in sequence order
    assert sent[0][0] == f"{ev3.event_id}:cost_threshold"
    assert sent[0][1]["event"]["sequence"] == 3
    assert sent[0][1]["kind"] == "cost_threshold"

    assert sent[1][0] == f"{ev5.event_id}:budget_exceeded"
    assert sent[1][1]["event"]["sequence"] == 5
    assert sent[1][1]["kind"] == "budget_exceeded"

    # State file mode preserved as 0600
    assert (state_path.stat().st_mode & 0o777) == 0o600

    # Second run immediately sends 0
    second_sent: list[tuple[str, dict[str, Any]]] = []
    count_second_run = run_alarms(
        client, state_path, lambda k, b: second_sent.append((k, b))
    )
    assert count_second_run == 0
    assert len(second_sent) == 0


def test_failure_on_page_2_keeps_page_1_saved_and_rerun_resends_page_2_only(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "alarm_state.json"
    ws_id = uuid4()

    # 4 events, page size 2:
    # Page 1: ev1 (alert), ev2 (alert) -> seq 1, 2
    # Page 2: ev3 (alert), ev4 (alert) -> seq 3, 4
    ev1 = make_event(
        sequence=1,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "cost_threshold"},
    )
    ev2 = make_event(
        sequence=2,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "safety_check_failed"},
    )
    ev3 = make_event(
        sequence=3,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "budget_exceeded"},
    )
    ev4 = make_event(
        sequence=4,
        workspace_id=ws_id,
        event_type="record_alarm_signal",
        payload={"signal": "repeated_failure"},
    )

    client = FakeWorkClient([ev1, ev2, ev3, ev4], page_size=2)

    # Initialize empty state (at sequence 0)
    client.all_events = []
    init_alarms(client, state_path)
    client.all_events = [ev1, ev2, ev3, ev4]

    # Sender fails when encountering sequence 3 (page 2)
    first_run_sent: list[tuple[str, dict[str, Any]]] = []

    def failing_send(key: str, body: dict[str, Any]) -> None:
        if body["event"]["sequence"] >= 3:
            raise RuntimeError("Boom on page 2")
        first_run_sent.append((key, body))

    with pytest.raises(RuntimeError, match="Boom on page 2"):
        run_alarms(client, state_path, failing_send)

    # Page 1 alerts were sent
    assert len(first_run_sent) == 2
    assert first_run_sent[0][1]["event"]["sequence"] == 1
    assert first_run_sent[1][1]["event"]["sequence"] == 2

    # State file on disk must have page 1 saved (after_sequence == 2)
    from omp_work.alarm_classify import AlarmState

    saved_state = AlarmState.from_json(state_path.read_text(encoding="utf-8"))
    assert saved_state.after_sequence == 2

    # Rerun now succeeds
    second_run_sent: list[tuple[str, dict[str, Any]]] = []
    count = run_alarms(
        client, state_path, lambda k, b: second_run_sent.append((k, b))
    )

    # Only page 2 was resent!
    assert count == 2
    assert len(second_run_sent) == 2
    assert second_run_sent[0][1]["event"]["sequence"] == 3
    assert second_run_sent[1][1]["event"]["sequence"] == 4

    # Final state is saved at sequence 4
    final_state = AlarmState.from_json(state_path.read_text(encoding="utf-8"))
    assert final_state.after_sequence == 4


def test_digest_omits_alert_events() -> None:
    ws_id = uuid4()
    item1 = uuid4()
    item2 = uuid4()

    dt_day1 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    dt_day2 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)

    # Day 1: 2 rest events, 2 alert events
    ev1 = make_event(
        sequence=1,
        workspace_id=ws_id,
        aggregate_id=item1,
        event_type="create_work_batch",
        outcome="applied",
        occurred_at=dt_day1,
    )
    ev2 = make_event(
        sequence=2,
        workspace_id=ws_id,
        aggregate_id=item1,
        event_type="record_alarm_signal",
        outcome="applied",
        payload={"signal": "cost_threshold"},
        occurred_at=dt_day1,
    )
    ev3 = make_event(
        sequence=3,
        workspace_id=ws_id,
        aggregate_id=item2,
        event_type="revise_work",
        outcome="refused",  # safety_check_failed alert
        occurred_at=dt_day1,
    )
    ev4 = make_event(
        sequence=4,
        workspace_id=ws_id,
        aggregate_id=item2,
        event_type="revise_work",
        outcome="applied",
        occurred_at=dt_day1,
    )
    # Day 2 event
    ev5 = make_event(
        sequence=5,
        workspace_id=ws_id,
        aggregate_id=item1,
        event_type="record_alarm_signal",
        outcome="applied",
        payload={"signal": "budget_exceeded"},
        occurred_at=dt_day2,
    )

    client = FakeWorkClient([ev1, ev2, ev3, ev4, ev5], page_size=2)

    sent: list[tuple[str, dict[str, Any]]] = []
    body = run_digest(
        client, ws_id, "2026-09-28", lambda k, b: sent.append((k, b))
    )

    # Sent with key f"digest:{ws_id}:2026-09-28"
    assert len(sent) == 1
    assert sent[0][0] == f"digest:{ws_id}:2026-09-28"
    assert sent[0][1] == body

    # Body omits alert events (ev2, ev3), only rest events (ev1, ev4) counted
    assert body["type"] == "digest"
    assert body["workspace_id"] == str(ws_id)
    assert body["day"] == "2026-09-28"
    assert body["event_count"] == 2
    assert body["by_event_type"] == {"create_work_batch": 1, "revise_work": 1}
    assert body["by_outcome"] == {"applied": 2}
    assert body["work_items"] == 2
    assert body["first_sequence"] == 1
    assert body["last_sequence"] == 4

    # Alert count reflects day 1 alerts (ev2, ev3 = 2), ev5 (day 2) is excluded
    assert body["alerts"] == 2
