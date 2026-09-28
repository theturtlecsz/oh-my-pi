# ruff: noqa: F811
"""Stopping an item's run when its budget is overrun (OMP-404-s05).

Runs on the shared ``omp_jobs`` substrate with the module-scoped ``native_jobs``
fixture and seeds the ``intake_publication`` receipt by SQL (the same helper
shape as the s04 alert tests). Proves the stop side of item budgets:

- usage that crosses an item's token limit stops the whole tree (root and
  sub-agents), writes the 100 alert, refuses a later root enqueue with
  ``budget_exceeded``, and leaves no job for a claim;
- a child that would push an item past ``max_subagents`` is refused without a
  row, writes the 100 subagents alert beside the 50 and 80 rows, and the
  already-running tree is stopped;
- a wall-clock limit is enforced by ``sweep_item_budgets`` even though no usage
  row arrives to trigger a check;
- an item with no published budget keeps running through the same usage.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.budget import sweep_item_budgets
from omp_work.jobs.store import JobError, NativeJobStore, register_worker
from omp_work.jobs.usage import record_usage
from omp_work.v1.canonical import sha256
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_research_contract import _register_component
from test_workflow_service import _create

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_EXCEEDED_EVENT = "budget.exceeded"

_RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
_GENEROUS = {
    "usd": "1000000.00",
    "tokens": 100_000_000,
    "wall_clock_seconds": 86_400,
    "max_subagents": 0,
}


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _cap() -> str:
    """A unique capability inside the research-component pattern."""
    return "isolate.a" + uuid4().hex


def _item(native_jobs, title: str) -> UUID:
    item = _create(native_jobs.service, native_jobs.workspace_id, title)
    return UUID(item["work_id"])


def _seed_budget(
    native_jobs,
    *,
    work_id: UUID,
    revision_id: UUID,
    budget: dict[str, object],
) -> None:
    """Direct-SQL seed of the ``intake_publication`` receipt that carries the budget."""
    config = native_jobs.service.config
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(native_jobs.actor_id),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                native_jobs.workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                native_jobs.workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _item_with_budget(
    native_jobs, title: str, budget: dict[str, object]
) -> UUID:
    item = _create(native_jobs.service, native_jobs.workspace_id, title)
    work_id = UUID(item["work_id"])
    _seed_budget(
        native_jobs,
        work_id=work_id,
        revision_id=UUID(item["revision_id"]),
        budget=budget,
    )
    return work_id


def _enqueue(
    native_jobs,
    store: NativeJobStore,
    work_id: UUID,
    *,
    parent_job_id: str | None = None,
    capabilities: list[str] | None = None,
    operation_id: str | None = None,
) -> str:
    job_id = f"job-{uuid4()}"
    enqueue_job(
        store,
        operation_id=operation_id or str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=capabilities or ["compute.cpu"],
        resources=dict(_RESOURCES),
        lease_seconds=30,
        parent_job_id=parent_job_id,
    )
    return job_id


def _record(
    native_jobs,
    store: NativeJobStore,
    *,
    work_id: UUID,
    job_id: str | None,
    input_tokens: int,
    output_tokens: int,
    price_usd: str,
) -> dict[str, object]:
    return record_usage(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=0,
        measurement="measured",
        price_usd=price_usd,
    )


def _worker(native_jobs, store: NativeJobStore, capabilities: list[str]) -> str:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"stop-{uuid4()}",
        capabilities=tuple(sorted(set(capabilities))),
    )
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=component,
        capabilities=capabilities,
        capacity=4,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def _claim(native_jobs, store: NativeJobStore, worker_id: str) -> dict[str, object]:
    return claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )


def _insert_backlog(
    native_jobs,
    job_id: str,
    work_id: UUID,
    capabilities: list[str],
    *,
    parent_job_id: str | None = None,
) -> None:
    with _connect(native_jobs) as conn:
        conn.execute(
            """
            INSERT INTO omp_jobs.jobs(
                job_id, status, source, workspace_id, work_id, parent_job_id, kind,
                required_capabilities, resources, lease_seconds, fence, attempt
            ) VALUES (
                %s, 'backlog', 'native', %s, %s, %s, 'compute',
                %s, %s, 30, 0, 0
            )
            """,
            (
                job_id,
                native_jobs.workspace_id,
                work_id,
                parent_job_id,
                Jsonb(capabilities),
                Jsonb(dict(_RESOURCES)),
            ),
        )


def _backdate(native_jobs, job_id: str, seconds: int) -> None:
    with _connect(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET created_at = clock_timestamp() - %s * interval '1 second' WHERE job_id=%s",
            (seconds, job_id),
        )


def _job(native_jobs, job_id: str) -> dict[str, object] | None:
    with _connect(native_jobs) as conn:
        return conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()


def _alert_event_id(native_jobs, work_id: UUID, dimension: str, threshold: int) -> str:
    return sha256(
        {
            "workspace_id": str(native_jobs.workspace_id),
            "work_id": str(work_id),
            "dimension": dimension,
            "threshold": threshold,
        }
    )


def _alert_rows(native_jobs, event_ids: list[str]) -> dict[str, dict[str, object]]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            """
            SELECT event_id, kind, state, revision, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s AND event_id = ANY(%s)
            """,
            (native_jobs.workspace_id, event_ids),
        ).fetchall()
    return {str(row["event_id"]): row for row in rows}


def test_tokens_overrun_stops_tree_refuses_root_and_starves_claim(native_jobs) -> None:
    """A child's usage past the token limit stops the tree, refuses a new root, and blocks claim."""
    store = _store(native_jobs)
    root = _item_with_budget(
        native_jobs, "stop tokens root", {**_GENEROUS, "tokens": 1000}
    )
    child_work = _item(native_jobs, "stop tokens child")

    root_job = _enqueue(native_jobs, store, root)
    child_job = _enqueue(native_jobs, store, child_work, parent_job_id=root_job)

    stopped = _record(
        native_jobs,
        store,
        work_id=child_work,
        job_id=child_job,
        input_tokens=700,
        output_tokens=400,
        price_usd="0.00",
    )
    assert stopped["status"] == "applied"

    # Usage of the child, recorded under another work item, stops the root item's
    # whole tree and writes the 100 alert on the root item.
    for job_id in (root_job, child_job):
        row = _job(native_jobs, job_id)
        assert row["status"] == "cancelled"
        assert row["cancel_reason"] == "budget_exceeded"
        assert row["cancelled_at"] is not None

    tokens_100 = _alert_event_id(native_jobs, root, "tokens", 100)
    rows = _alert_rows(native_jobs, [tokens_100])
    assert set(rows) == {tokens_100}
    assert rows[tokens_100]["kind"] == "budget_alert"
    assert rows[tokens_100]["payload"]["event"] == _EXCEEDED_EVENT
    assert rows[tokens_100]["payload"]["dimension"] == "tokens"
    assert rows[tokens_100]["payload"]["spent"] == 1100
    assert rows[tokens_100]["payload"]["limit"] == 1000

    # A new root enqueue on the exhausted item is refused with budget_exceeded
    # and inserts no row.
    refused_job = f"job-{uuid4()}"
    with pytest.raises(JobError) as refused:
        enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=refused_job,
            work_id=root,
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources=dict(_RESOURCES),
            lease_seconds=30,
        )
    assert refused.value.code == "budget_exceeded"
    assert _job(native_jobs, refused_job) is None

    # A backlog job placed directly on the exhausted item is not leased: the
    # claim checks the root item before taking the lease.
    cap = _cap()
    stranded = f"job-{uuid4()}"
    _insert_backlog(native_jobs, stranded, root, [cap])
    worker_id = _worker(native_jobs, store, [cap])
    missed = _claim(native_jobs, store, worker_id)
    assert missed["status"] == "applied"
    assert missed["job"] is None
    assert _job(native_jobs, stranded)["status"] == "backlog"


