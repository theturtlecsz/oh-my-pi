from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.native.consumer import (
    NativeEventConsumer,
    NativeSourceUnavailableError,
    deterministic_event_observation_id,
    native_readonly_session,
)
from omp_knowledge.server import create_app
from omp_knowledge.storage.db import (
    IdempotencyConflictError,
    get_db_connection,
    store_observation,
)
from omp_work.knowledge_contracts import ObservationKind, SourceObservation, SourceRef
from omp_work.v1.canonical import sha256
from support.fixtures import write_test_capability_file
from support.native_fixture import insert_test_receipt
from support.null_engine import NullEngine


def _covers(ranges: list[list[Any]] | None, seq: int) -> bool:
    if not ranges:
        return False
    for item in ranges:
        if len(item) >= 2 and item[0] <= seq <= item[1]:
            return True
    return False


def insert_test_event(
    native_conn: psycopg.Connection,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    event_id: UUID | None = None,
    sequence: int | None = None,
    event_type: str = "work_item_created",
    aggregate_type: str = "work_item",
    aggregate_id: UUID | None = None,
    aggregate_version: int = 1,
    payload: dict[str, Any] | None = None,
    previous_event_sha256: str | None = None,
) -> dict[str, Any]:
    """Helper inserting a test domain event directly using superuser connection."""
    eid = event_id or uuid4()
    agg_id = aggregate_id or uuid4()
    req_id = uuid4()
    corr_id = uuid4()
    op_id = uuid4()
    caus_id = uuid4()
    cap_id = uuid4()

    event_payload = payload if payload is not None else {"title": "Test event", "step": 1}
    payload_hash = sha256(event_payload)
    event_hash = sha256({
        "event_id": str(eid),
        "payload_sha256": payload_hash,
        "previous_event_sha256": previous_event_sha256,
    })

    with native_conn.cursor() as cur:
        # Ensure workspace exists in omp_control.workspaces
        cur.execute(
            "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
        if sequence is not None:
            cur.execute(
                """
                INSERT INTO omp_audit.domain_events (
                    event_id, sequence, workspace_id, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, actor_kind, capability_id, request_id,
                    correlation_id, operation_id, causation_id, event_type, outcome,
                    payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, 'agent', %s, %s,
                    %s, %s, %s, %s, 'success',
                    %s, %s, %s, %s, clock_timestamp()
                )
                RETURNING sequence, event_id, payload_sha256, event_sha256, occurred_at
                """,
                (
                    eid,
                    sequence,
                    workspace_id,
                    aggregate_type,
                    agg_id,
                    aggregate_version,
                    actor_id,
                    cap_id,
                    req_id,
                    corr_id,
                    op_id,
                    caus_id,
                    event_type,
                    json.dumps(event_payload),
                    payload_hash,
                    previous_event_sha256,
                    event_hash,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO omp_audit.domain_events (
                    event_id, workspace_id, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, actor_kind, capability_id, request_id,
                    correlation_id, operation_id, causation_id, event_type, outcome,
                    payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, 'agent', %s, %s,
                    %s, %s, %s, %s, 'success',
                    %s, %s, %s, %s, clock_timestamp()
                )
                RETURNING sequence, event_id, payload_sha256, event_sha256, occurred_at
                """,
                (
                    eid,
                    workspace_id,
                    aggregate_type,
                    agg_id,
                    aggregate_version,
                    actor_id,
                    cap_id,
                    req_id,
                    corr_id,
                    op_id,
                    caus_id,
                    event_type,
                    json.dumps(event_payload),
                    payload_hash,
                    previous_event_sha256,
                    event_hash,
                ),
            )
        row = cur.fetchone()
        assert row is not None
        return {
            "event_id": eid,
            "sequence": row[0],
            "workspace_id": workspace_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": agg_id,
            "aggregate_version": aggregate_version,
            "actor_id": actor_id,
            "actor_kind": "agent",
            "event_type": event_type,
            "outcome": "success",
            "payload": event_payload,
            "payload_sha256": payload_hash,
            "previous_event_sha256": previous_event_sha256,
            "event_sha256": event_hash,
            "occurred_at": row[4],
        }


def test_native_event_capture_duplicate_and_replay(dual_pg_cluster: KnowledgeConfig) -> None:
    """Duplicate / replay test:
    Identical identity, complete lineage, and payload replays cleanly as an idempotent noop
    without creating duplicate rows or raising errors.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # Connect to native DB as postgres superuser to insert event
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            payload={"action": "replayed_task"},
        )

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
    )

    # 1. First consume batch
    res1 = consumer.consume_batch(k_conn)
    assert res1["events_processed"] == 1
    assert res1["after_sequence"] == ev["sequence"]
    assert res1["pending_gaps"] == []

    # Verify observation stored in knowledge DB
    obs_id = deterministic_event_observation_id(ws_id, ev["event_id"])
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
        assert cur.fetchone()["count"] == 1

    # 2. Replay the exact same event explicitly via store_observation
    obs = consumer.transform_event_to_observation(ev)
    store_observation(k_conn, observation=obs)

    # Verify count remains exactly 1 (no duplicate row)
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
        assert cur.fetchone()["count"] == 1

    k_conn.close()


def test_native_event_capture_restart(dual_pg_cluster: KnowledgeConfig) -> None:
    """Restart test:
    A consumer restarts from its saved checkpoint watermark without duplicating or losing events.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # Insert events 1, 2, 3
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, payload={"step": 1})
        ev2 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, payload={"step": 2})
        ev3 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, payload={"step": 3})

    # Consumer instance 1 processes batch
    consumer_1 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="restart_consumer",
    )
    res1 = consumer_1.consume_batch(k_conn)
    assert res1["events_processed"] == 3
    assert res1["after_sequence"] == ev3["sequence"]

    # Insert events 4, 5
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev4 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, payload={"step": 4})
        ev5 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, payload={"step": 5})

    # Consumer instance 2 (simulating fresh process startup after restart)
    consumer_2 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="restart_consumer",
    )
    # Checkpoint read by consumer 2 must be ev3["sequence"]
    after_seq, pending_gaps, _ = consumer_2.get_checkpoint(k_conn)
    assert after_seq == ev3["sequence"]
    assert pending_gaps == []

    # Consumer 2 processes remaining events
    res2 = consumer_2.consume_batch(k_conn)
    assert res2["events_processed"] == 2
    assert res2["after_sequence"] == ev5["sequence"]

    # Total observations in knowledge DB must be 5
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone()["count"] == 5

    k_conn.close()


def test_native_event_capture_out_of_order_sequence(dual_pg_cluster: KnowledgeConfig) -> None:
    """Out-of-order sequence & explicit pending gap reconciliation:
    When events commit out of order (e.g. sequence 1 and 3 arrive while sequence 2 is in-flight),
    the consumer records a pending gap for sequence 2 and holds the contiguous watermark at 1.
    When sequence 2 subsequently appears, reconciliation resolves the gap and advances the watermark to 3.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="ooo_consumer",
    )

    # 1. Insert sequence 1 and sequence 3 (sequence 2 is in-flight/missing)
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=1, payload={"s": 1})
        ev3 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=3, payload={"s": 3})

    # First cycle: should detect gap at sequence 2
    res1 = consumer.consume_batch(k_conn)
    assert res1["events_processed"] == 2
    assert res1["pending_gaps"] == [2]
    # Watermark must not advance past gap: watermark is 1!
    assert res1["after_sequence"] == 1

    # Checkpoint in DB confirms pending gap
    after_seq, pending_gaps, _ = consumer.get_checkpoint(k_conn)
    assert after_seq == 1
    assert pending_gaps == [2]

    # 2. Sequence 2 commits now
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev2 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=2, payload={"s": 2})

    # Second cycle: explicit gap reconciliation finds sequence 2 and advances watermark to 3
    res2 = consumer.consume_batch(k_conn)
    assert res2["events_processed"] == 1
    assert res2["pending_gaps"] == []
    assert res2["after_sequence"] == 3

    after_seq2, pending_gaps2, _ = consumer.get_checkpoint(k_conn)
    assert after_seq2 == 3
    assert pending_gaps2 == []

    # All 3 observations present in knowledge DB
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone()["count"] == 3

    k_conn.close()


