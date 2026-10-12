"""Spend and usage can share one caller-owned PostgreSQL transaction.

Also proves model-call admission: a stopped workspace, a stale lease, a wrong
worker/fence, or a job on the wrong work item is refused before any send; each
cap (project, item USD/tokens/wall-clock, task) refuses a bound that would
exceed it; an admission holds the project and item budget locks so a second
connection cannot pass them; and settling records usage and spend once while an
abort writes no ledgers and frees the locks.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work import model_proxy_budget as budget_module
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.budget import item_spend, root_work_id
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy_budget import (
    ModelProxyBudget,
    ModelProxyBudgetError,
    lock_captured_identity,
)
from omp_work.model_proxy_upstream import GEMINI, ModelRoute, Prepared
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from psycopg import sql
from psycopg.errors import CheckViolation, LockNotAvailable
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_agent_stop_store import _envelope
from test_item_budget_alerts import _seed_budget
from test_workflow_service import OWNER, _create, _grant

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
NOW = datetime(2026, 10, 11, tzinfo=UTC)

_GENEROUS_ITEM = {
    "usd": "1000.00",
    "tokens": 1_000_000,
    "wall_clock_seconds": 86_400,
    "max_subagents": 0,
}


@pytest.fixture
def budget_context(service):
    apply_jobs_migrations(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "proxy budget target")
    projects = PostgresWorkStore(service.config)
    project_id = projects.ensure_project(
        workspace_id, OWNER, "proxy-budget", "Proxy budget", "surface"
    )
    projects.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal(10), Decimal(5)),
        ChangeAuthority(
            requested_by_kind="owner", decision_id=uuid4(), answered_by_kind="owner"
        ),
    )
    return SimpleNamespace(
        service=service,
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=UUID(item["work_id"]),
        item=item,
        project_id=project_id,
        projects=projects,
        jobs=NativeJobStore(service.config),
    )


def _usage(ctx, cur, operation_id, request_id, price="1"):
    return record_usage(
        ctx.jobs,
        cur=cur,
        operation_id=operation_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        work_id=ctx.work_id,
        request_id=request_id,
        role="coder",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        cache_tokens=0,
        measurement="measured",
        price_usd=price,
    )


def _spend(ctx, cur, amount, record_id):
    decision = ctx.projects.authorize_spend_cursor(
        cur, ctx.workspace_id, ctx.project_id, None, amount, NOW
    )
    ctx.projects.append_spend_cursor(
        cur, ctx.workspace_id, ctx.project_id, None, amount, decision, record_id
    )
    return decision


def _counts(ctx, cur):
    counts = []
    for table in (
        "omp_work.spend_records",
        "omp_jobs.usage_events",
        "omp_jobs.outbox",
        "omp_jobs.operations",
    ):
        cur.execute(
            f"SELECT count(*) AS n FROM {table} WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        counts.append(cur.fetchone()["n"])
    return tuple(counts)


def test_caller_rollback_removes_both_ledgers_outbox_and_operation(budget_context):
    ctx = budget_context
    operation_id, request_id, record_id = str(uuid4()), str(uuid4()), uuid4()
    with (
        pytest.raises(RuntimeError, match="abort caller"),
        ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur,
    ):
        _spend(ctx, cur, Decimal(1), record_id)
        assert _usage(ctx, cur, operation_id, request_id)["status"] == "applied"
        assert _counts(ctx, cur) == (1, 1, 1, 1)
        raise RuntimeError("abort caller")
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 0, 0, 0)
        # Rolled-back identities remain usable, including the spend record id.
        _spend(ctx, cur, Decimal(1), record_id)
        assert _usage(ctx, cur, operation_id, request_id)["status"] == "applied"
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (1, 1, 1, 1)


def test_commit_writes_both_ledgers_and_outbox_once(budget_context):
    ctx = budget_context
    record_id = uuid4()
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        decision = _spend(ctx, cur, Decimal(5), record_id)
        assert decision.allowed and decision.tier == "tier1"
        result = _usage(ctx, cur, str(uuid4()), str(uuid4()), "5")
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (1, 1, 1, 1)
        cur.execute(
            "SELECT record_id, amount_usd, tier FROM omp_work.spend_records WHERE record_id=%s",
            (record_id,),
        )
        assert cur.fetchone() == {
            "record_id": record_id,
            "amount_usd": Decimal(5),
            "tier": "tier1",
        }
        cur.execute(
            "SELECT price_usd FROM omp_jobs.usage_events WHERE usage_id=%s",
            (result["usage_id"],),
        )
        assert cur.fetchone()["price_usd"] == Decimal(5)


def test_cursor_usage_replay_and_conflict_keep_original_rows(budget_context):
    ctx = budget_context
    operation_id, request_id = str(uuid4()), str(uuid4())
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        first = _usage(ctx, cur, operation_id, request_id)
        assert _usage(ctx, cur, operation_id, request_id)["status"] == "replayed"
        second = _usage(ctx, cur, str(uuid4()), request_id)
        assert second["status"] == "replayed"
        assert second["usage_id"] == first["usage_id"]
    for conflicting_operation in (operation_id, str(uuid4())):
        with (
            pytest.raises(JobError) as exc,
            ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur,
        ):
            _usage(ctx, cur, conflicting_operation, request_id, "2")
        assert exc.value.code == "idempotency_conflict"
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 1, 1, 2)
        cur.execute(
            "SELECT step_index, price_usd FROM omp_jobs.usage_events WHERE usage_id=%s",
            (first["usage_id"],),
        )
        assert cur.fetchone() == {"step_index": 1, "price_usd": Decimal(1)}


@pytest.mark.parametrize(
    "amount,code", [("6", "standing_policy_required"), ("11", "ceiling_exceeded")]
)
def test_cursor_and_legacy_refusals_leave_no_spend(budget_context, amount, code):
    ctx = budget_context
    for cursor_path in (False, True):
        with pytest.raises(ProjectAuthorityRefused) as exc:
            if cursor_path:
                with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
                    _spend(ctx, cur, Decimal(amount), uuid4())
            else:
                ctx.projects.record_spend(
                    ctx.workspace_id,
                    ctx.actor_id,
                    ctx.project_id,
                    None,
                    Decimal(amount),
                    NOW,
                )
        assert exc.value.code == code
    ctx.projects.record_spend(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, None, Decimal(1), NOW
    )
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (1, 0, 0, 0)


def test_legacy_zero_fails_check_but_proxy_zero_records_usage(budget_context):
    ctx = budget_context
    with pytest.raises(CheckViolation):
        ctx.projects.record_spend(
            ctx.workspace_id, ctx.actor_id, ctx.project_id, None, Decimal(0), NOW
        )
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 0, 0, 0)
        assert _usage(ctx, cur, str(uuid4()), str(uuid4()), "0")["status"] == "applied"
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 1, 1, 1)


def test_cursor_usage_budget_alerts_follow_caller_rollback(budget_context):
    ctx = budget_context
    _seed_budget(
        ctx,
        work_id=ctx.work_id,
        revision_id=UUID(ctx.item["revision_id"]),
        budget={
            "usd": "2",
            "tokens": 1000,
            "wall_clock_seconds": 3600,
            "max_subagents": 10,
        },
    )
    operation_id, request_id = str(uuid4()), str(uuid4())
    with (
        pytest.raises(RuntimeError, match="abort caller"),
        ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur,
    ):
        _usage(ctx, cur, operation_id, request_id)
        assert _counts(ctx, cur) == (0, 1, 2, 1)
        raise RuntimeError("abort caller")
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 0, 0, 0)
        _usage(ctx, cur, operation_id, request_id)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 1, 2, 1)
        cur.execute(
            "SELECT payload FROM omp_jobs.outbox WHERE workspace_id=%s AND kind='budget_alert'",
            (ctx.workspace_id,),
        )
        payload = cur.fetchone()["payload"]
        assert payload["dimension"] == "usd"
        assert payload["threshold_percent"] == 50


def test_cursor_and_legacy_spend_obey_workspace_stop(budget_context):
    ctx = budget_context
    ctx.projects.execute(
        _envelope(ctx.workspace_id, "engage_stop", "stop model spending"),
        actor_id=ctx.actor_id,
        actor_kind="owner",
        required_scope="work.stop",
    )
    for cursor_path in (False, True):
        with pytest.raises(ProjectAuthorityRefused) as exc:
            if cursor_path:
                with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
                    _spend(ctx, cur, Decimal(1), uuid4())
            else:
                ctx.projects.record_spend(
                    ctx.workspace_id,
                    ctx.actor_id,
                    ctx.project_id,
                    None,
                    Decimal(1),
                    NOW,
                )
        assert exc.value.code == "agent_stop_engaged"
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 0, 0, 0)


def test_cursor_usage_rejects_invalid_price_before_writing(budget_context):
    ctx = budget_context
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        with pytest.raises(JobError) as exc:
            _usage(ctx, cur, str(uuid4()), str(uuid4()), "-1")
        assert exc.value.code == "invalid_request"
        assert _counts(ctx, cur) == (0, 0, 0, 0)


# --- Model-call admission -------------------------------------------------

# Every ledger an admission may write, as a table the tests can count.
_ADMISSION_TABLES = (
    "omp_work.spend_records",
    "omp_jobs.usage_events",
    "omp_jobs.outbox",
    "omp_jobs.operations",
)


def _prepared(max_usd: str, max_input_tokens: int = 100, max_output_tokens: int = 50) -> Prepared:
    route = ModelRoute(
        api=GEMINI,
        model="test-model",
        origin="https://generativelanguage.googleapis.com",
        key_file="gemini-key",
        context_tokens=200_000,
        max_output_tokens=1000,
        input_usd=Decimal("1"),
        output_usd=Decimal("1"),
        cache_usd=Decimal("1"),
        price_version="p1",
    )
    return Prepared(
        route=route,
        url="https://example.test/v1:predict",
        headers=(("Content-Type", "application/json"),),
        body=b"{}",
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_usd=Decimal(max_usd),
    )


def _admitting_job(
    ctx,
    *,
    work_id: UUID | None = None,
    worker_id: str = "worker-1",
    fence: int = 1,
    lease_seconds: int = 60,
    expired: bool = False,
    status: str = "admitted",
    parent_job_id: str | None = None,
) -> str:
    job_id = f"job-{uuid4()}"
    enqueue_job(
        ctx.jobs,
        operation_id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        job_id=job_id,
        work_id=work_id or ctx.work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=lease_seconds,
        parent_job_id=parent_job_id,
    )
    lease = (
        "clock_timestamp() - interval '10 seconds'"
        if expired
        else f"clock_timestamp() + ({lease_seconds} * interval '1 second')"
    )
    with psycopg.connect(
        **ctx.service.config.connection_kwargs("postgres")
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status=%s, worker_id=%s, fence=%s,"
            f" lease_expires_at={lease}, updated_at=clock_timestamp()"  # nosec B608 - literal interval expression
            " WHERE workspace_id=%s AND job_id=%s",
            (status, worker_id, fence, ctx.workspace_id, job_id),
        )
    return job_id


def _engage_stop(ctx) -> None:
    ctx.projects.execute(
        _envelope(ctx.workspace_id, "engage_stop", "stop model spending"),
        actor_id=ctx.actor_id,
        actor_kind="owner",
        required_scope="work.stop",
    )


def _admission_counts(ctx) -> tuple[int, ...]:
    counts = []
    with psycopg.connect(
        **ctx.service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true),"
            " set_config('omp.actor_id', %s, true)",
            (str(ctx.workspace_id), str(ctx.actor_id)),
        )
        for table in _ADMISSION_TABLES:
            row = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE workspace_id=%s",  # nosec B608 - fixed table list
                (ctx.workspace_id,),
            ).fetchone()
            counts.append(row["n"])
    return tuple(counts)


def _admit(ctx, prepared, **kw):
    return ctx.budget.admit(
        ctx.workspace_id,
        ctx.actor_id,
        kw.pop("project_id", ctx.project_id),
        kw.pop("mission_id", None),
        kw.pop("work_id", ctx.work_id),
        kw.pop("job_id", None),
        kw.pop("worker_id", None),
        kw.pop("fence", None),
        kw.pop("request_id", f"req-{uuid4()}"),
        kw.pop("task_ceiling_usd", None),
        prepared,
        **kw,
    )


def _budget_ctx(service, **budget_kwargs) -> SimpleNamespace:
    """A workspace with a generous project budget and one base (unbudgeted) item."""
    apply_jobs_migrations(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    # _create seeds the workspace row that the project foreign key needs.
    item = _create(service, workspace_id, "admission base item")
    projects = PostgresWorkStore(service.config)
    project_id = projects.ensure_project(
        workspace_id, OWNER, f"admit-{uuid4()}", "Admission", "surface"
    )
    projects.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal(budget_kwargs.get("ceiling", "100000")),
                    Decimal(budget_kwargs.get("threshold", "100000"))),
        ChangeAuthority("owner", uuid4(), "owner"),
    )
    return SimpleNamespace(
        service=service,
        workspace_id=workspace_id,
        actor_id=OWNER,
        project_id=project_id,
        projects=projects,
        jobs=NativeJobStore(service.config),
        budget=ModelProxyBudget(service.config),
        item=item,
        work_id=UUID(item["work_id"]),
    )


@pytest.fixture
def admission_context(service):
    return _budget_ctx(service)


def _item(ctx, title="admitted item", budget=None, **extra) -> UUID:
    item = _create(ctx.service, ctx.workspace_id, title, **extra)
    work_id = UUID(item["work_id"])
    if budget is not None:
        _seed_budget(
            ctx,
            work_id=work_id,
            revision_id=UUID(item["revision_id"]),
            budget=budget,
        )
    return work_id


def test_admission_refuses_stop_before_upstream(admission_context):
    """An engaged workspace refuses admission and writes no ledger."""
    ctx = admission_context
    _engage_stop(ctx)
    sent = False
    with pytest.raises(ModelProxyBudgetError) as exc:
        admission = _admit(ctx, _prepared("1"))
        sent = True
        admission.abort()
    assert exc.value.code == "agent_stop_engaged"
    assert not sent
    assert _admission_counts(ctx) == (0, 0, 0, 0)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _counts(ctx, cur) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "override,code",
    [
        ({"expired": True}, "job_fence_stale"),
        ({"status": "sealed"}, "job_fence_stale"),
        ({"status": "backlog"}, "job_fence_stale"),
        ({"fence": 99}, "job_fence_stale"),
        ({"worker_id": "other-worker"}, "job_fence_stale"),
    ],
)
def test_admission_refuses_bad_lease_or_identity(admission_context, override, code):
    """An expired lease, a non-admitted job, or a wrong worker/fence is refused.

    The job row is altered to the override; the caller always presents the
    baseline ``worker-1``/fence 1, so a fence or worker override is a mismatch.
    """
    ctx = admission_context
    job = _admitting_job(ctx, **override)
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("1"), job_id=job, worker_id="worker-1", fence=1)
    assert exc.value.code == code
    assert _admission_counts(ctx) == before


def test_admission_refuses_unknown_job_and_missing_lease_identity(admission_context):
    """An unknown job id, and a job present without worker/fence, are refused."""
    ctx = admission_context
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc_missing:
        _admit(ctx, _prepared("1"), job_id=f"job-{uuid4()}", worker_id="w", fence=1)
    assert exc_missing.value.code == "job_fence_stale"

    job = _admitting_job(ctx)
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc_invalid:
        _admit(ctx, _prepared("1"), job_id=job)
    assert exc_invalid.value.code == "invalid_request"

    assert _admission_counts(ctx) == before


def test_admission_refuses_job_on_other_work_item(admission_context):
    """A live job whose work item is not the caller's is refused."""
    ctx = admission_context
    other = _item(ctx, "other work")
    job = _admitting_job(ctx)  # on ctx.work_id
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(
            ctx,
            _prepared("1"),
            work_id=other,
            job_id=job,
            worker_id="worker-1",
            fence=1,
        )
    assert exc.value.code == "invalid_request"
    assert _admission_counts(ctx) == before


