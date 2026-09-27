"""Native job descendant cancellation (R03, OMP-324).

Proves cancelling a root cancels every non-terminal descendant, releases a
leased child's reservation, and leaves the s02/s03 admission and lease rules
in force: enqueue and claim refuse the cancelled tree, reconcile does not
requeue it, a same-reason repeat replays, and a sealed job is refused.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs, renew_lease, settle_job
from omp_work.jobs.store import JobError, NativeJobStore, register_worker
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _resources(
    cpu: int = 1, memory_mib: int = 0, gpu: int = 0, model_calls: int = 0
) -> dict[str, int]:
    return {"cpu": cpu, "memory_mib": memory_mib, "gpu": gpu, "model_calls": model_calls}


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _work_id(native_jobs) -> UUID:
    return UUID(native_jobs.item["work_id"])


def _cap() -> str:
    return "isolate.a" + uuid4().hex


def _enqueue(native_jobs, store: NativeJobStore, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "job_id": f"job-{uuid4()}",
        "work_id": _work_id(native_jobs),
        "kind": "compute",
        "required_capabilities": ["compute.cpu"],
        "resources": _resources(),
        "lease_seconds": 60,
    }
    body.update(overrides)
    return enqueue_job(store, **body)


def _claim(native_jobs, store: NativeJobStore, worker_id: str) -> dict[str, object]:
    return claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )


def _worker(native_jobs, store: NativeJobStore, capabilities: list[str]) -> str:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"cancel-{uuid4()}",
        capabilities=tuple(sorted(set(capabilities))),
    )
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=component,
        capabilities=capabilities,
        capacity=1,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def _cancel(native_jobs, store: NativeJobStore, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "reason": "stop",
    }
    body.update(overrides)
    return cancel_job(store, **body)


def _evidence() -> dict[str, object]:
    return {
        "id": f"ev-{uuid4()}",
        "kind": "receipt",
        "content_digest": "abcd1234ef",
        "trust": "verified",
    }


def _receipt(native_jobs) -> dict[str, object]:
    return {
        "role": "audit",
        "issuer_component_sha256": native_jobs.components["audit"],
        "evidence": _evidence(),
    }


def _job(native_jobs, job_id: str) -> dict[str, object] | None:
    with _connect(native_jobs) as conn:
        return conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()


def _events(native_jobs, job_id: str) -> list[str]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
            (job_id,),
        ).fetchall()
    return [row["kind"] for row in rows]


def _reservations(native_jobs, job_id: str) -> list[dict[str, object]]:
    with _connect(native_jobs) as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT reservation_id, worker_id, fence, released_at, release_reason
                FROM omp_jobs.reservations WHERE job_id=%s ORDER BY reservation_id
                """,
                (job_id,),
            ).fetchall()
        ]