def test_native_event_capture_conflict_divergent_payload_rejected_and_original_remains(dual_pg_cluster: KnowledgeConfig) -> None:
    """Conflict contract 1:
    When an incoming observation carries the same deterministic observation identity
    as an existing observation but with a divergent payload, an IdempotencyConflictError
    is raised, the checkpoint watermark remains unchanged, and the original observation remains intact.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    ev_id = uuid4()

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="payload_conflict_consumer",
    )

    # 1. Insert original event and consume it
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            event_id=ev_id,
            sequence=10,
            payload={"key": "original_payload"},
        )

    res1 = consumer.consume_batch(k_conn)
    assert res1["events_processed"] == 1
    assert res1["after_sequence"] == 10

    obs_id = deterministic_event_observation_id(ws_id, ev_id)
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT payload, payload_sha256, native_payload_sha256 FROM omp_knowledge.observations WHERE observation_id = %s",
            (obs_id,),
        )
        orig_row = cur.fetchone()
        assert orig_row is not None
        orig_canonical_sha = orig_row["payload_sha256"]
        orig_payload = orig_row["payload"]

    # 2. Fabricate conflicting observation with SAME observation_id but DIFFERENT payload
    conflicting_payload = {"type": "domain_event", "data": {"key": "tampered_payload"}}
    conflicting_obs = SourceObservation(
        observation_id=obs_id,
        source=SourceRef(
            workspace_id=ws_id,
            repository_id=repo_id,
            producer="native_event_consumer",
            observed_at=ev1["occurred_at"],
            native_validity_ref=str(ev_id),
            native_event_sha256=ev1["event_sha256"],
            sequence=10,
        ),
        kind=ObservationKind.EXECUTION_TRACE,
        payload=conflicting_payload,
        payload_sha256=sha256(conflicting_payload),
        native_payload_sha256=sha256({"key": "tampered_payload"}),
        observed_at=ev1["occurred_at"],
    )

    # Must raise IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError):
        store_observation(k_conn, observation=conflicting_obs)

    # Checkpoint in knowledge DB remains exactly 10
    after_seq, _, _ = consumer.get_checkpoint(k_conn)
    assert after_seq == 10

    # Original observation in knowledge DB must remain completely untouched
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT payload, payload_sha256, native_payload_sha256 FROM omp_knowledge.observations WHERE observation_id = %s",
            (obs_id,),
        )
        after_row = cur.fetchone()
        assert after_row is not None
        assert after_row["payload_sha256"] == orig_canonical_sha
        assert after_row["native_payload_sha256"] == ev1["payload_sha256"]
        assert after_row["payload"] == orig_payload

    k_conn.close()


def test_native_event_capture_conflict_divergent_lineage_rejected_and_original_remains(dual_pg_cluster: KnowledgeConfig) -> None:
    """Conflict contract 2:
    When an incoming observation carries the same deterministic observation identity
    and identical payload, but carries divergent native lineage (e.g. sequence, native_event_sha256,
    previous_event_sha256), an IdempotencyConflictError is raised, the checkpoint watermark
    remains unchanged, and the original observation remains intact.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    ev_id = uuid4()

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="lineage_conflict_consumer",
    )

    # 1. Insert original event and consume it
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            event_id=ev_id,
            sequence=20,
            payload={"key": "authentic_lineage_payload"},
        )

    res1 = consumer.consume_batch(k_conn)
    assert res1["events_processed"] == 1
    assert res1["after_sequence"] == 20

    obs_id = deterministic_event_observation_id(ws_id, ev_id)
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT source, payload, payload_sha256, native_payload_sha256 FROM omp_knowledge.observations WHERE observation_id = %s",
            (obs_id,),
        )
        orig_row = cur.fetchone()
        assert orig_row is not None
        orig_source = orig_row["source"]
        if isinstance(orig_source, str):
            orig_source = json.loads(orig_source)
        orig_canonical_sha = orig_row["payload_sha256"]
        orig_payload = orig_row["payload"]

    # 2. Fabricate observation with SAME observation_id and SAME payload, but DIVERGENT native lineage (sequence = 99 and divergent native_event_sha256)
    conflicting_lineage_obs = SourceObservation(
        observation_id=obs_id,
        source=SourceRef(
            workspace_id=ws_id,
            repository_id=repo_id,
            producer="native_event_consumer",
            observed_at=ev1["occurred_at"],
            native_validity_ref=str(ev_id),
            native_event_sha256="0" * 64,  # Tampered event hash
            previous_event_sha256="1" * 64,  # Tampered previous event hash
            sequence=99,  # Divergent sequence
        ),
        kind=ObservationKind.EXECUTION_TRACE,
        payload=orig_payload,
        payload_sha256=orig_canonical_sha,
        native_payload_sha256=ev1["payload_sha256"],
        observed_at=ev1["occurred_at"],
    )

    # Must raise IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError):
        store_observation(k_conn, observation=conflicting_lineage_obs)

    # Checkpoint in knowledge DB remains exactly 20
    after_seq, _, _ = consumer.get_checkpoint(k_conn)
    assert after_seq == 20

    # Original observation in knowledge DB must retain its original authentic lineage and payload
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT source, payload, payload_sha256, native_payload_sha256 FROM omp_knowledge.observations WHERE observation_id = %s",
            (obs_id,),
        )
        after_row = cur.fetchone()
        assert after_row is not None
        row_src = after_row["source"]
        if isinstance(row_src, str):
            row_src = json.loads(row_src)
        assert row_src.get("sequence") == 20
        assert row_src.get("native_event_sha256") == ev1["event_sha256"]
        assert after_row["payload_sha256"] == orig_canonical_sha
        assert after_row["native_payload_sha256"] == ev1["payload_sha256"]

    k_conn.close()


def test_native_event_capture_readonly_and_rls_refusal(dual_pg_cluster: KnowledgeConfig) -> None:
    """Readonly and RLS refusal test:
    - Consumer session rejects native DB mutations with read-only error.
    - Querying native DB without workspace claim is refused by RLS.
    - Querying with mismatched workspace claim filters out other workspace events.
    """
    ws_id = uuid4()
    actor_id = uuid4()
    other_ws_id = uuid4()

    # Insert an event in ws_id
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id)

    # 1. Read-only refusal: Any attempt to mutate native DB via read-only session must fail
    with native_readonly_session(dual_pg_cluster, workspace_id=ws_id, actor_id=actor_id) as ro_conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with ro_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)",
                    (uuid4(),),
                )

    # 2. RLS refusal: Session without workspace claim raises error on accessing protected table
    with pytest.raises(psycopg.errors.RaiseException, match="workspace claim required"):
        with native_readonly_session(dual_pg_cluster, workspace_id=None, actor_id=None) as no_claim_conn:
            with no_claim_conn.cursor() as cur:
                cur.execute("SELECT * FROM omp_audit.domain_events")
                cur.fetchall()

    # 3. Cross-workspace isolation: Session with other_ws_id returns 0 rows for ws_id events
    with native_readonly_session(dual_pg_cluster, workspace_id=other_ws_id, actor_id=actor_id) as other_conn:
        with other_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_audit.domain_events WHERE event_id = %s", (ev["event_id"],))
            row = cur.fetchone()
            assert (row["count"] if isinstance(row, dict) else row[0]) == 0


