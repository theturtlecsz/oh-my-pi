# ruff: noqa: F811
"""PostgreSQL integration tests for budget alert relay (OMP-430-s05).

Verifies the budget notice relay to ops.alarm push subscribers:
- 50%, 80%, and 100% tokens rows give cost_threshold, cost_threshold, and
  budget_exceeded events with subject, detail, and work_id;
- a store error leaves the outbox row failed, and the next call records it
  with the same deterministic operation id;
- closed rows are not resent;
- budget-alerts CLI command sweeps and relays without grokbot or OMP_GROKBOT_* env.
"""

from __future__ import annotations

import json
import os
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_workflow_service import _create

from omp_work.__main__ import main
from omp_work.jobs.budget_relay import relay_budget_alerts
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.canonical import sha256
from omp_work.v1.store import PostgresWorkStore, WorkStoreError

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _work_store(native_jobs) -> PostgresWorkStore:
    return PostgresWorkStore(native_jobs.service.config)


def _event_id(workspace_id: UUID, work_id: UUID, threshold: int) -> str:
    return sha256(
        {
            "workspace_id": str(workspace_id),
            "work_id": str(work_id),
            "dimension": "tokens",
            "threshold": threshold,
        }
    )


def _insert_outbox(
    store: NativeJobStore,
    workspace_id: UUID,
    actor_id: UUID,
    event_id: str,
    payload: dict[str, object],
    *,
    kind: str = "budget_alert",
    state: str = "committed",
    revision: int = 1,
) -> None:
    with store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            INSERT INTO omp_jobs.outbox(
                event_id, workspace_id, operation_id, kind, payload, state, revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                workspace_id,
                f"op-{event_id[:12]}",
                kind,
                Jsonb(payload),
                state,
                revision,
            ),
        )


def _outbox_rows(native_jobs, workspace_id: UUID) -> dict[str, dict[str, object]]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        found = conn.execute(
            """
            SELECT event_id, kind, state, revision, ack_token, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s
            """,
            (workspace_id,),
        ).fetchall()
    return {str(row["event_id"]): row for row in found}