def test_subagent_cap_refuses_third_child_and_stops_tree(native_jobs) -> None:
    """children 1 and 2 alert at 50 and 80; child 3 is refused, writes 100, and stops the tree."""
    store = _store(native_jobs)
    root = _item_with_budget(
        native_jobs, "stop subagents root", {**_GENEROUS, "max_subagents": 2}
    )

    root_job = _enqueue(native_jobs, store, root)
    child_one = _enqueue(native_jobs, store, root, parent_job_id=root_job)
    child_two = _enqueue(native_jobs, store, root, parent_job_id=root_job)

    sub_50 = _alert_event_id(native_jobs, root, "subagents", 50)
    sub_80 = _alert_event_id(native_jobs, root, "subagents", 80)
    rows = _alert_rows(native_jobs, [sub_50, sub_80])
    assert set(rows) == {sub_50, sub_80}
    assert rows[sub_50]["payload"]["spent"] == 1
    assert rows[sub_50]["payload"]["limit"] == 2
    assert rows[sub_80]["payload"]["spent"] == 2
    assert rows[sub_80]["payload"]["limit"] == 2

    child_three = f"job-{uuid4()}"
    with pytest.raises(JobError) as refused:
        enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=child_three,
            work_id=root,
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources=dict(_RESOURCES),
            lease_seconds=30,
            parent_job_id=root_job,
        )
    assert refused.value.code == "budget_exceeded"
    assert _job(native_jobs, child_three) is None

    sub_100 = _alert_event_id(native_jobs, root, "subagents", 100)
    all_rows = _alert_rows(native_jobs, [sub_50, sub_80, sub_100])
    assert set(all_rows) == {sub_50, sub_80, sub_100}
    assert all_rows[sub_100]["kind"] == "budget_alert"
    assert all_rows[sub_100]["payload"]["event"] == _EXCEEDED_EVENT
    assert all_rows[sub_100]["payload"]["dimension"] == "subagents"
    assert all_rows[sub_100]["payload"]["limit"] == 2

    # The already-running tree is stopped and the 100 row persists.
    for job_id in (root_job, child_one, child_two):
        row = _job(native_jobs, job_id)
        assert row["status"] == "cancelled"
        assert row["cancel_reason"] == "budget_exceeded"
    assert set(
        _alert_rows(native_jobs, [sub_50, sub_80, sub_100])
    ) == {sub_50, sub_80, sub_100}