def test_native_event_capture_unavailable_source_leaves_checkpoint_unchanged(dual_pg_cluster: KnowledgeConfig) -> None:
    """Unavailable native source leaves checkpoint unchanged:
    If native database connection fails, checkpoint in knowledge DB is preserved without corruption.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # Set up an initial checkpoint
    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="unavail_consumer",
    )
    consumer.save_checkpoint(k_conn, after_sequence=42, pending_gaps=[40], applied_event_ids=["ev-init"])

    # Configure consumer pointing to a non-existent port where connection will fail
    broken_cfg = KnowledgeConfig(
        pg_host=dual_pg_cluster.pg_host,
        pg_port=dual_pg_cluster.pg_port,
        pg_database=dual_pg_cluster.pg_database,
        pg_user=dual_pg_cluster.pg_user,
        pg_password=dual_pg_cluster.pg_password,
        native_pg_host="127.0.0.1",
        native_pg_port=59998,
        native_pg_database="omp_work",
        native_pg_user="omp_work_readonly",
        state_dir=dual_pg_cluster.state_dir,
        config_dir=dual_pg_cluster.config_dir,
    )
    broken_consumer = NativeEventConsumer(
        broken_cfg,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="unavail_consumer",
    )

    with pytest.raises(psycopg.OperationalError):
        broken_consumer.consume_batch(k_conn)

    # Checkpoint in knowledge DB is completely unchanged
    after_seq, pending_gaps, applied = consumer.get_checkpoint(k_conn)
    assert after_seq == 42
    assert pending_gaps == [40]
    assert applied == ["ev-init"]

    k_conn.close()


def test_native_event_capture_separate_hash_fields(dual_pg_cluster: KnowledgeConfig) -> None:
    """Separate hash fields contract:
    Native payload hash and canonical observation payload hash are separate fields with distinct values/meanings.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    raw_payload = {"command": "deploy", "parameters": {"env": "staging", "replicas": 3}}

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            payload=raw_payload,
        )

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
    )
    consumer.consume_batch(k_conn)

    obs_id = deterministic_event_observation_id(ws_id, ev["event_id"])
    with k_conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload_sha256, native_payload_sha256, payload
            FROM omp_knowledge.observations
            WHERE observation_id = %s
            """,
            (obs_id,),
        )
        row = cur.fetchone()
        assert row is not None

        canonical_hash = row["payload_sha256"]
        native_hash = row["native_payload_sha256"]
        stored_payload = row["payload"]

        # Native hash must equal the raw native event's payload_sha256
        assert native_hash == ev["payload_sha256"]

        # Canonical hash must equal the hash of the full observation payload
        assert canonical_hash == sha256(stored_payload)

        # The two hashes must be distinct
        assert canonical_hash != native_hash
        assert len(canonical_hash) == 64
        assert len(native_hash) == 64

    k_conn.close()


def test_native_event_capture_workspace_isolation_same_consumer_name(dual_pg_cluster: KnowledgeConfig) -> None:
    """Two workspace consumers with the exact same consumer_name must not share
    checkpoint state, after_sequence, pending_gaps, or applied_event_ids.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws1 = uuid4()
    ws2 = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    shared_consumer_name = "fleet_consumer"

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        # ws1: seq 10 and 12 (gap at 11)
        ev1_a = insert_test_event(native_admin_conn, workspace_id=ws1, actor_id=actor_id, sequence=10, payload={"w": 1, "s": 10})
        ev1_b = insert_test_event(native_admin_conn, workspace_id=ws1, actor_id=actor_id, sequence=12, payload={"w": 1, "s": 12})
        # ws2: seq 20 and 21 (no gap)
        ev2_a = insert_test_event(native_admin_conn, workspace_id=ws2, actor_id=actor_id, sequence=20, payload={"w": 2, "s": 20})
        ev2_b = insert_test_event(native_admin_conn, workspace_id=ws2, actor_id=actor_id, sequence=21, payload={"w": 2, "s": 21})

    consumer_ws1 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws1,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=shared_consumer_name,
    )
    consumer_ws2 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws2,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=shared_consumer_name,
    )

    # 1. Process batch for workspace 1
    res1 = consumer_ws1.consume_batch(k_conn)
    assert res1["events_processed"] == 2
    assert res1["after_sequence"] == 10
    assert res1["pending_gaps"] == [11]

    # Workspace 2 consumer must have zero checkpoint state and not share ws1's state
    ws2_seq, ws2_gaps, ws2_applied = consumer_ws2.get_checkpoint(k_conn)
    assert ws2_seq == 0
    assert ws2_gaps == []
    assert ws2_applied == []

    # 2. Process batch for workspace 2
    res2 = consumer_ws2.consume_batch(k_conn)
    assert res2["events_processed"] == 2
    assert res2["after_sequence"] == 21
    assert res2["pending_gaps"] == []

    # Re-verify workspace 1 consumer retains its own state
    ws1_seq, ws1_gaps, ws1_applied = consumer_ws1.get_checkpoint(k_conn)
    assert ws1_seq == 10
    assert ws1_gaps == [11]
    assert set(ws1_applied) == {str(ev1_a["event_id"]), str(ev1_b["event_id"])}

    # Re-verify workspace 2 consumer retains its own state
    ws2_seq_after, ws2_gaps_after, ws2_applied_after = consumer_ws2.get_checkpoint(k_conn)
    assert ws2_seq_after == 21
    assert ws2_gaps_after == []
    assert set(ws2_applied_after) == {str(ev2_a["event_id"]), str(ev2_b["event_id"])}

    # Assert distinct DB rows
    with k_conn.cursor() as cur:
        cur.execute(
            """
            SELECT consumer, after_sequence, pending_gaps, applied_event_ids
            FROM omp_knowledge.native_event_checkpoints
            WHERE consumer = ANY(%s)
            """,
            ([consumer_ws1.checkpoint_key, consumer_ws2.checkpoint_key],),
        )
        rows = {r["consumer"]: r for r in cur.fetchall()}
        assert len(rows) == 2
        assert rows[consumer_ws1.checkpoint_key]["after_sequence"] == 10
        assert rows[consumer_ws2.checkpoint_key]["after_sequence"] == 21

    k_conn.close()


def test_native_event_capture_foreign_sequence_does_not_pin_after_horizon(dual_pg_cluster: KnowledgeConfig) -> None:
    """Interleaved foreign-workspace sequence or rolled-back hole:
    Consumer briefly holds watermark for a detected gap. Since the missing sequence belongs
    to a foreign workspace (or rolled back), it will not resolve; after the configured cycle
    horizon (e.g. 2 cycles), the gap expires and watermark progresses instead of pinning forever.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_target = uuid4()
    ws_foreign = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        # Target workspace has seq 1 and seq 3
        ev1 = insert_test_event(native_admin_conn, workspace_id=ws_target, actor_id=actor_id, sequence=1, payload={"step": 1})
        # Foreign workspace consumes global bigserial sequence 2!
        insert_test_event(native_admin_conn, workspace_id=ws_foreign, actor_id=actor_id, sequence=2, payload={"step": 2, "foreign": True})
        ev3 = insert_test_event(native_admin_conn, workspace_id=ws_target, actor_id=actor_id, sequence=3, payload={"step": 3})

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_target,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="foreign_horizon_consumer",
        gap_horizon_cycles=2,
    )

    # Cycle 1: sees seq 1 and seq 3, detects gap at seq 2, briefly holds watermark at 1
    res1 = consumer.consume_batch(k_conn)
    assert res1["events_processed"] == 2
    assert res1["after_sequence"] == 1
    assert res1["pending_gaps"] == [2]

    # Cycle 2: seq 2 belongs to foreign workspace so reconciliation fails;
    # after reaching horizon (2 cycles), gap 2 expires and watermark progresses to 3
    res2 = consumer.consume_batch(k_conn)
    assert res2["events_processed"] == 0
    assert res2["after_sequence"] == 3
    assert res2["pending_gaps"] == []

    # Checkpoint in database reflects advanced watermark and cleared gaps
    after_seq, pending_gaps, _ = consumer.get_checkpoint(k_conn)
    assert after_seq == 3
    assert pending_gaps == []

    # Subsequent cycle stays at 3 and does not pin or re-fetch
    res3 = consumer.consume_batch(k_conn)
    assert res3["events_processed"] == 0
    assert res3["after_sequence"] == 3
    assert res3["pending_gaps"] == []

    # Both target workspace events stored in knowledge DB
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_target,))
        assert cur.fetchone()["count"] == 2

    k_conn.close()


def test_native_event_capture_restart_retains_isolated_state_and_gap_metadata(dual_pg_cluster: KnowledgeConfig) -> None:
    """Process restart preserves workspace-specific checkpoint state and bounded gap metadata
    without cross-workspace leakage or lost cycle counts.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws1 = uuid4()
    ws2 = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "restart_isolated_consumer"

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        # ws1: seq 1 and seq 3 (seq 2 missing/foreign)
        insert_test_event(native_admin_conn, workspace_id=ws1, actor_id=actor_id, sequence=1, payload={"ws": 1, "s": 1})
        insert_test_event(native_admin_conn, workspace_id=ws2, actor_id=actor_id, sequence=2, payload={"ws": 2, "s": 2})
        insert_test_event(native_admin_conn, workspace_id=ws1, actor_id=actor_id, sequence=3, payload={"ws": 1, "s": 3})

    # Initial run for both workspaces using same consumer_name
    c1 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws1,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )
    c2 = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws2,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )

    res1 = c1.consume_batch(k_conn)
    assert res1["after_sequence"] == 1
    assert res1["pending_gaps"] == [2]

    res2 = c2.consume_batch(k_conn)
    assert res2["after_sequence"] == 2
    assert res2["pending_gaps"] == []

    # Simulate process restart by instantiating fresh consumer objects
    del c1
    del c2

    c1_restart = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws1,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )
    c2_restart = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws2,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )

    # ws1 restart preserves after_sequence, pending_gaps, and gap metadata
    seq1, gaps1, applied1, meta1 = c1_restart.get_checkpoint_state(k_conn)
    assert seq1 == 1
    assert gaps1 == [2]
    assert len(applied1) == 2
    assert "2" in meta1
    assert meta1["2"]["cycles"] == 1

    # ws2 restart preserves after_sequence=2 and empty gaps
    seq2, gaps2, applied2, meta2 = c2_restart.get_checkpoint_state(k_conn)
    assert seq2 == 2
    assert gaps2 == []
    assert len(applied2) == 1

    # Run cycle 2 on c1_restart: gap 2 reaches cycle horizon and expires, advancing watermark to 3
    res1_cycle2 = c1_restart.consume_batch(k_conn)
    assert res1_cycle2["after_sequence"] == 3
    assert res1_cycle2["pending_gaps"] == []

    # ws2 state remains isolated and unchanged
    seq2_final, gaps2_final, _ = c2_restart.get_checkpoint(k_conn)
    assert seq2_final == 2
    assert gaps2_final == []

    k_conn.close()


