"""Renew, settle, and reconcile native research job leases (R03, OMP-324).

Every call runs through ``NativeJobStore.run_operation``. Renew and settle
hold the lease only while the job is native and admitted, the worker and fence
match the row, and ``lease_expires_at`` is still in the future. Anything else
is ``job_fence_stale`` and writes nothing. A terminal job accepts one
settlement; the same settlement under a new operation id replays, and receipts
stay as first written.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from omp_work.contracts.r02.validate import validate_instance
from omp_work.jobs.store import JobError, NativeJobStore, OperationOutcome
from omp_work.v1.canonical import canonical_json

_OUTCOMES = {"succeeded": "sealed", "failed": "failed"}
_ROLES = frozenset({"audit", "release", "evaluator"})
_RECEIPT_KEYS = frozenset({"role", "issuer_component_sha256", "evidence"})
_TERMINAL = frozenset({"sealed", "failed"})


def renew_lease(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    job_id: str,
    worker_id: str,
    fence: int,
) -> dict[str, object]:
    """Add ``lease_seconds`` to the job lease and its reservation expiry.

    The reservation is ``{job_id}:{fence}``. A repeated operation id replays
    the stored job and does not extend a second time.
    """
    worker_id, fence = _require_holder(worker_id, fence)
    request = {
        "workspace_id": str(workspace_id),
        "job_id": job_id,
        "worker_id": worker_id,
        "fence": fence,
    }

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            cur.execute(
                """
                UPDATE omp_jobs.jobs
                SET lease_expires_at = lease_expires_at + (lease_seconds * interval '1 second'),
                    updated_at = clock_timestamp()
                WHERE workspace_id=%s AND job_id=%s AND source='native' AND status='admitted'
                  AND worker_id=%s AND fence=%s
                  AND lease_expires_at > clock_timestamp()
                RETURNING lease_seconds
                """,
                (workspace_id, job_id, worker_id, fence),
            )
            updated = cur.fetchone()
            if updated is None:
                raise JobError(
                    "job_fence_stale",
                    ("lease is not held by this worker and fence",),
                )
            cur.execute(
                """
                UPDATE omp_jobs.reservations
                SET expires_at = expires_at + (%s * interval '1 second')
                WHERE reservation_id=%s AND workspace_id=%s AND job_id=%s
                  AND worker_id=%s AND fence=%s AND released_at IS NULL
                """,
                (
                    updated["lease_seconds"],
                    f"{job_id}:{fence}",
                    workspace_id,
                    job_id,
                    worker_id,
                    fence,
                ),
            )
            return {"status": "applied", "job": store.job_view(cur, workspace_id, job_id)}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_renew", request, apply
        )
    return _public(operation_id, outcome, "job")


def settle_job(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    job_id: str,
    worker_id: str,
    fence: int,
    outcome: str,
    receipts: list[dict[str, object]],
) -> dict[str, object]:
    """Seal a held lease as ``succeeded`` or mark it ``failed``.

    ``succeeded`` stores status ``sealed``; ``failed`` stores ``failed``.
    Each receipt's evidence must validate as an R02 evidence document of kind
    ``receipt``, and its issuer must be a registered component in this
    workspace whose kind equals the receipt role (``audit``, ``release``, or
    ``evaluator``). A bad receipt is ``invalid_request`` and writes nothing.
    The settlement ``{outcome, fence, worker_id, receipts}`` is written once,
    with ``settled_at``, the reservation released as ``settled``, and a
    ``settled`` event. A later operation whose settlement is byte-identical
    replays the stored settlement. Any other settlement for a terminal job,
    or a lease that is not held, is ``job_fence_stale`` and writes nothing.
    """
    worker_id, fence = _require_holder(worker_id, fence)
    status = _require_outcome(outcome)
    checked = _require_receipts(receipts)
    document = _settlement(outcome, fence, worker_id, checked)
    request = {
        "workspace_id": str(workspace_id),
        "job_id": job_id,
        "worker_id": worker_id,
        "fence": fence,
        "outcome": outcome,
        "receipts": checked,
    }

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            _require_issuers(cur, workspace_id, checked)
            row = _lock_job(cur, workspace_id, job_id)
            if row is not None and row["source"] == "native" and row["status"] in _TERMINAL:
                stored = _json_object(row["settlement"])
                if stored is not None and canonical_json(stored) == canonical_json(document):
                    return {
                        "status": "replayed",
                        "job": store.job_view(cur, workspace_id, job_id),
                        "settlement": stored,
                    }
                raise JobError(
                    "job_fence_stale",
                    ("settlement does not match the stored settlement",),
                )
            if not _lease_held(row, worker_id, fence):
                raise JobError(
                    "job_fence_stale",
                    ("lease is not held by this worker and fence",),
                )
            cur.execute(
                """
                UPDATE omp_jobs.jobs
                SET status=%s,
                    settlement=%s,
                    settled_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE workspace_id=%s AND job_id=%s AND source='native' AND status='admitted'
                  AND worker_id=%s AND fence=%s
                  AND lease_expires_at > clock_timestamp()
                  AND settlement IS NULL
                """,
                (status, Jsonb(document), workspace_id, job_id, worker_id, fence),
            )
            if cur.rowcount != 1:
                raise JobError(
                    "job_fence_stale",
                    ("lease is not held by this worker and fence",),
                )
            _release_reservation(
                cur, workspace_id, job_id, worker_id, fence, "settled"
            )
            store.append_event(
                cur,
                workspace_id=workspace_id,
                job_id=job_id,
                kind="settled",
                actor=worker_id,
                operation_id=operation_id,
                payload={"outcome": outcome, "fence": fence, "worker_id": worker_id},
            )
            view = store.job_view(cur, workspace_id, job_id)
            return {
                "status": "applied",
                "job": view,
                "settlement": None if view is None else view["settlement"],
            }

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_settle", request, apply
        )
    return _public(operation_id, outcome, "job", "settlement")


def reconcile_jobs(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
) -> dict[str, object]:
    """Return expired admitted native jobs to the backlog.

    Each admitted native job with ``lease_expires_at <= now`` gains one fence,
    returns to ``backlog``, loses ``worker_id`` and ``lease_expires_at``, has
    its reservation released as ``lease_expired``, and gains a
    ``lease_expired`` event. The result is ``{status, job_ids}`` in
    ``(created_at, job_id)`` order. A second call finds nothing to reclaim.
    """
    request = {"workspace_id": str(workspace_id)}

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            cur.execute(
                """
                SELECT job_id, fence, worker_id
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND source='native' AND status='admitted'
                  AND lease_expires_at <= clock_timestamp()
                ORDER BY created_at, job_id
                FOR UPDATE
                """,
                (workspace_id,),
            )
            expired = list(cur.fetchall())
            job_ids: list[str] = []
            for row in expired:
                job_id = str(row["job_id"])
                old_fence = int(row["fence"])
                holder = row["worker_id"]
                cur.execute(
                    """
                    UPDATE omp_jobs.jobs
                    SET fence=fence + 1,
                        status='backlog',
                        worker_id=NULL,
                        lease_expires_at=NULL,
                        updated_at=clock_timestamp()
                    WHERE workspace_id=%s AND job_id=%s AND source='native' AND status='admitted'
                      AND lease_expires_at <= clock_timestamp()
                    RETURNING fence
                    """,
                    (workspace_id, job_id),
                )
                advanced = cur.fetchone()
                if advanced is None:
                    continue
                if isinstance(holder, str):
                    _release_reservation(
                        cur, workspace_id, job_id, holder, old_fence, "lease_expired"
                    )
                store.append_event(
                    cur,
                    workspace_id=workspace_id,
                    job_id=job_id,
                    kind="lease_expired",
                    actor=holder if isinstance(holder, str) else None,
                    operation_id=operation_id,
                    payload={
                        "worker_id": holder,
                        "fence": old_fence,
                        "next_fence": int(advanced["fence"]),
                    },
                )
                job_ids.append(job_id)
            return {"status": "applied", "job_ids": job_ids}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_reconcile", request, apply
        )
    return _public(operation_id, outcome, "job_ids")


def _public(
    operation_id: str, outcome: OperationOutcome, *keys: str
) -> dict[str, object]:
    status = (
        "replayed"
        if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
        else "applied"
    )
    body: dict[str, object] = {"operation_id": operation_id, "status": status}
    for key in keys:
        body[key] = outcome.result.get(key)
    return body


def _require_holder(worker_id: object, fence: object) -> tuple[str, int]:
    if not isinstance(worker_id, str) or worker_id == "":
        raise JobError("job_fence_stale", ("worker_id is required",))
    if isinstance(fence, bool) or not isinstance(fence, int):
        raise JobError("job_fence_stale", ("fence is required",))
    return worker_id, fence


def _require_outcome(outcome: object) -> str:
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise JobError(
            "invalid_request",
            ("outcome must be succeeded or failed",),
        )
    return _OUTCOMES[outcome]


def _require_receipts(receipts: object) -> list[dict[str, object]]:
    if not isinstance(receipts, list):
        raise JobError("invalid_request", ("receipts must be a list",))
    checked: list[dict[str, object]] = []
    for item in receipts:
        if not isinstance(item, dict) or set(item) != _RECEIPT_KEYS:
            raise JobError(
                "invalid_request",
                ("receipt must have role, issuer_component_sha256, and evidence",),
            )
        role = item["role"]
        issuer = item["issuer_component_sha256"]
        evidence = item["evidence"]
        if not isinstance(role, str) or role not in _ROLES:
            raise JobError(
                "invalid_request",
                ("receipt role must be audit, release, or evaluator",),
            )
        if not isinstance(issuer, str) or issuer == "":
            raise JobError(
                "invalid_request",
                ("issuer is not a registered component of this role",),
            )
        if not isinstance(evidence, dict):
            raise JobError("invalid_request", ("receipt evidence is malformed",))
        try:
            validate_instance("evidence", evidence)
        except ValueError as err:
            raise JobError("invalid_request", (str(err),)) from err
        if evidence.get("kind") != "receipt":
            raise JobError("invalid_request", ("evidence kind must be receipt",))
        checked.append(item)
    return checked


def _require_issuers(
    cur: Any, workspace_id: UUID, receipts: list[dict[str, object]]
) -> None:
    for item in receipts:
        cur.execute(
            """
            SELECT kind FROM omp_research.components
            WHERE workspace_id=%s AND component_sha256=%s
            """,
            (workspace_id, item["issuer_component_sha256"]),
        )
        row = cur.fetchone()
        if row is None or row["kind"] != item["role"]:
            raise JobError(
                "invalid_request",
                ("issuer is not a registered component of this role",),
            )


def _lock_job(cur: Any, workspace_id: UUID, job_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT status, source, worker_id, fence, settlement,
               (lease_expires_at > clock_timestamp()) AS lease_held
        FROM omp_jobs.jobs
        WHERE workspace_id=%s AND job_id=%s
        FOR UPDATE
        """,
        (workspace_id, job_id),
    )
    return cur.fetchone()


def _lease_held(row: dict[str, Any] | None, worker_id: str, fence: int) -> bool:
    return bool(
        row is not None
        and row["source"] == "native"
        and row["status"] == "admitted"
        and row["worker_id"] == worker_id
        and row["fence"] == fence
        and row["lease_held"] is True
    )


def _settlement(
    outcome: str, fence: int, worker_id: str, receipts: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "outcome": outcome,
        "fence": fence,
        "worker_id": worker_id,
        "receipts": receipts,
    }


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        return value
    return None


def _release_reservation(
    cur: Any,
    workspace_id: UUID,
    job_id: str,
    worker_id: str,
    fence: int,
    reason: str,
) -> None:
    cur.execute(
        """
        UPDATE omp_jobs.reservations
        SET released_at=clock_timestamp(), release_reason=%s
        WHERE reservation_id=%s AND workspace_id=%s AND job_id=%s
          AND worker_id=%s AND fence=%s AND released_at IS NULL
        """,
        (reason, f"{job_id}:{fence}", workspace_id, job_id, worker_id, fence),
    )
