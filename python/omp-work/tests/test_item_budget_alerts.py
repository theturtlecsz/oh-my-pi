# ruff: noqa: F811
"""Item budget accounting and threshold alerts (OMP-404).

Proves the accounting contract on the shared ``omp_jobs`` substrate:

- a child job's usage under another work item counts toward the root item's
  budget, because the tree is the root's jobs plus its ``parent_job_id``
  descendants whichever work item they belong to;
- ``tokens`` alerts at 50% then 80% are exactly two rows; a replay writes none;
  ``usd`` alerts independently of ``tokens``; 100% writes ``budget.exceeded`` and
  reports the dimension exhausted;
- ``subagents`` alerts by count/max at 50%/80% only, and never with a max of 0;
- an item with no ``intake_publication`` receipt has no budget and writes nothing;
- ``deliver_outbox`` drains only ``usage_ledger`` rows and leaves ``budget_alert``
  rows committed.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.budget import check_item_budget, item_budget, item_spend, root_work_id
from omp_work.jobs.outbox import deliver_outbox
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.v1.canonical import sha256
from psycopg.rows import dict_row
from test_workflow_service import _create

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_THRESHOLD_EVENT = "budget.threshold_reached"
_EXCEEDED_EVENT = "budget.exceeded"


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


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


def _item_with_budget(native_jobs, title: str, budget: dict[str, object]) -> UUID:
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
) -> str:
    job_id = f"job-{uuid4()}"
    enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
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
    request_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, object]:
    return record_usage(
        store,
        operation_id=operation_id or f"op-{uuid4()}",
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=job_id,
        request_id=request_id or f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=0,
        measurement="measured",
        price_usd=price_usd,
    )


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
            SELECT event_id, kind, state, revision, ack_token, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s AND event_id = ANY(%s)
            """,
            (native_jobs.workspace_id, event_ids),
        ).fetchall()
    return {str(row["event_id"]): row for row in rows}


_GENEROUS = {
    "usd": "1000000.00",
    "tokens": 100_000_000,
    "wall_clock_seconds": 86_400,
    "max_subagents": 0,
}


def test_child_usage_under_another_work_id_counts_toward_root(native_jobs) -> None:
    """Usage recorded on a child job of another work item alerts the root item's budget."""
    store = _store(native_jobs)
    root = _item_with_budget(native_jobs, "budget root", {**_GENEROUS, "tokens": 1000})
    child_work = _item(native_jobs, "budget child")

    root_job = _enqueue(native_jobs, store, root)
    child_job = _enqueue(native_jobs, store, child_work, parent_job_id=root_job)

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        assert root_work_id(cur, native_jobs.workspace_id, child_job) == root
        spend = item_spend(cur, native_jobs.workspace_id, root)
    assert spend.tokens == 0
    assert spend.subagents == 1

    _record(
        native_jobs,
        store,
        work_id=child_work,
        job_id=child_job,
        input_tokens=500,
        output_tokens=0,
        price_usd="0.00",
    )

    rows = _alert_rows(native_jobs, [_alert_event_id(native_jobs, root, "tokens", 50)])
    assert set(rows) == {_alert_event_id(native_jobs, root, "tokens", 50)}
    assert rows[_alert_event_id(native_jobs, root, "tokens", 50)]["kind"] == "budget_alert"


