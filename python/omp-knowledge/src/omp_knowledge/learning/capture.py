from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from omp_work.v1.api_models import DomainEventsPage
from omp_work.v1.canonical import canonical_json

from .generation import (
    GeneratorError,
    LessonGenerator,
    ensure_repository_precondition,
    extract_trace_repository,
)
from .models import Attribution, RunStatus, SourceIdentity, StrictModel, UnitState
from .policy import NativeReceipts
from .proposals import create_proposal
from .lifecycle import sweep
from .record import (
    NativeRecords,
    bounded_capture_records,
)
from .store import LearningStore, compute_unit_id

DEFAULT_CAPTURE_TYPES = frozenset({"complete_work", "complete_execution_item"})
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
    model: str | None = None
    profile: str | None = None
    event_sequence: int | None = None


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


class _MemoRecords:
    """Reuse successful work-item and workflow reads for the rest of one drain.

    The sweep that follows admission resolves the same keys the queue just
    read. Capture tests assert the exact read list of that queue, so a second
    live call would fail them. Failures are not cached: a transport error must
    still surface on a later call. ``research`` is exposed only when the inner
    seam has it, because a records double without ``research`` must stay that way.
    """

    def __init__(self, inner: NativeRecords) -> None:
        self._inner = inner
        self._pages: dict[tuple[Any, Any], Any] = {}
        self._workflows: dict[str, Any] = {}
        self._research_pages: dict[str, Any] = {}
        research = getattr(inner, "research", None)
        if callable(research):
            self._research_fn = research
            self.research = self._research

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> Any:
        key = (after, limit)
        if key not in self._pages:
            self._pages[key] = self._inner.work_items(after=after, limit=limit)
        return self._pages[key]

    def workflow(self, key: str) -> Any:
        if key not in self._workflows:
            self._workflows[key] = self._inner.workflow(key)
        return self._workflows[key]

    def _research(self, key: str) -> Any:
        if key not in self._research_pages:
            self._research_pages[key] = self._research_fn(key)
        return self._research_pages[key]


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


def _unit_event_sequence(row: Any) -> int | None:
    """The source domain-event sequence recorded on a unit row, if readable.

    Every queued unit writes ``source_json``, so a run that reports units which
    never reached the generator (dropped leases) can still name the exact source
    event it came from.
    """
    raw = row["source_json"]
    if not raw:
        return None
    try:
        source = SourceIdentity.model_validate_json(raw)
    except ValueError:
        return None
    return source.event_sequence


def _event_source(workspace_id: str, event: Any) -> SourceIdentity:
    return SourceIdentity(
        workspace_id=workspace_id,
        event_id=str(event.event_id),
        event_sequence=int(event.sequence),
        aggregate_id=str(event.aggregate_id),
    )