def test_fresh_consumer_cannot_be_advanced_by_client_native_record_observation(dual_pg_cluster: KnowledgeConfig) -> None:
    """A fresh consumer starts at after_sequence=0 and consumes real native domain events,
    ignoring any arbitrary sequence numbers present in omp_knowledge.observations
    (e.g. from client native_record ingest requests).
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # 1. Insert an arbitrary client observation with high sequence (e.g. 500) and native_event_sha256
    fake_client_obs = SourceObservation(
        observation_id=uuid4(),
        source=SourceRef(
            workspace_id=ws_id,
            repository_id=repo_id,
            producer="client_ingester",
            observed_at="2026-09-13T12:00:00Z",
            native_validity_ref="client-event-1",
            native_event_sha256="a" * 64,
            sequence=500,
        ),
        kind=ObservationKind.EXECUTION_TRACE,
        payload={"msg": "untrusted client observation"},
        payload_sha256=sha256({"msg": "untrusted client observation"}),
        native_payload_sha256="b" * 64,
        relevance_tags=(f"workspace:{ws_id}",),
        observed_at=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
    )
    store_observation(k_conn, observation=fake_client_obs)

    # 2. Insert real native domain events in the native database with sequence 1 and sequence 2
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=1, payload={"step": 1})
        ev2 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=2, payload={"step": 2})

    # 3. Create a fresh consumer with NO prior checkpoint row
    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="fresh_consumer_no_client_advance",
    )

    # 4. Consume batch
    res = consumer.consume_batch(k_conn)

    # The fresh consumer must NOT have jumped to 500! It must process native events 1 and 2
    assert res["events_processed"] == 2
    assert res["after_sequence"] == 2
    assert res["pending_gaps"] == []

    # Checkpoint watermark in database is 2
    after_seq, pending_gaps, applied = consumer.get_checkpoint(k_conn)
    assert after_seq == 2
    assert pending_gaps == []
    assert len(applied) == 2

    # Verify observations in knowledge DB include the two native events
    obs_id_1 = deterministic_event_observation_id(ws_id, ev1["event_id"])
    obs_id_2 = deterministic_event_observation_id(ws_id, ev2["event_id"])
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM omp_knowledge.observations WHERE observation_id = ANY(%s)",
            ([obs_id_1, obs_id_2],),
        )
        assert cur.fetchone()["count"] == 2

    k_conn.close()


def test_migrated_checkpoint_path(dual_pg_cluster: KnowledgeConfig) -> None:
    """Migration 0002 schema is authoritative:
    Checkpoint state includes workspace_id, pending_gaps, applied_event_ids,
    and pending_gap_metadata. Schema errors fail loudly without fallback masking.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "migrated_checkpoint_consumer"

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
    )

    # 1. Save checkpoint using migrated schema fields
    meta = {"15": {"cycles": 2}}
    consumer.save_checkpoint(
        k_conn,
        after_sequence=10,
        pending_gaps=[15],
        applied_event_ids=["ev-test-1", "ev-test-2"],
        pending_gap_metadata=meta,
    )

    # 2. Verify row in omp_knowledge.native_event_checkpoints has workspace_id and metadata stored
    with k_conn.cursor() as cur:
        cur.execute(
            """
            SELECT consumer, workspace_id, after_sequence, pending_gaps, applied_event_ids, pending_gap_metadata
            FROM omp_knowledge.native_event_checkpoints
            WHERE consumer = %s
            """,
            (consumer.checkpoint_key,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["workspace_id"] == ws_id
        assert row["after_sequence"] == 10
        raw_gaps = row["pending_gaps"]
        assert (json.loads(raw_gaps) if isinstance(raw_gaps, str) else raw_gaps) == [15]
        raw_meta = row["pending_gap_metadata"]
        stored_meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        assert stored_meta == {"15": {"cycles": 2}}

    # 3. Read checkpoint state via get_checkpoint_state
    seq, gaps, applied, read_meta = consumer.get_checkpoint_state(k_conn)
    assert seq == 10
    assert gaps == [15]
    assert applied == ["ev-test-1", "ev-test-2"]
    assert read_meta == {"15": {"cycles": 2}}

    k_conn.close()


def test_deferred_gaps_retained_as_unresolved_ranges_never_dropped(dual_pg_cluster: KnowledgeConfig) -> None:
    """Unresolved gaps are retained as bounded unresolved_ranges across cycles and restarts,
    never dropped, and captured when committed later.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "gap_diagnostics_consumer"

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=1, payload={"s": 1})
        insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=4, payload={"s": 4})

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        max_pending_gaps=1,
        gap_horizon_cycles=2,
    )

    # Cycle 1: sees 1 and 4, tracks gap 2, spills gap 3 to unresolved_ranges (not dropped)
    res1 = consumer.consume_batch(k_conn)
    assert res1["after_sequence"] == 1
    assert res1["pending_gaps"] == [2]
    assert _covers(res1.get("unresolved_ranges") or [], 3)

    # Cycle 2: gap 2 reaches horizon and defers into unresolved_ranges; pending empty, after 4
    res2 = consumer.consume_batch(k_conn)
    assert res2["after_sequence"] == 4
    assert res2["pending_gaps"] == []
    seq2, gaps2, applied2, meta2 = consumer.get_checkpoint_state(k_conn)
    assert seq2 == 4
    assert gaps2 == []
    ranges2 = meta2.get("unresolved_ranges") or []
    assert _covers(ranges2, 2)
    assert _covers(ranges2, 3)
    assert "expired_gaps" not in meta2
    assert "dropped_gaps" not in meta2

    # Fresh consumer instance (restart) reads identical ranges
    fresh_consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        max_pending_gaps=1,
        gap_horizon_cycles=2,
    )
    restart_seq, restart_gaps, restart_applied, restart_meta = fresh_consumer.get_checkpoint_state(k_conn)
    assert restart_seq == 4
    assert restart_gaps == []
    restart_ranges = restart_meta.get("unresolved_ranges") or []
    assert _covers(restart_ranges, 2)
    assert _covers(restart_ranges, 3)
    assert "expired_gaps" not in restart_meta
    assert "dropped_gaps" not in restart_meta

    # Insert own seq 3 (explicit sequence)
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=3, payload={"s": 3})

    # Next cycle: events_processed 1, observation count 3, ranges no longer cover 3 but still cover 2
    res3 = fresh_consumer.consume_batch(k_conn)
    assert res3["events_processed"] == 1

    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone()["count"] == 3

    ranges3 = res3.get("unresolved_ranges") or []
    assert not _covers(ranges3, 3)
    assert _covers(ranges3, 2)

    k_conn.close()


def test_client_native_record_cannot_poison_real_native_event_capture(dual_pg_cluster: KnowledgeConfig) -> None:
    """Trust boundary validation and store precedence:
    1. NativeRecordIngestRequest rejects client-supplied native lineage fields.
    2. A client pre-creating deterministic observation ID cannot poison or block
       subsequent real consumer capture (Item 4).
    """
    from omp_knowledge.models import NativeRecordIngestRequest

    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    ev_id = uuid4()

    # 1. NativeRecordIngestRequest must reject client-supplied native lineage fields
    deterministic_obs_id = deterministic_event_observation_id(ws_id, ev_id)
    now = datetime.now(timezone.utc)
    poison_source = SourceRef(
        workspace_id=ws_id,
        repository_id=repo_id,
        producer="client_poisoner",
        observed_at=now,
        sequence=42,  # Client attempting to fabricate native sequence!
    )
    with pytest.raises(ValueError, match="Client-supplied native lineage fields are forbidden"):
        NativeRecordIngestRequest(
            operation_id=uuid4(),
            workspace_id=ws_id,
            repository_id=repo_id,
            source_ref=poison_source,
            observation=SourceObservation(
                observation_id=deterministic_obs_id,
                source=poison_source,
                kind=ObservationKind.EXECUTION_TRACE,
                payload={"poison": True},
                payload_sha256=sha256({"poison": True}),
                observed_at=now,
            ),
        )

    # 2. Client manages to insert a raw observation under deterministic_obs_id
    client_precreated_obs = SourceObservation(
        observation_id=deterministic_obs_id,
        source=SourceRef(
            workspace_id=ws_id,
            repository_id=repo_id,
            producer="client_attacker",
            observed_at=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        ),
        kind=ObservationKind.EXECUTION_TRACE,
        payload={"client_fake_data": "pre_created"},
        payload_sha256=sha256({"client_fake_data": "pre_created"}),
        relevance_tags=(f"workspace:{ws_id}",),
        observed_at=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
    )
    store_observation(k_conn, observation=client_precreated_obs)

    # Verify client observation is in DB
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT source, payload FROM omp_knowledge.observations WHERE observation_id = %s",
            (deterministic_obs_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert "client_attacker" in row["source"]["producer"]

    # 3. Real event occurs in native audit store
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            event_id=ev_id,
            sequence=1,
            payload={"authentic": "real_native_event"},
        )

    # 4. Consumer runs: must NOT be blocked by client pre-created observation!
    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="trust_boundary_consumer",
    )
    res = consumer.consume_batch(k_conn)
    assert res["events_processed"] == 1
    assert res["after_sequence"] == 1

    # 5. Authoritative observation row is updated with authentic native domain event
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT source, payload FROM omp_knowledge.observations WHERE observation_id = %s",
            (deterministic_obs_id,),
        )
        row_after = cur.fetchone()
        assert row_after is not None
        assert "native_event_consumer" in row_after["source"]["producer"]
        assert row_after["source"]["sequence"] == 1
        assert row_after["payload"]["data"] == {"authentic": "real_native_event"}

    k_conn.close()


def test_unresolved_ranges_bounded_checkpoint_and_windowed_reconciliation(dual_pg_cluster: KnowledgeConfig) -> None:
    """Unresolved ranges remain bounded in checkpoint metadata and reconcile across cycles."""
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "bounded_ranges_consumer"

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        reconcile_span=250_000,
    )

    consumer.save_checkpoint(
        k_conn,
        after_sequence=1_000_000,
        pending_gaps=[],
        applied_event_ids=[],
        pending_gap_metadata={
            "unresolved_ranges": [[1, 999_999, None]],
        },
    )

    # Run cycle 1 and assert len(json.dumps(metadata)) < 4096 and reconcile_cursor advanced to 250_001
    consumer.consume_batch(k_conn)
    _, _, _, meta1 = consumer.get_checkpoint_state(k_conn)
    assert len(json.dumps(meta1)) < 4096
    assert meta1.get("reconcile_cursor") == 250_001

    # Insert own event with explicit sequence 600_000 (a late commit deep inside the range)
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev600 = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            sequence=600_000,
            payload={"s": 600_000},
        )

    # Run cycles until captured (must be within 3 more cycles) asserting metadata size < 4096 after every cycle
    cycles_run = 1
    captured = False
    while cycles_run < 5 and not captured:
        res = consumer.consume_batch(k_conn)
        cycles_run += 1
        _, _, _, meta = consumer.get_checkpoint_state(k_conn)
        assert len(json.dumps(meta)) < 4096
        if res["events_processed"] > 0:
            captured = True
    assert captured
    assert cycles_run <= 4

    obs_id = deterministic_event_observation_id(ws_id, ev600["event_id"])
    with k_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
        assert cur.fetchone() is not None

    assert not _covers(meta.get("unresolved_ranges") or [], 600_000)

    # Continue up to 10 total cycles: unresolved ranges remain bounded and cover not-yet-finalized gaps
    while cycles_run < 10:
        consumer.consume_batch(k_conn)
        cycles_run += 1
        _, _, _, meta = consumer.get_checkpoint_state(k_conn)
        assert len(json.dumps(meta)) < 4096

    ranges = meta.get("unresolved_ranges") or []
    assert len(ranges) >= 1
    assert _covers(ranges, 1)
    assert _covers(ranges, 599_999)
    assert _covers(ranges, 600_001)
    assert _covers(ranges, 999_999)
    assert not _covers(ranges, 600_000)

    # Inspect actual server-owned horizon and range bounds
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        horizon_xmin, _ = consumer._read_native_horizon(native_admin_conn)

    horizon_proves_bound = bool(ranges) and all(
        r[2] is not None and horizon_xmin > r[2] for r in ranges
    )

    if not ranges:
        assert meta.get("finalized_gap_count") == 999_998
    elif horizon_proves_bound:
        for _ in range(8):
            consumer.consume_batch(k_conn)
            _, _, _, meta = consumer.get_checkpoint_state(k_conn)
            assert len(json.dumps(meta)) < 4096
            if (meta.get("unresolved_ranges") or []) == []:
                break
        meta_final = consumer.get_checkpoint_state(k_conn)[3]
        assert (meta_final.get("unresolved_ranges") or []) == []
        assert meta_final.get("finalized_gap_count") == 999_998
    else:
        # Implementation correctly retains ranges while server-owned horizon has not exceeded range bounds
        assert len(ranges) >= 1
        assert _covers(ranges, 1)
        assert _covers(ranges, 599_999)
        assert _covers(ranges, 600_001)
        assert _covers(ranges, 999_999)
        assert not _covers(ranges, 600_000)
        assert len(json.dumps(meta)) < 4096
        with k_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
            assert cur.fetchone() is not None

    after_seq, _, _, _ = consumer.get_checkpoint_state(k_conn)
    assert after_seq == 1_000_000

    k_conn.close()


def test_native_consume_service_endpoint_and_checkpoint(dual_pg_cluster: KnowledgeConfig) -> None:
    """Service surface contract:
    - POST /v1/native/consume requires knowledge.ingest and valid workspace.
    - Successfully consumes native domain events through durable owner/CAS and advances checkpoint.
    - GET /v1/native/checkpoint requires knowledge.read and returns current migrated checkpoint schema.
    - Forbidden scope / mismatched workspace are rejected with HTTP 403.
    """
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    foreign_ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    token = "native_test_token"
    read_only_token = "native_readonly_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest"],
        name="test-native-actor",
    )
    write_test_capability_file(
        cap_dir,
        token=read_only_token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read"],
        name="test-readonly-actor",
    )

    app = create_app(dual_pg_cluster, engine=NullEngine())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    ro_headers = {"Authorization": f"Bearer {read_only_token}"}

    # Initial checkpoint query when no consumer checkpoint exists
    res_init_cp = client.get(
        f"/v1/native/checkpoint?workspace_id={ws_id}&consumer_name=svc_consumer",
        headers=headers,
    )
    assert res_init_cp.status_code == 200
    init_cp_data = res_init_cp.json()
    assert init_cp_data["after_sequence"] == 0
    assert init_cp_data["pending_gaps"] == []
    assert init_cp_data["consumer"] == f"svc_consumer:{ws_id}"

    # Insert a native domain event
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            sequence=1,
            payload={"msg": "service_consume_test"},
        )

    # 1. Scope enforcement: Token without knowledge.ingest gets 403 on POST /v1/native/consume
    res_no_scope = client.post(
        "/v1/native/consume",
        headers=ro_headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "consumer_name": "svc_consumer",
        },
    )
    assert res_no_scope.status_code == 403
    assert res_no_scope.json()["detail"]["error"]["code"] == "forbidden"

    # 2. Workspace check: Request with foreign workspace gets 403
    res_foreign = client.post(
        "/v1/native/consume",
        headers=headers,
        json={
            "workspace_id": str(foreign_ws_id),
            "repository_id": str(repo_id),
            "consumer_name": "svc_consumer",
        },
    )
    assert res_foreign.status_code == 403
    assert res_foreign.json()["detail"]["error"]["code"] == "forbidden"

    # 3. Successful consume
    op_id = uuid4()
    res_consume = client.post(
        "/v1/native/consume",
        headers=headers,
        json={
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "consumer_name": "svc_consumer",
            "batch_size": 50,
        },
    )
    assert res_consume.status_code == 200
    consume_data = res_consume.json()
    assert consume_data["state"] == "completed"
    assert consume_data["response"]["events_processed"] == 1
    assert consume_data["response"]["after_sequence"] == 1

    # 4. Read updated checkpoint via GET /v1/native/checkpoint
    res_cp = client.get(
        f"/v1/native/checkpoint?workspace_id={ws_id}&consumer_name=svc_consumer",
        headers=headers,
    )
    assert res_cp.status_code == 200
    cp_data = res_cp.json()
    assert cp_data["after_sequence"] == 1
    assert cp_data["pending_gaps"] == []
    assert cp_data["workspace_id"] == str(ws_id)
    assert str(ev["event_id"]) in cp_data["applied_event_ids"]
    assert "updated_at" in cp_data

    # Checkpoint read with foreign workspace gets 403
    res_cp_foreign = client.get(
        f"/v1/native/checkpoint?workspace_id={foreign_ws_id}&consumer_name=svc_consumer",
        headers=headers,
    )
    assert res_cp_foreign.status_code == 403
    assert res_cp_foreign.json()["detail"]["error"]["code"] == "forbidden"


def test_native_consume_unavailable_source_fail_closed_checkpoint_and_job_state(dual_pg_cluster: KnowledgeConfig) -> None:
    """When native database connection is unavailable during POST /v1/native/consume:
    - Service returns structured HTTP 503 with native_source_unavailable error.
    - Checkpoint in knowledge DB remains unchanged (fail-closed).
    - Durable job in omp_knowledge.ingestion_jobs is marked failed with error diagnostics.
    """
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    token = "native_unavail_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest"],
        name="test-unavail-actor",
    )

    # Pre-populate a checkpoint at sequence 42
    consumer_name = "unavail_svc_consumer"
    c = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
    )
    k_conn = get_db_connection(dual_pg_cluster)
    c.save_checkpoint(k_conn, after_sequence=42, pending_gaps=[40], applied_event_ids=["init-ev-id"])
    k_conn.commit()
    k_conn.close()

    # Create config pointing to non-existent native port
    broken_cfg = KnowledgeConfig(
        pg_host=dual_pg_cluster.pg_host,
        pg_port=dual_pg_cluster.pg_port,
        pg_database=dual_pg_cluster.pg_database,
        pg_user=dual_pg_cluster.pg_user,
        pg_password=dual_pg_cluster.pg_password,
        native_pg_host="127.0.0.1",
        native_pg_port=59998,
        native_pg_database="omp_work",
        native_pg_user="omp_work_readonly",
        state_dir=dual_pg_cluster.state_dir,
        config_dir=dual_pg_cluster.config_dir,
    )

    app = create_app(broken_cfg, engine=NullEngine())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    op_id = uuid4()
    res = client.post(
        "/v1/native/consume",
        headers=headers,
        json={
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "consumer_name": consumer_name,
        },
    )

    # Must return structured 503
    assert res.status_code == 503
    body = res.json()
    assert body["error"]["code"] == "native_source_unavailable"

    # Knowledge DB checkpoint must remain strictly unchanged at 42
    k_conn = get_db_connection(dual_pg_cluster)
    after_seq, pending_gaps, applied = c.get_checkpoint(k_conn)
    assert after_seq == 42
    assert pending_gaps == [40]
    assert applied == ["init-ev-id"]

    # Ingestion job must be durably recorded as failed
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT state, error FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
            (op_id,),
        )
        job_row = cur.fetchone()
        assert job_row is not None
        assert job_row["state"] == "failed"
        err = job_row["error"]
        if isinstance(err, str):
            err = json.loads(err)
        assert "NativeSourceUnavailableError" in err.get("type", "") or "native_source_unavailable" in err.get("message", "")

    k_conn.close()


def test_native_events_read_database_error_translates_to_503_checkpoint_unchanged(
    dual_pg_cluster: KnowledgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission/RLS/DatabaseError during native domain_events read becomes NativeSourceUnavailableError
    and structured 503, leaving knowledge checkpoint unchanged.
    """
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    token = "test_db_err_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest"],
        name="test-dberr-actor",
    )

    consumer_name = "dberr_svc_consumer"
    c = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
    )
    k_conn = get_db_connection(dual_pg_cluster)
    c.save_checkpoint(k_conn, after_sequence=42, pending_gaps=[40], applied_event_ids=["init-ev-id"])
    k_conn.commit()
    k_conn.close()

    # Monkeypatch _fetch_native_events_since to raise psycopg.DatabaseError (e.g. permission/RLS error)
    def mock_fetch_error(self_consumer: Any, n_conn: Any, query_watermark: int) -> list[dict[str, Any]]:
        raise psycopg.DatabaseError("permission denied for relation domain_events")

    monkeypatch.setattr(NativeEventConsumer, "_fetch_native_events_since", mock_fetch_error)

    # 1. Direct consume_batch must raise typed NativeSourceUnavailableError
    k_conn = get_db_connection(dual_pg_cluster)
    with pytest.raises(NativeSourceUnavailableError):
        c.consume_batch(k_conn)

    # Checkpoint must be unchanged at sequence 42
    after_seq, pending_gaps, applied = c.get_checkpoint(k_conn)
    assert after_seq == 42
    assert pending_gaps == [40]
    assert applied == ["init-ev-id"]
    k_conn.close()

    # 2. HTTP endpoint must return structured 503
    app = create_app(dual_pg_cluster, engine=NullEngine())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    op_id = uuid4()
    res = client.post(
        "/v1/native/consume",
        headers=headers,
        json={
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "consumer_name": consumer_name,
        },
    )
    assert res.status_code == 503
    body = res.json()
    assert body["error"]["code"] == "native_source_unavailable"

    # Checkpoint must still be unchanged
    k_conn = get_db_connection(dual_pg_cluster)
    after_seq, pending_gaps, applied = c.get_checkpoint(k_conn)
    assert after_seq == 42
    assert pending_gaps == [40]
    assert applied == ["init-ev-id"]
    k_conn.close()


