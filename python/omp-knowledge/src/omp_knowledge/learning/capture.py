from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from omp_work.v1.api_models import DomainEventsPage
from omp_work.v1.canonical import canonical_json

from .generation import GeneratorError, LessonGenerator
from .models import Attribution, RunStatus, SourceIdentity, StrictModel, UnitState
from .policy import NativeReceipts
from .proposals import create_proposal
from .store import LearningStore, compute_unit_id

DEFAULT_CAPTURE_TYPES = frozenset({"complete_work"})
DEFAULT_LIMIT = 50
DEFAULT_LEASE_SECONDS = 300
DROPPED_ERROR_CODE = "dropped"


@runtime_checkable
class NativeEvents(Protocol):
    """Read-only native domain-event stream seam (``WorkClient`` fits)."""

    def events(self, after_sequence: int, limit: int) -> DomainEventsPage: ...


class UnitRecord(StrictModel):
    unit_id: str
    event_id: str
    state: UnitState
    attempts: int
    retryable: bool
    error_code: str | None = None
    proposal_ids: tuple[str, ...] = ()


class RunRecord(StrictModel):
    run_id: str
    workspace_id: str
    status: RunStatus
    units_total: int
    units_failed: int
    units_no_lesson: int
    proposals_accepted: int
    proposals_rejected: int
    started_at: str
    completed_at: str | None = None
    units: tuple[UnitRecord, ...] = ()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generator_attribution(generator: LessonGenerator) -> tuple[str, str]:
    """Resolve the model/profile a generator attributes its units and proposals to.

    The capture seam is :class:`LessonGenerator`, which carries no attribution,
    so it is read from the concrete generator. ``LocalChatGenerator`` keeps its
    ``_model``/``_profile`` private, so a public attribute is preferred and the
    private spelling is the fallback. Attribution is required: every queued unit
    row is written with a non-empty ``model``, ``profile``, and ``source_json``
    before generation runs.
    """
    model = getattr(generator, "model", None) or getattr(generator, "_model", None)
    profile = getattr(generator, "profile", None) or getattr(
        generator, "_profile", None
    )
    if not model or not profile:
        raise ValueError(
            "generator must expose non-empty model and profile attribution"
        )
    return str(model), str(profile)


def _read_cursor(store: LearningStore, workspace_id: str) -> int:
    row = store.execute(
        "SELECT last_sequence FROM capture_cursor WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return int(row["last_sequence"]) if row is not None else 0


def _event_source(workspace_id: str, event: Any) -> SourceIdentity:
    return SourceIdentity(
        workspace_id=workspace_id,
        event_id=str(event.event_id),
        event_sequence=int(event.sequence),
        aggregate_id=str(event.aggregate_id),
    )


def _event_trace(workspace_id: str, event: Any) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "event_id": str(event.event_id),
        "event_sequence": int(event.sequence),
        "event_type": event.event_type,
        "event_outcome": event.outcome,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "actor_id": str(event.actor_id),
        "actor_kind": event.actor_kind,
        "payload": event.payload,
    }


def _queue_events(
    store: LearningStore,
    workspace_id: str,
    page: DomainEventsPage,
    capture_types: Collection[str],
    model: str,
    profile: str,
) -> None:
    now = _utcnow().isoformat()
    with store.transaction() as conn:
        for event in page.events:
            if event.event_type not in capture_types:
                continue
            source = _event_source(workspace_id, event)
            conn.execute(
                """
                INSERT OR IGNORE INTO units (
                    unit_id, workspace_id, event_id, state, attempts, retryable,
                    error_code, lease_until, trace_json, model, profile,
                    source_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 0, 0, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compute_unit_id(workspace_id, event.event_id),
                    workspace_id,
                    str(event.event_id),
                    canonical_json(_event_trace(workspace_id, event)),
                    model,
                    profile,
                    source.model_dump_json(),
                    now,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO capture_cursor (workspace_id, last_sequence, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                last_sequence = excluded.last_sequence,
                updated_at = excluded.updated_at
            """,
            (workspace_id, int(page.next_after_sequence), now),
        )


def _drop_expired_units(
    store: LearningStore, workspace_id: str, now: datetime
) -> list[UnitRecord]:
    rows = store.execute(
        """
        SELECT unit_id, event_id, attempts, lease_until FROM units
        WHERE workspace_id = ? AND state = 'running' AND lease_until IS NOT NULL
        """,
        (workspace_id,),
    ).fetchall()
    expired: list[Any] = []
    for row in rows:
        try:
            lease = datetime.fromisoformat(str(row["lease_until"]))
        except ValueError:
            continue
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        if lease < now:
            expired.append(row)
    if not expired:
        return []
    records: list[UnitRecord] = []
    with store.transaction() as conn:
        for row in expired:
            unit_id = str(row["unit_id"])
            conn.execute(
                """
                UPDATE units SET state = 'failed', retryable = 1, error_code = ?,
                    lease_until = NULL, updated_at = ?
                WHERE unit_id = ? AND state = 'running'
                """,
                (DROPPED_ERROR_CODE, now.isoformat(), unit_id),
            )
            records.append(
                UnitRecord(
                    unit_id=unit_id,
                    event_id=str(row["event_id"]),
                    state="failed",
                    attempts=int(row["attempts"]),
                    retryable=True,
                    error_code=DROPPED_ERROR_CODE,
                )
            )
    return records


