"""Tamper-evident chain verification for the two orchestrator ledgers (OMP-417, lock 11).

``verify_domain_chain`` recomputes ``payload_sha256`` and ``event_sha256`` for
every applied domain event of an aggregate exactly as ``PostgresWorkStore``
writes them in ``v1/store.py``, and walks ``previous_event_sha256``.
``verify_step_chain`` does the same for the mission's ``orch_step`` events,
whose stored payload is the step mapping plus the two hash fields; only
``event_sha256`` is excluded from the digest, so the chain link
(``previous_event_sha256``) is itself authenticated.

Each returns ``(ok, first bad position)``: the sequence number of the first bad
domain event, or the step index of the first bad step; ``(True, None)`` when the
whole chain verifies. The caller connects as the owner: ``omp_work_app`` cannot
UPDATE or DELETE the immutable rows, so only an owner-side edit is detectable.
"""

from __future__ import annotations

import psycopg

from omp_work.v1.canonical import sha256

__all__ = [
    "verify_domain_chain",
    "verify_step_chain",
]

_STEP_HASH_FIELD = "event_sha256"

_DOMAIN_EVENTS = """
SELECT sequence, aggregate_id, operation_id, payload, payload_sha256,
       previous_event_sha256, event_sha256
FROM omp_audit.domain_events
WHERE workspace_id = %s AND aggregate_id = %s AND outcome = 'applied'
ORDER BY sequence
"""

_STEPS = """
SELECT e.payload
FROM omp_jobs.job_events e
JOIN omp_jobs.jobs j
  ON j.job_id = e.job_id AND j.workspace_id = %s AND j.source = 'native'
WHERE e.kind = 'orch_step' AND e.payload->>'mission_id' = %s
ORDER BY e.seq
"""


def _verify_link(
    payload: object,
    payload_sha256: str,
    previous_event_sha256: str | None,
    event_sha256: str,
    *,
    aggregate_id: str,
    operation_id: str,
) -> bool:
    if sha256(payload) != payload_sha256:
        return False
    expected = sha256(
        {
            "aggregate_id": aggregate_id,
            "operation_id": operation_id,
            "previous_event_sha256": previous_event_sha256,
            "payload_sha256": payload_sha256,
        }
    )
    return expected == event_sha256


def verify_domain_chain(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: object,
    aggregate_id: object,
) -> tuple[bool, int | None]:
    """Whether the aggregate's applied domain events form an unbroken chain."""
    cur.execute(_DOMAIN_EVENTS, (workspace_id, aggregate_id))
    previous: str | None = None
    for row in cur.fetchall():
        if row["previous_event_sha256"] != previous:
            return (False, int(row["sequence"]))
        ok = _verify_link(
            row["payload"],
            str(row["payload_sha256"]),
            row["previous_event_sha256"],
            str(row["event_sha256"]),
            aggregate_id=str(row["aggregate_id"]),
            operation_id=str(row["operation_id"]),
        )
        if not ok:
            return (False, int(row["sequence"]))
        previous = str(row["event_sha256"])
    return (True, None)


def verify_step_chain(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: object,
    mission_id: str,
) -> tuple[bool, int | None]:
    """Whether the mission's ``orch_step`` events form an unbroken chain."""
    cur.execute(_STEPS, (workspace_id, mission_id))
    previous: str | None = None
    for row in cur.fetchall():
        payload = dict(row["payload"])
        step_index = int(payload["step_index"])
        if payload.get("previous_event_sha256") != previous:
            return (False, step_index)
        body = {key: value for key, value in payload.items() if key != _STEP_HASH_FIELD}
        if sha256(body) != payload.get("event_sha256"):
            return (False, step_index)
        previous = str(payload.get("event_sha256"))
    return (True, None)