def test_knowledge_db_mutation_error_not_masked_as_native_source_unavailable(
    dual_pg_cluster: KnowledgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors occurring during knowledge DB mutation (e.g. store_observation) must NOT be caught
    or converted into NativeSourceUnavailableError.
    """
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # Insert an event into native DB
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_conn:
        insert_test_event(native_conn, workspace_id=ws_id, actor_id=actor_id)

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="knowledge_err_consumer",
    )

    class CustomKnowledgeMutationError(psycopg.DatabaseError):
        pass

    def mock_failing_store_observation(conn: Any, observation: Any) -> None:
        raise CustomKnowledgeMutationError("simulated knowledge DB write failure")

    monkeypatch.setattr("omp_knowledge.native.consumer.store_observation", mock_failing_store_observation)

    k_conn = get_db_connection(dual_pg_cluster)
    try:
        with pytest.raises(CustomKnowledgeMutationError) as exc_info:
            consumer.consume_batch(k_conn)
        assert not isinstance(exc_info.value, NativeSourceUnavailableError)
        assert "simulated knowledge DB write failure" in str(exc_info.value)
    finally:
        k_conn.close()


def test_native_events_cli_consume_and_checkpoint(
    dual_pg_cluster: KnowledgeConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """CLI consume-events and checkpoint commands execute correctly and emit valid structured JSON."""
    from omp_knowledge import cli

    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # 1. Insert native domain event
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev = insert_test_event(
            native_admin_conn,
            workspace_id=ws_id,
            actor_id=actor_id,
            sequence=100,
            payload={"msg": "cli_consume_event"},
        )

    monkeypatch.setattr(cli, "load_config", lambda: dual_pg_cluster)

    # 2. Run CLI consume-events
    monkeypatch.setattr(
        "sys.argv",
        [
            "omp-knowledge",
            "consume-events",
            "--workspace-id",
            str(ws_id),
            "--repository-id",
            str(repo_id),
            "--consumer-name",
            "cli_consumer",
            "--actor-id",
            str(actor_id),
        ],
    )
    cli.main()
    captured_consume = capsys.readouterr()
    consume_output = json.loads(captured_consume.out)
    assert consume_output["state"] == "completed"
    assert consume_output["response"]["events_processed"] == 1
    assert consume_output["response"]["after_sequence"] == 100

    # 3. Run CLI checkpoint
    monkeypatch.setattr(
        "sys.argv",
        [
            "omp-knowledge",
            "checkpoint",
            "--workspace-id",
            str(ws_id),
            "--consumer-name",
            "cli_consumer",
        ],
    )
    cli.main()
    captured_cp = capsys.readouterr()
    cp_output = json.loads(captured_cp.out)
    assert cp_output["after_sequence"] == 100
    assert cp_output["consumer"] == f"cli_consumer:{ws_id}"
    assert cp_output["workspace_id"] == str(ws_id)
    assert str(ev["event_id"]) in cp_output["applied_event_ids"]


def test_native_gap_durability_late_commit_recovery_real_postgres(dual_pg_cluster: KnowledgeConfig) -> None:
    """HIGH native gap durability with real disposable PostgreSQL:
    Sequence N+1 commits while sequence N transaction is in-flight.
    Sequence N expires after gap_horizon_cycles, allowing the watermark to advance
    and preserving foreign traffic liveness.
    When transaction for sequence N commits later, subsequent consume cycle captures
    sequence N exactly once via the retry path, and checkpoint/observations converge.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_target = uuid4()
    ws_foreign = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_target,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="gap_durability_consumer",
        gap_horizon_cycles=2,
    )

    # 1. Insert sequence 1 (committed) for ws_target
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev1 = insert_test_event(
            native_admin_conn,
            workspace_id=ws_target,
            actor_id=actor_id,
            sequence=1,
            payload={"step": 1},
        )

    # 2. In-flight transaction: sequence N=2 is allocated in an open, uncommitted transaction
    native_tx_conn = psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=False,
    )
    try:
        ev2 = insert_test_event(
            native_tx_conn,
            workspace_id=ws_target,
            actor_id=actor_id,
            sequence=2,
            payload={"step": 2, "late_commit": True},
        )

        # 3. Sequence N+1=3 commits in another connection, along with foreign traffic seq 4 and target seq 5
        with psycopg.connect(
            host=dual_pg_cluster.native_pg_host,
            port=dual_pg_cluster.native_pg_port,
            user="postgres",
            dbname=dual_pg_cluster.native_pg_database,
            autocommit=True,
        ) as native_admin_conn:
            ev3 = insert_test_event(
                native_admin_conn,
                workspace_id=ws_target,
                actor_id=actor_id,
                sequence=3,
                payload={"step": 3},
            )
            # Foreign workspace traffic at sequence 4
            insert_test_event(
                native_admin_conn,
                workspace_id=ws_foreign,
                actor_id=actor_id,
                sequence=4,
                payload={"step": 4, "foreign": True},
            )
            ev5 = insert_test_event(
                native_admin_conn,
                workspace_id=ws_target,
                actor_id=actor_id,
                sequence=5,
                payload={"step": 5},
            )

        # Cycle 1: sees seq 1, 3, 5; detects gaps at seq 2 and 4. Holds watermark at min_gap - 1 = 1
        res1 = consumer.consume_batch(k_conn)
        assert res1["events_processed"] == 3  # ev1, ev3, ev5
        assert res1["after_sequence"] == 1
        assert res1["pending_gaps"] == [2, 4]

        # Cycle 2: seq 2 is still uncommitted, seq 4 is foreign. Both reach 2 cycles and move to unresolved_ranges.
        # Watermark advances past gaps to highest seen (5), preserving foreign traffic liveness.
        res2 = consumer.consume_batch(k_conn)
        assert res2["events_processed"] == 0
        assert res2["after_sequence"] == 5
        assert res2["pending_gaps"] == []

        # Checkpoint confirms unresolved ranges tracked in metadata
        after_seq2, pending2, applied2, meta2 = consumer.get_checkpoint_state(k_conn)
        assert after_seq2 == 5
        assert pending2 == []
        ranges2 = meta2.get("unresolved_ranges") or []
        assert _covers(ranges2, 2)
        assert _covers(ranges2, 4)

        # Run 3 further cycles while the seq-2 transaction is STILL OPEN and assert seq 2 remains covered (never finalized while in flight) and events_processed 0 each
        for _ in range(3):
            res_open = consumer.consume_batch(k_conn)
            assert res_open["events_processed"] == 0
            _, _, _, meta_open = consumer.get_checkpoint_state(k_conn)
            assert _covers(meta_open.get("unresolved_ranges") or [], 2)

        # 4. Transaction for sequence N=2 finally COMMITS!
        native_tx_conn.commit()
    finally:
        native_tx_conn.close()

    # Cycle 3: Consumer retry path checks unresolved ranges, finds committed sequence 2,
    # captures it exactly once, and prunes it from unresolved ranges.
    res3 = consumer.consume_batch(k_conn)
    assert res3["events_processed"] == 1
    assert res3["after_sequence"] == 5
    assert res3["pending_gaps"] == []

    # Checkpoint and observations converge
    after_seq3, pending3, applied3, meta3 = consumer.get_checkpoint_state(k_conn)
    assert after_seq3 == 5
    assert pending3 == []
    assert not _covers(meta3.get("unresolved_ranges") or [], 2)
    assert str(ev2["event_id"]) in applied3

    # All target workspace observations (seq 1, 2, 3, 5) are present in knowledge DB
    obs_ids = [
        deterministic_event_observation_id(ws_target, ev1["event_id"]),
        deterministic_event_observation_id(ws_target, ev2["event_id"]),
        deterministic_event_observation_id(ws_target, ev3["event_id"]),
        deterministic_event_observation_id(ws_target, ev5["event_id"]),
    ]
    with k_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM omp_knowledge.observations WHERE observation_id = ANY(%s)",
            (obs_ids,),
        )
        assert cur.fetchone()["count"] == 4

        # Total observations for target workspace must be exactly 4 (no foreign event 4, no duplicates)
        cur.execute(
            "SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s",
            (ws_target,),
        )
        assert cur.fetchone()["count"] == 4

    # Loop <= 6 cycles until ranges == [] and finalized_gap_count == 1 (foreign seq 4)
    for _ in range(6):
        consumer.consume_batch(k_conn)
        _, _, _, m_fin = consumer.get_checkpoint_state(k_conn)
        if (m_fin.get("unresolved_ranges") or []) == []:
            break

    _, _, _, meta_final = consumer.get_checkpoint_state(k_conn)
    assert (meta_final.get("unresolved_ranges") or []) == []
    assert meta_final.get("finalized_gap_count") == 1
    assert "expired_gaps" not in meta_final
    assert "dropped_gaps" not in meta_final

    k_conn.close()


