# ruff: noqa: F811
"""Native job usage recording (R03, OMP-324).

Proves WP5 accounting on the shared omp_jobs substrate:
- a retry under a new operation_id keeps one usage_id, row, outbox row and step_index;
- a different token split or price is refused, row unchanged;
- unknown usage stores NULL tokens;
- job/work mismatch refused;
- job-less usage accepted.
"""
from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import JobError, NativeJobStore
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


def _work_id(native_jobs) -> UUID:
    return UUID(native_jobs.item["work_id"])


def _enqueue_test_job(
    native_jobs,
    store: NativeJobStore,
    *,
    job_id: str | None = None,
    work_id: UUID | None = None,
) -> str:
    jid = job_id or f"job-{uuid4()}"
    wid = work_id or _work_id(native_jobs)
    enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=jid,
        work_id=wid,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
    )
    return jid


def test_retry_under_new_operation_id_keeps_single_row_and_step_index(native_jobs) -> None:
    """A retry under a new operation_id replays: one row, outbox row, and original step_index."""
    store = _store(native_jobs)
    job_id = _enqueue_test_job(native_jobs, store)
    work_id = _work_id(native_jobs)
    request_id = f"req-{uuid4()}"

    kwargs = {
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "work_id": work_id,
        "job_id": job_id,
        "request_id": request_id,
        "role": "coder",
        "model": "gemini-3.8-flash",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_tokens": 25,
        "measurement": "measured",
        "price_usd": "0.0050000000",
        "price_version": "v1",
    }

    first = record_usage(store, operation_id=f"op-1-{uuid4()}", **kwargs)
    assert first["status"] == "applied"
    usage_id = first["usage_id"]
    assert isinstance(usage_id, str) and len(usage_id) == 64

    # Retry with a completely new operation_id
    second = record_usage(store, operation_id=f"op-2-{uuid4()}", **kwargs)
    assert second["status"] == "replayed"
    assert second["usage_id"] == usage_id

    with _connect(native_jobs) as conn:
        usage_rows = conn.execute(
            """
            SELECT conversation_id, step_index, source_file, tokens, role_derived,
                   job_id_derived, input_tokens, output_tokens, cache_tokens,
                   measurement, price_usd, price_version
            FROM omp_jobs.usage_events WHERE usage_id=%s
            """,
            (usage_id,),
        ).fetchall()
        assert len(usage_rows) == 1
        row = usage_rows[0]
        assert row["conversation_id"] == job_id
        assert row["step_index"] == 1
        assert row["source_file"] == "omp_jobs:native"
        assert row["tokens"] == 175
        assert row["role_derived"] == "coder"
        assert row["job_id_derived"] == job_id
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["cache_tokens"] == 25
        assert row["measurement"] == "measured"
        assert row["price_usd"] == Decimal("0.0050000000")
        assert row["price_version"] == "v1"

        outbox_rows = conn.execute(
            """
            SELECT event_id, workspace_id, kind, state, revision, payload
            FROM omp_jobs.outbox WHERE event_id=%s
            """,
            (f"usage:{usage_id}",),
        ).fetchall()
        assert len(outbox_rows) == 1
        outbox = outbox_rows[0]
        assert outbox["workspace_id"] == native_jobs.workspace_id
        assert outbox["kind"] == "usage_ledger"
        assert outbox["state"] == "committed"
        assert outbox["revision"] == 1

        payload = outbox["payload"]
        assert payload["kind"] == "job_usage"
        assert payload["usage_id"] == usage_id
        assert payload["conversation_id"] == job_id
        assert payload["step_index"] == 1
        assert payload["role"] == "coder"
        assert payload["model"] == "gemini-3.8-flash"
        assert payload["input_tokens"] == 100
        assert payload["output_tokens"] == 50
        assert payload["cache_tokens"] == 25
        assert payload["measurement"] == "measured"
        assert payload["price_usd"] == "0.0050000000"
        assert payload["price_version"] == "v1"
        assert "at" in payload


