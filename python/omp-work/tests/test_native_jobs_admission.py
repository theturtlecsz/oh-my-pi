"""Native job enqueue and claim (R03, OMP-324).

Proves admission on the shared omp_jobs substrate: identical enqueue replays,
a different body conflicts, a cancelled ancestor writes nothing, and claim
leases one matching backlog job to an active worker.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.store import JobError, NativeJobStore, drain_worker, register_worker
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
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
    """A unique capability inside the research-component pattern."""
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


def _claim(native_jobs, store: NativeJobStore, worker_id: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "worker_id": worker_id,
    }
    body.update(overrides)
    return claim_job(store, **body)


def _worker(
    native_jobs,
    store: NativeJobStore,
    capabilities: list[str],
    *,
    capacity: int = 1,
    component: str | None = None,
) -> str:
    if component is None:
        component = _register_component(
            native_jobs.service,
            native_jobs.workspace_id,
            "worker",
            name=f"admit-{uuid4()}",
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


def _insert_job(native_jobs, job_id: str, **columns: object) -> None:
    fields: dict[str, object] = {
        "job_id": job_id,
        "status": "backlog",
        "source": "native",
        "workspace_id": native_jobs.workspace_id,
        "work_id": _work_id(native_jobs),
        "kind": "compute",
        "required_capabilities": Jsonb(["compute.cpu"]),
        "resources": Jsonb(_resources()),
        "lease_seconds": 30,
    }
    fields.update(columns)
    names = ",".join(fields)
    placeholders = ",".join(["%s"] * len(fields))
    with _connect(native_jobs) as conn:
        conn.execute(
            f"INSERT INTO omp_jobs.jobs({names}) VALUES ({placeholders})",  # nosec B608 - test-built
            tuple(fields.values()),
        )


def _events(native_jobs, job_id: str) -> list[str]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
            (job_id,),
        ).fetchall()
    return [row["kind"] for row in rows]


def _job(native_jobs, job_id: str) -> dict[str, object] | None:
    with _connect(native_jobs) as conn:
        return conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()


def _reservations(native_jobs, job_id: str) -> list[dict[str, object]]:
    with _connect(native_jobs) as conn:
        return list(
            conn.execute(
                """
                SELECT reservation_id, worker_id, fence, resources, expires_at
                FROM omp_jobs.reservations WHERE job_id=%s ORDER BY reservation_id
                """,
                (job_id,),
            ).fetchall()
        )


def test_enqueue_replays_identical_job_and_conflicts_on_change(native_jobs) -> None:
    """Same job id replays; a changed body or a changed operation request conflicts."""
    store = _store(native_jobs)
    job_id = f"job-{uuid4()}"
    operation_id = str(uuid4())
    capabilities = ["zeta", "alpha", "alpha"]
    first = _enqueue(
        native_jobs,
        store,
        operation_id=operation_id,
        job_id=job_id,
        required_capabilities=capabilities,
        resources=_resources(cpu=2, gpu=1),
        lease_seconds=45,
        kind="model",
    )
    assert first["status"] == "applied"
    job = first["job"]
    assert job["status"] == "backlog"
    assert job["source"] == "native"
    assert job["fence"] == 0
    assert job["attempt"] == 0
    assert job["kind"] == "model"
    assert job["required_capabilities"] == ["alpha", "zeta"]
    assert job["resources"] == _resources(cpu=2, gpu=1)
    assert job["lease_seconds"] == 45
    assert job["worker_id"] is None
    assert _events(native_jobs, job_id) == ["enqueued"]

    replay = _enqueue(
        native_jobs,
        store,
        job_id=job_id,
        required_capabilities=["zeta", "alpha"],
        resources=_resources(cpu=2, gpu=1),
        lease_seconds=45,
        kind="model",
    )
    assert replay["status"] == "replayed"
    assert replay["job"]["job_id"] == job_id
    assert _events(native_jobs, job_id) == ["enqueued"]

    same_operation = _enqueue(
        native_jobs,
        store,
        operation_id=operation_id,
        job_id=job_id,
        required_capabilities=["alpha", "zeta"],
        resources=_resources(gpu=1, cpu=2),
        lease_seconds=45,
        kind="model",
    )
    assert same_operation["status"] == "replayed"
    assert _events(native_jobs, job_id) == ["enqueued"]

    with pytest.raises(JobError) as changed:
        _enqueue(
            native_jobs,
            store,
            job_id=job_id,
            required_capabilities=["alpha", "zeta"],
            resources=_resources(cpu=2, gpu=1),
            lease_seconds=46,
            kind="model",
        )
    assert changed.value.code == "idempotency_conflict"
    with pytest.raises(JobError) as same_op:
        _enqueue(
            native_jobs,
            store,
            operation_id=operation_id,
            job_id=job_id,
            required_capabilities=["alpha", "zeta"],
            resources=_resources(cpu=2, gpu=1),
            lease_seconds=50,
            kind="model",
        )
    assert same_op.value.code == "idempotency_conflict"
    assert _job(native_jobs, job_id)["lease_seconds"] == 45
    assert _events(native_jobs, job_id) == ["enqueued"]


def test_enqueue_under_cancelled_parent_writes_nothing(native_jobs) -> None:
    """A parent or older ancestor cancelled through SQL leaves no child row."""
    store = _store(native_jobs)
    parent = f"parent-{uuid4()}"
    child = f"child-{uuid4()}"
    operation_id = str(uuid4())
    _insert_job(native_jobs, parent, status="cancelled", cancel_reason="stop")
    with pytest.raises(JobError) as refused:
        _enqueue(
            native_jobs,
            store,
            operation_id=operation_id,
            job_id=child,
            parent_job_id=parent,
        )
    assert refused.value.code == "job_cancelled"
    assert _job(native_jobs, child) is None
    assert _events(native_jobs, child) == []
    with _connect(native_jobs) as conn:
        stored = conn.execute(
            "SELECT operation_id FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()
    assert stored is None

    root = f"root-{uuid4()}"
    mid = f"mid-{uuid4()}"
    leaf = f"leaf-{uuid4()}"
    leaf_operation = str(uuid4())
    _insert_job(native_jobs, root, status="cancelled", cancel_reason="stop")
    _insert_job(native_jobs, mid, parent_job_id=root, status="backlog")
    with pytest.raises(JobError) as ancestor:
        _enqueue(
            native_jobs,
            store,
            operation_id=leaf_operation,
            job_id=leaf,
            parent_job_id=mid,
        )
    assert ancestor.value.code == "job_cancelled"
    assert _job(native_jobs, leaf) is None
    assert _events(native_jobs, leaf) == []
    with _connect(native_jobs) as conn:
        stored = conn.execute(
            "SELECT operation_id FROM omp_jobs.operations WHERE operation_id=%s",
            (leaf_operation,),
        ).fetchone()
    assert stored is None


def test_enqueue_refuses_invalid_request(native_jobs) -> None:
    """Shape, workspace, and trial checks refuse before a row is written."""
    store = _store(native_jobs)
    job_id = f"job-{uuid4()}"
    cases = (
        {"lease_seconds": 0},
        {"lease_seconds": 3601},
        {"lease_seconds": True},
        {"kind": "batch"},
        {"resources": {"cpu": 1, "memory_mib": 0, "gpu": 0}},
        {"resources": _resources(cpu=-1)},
        {"resources": {**_resources(), "disk": 1}},
        {"required_capabilities": ["ok", 1]},
        {"work_id": uuid4()},
        {"parent_job_id": f"missing-{uuid4()}"},
        {"trial_id": uuid4()},
    )
    for overrides in cases:
        with pytest.raises(JobError) as err:
            _enqueue(native_jobs, store, job_id=job_id, **overrides)
        assert err.value.code == "invalid_request"
    assert _job(native_jobs, job_id) is None


def test_claim_skips_worker_missing_a_capability(native_jobs) -> None:
    """A worker that lacks one required capability gets nothing; the job stays backlog."""
    store = _store(native_jobs)
    cap = _cap()
    other = _cap()
    worker_id = _worker(native_jobs, store, [other, cap])
    narrow = _worker(
        native_jobs,
        store,
        [other],
        component=_register_component(
            native_jobs.service,
            native_jobs.workspace_id,
            "worker",
            name=f"narrow-{uuid4()}",
            capabilities=(other, cap),
        ),
    )
    job = _enqueue(native_jobs, store, required_capabilities=[cap, other])
    job_id = job["job"]["job_id"]
    missed = _claim(native_jobs, store, narrow)
    assert missed["status"] == "applied"
    assert missed["job"] is None
    assert _job(native_jobs, job_id)["status"] == "backlog"
    assert _reservations(native_jobs, job_id) == []
    # The worker that holds both capabilities can take it, so the miss was the cap.
    taken = _claim(native_jobs, store, worker_id)
    assert taken["job"]["job_id"] == job_id
    assert taken["job"]["status"] == "admitted"


def test_draining_or_unknown_worker_is_unavailable(native_jobs) -> None:
    """A draining worker and an unknown worker are job_worker_unavailable."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap])
    drained = drain_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert drained["worker"]["state"] == "draining"
    job = _enqueue(native_jobs, store, required_capabilities=[cap])
    job_id = job["job"]["job_id"]
    operation_id = str(uuid4())
    with pytest.raises(JobError) as refused:
        _claim(native_jobs, store, worker_id, operation_id=operation_id)
    assert refused.value.code == "job_worker_unavailable"
    with pytest.raises(JobError) as unknown:
        _claim(native_jobs, store, f"missing-{uuid4()}")
    assert unknown.value.code == "job_worker_unavailable"
    assert _job(native_jobs, job_id)["status"] == "backlog"
    assert _reservations(native_jobs, job_id) == []
    with _connect(native_jobs) as conn:
        stored = conn.execute(
            "SELECT operation_id FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()
    assert stored is None


def test_capacity_blocks_a_second_claim(native_jobs) -> None:
    """One free slot leases one job; the next claim leaves the other job backlog."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap], capacity=1)
    older = _enqueue(
        native_jobs, store, required_capabilities=[cap], resources=_resources(cpu=1), lease_seconds=30
    )
    newer = _enqueue(
        native_jobs, store, required_capabilities=[cap], resources=_resources(cpu=1), lease_seconds=30
    )
    first = _claim(native_jobs, store, worker_id)
    second = _claim(native_jobs, store, worker_id)
    assert first["status"] == "applied"
    assert first["job"]["job_id"] == older["job"]["job_id"]
    assert first["job"]["status"] == "admitted"
    assert first["job"]["fence"] == 1
    assert first["job"]["attempt"] == 1
    assert first["job"]["worker_id"] == worker_id
    assert second["job"] is None
    assert _job(native_jobs, newer["job"]["job_id"])["status"] == "backlog"
    reservations = _reservations(native_jobs, older["job"]["job_id"])
    assert len(reservations) == 1
    assert reservations[0]["reservation_id"] == f"{older['job']['job_id']}:1"
    assert reservations[0]["worker_id"] == worker_id
    assert reservations[0]["fence"] == 1
    assert reservations[0]["resources"] == _resources(cpu=1)
    with _connect(native_jobs) as conn:
        timing = conn.execute(
            """
            SELECT r.expires_at = j.lease_expires_at AS same,
                   EXTRACT(EPOCH FROM (j.lease_expires_at - clock_timestamp())) AS ahead
            FROM omp_jobs.jobs j
            JOIN omp_jobs.reservations r ON r.job_id = j.job_id
            WHERE j.job_id=%s
            """,
            (older["job"]["job_id"],),
        ).fetchone()
    assert timing["same"] is True
    assert 0 < float(timing["ahead"]) <= 31
    assert _events(native_jobs, older["job"]["job_id"]) == ["enqueued", "claimed"]
    assert _reservations(native_jobs, newer["job"]["job_id"]) == []

    # Weight, not reservation count: capacity 3 holds cpu=2, refuses another
    # cpu=2, and still accepts cpu=1 with the leftover unit.
    wide = _cap()
    wide_worker = _worker(native_jobs, store, [wide], capacity=3)
    first_heavy = _enqueue(native_jobs, store, required_capabilities=[wide], resources=_resources(cpu=2))
    second_heavy = _enqueue(native_jobs, store, required_capabilities=[wide], resources=_resources(cpu=2))
    light = _enqueue(native_jobs, store, required_capabilities=[wide], resources=_resources(cpu=1))
    assert _claim(native_jobs, store, wide_worker)["job"]["job_id"] == first_heavy["job"]["job_id"]
    second = _claim(native_jobs, store, wide_worker)
    assert second["job"]["job_id"] == light["job"]["job_id"]
    assert _job(native_jobs, second_heavy["job"]["job_id"])["status"] == "backlog"


def test_concurrent_claims_lease_one_job_once(native_jobs) -> None:
    """Eight workers racing one backlog job produce a single lease."""
    store = _store(native_jobs)
    cap = _cap()
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"race-{uuid4()}",
        capabilities=(cap,),
    )
    workers = [
        _worker(native_jobs, store, [cap], capacity=4, component=component)
        for _ in range(8)
    ]
    job = _enqueue(native_jobs, store, required_capabilities=[cap], lease_seconds=60)
    job_id = job["job"]["job_id"]
    barrier = threading.Barrier(8)

    def once(worker_id: str) -> dict[str, object]:
        barrier.wait(timeout=30)
        return _claim(native_jobs, store, worker_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(once, workers))
    leased = [result for result in results if result["job"] is not None]
    assert len(leased) == 1
    assert leased[0]["job"]["job_id"] == job_id
    assert leased[0]["job"]["status"] == "admitted"
    assert leased[0]["job"]["fence"] == 1
    assert leased[0]["job"]["attempt"] == 1
    assert all(result["status"] == "applied" for result in results)
    assert _job(native_jobs, job_id)["attempt"] == 1
    assert len(_reservations(native_jobs, job_id)) == 1
    assert _events(native_jobs, job_id) == ["enqueued", "claimed"]


def test_trial_less_job_is_claimable_and_empty_claim_replays(native_jobs) -> None:
    """A job with no trial leases, and a committed empty claim does not lease later."""
    store = _store(native_jobs)
    cap = _cap()
    worker_id = _worker(native_jobs, store, [cap])
    operation_id = str(uuid4())
    empty = _claim(native_jobs, store, worker_id, operation_id=operation_id)
    assert empty == {
        "operation_id": operation_id,
        "status": "applied",
        "job": None,
    }
    job = _enqueue(native_jobs, store, required_capabilities=[cap], trial_id=None)
    job_id = job["job"]["job_id"]
    assert job["job"]["trial_id"] is None
    replay = _claim(native_jobs, store, worker_id, operation_id=operation_id)
    assert replay["status"] == "replayed"
    assert replay["job"] is None
    assert _job(native_jobs, job_id)["status"] == "backlog"
    taken = _claim(native_jobs, store, worker_id)
    assert taken["status"] == "applied"
    assert taken["job"]["job_id"] == job_id
    assert taken["job"]["trial_id"] is None
    assert taken["job"]["status"] == "admitted"
    expires = datetime.fromisoformat(str(taken["job"]["lease_expires_at"]))
    started = expires - timedelta(seconds=30)
    now = datetime.now(UTC)
    assert now - timedelta(seconds=20) <= started <= now + timedelta(seconds=5)


def test_worker_outside_compatibility_is_not_given_the_trial_job(native_jobs) -> None:
    """A trial job stays backlog for a worker whose component is not on the manifest."""
    store = _store(native_jobs)
    outside = _worker(native_jobs, store, ["compute.gpu"])
    job = _enqueue(
        native_jobs,
        store,
        required_capabilities=["compute.gpu"],
        trial_id=native_jobs.trial_id,
    )
    job_id = job["job"]["job_id"]
    missed = _claim(native_jobs, store, outside)
    assert missed["job"] is None
    assert _job(native_jobs, job_id)["status"] == "backlog"
    assert _reservations(native_jobs, job_id) == []
    inside = _worker(
        native_jobs,
        store,
        ["compute.gpu"],
        component=native_jobs.components["worker"],
    )
    taken = _claim(native_jobs, store, inside)
    assert taken["job"]["job_id"] == job_id
    assert taken["job"]["status"] == "admitted"


def test_claim_skips_a_cancelled_ancestor(native_jobs) -> None:
    """The oldest backlog row under a cancelled ancestor is not leased."""
    store = _store(native_jobs)
    cap = _cap()
    root = f"root-{uuid4()}"
    blocked = f"blocked-{uuid4()}"
    _insert_job(native_jobs, root, status="cancelled", cancel_reason="stop")
    _insert_job(
        native_jobs,
        blocked,
        parent_job_id=root,
        required_capabilities=Jsonb([cap]),
        resources=Jsonb(_resources()),
    )
    worker_id = _worker(native_jobs, store, [cap])
    live = _enqueue(native_jobs, store, required_capabilities=[cap])
    taken = _claim(native_jobs, store, worker_id)
    assert taken["job"]["job_id"] == live["job"]["job_id"]
    assert _job(native_jobs, blocked)["status"] == "backlog"
    assert _reservations(native_jobs, blocked) == []
