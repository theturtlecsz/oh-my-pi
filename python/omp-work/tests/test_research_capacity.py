"""OMP-318-s02-s02: research enqueue holds back engineering capacity.

A remembered CPU worker of capacity 2 refuses ``instrument:scope`` and a job
whose weight consumes the engineering reserve. Neither refusal writes a job
row. A job that leaves that reserve enqueues as compute with no worker assigned.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs import NativeJobStore
from omp_work.research.capacity import CapacityRefused, ResearchCapacity

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
