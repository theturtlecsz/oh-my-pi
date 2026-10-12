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
import queue
import threading
import time
from contextlib import ExitStack, contextmanager
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
from omp_work.jobs.store import JobError, NativeJobStore, TransactionDeadlineExceeded
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy_budget import (
    ModelProxyBudget,
    ModelProxyBudgetError,
    lock_captured_identity,
)
from omp_work.model_proxy_upstream import (
    GEMINI,
    OLLAMA,
    ModelProxyUpstream,
    ModelRoute,
    Prepared,
    prepare,
)
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
from test_model_proxy_transactions import _transport
from test_model_proxy_upstream import (
    FakeTransport,
    body as gemini_body,
    gemini_route,
    ollama_body,
    ollama_route,
)
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
def staged_accounting(monkeypatch):
    """Stage private reconciliation tests before terminal lifecycle handling.

    These tests inspect captured authority and owner exclusion across explicit
    phase boundaries. Persistent failure closure is covered separately through
    the public settlement path; this seam never changes production behavior.
    """
    monkeypatch.setattr(
        budget_module.ModelCallAdmission, "_resolve",
        budget_module.ModelCallAdmission._resolve_accounting,
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
    def bounded_transaction(workspace_id, actor_id, **kwargs):
        with transaction(workspace_id, actor_id, **kwargs) as cur:
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


def test_usage_after_spend_fault_proves_atomic_bound_replacement(admission_context, monkeypatch):
    """SQL failure replaces spend with one bound under held locks; replay adds no effects."""
    ctx = admission_context
    mission = _mission(ctx, "1000")
    root = _item(ctx, "SQL fault root", {**_GENEROUS_ITEM, "usd": "1"})
    task = _item(ctx, "SQL fault task", _GENEROUS_ITEM)
    unrelated_item = _item(ctx, "SQL fault gate waiter", _GENEROUS_ITEM)
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
    pauses = None
    gap, release, bound_entered = (threading.Event() for _ in range(3))
    allow_bound_write, bound_returned, allow_bound_return = (threading.Event() for _ in range(3))
    gate_waiter = None
    gate_outcomes = []
    settlement_thread = waiter_thread = None
    probe = monitor = waiter = None
    settlement_errors, waiter_errors, waiter_rows = [], [], []
    sends: list[str] = []
    execute = psycopg.Cursor.execute
    persist = budget_module.ModelCallAdmission._persist
    row_probes = (
        ("SELECT project_id FROM omp_work.projects"
         " WHERE workspace_id=%s AND project_id=%s", (ctx.workspace_id, ctx.project_id)),
        ("SELECT mission_id FROM omp_work.project_missions"
         " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
         (ctx.workspace_id, ctx.project_id, mission)),
        ("SELECT budget_id FROM omp_work.spend_budgets"
         " WHERE workspace_id=%s AND project_id=%s AND active AND mission_id=%s",
         (ctx.workspace_id, ctx.project_id, mission)),
        ("SELECT budget_id FROM omp_work.spend_budgets"
         " WHERE workspace_id=%s AND project_id=%s AND active AND mission_id IS NULL",
         (ctx.workspace_id, ctx.project_id)),
        ("SELECT job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
         (ctx.workspace_id, child)),
    )

    def probe_locks(held):
        for key in (
            f"omp_jobs.operation:model_call:receipt:{admission._identity}",
            f"omp_work.item_budget:{ctx.workspace_id}:{root}",
            f"omp_work.task_budget:{ctx.workspace_id}:{task}",
        ):
            with probe.transaction():
                assert probe.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (key,),
                ).fetchone() == (not held,)
        for query, params in row_probes:
            if held:
                # Catch outside transaction: a failed NOWAIT probe must roll back
                # before the next lock is tested on this independent connection.
                with pytest.raises(LockNotAvailable):
                    with probe.transaction():
                        probe.execute(query + " FOR UPDATE NOWAIT", params)
            else:
                with probe.transaction():
                    rows = probe.execute(query + " FOR UPDATE NOWAIT", params).fetchall()
                    assert len(rows) == 1

    def pause_after_rollback(cur, query, *args, **kwargs):
        result = execute(cur, query, *args, **kwargs)
        if cur is admission._cur and query == "ROLLBACK TO SAVEPOINT model_call_settlement":
            gap.set()
            assert release.wait(5), "rollback-gap barrier was not released"
        return result

    def observe_persist(owner, cur, captured):
        if owner is admission and json.loads(captured.payload)["measurement"] == "estimated":
            bound_entered.set()
            assert release.is_set(), "bound write entered before rollback-gap probes"
            assert allow_bound_write.wait(5), "bound-write pause was not released"
            result = persist(owner, cur, captured)
            bound_returned.set()
            assert allow_bound_return.wait(5), "bound-return pause was not released"
            return result
        return persist(owner, cur, captured)

    def settle():
        try:
            # The upstream send is the caller's, before settlement observes the SQL error.
            if not admission.settled:
                sends.append("upstream")
            admission.settle(
                actual_usd="0.25", input_tokens=7, output_tokens=3,
                cache_tokens=2, measurement="measured", price_version="p1",
            )
        except BaseException as error:
            settlement_errors.append(error)

    def wait_on_project():
        try:
            with waiter.transaction():
                waiter.execute("SET LOCAL statement_timeout = '20s'")
                query, params = row_probes[0]
                waiter_rows.extend(waiter.execute(query + " FOR UPDATE", params).fetchall())
        except BaseException as error:
            waiter_errors.append(error)

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

        pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs)
        admission = _admit(
            ctx, _prepared("1", 100, 50), mission_id=mission, work_id=task,
            job_id=child, worker_id="worker-1", fence=1,
            request_id=request_id, task_ceiling_usd=Decimal("1000"),
        )
        assert admission.root_work_id == root
        assert admission._spend_record_id() == record_id
        probe = psycopg.connect(**kwargs, autocommit=True)
        monitor = psycopg.connect(**kwargs, autocommit=True)
        waiter = psycopg.connect(**kwargs, autocommit=True)
        holder_pid = admission._cur.connection.info.backend_pid
        monkeypatch.setattr(psycopg.Cursor, "execute", pause_after_rollback)
        monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", observe_persist)
        settlement_thread = threading.Thread(target=settle)
        settlement_thread.start()
        assert gap.wait(5), "settlement never reached rollback gap"
        assert not bound_entered.is_set()

        before = _admission_counts(ctx)
        assert before == (0, 0, 0, 2)
        committed_counts = (1, 1, 4, 4)

        gate_waiter, gate_waiting, gate_finished, gate_outcomes = _start_gate_waiter(
            ctx, unrelated_item, f"gate-waiter-{uuid4()}"
        )
        assert gate_waiting.wait(5), "gate waiter did not start"
        assert not gate_finished.wait(0.02), "gate released at rollback gap"

        waiter_thread = threading.Thread(target=wait_on_project)
        waiter_thread.start()
        _wait_for_blocker(monitor, holder_pid)
        probe_locks(held=True)
        assert not bound_entered.is_set()
        release.set()

        assert bound_entered.wait(5), "bound write was not entered"
        assert not gate_finished.wait(0.02), "gate released before bound write"
        allow_bound_write.set()

        assert bound_returned.wait(5), "bound write did not return"
        assert not gate_finished.wait(0.02), "gate released at bound return pause"
        allow_bound_return.set()

        assert pauses.cursor_closing.wait(5), "settlement never reached cursor close"
        assert _admission_counts(ctx) == before
        assert not gate_finished.wait(0.02), "gate released before cursor close"
        pauses.allow_cursor.set()

        assert pauses.committing.wait(5), "settlement never reached commit"
        assert _admission_counts(ctx) == before
        assert not gate_finished.wait(0.02), "gate released before commit"
        pauses.allow_commit.set()

        assert pauses.connection_closing.wait(5), "settlement never reached connection close"
        assert _admission_counts(ctx) == committed_counts
        assert not admission.settled
        assert not gate_finished.wait(0.02), "gate released before connection cleanup"
        pauses.allow_connection.set()

        settlement_thread.join(timeout=7)
        gate_waiter.join(timeout=7)
        waiter_thread.join(timeout=22)
        assert not settlement_thread.is_alive()
        assert not gate_waiter.is_alive()
        assert not waiter_thread.is_alive()
        assert gate_outcomes == ["admitted"]
        assert _admission_counts(ctx) == committed_counts
        assert waiter_errors == []
        assert waiter_rows == [(ctx.project_id,)]
        assert bound_entered.is_set()
        probe_locks(held=False)
        assert len(settlement_errors) == 1
        error = settlement_errors[0]
        assert isinstance(error, CheckViolation)
        assert error.sqlstate == "23514"
        assert error.diag.message_primary == "measured usage rejected after spend"
        detail = dict(part.split("=", 1) for part in error.diag.message_detail.split())
        assert UUID(detail["record_id"]) == record_id
        assert Decimal(detail["amount_usd"]) == Decimal("0.25")
        assert fault_name in error.diag.context
        assert admission.closed and admission.settled
        assert sends == ["upstream"]

        # Counts cannot tell a wrong bound from the admitted one, or a second effect
        # hiding beside a matching row. Compare the captured payload and identity.
        bound = {
            "actual_usd": "1", "input_tokens": 100, "output_tokens": 50,
            "cache_tokens": 0, "measurement": "estimated", "model": "test-model",
            "price_version": "p1",
        }
        durable = _accounting_snapshot(ctx)
        spend, usage, outbox, operations = durable
        assert len(spend) == len(usage) == 1
        assert spend[0]["record_id"] == record_id
        assert spend[0]["amount_usd"] == Decimal(1)
        assert spend[0]["mission_id"] == mission
        assert spend[0]["amount_usd"] != Decimal("0.25")
        assert usage[0]["request_id"] == usage_request
        assert usage[0]["work_id"] == task and usage[0]["job_id_derived"] == child
        assert usage[0]["price_usd"] == Decimal(1)
        assert usage[0]["model"] == "test-model"
        assert usage[0]["measurement"] == "estimated"
        assert usage[0]["price_version"] == "p1"
        assert usage[0]["input_tokens"] == 100
        assert usage[0]["output_tokens"] == 50
        assert usage[0]["cache_tokens"] == 0
        assert usage[0]["tokens"] == 150
        usage_payload = usage[0]["payload"]
        assert usage_payload["price_usd"] == "1"
        assert usage_payload["input_tokens"] == 100
        assert usage_payload["output_tokens"] == 50
        assert usage_payload["cache_tokens"] == 0
        assert usage_payload["measurement"] == "estimated"
        assert usage_payload["model"] == "test-model"
        assert usage_payload["price_version"] == "p1"
        ledgers = [row for row in outbox if row["kind"] == "usage_ledger"]
        assert len(ledgers) == 1
        assert ledgers[0]["event_id"] == f"usage:{usage[0]['usage_id']}"
        ledger_payload = ledgers[0]["payload"]
        assert ledger_payload["price_usd"] == "1"
        assert ledger_payload["input_tokens"] == 100
        assert ledger_payload["output_tokens"] == 50
        assert ledger_payload["cache_tokens"] == 0
        assert ledger_payload["measurement"] == "estimated"
        assert ledger_payload["model"] == "test-model"
        assert ledger_payload["price_version"] == "p1"
        receipts = [row for row in operations if row["kind"] == "model_call_receipt"]
        assert len(receipts) == 1
        assert admission._identity == identity
        assert receipts[0]["operation_id"] == f"model_call:receipt:{admission._identity}"
        receipt = receipts[0]["result"]
        captured_fingerprint = json.loads(admission._receipt_request)["fingerprint"]
        assert receipt["root_work_id"] == str(root)
        assert receipt["fingerprint"] == captured_fingerprint
        assert receipt["fingerprint"]["work_id"] == str(task)
        assert receipt["fingerprint"]["job_id"] == child
        assert receipt["fingerprint"]["worker_id"] == "worker-1"
        assert receipt["fingerprint"]["fence"] == 1
        assert receipt["fingerprint"]["request_id"] == request_id
        assert receipt["fingerprint"]["mission_id"] == str(mission)
        assert receipt["settlement"] == bound
        assert receipt["settlement"]["input_tokens"] == 100
        assert receipt["settlement"]["output_tokens"] == 50
        assert receipt["settlement"]["cache_tokens"] == 0
        for payload in (usage[0], usage_payload, ledger_payload, receipt["settlement"]):
            assert payload.get("input_tokens") != 7
            assert payload.get("output_tokens") != 3
            assert payload.get("cache_tokens") != 2
            assert payload.get("measurement") != "measured"

        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute(
                "SELECT job_id, status, cancel_reason FROM omp_jobs.jobs"
                " WHERE workspace_id=%s AND job_id = ANY(%s) ORDER BY job_id",
                (ctx.workspace_id, [parent, child]),
            )
            job_states = cur.fetchall()
            cur.execute(
                "SELECT payload FROM omp_jobs.outbox"
                " WHERE workspace_id=%s AND kind='budget_alert'"
                " AND payload->>'event'='budget.exceeded'",
                (ctx.workspace_id,),
            )
            exceeded = cur.fetchall()
        assert len(job_states) == 2
        assert {row["job_id"] for row in job_states} == {parent, child}
        assert all(
            row["status"] == "cancelled" and row["cancel_reason"] == "budget_exceeded"
            for row in job_states
        )
        assert len(exceeded) == 1
        assert exceeded[0]["payload"]["dimension"] == "usd"
        assert exceeded[0]["payload"]["work_id"] == str(root)
        assert sum(
            1 for row in outbox
            if row["kind"] == "budget_alert" and row["payload"]["event"] == "budget.exceeded"
        ) == 1

        # The measured-usage trigger stays installed across replay and is dropped in finally.
        pauses.enabled["on"] = False
        assert probe.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname=%s", (fault_name,),
        ).fetchone() is not None
        with _admit(
            ctx, _prepared("1", 100, 50), mission_id=mission, work_id=task,
            job_id=child, worker_id="worker-1", fence=1,
            request_id=request_id, task_ceiling_usd=Decimal("1000"),
        ) as replay:
            if not replay.settled:
                sends.append("replay")
            assert replay.settled
            replayed = replay.settle(**bound)
        assert replayed["status"] == "settled"
        assert replayed["actual_usd"] == Decimal(1)
        assert replayed["usage"]["usage_id"] == usage[0]["usage_id"]
        assert sends == ["upstream"]
        assert _accounting_snapshot(ctx) == durable
        assert _admission_counts(ctx) == committed_counts
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute(
                "SELECT operation_id, result FROM omp_jobs.operations"
                " WHERE workspace_id=%s AND operation_id=%s",
                (ctx.workspace_id, f"model_call:receipt:{admission._identity}"),
            )
            stable_receipt = cur.fetchone()
            cur.execute(
                "SELECT job_id, status, cancel_reason FROM omp_jobs.jobs"
                " WHERE workspace_id=%s AND job_id = ANY(%s) ORDER BY job_id",
                (ctx.workspace_id, [parent, child]),
            )
            assert cur.fetchall() == job_states
        assert stable_receipt["operation_id"] == receipts[0]["operation_id"]
        assert stable_receipt["result"] == receipt
    finally:
        if pauses is not None:
            pauses.enabled["on"] = False
            pauses.release()
        release.set()
        allow_bound_write.set()
        allow_bound_return.set()
        try:
            if settlement_thread is not None:
                settlement_thread.join(timeout=7)
                assert not settlement_thread.is_alive(), "settlement thread did not stop"
        finally:
            try:
                try:
                    with psycopg.connect(**kwargs) as conn:
                        conn.execute(sql.SQL(
                            "DROP TRIGGER IF EXISTS {trigger} ON omp_jobs.usage_events"
                        ).format(trigger=trigger))
                        conn.execute(sql.SQL("DROP FUNCTION IF EXISTS {function}()"
                                             ).format(function=function))
                finally:
                    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
                    monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", persist)
            finally:
                try:
                    if admission is not None:
                        if not admission.closed:
                            admission.abort()
                        elif not admission.settled:
                            admission._resolve()
                finally:
                    try:
                        for thread in (waiter_thread, gate_waiter):
                            if thread is not None:
                                thread.join(timeout=22)
                                assert not thread.is_alive(), "waiter thread did not stop"
                    finally:
                        for connection in (probe, monitor, waiter):
                            if connection is not None:
                                connection.close()
        with psycopg.connect(**kwargs) as conn:
            assert conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname=%s", (fault_name,),
            ).fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                " WHERE n.nspname='omp_jobs' AND p.proname=%s", (fault_name,),
            ).fetchone() is None