def test_admission_refuses_project_cap_before_upstream(service):
    """A bound over the project ceiling is refused and writes no ledger."""
    ctx = _budget_ctx(service, ceiling="1000", threshold="1000")
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("1001"))
    assert exc.value.code == "ceiling_exceeded"
    assert _admission_counts(ctx) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "bound,budget",
    [
        ("1001", {**_GENEROUS_ITEM, "usd": "1000.00"}),
        ("10", {**_GENEROUS_ITEM, "usd": "1000.00", "tokens": 100}),
        ("10", {**_GENEROUS_ITEM, "usd": "1000.00", "wall_clock_seconds": 1}),
    ],
)
def test_admission_refuses_each_item_cap(admission_context, bound, budget):
    """A USD, token, or wall-clock item cap refuses a bound that exceeds it."""
    ctx = admission_context
    work_id = _item(ctx, "capped item", budget)
    # One admitted job puts the item's job tree on the clock, so the wall-clock
    # spend is one second older once we wait past a one-second cap.
    _admitting_job(ctx, work_id=work_id)
    if budget["wall_clock_seconds"] == 1:
        time.sleep(1.1)
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared(bound), work_id=work_id)
    assert exc.value.code == "item_budget_exceeded"
    assert _admission_counts(ctx) == before


def test_admission_refuses_task_ceiling(admission_context):
    """A bound over the work item's task USD ceiling is refused."""
    ctx = admission_context
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("101"), task_ceiling_usd="100")
    assert exc.value.code == "task_ceiling_exceeded"
    assert _admission_counts(ctx) == before


def test_admission_holds_project_lock_against_second_connection(admission_context):
    """A second connection cannot authorize a spend while an admission holds the lock."""
    ctx = admission_context
    admission = _admit(ctx, _prepared("6"))
    blocked: list[str] = []

    def second() -> None:
        try:
            with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
                cur.execute("SET LOCAL lock_timeout = '5s'")
                _spend(ctx, cur, Decimal(6), uuid4())
            blocked.append("passed")
        except Exception as exc:  # noqa: BLE001 - the observed outcome is the assertion
            blocked.append(type(exc).__name__)

    thread = threading.Thread(target=second)
    try:
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive(), "second connection passed a held budget lock"
    finally:
        admission.abort()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert blocked == ["passed"]


def test_admission_holds_item_lock_against_second_connection(admission_context):
    """A second connection cannot pass the held root-item advisory lock."""
    ctx = admission_context
    work_id = _item(ctx, "locked item", _GENEROUS_ITEM)
    parent = _admitting_job(ctx, work_id=work_id)
    job = _admitting_job(ctx, work_id=work_id, worker_id="w2", fence=2, parent_job_id=parent)
    admission = _admit(
        ctx, _prepared("6"), work_id=work_id, job_id=job, worker_id="w2", fence=2
    )

    key = f"omp_work.item_budget:{ctx.workspace_id}:{work_id}"
    kwargs = ctx.service.config.connection_kwargs("postgres")
    admission_pid = int(
        admission.cursor.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    )
    waiter_conn = psycopg.connect(**kwargs, autocommit=True, row_factory=dict_row)
    waiter_conn.execute("SET lock_timeout = '5s'")
    monitor = psycopg.connect(**kwargs, autocommit=True, row_factory=dict_row)
    thread_result: list[str] = []

    def waiter() -> None:
        try:
            waiter_conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,)
            )
            thread_result.append("acquired")
        except Exception as exc:  # noqa: BLE001
            thread_result.append(type(exc).__name__)

    thread = threading.Thread(target=waiter)
    try:
        thread.start()
        deadline = time.time() + 5
        blocked = False
        while time.time() < deadline:
            row = monitor.execute(
                "SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)) LIMIT 1",
                (admission_pid,),
            ).fetchone()
            if row is not None:
                blocked = True
                break
            time.sleep(0.02)
        assert blocked, "second connection acquired a held item advisory lock"
        admission.abort()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert thread_result == ["acquired"]
    finally:
        admission.abort()
        thread.join(timeout=10)
        waiter_conn.execute("SELECT pg_advisory_unlock_all()")
        waiter_conn.close()
        monitor.close()


def test_admission_settle_writes_usage_and_spend_once(admission_context):
    """Settling records one usage event and one project spend, then frees the locks."""
    ctx = admission_context
    admission = _admit(ctx, _prepared("6"), request_id="req-settle", operation_id="op-settle")
    result = admission.settle(
        actual_usd=Decimal("6"), input_tokens=10, output_tokens=5, cache_tokens=0
    )
    assert result["status"] == "settled"
    assert result["usage"]["status"] == "applied"
    assert _admission_counts(ctx) == (1, 1, 1, 2)

    # The locks are released: a second connection can now authorize a spend.
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        decision = _spend(ctx, cur, Decimal(1), uuid4())
        assert decision.allowed


@pytest.mark.parametrize("settlement", ["actual", "bound"])
def test_settlement_usage_sees_uncommitted_spend_on_same_cursor(
    admission_context, monkeypatch, settlement,
):
    """Usage sees its spend before recording; replay commits no duplicate ledgers or receipt."""
    ctx = admission_context
    prepared = _prepared("6")
    admission = _admit(ctx, prepared, request_id="writer-order")
    recorded = []

    def observe_spend(*args, **kwargs):
        cur = kwargs["cur"]
        assert cur is admission.cursor
        cur.execute(
            "SELECT amount_usd FROM omp_work.spend_records WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        assert cur.fetchall() == [{"amount_usd": Decimal(6)}]
        # Another connection cannot see any part of the uncommitted settlement.
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        recorded.append(record_usage(*args, **kwargs))
        return recorded[-1]

    def settle(call):
        if settlement == "bound":
            with pytest.raises(ModelProxyBudgetError) as exc:
                call.settle_unmeasurable_failure("provider disconnected")
            assert exc.value.code == "unmeasurable_failure"
        else:
            call.settle(actual_usd="6", input_tokens=10, output_tokens=5, cache_tokens=0)

    try:
        monkeypatch.setattr(budget_module, "record_usage", observe_spend)
        settle(admission)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        with _admit(ctx, prepared, request_id="writer-order") as replay:
            assert replay.settled
            settle(replay)
        assert len(recorded) == 1
        assert _admission_counts(ctx) == (1, 1, 1, 2)
    finally:
        monkeypatch.setattr(budget_module, "record_usage", record_usage)
        if not admission.settled:
            settle(admission)


def test_admission_abort_writes_no_ledgers_and_frees_locks(admission_context):
    """Aborting before a send rolls back, writes nothing, and releases the locks."""
    ctx = admission_context
    admission = _admit(ctx, _prepared("6"))
    admission.abort()
    assert _admission_counts(ctx) == (0, 0, 0, 0)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert _spend(ctx, cur, Decimal(6), uuid4()).allowed


def _mission(ctx, ceiling="10"):
    mission_id = uuid4()
    ctx.projects.link_mission(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, mission_id, "Model work", "draft"
    )
    ctx.projects.set_spend_budget(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, mission_id,
        SpendBudget("mission", Decimal(ceiling), Decimal(ceiling)),
        ChangeAuthority("owner", uuid4(), "owner"),
    )
    return mission_id


def test_admission_refuses_mission_bound(admission_context):
    ctx = admission_context
    mission = _mission(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("11"), mission_id=mission)
    assert exc.value.code == "ceiling_exceeded"
    assert _admission_counts(ctx) == (0, 0, 0, 0)


@pytest.mark.parametrize("cap", ["project", "mission", "task", "item_usd", "item_tokens"])
def test_exhausted_cap_refuses_zero_bound(service, cap):
    """Zero-cost calls cannot bypass an already exhausted cap."""
    ctx = _budget_ctx(service, ceiling="2" if cap == "mission" else "1",
                      threshold="2" if cap == "mission" else "1")
    kw = {}
    if cap == "mission":
        kw["mission_id"] = _mission(ctx, "1")
    if cap.startswith("item_"):
        kw["work_id"] = _item(ctx, budget={
            **_GENEROUS_ITEM,
            **({"usd": "1"} if cap == "item_usd" else {"tokens": 1}),
        })
    if cap == "task":
        kw["task_ceiling_usd"] = "1"
    # Allow exactly reaching each ceiling, then refuse even a zero bound.
    with _admit(ctx, _prepared("1", 1, 0), **kw) as admission:
        admission.settle(actual_usd="1", input_tokens=1)
    if cap not in ("project", "mission"):
        kw["project_id"] = None  # Isolate task/item exhaustion from project cap.
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("0", 0, 0), **kw)
    expected = ("item_budget_exceeded" if cap.startswith("item_") else
                "task_ceiling_exceeded" if cap == "task" else "ceiling_exceeded")
    assert exc.value.code == expected
    assert _admission_counts(ctx) == before


def test_admission_identity_and_bounds_cannot_redirect_settlement(admission_context):
    ctx = admission_context
    with _admit(ctx, _prepared("1"), request_id="immutable") as admission:
        for name, value in {
            "prepared": _prepared("100"), "decision": None,
            "workspace_id": uuid4(), "actor_id": uuid4(), "work_id": uuid4(),
            "root_work_id": uuid4(), "project_id": uuid4(), "mission_id": uuid4(), "job_id": "other",
            "worker_id": "other", "fence": 99, "request_id": "other",
            "operation_id": "other", "role": "other",
        }.items():
            with pytest.raises(FrozenInstanceError):
                setattr(admission, name, value)
        with pytest.raises(ModelProxyBudgetError) as exc:
            admission.settle(actual_usd="2")
        assert exc.value.code == "usage_exceeds_admission"
        assert admission.closed and admission.settled
        bound = admission._admitted_bound_payload()
        admission.settle(**bound)
        with pytest.raises(ModelProxyBudgetError) as retry:
            admission.settle(actual_usd="1")
        assert retry.value.code == "idempotency_conflict"
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "SELECT work_id, request_id, price_usd FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        assert cur.fetchone() == {
            "work_id": ctx.work_id, "request_id": f"model_call:request:{admission._identity}", "price_usd": Decimal(1)
        }


