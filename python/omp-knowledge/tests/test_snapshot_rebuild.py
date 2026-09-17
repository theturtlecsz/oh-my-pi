from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter
from omp_knowledge.engine.protocol import (
    CorrectResult,
    EngineStatus,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RetireResult,
)
from omp_knowledge.models import validate_canonical_snapshot_id
from omp_knowledge.server import create_app
from omp_knowledge.storage.db import get_db_connection
from omp_work.knowledge_contracts import JobState, PublicationStatus
from support.fixtures import (
    load_staged_fixture,
    make_synthetic_enola_snapshot_ref,
    write_test_capability_file,
)


class ConfigurableEngineSpy(KnowledgeEngine):
    """Engine spy allowing injection of modified graph_sha256 on rebuild.

    The override applies to both the preflight plan and the write so it models a
    genuinely divergent engine projection, not a spy-only artifact.
    """

    def __init__(self, target: KnowledgeEngine) -> None:
        self.target = target
        self.plan_calls: list[dict] = []
        self.ingest_calls: list[dict] = []
        self.override_graph_sha256: str | None = None

    async def plan_snapshot(self, **kwargs) -> IngestPlan:
        self.plan_calls.append(kwargs)
        plan = await self.target.plan_snapshot(**kwargs)
        if self.override_graph_sha256 is not None:
            return plan.model_copy(update={"graph_sha256": self.override_graph_sha256})
        return plan

    async def ingest_snapshot(self, **kwargs) -> IngestResult:
        self.ingest_calls.append(kwargs)
        res = await self.target.ingest_snapshot(**kwargs)
        if self.override_graph_sha256 is not None:
            return res.model_copy(update={"graph_sha256": self.override_graph_sha256})
        return res

    async def ingest_records(self, **kwargs) -> IngestResult:
        return await self.target.ingest_records(**kwargs)

    async def query(self, **kwargs) -> QueryResult:
        return await self.target.query(**kwargs)

    async def lookup(self, **kwargs) -> LookupResult:
        return await self.target.lookup(**kwargs)

    async def correct(self, **kwargs) -> CorrectResult:
        return await self.target.correct(**kwargs)

    async def retire(self, **kwargs) -> RetireResult:
        return await self.target.retire(**kwargs)

    async def status(self) -> EngineStatus:
        return await self.target.status()


@pytest.fixture
def test_setup(pg_cluster: KnowledgeConfig):
    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    token = "test_s2_token_secret"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=uuid4(),
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s2-admin",
    )

    if not COGNEE_AVAILABLE:
        if os.environ.get("OMP_KNOWLEDGE_COGNEE_INTEGRATION") == "1":
            pytest.fail("Cognee unavailable")
        pytest.skip("Cognee unavailable")

    base_engine = RealCogneeAdapter(pg_cluster)
    spy_engine = ConfigurableEngineSpy(base_engine)
    app = create_app(pg_cluster, engine=spy_engine)

    with TestClient(app) as client:
        yield {
            "config": pg_cluster,
            "client": client,
            "engine": spy_engine,
            "ws_id": ws_id,
            "repo_id": repo_id,
            "token": token,
            "headers": {"Authorization": f"Bearer {token}"},
        }


