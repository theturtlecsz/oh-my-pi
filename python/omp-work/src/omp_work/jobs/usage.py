"""Stable usage ids in WP5 accounting (omp_jobs.usage_events, R03, OMP-324).

Records usage events idempotently with stable usage_id hashing, outbox ledger
delivery, and automatic step_index assignment.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from omp_work.contracts.v1.recovery import CloseoutRecord, apply_commit
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.v1.canonical import sha256

_VALID_MEASUREMENTS = frozenset({"measured", "estimated", "unknown"})

__all__ = ["record_usage"]


def record_usage(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID | str,
    actor_id: UUID | str,
    work_id: UUID | str,
    job_id: str | None = None,
    request_id: str,
    role: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_tokens: int | None,
    measurement: str,
    price_usd: str | Decimal | None,
    price_version: str | None = None,
) -> dict[str, object]:
    """Record one usage event in ``omp_jobs.usage_events`` and ``omp_jobs.outbox``.

    Usage ids are stable sha256 hashes of (workspace_id, work_id, job_id, request_id).
    Retries under the same or new operation_id replay without writing duplicates or
    incrementing step_index. Discrepancies on existing usage ids raise idempotency_conflict.
    """
    if isinstance(workspace_id, str):
        try:
            workspace_id = UUID(workspace_id)
        except ValueError:
            raise JobError("invalid_request", ("workspace_id must be a UUID",))
    elif not isinstance(workspace_id, UUID):
        raise JobError("invalid_request", ("workspace_id must be a UUID",))

    if isinstance(actor_id, str):
        try:
            actor_id = UUID(actor_id)
        except ValueError:
            raise JobError("invalid_request", ("actor_id must be a UUID",))
    elif not isinstance(actor_id, UUID):
        raise JobError("invalid_request", ("actor_id must be a UUID",))

    if isinstance(work_id, str):
        try:
            work_id = UUID(work_id)
        except ValueError:
            raise JobError("invalid_request", ("work_id must be a UUID",))
    elif not isinstance(work_id, UUID):
        raise JobError("invalid_request", ("work_id must be a UUID",))

    if not isinstance(operation_id, str) or not operation_id:
        raise JobError("invalid_request", ("operation_id is required",))

    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        raise JobError("invalid_request", ("job_id must be a non-empty string or None",))

    if not isinstance(request_id, str) or not request_id:
        raise JobError("invalid_request", ("request_id is required",))

    if not isinstance(role, str) or not role:
        raise JobError("invalid_request", ("role is required",))

    if not isinstance(model, str) or not model:
        raise JobError("invalid_request", ("model is required",))

    if price_version is not None and (
        not isinstance(price_version, str) or not price_version
    ):
        raise JobError("invalid_request", ("price_version must be a string or None",))

    if measurement not in _VALID_MEASUREMENTS:
        raise JobError(
            "invalid_request",
            ("measurement must be measured, estimated, or unknown",),
        )

    for token_name, count in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cache_tokens", cache_tokens),
    ):
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            raise JobError(
                "invalid_request",
                (f"{token_name} must be an integer >= 0 or None",),
            )

    if measurement == "unknown" and any(
        c is not None for c in (input_tokens, output_tokens, cache_tokens)
    ):
        raise JobError(
            "invalid_request",
            ("unknown measurement requires all token counts to be None",),
        )

    price_str: str | None
    price_val: Decimal | None
    if price_usd is not None:
        if isinstance(price_usd, bool) or not isinstance(price_usd, (str, Decimal)):
            raise JobError(
                "invalid_request",
                ("price_usd must be a decimal string or None",),
            )
        try:
            price_val = Decimal(str(price_usd))
        except (InvalidOperation, TypeError):
            raise JobError(
                "invalid_request",
                ("price_usd must be a valid decimal string",),
            )
        if not price_val.is_finite() or price_val < 0:
            raise JobError(
                "invalid_request",
                ("price_usd must be a non-negative finite decimal",),
            )
        price_str = str(price_usd)
    else:
        price_str = None
        price_val = None

    usage_id = sha256(
        {
            "workspace_id": str(workspace_id),
            "work_id": str(work_id),
            "job_id": job_id,
            "request_id": request_id,
        }
    )

    conversation_id = str(job_id or work_id)
    source_file = "omp_jobs:native"

    if input_tokens is not None and output_tokens is not None and cache_tokens is not None:
        tokens: int | None = input_tokens + output_tokens + cache_tokens
    else:
        tokens = None

    request: dict[str, object] = {
        "workspace_id": str(workspace_id),
        "work_id": str(work_id),
        "job_id": job_id,
        "request_id": request_id,
        "role": role,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "measurement": measurement,
        "price_usd": price_str,
        "price_version": price_version,
    }

    def _matches_existing(row: dict[str, Any]) -> bool:
        row_price = row.get("price_usd")
        if (row_price is None) != (price_val is None):
            return False
        if (
            row_price is not None
            and price_val is not None
            and Decimal(str(row_price)) != price_val
        ):
            return False

        row_job = row.get("job_id_derived")
        if row_job != job_id:
            return False

        row_version = row.get("price_version")
        if row_version != price_version:
            return False

        return bool(
            UUID(str(row["workspace_id"])) == workspace_id
            and UUID(str(row["work_id"])) == work_id
            and row.get("request_id") == request_id
            and row.get("role_derived") == role
            and row.get("model") == model
            and row.get("input_tokens") == input_tokens
            and row.get("output_tokens") == output_tokens
            and row.get("cache_tokens") == cache_tokens
            and row.get("measurement") == measurement
        )

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            if job_id is not None:
                cur.execute(
                    "SELECT work_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                    (workspace_id, job_id),
                )
                job_row = cur.fetchone()
                if job_row is None:
                    raise JobError("invalid_request", ("job not found in workspace",))
                if job_row["work_id"] is None or UUID(str(job_row["work_id"])) != work_id:
                    raise JobError(
                        "invalid_request",
                        ("job does not belong to the specified work item",),
                    )

            cur.execute(
                """
                SELECT workspace_id, work_id, request_id, role_derived, job_id_derived,
                       model, input_tokens, output_tokens, cache_tokens, measurement,
                       price_usd, price_version
                FROM omp_jobs.usage_events
                WHERE usage_id=%s
                FOR UPDATE
                """,
                (usage_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if _matches_existing(existing):
                    return {"status": "replayed", "usage_id": usage_id}
                raise JobError(
                    "idempotency_conflict",
                    ("usage identity already recorded with different fields",),
                )

            cur.execute(
                """
                SELECT COALESCE(MAX(step_index), 0) + 1 AS next_step
                FROM omp_jobs.usage_events
                WHERE conversation_id=%s AND source_file=%s
                """,
                (conversation_id, source_file),
            )
            step_row = cur.fetchone()
            step_index = int(step_row["next_step"]) if step_row is not None else 1

            at = time.time()
            recorded_at = datetime.fromtimestamp(at, timezone.utc)

            ledger_event: dict[str, object] = {
                "kind": "job_usage",
                "usage_id": usage_id,
                "conversation_id": conversation_id,
                "step_index": step_index,
                "role": role,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_tokens": cache_tokens,
                "measurement": measurement,
                "price_usd": price_str,
                "price_version": price_version,
                "at": at,
            }

            try:
                cur.execute(
                    """
                    INSERT INTO omp_jobs.usage_events(
                        conversation_id, step_index, source_file, tokens,
                        role_derived, job_id_derived, provider_derived,
                        recorded_at, payload, usage_id, workspace_id, work_id,
                        request_id, model, input_tokens, output_tokens, cache_tokens,
                        measurement, price_usd, price_version
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        conversation_id,
                        step_index,
                        source_file,
                        tokens,
                        role,
                        job_id,
                        None,
                        recorded_at,
                        Jsonb(ledger_event),
                        usage_id,
                        workspace_id,
                        work_id,
                        request_id,
                        model,
                        input_tokens,
                        output_tokens,
                        cache_tokens,
                        measurement,
                        price_val,
                        price_version,
                    ),
                )
            except UniqueViolation as error:
                cur.execute(
                    """
                    SELECT workspace_id, work_id, request_id, role_derived, job_id_derived,
                           model, input_tokens, output_tokens, cache_tokens, measurement,
                           price_usd, price_version
                    FROM omp_jobs.usage_events
                    WHERE usage_id=%s
                    """,
                    (usage_id,),
                )
                raced = cur.fetchone()
                if raced is not None and _matches_existing(raced):
                    return {"status": "replayed", "usage_id": usage_id}
                raise JobError(
                    "idempotency_conflict",
                    ("usage identity already recorded",),
                ) from error

            event_id = f"usage:{usage_id}"
            closeout = apply_commit(CloseoutRecord(event_id, "open"))
            cur.execute(
                """
                INSERT INTO omp_jobs.outbox(
                    event_id, workspace_id, operation_id, kind, payload, state, revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id,
                    workspace_id,
                    operation_id,
                    "usage_ledger",
                    Jsonb(ledger_event),
                    closeout.state,
                    closeout.revision,
                ),
            )
            return {"status": "applied", "usage_id": usage_id}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "usage_record", request, apply
        )

    status = (
        "replayed"
        if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
        else "applied"
    )
    return {
        "operation_id": operation_id,
        "status": status,
        "usage_id": outcome.result.get("usage_id", usage_id),
    }