def _wait_for_blocker(monitor, blocker_pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if monitor.execute(
            "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
            (blocker_pid,),
        ).fetchone():
            return
        time.sleep(0.02)
    pytest.fail("admission never waited on held lock")


@pytest.mark.parametrize("change", ["stop", "lease", "identity", "root"])
def test_final_job_lock_wait_rechecks_stop_lease_identity(admission_context, monkeypatch, change):
    """State changes during final row-lock wait refuse before caller can send."""
    ctx = admission_context
    job = _admitting_job(ctx)
    other = _item(ctx, "changed job identity") if change in ("identity", "root") else None
    parent = _admitting_job(ctx, work_id=other) if change == "root" else None
    transaction = ctx.budget._jobs.transaction

    @contextmanager
    def bounded_transaction(workspace_id, actor_id):
        with transaction(workspace_id, actor_id) as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            yield cur

    monkeypatch.setattr(ctx.budget._jobs, "transaction", bounded_transaction)
    before = _admission_counts(ctx)
    outcomes = []

    def waiter():
        try:
            with _admit(ctx, _prepared("1"), job_id=job, worker_id="worker-1", fence=1):
                outcomes.append("sent")
        except ModelProxyBudgetError as exc:
            outcomes.append(exc.code)
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    kwargs = ctx.service.config.connection_kwargs("postgres")
    with psycopg.connect(**kwargs) as blocker, psycopg.connect(**kwargs, autocommit=True) as monitor:
        blocker.execute(
            "SELECT job_id FROM omp_jobs.jobs WHERE job_id=%s FOR UPDATE", (job,)
        )
        blocker_pid = blocker.info.backend_pid
        # Commit short lease first would release row lock; use a separate update
        # before starting the blocker below for the lease-expiry case instead.
        if change == "lease":
            blocker.execute(
                "UPDATE omp_jobs.jobs SET lease_expires_at=clock_timestamp()+interval '1 second' WHERE job_id=%s",
                (job,),
            )
            blocker.commit()
            blocker.execute("SELECT job_id FROM omp_jobs.jobs WHERE job_id=%s FOR UPDATE", (job,))
        thread = threading.Thread(target=waiter)
        try:
            thread.start()
            _wait_for_blocker(monitor, blocker_pid)
            if change == "stop":
                _engage_stop(ctx)
            elif change == "lease":
                time.sleep(1.1)
            elif change == "identity":
                blocker.execute("UPDATE omp_jobs.jobs SET work_id=%s WHERE job_id=%s", (other, job))
            else:
                blocker.execute("UPDATE omp_jobs.jobs SET parent_job_id=%s WHERE job_id=%s", (parent, job))
        finally:
            blocker.commit()
            thread.join(timeout=7)
        assert not thread.is_alive()
    assert outcomes == [{"stop": "agent_stop_engaged", "lease": "job_fence_stale", "identity": "invalid_request", "root": "invalid_request"}[change]]
    assert _admission_counts(ctx) == before


def test_admission_refuses_imported_job(admission_context):
    ctx = admission_context
    job = _admitting_job(ctx)
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET source='flood_import', kind=NULL WHERE job_id=%s",
            (job,),
        )
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("1"), job_id=job, worker_id="worker-1", fence=1)
    assert exc.value.code == "job_fence_stale"
    assert _admission_counts(ctx) == before


def test_budget_instances_share_gate_until_abort(admission_context):
    """Separate budget instances serialize even without shared database keys."""
    ctx = admission_context
    other_work = _item(ctx, "separate task")
    other_budget = ModelProxyBudget(ctx.service.config)
    started, finished = threading.Event(), threading.Event()
    outcomes = []

    def waiter():
        started.set()
        try:
            with other_budget.admit(
                ctx.workspace_id, ctx.actor_id, None, None, other_work,
                None, None, None, "other-instance", None, _prepared("1"),
            ):
                outcomes.append("admitted")
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        finally:
            finished.set()

    admission = _admit(ctx, _prepared("1"), project_id=None)
    thread = threading.Thread(target=waiter)
    try:
        thread.start()
        assert started.wait(2)
        assert not finished.wait(0.2), "separate instance bypassed serialization gate"
    finally:
        admission.abort()
        thread.join(timeout=7)
    assert not thread.is_alive()
    assert outcomes == ["admitted"]
    assert _admission_counts(ctx) == (0, 0, 0, 0)


def test_child_job_uses_root_item_budget(admission_context):
    """Child on an unbudgeted work item still consumes its root item's cap."""
    ctx = admission_context
    root = _item(ctx, "root budget", {**_GENEROUS_ITEM, "usd": "1"})
    child = _item(ctx, "unbudgeted child")
    parent_job = _admitting_job(ctx, work_id=root)
    job = _admitting_job(ctx, work_id=child, parent_job_id=parent_job)
    before = _admission_counts(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("2"), work_id=child, job_id=job, worker_id="worker-1", fence=1)
    assert exc.value.code == "item_budget_exceeded"
    assert _admission_counts(ctx) == before
    with _admit(ctx, _prepared("1"), work_id=child, job_id=job, worker_id="worker-1", fence=1) as admission:
        assert admission.root_work_id == root
        admission.settle(actual_usd="1")
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert item_spend(cur, ctx.workspace_id, root).usd == Decimal(1)


def test_settlement_receipt_replays_after_tree_cancellation_and_stop(admission_context, monkeypatch):
    """Completed calls bypass authorization/send after cancellation and stop, without double charge."""
    ctx = admission_context
    root = _item(ctx, "capped root", {**_GENEROUS_ITEM, "usd": "1"})
    parent = _admitting_job(ctx, work_id=root)
    child = _item(ctx, "child")
    job = _admitting_job(ctx, work_id=child, parent_job_id=parent)
    kw = dict(work_id=child, job_id=job, worker_id="worker-1", fence=1, request_id="receipt-replay")
    payload = dict(actual_usd="1.00", input_tokens=10, output_tokens=5, cache_tokens=0)
    sends = []

    def call():
        with _admit(ctx, _prepared("1"), **kw) as admission:
            if not admission.settled:
                sends.append("upstream")
            return admission.settle(**payload)

    first = call()
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute("SELECT status, cancel_reason FROM omp_jobs.jobs WHERE job_id=ANY(%s)", ([parent, job],))
        assert cur.fetchall() == [{"status": "cancelled", "cancel_reason": "budget_exceeded"}] * 2
        cur.execute("SELECT result FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt'", (ctx.workspace_id,))
        receipt = cur.fetchone()["result"]
        assert receipt["settlement"]["actual_usd"] == "1"
        assert receipt["fingerprint"]["worker_id"] == "worker-1"
        assert receipt["fingerprint"]["prepared"]["max_usd"] == "1"
    before = _admission_counts(ctx)
    assert before[:2] == (1, 1)
    _engage_stop(ctx)

    def forbidden(*args, **kwargs):
        pytest.fail("receipt replay tried fresh authorization")

    monkeypatch.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden)
    payload["actual_usd"] = "1"
    assert call() == first
    assert sends == ["upstream"]
    assert _admission_counts(ctx) == before


@pytest.mark.parametrize("change", ["body", "route", "bounds", "worker", "fence", "ceiling", "role", "project"])
def test_completed_call_refuses_changed_intent(admission_context, change):
    """Same call identity with different intent conflicts even when workspace stopped."""
    ctx = admission_context
    prepared = _prepared("2")
    kw = dict(request_id="intent", task_ceiling_usd="10")
    with _admit(ctx, prepared, **kw) as admission:
        admission.settle(actual_usd="1")
    _engage_stop(ctx)
    before = _admission_counts(ctx)
    if change == "body":
        prepared = replace(prepared, body=b'{"different":true}')
    elif change == "route":
        prepared = replace(prepared, route=replace(prepared.route, model="other"))
    elif change == "bounds":
        prepared = replace(prepared, max_output_tokens=51)
    else:
        kw.update({"worker": {"worker_id": "other"}, "fence": {"fence": 2},
                   "ceiling": {"task_ceiling_usd": "11"}, "role": {"role": "other"},
                   "project": {"project_id": uuid4()}}[change])
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, prepared, **kw)
    assert exc.value.code == "idempotency_conflict"
    assert _admission_counts(ctx) == before


@pytest.mark.parametrize("changed", [
    {"actual_usd": "1.5"}, {"actual_usd": "3"}, {"input_tokens": 151}, {"input_tokens": 11}, {"output_tokens": 6},
    {"cache_tokens": 1}, {"model": "other"}, {"measurement": "estimated"},
    {"price_version": "other"}, {"actual_usd": "invalid"}, {"actual_usd": "NaN"},
    {"input_tokens": True}, {"output_tokens": -1}, {"measurement": []},
    {"measurement": "unknown"}, {"price_version": ""}, {"model": object()},
])
def test_settlement_replay_refuses_changed_usage(admission_context, changed):
    """Receipt payload conflicts preserve original ledgers and result."""
    ctx = admission_context
    kw = dict(request_id="usage-conflict")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    with _admit(ctx, _prepared("2"), **kw) as admission:
        first = admission.settle(**payload)
    durable = _accounting_snapshot(ctx)
    replay = _admit(ctx, _prepared("2"), **kw)
    assert replay.settled and replay.closed
    with pytest.raises(ModelProxyBudgetError) as exc:
        replay.settle(**(payload | changed))
    assert exc.value.code == "idempotency_conflict"
    assert replay.settle(**payload) == first
    assert _accounting_snapshot(ctx) == durable
    assert _admission_counts(ctx) == (1, 1, 1, 2)


@pytest.mark.parametrize("changed, code", [
    ({"actual_usd": "invalid"}, "invalid_request"),
    ({"actual_usd": True}, "invalid_request"),
    ({"actual_usd": "NaN"}, "invalid_request"),
    ({"actual_usd": "Infinity"}, "invalid_request"),
    ({"actual_usd": "-1"}, "invalid_request"),
    ({"actual_usd": "3"}, "usage_exceeds_admission"),
    ({"cache_tokens": 136}, "usage_exceeds_admission"),
    ({"input_tokens": True}, "invalid_request"),
    ({"output_tokens": -1}, "invalid_request"),
    ({"cache_tokens": 1.5}, "invalid_request"),
    ({"measurement": "bad"}, "invalid_request"),
    ({"measurement": []}, "invalid_request"),
    ({"measurement": "unknown", "input_tokens": 0, "output_tokens": None, "cache_tokens": None}, "invalid_request"),
    ({"measurement": "unknown", "input_tokens": None, "output_tokens": 0, "cache_tokens": None}, "invalid_request"),
    ({"measurement": "unknown", "input_tokens": None, "output_tokens": None, "cache_tokens": 0}, "invalid_request"),
    ({"price_version": ""}, "invalid_request"),
    ({"model": object()}, "invalid_request"),
    ({"price_version": {"metadata": object()}}, "invalid_request"),
])
def test_invalid_consumed_usage_charges_prepared_bound(admission_context, monkeypatch, changed, code):
    """Invalid consumed usage charges Prepared capacity before surfacing its error."""
    ctx = admission_context
    prepared = _prepared("2")
    admission = _admit(ctx, prepared)
    recorded = []

    def observe_record(*args, **kwargs):
        recorded.append(kwargs)
        return record_usage(*args, **kwargs)

    monkeypatch.setattr(budget_module, "record_usage", observe_record)
    try:
        with pytest.raises(ModelProxyBudgetError) as exc:
            with admission:
                admission.settle(**(dict(
                    actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0,
                    model="caller-model", price_version="caller-price",
                ) | changed))
        assert exc.value.code == code
        assert admission.closed and admission.settled
        # Bad measurement/counts never reach record_usage, even before rollback.
        assert len(recorded) == 1
        assert recorded[0]["measurement"] == "estimated"
        spend, usage, outbox, operations = _accounting_snapshot(ctx)
        assert len(spend) == len(usage) == len(outbox) == 1
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == prepared.max_usd
        assert usage[0]["input_tokens"] == prepared.max_input_tokens
        assert usage[0]["output_tokens"] == prepared.max_output_tokens
        assert usage[0]["cache_tokens"] == 0
        assert usage[0]["model"] == prepared.route.model
        assert usage[0]["price_version"] == prepared.route.price_version
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["settlement"] == admission._admitted_bound_payload() | {"actual_usd": "2"}
        assert _admission_counts(ctx) == (1, 1, 1, 2)
    finally:
        monkeypatch.setattr(budget_module, "record_usage", record_usage)
        if admission.closed and not admission.settled:
            admission._resolve()
        elif not admission.closed:
            admission.abort()


def test_payload_serialization_failure_charges_bound(admission_context, monkeypatch):
    """A fresh serialization error cannot escape to zero-accounting context abort."""
    ctx = admission_context
    serialize = budget_module.canonical_json
    admission = _admit(ctx, _prepared("2"))
    failed = False

    def fail_fresh_payload(value):
        nonlocal failed
        if isinstance(value, dict) and value.get("actual_usd") == "1" and not failed:
            failed = True
            raise TypeError("settlement metadata cannot serialize")
        return serialize(value)

    monkeypatch.setattr(budget_module, "canonical_json", fail_fresh_payload)
    try:
        with pytest.raises(TypeError, match="settlement metadata cannot serialize"):
            with admission:
                admission.settle(actual_usd="1", input_tokens=10)
        assert failed and admission.closed and admission.settled
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        assert _accounting_snapshot(ctx)[1][0]["price_usd"] == Decimal(2)
    finally:
        monkeypatch.setattr(budget_module, "canonical_json", serialize)
        if admission.closed and not admission.settled:
            admission._resolve()
        elif not admission.closed:
            admission.abort()


@pytest.mark.parametrize("measurement, counts", [
    ("measured", (10, None, 2)),
    ("estimated", (None, 5, None)),
    ("unknown", (None, None, None)),
])
def test_partial_valid_counts_settle_without_relabeling(admission_context, measurement, counts):
    """Partial counts stay partial; only wholly absent counts permit unknown usage."""
    ctx = admission_context
    with _admit(ctx, _prepared("2")) as admission:
        admission.settle(
            actual_usd="1", input_tokens=counts[0], output_tokens=counts[1],
            cache_tokens=counts[2], measurement=measurement,
        )
    spend, usage, _, _ = _accounting_snapshot(ctx)
    assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(1)
    assert tuple(usage[0][key] for key in ("input_tokens", "output_tokens", "cache_tokens")) == counts
    assert usage[0]["measurement"] == measurement


@pytest.mark.parametrize("change", ["stop", "policy_expiry"])
def test_admitted_settlement_survives_authority_change(admission_context, change):
    """Admission authority remains valid for settlement after stop or policy expiry."""
    ctx = admission_context
    expiry = datetime.now(UTC) + timedelta(seconds=1)
    policy = StandingPolicy(
        policy_id=uuid4(), action_class="spend_beyond_threshold", money_limit_usd=Decimal(1000),
        decision_id=uuid4(), expires_at=expiry,
    )
    # Force a tier2 call covered by a policy at admission.
    ctx.projects.set_spend_budget(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, None,
        SpendBudget("project", Decimal(1000), Decimal(0)),
        ChangeAuthority("owner", uuid4(), "owner"),
    )
    ctx.projects.put_standing_policy(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, policy,
        ChangeAuthority("owner", policy.decision_id, "owner"),
    )
    with _admit(ctx, _prepared("1"), now=expiry-timedelta(seconds=1)) as admission:
        assert admission.decision.policy_id == policy.policy_id
        if change == "stop":
            _engage_stop(ctx)
        else:
            time.sleep(max(0, (expiry-datetime.now(UTC)).total_seconds()) + 0.05)
        admission.settle(actual_usd="1", input_tokens=1)
    assert _admission_counts(ctx) == (1, 1, 1, 2)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute("SELECT policy_id, tier FROM omp_work.spend_records WHERE workspace_id=%s", (ctx.workspace_id,))
        assert cur.fetchone() == {"policy_id": policy.policy_id, "tier": "tier2"}


