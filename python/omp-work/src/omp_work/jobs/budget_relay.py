"""Relay ``budget_alert`` outbox rows to ``ops.alarm`` push subscribers (OMP-430).

Budget notices become ``record_alarm_signal`` domain events:
- ``budget.exceeded`` becomes ``budget_exceeded``,
- threshold notices become ``cost_threshold``,
- subject and detail describe the dimension, threshold, spend, and limit.

Rows, locking, and outbox recovery match the native jobs outbox pattern:
pending (``committed`` or ``failed``) rows are locked with ``FOR UPDATE SKIP LOCKED``,
a previously failed row commits again before the attempt, and store errors
leave the row in ``failed`` state for retry under the same deterministic operation id.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omp_work.contracts.v1 import recovery
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.models import (
    CommandEnvelope,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)
from omp_work.v1.store import PostgresWorkStore

__all__ = ["relay_budget_alerts"]

API_VERSION = "work.omp.dev/v1"
_PENDING_STATES = ("committed", "failed")


def relay_budget_alerts(
    job_store: NativeJobStore,
    work_store: PostgresWorkStore,
    *,
    workspace_id: UUID | str,
    actor_id: UUID | str,
) -> int:
    """Relay pending ``budget_alert`` rows into ``work_store`` via ``record_alarm_signal``."""
    if isinstance(workspace_id, str):
        workspace_id = UUID(workspace_id)
    if isinstance(actor_id, str):
        actor_id = UUID(actor_id)

    relayed = 0
    with job_store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            SELECT event_id, state, revision, ack_token, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s AND state = ANY(%s) AND kind = 'budget_alert'
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

            raw_payload = row["payload"]
            if isinstance(raw_payload, str):
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    payload = {}
            elif isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = {}

            event = payload.get("event")
            signal = (
                "budget_exceeded"
                if event == "budget.exceeded"
                else "cost_threshold"
            )
            dimension = payload.get("dimension", "")
            threshold_percent = payload.get("threshold_percent", "")
            spent = payload.get("spent", "")
            limit = payload.get("limit", "")
            work_id_raw = payload.get("work_id")
            work_id = UUID(str(work_id_raw)) if work_id_raw is not None else None

            subject = f"{dimension} budget {threshold_percent}% reached"
            detail = f"spent {spent} of {limit}"

            event_id = str(row["event_id"])
            envelope_id = uuid5(NAMESPACE_URL, f"omp-work:budget-alert:{event_id}")

            envelope = CommandEnvelope(
                api_version=API_VERSION,
                workspace_id=workspace_id,
                operation_id=envelope_id,
                request_id=envelope_id,
                correlation_id=envelope_id,
                command=RecordAlarmSignalCommand(
                    type="record_alarm_signal",
                    payload=RecordAlarmSignalPayload(
                        signal=signal,
                        work_id=work_id,
                        subject=subject,
                        detail=detail,
                    ),
                ),
            )

            try:
                work_store.execute(
                    envelope,
                    actor_id=actor_id,
                    actor_kind="automation",
                    required_scope="work.mutate",
                )
            except Exception:  # noqa: BLE001
                failed = recovery.recover_unacked_commit(
                    record, evidence_trusted=False
                )
                _persist(cur, workspace_id, failed)
                continue

            acknowledged = recovery.recover_unacked_commit(
                record, evidence_trusted=True
            )
            _persist(cur, workspace_id, recovery.close(acknowledged))
            relayed += 1

    return relayed


def _persist(cur: Any, workspace_id: UUID, record: recovery.CloseoutRecord) -> None:
    cur.execute(
        """
        UPDATE omp_jobs.outbox
        SET state=%s, revision=%s, ack_token=%s, updated_at=clock_timestamp()
        WHERE event_id=%s AND workspace_id=%s
        """,
        (record.state, record.revision, record.ack_token, record.id, workspace_id),
    )