def test_different_token_split_or_price_refused_row_unchanged(native_jobs) -> None:
    """A different token split or price under the same usage identity is refused with idempotency_conflict."""
    store = _store(native_jobs)
    job_id = _enqueue_test_job(native_jobs, store)
    work_id = _work_id(native_jobs)
    request_id = f"req-{uuid4()}"

    kwargs = {
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "work_id": work_id,
        "job_id": job_id,
        "request_id": request_id,
        "role": "coder",
        "model": "gemini-3.8-flash",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_tokens": 25,
        "measurement": "measured",
        "price_usd": "0.0100000000",
        "price_version": "v1",
    }

    first = record_usage(store, operation_id=f"op-split-1-{uuid4()}", **kwargs)
    assert first["status"] == "applied"
    usage_id = first["usage_id"]

    # Different token split (80/70 instead of 100/50, same total 175)
    split_diff = dict(kwargs)
    split_diff["input_tokens"] = 80
    split_diff["output_tokens"] = 70
    with pytest.raises(JobError) as err_split:
        record_usage(store, operation_id=f"op-split-2-{uuid4()}", **split_diff)
    assert err_split.value.code == "idempotency_conflict"

    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, price_usd FROM omp_jobs.usage_events WHERE usage_id=%s",
            (usage_id,),
        ).fetchone()
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["price_usd"] == Decimal("0.0100000000")

    # Different price
    price_diff = dict(kwargs)
    price_diff["price_usd"] = "0.0200000000"
    with pytest.raises(JobError) as err_price:
        record_usage(store, operation_id=f"op-price-3-{uuid4()}", **price_diff)
    assert err_price.value.code == "idempotency_conflict"

    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT price_usd FROM omp_jobs.usage_events WHERE usage_id=%s",
            (usage_id,),
        ).fetchone()
        assert row["price_usd"] == Decimal("0.0100000000")


def test_unknown_usage_stores_null_tokens(native_jobs) -> None:
    """Measurement 'unknown' stores NULL for all token counts and sum tokens."""
    store = _store(native_jobs)
    job_id = _enqueue_test_job(native_jobs, store)
    work_id = _work_id(native_jobs)
    request_id = f"req-{uuid4()}"

    res = record_usage(
        store,
        operation_id=f"op-unknown-{uuid4()}",
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=job_id,
        request_id=request_id,
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=None,
        output_tokens=None,
        cache_tokens=None,
        measurement="unknown",
        price_usd=None,
    )
    assert res["status"] == "applied"
    usage_id = res["usage_id"]

    with _connect(native_jobs) as conn:
        row = conn.execute(
            """
            SELECT input_tokens, output_tokens, cache_tokens, tokens, measurement, price_usd
            FROM omp_jobs.usage_events WHERE usage_id=%s
            """,
            (usage_id,),
        ).fetchone()
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cache_tokens"] is None
        assert row["tokens"] is None
        assert row["measurement"] == "unknown"
        assert row["price_usd"] is None

    # Providing non-None tokens with measurement unknown must fail invalid_request
    with pytest.raises(JobError) as err:
        record_usage(
            store,
            operation_id=f"op-unknown-bad-{uuid4()}",
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            work_id=work_id,
            job_id=job_id,
            request_id=f"req-{uuid4()}",
            role="coder",
            model="gemini-3.8-flash",
            input_tokens=10,
            output_tokens=None,
            cache_tokens=None,
            measurement="unknown",
            price_usd=None,
        )
    assert err.value.code == "invalid_request"


def test_job_work_mismatch_refused(native_jobs) -> None:
    """Usage with job_id not matching work_id or missing in workspace is refused."""
    store = _store(native_jobs)
    job_id = _enqueue_test_job(native_jobs, store)
    mismatched_work_id = uuid4()

    # Mismatched work_id
    with pytest.raises(JobError) as err_mismatch:
        record_usage(
            store,
            operation_id=f"op-mismatch-{uuid4()}",
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            work_id=mismatched_work_id,
            job_id=job_id,
            request_id=f"req-{uuid4()}",
            role="coder",
            model="gemini-3.8-flash",
            input_tokens=10,
            output_tokens=10,
            cache_tokens=0,
            measurement="measured",
            price_usd="0.001",
        )
    assert err_mismatch.value.code == "invalid_request"

    # Nonexistent job_id
    with pytest.raises(JobError) as err_missing:
        record_usage(
            store,
            operation_id=f"op-missing-{uuid4()}",
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            work_id=_work_id(native_jobs),
            job_id=f"nonexistent-{uuid4()}",
            request_id=f"req-{uuid4()}",
            role="coder",
            model="gemini-3.8-flash",
            input_tokens=10,
            output_tokens=10,
            cache_tokens=0,
            measurement="measured",
            price_usd="0.001",
        )
    assert err_missing.value.code == "invalid_request"


