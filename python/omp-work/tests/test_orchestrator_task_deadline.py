"""OMP-595-s07-s01: the task deadline is the item tree's exact creation plus its explicit limit.

PostgreSQL integration. ``operations._task_deadline`` and ``operations._sandbox``
read a real job tree and published budget; nothing here echoes a constant.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.orchestrator import operations
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import OrchestratorHandler, submit
from orchestrator_e2e_support import (
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER  # noqa: F401  (fixture module pairing)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

PUBLISHED_SECONDS = 60  # seed_published_budget's wall_clock_seconds


def _admin(service: Any) -> psycopg.Connection:
    return psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    )


def _open(service: Any, tmp_path: Path, monkeypatch: Any, *, budget: bool) -> SimpleNamespace:
    """A mission whose implement job is admitted; optionally with a published item budget."""
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    world = open_e2e_world(
        service,
        tmp_path,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )
    worker = open_worker(world)
    tick_until(
        worker,
        world,
        lambda v: any(s.get("stage") == "grant" and s.get("kind") == "advance" for s in v["steps"]),
    )
    if not budget:
        # Intake publication requires a budget; drop the receipt after the mission advanced.
        with _admin(service) as conn:
            conn.execute(
                "ALTER TABLE omp_evidence.receipts DISABLE TRIGGER USER"
            )
            try:
                conn.execute(
                    "DELETE FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s"
                    " AND kind='intake_publication'",
                    (world.workspace_id, work_id),
                )
            finally:
                conn.execute("ALTER TABLE omp_evidence.receipts ENABLE TRIGGER USER")
    with _admin(service) as conn:
        rows = conn.execute(
            "UPDATE omp_jobs.jobs SET status='admitted', worker_id=%s, fence=1,"
            " lease_expires_at = clock_timestamp() + interval '1 hour'"
            " WHERE job_id LIKE %s AND status='backlog' RETURNING job_id",
            (worker.worker_id, f"orch:{world.mission_id}:%"),
        ).fetchall()
    assert len(rows) == 1
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return SimpleNamespace(
        service=service,
        world=world,
        handler=handler,
        job_id=str(rows[0]["job_id"]),
        claim=orchestrator.LeaseClaim(job_id=str(rows[0]["job_id"]), worker_id=worker.worker_id, fence=1),
    )


def _ctx(ready: SimpleNamespace, controls: dict[str, Any], *, alive: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        config=ready.world.config,
        workspace_id=ready.world.workspace_id,
        principal=ready.handler.principal,
        lease=ready.claim,
        request={"controls": controls},
        lease_expires_at=lambda: datetime.now(UTC) if alive else None,
    )


def _created_at(ready: SimpleNamespace, microseconds: int | None = None) -> datetime:
    """Oldest tree creation; optionally rewritten to a fractional-second instant."""
    with _admin(ready.service) as conn:
        if microseconds is not None:
            conn.execute(
                "UPDATE omp_jobs.jobs SET created_at ="
                " date_trunc('second', created_at) + make_interval(secs => %s)"
                " WHERE work_id = (SELECT work_id FROM omp_jobs.jobs WHERE job_id=%s)",
                (microseconds / 1_000_000, ready.job_id),
            )
        row = conn.execute(
            "SELECT MIN(created_at) AS oldest FROM omp_jobs.jobs WHERE work_id ="
            " (SELECT work_id FROM omp_jobs.jobs WHERE job_id=%s)",
            (ready.job_id,),
        ).fetchone()
    return row["oldest"]


def test_explicit_limit_without_budget_is_exact_creation_plus_limit(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """An absent budget with control 7200 gives creation+7200 — never expired, never 3600."""
    ready = _open(service, tmp_path, monkeypatch, budget=False)
    created = _created_at(ready, 123_457)
    assert created.microsecond == 123_457
    ctx = _ctx(ready, {"wall_clock_seconds": 7200})
    first = operations._task_deadline(ctx)
    assert first == created + timedelta(seconds=7200)
    # Repeated calculation (a renewal re-enters here) cannot move the deadline.
    with _admin(service) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() + interval '2 hours'"
            " WHERE job_id=%s",
            (ready.job_id,),
        )
    assert operations._task_deadline(ctx) == first == operations._task_deadline(ctx)


def test_published_and_control_limits_take_the_minimum(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    ready = _open(service, tmp_path, monkeypatch, budget=True)
    created = _created_at(ready, 500_001)
    assert operations._task_deadline(_ctx(ready, {})) == created + timedelta(seconds=PUBLISHED_SECONDS)
    assert operations._task_deadline(_ctx(ready, {"wall_clock_seconds": 40})) == created + timedelta(seconds=40)
    # A looser control never widens the published budget.
    assert operations._task_deadline(_ctx(ready, {"wall_clock_seconds": 7200})) == created + timedelta(
        seconds=PUBLISHED_SECONDS
    )
    # Non-positive, boolean and non-integer controls are not limits.
    for bad in (0, -5, True, "40", 1.5):
        assert operations._task_deadline(_ctx(ready, {"wall_clock_seconds": bad})) == created + timedelta(
            seconds=PUBLISHED_SECONDS
        )


def test_elapsed_limit_refuses_the_sandbox(service: Any, tmp_path: Path, monkeypatch: Any) -> None:
    ready = _open(service, tmp_path, monkeypatch, budget=True)
    with _admin(service) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET created_at = clock_timestamp() - interval '2 hours'"
            " WHERE work_id = (SELECT work_id FROM omp_jobs.jobs WHERE job_id=%s)",
            (ready.job_id,),
        )
    assert operations._task_deadline(_ctx(ready, {})) is None
    # The sandbox refuses before provisioning anything.
    ran = operations._sandbox(_ctx(ready, {}), ["/bin/true"], tmp_path)
    assert ran is None


def test_neither_limit_keeps_the_launch_fallback(service: Any, tmp_path: Path, monkeypatch: Any) -> None:
    ready = _open(service, tmp_path, monkeypatch, budget=False)
    with _admin(service) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET created_at = clock_timestamp() - interval '5 hours'"
            " WHERE work_id = (SELECT work_id FROM omp_jobs.jobs WHERE job_id=%s)",
            (ready.job_id,),
        )
    before = datetime.now(UTC)
    deadline = operations._task_deadline(_ctx(ready, {}))
    assert deadline is not None
    # From now, not from the (old) creation instant.
    assert before + timedelta(seconds=3600) <= deadline <= datetime.now(UTC) + timedelta(seconds=3600)


def test_dead_lease_has_no_deadline_and_refuses_launch(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    ready = _open(service, tmp_path, monkeypatch, budget=True)
    dead = _ctx(ready, {"wall_clock_seconds": 7200}, alive=False)
    assert operations._task_deadline(dead) is None
    assert operations._sandbox(dead, ["/bin/true"], tmp_path) is None
