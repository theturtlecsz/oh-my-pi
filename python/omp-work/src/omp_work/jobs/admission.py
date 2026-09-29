"""Enqueue and claim native research jobs (R03, OMP-324).

Both operations run through ``NativeJobStore.run_operation``. A repeated
operation id with the same request replays the stored result and does not
lease or insert a second time.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from omp_work.jobs.budget import (
    alert_subagents_exceeded,
    check_item_budget,
    item_budget,
    item_spend,
    root_work_id,
)
from omp_work.jobs.cancel import cancel_item_tree
from omp_work.jobs.store import JobError, NativeJobStore, OperationOutcome

_RESOURCE_KEYS = ("cpu", "memory_mib", "gpu", "model_calls")
_KINDS = frozenset({"model", "compute"})
_TRIAL_CAMPAIGN_STATES = frozenset({"admitted", "running"})
_BUDGET_CODE = "budget_exceeded"
_BUDGET_REASON = "budget_exceeded"
_JOB_IDENTITY = (
    "source, workspace_id, work_id, kind, parent_job_id, trial_id, "
    "required_capabilities, resources, lease_seconds"
)


def enqueue_job(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    job_id: str,
    work_id: UUID,
    kind: str,
    required_capabilities: list[str],
    resources: dict[str, int],
    lease_seconds: int,
    parent_job_id: str | None = None,
    trial_id: UUID | None = None,
) -> dict[str, object]:
    """Insert one native backlog job, or replay an identical ``job_id``.

    Resources are exactly ``cpu``, ``memory_mib``, ``gpu``, and ``model_calls``,
    each an integer >= 0. Capabilities are stored sorted and unique.
    ``lease_seconds`` is 1..3600. The work item, and the parent when given,
    must already be in the workspace. A cancelled parent or ancestor refuses
    with ``job_cancelled`` and writes nothing. A ``trial_id`` must be a
    proposed trial of this work whose campaign is admitted or running.
    The same ``job_id`` with the same identity replays; any other row with
    that id is ``idempotency_conflict``.
    """
    capabilities = _capabilities(required_capabilities)
    vector = _require_resources(resources)
    if kind not in _KINDS:
        raise JobError("invalid_request", ("kind must be model or compute",))
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 1 <= lease_seconds <= 3600
    ):
        raise JobError(
            "invalid_request",
            ("lease_seconds must be an integer from 1 to 3600",),
        )
    if not isinstance(job_id, str) or not job_id:
        raise JobError("invalid_request", ("job_id is required",))
    if not isinstance(work_id, UUID):
        raise JobError("invalid_request", ("work_id must be a UUID",))
    if parent_job_id is not None and (
        not isinstance(parent_job_id, str) or not parent_job_id
    ):
        raise JobError("invalid_request", ("parent_job_id must be a job id",))
    if trial_id is not None and not isinstance(trial_id, UUID):
        raise JobError("invalid_request", ("trial_id must be a UUID",))
    request = {
        "workspace_id": str(workspace_id),
        "job_id": job_id,
        "work_id": str(work_id),
        "kind": kind,
        "parent_job_id": parent_job_id,
        "trial_id": None if trial_id is None else str(trial_id),
        "required_capabilities": capabilities,
        "resources": vector,
        "lease_seconds": lease_seconds,
    }

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            existing = _existing_job(store, cur, workspace_id, job_id, request)
            if existing is not None:
                return existing
            _require_work_item(cur, workspace_id, work_id)
            if parent_job_id is not None:
                _require_live_parent(store, cur, workspace_id, parent_job_id)
            if trial_id is not None:
                _require_trial(cur, workspace_id, work_id, trial_id)
            budget_work_id = (
                root_work_id(cur, workspace_id, parent_job_id)
                if parent_job_id is not None
                else work_id
            )
            if budget_work_id is not None and _budget_refuses(
                store,
                cur,
                workspace_id=workspace_id,
                actor_id=actor_id,
                operation_id=operation_id,
                budget_work_id=budget_work_id,
                parent_job_id=parent_job_id,
            ):
                return {"status": "refused", "code": _BUDGET_CODE}
            cur.execute(
                """
                INSERT INTO omp_jobs.jobs(
                    job_id, status, source, workspace_id, work_id, trial_id,
                    parent_job_id, kind, required_capabilities, resources,
                    lease_seconds, fence, attempt
                ) VALUES (
                    %s, 'backlog', 'native', %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, 0, 0
                )
                ON CONFLICT (job_id) DO NOTHING
                RETURNING job_id
                """,
                (
                    job_id,
                    workspace_id,
                    work_id,
                    trial_id,
                    parent_job_id,
                    kind,
                    Jsonb(capabilities),
                    Jsonb(vector),
                    lease_seconds,
                ),
            )
            if cur.fetchone() is None:
                raced = _existing_job(store, cur, workspace_id, job_id, request)
                if raced is None:
                    raise JobError(
                        "idempotency_conflict",
                        ("job id already used for a different request",),
                    )
                return raced
            store.append_event(
                cur,
                workspace_id=workspace_id,
                job_id=job_id,
                kind="enqueued",
                actor=str(actor_id),
                operation_id=operation_id,
                payload={"kind": kind, "lease_seconds": lease_seconds},
            )
            if parent_job_id is not None and budget_work_id is not None:
                check_item_budget(
                    store,
                    cur,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    work_id=budget_work_id,
                    operation_id=operation_id,
                )
            return {
                "status": "applied",
                "job": store.job_view(cur, workspace_id, job_id),
            }

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_enqueue", request, apply
        )
    if outcome.result.get("code") == _BUDGET_CODE:
        raise JobError(
            _BUDGET_CODE,
            ("item budget is exhausted",),
        )
    return _public(operation_id, outcome)


def _budget_refuses(
    store: NativeJobStore,
    cur: Any,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    operation_id: str,
    budget_work_id: UUID,
    parent_job_id: str | None,
) -> bool:
    """True when this enqueue must be refused, after stopping the item tree.

    The item's budget is checked first: an exhausted dimension stops the whole
    tree and refuses. Otherwise a child (a job with a ``parent_job_id``) is
    refused when the tree already holds ``max_subagents`` jobs with a parent —
    the tree is stopped, and the 100 subagents alert is written beside the
    other budget alerts before the refusal. A child that only crosses a
    subagents threshold is admitted: ``check_item_budget`` writes the 50 or 80
    row after the insert.
    """
    budget = item_budget(cur, workspace_id, budget_work_id)
    if budget is None:
        return False
    spend = item_spend(cur, workspace_id, budget_work_id)
    if (
        spend.usd >= Decimal(budget.usd)
        or spend.tokens >= budget.tokens
        or spend.wall_clock_seconds >= budget.wall_clock_seconds
    ):
        cancel_item_tree(
            store,
            cur,
            workspace_id=workspace_id,
            actor_id=actor_id,
            operation_id=operation_id,
            work_id=budget_work_id,
            reason=_BUDGET_REASON,
        )
        return True
    if (
        parent_job_id is not None
        and budget.max_subagents > 0
        and spend.subagents >= budget.max_subagents
    ):
        alert_subagents_exceeded(
            cur,
            workspace_id=workspace_id,
            work_id=budget_work_id,
            operation_id=operation_id,
            maximum=budget.max_subagents,
            count=spend.subagents + 1,
        )
        cancel_item_tree(
            store,
            cur,
            workspace_id=workspace_id,
            actor_id=actor_id,
            operation_id=operation_id,
            work_id=budget_work_id,
            reason=_BUDGET_REASON,
        )
        return True
    return False


def claim_job(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    worker_id: str,
) -> dict[str, object]:
    """Lease the oldest matching native backlog job to one active worker.

    Routing reads the worker row only. An unknown or non-active worker is
    ``job_worker_unavailable``. The worker row is locked, then the oldest
    backlog native job in the workspace is taken with ``FOR UPDATE SKIP
    LOCKED``. It must be one whose capabilities the worker holds, whose
    resource weight fits the worker's free capacity, and whose ancestors are
    not cancelled. Weight is ``cpu + memory_mib + gpu + model_calls``. Free
    capacity is ``capacity`` minus the weight of that worker's unreleased
    reservations. The lease stores the job's resources on reservation
    ``{job_id}:{fence}``. A job with a ``trial_id`` is eligible only while
    the trial is proposed, the campaign is admitted or running, and the
    worker's component is in ``compatibility.workers``. No match returns
    ``job: None``.
    """
    if not isinstance(worker_id, str) or not worker_id:
        raise JobError("job_worker_unavailable", ("unknown worker",))
    request = {"workspace_id": str(workspace_id), "worker_id": worker_id}

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            cur.execute(
                """
                SELECT state, capabilities, capacity, component_sha256
                FROM omp_jobs.workers
                WHERE workspace_id=%s AND worker_id=%s
                FOR UPDATE
                """,
                (workspace_id, worker_id),
            )
            worker = cur.fetchone()
            if worker is None:
                raise JobError("job_worker_unavailable", ("unknown worker",))
            if worker["state"] != "active":
                raise JobError("job_worker_unavailable", ("worker is not active",))
            held = set(_json_list(worker["capabilities"]))
            free = int(worker["capacity"]) - _reserved_weight(
                cur, workspace_id, worker_id
            )
            component = str(worker["component_sha256"])
            cur.execute(
                """
                SELECT job_id, required_capabilities, resources, lease_seconds, trial_id
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND source='native' AND status='backlog'
                ORDER BY created_at, job_id
                """,
                (workspace_id,),
            )
            for candidate in cur.fetchall():
                if not _claimable(
                    store, cur, workspace_id, candidate, held, free, component
                ):
                    continue
                cur.execute(
                    """
                    SELECT job_id, required_capabilities, resources, lease_seconds, trial_id
                    FROM omp_jobs.jobs
                    WHERE workspace_id=%s AND job_id=%s AND source='native' AND status='backlog'
                    FOR UPDATE SKIP LOCKED
                    """,
                    (workspace_id, candidate["job_id"]),
                )
                locked = cur.fetchone()
                if locked is None or not _claimable(
                    store, cur, workspace_id, locked, held, free, component
                ):
                    continue
                if _root_budget_exhausted(
                    store, cur, workspace_id, str(locked["job_id"])
                ):
                    return {"status": "applied", "job": None}
                admitted = _admit(
                    store,
                    cur,
                    workspace_id,
                    str(locked["job_id"]),
                    worker_id,
                    operation_id,
                )
                if admitted is not None:
                    return admitted
            return {"status": "applied", "job": None}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "job_claim", request, apply
        )
    return _public(operation_id, outcome)


def _public(operation_id: str, outcome: OperationOutcome) -> dict[str, object]:
    status = (
        "replayed"
        if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
        else "applied"
    )
    return {
        "operation_id": operation_id,
        "status": status,
        "job": outcome.result.get("job"),
    }


def _capabilities(value: object) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise JobError(
            "invalid_request",
            ("capabilities must be strings",),
        )
    return sorted(set(value))


def _require_resources(value: object) -> dict[str, int]:
    vector = _resource_vector(value)
    if vector is None:
        raise JobError(
            "invalid_request",
            ("resources must be cpu, memory_mib, gpu, and model_calls integers >= 0",),
        )
    return vector


def _resource_vector(value: object) -> dict[str, int] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or set(value) != set(_RESOURCE_KEYS):
        return None
    vector: dict[str, int] = {}
    for key in _RESOURCE_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        vector[key] = item
    return vector


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _require_work_item(
    cur: Any, workspace_id: UUID, work_id: UUID
) -> None:
    cur.execute(
        "SELECT work_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
        (workspace_id, work_id),
    )
    if cur.fetchone() is None:
        raise JobError("invalid_request", ("work item is not in this workspace",))


def _require_live_parent(
    store: NativeJobStore,
    cur: Any,
    workspace_id: UUID,
    parent_job_id: str,
) -> None:
    cur.execute(
        "SELECT workspace_id, status FROM omp_jobs.jobs WHERE job_id=%s FOR UPDATE",
        (parent_job_id,),
    )
    parent = cur.fetchone()
    if parent is None or parent["workspace_id"] != workspace_id:
        raise JobError("invalid_request", ("parent job is not in this workspace",))
    if parent["status"] == "cancelled" or store.cancelled_ancestor(
        cur, workspace_id, parent_job_id
    ) is not None:
        raise JobError("job_cancelled", ("cancelled ancestor",))


def _require_trial(
    cur: Any, workspace_id: UUID, work_id: UUID, trial_id: UUID
) -> None:
    row = _trial_row(cur, workspace_id, trial_id)
    if (
        row is None
        or row["work_id"] != work_id
        or row["trial_state"] != "proposed"
        or row["campaign_state"] not in _TRIAL_CAMPAIGN_STATES
    ):
        raise JobError(
            "invalid_request",
            ("trial is not a proposed trial of this work on an admitted or running campaign",),
        )


def _trial_row(cur: Any, workspace_id: UUID, trial_id: UUID) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT t.work_id, t.state AS trial_state, c.state AS campaign_state, c.compatibility
        FROM omp_research.trials t
        JOIN omp_research.campaigns c
          ON c.workspace_id = t.workspace_id AND c.campaign_id = t.campaign_id
        WHERE t.workspace_id=%s AND t.trial_id=%s
        """,
        (workspace_id, trial_id),
    )
    return cur.fetchone()


