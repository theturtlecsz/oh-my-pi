"""Native job lease fencing (R03, OMP-324).

Proves a settlement is refused unless the caller holds the current lease, an
expired lease returns capacity, and a second identical settlement keeps the
receipts that were written the first time.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
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
        "lease_seconds": 30,
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


def _worker(
    native_jobs,
    store: NativeJobStore,
    capabilities: list[str],
    *,
    capacity: int = 1,
) -> str:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"fence-{uuid4()}",
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
        capacity=capacity,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def _evidence(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": f"ev-{uuid4()}",
        "kind": "receipt",
        "content_digest": "abcd1234ef",
        "trust": "verified",
    }
    body.update(overrides)
    return body


def _receipt(
    native_jobs, role: str = "audit", **overrides: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "role": role,
        "issuer_component_sha256": native_jobs.components[role],
        "evidence": _evidence(),
    }
    body.update(overrides)
    return body


def _settle(native_jobs, store: NativeJobStore, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "outcome": "succeeded",
        "receipts": [_receipt(native_jobs)],
    }
    body.update(overrides)
    return settle_job(store, **body)


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
                SELECT reservation_id, worker_id, fence, released_at, release_reason, expires_at
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


def _state(native_jobs, job_id: str) -> dict[str, object]:
    with _connect(native_jobs) as conn:
        job = conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        events = conn.execute(
            """
            SELECT seq, kind, actor, payload
            FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq
            """,
            (job_id,),
        ).fetchall()
        reservations = conn.execute(
            "SELECT * FROM omp_jobs.reservations WHERE job_id=%s ORDER BY reservation_id",
            (job_id,),
        ).fetchall()
    return {
        "job": None if job is None else dict(job),
        "events": [dict(row) for row in events],
        "reservations": [dict(row) for row in reservations],
    }


def _assert_refused(
    native_jobs, job_id: str, operation_id: str, call, code: str
) -> None:
    before = _state(native_jobs, job_id)
    with pytest.raises(JobError) as refused:
        call()
    assert refused.value.code == code
    assert _state(native_jobs, job_id) == before
    assert _operation(native_jobs, operation_id) is None


def test_stale_settle_is_refused_and_reclaim_advances_the_fence(native_jobs) -> None:
    """A backlog job, another worker, an expired lease, and the old fence after
    reconcile plus re-claim are refused. Re-claim's fence is one past reconcile."""
    store = _store(native_jobs)
    cap = _cap()
    holder = _worker(native_jobs, store, [cap], capacity=2)
    other = _worker(native_jobs, store, [cap], capacity=2)
    job = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=60)
    job_id = job["job"]["job_id"]

    backlog_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        backlog_op,
        lambda: _settle(
            native_jobs,
            store,
            operation_id=backlog_op,
            job_id=job_id,
            worker_id=holder,
            fence=0,
        ),
        "job_fence_stale",
    )
    assert _job(native_jobs, job_id)["status"] == "backlog"

    claimed = _claim(native_jobs, store, holder)
    assert claimed["job"]["job_id"] == job_id
    held_fence = claimed["job"]["fence"]
    assert held_fence == 1
    assert claimed["job"]["worker_id"] == holder

    other_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        other_op,
        lambda: _settle(
            native_jobs,
            store,
            operation_id=other_op,
            job_id=job_id,
            worker_id=other,
            fence=held_fence,
        ),
        "job_fence_stale",
    )

    _expire(native_jobs, job_id)
    expired_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        expired_op,
        lambda: _settle(
            native_jobs,
            store,
            operation_id=expired_op,
            job_id=job_id,
            worker_id=holder,
            fence=held_fence,
        ),
        "job_fence_stale",
    )
    assert _job(native_jobs, job_id)["status"] == "admitted"
    assert _job(native_jobs, job_id)["worker_id"] == holder

    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert reconciled["status"] == "applied"
    assert job_id in reconciled["job_ids"]
    returned = _job(native_jobs, job_id)
    assert returned["status"] == "backlog"
    assert returned["fence"] == held_fence + 1
    assert returned["worker_id"] is None
    assert returned["lease_expires_at"] is None
    released = _reservations(native_jobs, job_id)
    assert len(released) == 1
    assert released[0]["fence"] == held_fence
    assert released[0]["released_at"] is not None
    assert released[0]["release_reason"] == "lease_expired"
    assert _events(native_jobs, job_id) == ["enqueued", "claimed", "lease_expired"]

    reclaimed = _claim(native_jobs, store, holder)
    assert reclaimed["job"]["job_id"] == job_id
    assert reclaimed["job"]["fence"] == returned["fence"] + 1
    assert reclaimed["job"]["status"] == "admitted"
    assert reclaimed["job"]["worker_id"] == holder

    old_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        old_op,
        lambda: _settle(
            native_jobs,
            store,
            operation_id=old_op,
            job_id=job_id,
            worker_id=holder,
            fence=held_fence,
        ),
        "job_fence_stale",
    )
    current = _job(native_jobs, job_id)
    assert current["status"] == "admitted"
    assert current["fence"] == returned["fence"] + 1
    assert current["settlement"] is None
    assert current["settled_at"] is None