def test_native_full_migrations_apply_and_consume_replay_correction_journey(
    dual_pg_cluster: KnowledgeConfig,
) -> None:
    """Real assertion that the full migration set 0001 through 0023 applies to the native cluster
    and the relevant consume, replay, and correction journeys pass against the complete schema.
    """
    # 1. Assert full migration set (0001 through 0023) is applied on the native database
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin:
        with native_admin.cursor() as cur:
            # Check schema_migrations has all 23 migrations
            cur.execute("SELECT count(*) FROM omp_control.schema_migrations")
            mig_count = cur.fetchone()[0]
            assert mig_count == 23, f"Expected 23 applied migrations, got {mig_count}"

            # Verify tables from early, mid, and late migrations exist
            for table_name in (
                "omp_control.schema_migrations",
                "omp_control.runtime_compatibility",
                "omp_work.work_items",
                "omp_evidence.receipts",
                "omp_audit.domain_events",
                "omp_integration.raw_exports",
                "omp_work.repositories",
                "omp_control.workspace_authority",
                "omp_control.cutover_plan_attestations",
                "omp_work.close_attempts",
                "omp_work.authorization_uses",
                "omp_work.execution_grants",
            ):
                cur.execute("SELECT to_regclass(%s)", (table_name,))
                reg = cur.fetchone()[0]
                assert reg is not None, f"Expected table {table_name} to exist from migrations"

    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()

    # 2. Consume journey: Insert native domain events and consume them
    k_conn = get_db_connection(dual_pg_cluster)
    try:
        with psycopg.connect(
            host=dual_pg_cluster.native_pg_host,
            port=dual_pg_cluster.native_pg_port,
            user="postgres",
            dbname=dual_pg_cluster.native_pg_database,
            autocommit=True,
        ) as native_admin:
            ev1 = insert_test_event(
                native_admin,
                workspace_id=ws_id,
                actor_id=actor_id,
                sequence=1,
                event_type="work_item_created",
                payload={"title": "Journey test item", "status": "open"},
            )
            ev2 = insert_test_event(
                native_admin,
                workspace_id=ws_id,
                actor_id=actor_id,
                sequence=2,
                event_type="work_item_updated",
                payload={"title": "Journey test item", "status": "in_progress"},
            )

        consumer = NativeEventConsumer(
            dual_pg_cluster,
            workspace_id=ws_id,
            actor_id=actor_id,
            repository_id=repo_id,
            consumer_name="journey_consumer",
        )
        res_consume = consumer.consume_batch(k_conn)
        assert res_consume["events_processed"] == 2
        assert res_consume["after_sequence"] == 2
        assert res_consume["pending_gaps"] == []

        # Verify observations created in knowledge DB
        obs1_id = deterministic_event_observation_id(ws_id, ev1["event_id"])
        obs2_id = deterministic_event_observation_id(ws_id, ev2["event_id"])
        with k_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM omp_knowledge.observations WHERE observation_id IN (%s, %s)",
                (obs1_id, obs2_id),
            )
            assert cur.fetchone()["count"] == 2

        # 3. Replay journey: Replay consumption without new events; verify idempotence
        res_replay = consumer.consume_batch(k_conn)
        assert res_replay["events_processed"] == 0
        assert res_replay["after_sequence"] == 2
        assert res_replay["pending_gaps"] == []

        # Checkpoint remains stable
        after_seq, pending, applied, _ = consumer.get_checkpoint_state(k_conn)
        assert after_seq == 2
        assert pending == []
        assert str(ev1["event_id"]) in applied
        assert str(ev2["event_id"]) in applied
    finally:
        k_conn.close()

    # 4. Correction journey: Proposal creation, receipt evaluation, and correction withdrawal
    app = create_app(dual_pg_cluster, engine=NullEngine())
    client = TestClient(app)

    # Set up capability credentials
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)
    token = "journey_test_token"
    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="journey-actor",
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Ensure repository row in knowledge DB
    with psycopg.connect(dual_pg_cluster.pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_knowledge.repositories (repository_id, state) VALUES (%s, 'unbound') ON CONFLICT DO NOTHING",
            (repo_id,),
        )

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # Insert qualifying audit receipt in native DB
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        dbname=dual_pg_cluster.native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=ws_id,
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            kind="audit",
            verdict="PASS",
            independent=True,
            issuer="work-service/auditor-settle",
        )

    # Create proposal citing receipt
    p_res = client.post(
        "/v1/proposals",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "title": "Full migration journey proposal",
            "steps": ["execute journey"],
            "applicability_scope": "production",
            "supporting_evidence": [
                {
                    "workspace_id": str(ws_id),
                    "repository_id": str(repo_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec["payload_sha256"],
                    "native_validity_ref": str(rec["receipt_id"]),
                }
            ],
        },
    )
    assert p_res.status_code == 200
    prop_id = p_res.json()["proposal_id"]

    # Verify applicability accepted
    app_res = client.post(
        "/v1/applicability",
        headers=headers,
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
        },
    )
    assert app_res.status_code == 200
    assert app_res.json()["acceptance"] == "accepted"

    # Invalidate / withdraw proposal (correction journey)
    with psycopg.connect(dual_pg_cluster.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE omp_knowledge.procedure_proposals SET invalidated_at = clock_timestamp() WHERE proposal_id = %s",
                (UUID(prop_id),),
            )

    # Verify applicability after correction is not_accepted with proposal_invalidated
    app_res_post = client.post(
        "/v1/applicability",
        headers=headers,
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
        },
    )
    assert app_res_post.status_code == 200
    assert app_res_post.json()["acceptance"] == "not_accepted"
    assert "proposal_invalidated" in app_res_post.json()["reasons"]


