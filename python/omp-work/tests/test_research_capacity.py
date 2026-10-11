"""OMP-318-s02-s02: research enqueue holds back engineering capacity.

A remembered CPU worker of capacity 2 refuses ``instrument:scope`` and a job
whose weight consumes the engineering reserve. Neither refusal writes a job
row. A job that leaves that reserve enqueues as compute with no worker assigned.

OMP-318-s02-s03: the adapter's ``claim``/``cancel``/``reclaim`` are thin passes
to the native jobs API. One worker's free capacity admits both an
adapter-enqueued job and a directly enqueued engineering job, claim routing
filters on capabilities rather than capacity, and cleanup cancels every open
job so the module-scoped fixture holds no lease or backlog.

OMP-318-s02-s06: locality uses the s05 capability grammar and the adapter.
``_register_component`` records a worker named locality with compute.cpu,
compute.gpu, location:local, location:remote, and instrument:scope, and does
not replace the fixture worker component. Two uniquely named capacity-2
workers bind that sha: one holds compute.cpu and location:local, the other
holds compute.cpu and location:remote. Adapter enqueue of compute.cpu with
weight cpu=1, lease 60, trial_id None, and location local persists exactly
one location:local token. The remote claim returns no job; the local claim
returns that job admitted under its worker_id. A finally cancel with a fresh
operation id leaves no backlog or active lease.

OMP-318-s02-s08: lifecycle fencing. A capacity-2 compute.cpu worker on the
fixture worker sha claims a cpu=1 job (memory, gpu, and model calls 0,
lease 60). Cancel with reason "test cancellation", then settle of the saved
fence with a sound receipt, raises JobError job_fence_stale and the SQL row
stays cancelled. A fresh claim whose lease is moved one second into the past
is reclaimed by the adapter; the row is backlog at fence+1, and settle of
the saved holder and fence raises the same error while that row stays
backlog. Leftovers are cancelled in finally.
"""

from __future__ import annotations

import hashlib
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs import JobError, NativeJobStore, enqueue_job
from omp_work.research.capacity import CapacityRefused, ResearchCapacity
from omp_work.research.receipt import TrialReceipt
from test_research_contract import _register_component

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _resources(
    *,
    cpu: int = 0,
    memory_mib: int = 0,
    gpu: int = 0,
    model_calls: int = 0,
) -> dict[str, int]:
    return {
        "cpu": cpu,
        "memory_mib": memory_mib,
        "gpu": gpu,
        "model_calls": model_calls,
    }


def _job_rows(native_jobs, job_ids: list[str]) -> list[dict[str, object]]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        return list(
            conn.execute(
                """
                SELECT job_id, kind, parent_job_id, worker_id, work_id, trial_id
                FROM omp_jobs.jobs WHERE job_id = ANY(%s)
                """,
                (job_ids,),
            ).fetchall()
        )