def _event_trace(
    workspace_id: str,
    event: Any,
    work_record: dict[str, Any] | None = None,
    research_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The trace one unit sends to the generator.

    The event alone carries no execution evidence (a ``complete_work`` or
    ``complete_execution_item`` payload names ``work_id`` but not the item's
    execution record), so the finished item's own bounded WorkService record is
    attached when it could be read. ``work_record`` is present-but-``null`` when
    the read was unavailable: absence of evidence is visible to the generator
    instead of silently looking like an empty trace.
    Concluded research campaigns are attached as ``research_record`` when
    present, bounded in size and references.
    """
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
        "work_record": work_record,
        "research_record": research_record,
    }


def _present_identifier(value: Any) -> str | None:
    """A non-empty id as text. Empty, null, and other false values are absent."""
    if isinstance(value, str):
        return value if value else None
    if value:
        return str(value)
    return None


def _trace_event_work_id(trace: Mapping[str, Any]) -> str | None:
    """Payload ``work_id`` when that value is non-empty, else the aggregate id.

    A payload work id that differs from ``aggregate_id`` is the event work id.
    That difference is not a conflict.
    """
    payload = trace.get("payload")
    if isinstance(payload, dict):
        work_id = _present_identifier(payload.get("work_id"))
        if work_id is not None:
            return work_id
    return _present_identifier(trace.get("aggregate_id"))


def _event_work_id(event: Any) -> str:
    """The work item a captured event is about: payload ``work_id`` or the aggregate."""
    work_id = _trace_event_work_id(
        {
            "payload": event.payload if isinstance(event.payload, dict) else {},
            "aggregate_id": str(event.aggregate_id),
        }
    )
    return work_id if work_id is not None else str(event.aggregate_id)


def _record_member_id(
    work_record: Mapping[str, Any], group: str, key: str
) -> str | None:
    nested = work_record.get(group)
    if not isinstance(nested, dict):
        return None
    return _present_identifier(nested.get(key))


def _binding_ids(trace: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """``work_id``, ``candidate_id``, and ``revision_id`` copied from one trace.

    Candidate and revision come only from ``work_record["candidate"]["candidate_id"]``
    and ``work_record["revision"]["revision_id"]``, and only when ``work_record``
    is a dict whose ``work_id`` equals the event work id. A cited receipt is
    never a source for either id.

    A conflict is both sides present and different: the record work id versus
    the event work id, ``payload["candidate_id"]`` versus the record candidate,
    or ``payload["revision_id"]`` versus the record revision. On conflict the
    stored candidate and revision are null. A payload that omits ``candidate_id``
    is not a conflict.
    """
    event_work_id = _trace_event_work_id(trace)
    work_record = trace.get("work_record")
    if not isinstance(work_record, dict):
        return event_work_id, None, None
    record_work_id = _present_identifier(work_record.get("work_id"))
    if (
        record_work_id is None
        or event_work_id is None
        or record_work_id != event_work_id
    ):
        return event_work_id, None, None
    candidate_id = _record_member_id(work_record, "candidate", "candidate_id")
    revision_id = _record_member_id(work_record, "revision", "revision_id")
    payload = trace.get("payload")
    payload_map = payload if isinstance(payload, dict) else {}
    payload_candidate = (
        _present_identifier(payload_map.get("candidate_id"))
        if "candidate_id" in payload_map
        else None
    )
    payload_revision = (
        _present_identifier(payload_map.get("revision_id"))
        if "revision_id" in payload_map
        else None
    )
    candidate_conflict = (
        payload_candidate is not None
        and candidate_id is not None
        and payload_candidate != candidate_id
    )
    revision_conflict = (
        payload_revision is not None
        and revision_id is not None
        and payload_revision != revision_id
    )
    if candidate_conflict or revision_conflict:
        return event_work_id, None, None
    return event_work_id, candidate_id, revision_id


def _source_with_binding(source: SourceIdentity, trace: Any) -> SourceIdentity:
    """Keep the event identity and set the three binding ids from ``trace``."""
    if not isinstance(trace, Mapping):
        work_id, candidate_id, revision_id = None, None, None
    else:
        work_id, candidate_id, revision_id = _binding_ids(trace)
    return source.model_copy(
        update={
            "work_id": work_id,
            "candidate_id": candidate_id,
            "revision_id": revision_id,
        }
    )


def _queue_events(
    store: LearningStore,
    workspace_id: str,
    page: DomainEventsPage,
    capture_types: Collection[str],
    model: str,
    profile: str,
    records: NativeRecords | None = None,
) -> None:
    now = _utcnow().isoformat()
    enriched: list[tuple[Any, SourceIdentity, str]] = []
    for event in page.events:
        if event.event_type not in capture_types:
            continue
        source = _event_source(workspace_id, event)
        if records is not None:
            work_record, research_record = bounded_capture_records(
                records, _event_work_id(event)
            )
        else:
            work_record, research_record = None, None
        trace = _event_trace(
            workspace_id,
            event,
            work_record=work_record,
            research_record=research_record,
        )
        trace_json = canonical_json(trace)
        source = _source_with_binding(source, json.loads(trace_json))
        enriched.append((event, source, trace_json))

    with store.transaction() as conn:
        for event, source, trace_json in enriched:
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
                    trace_json,
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
        SELECT unit_id, event_id, attempts, lease_until, model, profile,
               source_json FROM units
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
                    model=str(row["model"]),
                    profile=str(row["profile"]),
                    event_sequence=_unit_event_sequence(row),
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
        # The queued source can be overwritten before retry. Recompute the three
        # ids from the stored trace; retry makes no NativeRecords call.
        source = _source_with_binding(
            SourceIdentity.model_validate_json(unit["source_json"]),
            trace,
        )
        bound_source_json = source.model_dump_json()
        if bound_source_json != unit["source_json"]:
            store.execute(
                """
                UPDATE units SET source_json = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (bound_source_json, _utcnow().isoformat(), unit_id),
            )
        event_id = str(unit["event_id"])
        attempts = int(unit["attempts"])
        # The queued row's attribution is what the unit was captured under; a
        # settled unit reports the attribution the generator answered with.
        captured_model = str(unit["model"])
        captured_profile = str(unit["profile"])
        base = {
            "unit_id": unit_id,
            "event_id": event_id,
            "attempts": attempts,
            "event_sequence": source.event_sequence,
        }

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
                    **base,
                    state="failed",
                    retryable=retryable,
                    error_code=error_code,
                    model=captured_model,
                    profile=captured_profile,
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
                    **base,
                    state="no_lesson",
                    retryable=False,
                    model=result.model,
                    profile=result.profile,
                )
            )
            continue

        repo = extract_trace_repository(trace)
        attribution = Attribution(
            model=result.model, profile=result.profile, source=source
        )
        proposal_ids: list[str] = []
        for lesson in result.lessons:
            if repo is not None:
                lesson = ensure_repository_precondition(lesson, repo)
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
                **base,
                state="succeeded",
                retryable=False,
                proposal_ids=tuple(proposal_ids),
                model=result.model,
                profile=result.profile,
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
    records: NativeRecords | None = None,
) -> RunRecord:
    """Bounded capture: page native events from the cursor to the watermark,
    queue one unit per matching event, expire dropped leases, then turn each
    queued unit's lessons into proposals.

    ``limit`` bounds the number of domain events scanned from the cursor in this
    drain — it is not a unit count, because most scanned events are not of a
    captured type. When ``records`` is supplied, each ``complete_work`` or
    ``complete_execution_item`` unit's trace carries that item's own bounded
    execution and research records before generation. The unit source copies
    work_id, candidate_id, and revision_id from that stored trace.
    ``_process_units`` sets those ids again from the trace, including on retry,
    which does not read records. After admissions, including a drain that queued
    nothing, every active lesson in the workspace is swept. Reads already made
    for this drain are reused so the sweep does not issue a second live call.
    """
    workspace = str(workspace_id)
    started_at = _utcnow().isoformat()
    model, profile = generator_attribution(generator)
    memo = _MemoRecords(records) if records is not None else None

    after = _read_cursor(store, workspace)
    page = events.events(after, limit)
    _queue_events(store, workspace, page, capture_types, model, profile, memo)
    dropped = _drop_expired_units(store, workspace, _utcnow())

    rows = _queued_rows(store, workspace)
    processed, accepted, rejected = _process_units(
        store, receipts, generator, rows, lease_seconds=lease_seconds
    )
    dropped.extend(processed)
    record = _finalize_run(
        store,
        workspace_id=workspace,
        started_at=started_at,
        records=dropped,
        proposals_accepted=accepted,
        proposals_rejected=rejected,
    )
    sweep(store, receipts, memo, workspace)
    return record


