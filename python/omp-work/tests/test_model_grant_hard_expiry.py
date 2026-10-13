"""HTTP grants obey durable task deadlines without extending lease authority."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from omp_work import model_proxy
from omp_work.jobs.lease import renew_lease
from omp_work.orchestrator.step_log import append_step
from test_model_proxy import call, proxy, spend, task  # noqa: F401 (fixture)
from test_model_proxy_budget import _GENEROUS_ITEM, _admitting_job, _item

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _persist_controls(ctx, job_id, controls):
    mission_id = uuid4()
    ctx.projects.link_mission(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, mission_id, "expiry", "running"
    )
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        append_step(
            ctx.jobs, cur, workspace_id=ctx.workspace_id, job_id=job_id,
            step={
                "mission_id": str(mission_id), "step_index": 0,
                "kind": "submitted", "rule_id": "submit",
                "idempotency_key": str(uuid4()), "request": {"controls": controls},
            },
        )
    return str(mission_id)


@pytest.fixture
def hard_task(proxy):
    ctx = proxy
    request = task(ctx)
    request["mission_id"] = _persist_controls(ctx, request["job_id"], {"wall_clock_seconds": 7200})
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "SELECT created_at, lease_expires_at FROM omp_jobs.jobs"
            " WHERE workspace_id=%s AND job_id=%s",
            (ctx.workspace_id, request["job_id"]),
        )
        row = cur.fetchone()
    deadline = row["created_at"] + timedelta(seconds=7200)
    assert deadline > row["lease_expires_at"]
    request["expires_at"] = deadline.isoformat()
    return ctx, request, deadline


def _register(ctx, request):
    return ctx.client.post(
        "/tasks", json=request, headers={"Authorization": "Bearer control-secret"}
    )


def _assert_refused(ctx, request, code):
    response = _register(ctx, request)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == code
    assert ctx.calls == []
    assert spend(ctx) == (0, Decimal(0))


def test_controls_only_exact_deadline_admits_original_lease(hard_task):
    """A 7200s grant outlives its 300s lease and its token can call provider."""
    ctx, request, _ = hard_task
    response = _register(ctx, request)
    assert response.status_code == 200, response.text
    assert call(ctx, response.json()).status_code == 200
    count, usd = spend(ctx)
    assert count == len(ctx.calls) == 1 and usd > 0


def test_renewal_keeps_grant_authority_and_fixed_expiry(hard_task, monkeypatch):
    """Renewal permits original token but cannot move its expiry instant."""
    ctx, request, deadline = hard_task
    response = _register(ctx, request)
    assert response.status_code == 200, response.text
    grant = response.json()
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "SELECT lease_expires_at FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
            (ctx.workspace_id, request["job_id"]),
        )
        initial_lease = cur.fetchone()["lease_expires_at"]
    renewed = renew_lease(
        ctx.jobs, operation_id=str(uuid4()), workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id, job_id=request["job_id"],
        worker_id=request["worker_id"], fence=request["fence"],
    )
    assert renewed["status"] == "applied"
    assert call(ctx, grant).status_code == 200
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "SELECT u.job_id_derived, j.worker_id, j.fence, u.work_id, j.lease_expires_at, j.created_at"
            " FROM omp_jobs.usage_events u JOIN omp_jobs.jobs j"
            " ON j.workspace_id=u.workspace_id AND j.job_id=u.job_id_derived"
            " WHERE u.workspace_id=%s", (ctx.workspace_id,),
        )
        usage = cur.fetchone()
    assert (usage["job_id_derived"], usage["worker_id"], usage["fence"], str(usage["work_id"])) == (
        request["job_id"], request["worker_id"], request["fence"], request["work_id"],
    )
    assert usage["lease_expires_at"] == initial_lease + timedelta(seconds=300)
    assert usage["created_at"] + timedelta(seconds=7200) == deadline
    before = spend(ctx)

    class AtDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline

    monkeypatch.setattr(model_proxy, "datetime", AtDeadline)
    assert call(ctx, grant).status_code == 403
    assert len(ctx.calls) == 1 and spend(ctx) == before


def test_changed_fence_revokes_original_authority(hard_task):
    """A replacement fence cannot use previously registered model token."""
    ctx, request, _ = hard_task
    response = _register(ctx, request)
    assert response.status_code == 200, response.text
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.jobs SET fence=fence+1 WHERE workspace_id=%s AND job_id=%s",
            (ctx.workspace_id, request["job_id"]),
        )
    result = call(ctx, response.json())
    assert result.status_code == 403
    assert ctx.calls == [] and spend(ctx) == (0, Decimal(0))


@pytest.mark.parametrize("excess", [timedelta(microseconds=1), timedelta(seconds=1)])
def test_expiry_above_exact_deadline_refused(hard_task, excess):
    """Neither microsecond nor second overflow can round into allowed expiry."""
    ctx, request, deadline = hard_task
    _assert_refused(ctx, {**request, "expires_at": (deadline + excess).isoformat()}, "expiry_refused")


def test_elapsed_task_deadline_refuses_future_grant(hard_task):
    """Live lease and future requested expiry cannot revive elapsed task."""
    ctx, request, _ = hard_task
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.jobs SET created_at=clock_timestamp()-interval '7201 seconds'"
            " WHERE workspace_id=%s AND job_id=%s", (ctx.workspace_id, request["job_id"]),
        )
    _assert_refused(ctx, request, "expiry_refused")


def test_item_budget_tightens_controls_from_oldest_job(proxy):
    """60s published budget bounds 7200s controls from oldest tree creation."""
    ctx = proxy
    work_id = _item(ctx, budget={**_GENEROUS_ITEM, "wall_clock_seconds": 60})
    oldest_job = _admitting_job(ctx, work_id=work_id, lease_seconds=300)
    request = task(ctx, work_id=str(work_id))
    request["job_id"] = _admitting_job(ctx, work_id=work_id, lease_seconds=300)
    request["mission_id"] = _persist_controls(ctx, request["job_id"], {"wall_clock_seconds": 7200})
    # Both jobs belong to budgeted item; older job must bound newer grant.
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.jobs SET created_at=clock_timestamp()-interval '10 seconds'"
            " WHERE workspace_id=%s AND job_id=%s RETURNING created_at",
            (ctx.workspace_id, oldest_job),
        )
        deadline = cur.fetchone()["created_at"] + timedelta(seconds=60)
    request["expires_at"] = deadline.isoformat()
    _assert_refused(ctx, {**request, "expires_at": (deadline + timedelta(microseconds=1)).isoformat()}, "expiry_refused")
    response = _register(ctx, request)
    assert response.status_code == 200, response.text
    assert call(ctx, response.json()).status_code == 200


@pytest.mark.parametrize("mission", ["empty_controls", "absent_mission"])
def test_unpersisted_controls_cannot_widen_expiry(proxy, mission):
    """Caller cannot claim 7200s when no durable controls grant that limit."""
    ctx = proxy
    request = task(ctx, expires_at=(datetime.now(UTC) + timedelta(seconds=7200)).isoformat())
    if mission == "empty_controls":
        request["mission_id"] = _persist_controls(ctx, request["job_id"], {})
    _assert_refused(ctx, request, "expiry_refused")


@pytest.mark.parametrize("field,code", [
    ("worker_id", "job_fence_stale"), ("fence", "job_fence_stale"),
    ("work_id", "job_work_mismatch"), ("workspace_id", "workspace_refused"),
])
def test_registration_identity_mismatch_never_spends(hard_task, field, code):
    """Identity overrides fail before provider invocation or spend recording."""
    ctx, request, _ = hard_task
    value = 2 if field == "fence" else "other-worker" if field == "worker_id" else str(uuid4())
    _assert_refused(ctx, {**request, field: value}, code)
