"""Hash-chained orchestrator step event log (OMP-417, lock 11).

A step is one ``omp_jobs.job_events`` row of kind ``orch_step`` on an existing
native job, carrying the step mapping plus ``previous_event_sha256`` and
``event_sha256``. ``event_sha256`` is
``omp_work.v1.canonical.sha256`` of the payload without ``event_sha256``; the
chain links each step to the mission's previous one, so a replay identity
``(mission_id, step_index, kind, idempotency_key)`` either repeats an identical
write or is refused as ``revision_conflict``.

Appends serialise per mission with ``pg_advisory_xact_lock`` so two writers
cannot fork the chain. The job row is the isolation check: ``append_event``
refuses a missing job or another workspace's job, so callers insert the job
first and pass its id here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.v1.canonical import sha256

__all__ = [
    "append_step",
    "list_steps",
    "open_intent",
    "STEP_EVENT_KIND",
]

STEP_EVENT_KIND = "orch_step"

# The step fields a replay compares; the two hash fields are bookkeeping.
_HASH_FIELDS = ("previous_event_sha256", "event_sha256")

_STEP_SELECT = """
SELECT e.payload
FROM omp_jobs.job_events e
JOIN omp_jobs.jobs j
  ON j.job_id = e.job_id AND j.workspace_id = %s AND j.source = 'native'
WHERE e.kind = 'orch_step' AND e.payload->>'mission_id' = %s
ORDER BY e.seq
"""


def _chain_order(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Order step payloads by the ``previous_event_sha256`` chain.

    The chain is the written order; a broken or altered chain leaves the
    unreached rows appended in ``seq`` order so the caller still sees them.
    """
    by_previous: dict[object, list[tuple[int, dict[str, object]]]] = {}
    head: tuple[int, dict[str, object]] | None = None
    for index, payload in enumerate(rows):
        previous = payload.get("previous_event_sha256")
        if previous is None and head is None:
            head = (index, payload)
        by_previous.setdefault(previous, []).append((index, payload))

    ordered: list[dict[str, object]] = []
    seen: set[int] = set()
    node = head
    while node is not None and node[0] not in seen:
        seen.add(node[0])
        ordered.append(node[1])
        successors = by_previous.get(node[1].get("event_sha256"), [])
        node = next((candidate for candidate in successors if candidate[0] not in seen), None)
    for index, payload in enumerate(rows):
        if index not in seen:
            ordered.append(payload)
    return ordered


def list_steps(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: object,
    mission_id: str,
) -> list[dict[str, object]]:
    """Every ``orch_step`` event of the mission, in chain order."""
    cur.execute(_STEP_SELECT, (workspace_id, mission_id))
    return _chain_order([dict(row["payload"]) for row in cur.fetchall()])


def _replay(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: object,
    step: Mapping[str, object],
) -> dict[str, object] | None:
    """The stored step for this identity, or None when it was never written."""
    cur.execute(
        """
        SELECT e.payload
        FROM omp_jobs.job_events e
        JOIN omp_jobs.jobs j
          ON j.job_id = e.job_id AND j.workspace_id = %s AND j.source = 'native'
        WHERE e.kind = 'orch_step'
          AND e.payload->>'mission_id' = %s
          AND (e.payload->>'step_index')::int = %s
          AND e.payload->>'kind' = %s
          AND COALESCE(e.payload->>'idempotency_key', '') = COALESCE(%s, '')
        ORDER BY e.seq DESC
        LIMIT 1
        """,
        (
            workspace_id,
            step["mission_id"],
            step["step_index"],
            step["kind"],
            step.get("idempotency_key"),
        ),
    )
    row = cur.fetchone()
    return None if row is None else dict(row["payload"])


def append_step(
    store: NativeJobStore,
    cur: psycopg.Cursor[dict[str, object]],
    *,
    workspace_id: object,
    job_id: str,
    step: Mapping[str, object],
) -> dict[str, object]:
    """Append one step event, or replay an identical one.

    Same ``(mission_id, step_index, kind, idempotency_key)`` and equal fields
    returns the stored step and writes nothing; differing fields raise
    ``JobError('revision_conflict')``. A missing job or another workspace's job
    is refused by the underlying ``append_event``.
    """
    mission_id = str(step["mission_id"])
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('orch:' || %s, 0))",
        (mission_id,),
    )
    replay = _replay(cur, workspace_id, step)
    if replay is not None:
        fields = {key: value for key, value in replay.items() if key not in _HASH_FIELDS}
        if fields == dict(step):
            return replay
        raise JobError(
            "revision_conflict",
            ("step identity already recorded with different fields",),
        )

    payload: dict[str, object] = dict(step)
    steps = list_steps(cur, workspace_id, mission_id)
    payload["previous_event_sha256"] = (
        None if not steps else steps[-1].get("event_sha256")
    )
    payload["event_sha256"] = sha256(payload)

    operation_id = step.get("idempotency_key") or (
        f"orch:{mission_id}:{step['step_index']}:{step['kind']}"
    )
    store.append_event(
        cur,
        workspace_id=workspace_id,
        job_id=job_id,
        kind=STEP_EVENT_KIND,
        reason=str(step["rule_id"]),
        payload=payload,
        operation_id=str(operation_id),
    )
    return payload


def open_intent(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: object,
    mission_id: str,
    key: str,
) -> dict[str, object] | None:
    """The mission's latest ``external_intent`` for ``key`` with no ``external_done``.

    Walks the chain newest first: the most recent ``external_intent`` or
    ``external_done`` carrying ``key`` decides. An intent with no later done is
    open; a done closes it, so a re-opened intent is open again.
    """
    steps = list_steps(cur, workspace_id, mission_id)
    for step in reversed(steps):
        if step.get("external_ref") != key:
            continue
        if step.get("kind") == "external_intent":
            return step
        if step.get("kind") == "external_done":
            return None
    return None