def retry(
    store: LearningStore,
    receipts: NativeReceipts,
    generator: LessonGenerator,
    *,
    unit_ids: Sequence[str | UUID] | None = None,
    records: NativeRecords | None = None,
    workspace_id: str | UUID | None = None,
) -> RunRecord:
    """Requeue failed retryable units and process them with the recovered generator.

    After admissions, including a retry that requeued nothing, active lessons in
    the workspace are swept. ``records`` defaults to ``None``: a retry that does
    not receive a records seam does not re-read the workflow. Pass ``workspace_id``
    to sweep that workspace even when no unit was requeued.
    """
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

    if workspace_id is not None:
        workspace = str(workspace_id)
    elif candidates:
        workspace = str(candidates[0]["workspace_id"])
    else:
        workspace = ""
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

    rows = _queued_rows(store, workspace, requeued_ids)
    processed, accepted, rejected = _process_units(
        store,
        receipts,
        generator,
        rows,
        lease_seconds=DEFAULT_LEASE_SECONDS,
    )
    record = _finalize_run(
        store,
        workspace_id=workspace,
        started_at=started_at,
        records=processed,
        proposals_accepted=accepted,
        proposals_rejected=rejected,
    )
    if workspace:
        sweep(store, receipts, records, workspace)
    return record


__all__ = [
    "DEFAULT_CAPTURE_TYPES",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_LIMIT",
    "DROPPED_ERROR_CODE",
    "NativeEvents",
    "NativeRecords",
    "RunRecord",
    "UnitRecord",
    "drain",
    "generator_attribution",
    "retry",
]
