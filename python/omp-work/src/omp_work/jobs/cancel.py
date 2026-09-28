"""Cancel a native job and every non-terminal descendant (R03, OMP-324).

``cancel_job`` runs through ``NativeJobStore.run_operation``. The target and
its same-workspace descendants are locked together, in ``job_id`` order, so
two overlapping cancels cannot deadlock. Backlog and admitted jobs become
``cancelled``. An admitted job also loses its lease: the fence advances, the
worker is cleared, and the open reservation is released as ``cancelled``, so
the previous holder's renew or settle is ``job_fence_stale``. Sealed and
failed jobs are left as they are.

``cancel_item_tree`` is the item-scoped counterpart used by the OMP-404 budget
stopper: it takes the caller's open cursor, locks the work item's jobs and
their descendants in ``job_id`` order, and cancels them with the same rules.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from omp_work.jobs.store import JobError, NativeJobStore, OperationOutcome

_CANCELLABLE = frozenset({"backlog", "admitted"})
_REASON_LIMIT = 4096


def cancel_job(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    job_id: str,
    reason: str,
) -> dict[str, object]:
    """Cancel ``job_id`` and every backlog or admitted descendant.

    The result is ``{status, job_ids}`` for the jobs this call cancelled, in
    ``job_id`` order. A target already cancelled for the same reason replays
    and does not write the job, its events, or its reservation again. A
    different reason is ``idempotency_conflict``. A missing target, or a
    sealed or failed target, is ``invalid_request`` and writes nothing.
    """
    job_id, reason = _require_request(job_id, reason)
    request = {
        "workspace_id": str(workspace_id),
        "job_id": job_id,
        "reason": reason,
    }

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            rows = _lock_tree(cur, workspace_id, job_id)
            target = next((row for row in rows if row["job_id"] == job_id), None)
            if target is None or target["source"] != "native":
                raise JobError("invalid_request", ("unknown job",))
            if target["status"] == "cancelled":
                if target["cancel_reason"] != reason:
                    raise JobError(
                        "idempotency_conflict",
                        ("cancel reason differs from the stored cancellation",),
                    )
                return {"status": "replayed", "job_ids": _cancelled_ids(rows, reason)}
            if target["status"] in ("sealed", "failed"):
                raise JobError(
                    "invalid_request",
                    ("sealed or failed job cannot be cancelled",),
                )
            if target["status"] not in _CANCELLABLE:
                raise JobError("invalid_request", ("job cannot be cancelled",))
            job_ids = _cancel_rows(
                store,
                cur,
                workspace_id,
                actor_id,
                operation_id,
                reason,
                rows,
            )
            return {"status": "applied", "job_ids": job_ids}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_cancel", request, apply
        )
    return _public(operation_id, outcome)


def cancel_item_tree(
    store: NativeJobStore,
    cur: Any,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    operation_id: str,
    work_id: UUID,
    reason: str,
) -> list[str]:
    """Cancel every backlog or admitted job of ``work_id`` and its descendants.

    The caller holds the open transaction: the jobs of the work item plus every
    ``parent_job_id`` descendant whichever work item they belong to are locked
    in ``job_id`` order, then ``_cancel_rows`` cancels the native backlog and
    admitted ones and returns their ids in ``job_id`` order. Unlike
    :func:`cancel_job` this is a sub-operation of a larger transaction, so it
    does not run through ``run_operation``: there is no separate request to
    replay, and the enclosing operation's id stamping the events is enough.
    Sealed, failed, and already cancelled jobs are left as they are.
    """
    if not isinstance(reason, str) or reason == "" or len(reason.encode()) > _REASON_LIMIT:
        raise JobError("invalid_request", ("cancel reason is required",))
    rows = _lock_item_tree(cur, workspace_id, work_id)
    return _cancel_rows(
        store,
        cur,
        workspace_id,
        actor_id,
        operation_id,
        reason,
        rows,
    )


def _require_request(job_id: object, reason: object) -> tuple[str, str]:
    if not isinstance(job_id, str) or job_id == "":
        raise JobError("invalid_request", ("job_id is required",))
    if (
        not isinstance(reason, str)
        or reason == ""
        or len(reason.encode()) > _REASON_LIMIT
    ):
        raise JobError("invalid_request", ("cancel reason is required",))
    return job_id, reason


def _lock_tree(
    cur: Any, workspace_id: UUID, job_id: str
) -> list[dict[str, Any]]:
    """Target plus same-workspace descendants, locked in ``job_id`` order.

    The path array stops a parent cycle from walking forever. ``FOR UPDATE``
    is on the jobs row, after ``ORDER BY``, so the locks are taken in that
    order.
    """
    cur.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT job_id, ARRAY[job_id]::text[] AS path
            FROM omp_jobs.jobs
            WHERE workspace_id = %s AND job_id = %s
            UNION ALL
            SELECT child.job_id, tree.path || child.job_id
            FROM omp_jobs.jobs AS child
            JOIN tree ON child.parent_job_id = tree.job_id
            WHERE child.workspace_id = %s
              AND NOT (child.job_id = ANY (tree.path))
        )
        SELECT
            j.job_id, j.status, j.source, j.fence, j.worker_id, j.cancel_reason
        FROM tree
        JOIN omp_jobs.jobs AS j
          ON j.workspace_id = %s AND j.job_id = tree.job_id
        ORDER BY j.job_id
        FOR UPDATE OF j
        """,
        (workspace_id, job_id, workspace_id, workspace_id),
    )
    return list(cur.fetchall())


