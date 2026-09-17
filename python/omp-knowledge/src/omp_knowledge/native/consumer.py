from __future__ import annotations

import contextlib
import json
from typing import Any, Generator
from uuid import NAMESPACE_OID, UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from omp_work.knowledge_contracts import (
    ObservationKind,
    SourceObservation,
    SourceRef,
)
from omp_work.v1.canonical import sha256

from ..config import KnowledgeConfig
from ..storage.db import IdempotencyConflictError, store_observation


class NativeSourceUnavailableError(psycopg.OperationalError):
    """Raised when authoritative native database connection or query fails."""

    def __init__(
        self,
        message: str = "native_source_unavailable: authoritative native database unavailable",
    ) -> None:
        super().__init__(message)


def deterministic_event_observation_id(workspace_id: UUID, event_id: UUID) -> UUID:
    """Observation identity using existing valid UUID namespace helper (NAMESPACE_OID pattern),
    deterministic from workspace + event_id.
    """
    key = f"omp-event:{workspace_id}:{event_id}"
    return uuid5(NAMESPACE_OID, key)


@contextlib.contextmanager
def native_readonly_session(
    config: KnowledgeConfig,
    workspace_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> Generator[psycopg.Connection, None, None]:
    """Read-only native database session enforcing default_transaction_read_only=on
    and RLS claims for workspace and actor. Native DB is read-only.
    """
    try:
        conn = psycopg.connect(
            config.native_pg_connection_string(),
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
            autocommit=True,
        )
    except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
        raise NativeSourceUnavailableError(str(exc)) from exc
    try:
        if workspace_id is not None or actor_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                    (str(workspace_id) if workspace_id else "", str(actor_id) if actor_id else ""),
                )
    except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
        conn.close()
        raise NativeSourceUnavailableError(str(exc)) from exc
    try:
        yield conn
    finally:
        conn.close()


def _covers(ranges: list[list[Any]], seq: int) -> bool:
    """Returns True if seq is contained in any range of unresolved_ranges."""
    return any(r[0] <= seq <= r[1] for r in ranges)


def _normalize_ranges(ranges: list[list[Any]]) -> list[list[Any]]:
    """Normalizes unresolved_ranges into sorted, non-overlapping ranges.
    Adjacent ranges are merged with xmax_bound = max of the two when both have bounds,
    or None when both are None. A bounded range adjacent to an unbounded range is not
    merged to prevent demoting scanned bounds. When ranges strictly overlap, null wins as unbounded.
    """
    clean_ranges: list[list[Any]] = []
    for r in ranges:
        if not r or len(r) < 2:
            continue
        try:
            lo = int(r[0])
            hi = int(r[1])
        except (ValueError, TypeError):
            continue
        bound = int(r[2]) if len(r) > 2 and r[2] is not None else None
        if lo <= hi:
            clean_ranges.append([lo, hi, bound])

    if not clean_ranges:
        return []

    clean_ranges.sort(key=lambda x: x[0])
    merged: list[list[Any]] = [clean_ranges[0]]

    for curr in clean_ranges[1:]:
        prev = merged[-1]
        if curr[0] <= prev[1]:
            # Overlapping ranges: merge them, null wins as unbounded
            prev[1] = max(prev[1], curr[1])
            if prev[2] is None or curr[2] is None:
                prev[2] = None
            else:
                prev[2] = max(prev[2], curr[2])
        elif curr[0] == prev[1] + 1:
            # Adjacent ranges:
            if prev[2] is not None and curr[2] is not None:
                prev[1] = curr[1]
                prev[2] = max(prev[2], curr[2])
            elif prev[2] is None and curr[2] is None:
                prev[1] = curr[1]
                prev[2] = None
            else:
                merged.append(curr)
        else:
            merged.append(curr)

    return merged


