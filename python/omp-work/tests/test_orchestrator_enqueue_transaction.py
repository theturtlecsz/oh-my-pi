"""Caller-owned enqueue visibility, rollback, replay, and shared admission gates."""
from __future__ import annotations

import os
from contextlib import nullcontext
from uuid import uuid4

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work.jobs.store import JobError
from test_item_budget_alerts import _item_with_budget
from test_native_jobs_admission import (
    _cap,
    _claim,
    _enqueue,
    _events,
    _insert_job,
    _job,
    _store,
    _worker,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _operation(native_jobs, operation_id):
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        cur.execute(
            "SELECT result FROM omp_jobs.operations WHERE operation_id=%s AND workspace_id=%s",
            (operation_id, native_jobs.workspace_id),
        )
        return cur.fetchone()


def test_claimant_sees_job_only_after_caller_commit(native_jobs):
    """Independent claimant cannot run a job while its request is uncommitted."""
    store = _store(native_jobs)
    claimant = _store(native_jobs)
    cap = _cap()
    worker = _worker(native_jobs, claimant, [cap])
    job_id, operation_id = f"job-{uuid4()}", str(uuid4())
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        _enqueue(native_jobs, store, cur=cur, job_id=job_id,
                 operation_id=operation_id, required_capabilities=[cap])
        assert _job(native_jobs, job_id) is None
        assert _events(native_jobs, job_id) == []
        assert _operation(native_jobs, operation_id) is None
        assert _claim(native_jobs, claimant, worker, job_id=job_id)["job"] is None
    claimed = _claim(native_jobs, claimant, worker, job_id=job_id)
    assert claimed["job"]["job_id"] == job_id
    assert claimed["job"]["status"] == "admitted"
    assert _events(native_jobs, job_id) == ["enqueued", "claimed"]
    assert _operation(native_jobs, operation_id)["result"]["job"]["job_id"] == job_id


def test_caller_failure_rolls_back_job_event_and_operation(native_jobs):
    """Failure after enqueue leaves no runnable job or replay ledger residue."""
    store = _store(native_jobs)
    job_id, operation_id = f"job-{uuid4()}", str(uuid4())
    with pytest.raises(RuntimeError, match="request failed"):
        with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
            _enqueue(native_jobs, store, cur=cur, job_id=job_id, operation_id=operation_id)
            raise RuntimeError("request failed")
    assert _job(native_jobs, job_id) is None
    assert _events(native_jobs, job_id) == []
    assert _operation(native_jobs, operation_id) is None
    retry = _enqueue(native_jobs, store, job_id=job_id, operation_id=operation_id)
    assert retry["status"] == "applied"


@pytest.mark.parametrize("enlisted", [False, True], ids=["owned", "enlisted"])
def test_replay_and_changed_identity_use_shared_idempotency(native_jobs, enlisted):
    """Same operation adds no effect; changed job or operation identity refuses."""
    store = _store(native_jobs)
    job_id, operation_id = f"job-{uuid4()}", str(uuid4())
    transaction = (
        store.transaction(native_jobs.workspace_id, native_jobs.actor_id)
        if enlisted else nullcontext()
    )
    with transaction as cur:
        first = _enqueue(native_jobs, store, cur=cur, job_id=job_id, operation_id=operation_id)
        replay = _enqueue(native_jobs, store, cur=cur, job_id=job_id, operation_id=operation_id)
        assert first["status"] == "applied"
        assert replay["status"] == "replayed"
        assert replay["job"] == first["job"]
        for changed_operation in (operation_id, str(uuid4())):
            with pytest.raises(JobError) as refused:
                _enqueue(native_jobs, store, cur=cur, job_id=job_id,
                         operation_id=changed_operation, lease_seconds=31)
            assert refused.value.code == "idempotency_conflict"
            if changed_operation != operation_id:
                # No operation ledger entry for a rejected job identity.
                assert _operation(native_jobs, changed_operation) is None
    assert _events(native_jobs, job_id) == ["enqueued"]
    assert _job(native_jobs, job_id)["lease_seconds"] == 30


@pytest.mark.parametrize("enlisted", [False, True], ids=["owned", "enlisted"])
@pytest.mark.parametrize("gate", ["cancelled_parent", "missing_work", "invalid_resources", "missing_trial"])
def test_admission_gates_write_nothing(native_jobs, enlisted, gate):
    """Enlistment preserves parent, work, shape, and trial refusals."""
    store = _store(native_jobs)
    job_id, operation_id = f"job-{uuid4()}", str(uuid4())
    overrides = {}
    code = "invalid_request"
    if gate == "cancelled_parent":
        parent = f"parent-{uuid4()}"
        _insert_job(native_jobs, parent, status="cancelled", cancel_reason="stop")
        overrides = {"parent_job_id": parent}
        code = "job_cancelled"
    elif gate == "missing_work":
        overrides = {"work_id": uuid4()}
    elif gate == "invalid_resources":
        overrides = {"resources": {"cpu": -1, "memory_mib": 0, "gpu": 0, "model_calls": 0}}
    else:
        overrides = {"trial_id": uuid4()}
    transaction = (
        store.transaction(native_jobs.workspace_id, native_jobs.actor_id)
        if enlisted else nullcontext()
    )
    with pytest.raises(JobError) as refused:
        with transaction as cur:
            _enqueue(native_jobs, store, cur=cur, job_id=job_id, operation_id=operation_id, **overrides)
    assert refused.value.code == code
    assert _job(native_jobs, job_id) is None
    assert _events(native_jobs, job_id) == []
    assert _operation(native_jobs, operation_id) is None


@pytest.mark.parametrize("mode", ["owned", "enlisted_commit", "enlisted_rollback"])
def test_budget_refusal_respects_transaction_owner(native_jobs, mode):
    """Subagent ceiling cancels tree durably only if transaction owner commits."""
    store = _store(native_jobs)
    work_id = _item_with_budget(native_jobs, f"enqueue transaction budget {mode}", {
        "usd": "100", "tokens": 100000, "wall_clock_seconds": 3600, "max_subagents": 1,
    })
    parent = _enqueue(native_jobs, store, work_id=work_id)["job"]["job_id"]
    child = _enqueue(native_jobs, store, work_id=work_id, parent_job_id=parent)["job"]["job_id"]
    job_id, operation_id = f"job-{uuid4()}", str(uuid4())
    transaction = (
        nullcontext() if mode == "owned"
        else store.transaction(native_jobs.workspace_id, native_jobs.actor_id)
    )
    with transaction as cur:
        with pytest.raises(JobError) as refused:
            _enqueue(native_jobs, store, cur=cur, work_id=work_id,
                     parent_job_id=parent, job_id=job_id, operation_id=operation_id)
        assert refused.value.code == "budget_exceeded"
        if cur is not None:
            assert _job(native_jobs, parent)["status"] == "backlog"
            assert _operation(native_jobs, operation_id) is None
            if mode == "enlisted_rollback":
                raise psycopg.Rollback()
    assert _job(native_jobs, job_id) is None
    assert _events(native_jobs, job_id) == []
    if mode == "enlisted_rollback":
        assert _job(native_jobs, parent)["status"] == "backlog"
        assert _job(native_jobs, child)["status"] == "backlog"
        assert _events(native_jobs, parent) == ["enqueued"]
        assert _operation(native_jobs, operation_id) is None
    else:
        assert _job(native_jobs, parent)["status"] == "cancelled"
        assert _job(native_jobs, child)["status"] == "cancelled"
        assert _operation(native_jobs, operation_id)["result"] == {"status": "refused", "code": "budget_exceeded"}