def test_expired_lease_frees_capacity_for_another_claim(native_jobs) -> None:
    """A full worker cannot take a second job until reconcile releases the expired lease."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap], capacity=1)
    older = _enqueue(
        native_jobs, store, required_capabilities=[cap], resources=_resources(cpu=1)
    )
    newer = _enqueue(
        native_jobs, store, required_capabilities=[cap], resources=_resources(cpu=1)
    )
    older_id = older["job"]["job_id"]
    newer_id = newer["job"]["job_id"]
    first = _claim(native_jobs, store, worker_id)
    assert first["job"]["job_id"] == older_id
    held_fence = first["job"]["fence"]
    assert _claim(native_jobs, store, worker_id)["job"] is None
    assert _job(native_jobs, newer_id)["status"] == "backlog"

    _expire(native_jobs, older_id)
    assert _claim(native_jobs, store, worker_id)["job"] is None
    assert _job(native_jobs, newer_id)["status"] == "backlog"

    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert older_id in reconciled["job_ids"]
    assert _job(native_jobs, older_id)["status"] == "backlog"
    assert _job(native_jobs, older_id)["fence"] == held_fence + 1
    taken = _claim(native_jobs, store, worker_id)
    assert taken["job"]["job_id"] == older_id
    assert taken["job"]["fence"] == held_fence + 2
    assert taken["job"]["status"] == "admitted"
    assert _job(native_jobs, newer_id)["status"] == "backlog"
    reservations = _reservations(native_jobs, older_id)
    assert [row["release_reason"] for row in reservations] == ["lease_expired", None]
    assert reservations[1]["released_at"] is None


def test_identical_settlement_replays_and_keeps_receipts(native_jobs) -> None:
    """A second identical settlement replays; a different one leaves receipts in place."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap], capacity=4)
    cases = (("succeeded", "sealed"), ("failed", "failed"))
    for outcome, status in cases:
        job = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=120)
        job_id = job["job"]["job_id"]
        claimed = _claim(native_jobs, store, worker_id)
        assert claimed["job"]["job_id"] == job_id
        fence = claimed["job"]["fence"]
        receipts = [
            _receipt(native_jobs, "audit"),
            _receipt(native_jobs, "evaluator"),
            _receipt(native_jobs, "release"),
        ]
        operation_id = str(uuid4())
        applied = _settle(
            native_jobs,
            store,
            operation_id=operation_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
            outcome=outcome,
            receipts=receipts,
        )
        assert applied["status"] == "applied"
        assert applied["job"]["status"] == status
        assert applied["job"]["settled_at"] is not None
        assert applied["settlement"] == {
            "outcome": outcome,
            "fence": fence,
            "worker_id": worker_id,
            "receipts": receipts,
        }
        stored = _job(native_jobs, job_id)
        assert stored["status"] == status
        assert stored["settlement"] == applied["settlement"]
        reservation = _reservations(native_jobs, job_id)[0]
        assert reservation["release_reason"] == "settled"
        assert reservation["released_at"] is not None
        assert _events(native_jobs, job_id) == ["enqueued", "claimed", "settled"]

        replayed = _settle(
            native_jobs,
            store,
            operation_id=operation_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
            outcome=outcome,
            receipts=receipts,
        )
        assert replayed["status"] == "replayed"
        assert replayed["settlement"] == applied["settlement"]

        again = _settle(
            native_jobs,
            store,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
            outcome=outcome,
            receipts=receipts,
        )
        assert again["status"] == "replayed"
        assert again["settlement"]["receipts"] == receipts
        assert _job(native_jobs, job_id)["settlement"] == stored["settlement"]
        assert _events(native_jobs, job_id) == ["enqueued", "claimed", "settled"]
        assert _reservations(native_jobs, job_id)[0]["released_at"] == reservation["released_at"]

        changed_op = str(uuid4())
        changed_receipts = [_receipt(native_jobs, "audit")]
        _assert_refused(
            native_jobs,
            job_id,
            changed_op,
            lambda: _settle(
                native_jobs,
                store,
                operation_id=changed_op,
                job_id=job_id,
                worker_id=worker_id,
                fence=fence,
                outcome=outcome,
                receipts=changed_receipts,
            ),
            "job_fence_stale",
        )
        assert _job(native_jobs, job_id)["settlement"]["receipts"] == receipts