def _carve_range_from_ranges(ranges: list[list[Any]], win_lo: int, win_hi: int) -> list[list[Any]]:
    result: list[list[Any]] = []
    for r in ranges:
        lo, hi, bound = r[0], r[1], r[2]
        if win_hi < lo or win_lo > hi:
            result.append([lo, hi, bound])
        elif win_lo <= lo and win_hi >= hi:
            continue
        elif win_lo <= lo and win_hi < hi:
            result.append([win_hi + 1, hi, bound])
        elif win_lo > lo and win_hi >= hi:
            result.append([lo, win_lo - 1, bound])
        else:
            result.append([lo, win_lo - 1, bound])
            result.append([win_hi + 1, hi, bound])
    return _normalize_ranges(result)


def _carve_sequence_from_ranges(ranges: list[list[Any]], seq: int) -> list[list[Any]]:
    return _carve_range_from_ranges(ranges, seq, seq)


def _assign_bound_to_range_window(
    ranges: list[list[Any]],
    win_lo: int,
    win_hi: int,
    bound: int,
) -> list[list[Any]]:
    result: list[list[Any]] = []
    for r in ranges:
        lo, hi, b = r[0], r[1], r[2]
        if win_hi < lo or win_lo > hi:
            result.append([lo, hi, b])
        elif win_lo <= lo and win_hi >= hi:
            new_b = bound if b is None else b
            result.append([lo, hi, new_b])
        elif win_lo <= lo and win_hi < hi:
            new_b = bound if b is None else b
            result.append([lo, win_hi, new_b])
            result.append([win_hi + 1, hi, b])
        elif win_lo > lo and win_hi >= hi:
            result.append([lo, win_lo - 1, b])
            new_b = bound if b is None else b
            result.append([win_lo, hi, new_b])
        else:
            result.append([lo, win_lo - 1, b])
            new_b = bound if b is None else b
            result.append([win_lo, win_hi, new_b])
            result.append([win_hi + 1, hi, b])
    return _normalize_ranges(result)


def _fold_legacy_gaps(gap_metadata: dict[str, Any]) -> list[list[Any]]:
    unresolved = list(gap_metadata.get("unresolved_ranges") or [])
    legacy_gaps: set[int] = set()
    if "expired_gaps" in gap_metadata:
        for x in gap_metadata.pop("expired_gaps") or []:
            try:
                legacy_gaps.add(int(x))
            except (ValueError, TypeError):
                pass
    if "dropped_gaps" in gap_metadata:
        for x in gap_metadata.pop("dropped_gaps") or []:
            try:
                legacy_gaps.add(int(x))
            except (ValueError, TypeError):
                pass
    if legacy_gaps:
        for g in sorted(legacy_gaps):
            unresolved.append([g, g, None])
        unresolved = _normalize_ranges(unresolved)
    return unresolved