def test_tokens_50_then_80_two_rows_replay_writes_none(native_jobs) -> None:
    """Tokens crossing 50% then 80% writes exactly two rows; a replay writes none."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "budget tokens", {**_GENEROUS, "tokens": 1000}
    )
    job_id = _enqueue(native_jobs, store, work_id)
    request_id = f"req-{uuid4()}"

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=300,
        output_tokens=200,
        price_usd="0.10",
        request_id=request_id,
    )
    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=200,
        output_tokens=100,
        price_usd="0.10",
    )

    tokens_50 = _alert_event_id(native_jobs, work_id, "tokens", 50)
    tokens_80 = _alert_event_id(native_jobs, work_id, "tokens", 80)
    rows = _alert_rows(native_jobs, [tokens_50, tokens_80])
    assert set(rows) == {tokens_50, tokens_80}
    for row in rows.values():
        assert row["kind"] == "budget_alert"
        assert row["state"] == "committed"
        assert row["revision"] == 1
        assert row["payload"]["event"] == _THRESHOLD_EVENT
        assert row["payload"]["dimension"] == "tokens"
    assert rows[tokens_80]["payload"]["threshold_percent"] == 80
    assert rows[tokens_80]["payload"]["spent"] == 800
    assert rows[tokens_80]["payload"]["limit"] == 1000

    # A replay of the first usage replays the operation: no new alert rows.
    replayed = _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=300,
        output_tokens=200,
        price_usd="0.10",
        request_id=request_id,
    )
    assert replayed["status"] == "replayed"
    assert set(_alert_rows(native_jobs, [tokens_50, tokens_80, _alert_event_id(native_jobs, work_id, "tokens", 100)])) == {
        tokens_50,
        tokens_80,
    }

    # Re-running the check directly writes nothing either.
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        assert (
            check_item_budget(
                store,
                cur,
                workspace_id=native_jobs.workspace_id,
                actor_id=native_jobs.actor_id,
                work_id=work_id,
                operation_id=str(uuid4()),
            )
            == ()
        )


def test_usd_alerts_independently_of_tokens(native_jobs) -> None:
    """usd thresholds fire on their own dimension without dragging tokens along."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "budget usd", {**_GENEROUS, "usd": "1.00", "tokens": 1_000_000}
    )
    job_id = _enqueue(native_jobs, store, work_id)

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=100,
        output_tokens=0,
        price_usd="0.50",
    )
    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=100,
        output_tokens=0,
        price_usd="0.30",
    )

    usd_50 = _alert_event_id(native_jobs, work_id, "usd", 50)
    usd_80 = _alert_event_id(native_jobs, work_id, "usd", 80)
    rows = _alert_rows(native_jobs, [usd_50, usd_80])
    assert set(rows) == {usd_50, usd_80}
    assert rows[usd_80]["payload"]["dimension"] == "usd"
    assert Decimal(rows[usd_80]["payload"]["spent"]) == Decimal("0.80")
    assert Decimal(rows[usd_80]["payload"]["limit"]) == Decimal("1.00")
    # Tokens stayed far below 50%, so no tokens row was written.
    assert (
        _alert_rows(native_jobs, [_alert_event_id(native_jobs, work_id, "tokens", 50)])
        == {}
    )


def test_tokens_exceeded_reports_exhausted(native_jobs) -> None:
    """Crossing 100% writes budget.exceeded and reports the dimension exhausted."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "budget exceeded", {**_GENEROUS, "tokens": 1000}
    )
    job_id = _enqueue(native_jobs, store, work_id)

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=400,
        output_tokens=0,
        price_usd="0.00",
    )
    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=700,
        output_tokens=0,
        price_usd="0.00",
    )

    tokens_100 = _alert_event_id(native_jobs, work_id, "tokens", 100)
    rows = _alert_rows(
        native_jobs,
        [
            _alert_event_id(native_jobs, work_id, "tokens", 50),
            _alert_event_id(native_jobs, work_id, "tokens", 80),
            tokens_100,
        ],
    )
    assert len(rows) == 3
    assert rows[tokens_100]["payload"]["event"] == _EXCEEDED_EVENT
    assert rows[tokens_100]["payload"]["threshold_percent"] == 100
    assert rows[tokens_100]["payload"]["spent"] == 1100

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        assert (
            check_item_budget(
                store,
                cur,
                workspace_id=native_jobs.workspace_id,
                actor_id=native_jobs.actor_id,
                work_id=work_id,
                operation_id=str(uuid4()),
            )
            == ("tokens",)
        )


def test_subagents_50_threshold(native_jobs) -> None:
    """Two children against max_subagents=4 write one subagents 50 row at enqueue."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "budget subagents", {**_GENEROUS, "max_subagents": 4}
    )
    root_job = _enqueue(native_jobs, store, work_id)
    _enqueue(native_jobs, store, work_id, parent_job_id=root_job)
    _enqueue(native_jobs, store, work_id, parent_job_id=root_job)

    sub_50 = _alert_event_id(native_jobs, work_id, "subagents", 50)
    sub_80 = _alert_event_id(native_jobs, work_id, "subagents", 80)
    # The enqueue path checks the item budget after inserting a child, so the
    # second child writes the 50% row before any usage is recorded. s05.
    assert set(_alert_rows(native_jobs, [sub_50, sub_80])) == {sub_50}

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=None,
        input_tokens=1,
        output_tokens=0,
        price_usd="0.00",
    )

    rows = _alert_rows(native_jobs, [sub_50, sub_80])
    assert set(rows) == {sub_50}
    assert rows[sub_50]["payload"]["dimension"] == "subagents"
    assert rows[sub_50]["payload"]["spent"] == 2
    assert rows[sub_50]["payload"]["limit"] == 4