def test_zero_settlement_retains_tokens_without_spend(admission_context):
    """Free calls still commit usage/outbox/receipt and replay token counts."""
    ctx = admission_context
    kw = dict(request_id="free")
    with _admit(ctx, _prepared("0"), **kw) as admission:
        first = admission.settle(actual_usd="0.00", input_tokens=10, output_tokens=5, cache_tokens=2)
    replay = _admit(ctx, _prepared("0"), **kw)
    assert replay.settled
    assert replay.settle(actual_usd="0", input_tokens=10, output_tokens=5, cache_tokens=2) == first
    assert _admission_counts(ctx) == (0, 1, 1, 2)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert item_spend(cur, ctx.workspace_id, ctx.work_id).tokens == 15
        cur.execute("SELECT tokens, cache_tokens FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,))
        assert cur.fetchone() == {"tokens": 17, "cache_tokens": 2}


@pytest.mark.parametrize("fault", ["spend", "usage"])
def test_settlement_failure_rolls_back_usage_receipt_and_cancellation(admission_context, monkeypatch, fault):
    """Failed measured writes roll back, then commit one bound charge and cancellation."""
    ctx = admission_context
    work = _item(ctx, "rollback root", {**_GENEROUS_ITEM, "usd": "1"})
    job = _admitting_job(ctx, work_id=work)
    kw = dict(work_id=work, job_id=job, worker_id="worker-1", fence=1, request_id="retry-atomic")
    append = ctx.budget._projects.append_spend_cursor
    failed = False
    error = RuntimeError(f"{fault} failed")

    def append_with_fault(*args, **kwargs):
        nonlocal failed
        if failed:
            assert ctx.jobs.job_view(args[0], ctx.workspace_id, job)["status"] == "admitted"
            # Fallback must retain admission locks while writing the bound.
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                with pytest.raises(LockNotAvailable):
                    conn.execute(
                        "SELECT 1 FROM omp_work.spend_budgets WHERE workspace_id=%s"
                        " AND project_id=%s AND active FOR UPDATE NOWAIT",
                        (ctx.workspace_id, ctx.project_id),
                    )
        result = append(*args, **kwargs)
        if fault == "spend" and not failed:
            failed = True
            raise error
        return result

    def fail_after_usage_once(*args, **kwargs):
        nonlocal failed
        result = record_usage(*args, **kwargs)
        if not failed:
            assert ctx.jobs.job_view(kwargs["cur"], ctx.workspace_id, job)["status"] == "cancelled"
            failed = True
            raise error
        return result

    admission = _admit(ctx, _prepared("1"), **kw)
    try:
        monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", append_with_fault)
        if fault == "usage":
            monkeypatch.setattr(budget_module, "record_usage", fail_after_usage_once)
        with pytest.raises(RuntimeError, match=f"{fault} failed") as raised:
            with admission:
                admission.settle(actual_usd="1", input_tokens=10, output_tokens=5)
        assert raised.value is error
        assert admission.closed and admission.settled
        charged = _admission_counts(ctx)
        assert charged[:2] == (1, 1)
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            assert ctx.jobs.job_view(cur, ctx.workspace_id, job)["status"] == "cancelled"
            cur.execute("SELECT result FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt'", (ctx.workspace_id,))
            receipt = cur.fetchone()["result"]
            bound = dict(
                actual_usd="1", input_tokens=100, output_tokens=50, cache_tokens=0,
                measurement="estimated", model="test-model", price_version="p1",
            )
            assert receipt["settlement"] == bound
            assert cur.fetchone() is None
            cur.execute("SELECT price_usd, tokens, cache_tokens, measurement, price_version FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,))
            assert cur.fetchone() == dict(price_usd=Decimal(1), tokens=150, cache_tokens=0, measurement="estimated", price_version="p1")
            cur.execute("SELECT count(*) AS n FROM omp_jobs.outbox WHERE workspace_id=%s AND kind='usage_ledger'", (ctx.workspace_id,))
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT payload FROM omp_jobs.outbox WHERE workspace_id=%s AND kind='budget_alert' AND payload->>'event'='budget.exceeded'", (ctx.workspace_id,))
            assert cur.fetchone()["payload"]["dimension"] == "usd"
            assert cur.fetchone() is None
        # Receipt replay after cancellation cannot cause another send or charge.
        with _admit(ctx, _prepared("1"), **kw) as replay:
            assert replay.settled
            replay.settle(**bound)
        assert _admission_counts(ctx) == charged
    finally:
        monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", append)
        monkeypatch.setattr(budget_module, "record_usage", record_usage)
        if not admission.settled:
            admission._resolve()


def test_usage_after_spend_fault_proves_atomic_bound_replacement(admission_context):
    """A real SQL usage failure discards measured spend before committing the bound."""
    ctx = admission_context
    mission = _mission(ctx, "1000")
    root = _item(ctx, "SQL fault root", {**_GENEROUS_ITEM, "usd": "1"})
    task = _item(ctx, "SQL fault task", _GENEROUS_ITEM)
    assert root != task
    parent = _admitting_job(ctx, work_id=root)
    child = _admitting_job(ctx, work_id=task, parent_job_id=parent)
    request_id = f"usage-after-spend-{uuid4()}"
    identity = budget_module.sha256({
        "workspace_id": str(ctx.workspace_id), "work_id": str(task),
        "job_id": child, "request_id": request_id,
    })
    record_id = UUID(bytes=bytes.fromhex(identity[:32]))
    usage_request = f"model_call:request:{identity}"
    fault_name = f"usage_after_spend_{uuid4().hex}"
    trigger = sql.Identifier(fault_name)
    function = sql.Identifier("omp_jobs", fault_name)
    kwargs = ctx.service.config.connection_kwargs("postgres")
    admission = None
    try:
        with psycopg.connect(**kwargs) as conn:
            conn.execute(sql.SQL("""
                CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE captured omp_work.spend_records%ROWTYPE;
                BEGIN
                    IF NEW.workspace_id = {workspace}
                       AND NEW.request_id = {request}
                       AND NEW.measurement = 'measured' THEN
                        SELECT * INTO captured FROM omp_work.spend_records
                        WHERE workspace_id = {workspace} AND record_id = {record};
                        IF NOT FOUND OR captured.amount_usd <> 0.25 THEN
                            RAISE EXCEPTION 'measured spend absent before usage'
                                USING ERRCODE = 'P0001';
                        END IF;
                        RAISE EXCEPTION 'measured usage rejected after spend'
                            USING ERRCODE = '23514',
                                  DETAIL = format('record_id=%s amount_usd=%s',
                                                  captured.record_id, captured.amount_usd);
                    END IF;
                    RETURN NEW;
                END;
                $$
            """).format(
                function=function, workspace=sql.Literal(ctx.workspace_id),
                request=sql.Literal(usage_request), record=sql.Literal(record_id),
            ))
            conn.execute(sql.SQL(
                "CREATE TRIGGER {trigger} BEFORE INSERT ON omp_jobs.usage_events"
                " FOR EACH ROW EXECUTE FUNCTION {function}()"
            ).format(trigger=trigger, function=function))

        admission = _admit(
            ctx, _prepared("1", 100, 50), mission_id=mission, work_id=task,
            job_id=child, worker_id="worker-1", fence=1,
            request_id=request_id, task_ceiling_usd=Decimal("1000"),
        )
        assert admission.root_work_id == root
        assert admission._spend_record_id() == record_id
        with pytest.raises(CheckViolation) as raised:
            admission.settle(
                actual_usd="0.25", input_tokens=7, output_tokens=3,
                cache_tokens=2, measurement="measured", price_version="p1",
            )
        assert raised.value.sqlstate == "23514"
        assert raised.value.diag.message_primary == "measured usage rejected after spend"
        detail = dict(part.split("=", 1) for part in raised.value.diag.message_detail.split())
        assert UUID(detail["record_id"]) == record_id
        assert Decimal(detail["amount_usd"]) == Decimal("0.25")
        assert fault_name in raised.value.diag.context
        assert admission.closed and admission.settled

        # Independent connection sees the committed bound, never the partial .25 write.
        spend, usage, _, operations = _accounting_snapshot(ctx)
        assert len(spend) == len(usage) == 1
        assert spend[0]["record_id"] == record_id
        assert spend[0]["amount_usd"] == Decimal(1)
        assert spend[0]["mission_id"] == mission
        assert usage[0]["request_id"] == usage_request
        assert usage[0]["work_id"] == task and usage[0]["job_id_derived"] == child
        assert usage[0]["price_usd"] == Decimal(1)
        assert usage[0]["measurement"] == "estimated"
        assert (usage[0]["input_tokens"], usage[0]["output_tokens"],
                usage[0]["cache_tokens"]) == (100, 50, 0)
        receipts = [row["result"] for row in operations if row["kind"] == "model_call_receipt"]
        assert len(receipts) == 1
        assert receipts[0]["settlement"] == {
            "actual_usd": "1", "input_tokens": 100, "output_tokens": 50,
            "cache_tokens": 0, "measurement": "estimated", "model": "test-model",
            "price_version": "p1",
        }
    finally:
        if admission is not None and not admission.closed:
            admission.abort()
        try:
            with psycopg.connect(**kwargs) as conn:
                conn.execute(sql.SQL(
                    "DROP TRIGGER IF EXISTS {trigger} ON omp_jobs.usage_events"
                ).format(trigger=trigger))
                conn.execute(sql.SQL("DROP FUNCTION IF EXISTS {function}()"
                                     ).format(function=function))
        finally:
            if admission is not None and admission.closed and not admission.settled:
                admission._resolve()
        with psycopg.connect(**kwargs) as conn:
            assert conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname=%s", (fault_name,),
            ).fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                " WHERE n.nspname='omp_jobs' AND p.proname=%s", (fault_name,),
            ).fetchone() is None