def _domain_events(
    job_store: NativeJobStore, workspace_id: UUID, actor_id: UUID
) -> list[dict[str, object]]:
    with job_store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            SELECT operation_id, event_type, payload
            FROM omp_audit.domain_events
            WHERE workspace_id=%s AND event_type='record_alarm_signal'
            ORDER BY sequence
            """,
            (workspace_id,),
        )
        return cur.fetchall()


def test_budget_relay_tokens_thresholds_and_exceeded(native_jobs) -> None:
    """50/80/100% tokens rows give cost_threshold, cost_threshold, budget_exceeded events."""
    job_store = _store(native_jobs)
    work_store = _work_store(native_jobs)
    workspace_id = native_jobs.workspace_id
    actor_id = native_jobs.actor_id

    item = _create(native_jobs.service, workspace_id, "tokens budget item")
    work_id = UUID(item["work_id"])

    payload_50 = {
        "event": "budget.threshold_reached",
        "dimension": "tokens",
        "threshold_percent": 50,
        "spent": 500,
        "limit": 1000,
        "work_id": str(work_id),
    }
    payload_80 = {
        "event": "budget.threshold_reached",
        "dimension": "tokens",
        "threshold_percent": 80,
        "spent": 800,
        "limit": 1000,
        "work_id": str(work_id),
    }
    payload_100 = {
        "event": "budget.exceeded",
        "dimension": "tokens",
        "threshold_percent": 100,
        "spent": 1000,
        "limit": 1000,
        "work_id": str(work_id),
    }

    event_id_50 = _event_id(workspace_id, work_id, 50)
    event_id_80 = _event_id(workspace_id, work_id, 80)
    event_id_100 = _event_id(workspace_id, work_id, 100)

    _insert_outbox(job_store, workspace_id, actor_id, event_id_50, payload_50)
    _insert_outbox(job_store, workspace_id, actor_id, event_id_80, payload_80)
    _insert_outbox(job_store, workspace_id, actor_id, event_id_100, payload_100)

    # Relay the 3 rows
    relayed = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    assert relayed == 3

    # Check the 3 recorded alarm signal events
    events_by_op = {
        UUID(str(ev["operation_id"])): ev
        for ev in _domain_events(job_store, workspace_id, actor_id)
        if ev["payload"].get("work_id") == str(work_id)
    }
    assert len(events_by_op) == 3

    op_id_50 = uuid5(NAMESPACE_URL, f"omp-work:budget-alert:{event_id_50}")
    op_id_80 = uuid5(NAMESPACE_URL, f"omp-work:budget-alert:{event_id_80}")
    op_id_100 = uuid5(NAMESPACE_URL, f"omp-work:budget-alert:{event_id_100}")

    # Threshold 50
    ev_50 = events_by_op[op_id_50]
    assert ev_50["payload"]["signal"] == "cost_threshold"
    assert ev_50["payload"]["subject"] == "tokens budget 50% reached"
    assert ev_50["payload"]["detail"] == "spent 500 of 1000"
    assert ev_50["payload"]["work_id"] == str(work_id)

    # Threshold 80
    ev_80 = events_by_op[op_id_80]
    assert ev_80["payload"]["signal"] == "cost_threshold"
    assert ev_80["payload"]["subject"] == "tokens budget 80% reached"
    assert ev_80["payload"]["detail"] == "spent 800 of 1000"
    assert ev_80["payload"]["work_id"] == str(work_id)

    # Threshold 100
    ev_100 = events_by_op[op_id_100]
    assert ev_100["payload"]["signal"] == "budget_exceeded"
    assert ev_100["payload"]["subject"] == "tokens budget 100% reached"
    assert ev_100["payload"]["detail"] == "spent 1000 of 1000"
    assert ev_100["payload"]["work_id"] == str(work_id)

    # Closed rows are not resent
    outbox = _outbox_rows(native_jobs, workspace_id)
    for eid in (event_id_50, event_id_80, event_id_100):
        row = outbox[eid]
        assert row["state"] == "closed"
        assert row["revision"] == 1
        assert row["ack_token"] == "ack-1"

    second_relayed = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    assert second_relayed == 0
    events_after = [
        ev
        for ev in _domain_events(job_store, workspace_id, actor_id)
        if ev["payload"].get("work_id") == str(work_id)
    ]
    assert len(events_after) == 3


def test_store_error_leaves_row_failed_then_next_call_records_same_operation_id(
    native_jobs, monkeypatch
) -> None:
    """A store error leaves the row failed, and the next call records it with the same operation_id."""
    job_store = _store(native_jobs)
    work_store = _work_store(native_jobs)
    workspace_id = native_jobs.workspace_id
    actor_id = native_jobs.actor_id

    item = _create(native_jobs.service, workspace_id, "retry item")
    work_id = UUID(item["work_id"])

    payload = {
        "event": "budget.threshold_reached",
        "dimension": "tokens",
        "threshold_percent": 80,
        "spent": 800,
        "limit": 1000,
        "work_id": str(work_id),
    }
    event_id = _event_id(workspace_id, work_id, 80)
    expected_op_id = uuid5(NAMESPACE_URL, f"omp-work:budget-alert:{event_id}")

    _insert_outbox(job_store, workspace_id, actor_id, event_id, payload)

    # Simulate store error on first attempt
    orig_execute = work_store.execute
    failed_once = False

    def _flaky_execute(envelope, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise WorkStoreError("unavailable", ("simulated_store_failure",))
        return orig_execute(envelope, **kwargs)

    monkeypatch.setattr(work_store, "execute", _flaky_execute)

    # First call fails
    relayed_1 = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    assert relayed_1 == 0

    # Outbox row is marked failed
    row_failed = _outbox_rows(native_jobs, workspace_id)[event_id]
    assert row_failed["state"] == "failed"
    assert row_failed["revision"] == 1
    assert row_failed["ack_token"] is None

    # No domain event yet
    matching_events_1 = [
        ev
        for ev in _domain_events(job_store, workspace_id, actor_id)
        if UUID(str(ev["operation_id"])) == expected_op_id
    ]
    assert len(matching_events_1) == 0

    # Second call succeeds
    relayed_2 = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    assert relayed_2 == 1

    # Outbox row is now closed with revision 2 and ack-2
    row_closed = _outbox_rows(native_jobs, workspace_id)[event_id]
    assert row_closed["state"] == "closed"
    assert row_closed["revision"] == 2
    assert row_closed["ack_token"] == "ack-2"

    # Event recorded with the exact same operation_id
    matching_events_2 = [
        ev
        for ev in _domain_events(job_store, workspace_id, actor_id)
        if UUID(str(ev["operation_id"])) == expected_op_id
    ]
    assert len(matching_events_2) == 1
    assert matching_events_2[0]["payload"]["signal"] == "cost_threshold"
    assert matching_events_2[0]["payload"]["subject"] == "tokens budget 80% reached"
    assert matching_events_2[0]["payload"]["detail"] == "spent 800 of 1000"
    assert matching_events_2[0]["payload"]["work_id"] == str(work_id)

    # Further call relays 0
    relayed_3 = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=actor_id,
    )
    assert relayed_3 == 0


def test_cli_budget_alerts_prints_json_count(native_jobs, capsys) -> None:
    """The budget-alerts CLI command prints JSON count without requiring grokbot env."""
    workspace_id = native_jobs.workspace_id
    actor_id = native_jobs.actor_id

    code = main(
        ["budget-alerts", "--workspace", str(workspace_id), "--actor", str(actor_id)]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == 0