def _lock_item_tree(
    cur: Any, workspace_id: UUID, work_id: UUID
) -> list[dict[str, Any]]:
    """Every job of ``work_id`` plus descendants, locked in ``job_id`` order.

    The tree roots are the work item's own jobs and every job whose
    ``parent_job_id`` chain reaches one of them, whichever work item the
    descendant belongs to. The path array stops a parent cycle from walking
    forever. ``FOR UPDATE`` is on the jobs row, after ``ORDER BY``, so the locks
    are taken in that order.
    """
    cur.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT job_id, ARRAY[job_id]::text[] AS path
            FROM omp_jobs.jobs
            WHERE workspace_id = %s AND work_id = %s
            UNION ALL
            SELECT child.job_id, tree.path || child.job_id
            FROM omp_jobs.jobs AS child
            JOIN tree ON child.parent_job_id = tree.job_id
            WHERE child.workspace_id = %s
              AND NOT (child.job_id = ANY (tree.path))
        )
        SELECT
            j.job_id, j.status, j.source, j.fence, j.worker_id, j.cancel_reason
        FROM tree
        JOIN omp_jobs.jobs AS j
          ON j.workspace_id = %s AND j.job_id = tree.job_id
        ORDER BY j.job_id
        FOR UPDATE OF j
        """,
        (workspace_id, work_id, workspace_id, workspace_id),
    )
    return list(cur.fetchall())


def _cancel_rows(
    store: NativeJobStore,
    cur: Any,
    workspace_id: UUID,
    actor_id: UUID,
    operation_id: str,
    reason: str,
    rows: list[dict[str, Any]],
) -> list[str]:
    job_ids: list[str] = []
    for row in rows:
        if row["source"] != "native" or row["status"] not in _CANCELLABLE:
            continue
        job = str(row["job_id"])
        admitted = row["status"] == "admitted"
        holder = row["worker_id"]
        old_fence = int(row["fence"])
        cur.execute(
            """
            UPDATE omp_jobs.jobs
            SET status = 'cancelled',
                cancel_reason = %s,
                cancelled_at = clock_timestamp(),
                updated_at = clock_timestamp(),
                fence = CASE WHEN status = 'admitted' THEN fence + 1 ELSE fence END,
                worker_id = CASE WHEN status = 'admitted' THEN NULL ELSE worker_id END
            WHERE workspace_id = %s AND job_id = %s AND source = 'native'
              AND status IN ('backlog', 'admitted')
            """,
            (reason, workspace_id, job),
        )
        if cur.rowcount != 1:
            continue
        if admitted and isinstance(holder, str) and holder != "":
            _release_reservation(cur, workspace_id, job, holder, old_fence)
        payload: dict[str, object] = {"reason": reason}
        if admitted:
            payload["worker_id"] = holder
            payload["fence"] = old_fence
        store.append_event(
            cur,
            workspace_id=workspace_id,
            job_id=job,
            kind="cancelled",
            actor=str(actor_id),
            reason=reason,
            operation_id=operation_id,
            payload=payload,
        )
        job_ids.append(job)
    return job_ids


def _release_reservation(
    cur: Any,
    workspace_id: UUID,
    job_id: str,
    worker_id: str,
    fence: int,
) -> None:
    cur.execute(
        """
        UPDATE omp_jobs.reservations
        SET released_at = clock_timestamp(), release_reason = 'cancelled'
        WHERE reservation_id = %s AND workspace_id = %s AND job_id = %s
          AND worker_id = %s AND fence = %s AND released_at IS NULL
        """,
        (f"{job_id}:{fence}", workspace_id, job_id, worker_id, fence),
    )


def _cancelled_ids(rows: list[dict[str, Any]], reason: str) -> list[str]:
    return [
        str(row["job_id"])
        for row in rows
        if row["source"] == "native"
        and row["status"] == "cancelled"
        and row["cancel_reason"] == reason
    ]


def _public(operation_id: str, outcome: OperationOutcome) -> dict[str, object]:
    status = (
        "replayed"
        if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
        else "applied"
    )
    return {
        "operation_id": operation_id,
        "status": status,
        "job_ids": outcome.result.get("job_ids"),
    }
