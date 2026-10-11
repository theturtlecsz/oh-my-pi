"""Spend and usage can share one caller-owned PostgreSQL transaction."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.v1.store import PostgresWorkStore
from psycopg.errors import CheckViolation
from test_agent_stop_store import _envelope
from test_item_budget_alerts import _seed_budget
from test_workflow_service import OWNER, _create, _grant

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
NOW = datetime(2026, 10, 11, tzinfo=UTC)


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