def _queued_rows(
    store: LearningStore, workspace_id: str, unit_ids: Sequence[str] | None = None
) -> list[Any]:
    if unit_ids is not None:
        if not unit_ids:
            return []
        return list(
            store.execute(
                """
                SELECT * FROM units
                WHERE state = 'queued' AND unit_id IN (SELECT value FROM json_each(?))
                ORDER BY created_at, unit_id
                """,
                (json.dumps([str(u) for u in unit_ids]),),
            ).fetchall()
        )
    return list(
        store.execute(
            """
            SELECT * FROM units
            WHERE workspace_id = ? AND state = 'queued'
            ORDER BY created_at, unit_id
            """,
            (workspace_id,),
        ).fetchall()
    )


def _mark_failed(
    store: LearningStore,
    unit_id: str,
    *,
    error_code: str,
    retryable: bool,
    now: datetime,
) -> None:
    store.execute(
        """
        UPDATE units SET state = 'failed', retryable = ?, error_code = ?,
            lease_until = NULL, updated_at = ?
        WHERE unit_id = ?
        """,
        (1 if retryable else 0, error_code, now.isoformat(), unit_id),
    )


def _mark_finished(
    store: LearningStore,
    unit_id: str,
    *,
    state: UnitState,
    model: str,
    profile: str,
    now: datetime,
) -> None:
    store.execute(
        """
        UPDATE units SET state = ?, model = ?, profile = ?, error_code = NULL,
            lease_until = NULL, updated_at = ?
        WHERE unit_id = ?
        """,
        (state, model, profile, now.isoformat(), unit_id),
    )


def _process_units(
    store: LearningStore,
    receipts: NativeReceipts,
    generator: LessonGenerator,
    rows: Sequence[Any],
    *,
    lease_seconds: int,
) -> tuple[list[UnitRecord], int, int]:
    lease_until = (_utcnow() + timedelta(seconds=lease_seconds)).isoformat()
    records: list[UnitRecord] = []
    accepted = 0
    rejected = 0

    for row in rows:
        unit_id = str(row["unit_id"])
        claimed = store.execute(
            """
            UPDATE units SET state = 'running', attempts = attempts + 1,
                lease_until = ?, updated_at = ?
            WHERE unit_id = ? AND state = 'queued'
            """,
            (lease_until, _utcnow().isoformat(), unit_id),
        )
        if claimed.rowcount == 0:
            continue

        unit = store.execute(
            "SELECT * FROM units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        trace = json.loads(unit["trace_json"])
        source = SourceIdentity.model_validate_json(unit["source_json"])
        event_id = str(unit["event_id"])
        attempts = int(unit["attempts"])

        try:
            result = generator.generate(trace)
        except GeneratorError as exc:
            error_code = str(exc.error_code)
            retryable = bool(getattr(exc, "retryable", True))
            _mark_failed(
                store,
                unit_id,
                error_code=error_code,
                retryable=retryable,
                now=_utcnow(),
            )
            records.append(
                UnitRecord(
                    unit_id=unit_id,
                    event_id=event_id,
                    state="failed",
                    attempts=attempts,
                    retryable=retryable,
                    error_code=error_code,
                )
            )
            continue

        if not result.lessons:
            _mark_finished(
                store,
                unit_id,
                state="no_lesson",
                model=result.model,
                profile=result.profile,
                now=_utcnow(),
            )
            records.append(
                UnitRecord(
                    unit_id=unit_id,
                    event_id=event_id,
                    state="no_lesson",
                    attempts=attempts,
                    retryable=False,
                )
            )
            continue

        attribution = Attribution(
            model=result.model, profile=result.profile, source=source
        )
        proposal_ids: list[str] = []
        for lesson in result.lessons:
            proposal = create_proposal(
                store,
                unit_id=unit_id,
                lesson=lesson,
                attribution=attribution,
                reader=receipts,
            )
            proposal_ids.append(proposal.proposal_id)
            if proposal.accepted:
                accepted += 1
            else:
                rejected += 1

        _mark_finished(
            store,
            unit_id,
            state="succeeded",
            model=result.model,
            profile=result.profile,
            now=_utcnow(),
        )
        records.append(
            UnitRecord(
                unit_id=unit_id,
                event_id=event_id,
                state="succeeded",
                attempts=attempts,
                retryable=False,
                proposal_ids=tuple(proposal_ids),
            )
        )

    return records, accepted, rejected


def _status(units_total: int, units_failed: int, proposals_total: int) -> RunStatus:
    if units_total > 0 and units_failed == units_total:
        return "failed"
    if units_failed > 0:
        return "partial"
    if proposals_total > 0:
        return "succeeded"
    return "no_lesson"


def _persist_run(store: LearningStore, record: RunRecord) -> None:
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, workspace_id, status, units_total, units_failed,
                units_no_lesson, proposals_accepted, proposals_rejected,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                record.workspace_id,
                record.status,
                record.units_total,
                record.units_failed,
                record.units_no_lesson,
                record.proposals_accepted,
                record.proposals_rejected,
                record.started_at,
                record.completed_at,
            ),
        )