def test_wall_clock_sweep_stops_item_without_new_usage(native_jobs) -> None:
    """A backdated job over a 1s wall-clock budget is stopped by sweep_item_budgets."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "stop wall clock", {**_GENEROUS, "wall_clock_seconds": 1}
    )
    job_id = _enqueue(native_jobs, store, work_id)
    _backdate(native_jobs, job_id, 2)

    stopped = sweep_item_budgets(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert work_id in stopped

    row = _job(native_jobs, job_id)
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "budget_exceeded"

    wall_100 = _alert_event_id(native_jobs, work_id, "wall_clock_seconds", 100)
    rows = _alert_rows(native_jobs, [wall_100])
    assert set(rows) == {wall_100}
    assert rows[wall_100]["payload"]["event"] == _EXCEEDED_EVENT
    assert rows[wall_100]["payload"]["limit"] == 1

    # A second sweep finds nothing left to stop: every live job of a visited
    # item was cancelled by the first.
    assert (
        sweep_item_budgets(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
        )
        == []
    )


def test_unbudgeted_item_keeps_running_through_the_same_usage(native_jobs) -> None:
    """Without an intake_publication budget the same usage stops nothing."""
    store = _store(native_jobs)
    work_id = _item(native_jobs, "stop unbudgeted")
    cap = _cap()
    root_job = _enqueue(native_jobs, store, work_id, capabilities=[cap])
    child_job = _enqueue(
        native_jobs, store, work_id, parent_job_id=root_job, capabilities=[cap]
    )

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=child_job,
        input_tokens=900_000,
        output_tokens=900_000,
        price_usd="9000.00",
    )

    assert _job(native_jobs, root_job)["status"] == "backlog"
    assert _job(native_jobs, child_job)["status"] == "backlog"

    expected = [
        _alert_event_id(native_jobs, work_id, dimension, threshold)
        for dimension in ("usd", "tokens", "wall_clock_seconds", "subagents")
        for threshold in (50, 80, 100)
    ]
    assert _alert_rows(native_jobs, expected) == {}

    # The item keeps admitting and leasing work.
    grown = _enqueue(native_jobs, store, work_id, capabilities=[cap])
    worker_id = _worker(native_jobs, store, [cap])
    claimed = _claim(native_jobs, store, worker_id)
    assert claimed["job"] is not None
    assert claimed["job"]["job_id"] in (root_job, child_job, grown)
    assert claimed["job"]["status"] == "admitted"
