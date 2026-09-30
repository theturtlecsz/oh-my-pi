"""OMP-415-s08-s02: serve ops.alarm subscriptions in the push runner.

An ``event_types == ["ops.alarm"]`` row is no longer skipped: its every domain
event is replayed into alerts and only the alerts past the row's cursor are
sent, with Idempotency-Key ``f"{event_id}:{kind}"``. A rerun sends none; a
mid-run send failure advances only to the failed alert's sequence minus one, so
the next run resends that alert with the same key and not the earlier ones; a
refused destination sends nothing and advances nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from omp_work.event_push import run_push, subscription_key
from omp_work.v1.api_models import DomainEventView

MASTER = bytes(range(32))
WORKSPACE = UUID("00000000-0000-7000-8000-0000000004f0")
SUB_A = UUID("00000000-0000-7000-8000-0000000004a1")


def _event(
    sequence: int,
    *,
    event_type: str = "record_alarm_signal",
    payload: dict[str, Any] | None = None,
    outcome: str = "applied",
    aggregate_id: UUID | None = None,
    event_id: UUID | None = None,
) -> DomainEventView:
    return DomainEventView(
        event_id=event_id or uuid5(NAMESPACE_URL, f"ops-ev:{sequence}"),
        sequence=sequence,
        workspace_id=WORKSPACE,
        aggregate_type="work_item",
        aggregate_id=aggregate_id or uuid4(),
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="automation",
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
        event_sha256="a" * 64,
        occurred_at=datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc),
    )


def _signal(sequence: int, kind: str, *, aggregate_id: UUID | None = None) -> DomainEventView:
    return _event(
        sequence,
        payload={"signal": kind, "reason": kind},
        aggregate_id=aggregate_id,
    )


def _refused(sequence: int, aggregate_id: UUID) -> DomainEventView:
    return _event(
        sequence,
        event_type="revise_work",
        payload={"reason": "refused"},
        outcome="refused",
        aggregate_id=aggregate_id,
    )


def _subscription(
    subscription_id: UUID = SUB_A,
    *,
    cursor: int = 0,
    push_url: str = "https://hooks.example/alarm",
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription_id),
        "client_id": str(uuid4()),
        "push_url": push_url,
        "event_types": event_types if event_types is not None else ["ops.alarm"],
        "cursor_sequence": cursor,
        "deleted": False,
    }


class _Client:
    """Fake client: subscription rows, paged domain events, applied cursors."""

    def __init__(
        self,
        subscriptions: list[dict[str, Any]],
        events: list[DomainEventView],
        *,
        page_size: int = 500,
    ) -> None:
        self.subscriptions = [dict(row) for row in subscriptions]
        self.events_all = list(events)
        self.page_size = page_size
        self.advances: list[tuple[str, int]] = []
        self.event_calls: list[int] = []

    def event_subscriptions(self) -> dict[str, Any]:
        return {"subscriptions": [dict(row) for row in self.subscriptions]}

    def events(self, after_sequence: int = 0, limit: int = 500) -> dict[str, Any]:
        self.event_calls.append(after_sequence)
        remaining = [e for e in self.events_all if e.sequence > after_sequence]
        effective = min(limit, self.page_size)
        page = remaining[:effective]
        return {
            "events": tuple(page),
            "watermark_sequence": (
                max((e.sequence for e in self.events_all), default=0)
            ),
            "next_after_sequence": page[-1].sequence if page else after_sequence,
            "has_more": len(remaining) > len(page),
        }

    def execute(self, envelope: Any) -> Any:
        payload = envelope.command.payload
        sub_id = str(payload.subscription_id)
        after = int(payload.after_sequence)
        self.advances.append((sub_id, after))
        for row in self.subscriptions:
            if row["subscription_id"] == sub_id:
                row["cursor_sequence"] = after
        return {"command": envelope.command.type}


class _Sender:
    def __init__(
        self, fail_on: str | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.keys_used: list[bytes] = []
        self.fail_on = fail_on
        self.error = error or RuntimeError("boom")

    def __call__(self, url: str, idem: str, body: Any, *, key: bytes) -> None:
        if self.fail_on is not None and idem == self.fail_on:
            raise self.error
        self.calls.append((url, idem, body))
        self.keys_used.append(key)

    def keys(self) -> list[str]:
        return [idem for _url, idem, _body in self.calls]


def _run(client: _Client, send: Any) -> dict[str, Any]:
    return run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"hooks.example"},
        resolve=lambda _host: ["8.8.8.8"],
        send=send,
    )


def test_alerts_past_cursor_sent_once_with_event_kind_keys_then_rerun_sends_none() -> None:
    ev1 = _signal(1, "cost_threshold")
    ev2 = _signal(2, "budget_exceeded")
    ev3 = _signal(3, "safety_check_failed")
    client = _Client([_subscription(cursor=0)], [ev1, ev2, ev3], page_size=2)
    sender = _Sender()

    result = _run(client, sender)

    assert result == {"pushed": 3}
    assert sender.keys() == [
        f"{ev1.event_id}:cost_threshold",
        f"{ev2.event_id}:budget_exceeded",
        f"{ev3.event_id}:safety_check_failed",
    ]
    assert [url for url, _idem, _body in sender.calls] == ["https://hooks.example/alarm"] * 3
    # each alert is signed with the row's per-subscription key
    assert sender.keys_used == [subscription_key(MASTER, SUB_A)] * 3
    # all pages were scanned (one state carried across the 2+1 split) and the
    # cursor landed on the last scanned event sequence
    assert client.event_calls == [0, 2]
    assert client.advances == [(str(SUB_A), 3)]

    rerun_sender = _Sender()
    assert _run(client, rerun_sender) == {"pushed": 0}
    assert rerun_sender.calls == []
    assert client.advances == [(str(SUB_A), 3)]


def test_repeated_failure_above_cursor_sent_when_earlier_refusals_are_below() -> None:
    aggregate = uuid4()
    ev1 = _refused(1, aggregate)
    ev2 = _refused(2, aggregate)
    ev3 = _refused(3, aggregate)
    # cursor sits at the second refusal: its safety_check_failed is below, the
    # third refusal's repeated_failure is above
    client = _Client([_subscription(cursor=2)], [ev1, ev2, ev3])
    sender = _Sender()

    result = _run(client, sender)

    assert result == {"pushed": 2}
    assert sender.keys() == [
        f"{ev3.event_id}:safety_check_failed",
        f"{ev3.event_id}:repeated_failure",
    ]
    assert client.advances == [(str(SUB_A), 3)]
    assert sender.keys_used == [subscription_key(MASTER, SUB_A)] * 2


def test_send_fails_on_second_of_three_alerts_and_next_run_resends_only_it() -> None:
    ev1 = _signal(1, "cost_threshold")
    ev2 = _signal(2, "budget_exceeded")
    ev3 = _signal(3, "safety_check_failed")
    client = _Client([_subscription(cursor=0)], [ev1, ev2, ev3], page_size=2)
    second_key = f"{ev2.event_id}:budget_exceeded"
    sender = _Sender(fail_on=second_key)

    result = _run(client, sender)

    assert result == {"failed": "boom"}
    # first alert delivered; the failed second one's sequence - 1 == 1
    assert sender.keys() == [f"{ev1.event_id}:cost_threshold"]
    assert client.advances == [(str(SUB_A), 1)]

    rerun_sender = _Sender()
    assert _run(client, rerun_sender) == {"pushed": 2}
    assert rerun_sender.keys() == [second_key, f"{ev3.event_id}:safety_check_failed"]
    assert client.advances[-1] == (str(SUB_A), 3)


def test_refused_ops_destination_sends_nothing_and_advances_nothing() -> None:
    ev1 = _signal(1, "cost_threshold")
    client = _Client(
        [_subscription(cursor=0, push_url="http://hooks.example/alarm")],
        [ev1],
    )
    sender = _Sender()

    result = _run(client, sender)

    assert result == {"refused": "scheme"}
    assert sender.calls == []
    assert client.advances == []
    assert client.event_calls == []