@pytest.mark.parametrize("fault", ["rollback-before-commit", "commit-then-raise"])
def test_failed_settlement_reconciles_durable_accounting(admission_context, monkeypatch, fault):
    """Consumed calls retain ownership across rollback or ambiguous commit, without another send/charge."""
    ctx = admission_context
    policy = StandingPolicy(
        policy_id=uuid4(), action_class="spend_beyond_threshold", money_limit_usd=Decimal(1000),
        decision_id=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    authority = ChangeAuthority("owner", policy.decision_id, "owner")
    ctx.projects.set_spend_budget(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, None,
        SpendBudget("project", Decimal(1000), Decimal(0)), authority,
    )
    ctx.projects.put_standing_policy(ctx.workspace_id, ctx.actor_id, ctx.project_id, policy, authority)
    job = _admitting_job(ctx)
    kw = dict(job_id=job, worker_id="worker-1", fence=1, request_id="failed-commit")
    payload = dict(actual_usd="1.00", input_tokens=10, output_tokens=5, cache_tokens=0)
    original = ctx.budget._jobs.transaction
    enabled = True
    connections = []
    sends = []
    replays = []

    @contextmanager
    def failing_transaction(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            connections.append(cur.connection)
            yield cur
            if enabled and fault == "rollback-before-commit":
                raise RuntimeError(fault)
        if enabled and fault == "commit-then-raise":
            raise RuntimeError(fault)

    monkeypatch.setattr(ctx.budget._jobs, "transaction", failing_transaction)
    admission = _admit(ctx, _prepared("2"), **kw)
    sends.append("upstream")
    before = _admission_counts(ctx)
    waiter, finished = None, threading.Event()
    outcomes = []
    try:
        # Private reconciliation has no authority to close a fresh admission.
        with pytest.raises(ModelProxyBudgetError):
            admission._resolve()
        assert not admission.closed and not admission.settled
        with pytest.raises(RuntimeError, match=fault):
            with admission:
                admission.settle(**payload)
        admission.abort()
        assert admission.closed and not admission.settled
        expected = before if fault == "rollback-before-commit" else tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        assert _admission_counts(ctx) == expected

        durable = _accounting_snapshot(ctx)
        for changed_usage in ({"actual_usd": "3"}, {"actual_usd": "NaN"}, {"measurement": []}):
            with pytest.raises(ModelProxyBudgetError) as changed:
                admission.settle(**(payload | changed_usage))
            assert changed.value.code == "idempotency_conflict"
            assert _accounting_snapshot(ctx) == durable
            assert admission.closed and not admission.settled
        with pytest.raises(RuntimeError, match=fault):
            admission.settle(**payload)
        assert _admission_counts(ctx) == expected
        assert not admission.settled
        assert len(connections) == 2 and connections[0] is not connections[1]
        assert all(conn.closed for conn in connections)

        def waiting():
            with _admit(ctx, _prepared("2"), **kw) as replay:
                replays.append(replay.settled)
                outcomes.append(replay.settle(**payload))
            finished.set()

        waiter = threading.Thread(target=waiting)
        waiter.start()
        assert not finished.wait(0.2), "failed settlement freed process gate"
        _engage_stop(ctx)
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("UPDATE omp_work.standing_policies SET expires_at=clock_timestamp()-interval '1 second' WHERE policy_id=%s", (policy.policy_id,))
            conn.execute("UPDATE omp_jobs.jobs SET worker_id='other', fence=fence+1, lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=%s", (job,))

        def forbidden(*args, **kwargs):
            pytest.fail("resolution reauthorized consumed call")

        monkeypatch.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden)
        enabled = False
        result = admission.settle(**(payload | {"actual_usd": "1"}))
        assert finished.wait(5)
        waiter.join(timeout=5)
        assert admission.closed and admission.settled
        assert outcomes == [result] and replays == [True] and sends == ["upstream"]
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute("SELECT result FROM omp_jobs.operations WHERE operation_id=%s", (f"model_call:receipt:{admission._identity}",))
            receipt = cur.fetchone()["result"]
            assert receipt["root_work_id"] == str(ctx.work_id)
            assert receipt["settlement"]["actual_usd"] == "1"
            cur.execute("SELECT work_id, job_id_derived, price_usd, tokens FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,))
            assert cur.fetchone() == dict(work_id=ctx.work_id, job_id_derived=job, price_usd=Decimal(1), tokens=15)
            cur.execute("SELECT project_id, amount_usd, policy_id, tier FROM omp_work.spend_records WHERE workspace_id=%s", (ctx.workspace_id,))
            assert cur.fetchone() == dict(project_id=ctx.project_id, amount_usd=Decimal(1), policy_id=policy.policy_id, tier="tier2")
    finally:
        enabled = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if not admission.settled:
            admission.settle(**payload)
        if waiter is not None:
            waiter.join(timeout=5)


@pytest.mark.parametrize("field", ["intent", "fingerprint", "root_work_id", "settlement"])
def test_resolution_receipt_conflict_retains_consumed_owner(admission_context, monkeypatch, field):
    """Conflicting durable receipt cannot settle/release an owner or rewrite its ledgers."""
    ctx = admission_context
    original = ctx.budget._jobs.transaction

    @contextmanager
    def ambiguous(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            yield cur
        raise RuntimeError("ambiguous commit")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", ambiguous)
    admission = _admit(ctx, _prepared("1"), request_id="receipt-conflict")
    receipt_id = f"model_call:receipt:{admission._identity}"
    stored = None
    try:
        with pytest.raises(RuntimeError, match="ambiguous commit"):
            admission.settle(actual_usd="1")
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        before = _admission_counts(ctx)
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute("SELECT request_sha256, result FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,))
            stored = cur.fetchone()
            if field == "intent":
                cur.execute("UPDATE omp_jobs.operations SET request_sha256=%s WHERE operation_id=%s", ("0" * 64, receipt_id))
            else:
                cur.execute("UPDATE omp_jobs.operations SET result=jsonb_set(result, %s, %s) WHERE operation_id=%s", ([field], Jsonb("changed"), receipt_id))
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(actual_usd="1")
        assert conflict.value.code == "idempotency_conflict"
        assert admission.closed and not admission.settled
        admission.abort()
        assert admission.__exit__(None, None, None) is False
        assert _admission_counts(ctx) == before
    finally:
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if stored is not None:
            with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
                cur.execute("UPDATE omp_jobs.operations SET request_sha256=%s, result=%s WHERE operation_id=%s", (stored["request_sha256"], Jsonb(stored["result"]), receipt_id))
        if not admission.settled:
            admission.settle(actual_usd="1")
    assert _admission_counts(ctx) == (1, 1, 1, 2)


def test_resolution_cannot_redirect_captured_root(admission_context, monkeypatch):
    """Reparenting a failed consumed job conflicts instead of accounting under new root locks."""
    ctx = admission_context
    other = _item(ctx, "different root")
    parent = _admitting_job(ctx, work_id=other)
    job = _admitting_job(ctx)
    original = ctx.budget._jobs.transaction
    enabled = True

    @contextmanager
    def rollback_before_commit(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            yield cur
            if enabled:
                raise RuntimeError("commit rolled back")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", rollback_before_commit)

    admission = _admit(ctx, _prepared("1"), job_id=job, worker_id="worker-1", fence=1)
    try:
        with pytest.raises(RuntimeError, match="commit rolled back"):
            admission.settle(actual_usd="1")
        enabled = False
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("UPDATE omp_jobs.jobs SET parent_job_id=%s WHERE job_id=%s", (parent, job))
        before = _admission_counts(ctx)
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(actual_usd="1")
        assert conflict.value.code == "idempotency_conflict"
        assert not admission.settled and admission.closed
        assert _admission_counts(ctx) == before
    finally:
        enabled = False
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("UPDATE omp_jobs.jobs SET parent_job_id=NULL WHERE job_id=%s", (job,))
        if not admission.settled:
            admission.settle(actual_usd="1")
    assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))


def _arm_cleanup_pauses(monkeypatch, jobs):
    """Pause cursor close, COMMIT, and connection close for one armed transaction.

    Class patches are restored by the monkeypatch fixture. Only the cursor and
    connection recorded while arming is enabled block, so a disposable count
    query can run during the pause.
    """
    phase = {"stage": None}
    targets: dict[str, object] = {}
    enabled = {"on": True}
    transaction = jobs.transaction
    cursor_closing, allow_cursor = threading.Event(), threading.Event()
    committing, allow_commit = threading.Event(), threading.Event()
    connection_closing, allow_connection = threading.Event(), threading.Event()
    real_cursor_close = psycopg.Cursor.close
    real_conn_close = psycopg.Connection.close
    real_exec = psycopg.Connection._exec_command

    def cursor_close(self):
        if self is targets.get("cursor") and phase["stage"] == "cleanup":
            cursor_closing.set()
            assert allow_cursor.wait(5), "cleanup test did not release cursor close"
            phase["stage"] = "after-cursor"
        return real_cursor_close(self)

    def connection_close(self):
        if self is targets.get("conn") and phase["stage"] in {"after-cursor", "after-commit"}:
            connection_closing.set()
            assert allow_connection.wait(5), "cleanup test did not release connection close"
            phase["stage"] = "done"
        return real_conn_close(self)

    def exec_command(self, command, *args, **kwargs):
        if (
            self is targets.get("conn")
            and phase["stage"] == "after-cursor"
            and command == b"COMMIT"
        ):
            committing.set()
            assert allow_commit.wait(5), "cleanup test did not release commit"
            phase["stage"] = "after-commit"
        return real_exec(self, command, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "close", cursor_close)
    monkeypatch.setattr(psycopg.Connection, "close", connection_close)
    monkeypatch.setattr(psycopg.Connection, "_exec_command", exec_command)

    @contextmanager
    def paused(workspace_id, actor_id):
        with transaction(workspace_id, actor_id) as cur:
            if enabled["on"]:
                targets["cursor"] = cur
                targets["conn"] = cur.connection
                phase["stage"] = "cleanup"
            yield cur

    monkeypatch.setattr(jobs, "transaction", paused)

    def release() -> None:
        allow_cursor.set()
        allow_commit.set()
        allow_connection.set()

    def reset() -> None:
        cursor_closing.clear()
        allow_cursor.clear()
        committing.clear()
        allow_commit.clear()
        connection_closing.clear()
        allow_connection.clear()
        phase["stage"] = None
        targets.clear()

    return SimpleNamespace(
        cursor_closing=cursor_closing,
        allow_cursor=allow_cursor,
        committing=committing,
        allow_commit=allow_commit,
        connection_closing=connection_closing,
        allow_connection=allow_connection,
        enabled=enabled,
        release=release,
        reset=reset,
    )


def _start_gate_waiter(ctx, work_id, request_id):
    """Start an unrelated admission that can enter only when the process gate is free."""
    waiting, finished = threading.Event(), threading.Event()
    outcomes: list[str] = []

    def waiter():
        waiting.set()
        try:
            with ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, None, None, work_id,
                None, None, None, request_id, None, _prepared("1"),
            ):
                outcomes.append("admitted")
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        finally:
            finished.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    return thread, waiting, finished, outcomes


@pytest.mark.parametrize("retry", [False, True])
def test_process_gate_stays_held_until_settlement_commit(admission_context, monkeypatch, retry):
    """Settlement keeps the gate through commit and cursor/connection cleanup."""
    ctx = admission_context
    other = _item(ctx, "separate commit waiter")
    if retry:
        admission = _admit(ctx, _prepared("1"))

        def fail(*args, **kwargs):
            raise RuntimeError("spend failed")

        with monkeypatch.context() as fault:
            fault.setattr(ctx.budget._projects, "append_spend_cursor", fail)
            with pytest.raises(RuntimeError, match="spend failed"):
                admission.settle(actual_usd="1")
        assert admission.closed and not admission.settled
        assert admission.cursor.connection.closed
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(actual_usd="1")
        assert conflict.value.code == "idempotency_conflict"
        pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs)
    else:
        pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs)
        admission = _admit(ctx, _prepared("1"))
    outcomes: list[str] = []

    def settle():
        try:
            if retry:
                admission._resolve()
            else:
                admission.settle(actual_usd="1")
            outcomes.append("committed")
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    settlement = threading.Thread(target=settle)
    second = None
    try:
        settlement.start()
        assert pauses.cursor_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        second, waiting, finished, waiter_outcomes = _start_gate_waiter(
            ctx, other, "commit-waiter"
        )
        assert waiting.wait(2)
        assert not finished.wait(0.2), "gate released before cursor cleanup"
        pauses.allow_cursor.set()

        assert pauses.committing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        assert not finished.wait(0.2), "gate released before commit"
        pauses.allow_commit.set()

        assert pauses.connection_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        assert not admission.settled
        assert not finished.wait(0.2), "gate released before connection cleanup"
    finally:
        pauses.release()
        settlement.join(timeout=7)
        if second is not None and second.ident is not None:
            second.join(timeout=7)
        if not admission.settled:
            admission._resolve()
        if second is not None and second.ident is not None:
            second.join(timeout=7)
    assert not settlement.is_alive() and second is not None and not second.is_alive()
    assert sorted(outcomes + waiter_outcomes) == ["admitted", "committed"]
    assert admission.settled and admission.closed
    assert _admission_counts(ctx) == (1, 1, 1, 2)

    if retry:
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute("SELECT result FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt'", (ctx.workspace_id,))
            assert cur.fetchone()["result"]["settlement"] == dict(
                actual_usd="1", input_tokens=100, output_tokens=50, cache_tokens=0,
                measurement="estimated", model="test-model", price_version="p1",
            )


def test_abort_and_receipt_replay_release_gate_after_cleanup(admission_context, monkeypatch):
    """Pre-send abort and receipt replay finish DB cleanup before one gate release."""
    ctx = admission_context
    abort_waiter_work = _item(ctx, "abort cleanup waiter")
    replay_waiter_work = _item(ctx, "replay cleanup waiter")
    pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs)
    admission = _admit(ctx, _prepared("1"), project_id=None, request_id="abort-cleanup")
    outcomes: list[str] = []

    def abort():
        try:
            admission.abort()
            outcomes.append("aborted")
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    worker = threading.Thread(target=abort)
    waiter = None
    try:
        worker.start()
        assert pauses.cursor_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        waiter, waiting, finished, waiter_outcomes = _start_gate_waiter(
            ctx, abort_waiter_work, "abort-waiter"
        )
        assert waiting.wait(2)
        assert not finished.wait(0.2), "gate released before abort cursor cleanup"
        pauses.allow_cursor.set()
        assert pauses.connection_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        assert not finished.wait(0.2), "gate released before abort connection cleanup"
    finally:
        pauses.release()
        worker.join(timeout=7)
        if waiter is not None and waiter.ident is not None:
            waiter.join(timeout=7)
        admission.abort()
    assert not worker.is_alive() and waiter is not None and not waiter.is_alive()
    assert sorted(outcomes + waiter_outcomes) == ["aborted", "admitted"]
    assert admission.closed and not admission.settled
    with pytest.raises(ModelProxyBudgetError) as exc:
        admission.settle(actual_usd="1")
    assert exc.value.code == "already_settled"
    admission.abort()
    assert admission.__exit__(None, None, None) is False
    assert _admission_counts(ctx) == (0, 0, 0, 0)

    pauses.enabled["on"] = False
    pauses.reset()
    payload = dict(actual_usd="1", input_tokens=1)
    with _admit(ctx, _prepared("1"), request_id="replay-cleanup") as settled:
        first = settled.settle(**payload)
    charged = _admission_counts(ctx)
    assert charged == (1, 1, 1, 2)
    pauses.enabled["on"] = True
    pauses.reset()
    replay_outcomes: list[str] = []
    replayed: list[object] = []

    def replay():
        try:
            found = _admit(ctx, _prepared("1"), request_id="replay-cleanup")
            replayed.append(found)
            replay_outcomes.append("replayed" if found.settled else "open")
        except Exception as exc:
            replay_outcomes.append(type(exc).__name__)

    worker = threading.Thread(target=replay)
    waiter = None
    try:
        worker.start()
        assert pauses.cursor_closing.wait(5), replay_outcomes
        assert _admission_counts(ctx) == charged
        waiter, waiting, finished, waiter_outcomes = _start_gate_waiter(
            ctx, replay_waiter_work, "replay-waiter"
        )
        assert waiting.wait(2)
        assert not finished.wait(0.2), "gate released before replay cursor cleanup"
        pauses.allow_cursor.set()
        assert pauses.committing.wait(5), replay_outcomes
        assert _admission_counts(ctx) == charged
        assert not finished.wait(0.2), "gate released before replay commit"
        pauses.allow_commit.set()
        assert pauses.connection_closing.wait(5), replay_outcomes
        assert _admission_counts(ctx) == charged
        assert not finished.wait(0.2), "gate released before replay connection cleanup"
    finally:
        pauses.release()
        worker.join(timeout=7)
        if replayed and not replayed[0].closed:
            replayed[0].abort()
        if waiter is not None and waiter.ident is not None:
            waiter.join(timeout=7)
    assert not worker.is_alive() and waiter is not None and not waiter.is_alive()
    assert replay_outcomes == ["replayed"]
    assert waiter_outcomes == ["admitted"]
    assert len(replayed) == 1
    found = replayed[0]
    assert found.settled and found.closed
    assert found.settle(**payload) == first
    assert _admission_counts(ctx) == charged