class NativeEventConsumer:
    """Consumes native domain events from omp_audit.domain_events with sequence cursor,
    deterministic observation identity, and explicit pending gap reconciliation for out-of-order commits.
    Native DB is read only; consumer writes knowledge DB only.

    Sequence is a global bigserial allocated inside the writer's INSERT; RLS hides foreign rows;
    there is no native committed frontier; finality is derived only from pg_current_snapshot() and
    requires the allocator to hold an xid by the cycle after detection (true for omp_work/v1/store.py
    _record_event, which writes earlier in the same transaction, and for any single-statement INSERT).
    """

    def __init__(
        self,
        config: KnowledgeConfig,
        workspace_id: UUID,
        actor_id: UUID,
        repository_id: UUID,
        consumer_name: str = "native_event_consumer",
        batch_size: int = 100,
        gap_horizon_cycles: int = 2,
        gap_visibility_horizon: int = 1000,
        max_pending_gaps: int = 1000,
        reconcile_span: int = 10_000,
        max_reconcile_queries: int = 16,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        self.repository_id = repository_id
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.gap_horizon_cycles = kwargs.get("max_gap_cycles", gap_horizon_cycles)
        self.gap_visibility_horizon = kwargs.get("max_gap_horizon", gap_visibility_horizon)
        self.max_pending_gaps = max_pending_gaps
        self.reconcile_span = reconcile_span
        self.max_reconcile_queries = max_reconcile_queries

    @property
    def checkpoint_key(self) -> str:
        """Derive internal workspace-scoped checkpoint key from consumer_name and workspace UUID."""
        suffix = f":{self.workspace_id}"
        if self.consumer_name.endswith(suffix):
            return self.consumer_name
        return f"{self.consumer_name}:{self.workspace_id}"

    def get_checkpoint(self, knowledge_conn: psycopg.Connection) -> tuple[int, list[int], list[str]]:
        after_seq, gaps, applied, _ = self.get_checkpoint_state(knowledge_conn)
        return after_seq, gaps, applied

    def get_checkpoint_state(
        self, knowledge_conn: psycopg.Connection
    ) -> tuple[int, list[int], list[str], dict[str, Any]]:
        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT after_sequence, pending_gaps, applied_event_ids, pending_gap_metadata
                FROM omp_knowledge.native_event_checkpoints
                WHERE consumer = %s
                FOR UPDATE
                """,
                (self.checkpoint_key,),
            )
            row = cur.fetchone()

            if not row:
                return 0, [], [], {}
            after_seq = row["after_sequence"] if isinstance(row, dict) else row[0]
            raw_gaps = row["pending_gaps"] if isinstance(row, dict) else row[1]
            raw_applied = row["applied_event_ids"] if isinstance(row, dict) else row[2]
            raw_meta = (
                (row["pending_gap_metadata"] if "pending_gap_metadata" in row else {})
                if isinstance(row, dict)
                else (row[3] if len(row) > 3 else {})
            )
            gaps = json.loads(raw_gaps) if isinstance(raw_gaps, str) else (raw_gaps or [])
            applied = json.loads(raw_applied) if isinstance(raw_applied, str) else (raw_applied or [])
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
            if not isinstance(meta, dict):
                meta = {}
            return int(after_seq), [int(g) for g in gaps], [str(a) for a in applied], meta

    def save_checkpoint(
        self,
        knowledge_conn: psycopg.Connection,
        after_sequence: int,
        pending_gaps: list[int],
        applied_event_ids: list[str],
        pending_gap_metadata: dict[str, Any] | None = None,
    ) -> None:
        gaps_list = sorted(set(int(g) for g in pending_gaps))
        meta = dict(pending_gap_metadata) if pending_gap_metadata else {}
        for g in gaps_list:
            sg = str(g)
            if sg not in meta:
                meta[sg] = {"cycles": 1}

        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.native_event_checkpoints (
                    consumer, workspace_id, after_sequence, pending_gaps, applied_event_ids, pending_gap_metadata, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (consumer)
                DO UPDATE SET
                    workspace_id = EXCLUDED.workspace_id,
                    after_sequence = EXCLUDED.after_sequence,
                    pending_gaps = EXCLUDED.pending_gaps,
                    applied_event_ids = EXCLUDED.applied_event_ids,
                    pending_gap_metadata = EXCLUDED.pending_gap_metadata,
                    updated_at = clock_timestamp()
                """,
                (
                    self.checkpoint_key,
                    self.workspace_id,
                    after_sequence,
                    json.dumps(gaps_list),
                    json.dumps(applied_event_ids[-1000:]),
                    json.dumps(meta),
                ),
            )

    def transform_event_to_observation(self, event: dict[str, Any]) -> SourceObservation:
        event_id = event["event_id"] if isinstance(event["event_id"], UUID) else UUID(str(event["event_id"]))
        obs_id = deterministic_event_observation_id(self.workspace_id, event_id)
        native_payload_sha256 = event["payload_sha256"]

        observation_payload = {
            "type": "domain_event",
            "event_type": event["event_type"],
            "aggregate_type": event["aggregate_type"],
            "aggregate_id": str(event["aggregate_id"]),
            "aggregate_version": event["aggregate_version"],
            "actor_id": str(event["actor_id"]),
            "actor_kind": event["actor_kind"],
            "outcome": event["outcome"],
            "data": event["payload"],
        }
        canonical_payload_sha256 = sha256(observation_payload)

        source_ref = SourceRef(
            workspace_id=self.workspace_id,
            repository_id=self.repository_id,
            work_id=event["aggregate_id"] if event["aggregate_type"] == "work_item" else None,
            producer=f"native_event_consumer:{event.get('actor_kind', 'unknown')}",
            observed_at=event["occurred_at"],
            native_validity_ref=str(event_id),
            native_event_sha256=event.get("event_sha256"),
            previous_event_sha256=event.get("previous_event_sha256"),
            sequence=event.get("sequence"),
        )

        return SourceObservation(
            observation_id=obs_id,
            source=source_ref,
            kind=ObservationKind.EXECUTION_TRACE,
            payload=observation_payload,
            payload_sha256=canonical_payload_sha256,
            native_payload_sha256=native_payload_sha256,
            relevance_tags=(
                f"workspace:{self.workspace_id}",
                f"event:{event_id}",
                f"type:{event['event_type']}",
                f"aggregate:{event['aggregate_id']}",
            ),
            observed_at=event["occurred_at"],
        )

    def _read_native_horizon(self, n_conn: psycopg.Connection) -> tuple[int, int]:
        try:
            with n_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_snapshot_xmin(s)::text::bigint AS xmin, pg_snapshot_xmax(s)::text::bigint AS xmax FROM pg_current_snapshot() AS s"
                )
                row = cur.fetchone()
                if not row:
                    return 0, 0
                xmin = row["xmin"] if isinstance(row, dict) else row[0]
                xmax = row["xmax"] if isinstance(row, dict) else row[1]
                return int(xmin), int(xmax)
        except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
            if not isinstance(exc, NativeSourceUnavailableError):
                raise NativeSourceUnavailableError(str(exc)) from exc
            raise

    def _fetch_native_range(
        self,
        n_conn: psycopg.Connection,
        lo: int,
        hi: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            with n_conn.cursor() as n_cur:
                n_cur.execute(
                    """
                    SELECT event_id, sequence, workspace_id, aggregate_type, aggregate_id,
                           aggregate_version, actor_id, actor_kind, capability_id, request_id,
                           correlation_id, operation_id, causation_id, event_type, outcome,
                           payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                    FROM omp_audit.domain_events
                    WHERE workspace_id = %s AND sequence >= %s AND sequence <= %s
                    ORDER BY sequence ASC
                    LIMIT %s
                    """,
                    (self.workspace_id, lo, hi, limit),
                )
                return n_cur.fetchall()
        except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
            if not isinstance(exc, NativeSourceUnavailableError):
                raise NativeSourceUnavailableError(str(exc)) from exc
            raise

    def _fetch_native_gaps(
        self,
        n_conn: psycopg.Connection,
        reconcile_list: list[int],
    ) -> list[dict[str, Any]]:
        try:
            with n_conn.cursor() as n_cur:
                n_cur.execute(
                    """
                    SELECT event_id, sequence, workspace_id, aggregate_type, aggregate_id,
                           aggregate_version, actor_id, actor_kind, capability_id, request_id,
                           correlation_id, operation_id, causation_id, event_type, outcome,
                           payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                    FROM omp_audit.domain_events
                    WHERE sequence = ANY(%s) AND workspace_id = %s
                    ORDER BY sequence ASC
                    """,
                    (reconcile_list, self.workspace_id),
                )
                return n_cur.fetchall()
        except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
            if not isinstance(exc, NativeSourceUnavailableError):
                raise NativeSourceUnavailableError(str(exc)) from exc
            raise

    def _fetch_native_events_since(
        self,
        n_conn: psycopg.Connection,
        query_watermark: int,
    ) -> list[dict[str, Any]]:
        try:
            with n_conn.cursor() as n_cur:
                n_cur.execute(
                    """
                    SELECT event_id, sequence, workspace_id, aggregate_type, aggregate_id,
                           aggregate_version, actor_id, actor_kind, capability_id, request_id,
                           correlation_id, operation_id, causation_id, event_type, outcome,
                           payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                    FROM omp_audit.domain_events
                    WHERE sequence > %s AND workspace_id = %s
                    ORDER BY sequence ASC
                    LIMIT %s
                    """,
                    (query_watermark, self.workspace_id, self.batch_size),
                )
                return n_cur.fetchall()
        except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
            if not isinstance(exc, NativeSourceUnavailableError):
                raise NativeSourceUnavailableError(str(exc)) from exc
            raise

    def consume_batch(
        self,
        knowledge_conn: psycopg.Connection,
        native_conn: psycopg.Connection | None = None,
    ) -> dict[str, Any]:
        """Execute one consumer cycle:
        1. Read current checkpoint (after_sequence, pending_gaps, metadata) from knowledge DB.
        2. Read server-owned xid horizon from pg_current_snapshot().
        3. Reconcile explicit pending gaps with _fetch_native_gaps.
        4. Windowed reconciliation of unresolved_ranges bounded by reconcile_span and max_reconcile_queries.
        5. Fetch new events past query_watermark, retaining gaps losslessly.
        6. Read updated xmax_now horizon.
        7. Assign xmax_now bound to marked pending gaps and range windows.
        8. Save updated checkpoint with bounded run-length ranges and return cycle summary.
        """
        events_processed = 0

        with knowledge_conn.transaction():
            after_seq, pending_gaps, applied_event_ids, gap_metadata = self.get_checkpoint_state(knowledge_conn)
            unresolved_ranges = _fold_legacy_gaps(gap_metadata)
            reconcile_cursor = int(gap_metadata.get("reconcile_cursor", 0))
            finalized_gap_count = int(gap_metadata.get("finalized_gap_count", 0))
            finalized_ranges_recent = list(gap_metadata.get("finalized_ranges_recent") or [])

            pending_gaps_set = set(pending_gaps)
            applied_ids = list(applied_event_ids)
            applied_ids_set = set(applied_event_ids)
            resolved_gaps: set[int] = set()

            persisted_bound = (
                gap_metadata.get("_highest_seen")
                if "_highest_seen" in gap_metadata
                else (
                    gap_metadata.get("highest_seen")
                    if "highest_seen" in gap_metadata
                    else (
                        gap_metadata.get("_upper_bound")
                        if "_upper_bound" in gap_metadata
                        else gap_metadata.get("upper_bound")
                    )
                )
            )
            persisted_highest = after_seq
            if persisted_bound is not None:
                try:
                    persisted_highest = int(persisted_bound)
                except (ValueError, TypeError):
                    persisted_highest = after_seq

            highest_seen = max(after_seq, persisted_highest)
            has_prior_events = bool(
                after_seq > 0
                or applied_ids_set
                or pending_gaps
                or unresolved_ranges
                or highest_seen > 0
            )

            pending_gaps_to_bound: list[int] = []
            range_windows_to_bound: list[tuple[int, int]] = []

            # Helper context manager to use provided native connection or establish a read-only session
            session_cm: Any
            if native_conn is not None:
                session_cm = contextlib.nullcontext(native_conn)
            else:
                session_cm = native_readonly_session(self.config, self.workspace_id, self.actor_id)

            with session_cm as n_conn:
                # 2. Server-owned MVCC horizon MUST precede every gap read this cycle
                horizon_xmin, _ = self._read_native_horizon(n_conn)

                # 3. Reconcile explicit pending gaps
                if pending_gaps_set:
                    reconcile_list = sorted(pending_gaps_set)
                    try:
                        gap_rows = self._fetch_native_gaps(n_conn, reconcile_list)
                    except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
                        if not isinstance(exc, NativeSourceUnavailableError):
                            raise NativeSourceUnavailableError(str(exc)) from exc
                        raise

                    for grow in gap_rows:
                        seq = grow["sequence"]
                        ev_id_str = str(grow["event_id"])
                        if ev_id_str not in applied_ids_set:
                            obs = self.transform_event_to_observation(grow)
                            store_observation(knowledge_conn, observation=obs)
                            applied_ids.append(ev_id_str)
                            applied_ids_set.add(ev_id_str)
                            events_processed += 1

                        pending_gaps_set.discard(seq)
                        gap_metadata.pop(str(seq), None)
                        resolved_gaps.add(seq)
                        highest_seen = max(highest_seen, seq)
                        unresolved_ranges = _carve_sequence_from_ranges(unresolved_ranges, seq)

                    # For still-unresolved pending gaps:
                    still_unresolved = sorted(pending_gaps_set)
                    for g in still_unresolved:
                        sg = str(g)
                        meta = gap_metadata.get(sg)
                        if isinstance(meta, dict):
                            cycles = meta.get("cycles", 1) + 1
                            curr_bound = meta.get("xmax_bound")
                        elif isinstance(meta, int):
                            cycles = meta + 1
                            curr_bound = None
                        else:
                            cycles = 2
                            curr_bound = None

                        if cycles >= 2 and curr_bound is None:
                            pending_gaps_to_bound.append(g)

                        if cycles >= self.gap_horizon_cycles:
                            pending_gaps_set.discard(g)
                            gap_metadata.pop(sg, None)
                            unresolved_ranges = _normalize_ranges(
                                unresolved_ranges + [[g, g, curr_bound]]
                            )
                            if curr_bound is None:
                                range_windows_to_bound.append((g, g))
                        else:
                            new_entry: dict[str, Any] = {"cycles": cycles}
                            if curr_bound is not None:
                                new_entry["xmax_bound"] = curr_bound
                            gap_metadata[sg] = new_entry

                # 4. Windowed reconciliation of unresolved_ranges
                if unresolved_ranges:
                    max_range_hi = max(r[1] for r in unresolved_ranges)
                    min_range_lo = min(r[0] for r in unresolved_ranges)
                    if reconcile_cursor > max_range_hi or reconcile_cursor < min_range_lo:
                        reconcile_cursor = min_range_lo

                    queries_done = 0
                    positions_scanned = 0
                    visited_intervals: list[tuple[int, int]] = []

                    while (
                        queries_done < self.max_reconcile_queries
                        and positions_scanned < self.reconcile_span
                        and unresolved_ranges
                    ):
                        candidate: list[Any] | None = None
                        for r in unresolved_ranges:
                            if r[1] >= reconcile_cursor:
                                candidate = r
                                break
                        if candidate is None:
                            reconcile_cursor = unresolved_ranges[0][0]
                            candidate = unresolved_ranges[0]

                        r_lo, r_hi, r_bound = candidate[0], candidate[1], candidate[2]
                        win_lo = max(r_lo, reconcile_cursor)
                        rem_span = self.reconcile_span - positions_scanned
                        win_hi = min(r_hi, win_lo + rem_span - 1)

                        if any(v_lo <= win_lo and win_hi <= v_hi for v_lo, v_hi in visited_intervals):
                            break
                        visited_intervals.append((win_lo, win_hi))

                        rows = self._fetch_native_range(n_conn, win_lo, win_hi, limit=self.batch_size)
                        queries_done += 1
                        span_len = win_hi - win_lo + 1
                        positions_scanned += span_len
                        reconcile_cursor = win_hi + 1

                        if rows:
                            for row in rows:
                                seq = row["sequence"]
                                ev_id_str = str(row["event_id"])
                                if ev_id_str not in applied_ids_set:
                                    obs = self.transform_event_to_observation(row)
                                    store_observation(knowledge_conn, observation=obs)
                                    applied_ids.append(ev_id_str)
                                    applied_ids_set.add(ev_id_str)
                                    events_processed += 1
                                pending_gaps_set.discard(seq)
                                gap_metadata.pop(str(seq), None)
                                resolved_gaps.add(seq)
                                highest_seen = max(highest_seen, seq)
                                unresolved_ranges = _carve_sequence_from_ranges(unresolved_ranges, seq)
                        else:
                            if r_bound is not None and r_bound < horizon_xmin:
                                unresolved_ranges = _carve_range_from_ranges(unresolved_ranges, win_lo, win_hi)
                                finalized_gap_count += span_len
                                finalized_ranges_recent.append([win_lo, win_hi])
                                finalized_ranges_recent = finalized_ranges_recent[-32:]
                            elif r_bound is None:
                                range_windows_to_bound.append((win_lo, win_hi))

                    if not unresolved_ranges:
                        reconcile_cursor = 0
                    else:
                        max_hi = max(r[1] for r in unresolved_ranges)
                        min_lo = min(r[0] for r in unresolved_ranges)
                        if reconcile_cursor > max_hi:
                            reconcile_cursor = min_lo

                # 5. Fetch new rows since query_watermark
                query_watermark = max(after_seq, highest_seen, *(resolved_gaps or {0}))
                try:
                    new_rows = self._fetch_native_events_since(n_conn, query_watermark)
                except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
                    if not isinstance(exc, NativeSourceUnavailableError):
                        raise NativeSourceUnavailableError(str(exc)) from exc
                    raise

                if not has_prior_events and new_rows:
                    expected = new_rows[0]["sequence"]
                else:
                    expected = query_watermark + 1

                for row in new_rows:
                    seq = row["sequence"]
                    if seq > expected:
                        gap_start = max(expected, seq - self.gap_visibility_horizon)
                        if gap_start > expected:
                            unresolved_ranges = _normalize_ranges(
                                unresolved_ranges + [[expected, gap_start - 1, None]]
                            )
                        for missing in range(gap_start, seq):
                            if len(pending_gaps_set) < self.max_pending_gaps:
                                pending_gaps_set.add(missing)
                                sm = str(missing)
                                if sm not in gap_metadata:
                                    gap_metadata[sm] = {"cycles": 1}
                            else:
                                unresolved_ranges = _normalize_ranges(
                                    unresolved_ranges + [[missing, missing, None]]
                                )

                    expected = max(expected, seq + 1)
                    if seq in pending_gaps_set:
                        pending_gaps_set.discard(seq)
                        gap_metadata.pop(str(seq), None)

                    resolved_gaps.add(seq)
                    highest_seen = max(highest_seen, seq)
                    unresolved_ranges = _carve_sequence_from_ranges(unresolved_ranges, seq)

                    if str(row["event_id"]) in applied_ids_set:
                        continue

                    obs = self.transform_event_to_observation(row)
                    store_observation(knowledge_conn, observation=obs)
                    applied_ids.append(str(row["event_id"]))
                    applied_ids_set.add(str(row["event_id"]))
                    events_processed += 1

                # 6. Read updated xmax_now AFTER all reads
                _, xmax_now = self._read_native_horizon(n_conn)

                # 7. Assign xmax_bound = xmax_now to marked pending gaps and range windows
                for g in pending_gaps_to_bound:
                    sg = str(g)
                    if sg in gap_metadata and isinstance(gap_metadata[sg], dict):
                        if gap_metadata[sg].get("xmax_bound") is None:
                            gap_metadata[sg]["xmax_bound"] = xmax_now

                for win_lo, win_hi in range_windows_to_bound:
                    unresolved_ranges = _assign_bound_to_range_window(
                        unresolved_ranges, win_lo, win_hi, xmax_now
                    )

            # 8. Compute updated watermark, save checkpoint, return summary
            if pending_gaps_set:
                min_gap = min(pending_gaps_set)
                new_after_seq = max(after_seq, min_gap - 1)
            else:
                new_after_seq = max(after_seq, highest_seen)

            new_gap_metadata = {
                str(g): gap_metadata[str(g)]
                for g in pending_gaps_set
                if str(g) in gap_metadata
            }
            if unresolved_ranges:
                new_gap_metadata["unresolved_ranges"] = unresolved_ranges
            if reconcile_cursor > 0:
                new_gap_metadata["reconcile_cursor"] = reconcile_cursor
            if finalized_gap_count > 0:
                new_gap_metadata["finalized_gap_count"] = finalized_gap_count
            if finalized_ranges_recent:
                new_gap_metadata["finalized_ranges_recent"] = finalized_ranges_recent[-32:]
            if highest_seen > new_after_seq:
                new_gap_metadata["_highest_seen"] = highest_seen

            self.save_checkpoint(
                knowledge_conn,
                after_sequence=new_after_seq,
                pending_gaps=sorted(pending_gaps_set),
                applied_event_ids=applied_ids,
                pending_gap_metadata=new_gap_metadata,
            )

            return {
                "events_processed": events_processed,
                "after_sequence": new_after_seq,
                "pending_gaps": sorted(pending_gaps_set),
                "unresolved_ranges": unresolved_ranges,
                "finalized_gap_count": finalized_gap_count,
            }


__all__ = [
    "NativeEventConsumer",
    "NativeSourceUnavailableError",
    "_covers",
    "deterministic_event_observation_id",
    "native_readonly_session",
]
