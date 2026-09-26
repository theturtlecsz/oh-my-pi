from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from omp_work.v1.api_models import DomainEventsPage, DomainEventView


def make_event(
    *,
    sequence: int,
    event_type: str = "complete_work",
    workspace_id: UUID | None = None,
    aggregate_id: UUID | None = None,
    outcome: str = "applied",
    payload: dict[str, Any] | None = None,
) -> DomainEventView:
    return DomainEventView(
        event_id=uuid4(),
        sequence=sequence,
        workspace_id=workspace_id or uuid4(),
        aggregate_type="work_item",
        aggregate_id=aggregate_id or uuid4(),
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="owner",
        capability_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        operation_id=uuid4(),
        causation_id=uuid4(),
        event_type=event_type,
        outcome=outcome,
        payload=payload if payload is not None else {"data": {"state": "DONE"}},
        payload_sha256="a" * 64,
        previous_event_sha256=None,
        event_sha256="b" * 64,
        occurred_at=datetime.now(timezone.utc),
    )


class FakeEvents:
    """In-memory ``NativeEvents`` double over a fixed, sequence-ordered stream."""

    def __init__(self, events: Sequence[DomainEventView]) -> None:
        self._events = sorted(events, key=lambda event: event.sequence)
        self.calls: list[tuple[int, int]] = []

    def events(self, after_sequence: int = 0, limit: int = 500) -> DomainEventsPage:
        self.calls.append((after_sequence, limit))
        selected = [e for e in self._events if e.sequence > after_sequence][:limit]
        watermark = max((e.sequence for e in self._events), default=0)
        next_after = selected[-1].sequence if selected else after_sequence
        return DomainEventsPage(
            events=tuple(selected),
            watermark_sequence=watermark,
            next_after_sequence=next_after,
            has_more=False,
        )


__all__ = ["FakeEvents", "make_event"]