def test_stale_owner_cleanup_cannot_release_another_gate(admission_context):
    """Repeated cleanup of a finished owner cannot admit a third while a second holds the gate."""
    ctx = admission_context
    second_work = _item(ctx, "second gate owner")
    third_work = _item(ctx, "third gate owner")
    payload = dict(actual_usd="1", input_tokens=1)
    first = _admit(ctx, _prepared("1"), request_id="first-owner")
    first_result = first.settle(**payload)
    assert first.settled and first.closed
    charged = _admission_counts(ctx)
    assert charged == (1, 1, 1, 2)

    def cleanup_first() -> None:
        first.abort()
        assert first.__exit__(None, None, None) is False
        with first:
            pass
        assert first.settle(**payload) == first_result

    second = ModelProxyBudget(ctx.service.config).admit(
        ctx.workspace_id, ctx.actor_id, None, None, second_work,
        None, None, None, "second-owner", None, _prepared("1"),
    )
    assert not second.closed
    cleanup_first()
    cleanup_first()
    assert _admission_counts(ctx) == charged

    started, entered, finished = (threading.Event() for _ in range(3))
    outcomes: list[str] = []

    def third():
        started.set()
        try:
            with ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, None, None, third_work,
                None, None, None, "third-owner", None, _prepared("1"),
            ):
                entered.set()
                outcomes.append("admitted")
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        finally:
            finished.set()

    thread = threading.Thread(target=third)
    try:
        thread.start()
        assert started.wait(2), outcomes
        assert not entered.wait(0.2) and not finished.wait(0.2), outcomes
        cleanup_first()
        assert not entered.wait(0.2) and not finished.wait(0.2), (
            "stale cleanup released the second owner's gate"
        )
        assert _admission_counts(ctx) == charged
    finally:
        second.abort()
        thread.join(timeout=7)
    assert not thread.is_alive()
    assert second.closed and not second.settled
    assert outcomes == ["admitted"]
    assert _admission_counts(ctx) == charged


@pytest.mark.parametrize("changed_intent", [False, True])
def test_waiting_admit_observes_replay_owner_publication(
    admission_context, monkeypatch, changed_intent
):
    """A retry already waiting at replay commit resolves or conflicts without a third retry."""
    ctx = admission_context
    prepared = _prepared("1")
    request_id = "waiting-replay-owner"
    payload = dict(actual_usd="1", input_tokens=1)
    sends = []
    with _admit(ctx, prepared, request_id=request_id) as admission:
        sends.append("upstream")
        first = admission.settle(**payload)
    charged = _admission_counts(ctx)
    assert charged == (1, 1, 1, 2)
    unrelated = _item(ctx, "waiting replay unrelated")
    at_exit, allow_exit = threading.Event(), threading.Event()
    waiting, retry_done = threading.Event(), threading.Event()
    outcomes, retries = [], []
    original_transaction = ctx.budget._jobs.transaction
    original_wait = budget_module._ProcessGate.transition.wait

    @contextmanager
    def failing_exit(workspace_id, actor_id):
        with original_transaction(workspace_id, actor_id) as cur:
            yield cur
            if threading.current_thread() is replay_thread:
                at_exit.set()
                assert allow_exit.wait(5), "replay exit not released"
        if threading.current_thread() is replay_thread:
            raise RuntimeError("replay commit failed")

    def observed_wait(timeout=None):
        if threading.current_thread() is retry_thread:
            # The condition is held here; publication cannot overtake this wait.
            waiting.set()
        return original_wait(timeout)

    def replay():
        try:
            _admit(ctx, prepared, request_id=request_id)
        except BaseException as exc:
            outcomes.append(exc)

    def retry():
        found = None
        try:
            body = replace(prepared, body=b'{"changed":true}') if changed_intent else prepared
            found = ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, ctx.project_id, None, ctx.work_id,
                None, None, None, request_id, None, body,
            )
            if not found.settled:
                sends.append("upstream")
            retries.append(found)
        except BaseException as exc:
            retries.append(exc)
        finally:
            if found is not None and not found.closed:
                found.abort()
            retry_done.set()

    replay_thread = threading.Thread(target=replay)
    retry_thread = threading.Thread(target=retry)
    waiter = None
    with monkeypatch.context() as patch:
        patch.setattr(ctx.budget._jobs, "transaction", failing_exit)
        patch.setattr(budget_module._ProcessGate.transition, "wait", observed_wait)
        try:
            replay_thread.start()
            assert at_exit.wait(5)
            retry_thread.start()
            assert waiting.wait(2)
            assert not retry_done.wait(0.2), "retry bypassed held replay gate"
            allow_exit.set()
            replay_thread.join(timeout=5)
            assert not replay_thread.is_alive()
            assert len(outcomes) == 1 and str(outcomes[0]) == "replay commit failed"
            assert retry_done.wait(5), "waiting retry missed retained-owner publication"
            retry_thread.join(timeout=5)
            assert len(retries) == 1
            if changed_intent:
                assert isinstance(retries[0], ModelProxyBudgetError)
                assert retries[0].code == "idempotency_conflict"
                waiter, started, finished, waiter_outcomes = _start_gate_waiter(
                    ctx, unrelated, "waiting-replay-unrelated"
                )
                assert started.wait(2)
                assert not finished.wait(0.2), "conflict released original ownership"
            else:
                assert retries[0].settled and retries[0].closed
                assert retries[0].settle(**payload) == first
            assert sends == ["upstream"] and _admission_counts(ctx) == charged
        finally:
            allow_exit.set()
            replay_thread.join(timeout=7)
            # Supported retry resolves any retained owner, including on assertion failure.
            with _admit(ctx, prepared, request_id=request_id) as cleaned:
                assert cleaned.settled
            if retry_thread.ident is not None:
                retry_thread.join(timeout=7)
            if waiter is not None:
                waiter.join(timeout=7)
    assert not replay_thread.is_alive() and not retry_thread.is_alive()
    if waiter is not None:
        assert not waiter.is_alive() and waiter_outcomes == ["admitted"]
    assert sends == ["upstream"] and _admission_counts(ctx) == charged


@pytest.mark.parametrize("fault", ["rollback-before-commit", "commit-then-raise"])
def test_replay_commit_exception_retains_owner_until_identical_admit(
    admission_context, monkeypatch, fault
):
    """A replay read-exit fault keeps one reachable owner and blocks another send.

    Another budget instance resolves that owner only for the same intent, after
    the durable receipt is read and cleaned up. Changed intent and repeated
    reconciliation failures leave the owner, gate, and ledgers in place.
    """
    ctx = admission_context
    unrelated = _item(ctx, "unrelated during replay retain")
    holder_work = _item(ctx, "holder after replay resolution")
    third_work = _item(ctx, "third after replay holder")
    request_id = "replay-commit-retain"
    prepared = _prepared("1")
    payload = dict(actual_usd="1", input_tokens=1)
    sends: list[str] = []
    with _admit(ctx, prepared, request_id=request_id) as admission:
        if not admission.settled:
            sends.append("upstream")
        first = admission.settle(**payload)
    charged = _admission_counts(ctx)
    assert charged == (1, 1, 1, 2) and sends == ["upstream"]

    original = ctx.budget._jobs.transaction
    enabled = {"on": True}
    connections: list[object] = []

    @contextmanager
    def failing_exit(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            connections.append(cur.connection)
            yield cur
            if enabled["on"] and fault == "rollback-before-commit":
                raise RuntimeError(fault)
        if enabled["on"] and fault == "commit-then-raise":
            raise RuntimeError(fault)

    def admit_other(body: Prepared, budget: ModelProxyBudget | None = None):
        return (budget or ModelProxyBudget(ctx.service.config)).admit(
            ctx.workspace_id, ctx.actor_id, ctx.project_id, None, ctx.work_id,
            None, None, None, request_id, None, body,
        )

    waiter = holder = None
    threads: list[threading.Thread] = []
    resolved: list[object] = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ctx.budget._jobs, "transaction", failing_exit)
            with pytest.raises(RuntimeError, match=fault):
                _admit(ctx, prepared, request_id=request_id)
            assert all(conn.closed for conn in connections)
            waiter, waiting, finished, waiter_outcomes = _start_gate_waiter(
                ctx, unrelated, "during-replay-retain"
            )
            threads.append(waiter)
            assert waiting.wait(2)
            assert not finished.wait(0.2), "replay commit exception released the gate"

            other = ModelProxyBudget(ctx.service.config)
            for _ in range(2):
                with pytest.raises(RuntimeError, match=fault):
                    admit_other(prepared, other)
                assert not finished.wait(0.2), "failed reconciliation released the gate"
                assert _admission_counts(ctx) == charged
            assert all(conn.closed for conn in connections)
            assert sends == ["upstream"]

            with pytest.raises(ModelProxyBudgetError) as conflict:
                admit_other(replace(prepared, body=b'{"different":true}'), other)
            assert conflict.value.code == "idempotency_conflict"
            assert not finished.wait(0.2), "changed intent replaced the retained owner"
            assert _admission_counts(ctx) == charged

            enabled["on"] = False
            errors: list[BaseException] = []
            ready = threading.Barrier(3)

            def resolve() -> None:
                found = None
                try:
                    ready.wait(5)
                    found = admit_other(prepared, ModelProxyBudget(ctx.service.config))
                    if not found.settled:
                        sends.append("upstream")
                    resolved.append(found)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if found is not None and not found.closed:
                        found.abort()

            resolvers = [threading.Thread(target=resolve) for _ in range(3)]
            threads.extend(resolvers)
            for thread in resolvers:
                thread.start()
            for thread in resolvers:
                thread.join(timeout=7)
            assert errors == [] and all(not thread.is_alive() for thread in resolvers)
            assert len(resolved) == 3
            assert all(found.settled and found.closed for found in resolved)
            assert all(found.settle(**payload) == first for found in resolved)
            assert sends == ["upstream"] and _admission_counts(ctx) == charged
            assert finished.wait(5), waiter_outcomes
            waiter.join(timeout=7)
            assert not waiter.is_alive() and waiter_outcomes == ["admitted"]

        holder = ModelProxyBudget(ctx.service.config).admit(
            ctx.workspace_id, ctx.actor_id, None, None, holder_work,
            None, None, None, "holder-after-replay", None, prepared,
        )
        third, third_waiting, third_finished, third_outcomes = _start_gate_waiter(
            ctx, third_work, "third-after-replay"
        )
        threads.append(third)
        same_done = threading.Event()
        same_out: list[object] = []

        def same_again() -> None:
            found = None
            try:
                found = admit_other(prepared)
                if not found.settled:
                    sends.append("upstream")
                same_out.append(found)
            except BaseException as exc:
                same_out.append(exc)
            finally:
                if found is not None and not found.closed:
                    found.abort()
                same_done.set()

        same_thread = threading.Thread(target=same_again)
        threads.append(same_thread)
        same_thread.start()
        assert third_waiting.wait(2), third_outcomes
        for found in resolved:
            found.abort()
            assert found.__exit__(None, None, None) is False
            assert found.settle(**payload) == first
        assert not third_finished.wait(0.2), "repeated resolution released the later gate"
        assert _admission_counts(ctx) == charged and sends == ["upstream"]
    finally:
        enabled["on"] = False
        if holder is not None and not holder.closed:
            holder.abort()
        try:
            cleaned = admit_other(prepared)
            if not cleaned.settled:
                cleaned.abort()
        except Exception:
            pass
        for thread in threads:
            thread.join(timeout=7)
    assert holder is not None
    assert holder.closed and not holder.settled
    assert third_finished.wait(5), third_outcomes
    assert same_done.wait(5)
    assert not third.is_alive() and not same_thread.is_alive()
    assert third_outcomes == ["admitted"]
    assert same_out and all(getattr(item, "settled", False) for item in same_out)
    assert sends == ["upstream"] and _admission_counts(ctx) == charged
    for thread in threads:
        assert not thread.is_alive()


def _accounting_snapshot(ctx):
    """Compare durable rows, including receipt/usage/outbox contents, across replay."""
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        rows = []
        keys = ("record_id", "usage_id", "event_id", "operation_id")
        for table, key in zip(_ADMISSION_TABLES, keys):
            cur.execute(
                f"SELECT * FROM {table} WHERE workspace_id=%s ORDER BY {key}",  # nosec B608 - fixed tables/keys
                (ctx.workspace_id,),
            )
            rows.append(cur.fetchall())
        return rows