def test_concurrent_snapshots_real_postgres(test_setup: dict) -> None:
    """Test concurrent snapshot ingestions:
    Multiple concurrent ingest operations for distinct snapshots succeed without deadlocks
    or CAS violations on real PostgreSQL.
    """
    client: TestClient = test_setup["client"]
    headers: dict = test_setup["headers"]
    ws_id: UUID = test_setup["ws_id"]
    repo_id: UUID = test_setup["repo_id"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    op_a = uuid4()

    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    snap_ref_b = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="B")
    op_b = uuid4()

    payload_a = {
        "kind": "code_snapshot",
        "operation_id": str(op_a),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_a.snapshot_id,
        "snapshot_ref": snap_ref_a.model_dump(mode="json"),
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
    }
    payload_b = {
        "kind": "code_snapshot",
        "operation_id": str(op_b),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_b.snapshot_id,
        "snapshot_ref": snap_ref_b.model_dump(mode="json"),
        "facts_jsonl": facts_b.decode("utf-8"),
        "receipt_json": receipt_b.decode("utf-8"),
        "insights_json": insights_b.decode("utf-8"),
    }

    import concurrent.futures

    def do_post(payload: dict) -> Any:
        return client.post("/v1/ingest", headers=headers, json=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_a = executor.submit(do_post, payload_a)
        f_b = executor.submit(do_post, payload_b)
        res_a = f_a.result()
        res_b = f_b.result()

    assert res_a.status_code == 200, res_a.text
    assert res_b.status_code == 200, res_b.text
    assert res_a.json()["state"] == JobState.COMPLETED.value
    assert res_b.json()["state"] == JobState.COMPLETED.value

    # Verify both snapshots recorded in knowledge DB
    conn = get_db_connection(test_setup["config"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.snapshots WHERE workspace_id = %s", (ws_id,))
            assert cur.fetchone()["count"] == 2
    finally:
        conn.close()
def test_partial_publication_failure_preserves_prior_published_snapshot(test_setup: dict) -> None:
    """Partial publication failure test:
    When Snapshot A is published and Snapshot B fails publication gating (e.g. missing CAS artifact or cancelled job),
    Snapshot A's published state, publication receipt, and queryability remain completely preserved and unchanged.
    """
    client: TestClient = test_setup["client"]
    headers: dict = test_setup["headers"]
    ws_id: UUID = test_setup["ws_id"]
    repo_id: UUID = test_setup["repo_id"]
    config: KnowledgeConfig = test_setup["config"]

    # 1. Ingest and publish Snapshot A
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    canonical_a_id = validate_canonical_snapshot_id(snap_ref_a.snapshot_id)
    op_a = uuid4()

    res_ingest_a = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_a),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_ingest_a.status_code == 200

    res_pub_a = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_pub_a.status_code == 200
    assert res_pub_a.json()["published"] is True

    # Capture Snapshot A publication receipt and query state
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, fact_count, graph_sha256, receipt_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            pub_a_record = cur.fetchone()
            assert pub_a_record["status"] == PublicationStatus.PUBLISHED.value
            original_graph_sha_a = pub_a_record["graph_sha256"]
            original_pub_at_a = pub_a_record["published_at"]
    finally:
        conn.close()
    # Query Snapshot A before failure
    query_a_before = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert query_a_before.status_code == 200

    # 2. Ingest Snapshot B (staged)
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    snap_ref_b = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="B")
    op_b = uuid4()

    res_ingest_b = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_b),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_b.snapshot_id,
            "snapshot_ref": snap_ref_b.model_dump(mode="json"),
            "facts_jsonl": facts_b.decode("utf-8"),
            "receipt_json": receipt_b.decode("utf-8"),
            "insights_json": insights_b.decode("utf-8"),
        },
    )
    assert res_ingest_b.status_code == 200

    # 3. Simulate failure for Snapshot B: mark its ingestion job as CANCELLED (a terminal gating refusal)
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE omp_knowledge.ingestion_jobs
                SET state = %s
                WHERE operation_id = %s
                """,
                (JobState.CANCELLED.value, op_b),
            )
        conn.commit()
    finally:
        conn.close()

    # Attempt to publish Snapshot B -> fails gating
    res_pub_b = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_b.snapshot_id,
        },
    )
    assert res_pub_b.status_code == 400
    assert res_pub_b.json()["detail"]["error"]["code"] == "publish_failed"

    # 4. Verify Snapshot A is completely preserved
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, fact_count, graph_sha256, receipt_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            pub_a_after = cur.fetchone()
            assert pub_a_after["status"] == PublicationStatus.PUBLISHED.value
            assert pub_a_after["graph_sha256"] == original_graph_sha_a
            assert pub_a_after["published_at"] == original_pub_at_a
            assert pub_a_after["publication_id"] == pub_a_record["publication_id"]
    finally:
        conn.close()

    # Query Snapshot A continues to succeed identically
    query_a_after = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert query_a_after.status_code == 200
    assert query_a_after.json()["facts"] == query_a_before.json()["facts"]


def test_retained_rebuild_hash_equality_and_mismatch(test_setup: dict) -> None:
    """Retained rebuild hash proof:
    1. Rebuild hash equality: Ingest & publish Snapshot A. Rebuild succeeds and matches graph_sha256.
    2. Rebuild hash mismatch: If readback graph hash differs, rebuild marks operation failed
       with explicit 'rebuild_hash_mismatch' and leaves prior published publication unchanged.
    """
    client: TestClient = test_setup["client"]
    headers: dict = test_setup["headers"]
    ws_id: UUID = test_setup["ws_id"]
    repo_id: UUID = test_setup["repo_id"]
    engine: ConfigurableEngineSpy = test_setup["engine"]
    config: KnowledgeConfig = test_setup["config"]

    # Ingest and publish Snapshot A
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    canonical_a_id = validate_canonical_snapshot_id(snap_ref_a.snapshot_id)
    op_a = uuid4()

    res_ingest_a = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_a),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_ingest_a.status_code == 200
    res_pub_a = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_pub_a.status_code == 200
    assert res_pub_a.json()["published"] is True

    # Read original published publication
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            original_pub = cur.fetchone()
            assert original_pub["status"] == PublicationStatus.PUBLISHED.value
            expected_graph_sha = original_pub["graph_sha256"]
    finally:
        conn.close()

    # 1. Rebuild hash EQUALITY
    rebuild_op_1 = uuid4()
    res_rebuild_1 = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_1),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_rebuild_1.status_code == 200
    body_rebuild_1 = res_rebuild_1.json()
    assert body_rebuild_1["state"] == JobState.COMPLETED.value
    assert body_rebuild_1["response"]["rebuilt"] is True
    assert body_rebuild_1["response"]["graph_sha256"] == expected_graph_sha
    # Preflight ran before the write, and the write happened exactly once
    assert len(engine.plan_calls) == 1
    assert len(engine.ingest_calls) == 2  # original ingest + one rebuild write

    # Idempotent replay of the same rebuild operation: no second engine write
    res_rebuild_1_replay = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_1),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_rebuild_1_replay.status_code == 200
    body_replay_1 = res_rebuild_1_replay.json()
    assert body_replay_1["replayed"] is True
    assert body_replay_1["state"] == JobState.COMPLETED.value
    assert body_replay_1["result_sha256"] == body_rebuild_1["result_sha256"]
    assert body_replay_1["response"] == body_rebuild_1["response"]
    res_job_rebuild_1 = client.get(f"/v1/jobs/{rebuild_op_1}", headers=headers)
    assert res_job_rebuild_1.status_code == 200
    assert res_job_rebuild_1.json()["state"] == JobState.COMPLETED.value
    assert res_job_rebuild_1.json()["result_sha256"] == body_rebuild_1["result_sha256"]
    assert body_rebuild_1["response"]["validity_reapplied"] == 0
    assert len(engine.ingest_calls) == 2

    # Engine content baseline before the mismatch attempt
    query_before = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert query_before.status_code == 200
    assert len(query_before.json()["facts"]) > 0
    ingest_calls_before_mismatch = len(engine.ingest_calls)

    # 2. Rebuild hash MISMATCH
    # Instruct engine spy to return a corrupted graph hash on next rebuild
    tampered_graph_sha = "f" * 64
    engine.override_graph_sha256 = tampered_graph_sha

    rebuild_op_2 = uuid4()
    res_rebuild_2 = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_2),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    # Must fail with HTTP 409 and explicit rebuild_hash_mismatch
    assert res_rebuild_2.status_code == 409
    err_body = res_rebuild_2.json()
    assert err_body["error"]["code"] == "rebuild_hash_mismatch"
    assert err_body["error"]["expected_graph_sha256"] == expected_graph_sha
    assert err_body["error"]["actual_graph_sha256"] == tampered_graph_sha

    # Verify durable job in PostgreSQL is marked FAILED with explicit rebuild_hash_mismatch diagnostic
    res_job = client.get(f"/v1/jobs/{rebuild_op_2}", headers=headers)
    assert res_job.status_code == 200
    job_data = res_job.json()
    assert job_data["state"] == JobState.FAILED.value
    assert "rebuild_hash_mismatch" in job_data["diagnostics"]

    # CRITICAL: mismatch was caught by preflight; the engine was never written to
    assert len(engine.ingest_calls) == ingest_calls_before_mismatch, (
        "Rebuild must verify the graph hash before mutating the live engine namespace"
    )

    # CRITICAL: engine content for the published snapshot is byte-identical
    query_after = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert query_after.status_code == 200
    assert query_after.json()["facts"] == query_before.json()["facts"]

    # CRITICAL: Prior published publication in PostgreSQL remains 100% UNCHANGED
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            pub_after_mismatch = cur.fetchone()
            assert pub_after_mismatch["status"] == PublicationStatus.PUBLISHED.value
            assert pub_after_mismatch["graph_sha256"] == expected_graph_sha
            assert pub_after_mismatch["published_at"] == original_pub["published_at"]
            assert pub_after_mismatch["publication_id"] == original_pub["publication_id"]
    finally:
        conn.close()

    # Reset engine spy override
    engine.override_graph_sha256 = None


def test_rebuild_cross_workspace_probe_does_not_reveal_existence(test_setup: dict) -> None:
    """Snapshot rebuild probes must not reveal existence or details of foreign snapshots.
    Probing a foreign snapshot ID returns 404 (snapshot_not_found), indistinguishable from
    a non-existent snapshot, while foreign workspace caller gets 403 (Item 6).
    """
    client: TestClient = test_setup["client"]
    headers: dict = test_setup["headers"]
    ws_id: UUID = test_setup["ws_id"]
    repo_id: UUID = test_setup["repo_id"]
    config: KnowledgeConfig = test_setup["config"]

    # Ingest and publish a valid snapshot under ws_id
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    res_ingest = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_ingest.status_code == 200

    res_pub = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_pub.status_code == 200

    # Set up a second workspace ws_other with its own admin token
    ws_other = uuid4()
    token_other = "token_other_admin_workspace"
    write_test_capability_file(
        config.config_dir / "capabilities",
        token=token_other,
        actor_id=uuid4(),
        workspaces=[ws_other],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-other-admin",
    )
    headers_other = {"Authorization": f"Bearer {token_other}"}

    # 1. Probing the foreign snapshot from ws_other returns generic 404 snapshot_not_found
    probe_foreign = client.post(
        "/v1/rebuild",
        headers=headers_other,
        json={
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_other),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert probe_foreign.status_code == 404
    assert probe_foreign.json()["detail"]["error"]["code"] == "snapshot_not_found"

    # 2. Probing a completely non-existent snapshot ID from ws_other also returns 404 snapshot_not_found
    probe_nonexistent = client.post(
        "/v1/rebuild",
        headers=headers_other,
        json={
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_other),
            "repository_id": str(repo_id),
            "snapshot_id": "0" * 64,
        },
    )
    assert probe_nonexistent.status_code == 404
    assert probe_nonexistent.json()["detail"]["error"]["code"] == "snapshot_not_found"

    # 3. Caller attempting to pass ws_id directly with ws_other token is refused with 403 forbidden
    unauthorized_rebuild = client.post(
        "/v1/rebuild",
        headers=headers_other,
        json={
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert unauthorized_rebuild.status_code == 403


def test_withdrawal_then_retained_rebuild_query_lookup_excludes_withdrawn_real_engine(test_setup: dict) -> None:
    """Journey test on RealCogneeAdapter via ConfigurableEngineSpy:
    - Ingest + publish fixture A.
    - f1 = id of first facts.jsonl line, f2 = id of second.
    - /v1/query '*' contains f1 and f2.
    - POST /v1/corrections withdraw_evidence target {snapshot_id: canonical_a_id, fact_id: f1} -> 200, cleanup job completed.
    - /v1/query excludes f1 and keeps f2; /v1/lookup f1 found False, f2 found True.
    - Simulate engine loss with asyncio.run(engine.retire(...)) (publication stays published); /v1/query returns 0 facts.
    - POST /v1/rebuild (op R) -> 200, state completed, response.graph_sha256 == published graph_sha256, response.validity_reapplied == 1, validity_unapplied == [], result_sha256 non-null.
    - Direct asyncio.run(engine.lookup(f1)) -> found False (Cognee adapter hides withdrawn) and engine.lookup(f2) found True.
    - /v1/query excludes f1, keeps f2; /v1/lookup f1 False, f2 True.
    - Replay op R -> replayed True, same result_sha256 and response, ingest_calls count unchanged.
    - /v1/jobs/R completed with same result_sha256.
    - snapshot_publications row (publication_id, status, graph_sha256, published_at) byte-identical to pre-rebuild.
    """
    client: TestClient = test_setup["client"]
    headers: dict = test_setup["headers"]
    ws_id: UUID = test_setup["ws_id"]
    repo_id: UUID = test_setup["repo_id"]
    engine: ConfigurableEngineSpy = test_setup["engine"]
    config: KnowledgeConfig = test_setup["config"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    canonical_a_id = validate_canonical_snapshot_id(snap_ref_a.snapshot_id)

    fact_lines = [json.loads(line) for line in facts_a.decode("utf-8").strip().splitlines() if line.strip()]
    f1 = fact_lines[0]["id"]
    f2 = fact_lines[1]["id"]

    op_ingest = uuid4()
    res_ingest = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_ingest),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_ingest.status_code == 200

    res_pub = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_pub.status_code == 200
    assert res_pub.json()["published"] is True

    # Read pre-rebuild publication baseline
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            pre_rebuild_pub = cur.fetchone()
            assert pre_rebuild_pub is not None
            assert pre_rebuild_pub["status"] == PublicationStatus.PUBLISHED.value
            published_graph_sha = pre_rebuild_pub["graph_sha256"]
    finally:
        conn.close()

    # GET /v1/query '*' contains f1 and f2
    res_q1 = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_q1.status_code == 200
    q1_fact_ids = {f["fact_id"] for f in res_q1.json()["facts"]}
    assert f1 in q1_fact_ids
    assert f2 in q1_fact_ids

    # POST /v1/corrections withdraw_evidence target {snapshot_id: canonical_a_id, fact_id: f1}
    res_corr = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "kind": "withdraw_evidence",
            "target": {
                "snapshot_id": canonical_a_id,
                "fact_id": f1,
            },
            "reason": "Withdraw fact 1 for real engine rebuild test",
        },
    )
    assert res_corr.status_code == 200
    cleanup_op = res_corr.json()["cleanup_operation_id"]
    res_job = client.get(f"/v1/jobs/{cleanup_op}", headers=headers)
    assert res_job.status_code == 200
    assert res_job.json()["state"] == JobState.COMPLETED.value

    # /v1/query excludes f1 and keeps f2
    res_q2 = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_q2.status_code == 200
    q2_fact_ids = {f["fact_id"] for f in res_q2.json()["facts"]}
    assert f1 not in q2_fact_ids
    assert f2 in q2_fact_ids

    # /v1/lookup f1 found False, f2 found True
    res_l1 = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "fact_id": f1,
        },
    )
    assert res_l1.status_code == 200
    assert res_l1.json()["found"] is False

    res_l2 = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "fact_id": f2,
        },
    )
    assert res_l2.status_code == 200
    assert res_l2.json()["found"] is True

    # Simulate engine loss with asyncio.run(engine.retire(...)) (publication stays published)
    asyncio.run(
        engine.retire(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=canonical_a_id,
        )
    )
    res_q_lost = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_q_lost.status_code == 200
    assert len(res_q_lost.json()["facts"]) == 0

    # POST /v1/rebuild (op R) -> 200, state completed
    rebuild_op_r = uuid4()
    res_rebuild = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_r),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_rebuild.status_code == 200
    body_rebuild = res_rebuild.json()
    assert body_rebuild["state"] == JobState.COMPLETED.value
    assert body_rebuild["response"]["rebuilt"] is True
    assert body_rebuild["response"]["graph_sha256"] == published_graph_sha
    assert body_rebuild["response"]["validity_reapplied"] == 1
    assert body_rebuild["response"]["validity_unapplied"] == []
    assert body_rebuild["result_sha256"] is not None

    # Direct asyncio.run(engine.lookup(f1)) -> found False (Cognee adapter hides withdrawn)
    # and engine.lookup(f2) found True
    direct_l1 = asyncio.run(
        engine.lookup(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=canonical_a_id,
            fact_id=f1,
        )
    )
    assert direct_l1.found is False

    direct_l2 = asyncio.run(
        engine.lookup(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=canonical_a_id,
            fact_id=f2,
        )
    )
    assert direct_l2.found is True

    # /v1/query excludes f1, keeps f2; /v1/lookup f1 False, f2 True
    res_q_after = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_q_after.status_code == 200
    q_after_ids = {f["fact_id"] for f in res_q_after.json()["facts"]}
    assert f1 not in q_after_ids
    assert f2 in q_after_ids

    res_l1_after = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "fact_id": f1,
        },
    )
    assert res_l1_after.status_code == 200
    assert res_l1_after.json()["found"] is False

    res_l2_after = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "fact_id": f2,
        },
    )
    assert res_l2_after.status_code == 200
    assert res_l2_after.json()["found"] is True

    # Replay op R -> replayed True, same result_sha256 and response, ingest_calls count unchanged
    ingest_count_after_first_rebuild = len(engine.ingest_calls)
    res_replay = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_r),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_replay.status_code == 200
    body_replay = res_replay.json()
    assert body_replay["replayed"] is True
    assert body_replay["state"] == JobState.COMPLETED.value
    assert body_replay["result_sha256"] == body_rebuild["result_sha256"]
    assert body_replay["response"] == body_rebuild["response"]
    assert len(engine.ingest_calls) == ingest_count_after_first_rebuild

    # /v1/jobs/R completed with same result_sha256
    res_job_r = client.get(f"/v1/jobs/{rebuild_op_r}", headers=headers)
    assert res_job_r.status_code == 200
    assert res_job_r.json()["state"] == JobState.COMPLETED.value
    assert res_job_r.json()["result_sha256"] == body_rebuild["result_sha256"]

    # snapshot_publications row (publication_id, status, graph_sha256, published_at) byte-identical to pre-rebuild
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_a_id),
            )
            post_rebuild_pub = cur.fetchone()
            assert post_rebuild_pub is not None
            assert post_rebuild_pub["publication_id"] == pre_rebuild_pub["publication_id"]
            assert post_rebuild_pub["status"] == pre_rebuild_pub["status"]
            assert post_rebuild_pub["graph_sha256"] == pre_rebuild_pub["graph_sha256"]
            assert post_rebuild_pub["published_at"] == pre_rebuild_pub["published_at"]
    finally:
        conn.close()