def _finalize_run(
    store: LearningStore,
    *,
    workspace_id: str,
    started_at: str,
    records: Sequence[UnitRecord],
    proposals_accepted: int,
    proposals_rejected: int,
) -> RunRecord:
    units_failed = sum(1 for record in records if record.state == "failed")
    units_no_lesson = sum(1 for record in records if record.state == "no_lesson")
    units_total = len(records)
    record = RunRecord(
        run_id=str(uuid4()),
        workspace_id=workspace_id,
        status=_status(
            units_total, units_failed, proposals_accepted + proposals_rejected
        ),
        units_total=units_total,
        units_failed=units_failed,
        units_no_lesson=units_no_lesson,
        proposals_accepted=proposals_accepted,
        proposals_rejected=proposals_rejected,
        started_at=started_at,
        completed_at=_utcnow().isoformat(),
        units=tuple(records),
    )
    if workspace_id:
        _persist_run(store, record)
    return record


def drain(
    store: LearningStore,
    events: NativeEvents,
    receipts: NativeReceipts,
    generator: LessonGenerator,
    *,
    workspace_id: str | UUID,
    capture_types: Collection[str] = DEFAULT_CAPTURE_TYPES,
    limit: int = DEFAULT_LIMIT,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> RunRecord:
    """Bounded capture: page native events from the cursor to the watermark,
    queue one unit per matching event, expire dropped leases, then turn each
    queued unit's lessons into proposals.
    """
    workspace = str(workspace_id)
    started_at = _utcnow().isoformat()
    model, profile = generator_attribution(generator)

    after = _read_cursor(store, workspace)
    page = events.events(after, limit)
    _queue_events(store, workspace, page, capture_types, model, profile)
    records = _drop_expired_units(store, workspace, _utcnow())

    rows = _queued_rows(store, workspace)
    processed, accepted, rejected = _process_units(
        store, receipts, generator, rows, lease_seconds=lease_seconds
    )
    records.extend(processed)
    return _finalize_run(
        store,
        workspace_id=workspace,
        started_at=started_at,
        records=records,
        proposals_accepted=accepted,
        proposals_rejected=rejected,
    )


def retry(
    store: LearningStore,
    receipts: NativeReceipts,
    generator: LessonGenerator,
    *,
    unit_ids: Sequence[str | UUID] | None = None,
) -> RunRecord:
    """Requeue failed retryable units and process them with the recovered generator."""
    started_at = _utcnow().isoformat()
    generator_attribution(generator)

    if unit_ids is None:
        candidates = list(
            store.execute(
                """
                SELECT * FROM units
                WHERE state = 'failed' AND retryable = 1
                ORDER BY created_at, unit_id
                """
            ).fetchall()
        )
    else:
        ids = [str(unit_id) for unit_id in unit_ids]
        if not ids:
            candidates = []
        else:
            candidates = list(
                store.execute(
                    """
                    SELECT * FROM units
                    WHERE state = 'failed' AND retryable = 1
                        AND unit_id IN (SELECT value FROM json_each(?))
                    ORDER BY created_at, unit_id
                    """,
                    (json.dumps(ids),),
                ).fetchall()
            )

    workspace_id = str(candidates[0]["workspace_id"]) if candidates else ""
    now = _utcnow().isoformat()
    requeued_ids: list[str] = []
    with store.transaction() as conn:
        for row in candidates:
            unit_id = str(row["unit_id"])
            cursor = conn.execute(
                """
                UPDATE units SET state = 'queued', retryable = 0, error_code = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE unit_id = ? AND state = 'failed' AND retryable = 1
                """,
                (now, unit_id),
            )
            if cursor.rowcount:
                requeued_ids.append(unit_id)

    rows = _queued_rows(store, workspace_id, requeued_ids)
    records, accepted, rejected = _process_units(
        store,
        receipts,
        generator,
        rows,
        lease_seconds=DEFAULT_LEASE_SECONDS,
    )
    return _finalize_run(
        store,
        workspace_id=workspace_id,
        started_at=started_at,
        records=records,
        proposals_accepted=accepted,
        proposals_rejected=rejected,
    )


__all__ = [
    "DEFAULT_CAPTURE_TYPES",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_LIMIT",
    "DROPPED_ERROR_CODE",
    "NativeEvents",
    "RunRecord",
    "UnitRecord",
    "drain",
    "generator_attribution",
    "retry",
]