def test_subagents_never_exceeds_and_zero_max_is_silent(native_jobs) -> None:
    """subagents alerts at 50/80 by count/max only, and max 0 writes nothing."""
    store = _store(native_jobs)
    capped = _item_with_budget(
        native_jobs, "budget subagents capped", {**_GENEROUS, "max_subagents": 2}
    )
    root_job = _enqueue(native_jobs, store, capped)
    _enqueue(native_jobs, store, capped, parent_job_id=root_job)
    _enqueue(native_jobs, store, capped, parent_job_id=root_job)
    _record(
        native_jobs,
        store,
        work_id=capped,
        job_id=None,
        input_tokens=1,
        output_tokens=0,
        price_usd="0.00",
    )

    sub_50 = _alert_event_id(native_jobs, capped, "subagents", 50)
    sub_80 = _alert_event_id(native_jobs, capped, "subagents", 80)
    sub_100 = _alert_event_id(native_jobs, capped, "subagents", 100)
    rows = _alert_rows(native_jobs, [sub_50, sub_80, sub_100])
    assert set(rows) == {sub_50, sub_80}
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        assert (
            check_item_budget(
                store,
                cur,
                workspace_id=native_jobs.workspace_id,
                actor_id=native_jobs.actor_id,
                work_id=capped,
                operation_id=str(uuid4()),
            )
            == ()
        )

    silent = _item_with_budget(
        native_jobs, "budget subagents silent", {**_GENEROUS, "max_subagents": 0}
    )
    silent_root = _enqueue(native_jobs, store, silent)
    _enqueue(native_jobs, store, silent, parent_job_id=silent_root)
    _record(
        native_jobs,
        store,
        work_id=silent,
        job_id=None,
        input_tokens=1,
        output_tokens=0,
        price_usd="0.00",
    )
    assert (
        _alert_rows(
            native_jobs,
            [
                _alert_event_id(native_jobs, silent, "subagents", 50),
                _alert_event_id(native_jobs, silent, "subagents", 80),
            ],
        )
        == {}
    )


def test_unbudgeted_item_writes_no_rows(native_jobs) -> None:
    """An item with no intake_publication receipt is unbudgeted and silent."""
    store = _store(native_jobs)
    work_id = _item(native_jobs, "budget unbudgeted")
    job_id = _enqueue(native_jobs, store, work_id)

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        assert item_budget(cur, native_jobs.workspace_id, work_id) is None

    _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=500,
        output_tokens=500,
        price_usd="5.00",
    )

    expected = [
        _alert_event_id(native_jobs, work_id, dimension, threshold)
        for dimension in ("usd", "tokens", "wall_clock_seconds", "subagents")
        for threshold in (50, 80, 100)
    ]
    assert _alert_rows(native_jobs, expected) == {}


def test_item_budget_reads_published_receipt(native_jobs) -> None:
    """item_budget returns the published budget and None without a receipt."""
    budget = {"usd": "12.50", "tokens": 250, "wall_clock_seconds": 600, "max_subagents": 3}
    work_id = _item_with_budget(native_jobs, "budget readable", budget)
    store = _store(native_jobs)

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        loaded = item_budget(cur, native_jobs.workspace_id, work_id)
    assert loaded is not None
    assert loaded.usd == "12.50"
    assert loaded.tokens == 250
    assert loaded.wall_clock_seconds == 600
    assert loaded.max_subagents == 3


def test_deliver_outbox_leaves_budget_alert_rows(native_jobs, tmp_path) -> None:
    """deliver_outbox drains usage_ledger rows and leaves budget_alert rows committed."""
    store = _store(native_jobs)
    work_id = _item_with_budget(
        native_jobs, "budget deliver", {**_GENEROUS, "usd": "1.00"}
    )
    job_id = _enqueue(native_jobs, store, work_id)
    usage = _record(
        native_jobs,
        store,
        work_id=work_id,
        job_id=job_id,
        input_tokens=10,
        output_tokens=0,
        price_usd="0.60",
    )
    usage_id = str(usage["usage_id"])
    usd_50 = _alert_event_id(native_jobs, work_id, "usd", 50)

    ledger = tmp_path / "usage.json"
    assert (
        deliver_outbox(
            store,
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        >= 1
    )

    events = json.loads(ledger.read_text())["events"]
    assert usage_id in [event["usage_id"] for event in events]

    alert = _alert_rows(native_jobs, [usd_50])[usd_50]
    assert alert["state"] == "committed"
    assert alert["revision"] == 1
    assert alert["ack_token"] is None

    usage_row = _alert_rows(native_jobs, [f"usage:{usage_id}"])[f"usage:{usage_id}"]
    assert usage_row["state"] == "closed"
