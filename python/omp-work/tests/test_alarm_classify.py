"""Tests for pure alarm classification and daily digest builder (OMP-406-s04)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from uuid import UUID, uuid4

import pytest

from omp_work.alarm_classify import (
    ALERT_KINDS,
    REPEATED_FAILURE_THRESHOLD,
    AlarmState,
    Alert,
    build_digest,
    classify,
)
from omp_work.v1.api_models import DomainEventView
from omp_work.v1.models import OWNER_APPROVAL_COMMAND_TYPES, OWNER_APPROVAL_REFUSED_EVENT


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


def test_alert_kinds_and_threshold_constants() -> None:
    expected_kinds = {
        "cost_threshold",
        "owner_approval_attempt",
        "safety_check_failed",
        "budget_exceeded",
        "repeated_failure",
        "credential_appeared",
    }
    assert ALERT_KINDS == expected_kinds
    assert REPEATED_FAILURE_THRESHOLD == 3


def test_each_alert_kind_from_minimal_event() -> None:
    ws_id = uuid4()
    agg_id = uuid4()

    # 1. cost_threshold
    ev_cost = make_event(
        sequence=1,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type="record_alarm_signal",
        payload={"signal": "cost_threshold"},
    )
    alerts, rest, state = classify([ev_cost], AlarmState(after_sequence=0))
    assert len(alerts) == 1
    assert alerts[0].kind == "cost_threshold"
    assert len(rest) == 0

    # 2. budget_exceeded
    ev_budget = make_event(
        sequence=2,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type="record_alarm_signal",
        payload={"signal": "budget_exceeded"},
    )
    alerts, rest, state = classify([ev_budget], state)
    assert len(alerts) == 1
    assert alerts[0].kind == "budget_exceeded"
    assert len(rest) == 0

    # 3. credential_appeared
    ev_cred = make_event(
        sequence=3,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type="record_alarm_signal",
        payload={"signal": "credential_appeared"},
    )
    alerts, rest, state = classify([ev_cred], state)
    assert len(alerts) == 1
    assert alerts[0].kind == "credential_appeared"
    assert len(rest) == 0

    # 4. safety_check_failed via record_alarm_signal
    ev_safety_sig = make_event(
        sequence=4,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type="record_alarm_signal",
        payload={"signal": "safety_check_failed"},
    )
    alerts, rest, state = classify([ev_safety_sig], state)
    assert len(alerts) == 1
    assert alerts[0].kind == "safety_check_failed"
    assert len(rest) == 0

    # safety_check_failed via other refused outcome
    ev_safety_refused = make_event(
        sequence=5,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type="complete_work",
        outcome="refused",
        payload={"code": "completion_blocked"},
    )
    alerts, rest, state = classify([ev_safety_refused], state)
    # Note: 2 failures so far on agg_id (seq 4 and 5), so no repeated_failure yet
    assert len(alerts) == 1
    assert alerts[0].kind == "safety_check_failed"
    assert len(rest) == 0

    # 5. owner_approval_attempt via owner-approval command types
    for cmd_type in sorted(OWNER_APPROVAL_COMMAND_TYPES):
        state = AlarmState(after_sequence=state.after_sequence, failures={})
        ev_owner = make_event(
            sequence=state.after_sequence + 1,
            workspace_id=ws_id,
            aggregate_id=agg_id,
            event_type=cmd_type,
            outcome="applied",
            payload={},
        )
        alerts, rest, state = classify([ev_owner], state)
        assert len(alerts) == 1
        assert alerts[0].kind == "owner_approval_attempt"
        assert len(rest) == 0

    # owner_approval_attempt via OWNER_APPROVAL_REFUSED_EVENT
    ev_owner_refused = make_event(
        sequence=state.after_sequence + 1,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_type=OWNER_APPROVAL_REFUSED_EVENT,
        outcome="refused",
        payload={"code": "forbidden"},
    )
    alerts, rest, state = classify([ev_owner_refused], state)
    assert alerts[0].kind == "owner_approval_attempt"

    # 6. repeated_failure (threshold 3)
    # Reset failures and cause 3 refusals on fresh aggregate
    fresh_agg = uuid4()
    f1 = make_event(sequence=100, aggregate_id=fresh_agg, event_type="set_work_state", outcome="refused")
    f2 = make_event(sequence=101, aggregate_id=fresh_agg, event_type="set_work_state", outcome="refused")
    f3 = make_event(sequence=102, aggregate_id=fresh_agg, event_type="set_work_state", outcome="refused")
    alerts, rest, state = classify([f1, f2, f3], AlarmState(after_sequence=99))
    kinds = [a.kind for a in alerts]
    assert "repeated_failure" in kinds
    rep = [a for a in alerts if a.kind == "repeated_failure"][0]
    assert rep.related_sequences == (100, 101, 102)


def test_create_work_batch_to_rest() -> None:
    ws_id = uuid4()
    work_id_1 = uuid4()
    work_id_2 = uuid4()
    ev_batch = make_event(
        sequence=1,
        workspace_id=ws_id,
        event_type="create_work_batch",
        outcome="applied",
        payload={
            "type": "create_work_batch",
            "items": [{"work_id": str(work_id_1)}, {"work_id": str(work_id_2)}],
        },
    )
    alerts, rest, state = classify([ev_batch], AlarmState(after_sequence=0))
    assert alerts == []
    assert rest == [ev_batch]
    assert state.after_sequence == 1
    assert state.failures == {}


def test_repeated_failure_third_and_fourth_and_aggregates_apart() -> None:
    agg1 = uuid4()
    agg2 = uuid4()

    # 1st refusal on agg1 -> 1 safety_check_failed alert, 0 repeated_failure
    ev1 = make_event(sequence=1, aggregate_id=agg1, event_type="op", outcome="refused")
    alerts1, rest1, state1 = classify([ev1], AlarmState(after_sequence=0))
    assert [a.kind for a in alerts1] == ["safety_check_failed"]
    assert state1.failures[agg1] == [1]

    # 2nd refusal on agg1 -> 1 safety_check_failed alert, 0 repeated_failure
    ev2 = make_event(sequence=2, aggregate_id=agg1, event_type="op", outcome="refused")
    alerts2, rest2, state2 = classify([ev2], state1)
    assert [a.kind for a in alerts2] == ["safety_check_failed"]
    assert state2.failures[agg1] == [1, 2]

    # 1st refusal on agg2 -> 1 safety_check_failed alert, agg2 counted apart
    ev3 = make_event(sequence=3, aggregate_id=agg2, event_type="op", outcome="refused")
    alerts3, rest3, state3 = classify([ev3], state2)
    assert [a.kind for a in alerts3] == ["safety_check_failed"]
    assert state3.failures[agg1] == [1, 2]
    assert state3.failures[agg2] == [3]

    # 3rd refusal on agg1 -> safety_check_failed AND repeated_failure!
    ev4 = make_event(sequence=4, aggregate_id=agg1, event_type="op", outcome="refused")
    alerts4, rest4, state4 = classify([ev4], state3)
    assert len(alerts4) == 2
    assert alerts4[0].kind == "safety_check_failed"
    assert alerts4[1].kind == "repeated_failure"
    assert alerts4[1].related_sequences == (1, 2, 4)
    assert alerts4[1].aggregate_id == agg1
    assert state4.failures[agg1] == [1, 2, 4]

    # 4th refusal on agg1 -> only safety_check_failed, NO repeated_failure (4th none)
    ev5 = make_event(sequence=5, aggregate_id=agg1, event_type="op", outcome="refused")
    alerts5, rest5, state5 = classify([ev5], state4)
    assert len(alerts5) == 1
    assert alerts5[0].kind == "safety_check_failed"
    assert state5.failures[agg1] == [1, 2, 4, 5]

    # 2nd refusal on agg2 -> no repeated_failure yet
    ev6 = make_event(sequence=6, aggregate_id=agg2, event_type="op", outcome="refused")
    alerts6, rest6, state6 = classify([ev6], state5)
    assert len(alerts6) == 1
    assert alerts6[0].kind == "safety_check_failed"

    # 3rd refusal on agg2 -> adds repeated_failure for agg2!
    ev7 = make_event(sequence=7, aggregate_id=agg2, event_type="op", outcome="refused")
    alerts7, rest7, state7 = classify([ev7], state6)
    assert len(alerts7) == 2
    assert alerts7[0].kind == "safety_check_failed"
    assert alerts7[1].kind == "repeated_failure"
    assert alerts7[1].related_sequences == (3, 6, 7)
    assert alerts7[1].aggregate_id == agg2


def test_state_json_round_trip() -> None:
    agg1 = uuid4()
    agg2 = uuid4()
    state = AlarmState(
        after_sequence=42,
        failures={agg1: [10, 20, 30], agg2: [40]},
    )

    serialized = state.to_json()
    assert isinstance(serialized, str)
    raw = json.loads(serialized)
    assert raw["after_sequence"] == 42
    assert raw["failures"][str(agg1)] == [10, 20, 30]
    assert raw["failures"][str(agg2)] == [40]

    restored = AlarmState.from_json(serialized)
    assert restored.after_sequence == state.after_sequence
    assert restored.failures == state.failures
    assert restored == state

    # Also test from_json accepting dict
    restored_from_dict = AlarmState.from_json(raw)
    assert restored_from_dict == state


def test_two_batches_equal_one() -> None:
    agg_work = uuid4()
    agg_other = uuid4()

    events = [
        make_event(sequence=1, aggregate_id=agg_work, event_type="create_work_batch"),
        make_event(sequence=2, aggregate_id=agg_work, event_type="revise_work", outcome="refused"),
        make_event(sequence=3, aggregate_id=agg_other, event_type="record_alarm_signal", payload={"signal": "cost_threshold"}),
        make_event(sequence=4, aggregate_id=agg_work, event_type="set_work_state", outcome="refused"),
        make_event(sequence=5, aggregate_id=agg_work, event_type="complete_work", outcome="refused"),
        make_event(sequence=6, aggregate_id=agg_other, event_type="record_alarm_signal", payload={"signal": "budget_exceeded"}),
    ]

    initial_state = AlarmState(after_sequence=0)

    # Run in two batches
    alerts_a, rest_a, state_a = classify(events[:3], initial_state)
    alerts_b, rest_b, state_b = classify(events[3:], state_a)

    # Run in one batch
    alerts_all, rest_all, state_all = classify(events, initial_state)

    assert alerts_a + alerts_b == alerts_all
    assert rest_a + rest_b == rest_all
    assert state_b == state_all
    assert state_b.after_sequence == 6


def test_digest_counts_only_that_days_rest_with_range_and_hash() -> None:
    ws_id = uuid4()
    item1 = uuid4()
    item2 = uuid4()

    day1 = datetime(2026, 9, 27, 22, 0, 0, tzinfo=timezone.utc)
    day2_a = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    day2_b = datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc)
    day2_c = datetime(2026, 9, 28, 20, 0, 0, tzinfo=timezone.utc)
    day3 = datetime(2026, 9, 29, 2, 0, 0, tzinfo=timezone.utc)

    rest_events = [
        make_event(sequence=10, aggregate_id=item1, event_type="e1", outcome="applied", occurred_at=day1, event_sha256="1" * 64),
        make_event(sequence=12, aggregate_id=item1, event_type="create_work_batch", outcome="applied", occurred_at=day2_a, event_sha256="2" * 64, payload={"items": [{"work_id": str(item1)}]}),
        make_event(sequence=15, aggregate_id=item2, event_type="revise_work", outcome="applied", occurred_at=day2_b, event_sha256="3" * 64),
        make_event(sequence=18, aggregate_id=item1, event_type="revise_work", outcome="applied", occurred_at=day2_c, event_sha256="4" * 64),
        make_event(sequence=22, aggregate_id=item2, event_type="e3", outcome="applied", occurred_at=day3, event_sha256="5" * 64),
    ]

    # Target day: 2026-09-28
    digest = build_digest(ws_id, "2026-09-28", rest_events, alert_count=4)
    assert digest["type"] == "digest"
    assert digest["workspace_id"] == str(ws_id)
    assert digest["day"] == "2026-09-28"
    assert digest["event_count"] == 3
    assert digest["by_event_type"] == {"create_work_batch": 1, "revise_work": 2}
    assert digest["by_outcome"] == {"applied": 3}
    assert digest["work_items"] == 2
    assert digest["first_sequence"] == 12
    assert digest["last_sequence"] == 18
    assert digest["last_event_sha256"] == "4" * 64
    assert digest["alerts"] == 4

    # Empty day test
    empty_digest = build_digest(ws_id, "2026-09-30", rest_events, alert_count=0)
    assert empty_digest == {
        "type": "digest",
        "workspace_id": str(ws_id),
        "day": "2026-09-30",
        "event_count": 0,
        "by_event_type": {},
        "by_outcome": {},
        "work_items": 0,
        "first_sequence": None,
        "last_sequence": None,
        "last_event_sha256": None,
        "alerts": 0,
    }


def test_alert_event_fields_match_source() -> None:
    ws_id = uuid4()
    agg_id = uuid4()
    ev_id = uuid4()
    sha = "f" * 64
    occurred = datetime(2026, 9, 28, 15, 30, 0, tzinfo=timezone.utc)

    source_ev = make_event(
        sequence=10,
        workspace_id=ws_id,
        aggregate_id=agg_id,
        event_id=ev_id,
        event_type="record_alarm_signal",
        event_sha256=sha,
        occurred_at=occurred,
        payload={
            "signal": "cost_threshold",
            "subject": "80% budget limit reached",
            "detail": "800/1000 tokens",
        },
    )

    alerts, rest, state = classify([source_ev], AlarmState(after_sequence=9))
    assert len(alerts) == 1
    alert = alerts[0]

    # Direct fields
    assert alert.kind == "cost_threshold"
    assert alert.workspace_id == ws_id
    assert alert.summary == "80% budget limit reached"
    assert alert.related_sequences == (10,)
    assert alert.event_id == ev_id
    assert alert.sequence == 10
    assert alert.event_sha256 == sha
    assert alert.event_type == "record_alarm_signal"
    assert alert.aggregate_id == agg_id
    assert alert.occurred_at == occurred
    assert alert.idempotency_key == f"{ev_id}:cost_threshold"

    # body() dictionary
    b = alert.body()
    assert b["type"] == "alert"
    assert b["kind"] == "cost_threshold"
    assert b["workspace_id"] == str(ws_id)
    assert b["summary"] == "80% budget limit reached"
    assert b["related_sequences"] == [10]
    assert b["idempotency_key"] == f"{ev_id}:cost_threshold"
    assert b["event"] == {
        "event_id": str(ev_id),
        "sequence": 10,
        "event_sha256": sha,
        "event_type": "record_alarm_signal",
        "aggregate_id": str(agg_id),
        "occurred_at": occurred.isoformat(),
    }


def test_classify_validates_ascending_sequence() -> None:
    ev1 = make_event(sequence=5)
    state = AlarmState(after_sequence=5)
    with pytest.raises(ValueError, match="sequence 5 <= after_sequence 5"):
        classify([ev1], state)

    ev_low = make_event(sequence=4)
    with pytest.raises(ValueError, match="sequence 4 <= after_sequence 5"):
        classify([ev_low], state)

    ev6 = make_event(sequence=6)
    ev5_dup = make_event(sequence=5)
    with pytest.raises(ValueError):
        classify([ev6, ev5_dup], AlarmState(after_sequence=0))


def test_classify_does_not_mutate_state() -> None:
    agg = uuid4()
    initial_failures = {agg: [1, 2]}
    old_state = AlarmState(after_sequence=2, failures=initial_failures)

    ev3 = make_event(sequence=3, aggregate_id=agg, outcome="refused")
    alerts, rest, new_state = classify([ev3], old_state)

    # old_state must be unmodified
    assert old_state.after_sequence == 2
    assert old_state.failures[agg] == [1, 2]

    # new_state is updated
    assert new_state.after_sequence == 3
    assert new_state.failures[agg] == [1, 2, 3]