def test_bad_receipts_are_refused(native_jobs) -> None:
    """Malformed evidence, a non-receipt kind, and a bad issuer write nothing."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap], capacity=2)
    job = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=120)
    job_id = job["job"]["job_id"]
    claimed = _claim(native_jobs, store, worker_id)
    assert claimed["job"]["job_id"] == job_id
    fence = claimed["job"]["fence"]
    valid = _receipt(native_jobs, "audit")
    cases = (
        _receipt(native_jobs, evidence=_evidence(content_digest="short", trust="nope")),
        _receipt(native_jobs, evidence={"id": "only"}),
        _receipt(native_jobs, evidence=_evidence(kind="metric", trust="untrusted")),
        _receipt(native_jobs, evidence=_evidence(kind="log_ref")),
        {
            "role": "audit",
            "issuer_component_sha256": native_jobs.components["worker"],
            "evidence": _evidence(),
        },
        {
            "role": "evaluator",
            "issuer_component_sha256": native_jobs.components["policy"],
            "evidence": _evidence(),
        },
        {
            "role": "release",
            "issuer_component_sha256": "a" * 64,
            "evidence": _evidence(),
        },
    )
    for receipts in cases:
        operation_id = str(uuid4())
        _assert_refused(
            native_jobs,
            job_id,
            operation_id,
            lambda receipts=receipts, operation_id=operation_id: _settle(
                native_jobs,
                store,
                operation_id=operation_id,
                job_id=job_id,
                worker_id=worker_id,
                fence=fence,
                receipts=[receipts] if isinstance(receipts, dict) else [valid, receipts],
            ),
            "invalid_request",
        )
    assert _job(native_jobs, job_id)["status"] == "admitted"
    assert _job(native_jobs, job_id)["settlement"] is None
    assert _events(native_jobs, job_id) == ["enqueued", "claimed"]
    assert _reservations(native_jobs, job_id)[0]["released_at"] is None


def test_renew_extends_expiry_by_lease_seconds(native_jobs) -> None:
    """Renewal adds lease_seconds to the job and reservation deadlines once per operation."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap])
    job = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=45)
    job_id = job["job"]["job_id"]
    claimed = _claim(native_jobs, store, worker_id)
    assert claimed["job"]["job_id"] == job_id
    fence = claimed["job"]["fence"]
    before_job = _job(native_jobs, job_id)["lease_expires_at"]
    before_reservation = _reservations(native_jobs, job_id)[0]["expires_at"]

    operation_id = str(uuid4())
    renewed = renew_lease(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert renewed["status"] == "applied"
    assert renewed["job"]["fence"] == fence
    assert renewed["job"]["worker_id"] == worker_id
    after_job = _job(native_jobs, job_id)["lease_expires_at"]
    after_reservation = _reservations(native_jobs, job_id)[0]["expires_at"]
    assert after_job - before_job == timedelta(seconds=45)
    assert after_reservation - before_reservation == timedelta(seconds=45)
    assert _events(native_jobs, job_id) == ["enqueued", "claimed"]

    replay = renew_lease(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert replay["status"] == "replayed"
    assert _job(native_jobs, job_id)["lease_expires_at"] == after_job

    stale_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        stale_op,
        lambda: renew_lease(
            store,
            operation_id=stale_op,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence + 1,
        ),
        "job_fence_stale",
    )
    _expire(native_jobs, job_id)
    expired_op = str(uuid4())
    _assert_refused(
        native_jobs,
        job_id,
        expired_op,
        lambda: renew_lease(
            store,
            operation_id=expired_op,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
        ),
        "job_fence_stale",
    )


def test_reconcile_skips_a_live_lease_and_a_second_call_is_empty(native_jobs) -> None:
    """Only expired admitted leases are reclaimed, and the next pass finds none."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap], capacity=3)
    first = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=30)
    second = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=30)
    live = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=3600)
    ids = []
    for job in (first, second, live):
        claimed = _claim(native_jobs, store, worker_id)
        assert claimed["job"]["job_id"] == job["job"]["job_id"]
        ids.append(claimed["job"]["job_id"])
    _expire(native_jobs, ids[0])
    _expire(native_jobs, ids[1])
    live_before = _state(native_jobs, ids[2])

    operation_id = str(uuid4())
    reconciled = reconcile_jobs(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert reconciled["status"] == "applied"
    assert reconciled["job_ids"].index(ids[0]) < reconciled["job_ids"].index(ids[1])
    assert ids[2] not in reconciled["job_ids"]
    assert _state(native_jobs, ids[2]) == live_before
    for job_id in ids[:2]:
        row = _job(native_jobs, job_id)
        assert row["status"] == "backlog"
        assert row["fence"] == 2
        assert row["worker_id"] is None
        assert row["lease_expires_at"] is None
        assert _events(native_jobs, job_id) == ["enqueued", "claimed", "lease_expired"]
        assert _reservations(native_jobs, job_id)[0]["release_reason"] == "lease_expired"

    replay = reconcile_jobs(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert replay["status"] == "replayed"
    assert replay["job_ids"] == reconciled["job_ids"]

    again = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert again["status"] == "applied"
    assert again["job_ids"] == []
    assert _job(native_jobs, ids[2])["status"] == "admitted"