def test_native_gap_late_commit_survives_restart_and_many_cycles(dual_pg_cluster: KnowledgeConfig) -> None:
    """A late-committing transaction remains protected in unresolved ranges across restarts
    and arbitrarily many consumer cycles, and is captured cleanly once committed.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "restart_durability_consumer"

    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=1, payload={"step": 1})

    native_tx_conn = psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=False,
    )
    try:
        insert_test_event(native_tx_conn, workspace_id=ws_id, actor_id=actor_id, sequence=2, payload={"step": 2})

        with psycopg.connect(
            host=dual_pg_cluster.native_pg_host,
            port=dual_pg_cluster.native_pg_port,
            user="postgres",
            dbname=dual_pg_cluster.native_pg_database,
            autocommit=True,
        ) as native_admin_conn:
            insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=3, payload={"step": 3})

        for _ in range(5):
            consumer_instance = NativeEventConsumer(
                dual_pg_cluster,
                workspace_id=ws_id,
                actor_id=actor_id,
                repository_id=repo_id,
                consumer_name=consumer_name,
                gap_horizon_cycles=2,
            )
            consumer_instance.consume_batch(k_conn)
            _, pending, _, meta = consumer_instance.get_checkpoint_state(k_conn)
            ranges = meta.get("unresolved_ranges") or []
            assert (2 in pending) or _covers(ranges, 2)

            with k_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_id,))
                assert cur.fetchone()["count"] == 2

        native_tx_conn.commit()
    finally:
        native_tx_conn.close()

    # Commit seq 2; new instance cycle -> events_processed 1, observations 3, ranges empty of 2
    c_post = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )
    res_post = c_post.consume_batch(k_conn)
    assert res_post["events_processed"] == 1
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone()["count"] == 3
    _, _, _, meta_post = c_post.get_checkpoint_state(k_conn)
    assert not _covers(meta_post.get("unresolved_ranges") or [], 2)

    # One more cycle -> 0 processed (exactly once)
    c_final = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )
    res_final = c_final.consume_batch(k_conn)
    assert res_final["events_processed"] == 0

    k_conn.close()


def test_multi_workspace_rls_holes_finalize_without_capture_and_without_pinning(
    dual_pg_cluster: KnowledgeConfig,
) -> None:
    """RLS holes from foreign workspace sequences finalize via xid horizon without capture
    and without permanently pinning the watermark.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_target = uuid4()
    ws_foreign = uuid4()
    actor_target = uuid4()
    actor_foreign = uuid4()
    repo_target = uuid4()
    repo_foreign = uuid4()
    consumer_name = "rls_hole_test_consumer"

    foreign_events = []
    target_events = []
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        target_events.append(insert_test_event(native_admin_conn, workspace_id=ws_target, actor_id=actor_target, sequence=1, payload={"s": 1}))
        foreign_events.append(insert_test_event(native_admin_conn, workspace_id=ws_foreign, actor_id=actor_foreign, sequence=2, payload={"s": 2}))
        target_events.append(insert_test_event(native_admin_conn, workspace_id=ws_target, actor_id=actor_target, sequence=3, payload={"s": 3}))
        foreign_events.append(insert_test_event(native_admin_conn, workspace_id=ws_foreign, actor_id=actor_foreign, sequence=4, payload={"s": 4}))
        foreign_events.append(insert_test_event(native_admin_conn, workspace_id=ws_foreign, actor_id=actor_foreign, sequence=5, payload={"s": 5}))
        target_events.append(insert_test_event(native_admin_conn, workspace_id=ws_target, actor_id=actor_target, sequence=6, payload={"s": 6}))

    target_consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_target,
        actor_id=actor_target,
        repository_id=repo_target,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )

    # Cycle 1: after 1, pending [2, 4, 5]
    res1 = target_consumer.consume_batch(k_conn)
    assert res1["after_sequence"] == 1
    assert res1["pending_gaps"] == [2, 4, 5]

    # Cycle 2: after 6, pending [], ranges cover 2, 4, 5
    res2 = target_consumer.consume_batch(k_conn)
    assert res2["after_sequence"] == 6
    assert res2["pending_gaps"] == []
    ranges2 = res2.get("unresolved_ranges") or []
    assert _covers(ranges2, 2)
    assert _covers(ranges2, 4)
    assert _covers(ranges2, 5)

    # Loop <= 8 cycles until ranges == []
    for _ in range(8):
        with psycopg.connect(
            host=dual_pg_cluster.native_pg_host,
            port=dual_pg_cluster.native_pg_port,
            user="postgres",
            dbname=dual_pg_cluster.native_pg_database,
            autocommit=True,
        ) as native_admin_conn:
            native_admin_conn.execute("SELECT txid_current()")
        target_consumer.consume_batch(k_conn)
        _, _, _, m = target_consumer.get_checkpoint_state(k_conn)
        if (m.get("unresolved_ranges") or []) == []:
            break

    _, _, _, meta_final = target_consumer.get_checkpoint_state(k_conn)
    assert (meta_final.get("unresolved_ranges") or []) == []
    assert meta_final.get("finalized_gap_count") == 3
    assert "expired_gaps" not in meta_final
    assert "dropped_gaps" not in meta_final

    # Target observations == 3 and none for foreign event ids
    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_target,))
        assert cur.fetchone()["count"] == 3

        foreign_obs_ids = [deterministic_event_observation_id(ws_foreign, ev["event_id"]) for ev in foreign_events]
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE observation_id = ANY(%s)", (foreign_obs_ids,))
        assert cur.fetchone()["count"] == 0

    # A ws_foreign consumer with the same consumer_name captures exactly its 3 events and shares no checkpoint row
    foreign_consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_foreign,
        actor_id=actor_foreign,
        repository_id=repo_foreign,
        consumer_name=consumer_name,
        gap_horizon_cycles=2,
    )
    for _ in range(3):
        if foreign_consumer.get_checkpoint_state(k_conn)[0] >= 5:
            break
        foreign_consumer.consume_batch(k_conn)

    with k_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM omp_knowledge.observations WHERE workspace_id = %s", (ws_foreign,))
        assert cur.fetchone()["count"] == 3

        cur.execute(
            "SELECT count(*) FROM omp_knowledge.native_event_checkpoints WHERE consumer LIKE %s",
            (f"{consumer_name}:%",),
        )
        assert cur.fetchone()["count"] == 2
        cur.execute(
            "SELECT count(*) FROM omp_knowledge.native_event_checkpoints WHERE consumer = %s",
            (f"{consumer_name}:{ws_target}",),
        )
        assert cur.fetchone()["count"] == 1
        cur.execute(
            "SELECT count(*) FROM omp_knowledge.native_event_checkpoints WHERE consumer = %s",
            (f"{consumer_name}:{ws_foreign}",),
        )
        assert cur.fetchone()["count"] == 1

    k_conn.close()


