"""Refuse cancellation checks when a parent is outside the job workspace."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from psycopg.types.json import Jsonb

from omp_work.jobs.admission import claim_job
from omp_work.jobs.store import JobError, NativeJobStore, register_worker
from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from test_native_jobs_workers import _insert_native_job

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.mark.parametrize("source", ["native", "flood_import"])
def test_invisible_parent_refuses_cancellation_check_at_any_depth(native_jobs, source: str) -> None:
    """Foreign and mirror parents must cause an error, even through a local ancestor."""
    parent_id, child_id, grandchild_id = (str(uuid4()) for _ in range(3))
    _insert_native_job(
        native_jobs,
        job_id=parent_id,
        workspace_id=uuid4() if source == "native" else None,
        source=source,
        kind="compute" if source == "native" else None,
        status="cancelled",
    )
    _insert_native_job(native_jobs, job_id=child_id, parent_job_id=parent_id)
    _insert_native_job(native_jobs, job_id=grandchild_id, parent_job_id=child_id)
    store = NativeJobStore(native_jobs.service.config)

    for job_id in (child_id, grandchild_id):
        with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
            with pytest.raises(JobError) as refused:
                store.cancelled_ancestor(cur, native_jobs.workspace_id, job_id)
        assert refused.value.code == "invalid_request"


@pytest.mark.parametrize("source", ["native", "flood_import"])
def test_claim_job_skips_invisible_parent_and_claims_later_backlog(native_jobs, source: str) -> None:
    """A backlog job with an invisible parent does not halt claim_job for later jobs."""
    store = NativeJobStore(native_jobs.service.config)
    worker_id = f"worker-{uuid4()}"
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.gpu"],
        capacity=10,
    )

    parent_id = str(uuid4())
    bad_id = str(uuid4())
    good_id = str(uuid4())

    _insert_native_job(
        native_jobs,
        job_id=parent_id,
        workspace_id=uuid4() if source == "native" else None,
        source=source,
        kind="compute" if source == "native" else None,
        status="cancelled",
    )
    _insert_native_job(
        native_jobs,
        job_id=bad_id,
        parent_job_id=parent_id,
        lease_seconds=30,
        resources=Jsonb({"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}),
        required_capabilities=Jsonb(["compute.gpu"]),
    )
    _insert_native_job(
        native_jobs,
        job_id=good_id,
        lease_seconds=30,
        resources=Jsonb({"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}),
        required_capabilities=Jsonb(["compute.gpu"]),
    )

    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["status"] == "applied"
    assert claimed["job"] is not None
    assert claimed["job"]["job_id"] == good_id

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        bad_view = store.job_view(cur, native_jobs.workspace_id, bad_id)
        assert bad_view["status"] == "backlog"


@pytest.mark.parametrize("source", ["native", "flood_import"])
def test_claim_job_skips_invisible_grandparent_and_claims_later_backlog(native_jobs, source: str) -> None:
    """A backlog job with an invisible grandparent does not halt claim_job for later jobs."""
    store = NativeJobStore(native_jobs.service.config)
    worker_id = f"worker-{uuid4()}"
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.gpu"],
        capacity=10,
    )

    parent_id = str(uuid4())
    child_id = str(uuid4())
    grandchild_id = str(uuid4())
    good_id = str(uuid4())

    _insert_native_job(
        native_jobs,
        job_id=parent_id,
        workspace_id=uuid4() if source == "native" else None,
        source=source,
        kind="compute" if source == "native" else None,
        status="cancelled",
    )
    _insert_native_job(
        native_jobs,
        job_id=child_id,
        parent_job_id=parent_id,
        status="admitted",
    )
    _insert_native_job(
        native_jobs,
        job_id=grandchild_id,
        parent_job_id=child_id,
        lease_seconds=30,
        resources=Jsonb({"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}),
        required_capabilities=Jsonb(["compute.gpu"]),
    )
    _insert_native_job(
        native_jobs,
        job_id=good_id,
        lease_seconds=30,
        resources=Jsonb({"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}),
        required_capabilities=Jsonb(["compute.gpu"]),
    )

    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["status"] == "applied"
    assert claimed["job"] is not None
    assert claimed["job"]["job_id"] == good_id

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        bad_view = store.job_view(cur, native_jobs.workspace_id, grandchild_id)
        assert bad_view["status"] == "backlog"

