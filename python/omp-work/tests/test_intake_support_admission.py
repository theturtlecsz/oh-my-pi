"""Grant check and O8 judge usage (OMP-604-s05).

Proves the admission contract for one research-route judge call and the O8
accounting of its usage:

- a mission with no budget at all is ``model_grant_required`` and writes no
  ``usage_events`` row; an unknown mission is refused the same way;
- a budget whose ceiling is above the recorded spend admits the research route
  model and reports ``remaining_usd`` as ``ceiling - spent`` under the tightest
  present budget;
- a budget already spent out is ``budget_exceeded``;
- a measured reply stores its input/output counts with a zero cache and no
  price; a tokenless reply stores null counts and price, never zero; a mixed
  pair is ``ValueError`` and writes nothing;
- the usage row is committed by ``settle_judge_usage`` itself, so it survives a
  rollback of the caller's transaction.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.intake_support_admission import (
    Admission,
    IntakeAdmissionError,
    resolve_admission,
    settle_judge_usage,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.routing.policy import load_policy
from omp_work.routing.router import route_stage
from omp_work.spend_budget import SpendBudget
from omp_work.v1.canonical import sha256
from omp_work.v1.store import PostgresWorkStore
from psycopg.rows import dict_row
from test_workflow_service import OWNER, _create, _grant

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_RESEARCH_MODEL = "deepseek-v4.1-flash:cloud"


@pytest.fixture
def admission(service):
    """A fresh workspace, project and migrated jobs substrate for one test."""
    apply_jobs_migrations(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "intake judge target")
    projects = PostgresWorkStore(service.config)
    project_id = projects.ensure_project(
        workspace_id, OWNER, f"judge-{uuid4().hex[:8]}", "Judge project", "surface"
    )
    return SimpleNamespace(
        service=service,
        workspace_id=workspace_id,
        actor_id=OWNER,
        item=item,
        work_id=UUID(item["work_id"]),
        projects=projects,
        jobs=NativeJobStore(service.config),
        project_id=project_id,
    )


def _mission(ctx) -> UUID:
    mission_id = uuid4()
    ctx.projects.link_mission(
        ctx.workspace_id,
        ctx.actor_id,
        ctx.project_id,
        mission_id,
        "Judge mission",
        "draft",
    )
    return mission_id


def _seed_budget(
    ctx,
    mission_id: UUID,
    *,
    project: str | None = None,
    mission: str | None = None,
) -> None:
    """Seed the active budgets with the ProjectStoreMixin._insert_budget INSERT."""
    with ctx.projects._transaction(ctx.workspace_id, ctx.actor_id) as cur:
        if project is not None:
            ctx.projects._insert_budget(
                cur,
                ctx.workspace_id,
                ctx.project_id,
                None,
                SpendBudget("project", Decimal(project), Decimal(project)),
                uuid4(),
            )
        if mission is not None:
            ctx.projects._insert_budget(
                cur,
                ctx.workspace_id,
                ctx.project_id,
                mission_id,
                SpendBudget(str(mission_id), Decimal(mission), Decimal(mission)),
                uuid4(),
            )


def _spend(ctx, amount: str, mission_id: UUID | None = None) -> None:
    ctx.projects.record_spend(
        ctx.workspace_id,
        ctx.actor_id,
        ctx.project_id,
        mission_id,
        Decimal(amount),
        datetime.now(UTC),
    )


def _admit(ctx, mission_id: UUID) -> Admission:
    with ctx.projects._transaction(ctx.workspace_id, ctx.actor_id) as cur:
        return resolve_admission(ctx.projects, cur, ctx.workspace_id, mission_id)


def _connect(ctx):
    return psycopg.connect(
        **ctx.service.config.connection_kwargs("postgres"),
        row_factory=dict_row,
        autocommit=True,
    )


def _usage_count(ctx) -> int:
    with _connect(ctx) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchone()
    return int(row["n"])


def _usage_row(ctx, usage_id: str) -> dict[str, object] | None:
    with _connect(ctx) as conn:
        return conn.execute(
            "SELECT * FROM omp_jobs.usage_events WHERE usage_id=%s",
            (usage_id,),
        ).fetchone()


def _settle(ctx, mission_id: UUID, *, input_tokens, output_tokens) -> str:
    return settle_judge_usage(
        ctx.jobs,
        operation_id=f"op-{uuid4()}",
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        mission_id=mission_id,
        model_id=_RESEARCH_MODEL,
        request_id=f"req-{uuid4()}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_no_budget_requires_grant_and_writes_no_usage(admission) -> None:
    """A mission with no budget is model_grant_required and writes no usage row."""
    ctx = admission
    mission_id = _mission(ctx)
    with pytest.raises(IntakeAdmissionError) as exc:
        _admit(ctx, mission_id)
    assert exc.value.code == "model_grant_required"
    assert _usage_count(ctx) == 0


def test_unknown_mission_requires_grant(admission) -> None:
    """A mission that project_missions does not know is model_grant_required."""
    ctx = admission
    _seed_budget(ctx, uuid4(), project="100")
    with pytest.raises(IntakeAdmissionError) as exc:
        _admit(ctx, uuid4())
    assert exc.value.code == "model_grant_required"


def test_ceiling_above_spend_admits_model_and_headroom(admission) -> None:
    """Headroom under the tightest present budget is ceiling - spent."""
    ctx = admission
    mission_id = _mission(ctx)
    _seed_budget(ctx, mission_id, project="100", mission="30")
    _spend(ctx, "12.50", mission_id)

    granted = _admit(ctx, mission_id)
    route = route_stage(load_policy(), "research")
    assert route.model == _RESEARCH_MODEL
    assert granted.model_id == _RESEARCH_MODEL
    # Project spend includes the mission row (100-12.5); mission is 30-12.5.
    assert granted.remaining_usd == Decimal("17.50")
    assert granted.grant_id == sha256(
        {
            "workspace_id": str(ctx.workspace_id),
            "mission_id": str(mission_id),
            "rule": route.rule,
            "model": route.model,
        }
    )


def test_spent_out_budget_is_exceeded(admission) -> None:
    """A budget whose spend reaches the ceiling is budget_exceeded."""
    ctx = admission
    mission_id = _mission(ctx)
    _seed_budget(ctx, mission_id, mission="5")
    _spend(ctx, "5", mission_id)
    with pytest.raises(IntakeAdmissionError) as exc:
        _admit(ctx, mission_id)
    assert exc.value.code == "budget_exceeded"


def test_measured_settle_stores_counts(admission) -> None:
    """A measured reply stores its counts, a zero cache and no price."""
    ctx = admission
    mission_id = _mission(ctx)
    usage_id = _settle(ctx, mission_id, input_tokens=1200, output_tokens=300)

    row = _usage_row(ctx, usage_id)
    assert row is not None
    assert row["role_derived"] == "intake_support_judge"
    assert UUID(str(row["work_id"])) == mission_id
    assert row["job_id_derived"] is None
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 300
    assert row["cache_tokens"] == 0
    assert row["measurement"] == "measured"
    assert row["price_usd"] is None
    assert row["tokens"] == 1500


def test_unknown_settle_stores_nulls_not_zero(admission) -> None:
    """A tokenless reply stores null input/output/cache/price, never zero."""
    ctx = admission
    mission_id = _mission(ctx)
    usage_id = _settle(ctx, mission_id, input_tokens=None, output_tokens=None)

    row = _usage_row(ctx, usage_id)
    assert row is not None
    assert row["measurement"] == "unknown"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cache_tokens"] is None
    assert row["price_usd"] is None
    assert row["tokens"] is None


def test_mixed_counts_refuse_and_write_nothing(admission) -> None:
    """One count without the other is ValueError and writes no usage row."""
    ctx = admission
    mission_id = _mission(ctx)
    before = _usage_count(ctx)
    with pytest.raises(ValueError):
        _settle(ctx, mission_id, input_tokens=1, output_tokens=None)
    with pytest.raises(ValueError):
        _settle(ctx, mission_id, input_tokens=None, output_tokens=1)
    assert _usage_count(ctx) == before


def test_usage_survives_caller_rollback(admission) -> None:
    """Settle commits on its own, so the row outlives a rolled-back caller."""
    ctx = admission
    mission_id = _mission(ctx)
    usage_id: str | None = None
    with (
        pytest.raises(RuntimeError),
        ctx.jobs.transaction(ctx.workspace_id, ctx.actor_id),
    ):
        usage_id = _settle(ctx, mission_id, input_tokens=7, output_tokens=3)
        raise RuntimeError("caller aborts")
    assert usage_id is not None
    row = _usage_row(ctx, usage_id)
    assert row is not None
    assert row["input_tokens"] == 7
    assert row["output_tokens"] == 3