def _operation(native_jobs, operation_id: str) -> dict[str, object] | None:
    with _connect(native_jobs) as conn:
        return conn.execute(
            "SELECT operation_id FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()


def _insert_backlog(native_jobs, job_id: str, parent_job_id: str, cap: str) -> None:
    with _connect(native_jobs) as conn:
        conn.execute(
            """
            INSERT INTO omp_jobs.jobs(
                job_id, status, source, workspace_id, work_id, parent_job_id, kind,
                required_capabilities, resources, lease_seconds, fence, attempt
            ) VALUES (
                %s, 'backlog', 'native', %s, %s, %s, 'compute',
                %s, %s, 60, 0, 0
            )
            """,
            (
                job_id,
                native_jobs.workspace_id,
                _work_id(native_jobs),
                parent_job_id,
                Jsonb([cap]),
                Jsonb(_resources()),
            ),
        )


def _expire(native_jobs, job_id: str) -> None:
    with _connect(native_jobs) as conn:
        conn.execute(
            """
            UPDATE omp_jobs.jobs
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE job_id=%s
            """,
            (job_id,),
        )


def test_cancel_root_cancels_tree_and_blocks_later_admission(native_jobs) -> None:
    """A leased child in a three-level tree loses its reservation; the worker
    cannot renew or settle; enqueue, claim, and reconcile leave the cancel."""
    store = _store(native_jobs)
    child_cap = _cap()
    done_cap = _cap()
    holder = _worker(native_jobs, store, [child_cap])
    finisher = _worker(native_jobs, store, [done_cap])

    root = _enqueue(native_jobs, store, required_capabilities=["compute.cpu"])
    root_id = root["job"]["job_id"]
    child = _enqueue(
        native_jobs,
        store,
        parent_job_id=root_id,
        required_capabilities=[child_cap],
    )
    child_id = child["job"]["job_id"]
    grandchild = _enqueue(
        native_jobs,
        store,
        parent_job_id=child_id,
        required_capabilities=["compute.cpu"],
    )
    grandchild_id = grandchild["job"]["job_id"]
    sealed = _enqueue(
        native_jobs,
        store,
        parent_job_id=root_id,
        required_capabilities=[done_cap],
    )
    sealed_id = sealed["job"]["job_id"]
    failed = _enqueue(
        native_jobs,
        store,
        parent_job_id=root_id,
        required_capabilities=[done_cap],
    )
    failed_id = failed["job"]["job_id"]

    assert _claim(native_jobs, store, holder)["job"]["job_id"] == child_id
    held_fence = _job(native_jobs, child_id)["fence"]
    assert held_fence == 1
    assert _job(native_jobs, child_id)["worker_id"] == holder

    assert _claim(native_jobs, store, finisher)["job"]["job_id"] == sealed_id
    sealed_fence = _job(native_jobs, sealed_id)["fence"]
    settled = settle_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=sealed_id,
        worker_id=finisher,
        fence=sealed_fence,
        outcome="succeeded",
        receipts=[_receipt(native_jobs)],
    )
    assert settled["job"]["status"] == "sealed"
    assert _claim(native_jobs, store, finisher)["job"]["job_id"] == failed_id
    failed_fence = _job(native_jobs, failed_id)["fence"]
    marked = settle_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=failed_id,
        worker_id=finisher,
        fence=failed_fence,
        outcome="failed",
        receipts=[_receipt(native_jobs)],
    )
    assert marked["job"]["status"] == "failed"

    reason = "stop-the-tree"
    operation_id = str(uuid4())
    cancelled = _cancel(
        native_jobs,
        store,
        operation_id=operation_id,
        job_id=root_id,
        reason=reason,
    )
    cancelled_ids = sorted([root_id, child_id, grandchild_id])
    assert cancelled == {
        "operation_id": operation_id,
        "status": "applied",
        "job_ids": cancelled_ids,
    }
    replayed = _cancel(
        native_jobs,
        store,
        operation_id=operation_id,
        job_id=root_id,
        reason=reason,
    )
    assert replayed["status"] == "replayed"
    assert replayed["job_ids"] == cancelled_ids

    for job_id in cancelled_ids:
        row = _job(native_jobs, job_id)
        assert row["status"] == "cancelled"
        assert row["cancel_reason"] == reason
        assert row["cancelled_at"] is not None
        assert _events(native_jobs, job_id)[-1] == "cancelled"
    assert _job(native_jobs, root_id)["fence"] == 0
    assert _job(native_jobs, grandchild_id)["fence"] == 0
    assert _reservations(native_jobs, root_id) == []
    assert _reservations(native_jobs, grandchild_id) == []

    released_child = _job(native_jobs, child_id)
    assert released_child["fence"] == held_fence + 1
    assert released_child["worker_id"] is None
    assert released_child["settlement"] is None
    reservation = _reservations(native_jobs, child_id)
    assert len(reservation) == 1
    assert reservation[0]["reservation_id"] == f"{child_id}:{held_fence}"
    assert reservation[0]["fence"] == held_fence
    assert reservation[0]["worker_id"] == holder
    assert reservation[0]["released_at"] is not None
    assert reservation[0]["release_reason"] == "cancelled"
    assert _events(native_jobs, child_id) == ["enqueued", "claimed", "cancelled"]

    for job_id, status in ((sealed_id, "sealed"), (failed_id, "failed")):
        row = _job(native_jobs, job_id)
        assert row["status"] == status
        assert row["cancel_reason"] is None
        assert row["cancelled_at"] is None
        assert "cancelled" not in _events(native_jobs, job_id)

    child_before = _snapshot(native_jobs, child_id)
    renew_op = str(uuid4())
    with pytest.raises(JobError) as renew_refused:
        renew_lease(
            store,
            operation_id=renew_op,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=child_id,
            worker_id=holder,
            fence=held_fence,
        )
    assert renew_refused.value.code == "job_fence_stale"
    assert _snapshot(native_jobs, child_id) == child_before
    assert _operation(native_jobs, renew_op) is None

    settle_op = str(uuid4())
    with pytest.raises(JobError) as settle_refused:
        settle_job(
            store,
            operation_id=settle_op,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=child_id,
            worker_id=holder,
            fence=held_fence,
            outcome="succeeded",
            receipts=[_receipt(native_jobs)],
        )
    assert settle_refused.value.code == "job_fence_stale"
    assert _job(native_jobs, child_id)["status"] == "cancelled"
    assert _job(native_jobs, child_id)["settlement"] is None
    assert _operation(native_jobs, settle_op) is None
    assert _snapshot(native_jobs, child_id) == child_before

    child_under = f"under-{uuid4()}"
    enqueue_op = str(uuid4())
    with pytest.raises(JobError) as enqueued:
        _enqueue(
            native_jobs,
            store,
            operation_id=enqueue_op,
            job_id=child_under,
            parent_job_id=root_id,
        )
    assert enqueued.value.code == "job_cancelled"
    assert _job(native_jobs, child_under) is None
    assert _events(native_jobs, child_under) == []
    assert _operation(native_jobs, enqueue_op) is None

    fresh = NativeJobStore(native_jobs.service.config)
    with fresh.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        for job_id in cancelled_ids:
            viewed = fresh.job_view(cur, native_jobs.workspace_id, job_id)
            assert viewed is not None
            assert viewed["status"] == "cancelled"
            assert viewed["cancel_reason"] == reason
        assert fresh.job_view(cur, native_jobs.workspace_id, sealed_id)["status"] == "sealed"

    assert _claim(native_jobs, store, holder)["job"] is None
    blocked = f"blocked-{uuid4()}"
    _insert_backlog(native_jobs, blocked, root_id, child_cap)
    assert _claim(native_jobs, fresh, holder)["job"] is None
    assert _job(native_jobs, blocked)["status"] == "backlog"
    assert _reservations(native_jobs, blocked) == []

    _expire(native_jobs, child_id)
    reconciled = reconcile_jobs(
        fresh,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert reconciled["status"] == "applied"
    assert reconciled["job_ids"] == []
    assert _job(native_jobs, child_id)["status"] == "cancelled"
    assert _job(native_jobs, child_id)["fence"] == held_fence + 1
    assert _events(native_jobs, child_id) == ["enqueued", "claimed", "cancelled"]

    root_at = _job(native_jobs, root_id)["cancelled_at"]
    child_released_at = _reservations(native_jobs, child_id)[0]["released_at"]
    repeated = _cancel(native_jobs, fresh, job_id=root_id, reason=reason)
    assert repeated["status"] == "replayed"
    assert repeated["job_ids"] == cancelled_ids
    assert _job(native_jobs, root_id)["cancelled_at"] == root_at
    assert _reservations(native_jobs, child_id)[0]["released_at"] == child_released_at
    assert _job(native_jobs, blocked)["status"] == "backlog"
    assert _events(native_jobs, blocked) == []
    assert _events(native_jobs, child_id) == ["enqueued", "claimed", "cancelled"]

    conflict_op = str(uuid4())
    with pytest.raises(JobError) as conflict:
        _cancel(
            native_jobs,
            fresh,
            operation_id=conflict_op,
            job_id=root_id,
            reason="a-different-reason",
        )
    assert conflict.value.code == "idempotency_conflict"
    assert _job(native_jobs, root_id)["cancel_reason"] == reason
    assert _operation(native_jobs, conflict_op) is None
    assert _job(native_jobs, blocked)["status"] == "backlog"

    sealed_op = str(uuid4())
    with pytest.raises(JobError) as sealed_refused:
        _cancel(native_jobs, fresh, operation_id=sealed_op, job_id=sealed_id, reason=reason)
    assert sealed_refused.value.code == "invalid_request"
    assert _job(native_jobs, sealed_id)["status"] == "sealed"
    assert _job(native_jobs, sealed_id)["cancel_reason"] is None
    assert _operation(native_jobs, sealed_op) is None
    assert "cancelled" not in _events(native_jobs, sealed_id)

    failed_op = str(uuid4())
    with pytest.raises(JobError) as failed_refused:
        _cancel(native_jobs, fresh, operation_id=failed_op, job_id=failed_id, reason=reason)
    assert failed_refused.value.code == "invalid_request"
    assert _job(native_jobs, failed_id)["status"] == "failed"
    assert _operation(native_jobs, failed_op) is None


def test_unknown_job_is_refused(native_jobs) -> None:
    """A job id that is not in the workspace writes no operation."""
    store = _store(native_jobs)
    missing = f"missing-{uuid4()}"
    operation_id = str(uuid4())
    with pytest.raises(JobError) as refused:
        _cancel(native_jobs, store, operation_id=operation_id, job_id=missing, reason="stop")
    assert refused.value.code == "invalid_request"
    assert _job(native_jobs, missing) is None
    assert _operation(native_jobs, operation_id) is None


def _snapshot(native_jobs, job_id: str) -> dict[str, object]:
    with _connect(native_jobs) as conn:
        job = conn.execute(
            """
            SELECT status, fence, worker_id, cancel_reason, cancelled_at,
                   settlement, lease_expires_at
            FROM omp_jobs.jobs WHERE job_id=%s
            """,
            (job_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT seq, kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
            (job_id,),
        ).fetchall()
        reservations = conn.execute(
            """
            SELECT reservation_id, released_at, release_reason
            FROM omp_jobs.reservations WHERE job_id=%s ORDER BY reservation_id
            """,
            (job_id,),
        ).fetchall()
    return {
        "job": dict(job),
        "events": [dict(row) for row in events],
        "reservations": [dict(row) for row in reservations],
    }