def _existing_job(
    store: NativeJobStore,
    cur: Any,
    workspace_id: UUID,
    job_id: str,
    request: dict[str, object],
) -> dict[str, object] | None:
    cur.execute(
        f"SELECT {_JOB_IDENTITY} FROM omp_jobs.jobs WHERE job_id=%s FOR UPDATE",  # nosec B608 - static column list
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if _same_identity(row, workspace_id, request):
        return {
            "status": "replayed",
            "job": store.job_view(cur, workspace_id, job_id),
        }
    raise JobError(
        "idempotency_conflict",
        ("job id already used for a different request",),
    )


def _same_identity(
    row: dict[str, Any], workspace_id: UUID, request: dict[str, object]
) -> bool:
    vector = _resource_vector(row["resources"])
    return bool(
        row["source"] == "native"
        and row["workspace_id"] == workspace_id
        and str(row["work_id"]) == request["work_id"]
        and row["kind"] == request["kind"]
        and row["parent_job_id"] == request["parent_job_id"]
        and _trial_text(row["trial_id"]) == request["trial_id"]
        and row["lease_seconds"] == request["lease_seconds"]
        and _json_list(row["required_capabilities"]) == request["required_capabilities"]
        and vector == request["resources"]
    )


def _trial_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _weight(value: object) -> int | None:
    vector = _resource_vector(value)
    if vector is None:
        return None
    return sum(vector.values())


def _reserved_weight(cur: Any, workspace_id: UUID, worker_id: str) -> int:
    cur.execute(
        """
        SELECT resources FROM omp_jobs.reservations
        WHERE workspace_id=%s AND worker_id=%s AND released_at IS NULL
        """,
        (workspace_id, worker_id),
    )
    total = 0
    for row in cur.fetchall():
        weight = _weight(row["resources"])
        if weight is not None:
            total += weight
    return total


def _root_budget_exhausted(
    store: NativeJobStore, cur: Any, workspace_id: UUID, job_id: str
) -> bool:
    """True when ``job_id``'s root item is budgeted and one dimension is spent out.

    The claim path calls this before leasing a candidate. An item with no
    published budget, or an unknown or parentless job, is never exhausted; a
    refused candidate is left ``backlog`` for the cancel path to stop.
    """
    work_id = root_work_id(cur, workspace_id, job_id)
    if work_id is None:
        return False
    budget = item_budget(cur, workspace_id, work_id)
    if budget is None:
        return False
    spend = item_spend(cur, workspace_id, work_id)
    return bool(
        spend.usd >= Decimal(budget.usd)
        or spend.tokens >= budget.tokens
        or spend.wall_clock_seconds >= budget.wall_clock_seconds
    )


def _claimable(
    store: NativeJobStore,
    cur: Any,
    workspace_id: UUID,
    job: dict[str, Any],
    held: set[str],
    free_weight: int,
    component_sha256: str,
) -> bool:
    weight = _weight(job["resources"])
    if weight is None or weight > free_weight:
        return False
    if job["lease_seconds"] is None:
        return False
    if not set(_json_list(job["required_capabilities"])) <= held:
        return False
    if store.cancelled_ancestor(cur, workspace_id, str(job["job_id"])) is not None:
        return False
    trial_id = job["trial_id"]
    if trial_id is None:
        return True
    return _trial_routable(cur, workspace_id, trial_id, component_sha256)


def _trial_routable(
    cur: Any, workspace_id: UUID, trial_id: UUID, component_sha256: str
) -> bool:
    row = _trial_row(cur, workspace_id, trial_id)
    if row is None:
        return False
    if row["trial_state"] != "proposed" or row["campaign_state"] not in _TRIAL_CAMPAIGN_STATES:
        return False
    compatibility = row["compatibility"]
    if isinstance(compatibility, str):
        try:
            compatibility = json.loads(compatibility)
        except json.JSONDecodeError:
            return False
    if not isinstance(compatibility, dict):
        return False
    workers = compatibility.get("workers") or []
    return component_sha256 in workers


def _admit(
    store: NativeJobStore,
    cur: Any,
    workspace_id: UUID,
    job_id: str,
    worker_id: str,
    operation_id: str,
) -> dict[str, object] | None:
    cur.execute(
        """
        UPDATE omp_jobs.jobs
        SET status='admitted',
            fence=fence + 1,
            attempt=attempt + 1,
            worker_id=%s,
            lease_expires_at=clock_timestamp() + (lease_seconds * interval '1 second'),
            updated_at=clock_timestamp()
        WHERE workspace_id=%s AND job_id=%s AND source='native' AND status='backlog'
        RETURNING fence, resources, lease_expires_at
        """,
        (worker_id, workspace_id, job_id),
    )
    updated = cur.fetchone()
    if updated is None:
        return None
    fence = int(updated["fence"])
    reservation_id = f"{job_id}:{fence}"
    cur.execute(
        """
        INSERT INTO omp_jobs.reservations(
            reservation_id, job_id, workspace_id, worker_id, fence, resources, expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            reservation_id,
            job_id,
            workspace_id,
            worker_id,
            fence,
            Jsonb(_resource_vector(updated["resources"])),
            updated["lease_expires_at"],
        ),
    )
    store.append_event(
        cur,
        workspace_id=workspace_id,
        job_id=job_id,
        kind="claimed",
        actor=worker_id,
        operation_id=operation_id,
        payload={
            "worker_id": worker_id,
            "fence": fence,
            "reservation_id": reservation_id,
        },
    )
    return {"status": "applied", "job": store.job_view(cur, workspace_id, job_id)}
