"""Deliver the WP5 usage outbox to the file ledger (R03, OMP-324).

``record_usage`` writes one ``omp_jobs.outbox`` row per usage identity in state
``committed``. This module drains the ``usage_ledger`` rows of one workspace to
the workspace's usage ledger file; other outbox kinds (for example the
``budget_alert`` rows written by ``jobs.budget``) have no file-ledger effect and
stay untouched. Delivery is at-least-once with an idempotent effect: the ledger
is keyed by ``usage_id``, so a row whose event already reached the ledger is
closed without appending again. State changes reuse the closeout helpers in
``omp_work.contracts.v1.recovery``; there is no second state machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.contracts.v1 import recovery
from omp_work.jobs.store import NativeJobStore
from omp_work.research_ledger import append_ledger_event

__all__ = ["deliver_outbox"]

_PENDING_STATES = ("committed", "failed")


def deliver_outbox(
    store: NativeJobStore,
    *,
    workspace_id: UUID | str,
    actor_id: UUID | str,
    ledger_path: Path | str,
) -> int:
    """Append pending usage outbox events to ``ledger_path`` and close their rows.

    Only ``kind='usage_ledger'`` rows of ``workspace_id`` in ``committed`` or
    ``failed`` state are read in ``event_id`` order under ``FOR UPDATE SKIP
    LOCKED``. A ``failed`` row is committed again first (``apply_commit``,
    revision + 1). An event whose ``usage_id`` is already in the ledger is closed
    without appending. Otherwise the payload is appended and counted; if the
    append raises, the row is returned to ``failed`` for a later retry.

    A crash between the append and the state update leaves the row ``committed``;
    the next call finds the ``usage_id`` in the ledger and closes it without
    appending, so the external effect happens at most once.

    Returns the number of events appended.
    """
    if isinstance(workspace_id, str):
        workspace_id = UUID(workspace_id)
    if isinstance(actor_id, str):
        actor_id = UUID(actor_id)

    appended = 0
    with store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            SELECT event_id, state, revision, ack_token, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s AND state = ANY(%s) AND kind = 'usage_ledger'
            ORDER BY event_id
            FOR UPDATE SKIP LOCKED
            """,
            (workspace_id, list(_PENDING_STATES)),
        )
        for row in cur.fetchall():
            record = recovery.CloseoutRecord(
                id=str(row["event_id"]),
                state=row["state"],
                revision=int(row["revision"]),
                ack_token=row["ack_token"],
            )
            if record.state == "failed":
                record = recovery.apply_commit(record)

            payload = row["payload"]
            usage_id = payload.get("usage_id") if isinstance(payload, dict) else None
            if not _ledger_has_usage(ledger_path, usage_id):
                try:
                    append_ledger_event(ledger_path, payload)
                except Exception:  # noqa: BLE001 - any append failure must fail the row closed for retry
                    failed = recovery.recover_unacked_commit(
                        record, evidence_trusted=False
                    )
                    _persist(cur, workspace_id, failed)
                    continue
                appended += 1

            acknowledged = recovery.recover_unacked_commit(
                record, evidence_trusted=True
            )
            _persist(cur, workspace_id, recovery.close(acknowledged))

    return appended


def _persist(cur: Any, workspace_id: UUID, record: recovery.CloseoutRecord) -> None:
    cur.execute(
        """
        UPDATE omp_jobs.outbox
        SET state=%s, revision=%s, ack_token=%s, updated_at=clock_timestamp()
        WHERE event_id=%s AND workspace_id=%s
        """,
        (record.state, record.revision, record.ack_token, record.id, workspace_id),
    )


def _ledger_has_usage(ledger_path: Path | str, usage_id: object) -> bool:
    """True when the ledger file already holds an event with this ``usage_id``.

    A missing, unreadable, or malformed ledger is reported as absent: the
    append path owns the failure and records it as a retryable ``failed`` state.
    """
    if usage_id is None:
        return False
    path = Path(ledger_path)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if isinstance(data, list):
        events: object = data
    elif isinstance(data, dict):
        events = data.get("events")
    else:
        return False
    if not isinstance(events, list):
        return False
    return any(
        isinstance(event, dict) and event.get("usage_id") == usage_id
        for event in events
    )
