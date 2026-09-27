"""Outbox delivery and reconciliation of usage to the WP5 file ledger (R03, OMP-324).

Proves the drain of ``omp_jobs.outbox`` rows written by ``jobs.usage.record_usage``
to the workspace usage ledger file with no duplicate external effect:
- two usages deliver as exactly one ledger event per ``usage_id`` and a second
  call appends nothing and returns 0;
- a crash after the append but before the state update (a raising ``close``)
  leaves the row ``committed``; the next call closes it without appending;
- an append failure marks the row ``failed``; a retry with a valid path appends
  once and closes the row at revision 2;
- an unknown-usage event is written with null tokens.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.contracts.v1 import recovery
from omp_work.jobs.outbox import deliver_outbox
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.usage import record_usage
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _workspace() -> str:
    """A fresh workspace id: the outbox tables filter by workspace_id only."""
    return str(uuid4())


def _record(
    native_jobs,
    store: NativeJobStore,
    workspace_id: str,
    *,
    measurement: str = "measured",
    input_tokens: int | None = 10,
    output_tokens: int | None = 5,
    cache_tokens: int | None = 0,
    price_usd: str | None = "0.001",
) -> str:
    result = record_usage(
        store,
        operation_id=f"op-{uuid4()}",
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=uuid4(),
        job_id=None,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        measurement=measurement,
        price_usd=price_usd,
    )
    assert result["status"] == "applied"
    return str(result["usage_id"])


def _outbox_rows(native_jobs, workspace_id: str) -> dict[str, dict[str, object]]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            """
            SELECT event_id, state, revision, ack_token
            FROM omp_jobs.outbox WHERE workspace_id=%s ORDER BY event_id
            """,
            (UUID(workspace_id),),
        ).fetchall()
    return {str(row["event_id"]): row for row in rows}


def _ledger_events(ledger_path) -> list[dict[str, object]]:
    return json.loads(ledger_path.read_text())["events"]


def test_deliver_appends_once_per_usage_and_closes_rows(native_jobs, tmp_path) -> None:
    """Two usages deliver as one event each; the second call appends nothing."""
    store = _store(native_jobs)
    workspace_id = _workspace()
    ledger = tmp_path / "usage.json"
    usage_ids = [_record(native_jobs, store, workspace_id) for _ in range(2)]

    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        == 2
    )

    events = _ledger_events(ledger)
    assert sorted(str(event["usage_id"]) for event in events) == sorted(usage_ids)

    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        == 0
    )
    assert _ledger_events(ledger) == events

    rows = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in rows.values()} == {"closed"}
    assert {str(row["ack_token"]) for row in rows.values()} == {"ack-1"}


def test_close_crash_leaves_committed_and_next_deliver_closes(
    native_jobs, tmp_path, monkeypatch
) -> None:
    """A append-then-crash leaves the row committed; the next call closes it, no re-append."""
    store = _store(native_jobs)
    workspace_id = _workspace()
    ledger = tmp_path / "usage.json"
    usage_id = _record(native_jobs, store, workspace_id)

    real_close = recovery.close
    calls = {"n": 0}

    def flaky_close(record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("close failed after append")
        return real_close(record)

    monkeypatch.setattr(recovery, "close", flaky_close)

    with pytest.raises(RuntimeError, match="close failed after append"):
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )

    assert [str(event["usage_id"]) for event in _ledger_events(ledger)] == [usage_id]
    crashed = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in crashed.values()} == {"committed"}

    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        == 0
    )
    assert len(_ledger_events(ledger)) == 1
    recovered = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in recovered.values()} == {"closed"}


def test_append_failure_marks_failed_then_retry_closes_at_revision_two(
    native_jobs, tmp_path
) -> None:
    """A directory ledger path fails the row; a valid retry appends once and closes at revision 2."""
    store = _store(native_jobs)
    workspace_id = _workspace()
    bad_path = tmp_path / "a-directory"
    bad_path.mkdir()

    _record(native_jobs, store, workspace_id)

    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=bad_path,
        )
        == 0
    )
    failed = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in failed.values()} == {"failed"}
    assert {int(row["revision"]) for row in failed.values()} == {1}

    ledger = tmp_path / "usage.json"
    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        == 1
    )
    assert len(_ledger_events(ledger)) == 1
    closed = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in closed.values()} == {"closed"}
    assert {int(row["revision"]) for row in closed.values()} == {2}


def test_unknown_usage_event_written_with_null_tokens(native_jobs, tmp_path) -> None:
    """An unknown-measurement usage reaches the ledger with null tokens and null price."""
    store = _store(native_jobs)
    workspace_id = _workspace()
    ledger = tmp_path / "usage.json"
    usage_id = _record(
        native_jobs,
        store,
        workspace_id,
        measurement="unknown",
        input_tokens=None,
        output_tokens=None,
        cache_tokens=None,
        price_usd=None,
    )

    assert (
        deliver_outbox(
            store,
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            ledger_path=ledger,
        )
        == 1
    )

    [event] = _ledger_events(ledger)
    assert event["usage_id"] == usage_id
    assert event["measurement"] == "unknown"
    assert event["input_tokens"] is None
    assert event["output_tokens"] is None
    assert event["cache_tokens"] is None
    assert event["price_usd"] is None

    closed = _outbox_rows(native_jobs, workspace_id)
    assert {str(row["state"]) for row in closed.values()} == {"closed"}
