"""Spend and usage can share one caller-owned PostgreSQL transaction.

Also proves model-call admission: a stopped workspace, a stale lease, a wrong
worker/fence, or a job on the wrong work item is refused before any send; each
cap (project, item USD/tokens/wall-clock, task) refuses a bound that would
exceed it; an admission holds the project and item budget locks so a second
connection cannot pass them; and settling records usage and spend once while an
abort writes no ledgers and frees the locks.
"""

from __future__ import annotations

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
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.budget import item_spend
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy_budget import (
    ModelProxyBudget,
    ModelProxyBudgetError,
)
from omp_work.model_proxy_upstream import GEMINI, ModelRoute, Prepared
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row
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
        admission.settle(actual_usd="1")
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
    {"price_version": "other"},
])
def test_settlement_replay_refuses_changed_usage(admission_context, changed):
    """Receipt payload conflicts preserve original ledgers and result."""
    ctx = admission_context
    kw = dict(request_id="usage-conflict")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    with _admit(ctx, _prepared("2"), **kw) as admission:
        first = admission.settle(**payload)
    replay = _admit(ctx, _prepared("2"), **kw)
    assert replay.settled and replay.closed
    with pytest.raises(ModelProxyBudgetError) as exc:
        replay.settle(**(payload | changed))
    assert exc.value.code == "idempotency_conflict"
    assert replay.settle(**payload) == first
    assert _admission_counts(ctx) == (1, 1, 1, 2)


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


def test_settlement_failure_rolls_back_usage_receipt_and_cancellation(admission_context, monkeypatch):
    """Spend insert failure cannot leave usage, receipt, alerts or cancelled jobs behind."""
    ctx = admission_context
    work = _item(ctx, "rollback root", {**_GENEROUS_ITEM, "usd": "1"})
    job = _admitting_job(ctx, work_id=work)
    kw = dict(work_id=work, job_id=job, worker_id="worker-1", fence=1, request_id="retry-atomic")
    before = _admission_counts(ctx)
    append = ctx.budget._projects.append_spend_cursor

    def fail(*args, **kwargs):
        raise RuntimeError("spend failed")

    monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", fail)
    with pytest.raises(RuntimeError, match="spend failed"):
        with _admit(ctx, _prepared("1"), **kw) as admission:
            admission.settle(actual_usd="1")
    assert admission.closed and not admission.settled
    assert _admission_counts(ctx) == before
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert ctx.jobs.job_view(cur, ctx.workspace_id, job)["status"] == "admitted"
    monkeypatch.setattr(ctx.budget._projects, "append_spend_cursor", append)
    with _admit(ctx, _prepared("1"), **kw) as admission:
        admission.settle(actual_usd="1")
    assert _admission_counts(ctx)[:2] == (1, 1)


def test_process_gate_stays_held_until_settlement_commit(admission_context, monkeypatch):
    """Another admission cannot enter while settlement writes await transaction commit."""
    ctx = admission_context
    other = _item(ctx, "separate commit waiter")
    transaction = ctx.budget._jobs.transaction
    committing, allow_commit, waiting, finished = (threading.Event() for _ in range(4))
    outcomes = []

    @contextmanager
    def held_commit(workspace_id, actor_id):
        with transaction(workspace_id, actor_id) as cur:
            yield cur
            committing.set()
            assert allow_commit.wait(5), "commit test did not release transaction"

    monkeypatch.setattr(ctx.budget._jobs, "transaction", held_commit)
    admission = _admit(ctx, _prepared("1"))

    def settle():
        try:
            admission.settle(actual_usd="1")
            outcomes.append("committed")
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    def waiter():
        waiting.set()
        try:
            with ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, None, None, other,
                None, None, None, "commit-waiter", None, _prepared("1"),
            ):
                outcomes.append("admitted")
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        finally:
            finished.set()

    settlement = threading.Thread(target=settle)
    second = threading.Thread(target=waiter)
    try:
        settlement.start()
        assert committing.wait(3)
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        second.start()
        assert waiting.wait(2)
        assert not finished.wait(0.2), "gate released before commit"
    finally:
        allow_commit.set()
        settlement.join(timeout=7)
        if second.ident is not None:
            second.join(timeout=7)
        admission.abort()
    assert not settlement.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["admitted", "committed"]
    assert admission.settled
    assert _admission_counts(ctx) == (1, 1, 1, 2)


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
            "SELECT price_usd, input_tokens, output_tokens, cache_tokens, measurement"
            " FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        assert cur.fetchone() == {
            "price_usd": Decimal(2), "input_tokens": 10, "output_tokens": 5,
            "cache_tokens": 0, "measurement": "estimated",
        }
        cur.execute(
            "SELECT result FROM omp_jobs.operations"
            " WHERE workspace_id=%s AND kind='model_call_receipt'",
            (ctx.workspace_id,),
        )
        assert cur.fetchone()["result"]["settlement"] == {
            "actual_usd": "2", "input_tokens": 10, "output_tokens": 5,
            "cache_tokens": 0, "measurement": "estimated", "model": "test-model",
            "price_version": None,
        }