@pytest.mark.parametrize("fault", ["rollback-before-commit", "commit-then-raise"])
def test_failed_settlement_reconciles_durable_accounting(admission_context, staged_accounting, monkeypatch, fault):
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
    def failing_transaction(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
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
            admission._resolve_accounting()
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
        assert len(connections) == 5 and len(set(connections)) == 5
        assert all(conn.closed for conn in connections)

        bound = admission._admitted_bound_payload()
        expected_settle = bound if fault == "rollback-before-commit" else payload

        def waiting():
            with _admit(ctx, _prepared("2"), **kw) as replay:
                replays.append(replay.settled)
                outcomes.append(replay.settle(**expected_settle))
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
        result = admission._resolve_accounting()
        assert finished.wait(5)
        waiter.join(timeout=5)
        assert not waiter.is_alive()
        assert admission.closed and admission.settled
        assert outcomes == [result] and replays == [True] and sends == ["upstream"]
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        expected_usd = Decimal(2) if fault == "rollback-before-commit" else Decimal(1)
        expected_tokens = 150 if fault == "rollback-before-commit" else 15
        expected_receipt_usd = "2" if fault == "rollback-before-commit" else "1"
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute("SELECT result FROM omp_jobs.operations WHERE operation_id=%s", (f"model_call:receipt:{admission._identity}",))
            receipt = cur.fetchone()["result"]
            assert receipt["root_work_id"] == str(ctx.work_id)
            assert receipt["settlement"]["actual_usd"] == expected_receipt_usd
            cur.execute("SELECT work_id, job_id_derived, price_usd, tokens FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,))
            assert cur.fetchone() == dict(work_id=ctx.work_id, job_id_derived=job, price_usd=expected_usd, tokens=expected_tokens)
            cur.execute("SELECT project_id, amount_usd, policy_id, tier FROM omp_work.spend_records WHERE workspace_id=%s", (ctx.workspace_id,))
            assert cur.fetchone() == dict(project_id=ctx.project_id, amount_usd=expected_usd, policy_id=policy.policy_id, tier="tier2")
    finally:
        enabled = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if not admission.settled:
            admission._resolve_accounting()
        if waiter is not None:
            waiter.join(timeout=5)
            assert not waiter.is_alive()


@pytest.mark.parametrize("field", [
    "workspace_id", "kind", "request_sha256", "intent", "fingerprint",
    "root_work_id", "settlement",
])
def test_resolution_receipt_conflict_retains_consumed_owner(admission_context, field):
    """Conflicting receipt cannot confirm, overwrite accounting, or release the gate."""
    ctx = admission_context
    other = _item(ctx, "receipt conflict waiter")
    other_workspace = uuid4()
    _grant(ctx.service, other_workspace)
    _create(ctx.service, other_workspace, "foreign receipt workspace")
    admission = _admit_and_fail_settle(
        ctx, _prepared("2"), dict(actual_usd="1"),
        commit_then_raise=True, request_id="receipt-conflict",
    )
    receipt_id = f"model_call:receipt:{admission._identity}"
    stored = None
    waiter = None
    try:
        with psycopg.connect(
            **ctx.service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn:
            stored = conn.execute(
                "SELECT * FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,)
            ).fetchone()
            if field in ("workspace_id", "kind", "request_sha256"):
                value = (other_workspace if field == "workspace_id" else
                         "other_kind" if field == "kind" else "0" * 64)
                conn.execute(sql.SQL("UPDATE omp_jobs.operations SET {}=%s WHERE operation_id=%s").format(
                    sql.Identifier(field)), (value, receipt_id))
            elif field == "intent":
                request = json.loads(admission._receipt_request)
                request["fingerprint"]["request_id"] = "changed-intent"
                # Reuse operation machinery for the workspace-bound intent digest.
                conn.execute("DELETE FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,))
                ctx.jobs.run_operation(
                    conn.cursor(), receipt_id, ctx.workspace_id, "model_call_receipt",
                    request, lambda: stored["result"],
                )
            else:
                conn.execute(
                    "UPDATE omp_jobs.operations SET result=jsonb_set(result, %s, %s) WHERE operation_id=%s",
                    ([field], Jsonb("changed"), receipt_id),
                )
        corrupted = _accounting_snapshot(ctx)
        with psycopg.connect(
            **ctx.service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn:
            receipt_before = conn.execute(
                "SELECT * FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,)
            ).fetchone()
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission._resolve_accounting()
        assert conflict.value.code == "idempotency_conflict"
        assert admission.closed and not admission.settled
        admission.abort()
        assert admission.__exit__(None, None, None) is False
        assert _accounting_snapshot(ctx) == corrupted
        with psycopg.connect(
            **ctx.service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn:
            assert conn.execute(
                "SELECT * FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,)
            ).fetchone() == receipt_before
        waiter, waiting, finished, outcomes = _start_gate_waiter(ctx, other, "receipt-conflict-waiter")
        assert waiting.wait(2) and not finished.wait(0.05), "conflict released gate"
    finally:
        if stored is not None:
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                conn.execute(
                    "UPDATE omp_jobs.operations SET workspace_id=%s, kind=%s, request_sha256=%s, result=%s, created_at=%s WHERE operation_id=%s",
                    (stored["workspace_id"], stored["kind"], stored["request_sha256"],
                     Jsonb(stored["result"]), stored["created_at"], receipt_id),
                )
        if not admission.settled:
            admission._resolve_accounting()
        if waiter is not None:
            waiter.join(timeout=5)
            assert not waiter.is_alive() and outcomes == ["admitted"]
    assert _admission_counts(ctx) == (1, 1, 1, 2)


def test_resolution_cannot_redirect_captured_root(admission_context, staged_accounting, monkeypatch):
    """Reparenting a failed consumed job conflicts instead of accounting under new root locks."""
    ctx = admission_context
    other = _item(ctx, "different root")
    parent = _admitting_job(ctx, work_id=other)
    job = _admitting_job(ctx)
    original = ctx.budget._jobs.transaction
    enabled = True

    @contextmanager
    def rollback_before_commit(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
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


def _arm_cleanup_pauses(monkeypatch, jobs, *, skip: int = 0):
    """Pause cursor close, COMMIT, and connection close for one armed transaction.

    Class patches are restored by the monkeypatch fixture. Only the cursor and
    connection recorded while arming is enabled block, so a disposable count
    query can run during the pause.
    """
    remaining_skips = [skip]
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
    def paused(workspace_id, actor_id, **kwargs):
        with transaction(workspace_id, actor_id, **kwargs) as cur:
            if remaining_skips[0] > 0:
                remaining_skips[0] -= 1
            elif enabled["on"] and "cursor" not in targets:
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
def test_process_gate_stays_held_until_settlement_commit(admission_context, staged_accounting, monkeypatch, retry):
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
        pauses = _arm_cleanup_pauses(monkeypatch, ctx.budget._jobs, skip=1)
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
        assert not finished.wait(0.02), "gate released before cursor cleanup"
        pauses.allow_cursor.set()

        assert pauses.committing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        assert not finished.wait(0.02), "gate released before commit"
        pauses.allow_commit.set()

        assert pauses.connection_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        assert not admission.settled
        assert not finished.wait(0.02), "gate released before connection cleanup"
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
        assert not finished.wait(0.02), "gate released before abort cursor cleanup"
        pauses.allow_cursor.set()
        assert pauses.connection_closing.wait(5), outcomes
        assert _admission_counts(ctx) == (0, 0, 0, 0)
        assert not finished.wait(0.02), "gate released before abort connection cleanup"
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
        assert not finished.wait(0.02), "gate released before replay cursor cleanup"
        pauses.allow_cursor.set()
        assert pauses.committing.wait(5), replay_outcomes
        assert _admission_counts(ctx) == charged
        assert not finished.wait(0.02), "gate released before replay commit"
        pauses.allow_commit.set()
        assert pauses.connection_closing.wait(5), replay_outcomes
        assert _admission_counts(ctx) == charged
        assert not finished.wait(0.02), "gate released before replay connection cleanup"
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


def test_stale_owner_cleanup_cannot_release_another_gate(admission_context, monkeypatch):
    """Repeated cleanup of a finished owner cannot admit a third while a second holds the gate."""
    ctx = admission_context
    queued = threading.Event()
    wait = budget_module._ProcessGate.transition.wait
    release = budget_module._release_process_gate
    releases = []

    def observed_wait(timeout=None):
        queued.set()
        return wait(timeout)

    def observed_release(token):
        releases.append(token)
        release(token)

    monkeypatch.setattr(budget_module._ProcessGate.transition, "wait", observed_wait)
    monkeypatch.setattr(budget_module, "_release_process_gate", observed_release)
    second_work = _item(ctx, "second gate owner")
    third_work = _item(ctx, "third gate owner")
    payload = dict(actual_usd="1", input_tokens=1)
    first = _admit(ctx, _prepared("1"), request_id="first-owner")
    first_result = first.settle(**payload)
    assert first.settled and first.closed
    charged = _admission_counts(ctx)
    assert charged == (1, 1, 1, 2)

    def cleanup_first() -> None:
        first._close()
        first._owner.release()
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
        assert started.wait(2) and queued.wait(2), outcomes
        assert not entered.wait(0.2) and not finished.wait(0.2), outcomes
        cleanup_first()
        assert budget_module._ProcessGate.owner is second._owner.token
        assert releases.count(first._owner.token) == 1
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
    def failing_exit(workspace_id, actor_id, **kwargs):
        with original_transaction(workspace_id, actor_id, **kwargs) as cur:
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
    def failing_exit(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
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
    admission_context, staged_accounting, monkeypatch
):
    """Rollback blocks another send; resolved owner's cleanup cannot free its successor."""
    ctx = admission_context
    second_work = _item(ctx, "rollback waiter")
    third_work = _item(ctx, "rollback third")
    original = ctx.budget._jobs.transaction
    connections = []
    fault = True

    @contextmanager
    def rollback_commit(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
            cur.execute("SET LOCAL lock_timeout = '3s'")
            connections.append(cur.connection)
            yield cur
            if fault:
                raise RuntimeError("settlement commit rolled back")

    monkeypatch.setattr(ctx.budget._jobs, "transaction", rollback_commit)
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    admission = _admit(ctx, prepared, request_id="rollback-owner")
    bound = admission._admitted_bound_payload()
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
        assert len(connections) == 5 and all(conn.closed for conn in connections)

        fault = False
        result = admission.settle(**payload)
        assert entered.wait(5), errors
        charged = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        spend, usage, outbox, operations = charged
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(2)
        assert usage[0]["work_id"] == ctx.work_id and usage[0]["tokens"] == 150
        assert usage[0]["measurement"] == "estimated"
        assert outbox[0]["event_id"] == f"usage:{usage[0]['usage_id']}"
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["settlement"] == bound | {"actual_usd": "2"}
        assert receipt["root_work_id"] == str(ctx.work_id)
        third, waiting, finished, outcomes = _start_gate_waiter(ctx, third_work, "rollback-third")
        assert waiting.wait(2)
        for _ in range(2):
            admission.abort()
            assert admission.__exit__(None, None, None) is False
            with admission:
                pass
            assert admission.settle(**bound) == result
            assert not finished.wait(0.2), "stale cleanup freed waiter's gate"
            assert sends == ["first"] and _accounting_snapshot(ctx) == charged
        finish_waiter.set()
        assert done.wait(5), errors
        second.join(timeout=7)
        assert not second.is_alive()
        assert finished.wait(5), outcomes
        third.join(timeout=7)
        assert not third.is_alive()
        assert errors == [] and outcomes == ["admitted"]
        completed = _accounting_snapshot(ctx)
        assert _admission_counts(ctx) == (2, 2, 2, 4)
        with _admit(ctx, prepared, request_id="rollback-owner") as replay:
            if not replay.settled:
                sends.append("first replay")
            assert replay.settle(**bound) == result
        assert sends == ["first", "second"] and _accounting_snapshot(ctx) == completed
    finally:
        fault = False
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        finish_waiter.set()
        if not admission.settled:
            admission._resolve()
        for thread in (second, third):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=7)
                assert not thread.is_alive()
    assert all(conn.closed for conn in connections)


@pytest.mark.parametrize("fault_kind", ["rollback-before-commit", "commit-then-raise"])
def test_resolution_locks_original_identity_after_commit_failure(
    admission_context, staged_accounting, monkeypatch, fault_kind
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
    def failing_commit(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
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
    bound = admission._admitted_bound_payload()
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
        if fault_kind == "rollback-before-commit":
            monkeypatch.setattr(ctx.budget, "lock_captured_identity", paused_locks)
            resolution = threading.Thread(target=resolve)
            resolution.start()
            assert locked.wait(5), results
            # Old admission connection is closed: these locks belong to resolution only.
            assert len(connections) == 5 and all(conn.closed for conn in connections[:-1])
            assert not connections[-1].closed
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
                assert isinstance(results[0], ModelProxyBudgetError), results
                assert results[0].code == "idempotency_conflict"
                assert not admission.settled and not fresh_done.wait(0.2)
                assert _accounting_snapshot(ctx) == durable
                for query, args in probes:
                    with probe.transaction():
                        probe.execute(query, args)
            # Root conflict must retain the gate; restore identity through the DB,
            # then reconcile captured authority even though lease/stop changed.
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                conn.execute("SET LOCAL lock_timeout = '3s'")
                conn.execute("UPDATE omp_jobs.jobs SET parent_job_id=%s WHERE job_id=%s", (parent, job))
            results[0] = admission.settle(**payload)
        else:
            resolution = threading.Thread(target=resolve)
            resolution.start()
            resolution.join(timeout=7)
            assert not resolution.is_alive() and len(results) == 1
            assert isinstance(results[0], dict), results
            assert results[0]["actual_usd"] == Decimal(1)
            assert admission.settled
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
        expected_usd = Decimal(2) if fault_kind == "rollback-before-commit" else Decimal(1)
        expected_tokens = 150 if fault_kind == "rollback-before-commit" else 15
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == expected_usd
        assert usage[0]["work_id"] == task and usage[0]["job_id_derived"] == job
        assert usage[0]["tokens"] == expected_tokens and outbox[0]["event_id"] == f"usage:{usage[0]['usage_id']}"
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        assert receipt["root_work_id"] == str(root)
        expected_settlement = (bound | {"actual_usd": "2"}) if fault_kind == "rollback-before-commit" else (payload | {"model": "test-model", "measurement": "measured", "price_version": None})
        assert receipt["settlement"] == expected_settlement
        with _admit(ctx, prepared, **kw) as replay:
            if not replay.settled:
                sends.append("replay")
            expected_settle = bound if fault_kind == "rollback-before-commit" else payload
            assert replay.settle(**expected_settle) == results[0]
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
            admission._resolve()
        if fresh is not None:
            fresh.join(timeout=7)
            assert not fresh.is_alive()

    assert all(conn.closed for conn in connections)


@pytest.mark.parametrize("fault_kind", ["rollback-before-commit", "commit-then-raise"])
def test_bound_uncertainty_retains_failure_and_reconciles_once(
    admission_context, staged_accounting, monkeypatch, fault_kind
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
    def uncertain_commit(workspace_id, actor_id, **kwargs):
        with transaction(workspace_id, actor_id, **kwargs) as cur:
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
    admission_context, staged_accounting
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
            admission._resolve_accounting()
        assert conflict.value.code == "idempotency_conflict"
        assert not finished.wait(0.2) and _accounting_snapshot(ctx) == snapshot
        remove_orphan()
        assert _accounting_snapshot(ctx) == baseline
        bound = admission._admitted_bound_payload()
        result = admission._resolve_accounting()
        waiter.join(timeout=7)
        assert not waiter.is_alive() and outcomes == ["admitted"]
        assert result["actual_usd"] == Decimal(2)
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        durable = _accounting_snapshot(ctx)
        with pytest.raises(ModelProxyBudgetError) as conflict:
            admission.settle(**payload)
        assert conflict.value.code == "idempotency_conflict"
        assert admission.settle(**bound) == result
        assert sends == ["upstream"] and _accounting_snapshot(ctx) == durable
    finally:
        if not admission.closed:
            admission.abort()
            remove_orphan()
        elif not admission.settled:
            remove_orphan()
            admission._resolve_accounting()
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


def _admit_and_fail_settle(ctx, prepared, payload, *, commit_then_raise=False, **kw):
    """Admit a call and trigger a settlement commit failure so admission is closed and unresolved."""
    original = ctx.budget._jobs.transaction
    enabled = True

    @contextmanager
    def failing_tx(workspace_id, actor_id, **kwargs):
        with original(workspace_id, actor_id, **kwargs) as cur:
            yield cur
            if enabled and not commit_then_raise:
                raise RuntimeError("simulated commit failure")
        if enabled and commit_then_raise:
            raise RuntimeError("simulated commit failure")

    ctx.budget._jobs.transaction = failing_tx
    try:
        admission = _admit(ctx, prepared, **kw)
        def pause_resolution(self, deadline=None):
            raise RuntimeError("simulated commit failure")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(budget_module.ModelCallAdmission, "_resolve", pause_resolution)
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                admission.settle(**payload)
        assert admission.closed and not admission.settled
        return admission
    finally:
        ctx.budget._jobs.transaction = original


def _pause_resolution_fallback(ctx, monkeypatch):
    """Pause after disposed lookup, before fresh fallback acquires any database locks."""
    original = ctx.budget._jobs.transaction
    gap, resume = threading.Event(), threading.Event()
    connections = []

    @contextmanager
    def transaction(workspace_id, actor_id, **kwargs):
        if len(connections) == 1:
            assert connections[0].closed, "lookup still owns transaction"
            gap.set()
            assert resume.wait(5), "fallback barrier not released"
        with original(workspace_id, actor_id, **kwargs) as cur:
            connections.append(cur.connection)
            yield cur

    monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
    return SimpleNamespace(original=original, gap=gap, resume=resume, connections=connections)


def test_resolve_default_deadline_phases_and_absent_fallback(admission_context, monkeypatch):
    """Receipt-first resolve defaults to now+5s: <=1s lookup and >=3s fallback on absent receipt."""
    ctx = admission_context
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    admission = _admit_and_fail_settle(ctx, prepared, payload, request_id="resolve-phases-test")
    bound = admission._admitted_bound_payload()

    observed_deadlines = []
    original = ctx.budget._jobs.transaction

    @contextmanager
    def tracking_transaction(workspace_id, actor_id, **kwargs):
        deadline = kwargs.get("deadline")
        assert deadline is not None, "deadline must not be omitted"
        observed_deadlines.append(deadline)
        with original(workspace_id, actor_id, **kwargs) as cur:
            yield cur

    monkeypatch.setattr(ctx.budget._jobs, "transaction", tracking_transaction)
    try:
        start = time.monotonic()
        result = admission._resolve_accounting()
        assert admission.closed and admission.settled
        # Absent receipt charges estimated $2
        assert result["actual_usd"] == Decimal(2)
        assert len(observed_deadlines) >= 2  # lookup + fallback
        lookup_deadline = observed_deadlines[-2]
        fallback_deadline = observed_deadlines[-1]
        # Lookup is <= 1s from start
        assert lookup_deadline - start <= 1.05
        # Fallback has >= 3s reserved from start (5s total budget)
        assert fallback_deadline - start >= 4.9

        # Verify durable receipt wrote bound payload
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute(
                "SELECT result FROM omp_jobs.operations WHERE operation_id=%s",
                (f"model_call:receipt:{admission._identity}",),
            )
            receipt = cur.fetchone()["result"]
            assert receipt["settlement"] == bound | {"actual_usd": "2"}
            assert receipt["settlement"]["measurement"] == "estimated"

        # Replay preserves $2
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        with _admit(ctx, prepared, request_id="resolve-phases-test") as replay:
            assert replay.settle(**bound) == result
    finally:
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if not admission.settled:
            admission._resolve_accounting()


@pytest.mark.parametrize("change, code", [
    ("stop", "agent_stop_engaged"),
    ("worker", "job_fence_stale"),
    ("fence", "job_fence_stale"),
    ("lease", "job_fence_stale"),
    ("policy", "standing_policy_required"),
    ("project_cap", "ceiling_exceeded"),
    ("mission_cap", "ceiling_exceeded"),
    ("task_cap", "task_ceiling_exceeded"),
])
def test_resolve_fallback_uses_captured_authority(admission_context, monkeypatch, change, code):
    """Authority changed after lookup cannot refuse or redirect consumed bound settlement."""
    ctx = admission_context
    mission = _mission(ctx, "1000")
    root = _item(ctx, "captured root", _GENEROUS_ITEM)
    task = _item(ctx, "captured task", _GENEROUS_ITEM)
    parent = _admitting_job(ctx, work_id=root)
    job = _admitting_job(ctx, work_id=task, parent_job_id=parent)
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
    prepared = _prepared("2")
    kw = dict(
        mission_id=mission, work_id=task, job_id=job, worker_id="worker-1", fence=1,
        task_ceiling_usd="2", request_id="captured-fallback",
    )
    if change in ("project_cap", "mission_cap"):
        # Prior independent spend becomes exhausted when its cap drops after lookup.
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            decision = ctx.projects.authorize_spend_cursor(
                cur, ctx.workspace_id, ctx.project_id, mission, Decimal(1), datetime.now(UTC),
            )
            ctx.projects.append_spend_cursor(
                cur, ctx.workspace_id, ctx.project_id, mission, Decimal(1), decision, uuid4(),
            )
    before = _admission_counts(ctx)
    admission = _admit_and_fail_settle(ctx, prepared, dict(actual_usd="1"), **kw)
    assert admission.decision.policy_id == policy.policy_id
    pause = _pause_resolution_fallback(ctx, monkeypatch)
    original, gap, resume, connections = pause.original, pause.gap, pause.resume, pause.connections
    outcomes, errors, sends = [], [], ["upstream"]
    resolver = None

    def forbidden(*args, **kwargs):
        pytest.fail("captured settlement reauthorized stop/lease/cap/policy")

    def resolve():
        try:
            outcomes.append(admission._resolve_accounting(deadline=time.monotonic() + 10))
        except BaseException as error:
            errors.append(error)

    try:
        resolver = threading.Thread(target=resolve)
        resolver.start()
        assert gap.wait(3), "resolution never reached absent-receipt fallback"
        assert _admission_counts(ctx) == before
        if change == "stop":
            _engage_stop(ctx)
        else:
            with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
                if change in ("worker", "fence", "lease"):
                    assignments = {
                        "worker": "worker_id='changed'",
                        "fence": "fence=fence+1",
                        "lease": "lease_expires_at=clock_timestamp()-interval '1 second'",
                    }
                    conn.execute(
                        sql.SQL("UPDATE omp_jobs.jobs SET " + assignments[change] + " WHERE job_id=%s"),
                        (job,),
                    )
                elif change == "policy":
                    conn.execute(
                        "UPDATE omp_work.standing_policies SET expires_at=clock_timestamp()-interval '1 second' WHERE policy_id=%s",
                        (policy.policy_id,),
                    )
                elif change in ("project_cap", "mission_cap"):
                    conn.execute(
                        "UPDATE omp_work.spend_budgets SET ceiling_usd=1, threshold_usd=0"
                        " WHERE workspace_id=%s AND project_id=%s AND active"
                        " AND mission_id IS NOT DISTINCT FROM %s",
                        (ctx.workspace_id, ctx.project_id, mission if change == "mission_cap" else None),
                    )
        with monkeypatch.context() as guards:
            guards.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden)
            for name in ("_revalidate", "_refuse_if_stopped", "_enforce_item_cap", "_enforce_task_cap"):
                guards.setattr(budget_module, name, forbidden)
            resume.set()
            resolver.join(timeout=11)
            assert not resolver.is_alive()
            assert errors == [] and len(outcomes) == 1
        assert len(connections) == 2 and all(conn.closed for conn in connections)
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        assert admission.closed and admission.settled
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        durable = _accounting_snapshot(ctx)
        spend, usage, outbox, operations = durable
        assert len(spend) == before[0] + 1
        assert len(usage) == len(outbox) == 1
        spend = [row for row in spend if row["record_id"] == admission._spend_record_id()]
        assert len(spend) == 1
        assert spend[0]["project_id"] == ctx.project_id
        assert spend[0]["mission_id"] == mission
        assert spend[0]["policy_id"] == policy.policy_id and spend[0]["tier"] == "tier2"
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(2)
        assert usage[0]["work_id"] == task and usage[0]["job_id_derived"] == job
        assert (usage[0]["input_tokens"], usage[0]["output_tokens"], usage[0]["cache_tokens"]) == (100, 50, 0)
        assert usage[0]["measurement"] == "estimated"
        receipts = [row["result"] for row in operations if row["kind"] == "model_call_receipt"]
        assert len(receipts) == 1
        assert receipts[0]["root_work_id"] == str(root)
        assert receipts[0]["fingerprint"] == json.loads(admission._receipt_request)["fingerprint"]
        assert receipts[0]["settlement"] == admission._admitted_bound_payload() | {"actual_usd": "2"}
        with _admit(ctx, prepared, **kw) as replay:
            if not replay.settled:
                pytest.fail("captured replay would send upstream again")
            assert replay.settle(**admission._admitted_bound_payload()) == outcomes[0]
        fresh = kw | {"request_id": "changed-authority-fresh"}
        with pytest.raises(ModelProxyBudgetError) as refused:
            _admit(ctx, prepared, **fresh)
        assert refused.value.code == code
        assert _accounting_snapshot(ctx) == durable and sends == ["upstream"]
    finally:
        resume.set()
        if resolver is not None:
            resolver.join(timeout=11)
            assert not resolver.is_alive()
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_receipt_inserted_between_lookup_and_fallback(admission_context, monkeypatch):
    """Second receipt check preserves independently committed actual usage and result."""
    ctx = admission_context
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    before = _admission_counts(ctx)
    admission = _admit_and_fail_settle(ctx, prepared, payload, request_id="receipt-race")
    pause = _pause_resolution_fallback(ctx, monkeypatch)
    original, gap, resume, connections = pause.original, pause.gap, pause.resume, pause.connections
    persist = budget_module.ModelCallAdmission._persist
    outcomes, errors = [], []
    resolver = None
    sends = ["upstream"]

    def forbidden_persist(*args, **kwargs):
        pytest.fail("racing actual receipt replaced with bound")

    def resolve():
        try:
            outcomes.append(admission._resolve_accounting(deadline=time.monotonic() + 10))
        except BaseException as error:
            errors.append(error)

    try:
        resolver = threading.Thread(target=resolve)
        resolver.start()
        assert gap.wait(3)
        assert _admission_counts(ctx) == before
        # Separate transaction uses the same atomic operation writer as an independent settler.
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            receipt = persist(admission, cur, budget_module._Settlement(
                budget_module.canonical_json(payload | {
                    "measurement": "measured", "model": prepared.route.model,
                    "price_version": None,
                }), admission.decision,
            ))
        durable = _accounting_snapshot(ctx)
        monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", forbidden_persist)
        resume.set()
        resolver.join(timeout=11)
        assert not resolver.is_alive() and errors == []
        assert len(connections) == 2 and all(conn.closed for conn in connections)
        assert admission.closed and admission.settled
        assert outcomes[0]["actual_usd"] == Decimal(1)
        assert outcomes[0]["usage"] == receipt["result"]["usage"]
        assert _accounting_snapshot(ctx) == durable
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        with _admit(ctx, prepared, request_id="receipt-race") as replay:
            if not replay.settled:
                sends.append("replay")
            assert sends == ["upstream"], "racing receipt replay would resend"
            assert replay.settle(**payload) == outcomes[0]
        assert _accounting_snapshot(ctx) == durable
    finally:
        resume.set()
        if resolver is not None:
            resolver.join(timeout=11)
            assert not resolver.is_alive()
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", persist)
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_matching_receipt_preserves_actual_without_bound_equality(
    admission_context, monkeypatch
):
    """Matching receipt in lookup phase preserves actual $1 without requiring bound-payload equality."""
    ctx = admission_context
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    # Commit succeeded in DB, but client experienced error on commit exit
    admission = _admit_and_fail_settle(ctx, prepared, payload, commit_then_raise=True, request_id="resolve-match-test")

    original_lock = ctx.budget.lock_captured_identity

    def spy_lock_identity(*args, **kwargs):
        pytest.fail("matching lookup receipt reached fallback")

    monkeypatch.setattr(ctx.budget, "lock_captured_identity", spy_lock_identity)
    try:
        result = admission._resolve_accounting()
        assert admission.closed and admission.settled
        # Matching receipt preserves stored actual usage $1, even though bound was $2
        assert result["actual_usd"] == Decimal(1)

        # Replay preserves actual $1
        with _admit(ctx, prepared, request_id="resolve-match-test") as replay:
            assert replay.settle(**payload) == result
    finally:
        monkeypatch.setattr(ctx.budget, "lock_captured_identity", original_lock)
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_lookup_failure_reaches_fresh_fallback(admission_context, monkeypatch):
    """Lookup failure/timeout is disposed and reaches fresh fallback while budget remains."""
    ctx = admission_context
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    admission = _admit_and_fail_settle(ctx, prepared, payload, request_id="resolve-lookup-failure-test")
    bound = admission._admitted_bound_payload()

    original = ctx.budget._jobs.transaction
    fail_lookup = True

    def tracking_lookup(workspace_id, actor_id, **kwargs):
        nonlocal fail_lookup
        deadline = kwargs.get("deadline")
        # Phase 1 lookup has a shorter deadline (<= 1.05s from now)
        if fail_lookup and deadline is not None and deadline - time.monotonic() <= 1.05:
            fail_lookup = False
            raise psycopg.OperationalError("lookup connection dropped")
        return original(workspace_id, actor_id, **kwargs)

    monkeypatch.setattr(ctx.budget._jobs, "transaction", tracking_lookup)
    try:
        result = admission._resolve_accounting()
        assert admission.closed and admission.settled
        assert result["actual_usd"] == Decimal(2)
        with _admit(ctx, prepared, request_id="resolve-lookup-failure-test") as replay:
            assert replay.settle(**bound) == result
    finally:
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original)
        if not admission.settled:
            admission._resolve_accounting()


@pytest.mark.parametrize("durable_receipt", [False, True])
@pytest.mark.parametrize("lookup_fails", [False, True])
def test_resolve_lookup_cleanup_failure_still_uses_one_fresh_fallback(
    admission_context, monkeypatch, durable_receipt, lookup_fails
):
    """Lookup/rollback faults cannot skip fallback or duplicate durable actual accounting."""
    ctx = admission_context
    prepared = _prepared("2")
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    request_id = f"resolve-cleanup-{uuid4()}"
    before = _admission_counts(ctx)
    admission = _admit_and_fail_settle(
        ctx, prepared, payload, commit_then_raise=durable_receipt, request_id=request_id
    )
    bound = admission._admitted_bound_payload()
    durable = _accounting_snapshot(ctx)
    original_transaction = ctx.budget._jobs.transaction
    original_execute = psycopg.Cursor.execute
    original_command = psycopg.Connection._exec_command
    connections = []
    deadlines = []
    lookup_errors = []
    cleanup_errors = []

    @contextmanager
    def failing_lookup_cleanup(workspace_id, actor_id, *, deadline):
        deadlines.append(deadline)
        lookup = len(deadlines) == 1
        if not lookup:
            assert connections[0].closed, "fallback reused an undisposed lookup"
        try:
            with original_transaction(workspace_id, actor_id, deadline=deadline) as cur:
                connections.append(cur.connection)
                yield cur
        finally:
            if lookup and not lookup_fails:
                assert connections[0].closed
                cleanup_errors.append("rollback acknowledgement lost")
                raise psycopg.OperationalError(cleanup_errors[-1])

    def failing_lookup_execute(cur, query, *args, **kwargs):
        if (
            lookup_fails and connections and cur.connection is connections[0]
            and query == "SELECT 1 FROM omp_jobs.operations WHERE operation_id=%s"
        ):
            lookup_errors.append("lookup failed")
            raise psycopg.OperationalError(lookup_errors[-1])
        return original_execute(cur, query, *args, **kwargs)

    def failing_rollback(conn, command, *args, **kwargs):
        if (
            lookup_fails and connections and conn is connections[0]
            and command == b"ROLLBACK" and not cleanup_errors
        ):
            cleanup_errors.append("rollback acknowledgement lost")
            raise psycopg.OperationalError(cleanup_errors[-1])
        return (yield from original_command(conn, command, *args, **kwargs))

    monkeypatch.setattr(ctx.budget._jobs, "transaction", failing_lookup_cleanup)
    monkeypatch.setattr(psycopg.Cursor, "execute", failing_lookup_execute)
    monkeypatch.setattr(psycopg.Connection, "_exec_command", failing_rollback)
    try:
        start = time.monotonic()
        result = admission._resolve_accounting()
        assert admission.closed and admission.settled
        assert len(deadlines) == 2, "resolve must use exactly one fresh fallback"
        assert 0 < deadlines[0] - start <= 1.05
        assert deadlines[1] - deadlines[0] >= 3
        assert deadlines[1] - start <= 5.05
        assert len(set(connections)) == 2
        assert all(connection.closed for connection in connections)
        assert lookup_errors == (["lookup failed"] if lookup_fails else [])
        assert cleanup_errors == ["rollback acknowledgement lost"]

        monkeypatch.setattr(ctx.budget._jobs, "transaction", original_transaction)
        monkeypatch.setattr(psycopg.Cursor, "execute", original_execute)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", original_command)
        expected_usd = Decimal(1) if durable_receipt else Decimal(2)
        expected_tokens = 15 if durable_receipt else 150
        assert result["actual_usd"] == expected_usd
        if durable_receipt:
            assert _accounting_snapshot(ctx) == durable
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute(
                "SELECT price_usd, tokens FROM omp_jobs.usage_events WHERE workspace_id=%s",
                (ctx.workspace_id,),
            )
            assert cur.fetchone() == dict(price_usd=expected_usd, tokens=expected_tokens)
            cur.execute(
                "SELECT amount_usd FROM omp_work.spend_records WHERE workspace_id=%s",
                (ctx.workspace_id,),
            )
            assert cur.fetchone()["amount_usd"] == expected_usd
            cur.execute(
                "SELECT result FROM omp_jobs.operations WHERE operation_id=%s",
                (f"model_call:receipt:{admission._identity}",),
            )
            receipt = cur.fetchone()["result"]
            assert Decimal(receipt["settlement"]["actual_usd"]) == expected_usd
            assert receipt["settlement"]["measurement"] == (
                "measured" if durable_receipt else "estimated"
            )
        with _admit(ctx, prepared, request_id=request_id) as replay:
            assert replay.settle(**(payload if durable_receipt else bound)) == result
        assert _admission_counts(ctx) == tuple(n + d for n, d in zip(before, (1, 1, 1, 2)))
    finally:
        monkeypatch.setattr(ctx.budget._jobs, "transaction", original_transaction)
        monkeypatch.setattr(psycopg.Cursor, "execute", original_execute)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", original_command)
        if not admission.settled:
            admission._resolve_accounting()


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf")])
def test_resolve_rejects_nonfinite_deadline(admission_context, deadline):
    """_resolve rejects nonfinite deadline values without modifying unresolved state."""
    ctx = admission_context
    admission = _admit_and_fail_settle(
        ctx, _prepared("1"), dict(actual_usd="1"), request_id=f"resolve-nonfinite-{uuid4()}"
    )
    try:
        with pytest.raises(ValueError, match="finite"):
            admission._resolve_accounting(deadline=deadline)
        assert admission.closed and not admission.settled
    finally:
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_past_deadline_raises_deadline_exceeded(admission_context):
    """_resolve with expired deadline immediately raises TransactionDeadlineExceeded."""
    ctx = admission_context
    admission = _admit_and_fail_settle(
        ctx, _prepared("1"), dict(actual_usd="1"), request_id=f"resolve-timeout-{uuid4()}"
    )
    try:
        with pytest.raises(TransactionDeadlineExceeded):
            admission._resolve_accounting(deadline=time.monotonic() - 1.0)
        assert admission.closed and not admission.settled
    finally:
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_fallback_fresh_mismatch_conflicts_without_overwrite(admission_context):
    """Fresh mismatch in fallback conflicts without overwriting the stored receipt."""
    ctx = admission_context
    admission = _admit_and_fail_settle(
        ctx, _prepared("2"), dict(actual_usd="1"), request_id="resolve-mismatch-test"
    )
    receipt_id = f"model_call:receipt:{admission._identity}"

    # Seed an altered receipt row under this receipt_id with different fingerprint
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        cur.execute(
            """
            INSERT INTO omp_jobs.operations (operation_id, workspace_id, kind, request_sha256, result)
            VALUES (%s, %s, 'model_call_receipt', %s, %s)
            """,
            (
                receipt_id,
                ctx.workspace_id,
                "0" * 64,
                Jsonb({"root_work_id": str(ctx.work_id), "fingerprint": {"changed": True}, "settlement": {"actual_usd": "5"}}),
            ),
        )

    try:
        with pytest.raises(ModelProxyBudgetError) as exc:
            admission._resolve_accounting()
        assert exc.value.code == "idempotency_conflict"
        assert admission.closed and not admission.settled

        # Verify the stored row was NOT overwritten
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute(
                "SELECT result FROM omp_jobs.operations WHERE operation_id=%s",
                (receipt_id,),
            )
            row = cur.fetchone()
            assert row["result"]["fingerprint"] == {"changed": True}
    finally:
        with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            cur.execute("DELETE FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,))
        if not admission.settled:
            admission._resolve_accounting()


def test_resolve_failed_fallback_retains_owner_no_reauthorization(
    admission_context, monkeypatch
):
    """Failed fallback retains owner, does not reauthorize or loop, and blocks subsequent callers."""
    ctx = admission_context
    other = _item(ctx, "failed fallback waiter")
    prepared = _prepared("2")
    admission = _admit_and_fail_settle(
        ctx, prepared, dict(actual_usd="1"), request_id="fallback-retain-test"
    )

    def fail_persist(*args, **kwargs):
        raise RuntimeError("simulated persist failure in fallback")

    def forbidden_auth(*args, **kwargs):
        pytest.fail("fallback reauthorized spend")

    monkeypatch.setattr(ctx.budget._projects, "authorize_spend_cursor", forbidden_auth)
    monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", fail_persist)

    waiter = None
    try:
        with pytest.raises(RuntimeError, match="simulated persist failure in fallback"):
            admission._resolve_accounting()
        assert admission.closed and not admission.settled

        # Verify owner is still held and gate is retained: a second admit blocks
        waiter, waiting, finished, outcomes = _start_gate_waiter(ctx, other, "fallback-retain-waiter")
        assert waiting.wait(2) and not finished.wait(0.2)
    finally:
        monkeypatch.undo()
        if not admission.settled:
            admission._resolve_accounting()
        if waiter is not None:
            waiter.join(timeout=7)
            assert not waiter.is_alive()


@pytest.mark.parametrize("fault", [
    "rollback-before-commit", "commit-then-raise", "closed-connection",
    "connection-loss", "connection-loss-cleanup", "savepoint-rollback", "bound-write", "bound-commit",
    "bound-outer-commit", "blocked-write",
])
def test_original_settlement_fault_resolves_before_return(admission_context, monkeypatch, fault):
    """One consumed send resolves actual or estimated accounting before gate release."""
    ctx = admission_context
    transaction = ctx.budget._jobs.transaction
    persist = budget_module.ModelCallAdmission._persist
    execute = psycopg.Cursor.execute
    exec_command = psycopg.Connection._exec_command
    release = budget_module._LifecycleOwner.release
    connections, deadlines, writes, releases = [], [], [], []
    injected = RuntimeError(fault)
    bound_written = False
    admission = None
    start = None

    @contextmanager
    def observed_transaction(workspace_id, actor_id, **kwargs):
        original = "deadline_control" in kwargs
        if not original:
            assert connections[0].closed, "resolution preceded original disposal"
            assert all(conn.closed for conn in connections), "fresh cleanup overlapped"
            deadlines.append(kwargs["deadline"])
        try:
            with transaction(workspace_id, actor_id, **kwargs) as cur:
                connections.append(cur.connection)
                yield cur
                if original:
                    control = kwargs["deadline_control"]
                    assert control.deadline is not None
                    assert 0 < control.deadline - start <= 1.05
                    if fault in {"rollback-before-commit", "bound-outer-commit"}:
                        raise injected
            if original and fault == "commit-then-raise":
                raise injected
        finally:
            if original and fault == "connection-loss-cleanup":
                assert connections[0].closed
                raise RuntimeError("original cleanup acknowledgement lost")

    def faulty_persist(owner, cur, captured):
        nonlocal bound_written
        measurement = json.loads(captured.payload)["measurement"]
        writes.append((cur.connection, measurement))
        if cur.connection is connections[0]:
            assert owner._deadline_control.deadline is not None
            if measurement == "measured":
                if fault in {"connection-loss", "connection-loss-cleanup"}:
                    cur.connection.close()
                    if fault == "connection-loss-cleanup":
                        raise JobError("invalid_request", ("connection lost during write",))
                    raise injected
                if fault == "blocked-write":
                    cur.execute("SELECT pg_sleep(2)")
                if fault in {"savepoint-rollback", "bound-write", "bound-commit", "bound-outer-commit"}:
                    # Real database write precedes failure; rollback must remove it.
                    persist(owner, cur, captured)
                    raise JobError("invalid_request", ("measured write rejected",))
            elif fault == "bound-write":
                persist(owner, cur, captured)
                raise injected
        result = persist(owner, cur, captured)
        if cur.connection is connections[0] and measurement == "estimated":
            bound_written = True
        return result

    def faulty_execute(cur, query, *args, **kwargs):
        if (
            fault == "savepoint-rollback" and connections and cur.connection is connections[0]
            and query == "ROLLBACK TO SAVEPOINT model_call_settlement"
        ):
            raise injected
        return execute(cur, query, *args, **kwargs)

    def faulty_command(conn, command, *args, **kwargs):
        command_bytes = command.as_bytes(conn) if isinstance(command, sql.Composable) else command
        if (
            fault == "bound-commit" and connections and conn is connections[0]
            and command_bytes.startswith(b"RELEASE ") and bound_written
        ):
            raise injected
        return (yield from exec_command(conn, command, *args, **kwargs))

    def observed_release(owner):
        if admission is not None and owner is admission._owner:
            assert all(conn.closed for conn in connections), "gate freed before cleanup"
            assert admission.settled, "gate freed without durable confirmation"
            releases.append(_accounting_snapshot(ctx))
        return release(owner)

    monkeypatch.setattr(ctx.budget._jobs, "transaction", observed_transaction)
    monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", faulty_persist)
    monkeypatch.setattr(psycopg.Cursor, "execute", faulty_execute)
    monkeypatch.setattr(psycopg.Connection, "_exec_command", faulty_command)
    monkeypatch.setattr(budget_module._LifecycleOwner, "release", observed_release)
    sends = []
    payload = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
    try:
        admission = _admit(ctx, _prepared("2"), request_id=f"immediate-{fault}")
        sends.append("upstream")
        if fault == "closed-connection":
            admission.cursor.connection.close()
        start = time.monotonic()
        error_type = (
            ModelProxyBudgetError if fault in {
                "connection-loss-cleanup", "savepoint-rollback", "bound-write", "bound-commit", "bound-outer-commit"
            } else psycopg.OperationalError if fault == "closed-connection"
            else TransactionDeadlineExceeded if fault == "blocked-write" else RuntimeError
        )
        with pytest.raises(error_type) as error:
            admission.settle(**payload)
        if error_type is ModelProxyBudgetError:
            assert error.value.code == "invalid_request"
        elif error_type is RuntimeError:
            assert error.value is injected
        assert time.monotonic() - start < 5.1
        assert admission.closed and admission.settled
        assert all(conn.closed for conn in connections)
        assert len(connections) == (2 if fault == "commit-then-raise" else 3)
        assert 0 < deadlines[0] - start <= 2.05
        assert all(deadline <= start + 5.05 for deadline in deadlines)
        if len(deadlines) == 2:
            assert deadlines[1] - deadlines[0] >= 2.9
        if fault in {"closed-connection", "connection-loss", "connection-loss-cleanup", "savepoint-rollback", "blocked-write"}:
            assert not any(
                conn is connections[0] and measurement == "estimated"
                for conn, measurement in writes
            ), "broken original received bound write"
        durable = _accounting_snapshot(ctx)
        assert releases == [durable]
        assert _admission_counts(ctx) == (1, 1, 1, 2)
        spend, usage, _, operations = durable
        actual = fault == "commit-then-raise"
        assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(1 if actual else 2)
        assert usage[0]["tokens"] == (15 if actual else 150)
        receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
        expected = payload if actual else admission._admitted_bound_payload()
        result = admission.settle(**expected)
        assert result["usage"]["usage_id"] == usage[0]["usage_id"]
        assert receipt["settlement"]["measurement"] == ("measured" if actual else "estimated")
        # Restore injection before replay; replay never sends or charges again.
        monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
        with _admit(ctx, _prepared("2"), request_id=f"immediate-{fault}") as replay:
            if not replay.settled:
                sends.append("replay")
            assert replay.settle(**expected) == result
        assert sends == ["upstream"] and _accounting_snapshot(ctx) == durable
        admission.abort()
        assert len(releases) == 1, "stale owner released gate twice"
    finally:
        monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
        monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", persist)
        monkeypatch.setattr(psycopg.Cursor, "execute", execute)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", exec_command)
        monkeypatch.setattr(budget_module._LifecycleOwner, "release", release)
        if admission is not None:
            if not admission.closed:
                admission.abort()
            elif not admission.settled:
                admission._resolve()


def test_receipt_replay_commit_is_bounded_and_owner_reachable(admission_context, monkeypatch):
    """Blocked replay COMMIT expires, disposes original, and permits identical reconciliation."""
    ctx = admission_context
    prepared = _prepared("1")
    request_id = "bounded-replay"
    payload = dict(actual_usd="1", input_tokens=1)
    with _admit(ctx, prepared, request_id=request_id) as admission:
        first = admission.settle(**payload)
    durable = _accounting_snapshot(ctx)
    transaction = ctx.budget._jobs.transaction
    exec_command = psycopg.Connection._exec_command
    connections, controls = [], []
    waiter = None
    unrelated = _item(ctx, "bounded replay gate waiter")

    @contextmanager
    def observed_transaction(workspace_id, actor_id, **kwargs):
        if "deadline_control" in kwargs:
            controls.append(kwargs["deadline_control"])
        else:
            assert kwargs["deadline"] <= time.monotonic() + 5.0
        with transaction(workspace_id, actor_id, **kwargs) as cur:
            connections.append(cur.connection)
            yield cur

    def blocked_commit(conn, command, *args, **kwargs):
        if connections and conn is connections[0] and command == b"COMMIT":
            assert controls[0].deadline is not None
            yield from exec_command(conn, b"SELECT pg_sleep(2)")
        return (yield from exec_command(conn, command, *args, **kwargs))

    monkeypatch.setattr(ctx.budget._jobs, "transaction", observed_transaction)
    monkeypatch.setattr(psycopg.Connection, "_exec_command", blocked_commit)
    try:
        start = time.monotonic()
        with pytest.raises(TransactionDeadlineExceeded):
            _admit(ctx, prepared, request_id=request_id)
        assert time.monotonic() - start < 1.1
        assert connections[0].closed
        assert 0 < controls[0].deadline - start <= 1.05
        waiter, waiting, finished, outcomes = _start_gate_waiter(ctx, unrelated, "replay-timeout-waiter")
        assert waiting.wait(2)
        assert not finished.wait(0.02), "failed replay released unresolved gate"
        monkeypatch.setattr(psycopg.Connection, "_exec_command", exec_command)
        with _admit(ctx, prepared, request_id=request_id) as replay:
            assert replay.settled and replay.settle(**payload) == first
        assert finished.wait(5)
        waiter.join(timeout=5)
        assert not waiter.is_alive() and outcomes == ["admitted"]
        assert all(conn.closed for conn in connections)
        assert _accounting_snapshot(ctx) == durable
    finally:
        monkeypatch.setattr(psycopg.Connection, "_exec_command", exec_command)
        monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
        with _admit(ctx, prepared, request_id=request_id) as cleaned:
            assert cleaned.settled
        if waiter is not None:
            waiter.join(timeout=5)
            assert not waiter.is_alive()


@pytest.mark.parametrize("outer, lookup, fresh", [
    ("commit", "normal", "normal"),
    ("rollback", "normal", "normal"),
    ("commit", "lock", "normal"),
    ("rollback", "lock", "normal"),
    ("commit", "transport", "normal"),
    ("rollback", "transport", "normal"),
    ("rollback", "transport", "exhaust"),
    ("rollback", "lock", "fail-commit"),
])
def test_outer_transport_deadlines_keep_gate_through_resolution(
    admission_context, monkeypatch, outer, lookup, fresh,
):
    """Lost outer ACKs cannot free capacity, drop deadlines, retry, or double charge.

    Total allowance is 5s from settle, including original disposal, receipt
    lookup, at most one fresh fallback and its cleanup. Allow 300ms scheduling
    tolerance on this shared host; phase barriers consume only 20ms each.
    """
    ctx = admission_context
    unrelated = _item(ctx, "transport successor")
    transaction = ctx.budget._jobs.transaction
    command = psycopg.Connection._exec_command
    execute = psycopg.Cursor.execute
    close = psycopg.Connection.close
    phases = queue.Queue()
    stop_barriers = threading.Event()
    successor_waiting = threading.Event()
    successor_entered, release_successor = threading.Event(), threading.Event()
    finished = threading.Event()
    connections, controls, deadlines, disposed, errors, sends = [], [], [], [], [], []
    threads = []
    admission = None
    start = None
    injected = RuntimeError("fresh commit refused")
    receipt_lock = None
    snapshot_at_commit = []
    original_command_seen = threading.Event()

    def barrier(phase):
        resume = threading.Event()
        phases.put((phase, resume))
        # Bounded even if main assertion fails; finally releases all barriers.
        end = time.monotonic() + 0.15
        while not stop_barriers.is_set() and not resume.wait(0.005):
            assert time.monotonic() < end, f"unreleased {phase} barrier"

    def observed_close(conn):
        if conn in connections:
            index = connections.index(conn)
            barrier(f"cleanup-{index}")
            result = close(conn)
            disposed.append((index, time.monotonic()))
            return result
        return close(conn)

    with ExitStack() as resources:
        original_config, original_blocked, original_commit = resources.enter_context(
            _transport(ctx.service.config)
        )
        holder = resources.enter_context(psycopg.connect(
            **ctx.service.config.connection_kwargs("postgres"), autocommit=True,
        ))
        lookup_blocked = None

        @contextmanager
        def observed_transaction(workspace_id, actor_id, **kwargs):
            nonlocal lookup_blocked, receipt_lock
            index = len(connections)
            if index == 0:
                controls.append(kwargs["deadline_control"])
                store = NativeJobStore(original_config)
            else:
                assert all(conn.closed for conn in connections), "resolution before disposal"
                deadlines.append(kwargs["deadline"])
                assert kwargs["deadline"] <= start + 5.01, "total deadline reset"
                store = NativeJobStore(ctx.service.config)
                if index == 1 and lookup == "lock":
                    receipt_lock = f"model_call:receipt:{admission._identity}"
                    holder.execute(
                        "SELECT pg_advisory_lock(hashtextextended('omp_jobs.operation:' || %s, 0))",
                        (receipt_lock,),
                    )
                if index == 1 and lookup == "transport":
                    config, lookup_blocked, _ = resources.enter_context(_transport(ctx.service.config))
                    store = NativeJobStore(config)
                if index == 2 and receipt_lock is not None:
                    holder.execute(
                        "SELECT pg_advisory_unlock(hashtextextended('omp_jobs.operation:' || %s, 0))",
                        (receipt_lock,),
                    )
                    receipt_lock = None
            with store.transaction(workspace_id, actor_id, **kwargs) as cur:
                connections.append(cur.connection)
                if index == 0:
                    assert cur.connection.deadline_control is controls[0]
                else:
                    assert cur.connection.deadline_control.deadline == kwargs["deadline"]
                if index:
                    barrier(f"transaction-{index}")
                    if index == 2 and fresh == "exhaust":
                        cur.execute("SELECT pg_sleep(6)")
                yield cur
                if index == 0 and outer == "rollback":
                    raise RuntimeError("pre-commit failure after measured write")

        def suppress_outer_reply(conn, sql_command, *args, **kwargs):
            if (
                fresh == "fail-commit" and len(connections) == 3
                and conn is connections[2] and sql_command == b"COMMIT"
            ):
                assert conn.deadline_control.deadline == deadlines[1]
                raise injected
            if connections and conn is connections[0] and sql_command in (b"COMMIT", b"ROLLBACK"):
                assert controls[0].deadline is not None, "outer cleanup lost deadline"
                original_command_seen.set()
                original_blocked.set()
                # Tunnel forwards COMMIT: receipt/actual usage may be durable
                # even though client cannot receive acknowledgement.
                barrier("outer-command")
            return (yield from command(conn, sql_command, *args, **kwargs))

        def stall_lookup(cur, query, *args, **kwargs):
            if (
                lookup_blocked is not None and len(connections) > 1
                and cur.connection is connections[1]
                and query == "SELECT 1 FROM omp_jobs.operations WHERE operation_id=%s"
            ):
                lookup_blocked.set()
            return execute(cur, query, *args, **kwargs)

        def settle():
            try:
                admission.settle(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        def successor():
            try:
                successor_waiting.set()
                with ModelProxyBudget(ctx.service.config).admit(
                    ctx.workspace_id, ctx.actor_id, None, None, unrelated,
                    None, None, None, "transport-successor", None, _prepared("1"),
                ):
                    sends.append("successor")
                    successor_entered.set()
                    assert release_successor.wait(8), "successor not released"
            except BaseException as error:
                errors.append(error)

        monkeypatch.setattr(ctx.budget._jobs, "transaction", observed_transaction)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", suppress_outer_reply)
        monkeypatch.setattr(psycopg.Cursor, "execute", stall_lookup)
        monkeypatch.setattr(psycopg.Connection, "close", observed_close)
        observed_phases = []
        try:
            admission = _admit(ctx, _prepared("2"), request_id="transport-original")
            sends.append("upstream")
            waiter = threading.Thread(target=successor)
            threads.append(waiter)
            waiter.start()
            assert successor_waiting.wait(1)
            assert not successor_entered.wait(0.02)
            start = time.monotonic()
            worker = threading.Thread(target=settle)
            threads.append(worker)
            worker.start()
            end = start + 5.3
            while not finished.is_set() or not phases.empty():
                assert time.monotonic() < end, "settle exceeded 5s total plus 300ms tolerance"
                try:
                    phase, resume = phases.get(timeout=0.02)
                except queue.Empty:
                    continue
                try:
                    observed_phases.append(phase)
                    assert not successor_entered.wait(0.02), f"waiter sent during {phase}"
                    if phase == "transaction-1" and outer == "commit":
                        snapshot_at_commit.append(_accounting_snapshot(ctx))
                finally:
                    resume.set()
            worker.join(timeout=0.3)
            assert not worker.is_alive()
            assert time.monotonic() - start < 5.3
            assert original_command_seen.is_set()
            assert original_commit.is_set() is (outer == "commit")
            assert admission.closed and all(conn.closed for conn in connections)
            assert 0 < controls[0].deadline - start <= 1.05
            assert disposed[0][1] <= controls[0].deadline + 0.3
            assert deadlines[0] <= start + 2.05
            assert disposed[1][1] <= deadlines[0] + 0.3
            fallback = outer == "rollback" or lookup != "normal"
            assert len(connections) == (3 if fallback else 2), "hidden fresh retry"
            if fallback:
                assert deadlines[1] - deadlines[0] >= 2.9, "lookup consumed fallback reserve"
                assert "transaction-2" in observed_phases and "cleanup-2" in observed_phases
            assert {"outer-command", "cleanup-0", "transaction-1", "cleanup-1"} <= set(observed_phases)
            with pytest.raises((TransactionDeadlineExceeded, psycopg.OperationalError, psycopg.InterfaceError)):
                admission.cursor.execute(
                    "INSERT INTO omp_jobs.operations(operation_id, workspace_id, kind, request_sha256, result) "
                    "VALUES (%s, %s, 'late-write', %s, '{}'::jsonb)",
                    ("transport-late-write", ctx.workspace_id, "0" * 64),
                )
            if fresh != "normal":
                waiter.join(timeout=2)
                assert not waiter.is_alive()
            assert len(errors) == (1 if fresh == "normal" else 2)
            if fresh != "normal":
                assert all(isinstance(error, ModelProxyBudgetError) and error.code == "accounting_unconfirmed" for error in errors)
                assert budget_module._ProcessGate.unconfirmed is admission
                assert not admission.settled and not successor_entered.is_set()
                assert _admission_counts(ctx) == (0, 0, 0, 0)
                for thread in threads:
                    thread.join(timeout=2)
                assert all(not thread.is_alive() for thread in threads)
                return
            assert isinstance(errors[0], TransactionDeadlineExceeded)
            if fresh == "normal":
                assert errors[0].outcome_uncertain is (outer == "commit")
            if fresh != "normal":
                assert not admission.settled
                assert not successor_entered.wait(0.05), "unresolved owner freed gate"
                assert _admission_counts(ctx) == (0, 0, 0, 0)
            # Restore all faults before explicit reconciliation; no implicit retry.
            monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
            monkeypatch.setattr(psycopg.Connection, "_exec_command", command)
            monkeypatch.setattr(psycopg.Cursor, "execute", execute)
            monkeypatch.setattr(psycopg.Connection, "close", close)
            if not admission.settled:
                admission._resolve()
            assert successor_entered.wait(2)
            durable = _accounting_snapshot(ctx)
            assert _admission_counts(ctx) == (1, 1, 1, 2)
            spend, usage, _, operations = durable
            assert spend[0]["amount_usd"] == usage[0]["price_usd"] == Decimal(1 if outer == "commit" else 2)
            assert usage[0]["tokens"] == (15 if outer == "commit" else 150)
            receipt = next(row["result"] for row in operations if row["kind"] == "model_call_receipt")
            assert receipt["settlement"]["measurement"] == ("measured" if outer == "commit" else "estimated")
            if snapshot_at_commit:
                assert snapshot_at_commit == [durable], "lost ACK replaced durable actual accounting"
            # Stale abort and context exit must not release successor's gate.
            admission.abort()
            admission.__exit__(None, None, None)
            probe, waiting, probe_finished, outcomes = _start_gate_waiter(ctx, unrelated, "transport-stale-probe")
            threads.append(probe)
            assert waiting.wait(1)
            assert not probe_finished.wait(0.05), "stale cleanup released successor gate"
            release_successor.set()
            assert probe_finished.wait(2)
            assert outcomes == ["admitted"]
            with _admit(ctx, _prepared("2"), request_id="transport-original") as replay:
                assert replay.settled
                expected = dict(actual_usd="1", input_tokens=10, output_tokens=5, cache_tokens=0) if outer == "commit" else admission._admitted_bound_payload()
                replay.settle(**expected)
            for thread in threads:
                thread.join(timeout=2)
            assert all(not thread.is_alive() for thread in threads)
            assert len(errors) == 1, "waiter failed after resolution"
            assert sends == ["upstream", "successor"]
            assert _accounting_snapshot(ctx) == durable
        finally:
            stop_barriers.set()
            release_successor.set()
            if admission is not None and not admission.closed:
                admission.abort()
            for thread in threads:
                thread.join(timeout=6)
            assert all(not thread.is_alive() for thread in threads)
            monkeypatch.setattr(ctx.budget._jobs, "transaction", transaction)
            monkeypatch.setattr(psycopg.Connection, "_exec_command", command)
            monkeypatch.setattr(psycopg.Cursor, "execute", execute)
            monkeypatch.setattr(psycopg.Connection, "close", close)
            if receipt_lock is not None:
                holder.execute(
                    "SELECT pg_advisory_unlock(hashtextextended('omp_jobs.operation:' || %s, 0))",
                    (receipt_lock,),
                )
            try:
                if admission is not None:
                    if not admission.closed:
                        admission.abort()
                    elif not admission.settled and budget_module._ProcessGate.unconfirmed is None:
                        admission._resolve()
            finally:
                for thread in threads:
                    thread.join(timeout=6)
                assert all(not thread.is_alive() for thread in threads)
                with budget_module._ProcessGate.transition:
                    budget_module._ProcessGate.unconfirmed = None
                assert all(not thread.is_alive() for thread in threads), "budget test thread survived cleanup"


@pytest.mark.parametrize("fault", [
    "sql", "commit", "receipt-lock", "connect", "statement", "commit-ack", "cleanup",
])
def test_persistent_accounting_failure_closes_process_before_waking_callers(
    admission_context, monkeypatch, fault,
):
    """Consumed accounting has 5s + 300ms scheduling allowance, then CLOSED.

    Reuse transaction transport seam: assertions here cover lifecycle, atomic
    ledgers and queued callers, rather than duplicating primitive SQL tests.
    """
    ctx = admission_context
    admission = _admit(ctx, _prepared("2"), request_id=f"persistent-{fault}")
    identity, intent, bounds = admission._identity, admission._receipt_request, admission.bounds
    admission.cursor.connection.close()
    original_reason = RuntimeError("provider consumed request; usage unavailable")
    transaction = ctx.budget._jobs.transaction
    execute_command = psycopg.Connection._exec_command
    persist = budget_module.ModelCallAdmission._persist
    wait = budget_module._ProcessGate.transition.wait
    queued = threading.Event()
    queued_count = 0
    results, threads, deadlines, connections, sends = [], [], [], [], ["upstream"]
    active_transport = {}

    def observed_wait(timeout=None):
        nonlocal queued_count
        queued_count += 1
        if queued_count == 2:
            queued.set()
        return wait(timeout)

    def caller(index):
        try:
            with ModelProxyBudget(ctx.service.config).admit(
                ctx.workspace_id, ctx.actor_id, None, None, ctx.work_id,
                None, None, None, f"persistent-waiter-{index}", None, _prepared("1"),
            ):
                sends.append("unexpected")
        except BaseException as error:
            results.append(error)

    with ExitStack() as resources:
        holder = resources.enter_context(psycopg.connect(
            **ctx.service.config.connection_kwargs("postgres"), autocommit=True,
        ))
        receipt_id = f"model_call:receipt:{identity}"
        if fault == "receipt-lock":
            holder.execute(
                "SELECT pg_advisory_lock(hashtextextended('omp_jobs.operation:' || %s, 0))",
                (receipt_id,),
            )

        @contextmanager
        def failing_transaction(workspace_id, actor_id, **kwargs):
            assert all(conn.closed for conn in connections)
            deadlines.append(kwargs["deadline"])
            store = NativeJobStore(ctx.service.config)
            if fault in ("connect", "commit-ack", "cleanup"):
                config, blocked, committed = resources.enter_context(_transport(
                    ctx.service.config, stall_connect=fault == "connect",
                ))
                store = NativeJobStore(config)
            with store.transaction(workspace_id, actor_id, **kwargs) as cur:
                connections.append(cur.connection)
                if fault in ("commit-ack", "cleanup") and len(deadlines) == 2:
                    active_transport[cur.connection] = (blocked, committed)
                yield cur

        def failed_persist(self, cur, captured):
            if self is admission:
                if fault == "statement":
                    cur.execute("SELECT pg_sleep(6)")
                if fault == "cleanup":
                    raise RuntimeError("fallback write failed before stalled rollback")
                result = persist(self, cur, captured)
                if fault == "sql":
                    cur.execute("SELECT 1/0")
                return result
            return persist(self, cur, captured)

        def failed_command(conn, command, *args, **kwargs):
            if len(deadlines) == 2 and connections and conn is connections[-1]:
                if fault == "commit" and command == b"COMMIT":
                    raise RuntimeError("persistent fallback COMMIT failure")
                if conn in active_transport and command == (b"COMMIT" if fault == "commit-ack" else b"ROLLBACK"):
                    active_transport[conn][0].set()
            return (yield from execute_command(conn, command, *args, **kwargs))

        monkeypatch.setattr(ctx.budget._jobs, "transaction", failing_transaction)
        monkeypatch.setattr(budget_module.ModelCallAdmission, "_persist", failed_persist)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", failed_command)
        monkeypatch.setattr(budget_module._ProcessGate.transition, "wait", observed_wait)
        try:
            for index in range(2):
                thread = threading.Thread(target=caller, args=(index,))
                threads.append(thread)
                thread.start()
            assert queued.wait(2), "both callers must wait under gate before settlement"
            start = time.monotonic()
            with pytest.raises(ModelProxyBudgetError) as raised:
                admission.settle_unmeasurable_failure(original_reason)
            assert time.monotonic() - start < 5.3
            assert raised.value.code == "accounting_unconfirmed"
            assert raised.value.original_reason is original_reason
            assert raised.value.__cause__ is not original_reason
            for thread in threads:
                thread.join(timeout=1)
            assert all(not thread.is_alive() for thread in threads)
            assert len(results) == 2
            assert all(isinstance(error, ModelProxyBudgetError) and error.code == "accounting_unconfirmed" for error in results)
            assert sends == ["upstream"]
            assert len(deadlines) == 2, "at most one fallback; no transaction retry"
            assert deadlines[0] <= start + 1.05 and deadlines[1] <= start + 5.05
            assert all(conn.closed for conn in connections)
            assert admission.closed and not admission.settled
            assert budget_module._ProcessGate.owner is None
            retained = budget_module._ProcessGate.unconfirmed
            assert retained is admission
            assert (retained._identity, retained._receipt_request, retained.bounds) == (identity, intent, bounds)
            assert retained.max_usd == Decimal(2) and retained.max_tokens == 150
            snapshot = _accounting_snapshot(ctx)
            assert _admission_counts(ctx) == ((1, 1, 1, 2) if fault == "commit-ack" else (0, 0, 0, 0))

            def forbidden_transaction(*args, **kwargs):
                pytest.fail("CLOSED admission reached database")

            monkeypatch.setattr(NativeJobStore, "transaction", forbidden_transaction)
            monkeypatch.setattr(ctx.budget._jobs, "transaction", forbidden_transaction)
            for budget in (ctx.budget, ModelProxyBudget(ctx.service.config)):
                with pytest.raises(ModelProxyBudgetError) as refused:
                    budget.admit(
                        ctx.workspace_id, ctx.actor_id, None, None, ctx.work_id,
                        None, None, None, "closed-fresh", None, _prepared("1"),
                    )
                assert refused.value.code == "accounting_unconfirmed"
            with pytest.raises(ModelProxyBudgetError) as retry:
                admission._resolve()
            assert retry.value.code == "accounting_unconfirmed"
            with pytest.raises(ModelProxyBudgetError) as primitive_retry:
                admission._resolve_accounting()
            assert primitive_retry.value.code == "accounting_unconfirmed"
            admission.abort()
            admission.__exit__(None, None, None)
            admission._owner.release()
            assert budget_module._ProcessGate.unconfirmed is retained
            monkeypatch.undo()
            assert _accounting_snapshot(ctx) == snapshot, "late write or partial accounting"
        finally:
            for thread in threads:
                thread.join(timeout=6)
            assert all(not thread.is_alive() for thread in threads)
            monkeypatch.undo()
            # Test-only reset after bounded joins. Disposable database owns rows.
            with budget_module._ProcessGate.transition:
                budget_module._ProcessGate.unconfirmed = None
                if budget_module._ProcessGate.owner is admission._owner.token:
                    admission._owner.release()


def _proxy_call(ctx, prepared, upstream, **kw):
    return ctx.budget.call(
        ctx.workspace_id, ctx.actor_id, ctx.project_id, None, ctx.work_id,
        kw.pop("job_id", None), kw.pop("worker_id", None), kw.pop("fence", None),
        kw.pop("request_id", "orchestrated"), None, prepared, upstream, **kw,
    )


def _proxy_upstream(ctx, monkeypatch, api=GEMINI, *, response=None, error=None):
    # Synthetic key only; use real preparation and invocation, no external I/O.
    monkeypatch.setattr(type(ctx.service.config), "read_secret", lambda self, name: "synthetic-key")
    if api == GEMINI:
        prepared = prepare(gemini_route(context_tokens=1000, max_output_tokens=100, thinking_tokens=0), gemini_body())
        response = response if response is not None else b'data: {"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":12,"candidatesTokenCount":5,"cachedContentTokenCount":2}}\n'
    else:
        prepared = prepare(ollama_route(context_tokens=1000, max_output_tokens=100), ollama_body())
        response = response if response is not None else b'{"done":true,"prompt_eval_count":12,"eval_count":5,"prompt_eval_cached_count":2}\n'
    transport = FakeTransport(body=response, error=error)
    return prepared, ModelProxyUpstream(ctx.service.config, transport), transport


@pytest.mark.parametrize("state,code", [("stop", "agent_stop_engaged"), ("dead", "job_fence_stale")])
def test_call_refused_admission_never_posts(admission_context, monkeypatch, state, code):
    """Stopped workspace or dead lease prevents even a real invoke's POST."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(ctx, monkeypatch)
    job = _admitting_job(ctx, expired=state == "dead")
    if state == "stop":
        _engage_stop(ctx)
    before = _accounting_snapshot(ctx)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _proxy_call(ctx, prepared, upstream, job_id=job, worker_id="worker-1", fence=1)
    assert exc.value.code == code
    assert transport.calls == []
    assert _accounting_snapshot(ctx) == before


@pytest.mark.parametrize("api", [GEMINI, OLLAMA])
def test_call_measured_receipt_replays_without_send(admission_context, monkeypatch, api):
    """Complete measured usage settles under locks; stopped replay sends nothing."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(ctx, monkeypatch, api)
    post = transport.post

    def locked_post(*args, **kwargs):
        assert budget_module._ProcessGate.owner is not None
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            with pytest.raises(LockNotAvailable):
                conn.execute(
                    "SELECT 1 FROM omp_work.projects WHERE project_id=%s FOR UPDATE NOWAIT",
                    (ctx.project_id,),
                )
        return post(*args, **kwargs)

    monkeypatch.setattr(transport, "post", locked_post)
    receipt = _proxy_call(ctx, prepared, upstream)
    expected_usd = (prepared.route.input_usd * 10 + prepared.route.output_usd * 5 + prepared.route.cache_usd * 2) / Decimal(1_000_000)
    assert receipt["status"] == "settled" and receipt["actual_usd"] == expected_usd
    snapshot = _accounting_snapshot(ctx)
    usage = snapshot[1][0]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cache_tokens"], usage["measurement"], usage["price_version"]) == (10, 5, 2, "measured", prepared.route.price_version)
    assert snapshot[0][0]["amount_usd"] == expected_usd
    with ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id) as cur:
        assert item_spend(cur, ctx.workspace_id, ctx.work_id).tokens == 15
    _engage_stop(ctx)
    assert _proxy_call(ctx, prepared, upstream) == receipt
    assert len(transport.calls) == 1
    assert _accounting_snapshot(ctx) == snapshot


@pytest.mark.parametrize("changed", [
    {"input_tokens": 0, "output_tokens": None, "cache_tokens": None},
    {"input_tokens": None}, {"cache_tokens": None},
    {"input_tokens": True}, {"output_tokens": -1}, {"cache_tokens": 1.5},
    {"usd": Decimal("NaN")}, {"usd": Decimal("Infinity")},
    {"usd": Decimal("-1")}, {"usd": "0"},
    {"price_version": "foreign"}, {"usd": Decimal("0")},
    {"input_tokens": 999, "cache_tokens": 2}, {"output_tokens": 101},
])
def test_call_invalid_result_charges_estimate(admission_context, monkeypatch, changed):
    """Partial, malformed, mispriced or over-bound accounting cannot free capacity."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(ctx, monkeypatch)
    invoke = upstream.invoke

    def invalid_result(bounds):
        result = replace(invoke(bounds), **changed)
        if "usd" not in changed and all(type(n) is int and n >= 0 for n in (result.input_tokens, result.output_tokens, result.cache_tokens)):
            result = replace(result, usd=(
                bounds.route.input_usd * result.input_tokens
                + bounds.route.output_usd * result.output_tokens
                + bounds.route.cache_usd * result.cache_tokens
            ) / Decimal(1_000_000))
        return result

    monkeypatch.setattr(upstream, "invoke", invalid_result)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _proxy_call(ctx, prepared, upstream)
    assert exc.value.code == "unmeasurable_failure"
    _assert_proxy_bound(ctx, prepared)
    snapshot = _accounting_snapshot(ctx)
    replay = _proxy_call(ctx, prepared, upstream)
    assert replay["actual_usd"] == prepared.max_usd
    assert len(transport.calls) == 1
    assert _accounting_snapshot(ctx) == snapshot


def _assert_proxy_bound(ctx, prepared):
    snapshot = _accounting_snapshot(ctx)
    usage = snapshot[1][0]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cache_tokens"], usage["measurement"]) == (prepared.max_input_tokens, prepared.max_output_tokens, 0, "estimated")
    assert usage["price_usd"] == snapshot[0][0]["amount_usd"] == prepared.max_usd
    assert _admission_counts(ctx) == (1, 1, 1, 2)


@pytest.mark.parametrize("failure", ["transport", "missing_usage", "unexpected", "exit", "missing_settlement"])
def test_call_possible_send_failure_charges_estimate(admission_context, monkeypatch, failure):
    """Transport failure, unmeasured response or interrupted settlement charges once."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(
        ctx, monkeypatch,
        response=b'data: {"candidates":[{"finishReason":"STOP"}]}\n' if failure == "missing_usage" else None,
        error=OSError("connection lost") if failure == "transport" else None,
    )
    invoke = upstream.invoke

    def interrupted(bounds):
        invoke(bounds)
        raise SystemExit() if failure == "exit" else RuntimeError("unexpected")

    if failure in ("unexpected", "exit"):
        monkeypatch.setattr(upstream, "invoke", interrupted)
    if failure == "missing_settlement":
        settle = budget_module.ModelCallAdmission.settle

        def omit_measured(self, **kw):
            if kw.get("measurement", "measured") == "measured":
                return None
            return settle(self, **kw)

        monkeypatch.setattr(budget_module.ModelCallAdmission, "settle", omit_measured)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _proxy_call(ctx, prepared, upstream)
    assert exc.value.code == "unmeasurable_failure"
    assert len(transport.calls) == 1
    _assert_proxy_bound(ctx, prepared)


def test_call_presend_refusal_charges_nothing(admission_context, monkeypatch):
    """Missing synthetic key proves no send; admission aborts without accounting."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(ctx, monkeypatch)
    monkeypatch.setattr(type(ctx.service.config), "read_secret", lambda self, name: None)
    with pytest.raises(ModelProxyBudgetError) as exc:
        _proxy_call(ctx, prepared, upstream)
    assert exc.value.code == "upstream_refused"
    assert transport.calls == []
    assert _admission_counts(ctx) == (0, 0, 0, 0)
    monkeypatch.setattr(type(ctx.service.config), "read_secret", lambda self, name: "synthetic-key")
    assert _proxy_call(ctx, prepared, upstream)["status"] == "settled"
    assert len(transport.calls) == 1


def test_call_server_priced_usage_above_usd_bound_charges_estimate(admission_context, monkeypatch):
    """Correct server price still cannot exceed admitted USD capacity."""
    ctx = admission_context
    prepared, upstream, transport = _proxy_upstream(ctx, monkeypatch)
    prepared = replace(prepared, max_usd=Decimal("0.000001"))
    with pytest.raises(ModelProxyBudgetError) as exc:
        _proxy_call(ctx, prepared, upstream)
    assert exc.value.code == "unmeasurable_failure"
    assert len(transport.calls) == 1
    _assert_proxy_bound(ctx, prepared)