def test_job_less_usage_accepted(native_jobs) -> None:
    """WP5 preflight rule: job_id=None is allowed and sets conversation_id to work_id."""
    store = _store(native_jobs)
    work_id = _work_id(native_jobs)
    request_id_1 = f"req-{uuid4()}"
    request_id_2 = f"req-{uuid4()}"

    first = record_usage(
        store,
        operation_id=f"op-jobless-1-{uuid4()}",
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=None,
        request_id=request_id_1,
        role="preflight",
        model="gemini-3.8-flash",
        input_tokens=20,
        output_tokens=10,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.0005",
    )
    assert first["status"] == "applied"
    usage_id_1 = first["usage_id"]

    second = record_usage(
        store,
        operation_id=f"op-jobless-2-{uuid4()}",
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=None,
        request_id=request_id_2,
        role="preflight",
        model="gemini-3.8-flash",
        input_tokens=30,
        output_tokens=15,
        cache_tokens=5,
        measurement="measured",
        price_usd="0.0008",
    )
    assert second["status"] == "applied"
    usage_id_2 = second["usage_id"]

    with _connect(native_jobs) as conn:
        rows = conn.execute(
            """
            SELECT usage_id, conversation_id, step_index, job_id_derived, tokens
            FROM omp_jobs.usage_events WHERE usage_id IN (%s, %s)
            ORDER BY step_index
            """,
            (usage_id_1, usage_id_2),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["usage_id"] == usage_id_1
        assert rows[0]["conversation_id"] == str(work_id)
        assert rows[0]["step_index"] == 1
        assert rows[0]["job_id_derived"] is None
        assert rows[0]["tokens"] == 30

        assert rows[1]["usage_id"] == usage_id_2
        assert rows[1]["conversation_id"] == str(work_id)
        assert rows[1]["step_index"] == 2
        assert rows[1]["job_id_derived"] is None
        assert rows[1]["tokens"] == 50


def test_partial_token_counts_stores_null_sum_tokens(native_jobs) -> None:
    """Tokens sum is NULL when any count is unknown (None)."""
    store = _store(native_jobs)
    job_id = _enqueue_test_job(native_jobs, store)
    work_id = _work_id(native_jobs)
    request_id = f"req-{uuid4()}"

    res = record_usage(
        store,
        operation_id=f"op-partial-{uuid4()}",
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=job_id,
        request_id=request_id,
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=50,
        output_tokens=25,
        cache_tokens=None,
        measurement="estimated",
        price_usd="0.002",
    )
    assert res["status"] == "applied"

    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cache_tokens, tokens, measurement FROM omp_jobs.usage_events WHERE usage_id=%s",
            (res["usage_id"],),
        ).fetchone()
        assert row["input_tokens"] == 50
        assert row["output_tokens"] == 25
        assert row["cache_tokens"] is None
        assert row["tokens"] is None
        assert row["measurement"] == "estimated"


def test_invalid_request_validations(native_jobs) -> None:
    """Invalid token values, measurements, or prices are rejected with invalid_request."""
    store = _store(native_jobs)
    work_id = _work_id(native_jobs)

    base = {
        "store": store,
        "operation_id": f"op-val-{uuid4()}",
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "work_id": work_id,
        "job_id": None,
        "request_id": f"req-{uuid4()}",
        "role": "coder",
        "model": "gemini-3.8-flash",
        "input_tokens": 10,
        "output_tokens": 10,
        "cache_tokens": 0,
        "measurement": "measured",
        "price_usd": "0.01",
    }

    # Negative tokens
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "input_tokens": -5})
    assert err.value.code == "invalid_request"

    # Boolean token count
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "output_tokens": True})  # type: ignore[arg-type]
    assert err.value.code == "invalid_request"

    # Invalid measurement
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "measurement": "guessed"})
    assert err.value.code == "invalid_request"

    # Non-decimal price
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "price_usd": "not-a-number"})
    assert err.value.code == "invalid_request"

    # Float price
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "price_usd": 0.05})  # type: ignore[arg-type]
    assert err.value.code == "invalid_request"

    # Negative price
    with pytest.raises(JobError) as err:
        record_usage(**{**base, "price_usd": "-0.01"})
    assert err.value.code == "invalid_request"