def test_legacy_expired_dropped_metadata_folded_into_unresolved_ranges(dual_pg_cluster: KnowledgeConfig) -> None:
    """Legacy expired_gaps and dropped_gaps in checkpoint metadata are folded into
    unresolved_ranges without loss, and pruned when captured.
    """
    k_conn = get_db_connection(dual_pg_cluster)
    ws_id = uuid4()
    actor_id = uuid4()
    repo_id = uuid4()
    consumer_name = "legacy_fold_consumer"

    consumer = NativeEventConsumer(
        dual_pg_cluster,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name=consumer_name,
    )

    # Seed checkpoint after_sequence=10, metadata {'expired_gaps':[7], 'dropped_gaps':[9]}
    consumer.save_checkpoint(
        k_conn,
        after_sequence=10,
        pending_gaps=[],
        applied_event_ids=[],
        pending_gap_metadata={
            "expired_gaps": [7],
            "dropped_gaps": [9],
        },
    )

    # Insert own row seq 7
    with psycopg.connect(
        host=dual_pg_cluster.native_pg_host,
        port=dual_pg_cluster.native_pg_port,
        user="postgres",
        dbname=dual_pg_cluster.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        ev7 = insert_test_event(native_admin_conn, workspace_id=ws_id, actor_id=actor_id, sequence=7, payload={"s": 7})

    # Cycle -> events_processed 1, observation present, metadata has no expired_gaps/dropped_gaps keys, ranges cover 9 and not 7
    res = consumer.consume_batch(k_conn)
    assert res["events_processed"] == 1

    obs_id = deterministic_event_observation_id(ws_id, ev7["event_id"])
    with k_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
        assert cur.fetchone() is not None

    _, _, _, meta = consumer.get_checkpoint_state(k_conn)
    assert "expired_gaps" not in meta
    assert "dropped_gaps" not in meta
    ranges = meta.get("unresolved_ranges") or []
    assert _covers(ranges, 9)
    assert not _covers(ranges, 7)

    k_conn.close()