def test_rolled_back_settlement_and_stale_cleanup_keep_successor_excluded(
    admission_context, monkeypatch
):
    """Rollback blocks another send; resolved owner's cleanup cannot free its successor."""
    ctx = admission_context
    second_work = _item(ctx, "rollback waiter")
    third_work = _item(ctx, "rollback third")
    original = ctx.budget._jobs.transaction
    connections = []
    fault = True

    @contextmanager
    def rollback_commit(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            cur.execute("SET LOCAL lock_timeout = '3s'")
            connections.append(cur.connection)
            yield cur
            if fault:
                raise RuntimeError("settlement commit rolled back")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", rollback_commit)
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    admission = _admit(ctx, prepared, request_id="rollback-owner")
    sends = ["first"]
    started, entered, finish_waiter, done = (threading.Event() for _ in range(4))
    errors = []

    def waiter():
        started.set()
        try:
            with ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, ctx.project_id, None, second_work,
                None, None, None, "rollback-waiter", None, prepared,
            ) as second:
                entered.set()
                assert finish_waiter.wait(5), "waiter not released"
                sends.append("second")
                second.settle(actual_usd="1", input_tokens=2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    second = threading.Thread(target=waiter)
    third = None
    try:
        with pytest.raises(RuntimeError, match="settlement commit rolled back"):
            with admission:
                admission.settle(**payload)
        assert admission.closed and not admission.settled
        assert connections and all(conn.closed for conn in connections)
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        second.start()
        assert started.wait(2)
        admission.abort()
        assert admission.__exit__(None, None, None) is False
        with pytest.raises(RuntimeError, match="settlement commit rolled back"):
            admission.settle(**payload)
        assert not entered.wait(0.2) and not done.is_set()
        assert sends == ["first"] and _accounting_snapshot(ctx) == [[], [], [], []]
        assert len(connections) == 2 and all(conn.closed for conn in connections)

        fault = False
        result = admission.settle(**payload)
        assert entered.wait(5), errors
        charged = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        spend, usage, outbox, operations = charged
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(1)
        assert usage[0]["work_id"] == ctx.work_id and usage[0]["tokens"] == 15
        assert outbox[0]["event_id"] == f"usage:{usage[0]['usage_id']}"
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["settlement"] == payload | {"model": "test-model", "measurement": "measured", "price_version": None}
        assert receipt["root_work_id"] == str(ctx.work_id)
        third, waiting, finished, outcomes = _start_gate_waiter(ctx, third_work, "rollback-third")
        assert waiting.wait(2)
        for _ in range(2):
            admission.abort()
            assert admission.__exit__(None, None, None) is False
            with admission:
                pass
            assert admission.settle(**payload) == result
            assert not finished.wait(0.2), "stale cleanup freed waiter's gate"
            assert sends == ["first"] and _accounting_snapshot(ctx) == charged
        finish_waiter.set()
        assert done.wait(5), errors
        second.join(timeout=7)
        assert finished.wait(5), outcomes
        third.join(timeout=7)
        assert errors == [] and outcomes == ["admitted"]
        completed = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (2, 2, 2, 4)
        with _admit(ctx, prepared, request_id="rollback-owner") as replay:
            if not replay.settled:
                sends.append("first replay")
            assert replay.settle(**payload) == result
        assert sends == ["first", "second"] and _accounting_snapshot(ctx) == completed
    finally:
        fault = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        finish_waiter.set()
        if not admission.settled:
            admission.settle(**payload)
        for thread in (second, third):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=7)
                assert not thread.is_alive()
    assert all(conn.closed for conn in connections)


@pytest.mark.parametrize("fault_kind", ["rollback-before-commit", "commit-then-raise"])
def test_resolution_locks_original_identity_after_commit_failure(
    admission_context, monkeypatch, fault_kind
):
    """Resolution locks captured identities after root/lease/stop changes; fresh sends refuse."""
    ctx = admission_context
    mission = _mission(ctx, "1000")
    policy = StandingPolicy(
        policy_id=uuid4(), action_class="spend_beyond_threshold", money_limit_usd=Decimal(1000),
        decision_id=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    authority = ChangeAuthority("owner", policy.decision_id, "owner")
    ctx.projects.set_spend_budget(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, mission,
        SpendBudget("mission", Decimal(1000), Decimal(0)), authority,
    )
    ctx.projects.put_standing_policy(ctx.workspace_id, ctx.actor_id, ctx.project_id, policy, authority)
    root = _item(ctx, "original resolution root", _GENEROUS_ITEM)
    task = _item(ctx, "resolution task", _GENEROUS_ITEM)
    changed_root = _item(ctx, "changed resolution root", _GENEROUS_ITEM)
    parent = _admitting_job(ctx, work_id=root)
    changed_parent = _admitting_job(ctx, work_id=changed_root)
    job = _admitting_job(ctx, work_id=task, parent_job_id=parent)
    kw = dict(work_id=task, mission_id=mission, job_id=job, worker_id="worker-1", fence=1,
              request_id="captured-resolution")
    original = ctx.budget._jobs.transaction
    connections = []
    fault = True
    baseline = _admission_counts(ctx)

    @contextmanager
    def failing_commit(workspace_id, actor_id):
        with original(workspace_id, actor_id) as cur:
            cur.execute("SET LOCAL lock_timeout = '3s'")
            connections.append(cur.connection)
            yield cur
            if fault and fault_kind == "rollback-before-commit":
                raise RuntimeError("settlement commit failed")
        if fault:
            raise RuntimeError("settlement commit failed")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", failing_commit)
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    admission = _admit(ctx, prepared, **kw)
    sends = ["upstream"]
    locked, proceed, fresh_started, fresh_done = (threading.Event() for _ in range(4))
    results, fresh_outcomes = [], []
    resolution = fresh = None
    lock_identity = ctx.budget.lock_captured_identity

    def paused_locks(*args, **kwargs):
        lock_identity(*args, **kwargs)
        locked.set()
        assert proceed.wait(5), "resolution locks not released"

    def resolve():
        try:
            results.append(admission.settle(**payload))
        except BaseException as exc:
            results.append(exc)

    def fresh_call():
        fresh_started.set()
        try:
            with _admit(ctx, prepared, **(kw | {"request_id": "fresh-captured"})):
                sends.append("fresh")
        except ModelProxyBudgetError as exc:
            fresh_outcomes.append(exc.code)
        except BaseException as exc:
            fresh_outcomes.append(exc)
        finally:
            fresh_done.set()

    try:
        with pytest.raises(RuntimeError, match="settlement commit failed"):
            admission.settle(**payload)
        assert admission.closed and not admission.settled
        assert all(conn.closed for conn in connections)
        fault = False
        durable = _accounting_snapshot(ctx)
        before = _admission_counts(ctx)
        charged = tuple(n + d for n, d in zip(baseline, (1, 1, 1, 2)))
        assert before == (baseline if fault_kind == "rollback-before-commit" else charged)
        fresh = threading.Thread(target=fresh_call)
        fresh.start()
        assert fresh_started.wait(2) and not fresh_done.wait(0.2)
        _engage_stop(ctx)
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("SET LOCAL lock_timeout = '3s'")
            conn.execute(
                "UPDATE omp_jobs.jobs SET parent_job_id=%s, worker_id='changed', fence=fence+1,"
                " lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=%s",
                (changed_parent, job),
            )
            conn.execute("UPDATE omp_work.standing_policies SET expires_at=clock_timestamp()-interval '1 second' WHERE policy_id=%s", (policy.policy_id,))

        def forbidden(*args, **kwargs):
            pytest.fail("resolution reauthorized consumed settlement")

        monkeypatch.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden)
        monkeypatch.setattr(ctx.budget, "lock_captured_identity", paused_locks)
        resolution = threading.Thread(target=resolve)
        resolution.start()
        assert locked.wait(5), results
        # Old admission connection is closed: these locks belong to resolution only.
        assert len(connections) == 2 and connections[0].closed
        assert not connections[1].closed
        probes = [
            ("SELECT pg_advisory_xact_lock(hashtextextended('omp_jobs.operation:' || %s, 0))", (f"model_call:receipt:{admission._identity}",)),
            ("SELECT project_id FROM omp_work.projects WHERE workspace_id=%s AND project_id=%s FOR UPDATE", (ctx.workspace_id, ctx.project_id)),
            ("SELECT mission_id FROM omp_work.project_missions WHERE workspace_id=%s AND project_id=%s AND mission_id=%s FOR UPDATE", (ctx.workspace_id, ctx.project_id, mission)),
            ("SELECT budget_id FROM omp_work.spend_budgets WHERE workspace_id=%s AND project_id=%s AND mission_id=%s AND active FOR UPDATE", (ctx.workspace_id, ctx.project_id, mission)),
            ("SELECT budget_id FROM omp_work.spend_budgets WHERE workspace_id=%s AND project_id=%s AND mission_id IS NULL AND active FOR UPDATE", (ctx.workspace_id, ctx.project_id)),
            ("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"omp_work.item_budget:{ctx.workspace_id}:{root}",)),
            ("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"omp_work.task_budget:{ctx.workspace_id}:{task}",)),
            ("SELECT job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s FOR UPDATE", (ctx.workspace_id, job)),
        ]
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres"), autocommit=True) as probe:
            probe.execute("SET lock_timeout = '150ms'")
            for query, args in probes:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    with probe.transaction():
                        probe.execute(query, args)
            with probe.transaction():
                probe.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"omp_work.item_budget:{ctx.workspace_id}:{changed_root}",))
            assert not fresh_done.is_set() and _accounting_snapshot(ctx) == durable
            proceed.set()
            resolution.join(timeout=7)
            assert not resolution.is_alive() and len(results) == 1
            if fault_kind == "rollback-before-commit":
                assert isinstance(results[0], ModelProxyBudgetError), results
                assert results[0].code == "idempotency_conflict"
                assert not admission.settled and not fresh_done.wait(0.2)
                assert _accounting_snapshot(ctx) == durable
            else:
                assert isinstance(results[0], dict), results
            for query, args in probes:
                with probe.transaction():
                    probe.execute(query, args)
        if fault_kind == "rollback-before-commit":
            # Root conflict must retain the gate; restore identity through the DB,
            # then reconcile captured authority even though lease/stop changed.
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                conn.execute("SET LOCAL lock_timeout = '3s'")
                conn.execute("UPDATE omp_jobs.jobs SET parent_job_id=%s WHERE job_id=%s", (parent, job))
            results[0] = admission.settle(**payload)
        fresh.join(timeout=7)
        assert not fresh.is_alive() and fresh_outcomes == ["agent_stop_engaged"]
        completed = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == charged
        if fault_kind == "commit-then-raise":
            assert completed == durable
        spend, usage, outbox, operations = completed
        assert len(spend) == len(usage) == len(outbox) == 1
        assert sum(row["kind"] == "model_call_receipt" for row in operations) == 1
        assert spend[0]["project_id"] == ctx.project_id and spend[0]["mission_id"] == mission
        assert spend[0]["policy_id"] == policy.policy_id and spend[0]["tier"] == "tier2"
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(1)
        assert usage[0]["work_id"] == task and usage[0]["job_id_derived"] == job
        assert usage[0]["tokens"] == 15 and outbox[0]["event_id"] == f"usage:{usage[0]['usage_id']}"
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["root_work_id"] == str(root)
        assert receipt["settlement"] == payload | {"model": "test-model", "measurement": "measured", "price_version": None}
        with _admit(ctx, prepared, **kw) as replay:
            if not replay.settled:
                sends.append("replay")
            assert replay.settle(**payload) == results[0]
        assert sends == ["upstream"] and _accounting_snapshot(ctx) == completed
    finally:
        fault = False
        proceed.set()
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        monkeypatch.setattr(ctx.budget, "lock_captured_identity", lock_identity)
        if resolution is not None:
            resolution.join(timeout=7)
            assert not resolution.is_alive()
        if not admission.settled:
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                conn.execute("SET LOCAL lock_timeout = '3s'")
                conn.execute("UPDATE omp_jobs.jobs SET parent_job_id=%s WHERE job_id=%s", (parent, job))
            admission.settle(**payload)
        if fresh is not None:
            fresh.join(timeout=7)
            assert not fresh.is_alive()

    assert all(conn.closed for conn in connections)


@pytest.mark.parametrize("fault_kind", ["rollback-before-commit", "commit-then-raise"])
def test_bound_uncertainty_retains_failure_and_reconciles_once(
    admission_context, monkeypatch, fault_kind
):
    """A failed actual write cannot replace bound accounting while commit is uncertain."""
    ctx = admission_context
    other = _item(ctx, "bound uncertainty waiter")
    transaction = ctx.budget._jobs.transaction
    append = ctx.budget._projects.append_spend_cursor
    original_failure = RuntimeError("actual write failed once")
    failed = False
    fault = True

    def fail_actual_once(*args, **kwargs):
        nonlocal failed
        result = append(*args, **kwargs)
        if not failed:
            failed = True
            raise original_failure
        return result

    @contextmanager
    def uncertain_commit(workspace_id, actor_id):
        with transaction(workspace_id, actor_id) as cur:
            yield cur
            if fault and fault_kind == "rollback-before-commit":
                raise RuntimeError("bound commit uncertain")
        if fault:
            raise RuntimeError("bound commit uncertain")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", uncertain_commit)
    monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", fail_actual_once)
    admission = _admit(ctx, _prepared("2"), request_id="bound-uncertainty")
    actual = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0,
                  price_version="p1")
    bound = admission._admitted_bound_payload()
    sends = ["upstream"]
    waiter = resolution = None
    pauses = None
    outcomes = []

    def resolve():
        try:
            outcomes.append(admission._resolve())
        except BaseException as error:
            outcomes.append(error)

    try:
        with pytest.raises(RuntimeError, match="bound commit uncertain"):
            admission.settle(**actual)
        assert admission.closed and not admission.settled
        captured = admission._state.unresolved
        assert captured.failure is original_failure
        assert admission._state.failure is original_failure
        assert captured.decision == admission.decision
        assert json.loads(captured.payload) == bound | {"actual_usd": "2"}
        before = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (
            (0, 0, 0, 0) if fault_kind == "rollback-before-commit" else (1, 1, 1, 2)
        )
        waiter, waiting, finished, waiter_outcomes = _start_gate_waiter(
            ctx, other, "bound-uncertainty-waiter"
        )
        assert waiting.wait(2) and not finished.wait(0.2)
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(**actual)
        assert conflict.value.code == "idempotency_conflict"
        assert admission._state.unresolved is captured
        assert _accounting_snapshot(ctx) == before

        fault = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
        monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", append)

        def forbidden(*args, **kwargs):
            pytest.fail("bound reconciliation reauthorized")

        monkeypatch.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden)
        pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs)
        resolution = threading.Thread(target=resolve)
        resolution.start()
        assert pauses.cursor_closing.wait(5), outcomes
        assert not finished.wait(0.2)
        pauses.allow_cursor.set()
        assert pauses.committing.wait(5), outcomes
        assert not finished.wait(0.2)
        pauses.allow_commit.set()
        assert pauses.connection_closing.wait(5), outcomes
        assert not admission.settled and not finished.wait(0.2)
        pauses.release()
        resolution.join(timeout=7)
        waiter.join(timeout=7)
        assert not resolution.is_alive() and not waiter.is_alive()
        assert waiter_outcomes == ["admitted"]
        assert len(outcomes) == 1 and isinstance(outcomes[0], dict), outcomes
        assert outcomes[0]["actual_usd"] == Decimal(2)
        assert admission.settled and admission._state.failure is original_failure
        pauses.enabled["on"] = False
        durable = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        spend, usage, _, operations = durable
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(2)
        assert usage[0]["tokens"] == 150
        assert usage[0]["model"] == "test-model" and usage[0]["price_version"] == "p1"
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["settlement"] == bound | {"actual_usd": "2"}
        if fault_kind == "commit-then-raise":
            assert durable == before
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(**actual)
        assert conflict.value.code == "idempotency_conflict"
        assert admission.settle(**bound) == outcomes[0]
        with _admit(ctx, _prepared("2"), request_id="bound-uncertainty") as replay:
            if not replay.settled:
                sends.append("replay")
            assert replay.settle(**bound) == outcomes[0]
        assert sends == ["upstream"] and _accounting_snapshot(ctx) == durable
    finally:
        fault = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
        monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", append)
        if pauses is not None:
            pauses.release()
        if resolution is not None:
            resolution.join(timeout=7)
            assert not resolution.is_alive()
        if not admission.closed:
            admission.abort()
        elif not admission.settled:
            admission._resolve()
        if waiter is not None:
            waiter.join(timeout=7)
            assert not waiter.is_alive()