def test_unknown_capability_and_headroom_write_no_job_rows(native_jobs) -> None:
    """instrument:scope and a weight of 2 against capacity 2 insert nothing."""
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    worker_id = f"cpu-{uuid4()}"
    registered = gate.register(
        str(uuid4()),
        worker_id,
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    assert registered["status"] == "applied"
    assert registered["worker"]["worker_id"] == worker_id
    assert registered["worker"]["capacity"] == 2

    work_id = UUID(native_jobs.item["work_id"])
    mismatch_id = f"job-{uuid4()}"
    held = ["instrument:scope"]
    with pytest.raises(CapacityRefused) as mismatch:
        gate.enqueue(
            str(uuid4()),
            mismatch_id,
            work_id,
            held,
            _resources(),
            30,
        )
    assert mismatch.value.code == "capability_mismatch"
    assert held == ["instrument:scope"]

    headroom_id = f"job-{uuid4()}"
    with pytest.raises(CapacityRefused) as headroom:
        gate.enqueue(
            str(uuid4()),
            headroom_id,
            work_id,
            ["compute.cpu"],
            _resources(cpu=2),
            30,
        )
    assert headroom.value.code == "engineering_headroom"

    located = ["compute.cpu"]
    located_id = f"job-{uuid4()}"
    with pytest.raises(CapacityRefused) as located_refusal:
        gate.enqueue(
            str(uuid4()),
            located_id,
            work_id,
            located,
            _resources(model_calls=1),
            30,
            location="bench",
        )
    assert located_refusal.value.code == "capability_mismatch"
    assert located == ["compute.cpu"]
    assert _job_rows(native_jobs, [mismatch_id, headroom_id, located_id]) == []


def test_enqueue_compute_when_headroom_remains(native_jobs) -> None:
    """Weight that leaves the reserve is a compute job with no worker assigned.

    A second, larger worker admits a job the capacity-2 worker cannot cover,
    so the reserve is per worker and one fit is enough.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    gate.register(
        str(uuid4()),
        f"cpu-{uuid4()}",
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    work_id = UUID(native_jobs.item["work_id"])
    trial_id = native_jobs.trial_id
    assert isinstance(work_id, UUID)
    assert isinstance(trial_id, UUID)
    operation_id = str(uuid4())
    job_id = f"job-{uuid4()}"
    resources = _resources(cpu=1)
    result = gate.enqueue(
        operation_id,
        job_id,
        work_id,
        ["compute.cpu"],
        resources,
        30,
        trial_id=trial_id,
    )
    assert result["operation_id"] == operation_id
    assert result["status"] == "applied"
    job = result["job"]
    assert job["kind"] == "compute"
    assert job["parent_job_id"] is None
    assert job["worker_id"] is None
    assert job["work_id"] == str(work_id)
    assert job["trial_id"] == str(trial_id)
    assert job["resources"] == resources
    assert job["required_capabilities"] == ["compute.cpu"]
    assert job["lease_seconds"] == 30

    heavy_id = f"job-{uuid4()}"
    heavy = _resources(cpu=2, memory_mib=1)
    with pytest.raises(CapacityRefused) as refused:
        gate.enqueue(
            str(uuid4()),
            heavy_id,
            work_id,
            ["compute.cpu"],
            heavy,
            30,
            trial_id=trial_id,
        )
    assert refused.value.code == "engineering_headroom"

    gate.register(
        str(uuid4()),
        f"cpu-{uuid4()}",
        native_jobs.components["worker"],
        ["compute.cpu"],
        4,
    )
    heavy_operation = str(uuid4())
    admitted = gate.enqueue(
        heavy_operation,
        heavy_id,
        work_id,
        ["compute.cpu"],
        heavy,
        45,
        trial_id=trial_id,
    )
    assert admitted["status"] == "applied"
    assert admitted["job"]["worker_id"] is None
    assert admitted["job"]["kind"] == "compute"
    assert admitted["job"]["parent_job_id"] is None
    assert admitted["job"]["resources"] == heavy

    rows = {row["job_id"]: row for row in _job_rows(native_jobs, [job_id, heavy_id])}
    assert set(rows) == {job_id, heavy_id}
    for row in rows.values():
        assert row["kind"] == "compute"
        assert row["parent_job_id"] is None
        assert row["worker_id"] is None
        assert row["work_id"] == work_id
        assert row["trial_id"] == trial_id


_RESOURCE_DIAGNOSTIC = (
    "resources must be cpu, memory_mib, gpu, and model_calls integers >= 0",
)


def test_malformed_resources_raise_flat_diagnostics_and_write_no_row(native_jobs) -> None:
    """A bad resource vector is invalid_request with one string diagnostic.

    Diagnostics stay ``tuple[str, ...]`` for a wrong shape and for a negative,
    boolean, or non-integer value, so joining them does not raise. No case
    writes a job row.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    gate.register(
        str(uuid4()),
        f"cpu-{uuid4()}",
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    work_id = UUID(native_jobs.item["work_id"])
    cases: list[object] = [
        None,
        [],
        {},
        {"cpu": 1},
        {**_resources(), "disk": 1},
        _resources(cpu=-1),
        {"cpu": True, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        {"cpu": 1.5, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        {"cpu": "1", "memory_mib": 0, "gpu": 0, "model_calls": 0},
    ]
    job_ids: list[str] = []
    for resources in cases:
        job_id = f"job-{uuid4()}"
        job_ids.append(job_id)
        required = ["compute.cpu"]
        with pytest.raises(JobError) as refused:
            gate.enqueue(
                str(uuid4()),
                job_id,
                work_id,
                required,
                resources,  # type: ignore[arg-type]
                30,
            )
        error = refused.value
        assert error.code == "invalid_request"
        assert error.diagnostics == _RESOURCE_DIAGNOSTIC
        assert all(isinstance(item, str) for item in error.diagnostics)
        assert _RESOURCE_DIAGNOSTIC[0] in " ".join(error.diagnostics)
        assert required == ["compute.cpu"]
    assert _job_rows(native_jobs, job_ids) == []


_CLEANUP_REASON = "research capacity test cleanup"


def _status_rows(native_jobs, job_ids: list[str]) -> dict[str, dict[str, object]]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        rows = conn.execute(
            """
            SELECT job_id, status, worker_id
            FROM omp_jobs.jobs WHERE job_id = ANY(%s)
            """,
            (job_ids,),
        ).fetchall()
    return {str(row["job_id"]): row for row in rows}


def _open_job_ids(native_jobs) -> list[str]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        return [
            str(row["job_id"])
            for row in conn.execute(
                """
                SELECT job_id FROM omp_jobs.jobs
                WHERE workspace_id = %s AND status IN ('backlog', 'admitted')
                ORDER BY created_at, job_id
                """,
                (native_jobs.workspace_id,),
            ).fetchall()
        ]


def _unreleased_reservations(native_jobs, worker_id: str) -> int:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        row = conn.execute(
            """
            SELECT count(*) AS held FROM omp_jobs.reservations
            WHERE workspace_id = %s AND worker_id = %s AND released_at IS NULL
            """,
            (native_jobs.workspace_id, worker_id),
        ).fetchone()
    return int(row["held"])


def _cancel_open(native_jobs, gate: ResearchCapacity) -> None:
    """Cancel every backlog or admitted job left in the fixture workspace."""
    for open_id in _open_job_ids(native_jobs):
        gate.cancel(str(uuid4()), open_id, _CLEANUP_REASON)


def test_claim_assigns_adapter_and_direct_jobs_from_shared_headroom(native_jobs) -> None:
    """One capacity-2 worker admits an adapter job and a direct engineering job.

    Both jobs weigh cpu=1; the gate's claim draws both from the same worker's
    free capacity in enqueue order and returns each admitted job with its
    worker_id and fence. With nothing left to route a third claim is
    ``job=None``, ``reclaim`` finds no expired lease, and the persisted rows
    are admitted under the same worker.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    worker_id = f"cpu-{uuid4()}"
    gate.register(
        str(uuid4()),
        worker_id,
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    work_id = UUID(native_jobs.item["work_id"])
    adapter_job_id = f"job-{uuid4()}"
    engineering_job_id = f"job-{uuid4()}"
    try:
        _cancel_open(native_jobs, gate)
        adapter = gate.enqueue(
            str(uuid4()),
            adapter_job_id,
            work_id,
            ["compute.cpu"],
            _resources(cpu=1),
            30,
            trial_id=native_jobs.trial_id,
        )
        assert adapter["status"] == "applied"
        engineering = enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=engineering_job_id,
            work_id=work_id,
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources=_resources(cpu=1),
            lease_seconds=30,
            parent_job_id=None,
            trial_id=None,
        )
        assert engineering["status"] == "applied"

        first = gate.claim(str(uuid4()), worker_id)
        second = gate.claim(str(uuid4()), worker_id)
        assert [first["job"]["job_id"], second["job"]["job_id"]] == [
            adapter_job_id,
            engineering_job_id,
        ]
        for claimed in (first, second):
            assert claimed["status"] == "applied"
            assert claimed["job"]["status"] == "admitted"
            assert claimed["job"]["worker_id"] == worker_id
            assert claimed["job"]["fence"] == 1

        exhausted = gate.claim(str(uuid4()), worker_id)
        assert exhausted["status"] == "applied"
        assert exhausted["job"] is None

        rows = _status_rows(native_jobs, [adapter_job_id, engineering_job_id])
        assert set(rows) == {adapter_job_id, engineering_job_id}
        for row in rows.values():
            assert row["status"] == "admitted"
            assert row["worker_id"] == worker_id

        reclaimed = gate.reclaim(str(uuid4()))
        assert reclaimed["status"] == "applied"
        assert reclaimed["job_ids"] == []

        cancelled = gate.cancel(str(uuid4()), engineering_job_id, "assignment done")
        assert cancelled["status"] == "applied"
        assert cancelled["job_ids"] == [engineering_job_id]
        assert _status_rows(native_jobs, [engineering_job_id])[engineering_job_id][
            "status"
        ] == "cancelled"
    finally:
        _cancel_open(native_jobs, gate)
    assert _open_job_ids(native_jobs) == []


def _sound_receipt(native_jobs, job_id: str) -> TrialReceipt:
    return TrialReceipt(
        trial_id=str(native_jobs.trial_id),
        campaign_id=str(native_jobs.campaign_id),
        job_id=job_id,
        candidate_digest="a" * 64,
        evaluator_component_sha256="b" * 64,
        evaluator_artifact_sha256="c" * 64,
        protocol_sha256="d" * 64,
        negative_control="rejected",
        verdict="qualified",
        metrics={"score": 1.0},
    )


@pytest.mark.parametrize("receipt_as_bytes", [False, True], ids=["model", "bytes"])
def test_settle_rejects_bad_receipt_then_seals_verified_evidence(
    native_jobs, receipt_as_bytes: bool,
) -> None:
    """Rejected receipts leave a lease admitted; sound receipts seal with its digest.

    Native fence errors still reach the caller without changing job state.
    Both model serialization and supplied canonical bytes produce evidence
    persisted under the registered evaluator component.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    worker_id = f"cpu-{uuid4()}"
    gate.register(
        str(uuid4()), worker_id, native_jobs.components["worker"], ["compute.cpu"], 2,
    )
    job_id = f"job-{uuid4()}"
    evaluator = native_jobs.components["evaluator"]
    try:
        _cancel_open(native_jobs, gate)
        gate.enqueue(
            str(uuid4()), job_id, UUID(native_jobs.item["work_id"]),
            ["compute.cpu"], _resources(cpu=1), 30,
            trial_id=native_jobs.trial_id,
        )
        claimed = gate.claim(str(uuid4()), worker_id)
        assert claimed["job"]["job_id"] == job_id
        fence = claimed["job"]["fence"]
        with pytest.raises(CapacityRefused) as rejected:
            gate.settle(
                str(uuid4()), job_id, worker_id, fence, "succeeded", b"not-json", evaluator,
            )
        assert rejected.value.code == "receipt_rejected"
        assert _status_rows(native_jobs, [job_id])[job_id]["status"] == "admitted"

        sound = _sound_receipt(native_jobs, job_id)
        receipt = sound.receipt_bytes() if receipt_as_bytes else sound
        with pytest.raises(JobError) as stale:
            gate.settle(
                str(uuid4()), job_id, worker_id, fence + 1, "succeeded", receipt, evaluator,
            )
        assert stale.value.code == "job_fence_stale"
        assert _status_rows(native_jobs, [job_id])[job_id]["status"] == "admitted"

        operation_id = str(uuid4())
        result = gate.settle(
            operation_id, job_id, worker_id, fence, "succeeded", receipt, evaluator,
        )
        assert result["operation_id"] == operation_id
        assert result["status"] == "applied"
        assert result["job"]["status"] == "sealed"
        assert result["settlement"] == {
            "outcome": "succeeded",
            "worker_id": worker_id,
            "fence": fence,
            "receipts": [{
                "role": "evaluator",
                "issuer_component_sha256": evaluator,
                "evidence": {
                    "id": job_id,
                    "kind": "receipt",
                    "content_digest": hashlib.sha256(sound.receipt_bytes()).hexdigest(),
                    "trust": "verified",
                },
            }],
        }
        with psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            row_factory=dict_row,
            autocommit=True,
        ) as conn:
            row = conn.execute(
                "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        assert row["status"] == "sealed"
        assert row["settlement"] == result["settlement"]
        assert _unreleased_reservations(native_jobs, worker_id) == 0
    finally:
        _cancel_open(native_jobs, gate)


def test_claim_filters_on_capability_not_free_capacity(native_jobs) -> None:
    """A cpu-only worker with free capacity does not claim a gpu job.

    The gpu job is routable — the gpu worker claims it — so the cpu worker's
    ``job=None`` is capability filtering, not missing headroom.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    cpu_worker = f"cpu-{uuid4()}"
    gpu_worker = f"gpu-{uuid4()}"
    gate.register(
        str(uuid4()),
        cpu_worker,
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    gate.register(
        str(uuid4()),
        gpu_worker,
        native_jobs.components["worker"],
        ["compute.gpu"],
        2,
    )
    work_id = UUID(native_jobs.item["work_id"])
    gpu_job_id = f"job-{uuid4()}"
    try:
        _cancel_open(native_jobs, gate)
        enqueued = gate.enqueue(
            str(uuid4()),
            gpu_job_id,
            work_id,
            ["compute.gpu"],
            _resources(gpu=1),
            30,
            trial_id=native_jobs.trial_id,
        )
        assert enqueued["status"] == "applied"

        assert _unreleased_reservations(native_jobs, cpu_worker) == 0
        cpu_claim = gate.claim(str(uuid4()), cpu_worker)
        assert cpu_claim["status"] == "applied"
        assert cpu_claim["job"] is None

        gpu_claim = gate.claim(str(uuid4()), gpu_worker)
        assert gpu_claim["status"] == "applied"
        assert gpu_claim["job"]["job_id"] == gpu_job_id
        assert gpu_claim["job"]["status"] == "admitted"
        assert gpu_claim["job"]["worker_id"] == gpu_worker
        assert gpu_claim["job"]["fence"] == 1
        row = _status_rows(native_jobs, [gpu_job_id])[gpu_job_id]
        assert row["status"] == "admitted"
        assert row["worker_id"] == gpu_worker
    finally:
        _cancel_open(native_jobs, gate)
    assert _open_job_ids(native_jobs) == []


def _persisted_capabilities(native_jobs, job_id: str) -> list[str]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        row = conn.execute(
            """
            SELECT required_capabilities FROM omp_jobs.jobs
            WHERE workspace_id = %s AND job_id = %s
            """,
            (native_jobs.workspace_id, job_id),
        ).fetchone()
    assert row is not None
    return list(row["required_capabilities"])


def test_locality_routes_the_located_job_to_the_local_worker(native_jobs) -> None:
    """location:local is persisted once and claimed only by the local worker.

    The locality component is the s05 grammar registered through the research
    contract. The fixture worker map is left as the campaign recorded it.
    trial_id None keeps the job off campaign compatibility, which does not
    list this component. The remote worker has no location:local token, so
    its claim misses; the local worker admits the same job.
    """
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    fixture_worker = native_jobs.components["worker"]
    locality_sha = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name="locality",
        capabilities=(
            "compute.cpu",
            "compute.gpu",
            "location:local",
            "location:remote",
            "instrument:scope",
        ),
    )
    assert native_jobs.components["worker"] == fixture_worker
    assert locality_sha != fixture_worker

    local_worker_id = f"local-{uuid4()}"
    remote_worker_id = f"remote-{uuid4()}"
    local_worker = gate.register(
        str(uuid4()),
        local_worker_id,
        locality_sha,
        ["compute.cpu", "location:local"],
        2,
    )
    remote_worker = gate.register(
        str(uuid4()),
        remote_worker_id,
        locality_sha,
        ["compute.cpu", "location:remote"],
        2,
    )
    assert local_worker["status"] == "applied"
    assert remote_worker["status"] == "applied"
    assert local_worker["worker"]["component_sha256"] == locality_sha
    assert remote_worker["worker"]["component_sha256"] == locality_sha
    assert local_worker["worker"]["capacity"] == 2
    assert remote_worker["worker"]["capacity"] == 2
    assert local_worker["worker"]["capabilities"] == ["compute.cpu", "location:local"]
    assert remote_worker["worker"]["capabilities"] == [
        "compute.cpu",
        "location:remote",
    ]
    assert "location:local" not in remote_worker["worker"]["capabilities"]

    work_id = UUID(native_jobs.item["work_id"])
    job_id = f"job-{uuid4()}"
    required = ["compute.cpu"]
    resources = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
    try:
        _cancel_open(native_jobs, gate)
        enqueued = gate.enqueue(
            str(uuid4()),
            job_id,
            work_id,
            required,
            resources,
            60,
            trial_id=None,
            location="local",
        )
        assert enqueued["status"] == "applied"
        assert required == ["compute.cpu"]
        persisted = _persisted_capabilities(native_jobs, job_id)
        assert persisted.count("location:local") == 1
        assert persisted == ["compute.cpu", "location:local"]

        remote_claim = gate.claim(str(uuid4()), remote_worker_id)
        assert remote_claim["status"] == "applied"
        assert remote_claim["job"] is None
        waiting = _status_rows(native_jobs, [job_id])[job_id]
        assert waiting["status"] == "backlog"
        assert waiting["worker_id"] is None

        local_claim = gate.claim(str(uuid4()), local_worker_id)
        assert local_claim["status"] == "applied"
        assert local_claim["job"]["job_id"] == job_id
        assert local_claim["job"]["status"] == "admitted"
        assert local_claim["job"]["worker_id"] == local_worker_id
        admitted = _status_rows(native_jobs, [job_id])[job_id]
        assert admitted["status"] == "admitted"
        assert admitted["worker_id"] == local_worker_id
        assert _unreleased_reservations(native_jobs, remote_worker_id) == 0
        assert _unreleased_reservations(native_jobs, local_worker_id) == 1
    finally:
        _cancel_open(native_jobs, gate)
    assert _open_job_ids(native_jobs) == []
    assert _unreleased_reservations(native_jobs, local_worker_id) == 0
    assert _unreleased_reservations(native_jobs, remote_worker_id) == 0


def _lifecycle_row(native_jobs, job_id: str) -> dict[str, object]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        row = conn.execute(
            """
            SELECT status, fence
            FROM omp_jobs.jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


def _cpu_worker(native_jobs, gate: ResearchCapacity) -> str:
    worker_id = f"cpu-{uuid4()}"
    registered = gate.register(
        str(uuid4()),
        worker_id,
        native_jobs.components["worker"],
        ["compute.cpu"],
        2,
    )
    assert registered["status"] == "applied"
    assert registered["worker"]["capacity"] == 2
    assert registered["worker"]["component_sha256"] == native_jobs.components["worker"]
    return worker_id


def _enqueue_cpu(native_jobs, gate: ResearchCapacity, job_id: str) -> None:
    resources = _resources(cpu=1, memory_mib=0, gpu=0, model_calls=0)
    enqueued = gate.enqueue(
        str(uuid4()),
        job_id,
        UUID(native_jobs.item["work_id"]),
        ["compute.cpu"],
        resources,
        60,
        trial_id=native_jobs.trial_id,
    )
    assert enqueued["status"] == "applied"
    assert enqueued["job"]["resources"] == resources
    assert enqueued["job"]["lease_seconds"] == 60


def test_stale_settlement_after_cancel_leaves_the_row_cancelled(native_jobs) -> None:
    """Cancel, then settle the saved fence: job_fence_stale, row stays cancelled."""
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    worker_id = _cpu_worker(native_jobs, gate)
    job_id = f"job-{uuid4()}"
    evaluator = native_jobs.components["evaluator"]
    try:
        _cancel_open(native_jobs, gate)
        _enqueue_cpu(native_jobs, gate, job_id)
        claimed = gate.claim(str(uuid4()), worker_id)
        assert claimed["status"] == "applied"
        assert claimed["job"]["job_id"] == job_id
        saved_fence = claimed["job"]["fence"]
        saved_worker_id = claimed["job"]["worker_id"]
        assert saved_worker_id == worker_id
        cancelled = gate.cancel(str(uuid4()), job_id, "test cancellation")
        assert cancelled["status"] == "applied"
        assert cancelled["job_ids"] == [job_id]
        with pytest.raises(JobError) as stale:
            gate.settle(
                str(uuid4()),
                job_id,
                saved_worker_id,
                saved_fence,
                "succeeded",
                _sound_receipt(native_jobs, job_id),
                evaluator,
            )
        assert stale.value.code == "job_fence_stale"
        assert _lifecycle_row(native_jobs, job_id)["status"] == "cancelled"
    finally:
        _cancel_open(native_jobs, gate)


def test_stale_settlement_after_reclaim_leaves_the_row_backlog(native_jobs) -> None:
    """Expire, reclaim, then settle the saved fence: job_fence_stale, fence stays +1."""
    store = NativeJobStore(native_jobs.service.config)
    gate = ResearchCapacity(store, native_jobs.workspace_id, native_jobs.actor_id)
    worker_id = _cpu_worker(native_jobs, gate)
    job_id = f"job-{uuid4()}"
    evaluator = native_jobs.components["evaluator"]
    try:
        _cancel_open(native_jobs, gate)
        _enqueue_cpu(native_jobs, gate, job_id)
        claimed = gate.claim(str(uuid4()), worker_id)
        assert claimed["status"] == "applied"
        assert claimed["job"]["job_id"] == job_id
        saved_fence = claimed["job"]["fence"]
        saved_worker_id = claimed["job"]["worker_id"]
        assert saved_worker_id == worker_id
        with psycopg.connect(
            **native_jobs.service.config.connection_kwargs("omp_work_app"),
            row_factory=dict_row,
            autocommit=True,
        ) as conn:
            conn.execute(
                """
                UPDATE omp_jobs.jobs
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE job_id=%s
                """,
                (job_id,),
            )
        reclaimed = gate.reclaim(str(uuid4()))
        assert reclaimed["status"] == "applied"
        assert job_id in reclaimed["job_ids"]
        returned = _lifecycle_row(native_jobs, job_id)
        assert returned["status"] == "backlog"
        assert returned["fence"] == saved_fence + 1
        with pytest.raises(JobError) as stale:
            gate.settle(
                str(uuid4()),
                job_id,
                saved_worker_id,
                saved_fence,
                "succeeded",
                _sound_receipt(native_jobs, job_id),
                evaluator,
            )
        assert stale.value.code == "job_fence_stale"
        stayed = _lifecycle_row(native_jobs, job_id)
        assert stayed["status"] == "backlog"
        assert stayed["fence"] == saved_fence + 1
    finally:
        _cancel_open(native_jobs, gate)