def test_orphan_usage_conflict_preserves_accounting_and_reachable_owner(
    admission_context
):
    """Usage lacking a call receipt conflicts unchanged until repaired and reconciled."""
    ctx = admission_context
    other = _item(ctx, "orphan waiter")
    identity = budget_module.sha256({
        "workspace_id": str(ctx.workspace_id), "work_id": str(ctx.work_id),
        "job_id": None, "request_id": "orphan-owner",
    })
    baseline = _accounting_snapshot(ctx)
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0,
                   model="test-model", measurement="measured", price_version="p1")
    record_usage(
        ctx.jobs, operation_id=f"model_call:usage:{identity}",
        workspace_id=ctx.workspace_id, actor_id=ctx.actor_id, work_id=ctx.work_id,
        job_id=None, request_id=f"model_call:request:{identity}",
        role="model_call", model="test-model", measurement="measured",
        input_tokens=10, output_tokens=5, cache_tokens=0,
        price_usd=Decimal(1), price_version="p1",
    )
    snapshot = _accounting_snapshot(ctx)
    admission = _admit(ctx, _prepared("2"), request_id="orphan-owner")
    waiter = None
    sends = ["upstream"]

    def remove_orphan():
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("SET LOCAL lock_timeout = '3s'")
            for table in reversed(_ADMISSION_TABLES):
                conn.execute(
                    f"DELETE FROM {table} WHERE workspace_id=%s",  # nosec B608 - fixed tables
                    (ctx.workspace_id,),
                )

    try:
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(**payload)
        assert conflict.value.code == "idempotency_conflict"
        assert admission.closed and not admission.settled
        assert admission._state.unresolved is not None
        assert _accounting_snapshot(ctx) == snapshot
        waiter, waiting, finished, outcomes = _start_gate_waiter(ctx, other, "orphan-waiter")
        assert waiting.wait(2) and not finished.wait(0.2)
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission._resolve()
        assert conflict.value.code == "idempotency_conflict"
        assert not finished.wait(0.2) and _accounting_snapshot(ctx) == snapshot
        remove_orphan()
        assert _accounting_snapshot(ctx) == baseline
        result = admission._resolve()
        waiter.join(timeout=7)
        assert not waiter.is_alive() and outcomes == ["admitted"]
        assert result["actual_usd"] == Decimal(1)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        durable = _accounting_snapshot(ctx)
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(**(payload | {"actual_usd": "2"}))
        assert conflict.value.code == "idempotency_conflict"
        assert admission.settle(**payload) == result
        assert sends == ["upstream"] and _accounting_snapshot(ctx) == durable
    finally:
        if not admission.closed:
            admission.abort()
            remove_orphan()
        elif not admission.settled:
            remove_orphan()
            admission._resolve()
        if waiter is not None:
            waiter.join(timeout=7)
            assert not waiter.is_alive()


def test_unmeasurable_failure_commits_bound_receipt_once(admission_context):
    """Unmeasurable receipt replay preserves separate input/output bounds once."""
    ctx = admission_context
    for _ in range(2):
        with _admit(ctx, _prepared("2", 10, 5), request_id="unmeasurable") as admission:
            with pytest.raises(ModelProxyBudgetError) as exc:
                admission.settle_unmeasurable_failure("provider disconnected")
            assert exc.value.code == "unmeasurable_failure"
            assert admission.settled and admission.closed
    assert _admission_counts(ctx) == (1, 1, 1, 2)
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            "SELECT price_usd, input_tokens, output_tokens, cache_tokens, measurement, model, price_version"
            " FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        assert cur.fetchone() == {
            "price_usd": Decimal(2), "input_tokens": 10, "output_tokens": 5,
            "cache_tokens": 0, "measurement": "estimated", "model": "test-model", "price_version": "p1",
        }
        cur.execute(
            "SELECT result FROM omp_jobs.operations"
            " WHERE workspace_id=%s AND kind='model_call_receipt'",
            (ctx.workspace_id,),
        )
        assert cur.fetchone()["result"]["settlement"] == {
            "actual_usd": "2", "input_tokens": 10, "output_tokens": 5,
            "cache_tokens": 0, "measurement": "estimated", "model": "test-model",
            "price_version": "p1",
        }


def test_lock_captured_identity_independent_transaction_probes(admission_context):
    """Independent-transaction lock probes prove all keys block until rollback."""
    ctx = admission_context
    mission_id = uuid4()
    ctx.projects.link_mission(
        ctx.workspace_id,
        ctx.actor_id,
        ctx.project_id,
        mission_id,
        "probe mission",
        "running",
    )
    ctx.projects.set_spend_budget(
        ctx.workspace_id,
        ctx.actor_id,
        ctx.project_id,
        mission_id,
        SpendBudget("mission-budget", Decimal("50000"), Decimal("50000")),
        ChangeAuthority("owner", uuid4(), "owner"),
    )

    root_work_id = _item(ctx, "probe root item", _GENEROUS_ITEM)
    task_work_id = _item(ctx, "probe task item", _GENEROUS_ITEM)
    assert root_work_id != task_work_id

    job_id = _admitting_job(ctx, work_id=task_work_id)
    receipt_id = f"probe:receipt:{uuid4()}"

    kwargs = ctx.service.config.connection_kwargs("postgres")
    holder_conn = psycopg.connect(**kwargs, autocommit=False, row_factory=dict_row)
    probe_conn = psycopg.connect(**kwargs, autocommit=True, row_factory=dict_row)

    root_item_lock_key = f"omp_work.item_budget:{ctx.workspace_id}:{root_work_id}"
    task_lock_key = f"omp_work.task_budget:{ctx.workspace_id}:{task_work_id}"

    probes = [
        (
            "receipt_operation",
            "SELECT pg_advisory_lock(hashtextextended('omp_jobs.operation:' || %s, 0))",
            (receipt_id,),
            "SELECT pg_advisory_unlock(hashtextextended('omp_jobs.operation:' || %s, 0))",
            True,
        ),
        (
            "project_row",
            "SELECT project_id FROM omp_work.projects WHERE workspace_id=%s AND project_id=%s FOR UPDATE",
            (ctx.workspace_id, ctx.project_id),
            None,
            False,
        ),
        (
            "mission_row",
            "SELECT mission_id FROM omp_work.project_missions WHERE workspace_id=%s AND project_id=%s AND mission_id=%s FOR UPDATE",
            (ctx.workspace_id, ctx.project_id, mission_id),
            None,
            False,
        ),
        (
            "mission_budget",
            "SELECT budget_id FROM omp_work.spend_budgets WHERE workspace_id=%s AND project_id=%s AND mission_id=%s AND active FOR UPDATE",
            (ctx.workspace_id, ctx.project_id, mission_id),
            None,
            False,
        ),
        (
            "project_budget",
            "SELECT budget_id FROM omp_work.spend_budgets WHERE workspace_id=%s AND project_id=%s AND mission_id IS NULL AND active FOR UPDATE",
            (ctx.workspace_id, ctx.project_id),
            None,
            False,
        ),
        (
            "root_item_advisory",
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (root_item_lock_key,),
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            True,
        ),
        (
            "task_advisory",
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (task_lock_key,),
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            True,
        ),
        (
            "job_row",
            "SELECT job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s FOR UPDATE",
            (ctx.workspace_id, job_id),
            None,
            False,
        ),
    ]

    try:
        holder_cur = holder_conn.cursor()
        lock_captured_identity(
            holder_cur,
            workspace_id=ctx.workspace_id,
            receipt_id=receipt_id,
            project_id=ctx.project_id,
            mission_id=mission_id,
            root_work_id=root_work_id,
            work_id=task_work_id,
            job_id=job_id,
            projects=ctx.projects,
        )

        probe_conn.execute("SET lock_timeout = '150ms'")
        for _name, query, args, _unlock, is_advisory in probes:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                if is_advisory:
                    probe_conn.execute(query, args)
                else:
                    with probe_conn.transaction():
                        probe_conn.execute(query, args)

        # Roll back the holder transaction: all locks are released
        holder_conn.rollback()

        # Each probe must now succeed after rollback
        for _name, query, args, unlock_query, is_advisory in probes:
            if is_advisory:
                probe_conn.execute(query, args)
                assert unlock_query is not None
                probe_conn.execute(unlock_query, args)
            else:
                with probe_conn.transaction():
                    row = probe_conn.execute(query, args).fetchone()
                    assert row is not None
    finally:
        holder_conn.close()
        probe_conn.close()


def test_lock_captured_identity_locks_captured_root_not_recomputed(admission_context):
    """Captured root key, not subsequently recomputed root, is locked."""
    ctx = admission_context
    root1 = _item(ctx, "captured root", _GENEROUS_ITEM)
    root2 = _item(ctx, "recomputed root", _GENEROUS_ITEM)
    task = _item(ctx, "task item", _GENEROUS_ITEM)
    assert root1 != root2 != task

    job_parent = _admitting_job(ctx, work_id=root2)
    job = _admitting_job(ctx, work_id=task, parent_job_id=job_parent)

    receipt_id = f"captured-root-test:{uuid4()}"
    kwargs = ctx.service.config.connection_kwargs("postgres")
    holder = psycopg.connect(**kwargs, autocommit=False, row_factory=dict_row)
    probe = psycopg.connect(**kwargs, autocommit=True)
    try:
        probe.execute("SET lock_timeout = '150ms'")
        holder_cur = holder.cursor()
        # In the database, the job's currently computed root is root2
        assert root_work_id(holder_cur, ctx.workspace_id, job) == root2

        # Invoking with captured root_work_id=root1 locks captured root1, not recomputed root2
        lock_captured_identity(
            holder_cur,
            workspace_id=ctx.workspace_id,
            receipt_id=receipt_id,
            project_id=ctx.project_id,
            root_work_id=root1,
            work_id=task,
            job_id=job,
            projects=ctx.projects,
        )

        # Captured root1 key is locked: competing probe times out
        key1 = f"omp_work.item_budget:{ctx.workspace_id}:{root1}"
        with pytest.raises(psycopg.errors.LockNotAvailable):
            probe.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key1,))

        # Recomputed root2 key is NOT locked: competing probe succeeds immediately
        key2 = f"omp_work.item_budget:{ctx.workspace_id}:{root2}"
        probe.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key2,))
        probe.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key2,))
    finally:
        holder.close()
        probe.close()


def test_lock_captured_identity_runs_after_stop_without_authorizing_spend(admission_context):
    """Lock-only helper can run after stop without authorizing spend."""
    ctx = admission_context
    root_work_id = _item(ctx, "stopped root", _GENEROUS_ITEM)
    task_work_id = _item(ctx, "stopped task", _GENEROUS_ITEM)
    job_id = _admitting_job(ctx, work_id=task_work_id)
    receipt_id = f"stopped:receipt:{uuid4()}"

    _engage_stop(ctx)

    before_counts = _admission_counts(ctx)

    # Calling admit on a stopped workspace is refused
    with pytest.raises(ModelProxyBudgetError) as exc:
        _admit(ctx, _prepared("1"), work_id=task_work_id, job_id=job_id, worker_id="worker-1", fence=1)
    assert exc.value.code == "agent_stop_engaged"
    assert _admission_counts(ctx) == before_counts

    # The lock-only helper runs and acquires all locks without authorizing spend
    kwargs = ctx.service.config.connection_kwargs("postgres")
    holder = psycopg.connect(**kwargs, autocommit=False, row_factory=dict_row)
    probe = psycopg.connect(**kwargs, autocommit=True)
    try:
        cur = holder.cursor()
        lock_captured_identity(
            cur,
            workspace_id=ctx.workspace_id,
            receipt_id=receipt_id,
            project_id=ctx.project_id,
            root_work_id=root_work_id,
            work_id=task_work_id,
            job_id=job_id,
            projects=ctx.projects,
        )
        # No spend or usage ledgers were authorized or written
        assert _admission_counts(ctx) == before_counts

        # Probe that locks are held even after stop
        probe.execute("SET lock_timeout = '150ms'")
        key = f"omp_work.item_budget:{ctx.workspace_id}:{root_work_id}"
        with pytest.raises(psycopg.errors.LockNotAvailable):
            probe.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
    finally:
        holder.close()
        probe.close()
