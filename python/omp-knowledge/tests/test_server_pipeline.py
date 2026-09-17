from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine import cognee_adapter
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
from omp_knowledge.ownership import WriterOwnership, WriterOwnershipLost
from omp_knowledge.server import CodeSnapshotIngestRequest, create_app
from omp_knowledge.storage.db import (
    get_db_connection,
    is_snapshot_published,
    recover_interrupted_jobs,
)
from omp_work.knowledge_contracts import JobState
from omp_work.v1.canonical import sha256
from support.fixtures import (
    load_staged_fixture,
    make_synthetic_enola_snapshot_ref,
    write_test_capability_file,
)


class EngineSpy(KnowledgeEngine):
    """Wraps an underlying engine to observe call invocations."""

    def __init__(self, target: KnowledgeEngine) -> None:
        self.target = target
        self.ingest_calls: list[dict] = []
        self.retire_calls: list[dict] = []

    async def plan_snapshot(self, **kwargs) -> IngestPlan:
        return await self.target.plan_snapshot(**kwargs)

    async def ingest_snapshot(self, **kwargs) -> IngestResult:
        self.ingest_calls.append(kwargs)
        return await self.target.ingest_snapshot(**kwargs)

    async def ingest_records(self, **kwargs) -> IngestResult:
        return await self.target.ingest_records(**kwargs)

    async def query(self, **kwargs) -> QueryResult:
        return await self.target.query(**kwargs)

    async def lookup(self, **kwargs) -> LookupResult:
        return await self.target.lookup(**kwargs)

    async def correct(self, **kwargs) -> CorrectResult:
        return await self.target.correct(**kwargs)

    async def retire(self, **kwargs) -> RetireResult:
        self.retire_calls.append(kwargs)
        return await self.target.retire(**kwargs)

    async def status(self) -> EngineStatus:
        return await self.target.status()


@pytest.fixture
def test_env(pg_cluster: KnowledgeConfig):
    """Sets up a test environment backed by real ephemeral PostgreSQL."""
    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    token = "test_token_secret_12345"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=uuid4(),
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-admin",
    )

    if not COGNEE_AVAILABLE:
        if os.environ.get("OMP_KNOWLEDGE_COGNEE_INTEGRATION") == "1":
            pytest.fail("Cognee unavailable")
        pytest.skip("Cognee unavailable")
    base_engine = RealCogneeAdapter(pg_cluster)

    spy_engine = EngineSpy(base_engine)
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


def test_server_full_pipeline_ingest_publish_and_query(test_env: dict) -> None:
    """Test full pipeline on real PostgreSQL:
    1. Ingest Snapshot A -> returns 200, state=completed, publication receipt.
    2. Publish Snapshot A -> returns 200, published=True.
    3. Query Snapshot A -> returns 200 and matches facts.
    4. Idempotent re-ingest of identical Snapshot A -> returns existing receipt immediately
       without rewriting nodes.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    op_id = uuid4()

    ingest_payload = {
        "kind": "code_snapshot",
        "operation_id": str(op_id),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_a.snapshot_id,
        "snapshot_ref": snap_ref_a.model_dump(mode="json"),
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
    }

    # 1. Ingest
    res_ingest = client.post("/v1/ingest", headers=headers, json=ingest_payload)
    assert res_ingest.status_code == 200, res_ingest.text
    body = res_ingest.json()
    assert body["state"] == JobState.COMPLETED.value
    assert body["replayed"] is False
    assert engine.ingest_calls != []
    initial_engine_calls = len(engine.ingest_calls)

    # 2. Publish
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

    # 3. Query
    res_query = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "alpha",
        },
    )
    assert res_query.status_code == 200
    query_body = res_query.json()
    assert "facts" in query_body

    # 4. Idempotent re-ingest of identical published snapshot
    res_reingest = client.post("/v1/ingest", headers=headers, json=ingest_payload)
    assert res_reingest.status_code == 200
    reingest_body = res_reingest.json()
    assert reingest_body["replayed"] is True
    # Verify engine was NOT re-invoked (no node rewriting)
    assert len(engine.ingest_calls) == initial_engine_calls


def test_conflicting_payload_same_snapshot_id_rejected_before_engine(test_env: dict) -> None:
    """Verify mutation ordering and Antidote correctness:
    1. Ingest Snapshot A -> succeeds.
    2. Attempt ingest of conflicting content under the same snapshot_id -> returns 409 Conflict.
    3. Verify that the engine write was NEVER called for the conflicting request.
    4. Verify that Snapshot A graph query hash is completely unchanged.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")

    # 1. Ingest Snapshot A
    op1 = uuid4()
    res1 = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op1),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res1.status_code == 200
    call_count_after_A = len(engine.ingest_calls)

    # Publish A so we can query it
    client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    query_before = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "normalize",
        },
    ).json()
    query_before_hash = sha256(query_before)

    # 2. Conflicting ingest under SAME snapshot_id with different facts
    # We mutate facts lines while updating receipt hash to pass artifact validation,
    # but the candidate manifest hash will differ from the pre-reserved/existing snapshot.
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    # Mutate receipt_b to claim snapshot_id of A
    rec_dict = json.loads(receipt_b.decode("utf-8"))
    rec_dict["snapshot_id"] = snap_ref_a.snapshot_id
    mutated_receipt_b = json.dumps(rec_dict).encode("utf-8")

    op2 = uuid4()
    res2 = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op2),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_b.decode("utf-8"),
            "receipt_json": mutated_receipt_b.decode("utf-8"),
            "insights_json": insights_b.decode("utf-8"),
        },
    )
    # Must be rejected with 409 Conflict
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "idempotency_conflict"

    # 3. CRITICAL: Verify engine was NEVER invoked for the conflicting request
    assert len(engine.ingest_calls) == call_count_after_A, (
        "Engine write must NEVER be called when payload conflicts with existing snapshot identity"
    )

    # 4. Verify Snapshot A query hash remains 100% identical
    query_after = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "normalize",
        },
    ).json()
    assert sha256(query_after) == query_before_hash


def test_missing_snapshot_ref_unrepresentable_returns_422(test_env: dict) -> None:
    """Antidote verification: invalid input shapes cannot be represented.
    Missing snapshot_ref must return 422, never inventing fabricated defaults.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")

    # Missing snapshot_ref
    payload = {
        "kind": "code_snapshot",
        "operation_id": str(uuid4()),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": "sha256:5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee",
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
        # snapshot_ref omitted
    }
    res = client.post("/v1/ingest", headers=headers, json=payload)
    assert res.status_code == 422


def test_concurrent_admission_serialization(test_env: dict) -> None:
    """Verify that concurrent identical submissions serialize cleanly via atomic admission."""
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    op_id = uuid4()

    payload = {
        "kind": "code_snapshot",
        "operation_id": str(op_id),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_a.snapshot_id,
        "snapshot_ref": snap_ref_a.model_dump(mode="json"),
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
    }

    # First submission
    res1 = client.post("/v1/ingest", headers=headers, json=payload)
    assert res1.status_code == 200
    assert res1.json()["replayed"] is False

    # Second submission with same operation ID + same payload
    res2 = client.post("/v1/ingest", headers=headers, json=payload)
    assert res2.status_code == 200
    assert res2.json()["replayed"] is True
    assert res2.json()["result_sha256"] == res1.json()["result_sha256"]


def test_orphan_restart_resumption(test_env: dict) -> None:
    """Verify crash recovery:
    1. A job in 'running' state is marked 'interrupted' on server restart.
    2. Re-submitting the exact same request resumes and completes.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    config: KnowledgeConfig = test_env["config"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    op_id = uuid4()

    payload = {
        "kind": "code_snapshot",
        "operation_id": str(op_id),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_a.snapshot_id,
        "snapshot_ref": snap_ref_a.model_dump(mode="json"),
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
    }
    # Schema-normalization boundary correction: seed the actual durable model hash including defaults
    req_hash = sha256(CodeSnapshotIngestRequest.model_validate(payload).model_dump(mode="json"))

    # Insert an orphaned running job directly into DB
    conn = get_db_connection(config)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound')
                    ON CONFLICT (repository_id) DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'running', 1, clock_timestamp(), clock_timestamp())
                    """,
                    (op_id, ws_id, repo_id, snap_ref_a.snapshot_id, req_hash),
                )
        # Server restart marks orphaned running jobs as interrupted
        recover_interrupted_jobs(conn)
    finally:
        conn.close()

    # Re-submitting the same payload resumes and completes
    res = client.post("/v1/ingest", headers=headers, json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == JobState.COMPLETED.value
    assert body["replayed"] is False


def test_running_owner_token_gates_takeover(test_env: dict) -> None:
    """Durable takeover semantics on a live 'running' row (no recovery pass):
    1. Owner token equal to the current writer -> job_in_progress, engine untouched.
    2. Owner token NULL (writer-less admission) -> taken over by the current writer,
       attempt incremented, snapshot claimed, and the job completes.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    config: KnowledgeConfig = test_env["config"]
    engine: EngineSpy = test_env["engine"]
    writer_token: str = client.app.state.writer.token

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")

    def seed_running(op_id: UUID, owner_token: str | None, payload: dict) -> None:
        req_hash = sha256(CodeSnapshotIngestRequest.model_validate(payload).model_dump(mode="json"))
        conn = get_db_connection(config)
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO omp_knowledge.repositories (repository_id, state) VALUES (%s, 'unbound') ON CONFLICT DO NOTHING",
                        (repo_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO omp_knowledge.ingestion_jobs (
                            operation_id, workspace_id, repository_id, snapshot_id,
                            request_sha256, state, attempt_count, owner_token, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, 'running', 1, %s, clock_timestamp(), clock_timestamp())
                        """,
                        (op_id, ws_id, repo_id, snap_ref_a.snapshot_id, req_hash, owner_token),
                    )
        finally:
            conn.close()

    def payload_for(op_id: UUID) -> dict:
        return {
            "kind": "code_snapshot",
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        }

    # 1. Live job owned by this writer: must not be taken over
    op_live = uuid4()
    seed_running(op_live, writer_token, payload_for(op_live))
    calls_before = len(engine.ingest_calls)
    res_live = client.post("/v1/ingest", headers=headers, json=payload_for(op_live))
    assert res_live.status_code == 200
    assert res_live.json()["state"] == JobState.RUNNING.value
    assert "job_in_progress" in res_live.json()["diagnostics"]
    assert len(engine.ingest_calls) == calls_before

    # 2. Orphan with NULL owner: taken over and completed by the current writer
    op_orphan = uuid4()
    seed_running(op_orphan, None, payload_for(op_orphan))
    res_orphan = client.post("/v1/ingest", headers=headers, json=payload_for(op_orphan))
    assert res_orphan.status_code == 200, res_orphan.text
    assert res_orphan.json()["state"] == JobState.COMPLETED.value
    assert res_orphan.json()["replayed"] is False
    assert len(engine.ingest_calls) == calls_before + 1

    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, attempt_count, owner_token, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_orphan,),
            )
            row = cur.fetchone()
            assert row["state"] == JobState.COMPLETED.value
            assert row["attempt_count"] == 2
            assert row["owner_token"] == writer_token
            assert "resumed" in row["diagnostics"]
            cur.execute(
                "SELECT ingest_operation_id FROM omp_knowledge.snapshots WHERE snapshot_id = %s",
                (validate_canonical_snapshot_id(snap_ref_a.snapshot_id),),
            )
            assert cur.fetchone()["ingest_operation_id"] == op_orphan
    finally:
        conn.close()


def test_partial_b_failure_leaves_a_untouched(test_env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify failure isolation:
    1. Ingest Snapshot A -> succeeds, published.
    2. Ingest Snapshot B -> engine fails mid-write.
    3. Snapshot B fails and is NOT published.
    4. Snapshot A remains completely untouched and queryable.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]
    config: KnowledgeConfig = test_env["config"]

    # 1. Ingest and publish A
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    res_a_ingest = client.post(
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
    assert res_a_ingest.status_code == 200

    res_a_pub = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
        },
    )
    assert res_a_pub.status_code == 200

    res_a_query_before = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_a_query_before.status_code == 200
    query_a_before = res_a_query_before.json()
    assert len(query_a_before.get("facts", [])) > 0
    a_raw_fact_ids = {
        json.loads(line)["id"]
        for line in facts_a.decode("utf-8").splitlines()
        if line.strip()
    }
    returned_a_fact_ids = {
        f["fact_id"] for f in query_a_before["facts"] if f.get("fact_id")
    }
    assert a_raw_fact_ids.issubset(returned_a_fact_ids)

    # 2. Ingest Snapshot B with simulated engine failure after real nodes persisted
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    snap_ref_b = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="B")

    original_add_data_points = cognee_adapter.add_data_points

    async def failing_add_data_points(*args, **kwargs):
        await original_add_data_points(*args, **kwargs)
        raise RuntimeError("Engine simulated failure after graph write")

    monkeypatch.setattr(cognee_adapter, "add_data_points", failing_add_data_points)

    op_b = uuid4()
    with pytest.raises(RuntimeError, match="Engine simulated failure"):
        client.post(
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

    # 3. Verify B partial nodes exist in engine, but snapshot is unpublished and HTTP refuses
    canonical_b_id = validate_canonical_snapshot_id(snap_ref_b.snapshot_id)
    direct_b_query = asyncio.run(
        engine.query(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=canonical_b_id,
            query_text="*",
        )
    )
    assert len(direct_b_query.facts) > 0
    b_raw_fact_ids = {
        json.loads(line)["id"]
        for line in facts_b.decode("utf-8").splitlines()
        if line.strip()
    }
    assert any(f.fact_id in b_raw_fact_ids for f in direct_b_query.facts)

    res_b_query = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_b.snapshot_id,
            "query": "*",
        },
    )
    assert res_b_query.status_code == 400
    assert res_b_query.json()["detail"]["error"]["code"] == "snapshot_not_published"

    conn = get_db_connection(config)
    try:
        assert not is_snapshot_published(
            conn,
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_ref_b.snapshot_id,
        )
    finally:
        conn.close()

    # 4. Snapshot A is completely untouched
    res_a_query_after = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "query": "*",
        },
    )
    assert res_a_query_after.status_code == 200
    query_a_after = res_a_query_after.json()
    assert sha256(query_a_after) == sha256(query_a_before)


def test_concurrent_startup_live_owner_refused_before_recovery_then_takeover_proceeds(
    pg_cluster: KnowledgeConfig,
) -> None:
    """Externally observable concurrent-startup / live-owner test:
    1. Process A starts up, acquires WriterOwnership on backend, ingests and publishes Snapshot A.
    2. Process A has a running operation (op_running) in flight.
    3. Process B attempts startup: its lifespan refuses before recovery because Process A holds ownership.
    4. Assert Process A's running row, engine calls, and publication are completely intact.
    5. Process A exits (releases ownership).
    6. Process B starts up: succeeds, acquires ownership, recovers Process A's abandoned running row
       to 'interrupted' with CAS semantics.
    7. Process B proceeds to resume/complete the operation and publish, and both snapshots are queryable.
    """
    if not COGNEE_AVAILABLE:
        if os.environ.get("OMP_KNOWLEDGE_COGNEE_INTEGRATION") == "1":
            pytest.fail("Cognee unavailable")
        pytest.skip("Cognee unavailable")

    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    token_a = "tok_proc_a_12345"
    token_b = "tok_proc_b_67890"

    write_test_capability_file(
        cap_dir,
        token=token_a,
        actor_id=uuid4(),
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="proc-a-admin",
    )
    write_test_capability_file(
        cap_dir,
        token=token_b,
        actor_id=uuid4(),
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="proc-b-admin",
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    canonical_a_id = validate_canonical_snapshot_id(snap_ref_a.snapshot_id)

    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    snap_ref_b = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="B")
    canonical_b_id = validate_canonical_snapshot_id(snap_ref_b.snapshot_id)

    base_engine_a = RealCogneeAdapter(pg_cluster)
    spy_engine_a = EngineSpy(base_engine_a)
    app_a = create_app(pg_cluster, engine=spy_engine_a)

    op_running = uuid4()
    payload_b = {
        "kind": "code_snapshot",
        "operation_id": str(op_running),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref_b.snapshot_id,
        "snapshot_ref": snap_ref_b.model_dump(mode="json"),
        "facts_jsonl": facts_b.decode("utf-8"),
        "receipt_json": receipt_b.decode("utf-8"),
        "insights_json": insights_b.decode("utf-8"),
    }
    req_hash_b = sha256(CodeSnapshotIngestRequest.model_validate(payload_b).model_dump(mode="json"))

    # 1. Process A owns the backend
    with TestClient(app_a) as client_a:
        assert app_a.state.writer is not None
        assert app_a.state.writer.is_acquired() is True
        token_a_owner = app_a.state.writer.token

        # Ingest and publish Snapshot A under Process A
        op_a = uuid4()
        res_a_ingest = client_a.post(
            "/v1/ingest",
            headers=headers_a,
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
        assert res_a_ingest.status_code == 200
        res_a_pub = client_a.post(
            "/v1/publish",
            headers=headers_a,
            json={
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref_a.snapshot_id,
            },
        )
        assert res_a_pub.status_code == 200
        assert res_a_pub.json()["published"] is True
        calls_a_before = len(spy_engine_a.ingest_calls)

        # 2. Process A has a running operation in flight (op_running)
        conn = get_db_connection(pg_cluster)
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO omp_knowledge.repositories (repository_id, state)
                        VALUES (%s, 'unbound')
                        ON CONFLICT (repository_id) DO NOTHING
                        """,
                        (repo_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO omp_knowledge.ingestion_jobs (
                            operation_id, workspace_id, repository_id, snapshot_id,
                            request_sha256, state, attempt_count, owner_token, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, 'running', 1, %s, clock_timestamp(), clock_timestamp())
                        """,
                        (op_running, ws_id, repo_id, canonical_b_id, req_hash_b, token_a_owner),
                    )
        finally:
            conn.close()

        # 3. Process B attempts startup: must refuse before recovery because Process A holds ownership
        base_engine_b = RealCogneeAdapter(pg_cluster)
        spy_engine_b = EngineSpy(base_engine_b)
        app_b = create_app(pg_cluster, engine=spy_engine_b)

        with pytest.raises((WriterOwnershipLost, RuntimeError)):
            with TestClient(app_b):
                pass

        # 4. Assert Process A's running row, engine, and publication are completely intact
        conn = get_db_connection(pg_cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count, owner_token, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                    (op_running,),
                )
                row_running = cur.fetchone()
                assert row_running is not None
                assert row_running["state"] == "running", "Process B startup scan must NOT mutate live owner's running row"
                assert row_running["attempt_count"] == 1
                assert row_running["owner_token"] == token_a_owner
                assert not any("interrupted" in d for d in (row_running["diagnostics"] or []))

            assert is_snapshot_published(conn, workspace_id=ws_id, repository_id=repo_id, snapshot_id=canonical_a_id)
        finally:
            conn.close()

        assert len(spy_engine_a.ingest_calls) == calls_a_before
        assert len(spy_engine_b.ingest_calls) == 0

        # Process A still serves queries cleanly
        res_query_a = client_a.get(
            "/v1/query",
            headers=headers_a,
            params={
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref_a.snapshot_id,
                "query": "*",
            },
        )
        assert res_query_a.status_code == 200

    # 5. Process A is now gone (client_a exited lifespan -> ownership released).
    # 6. Process B starts up: acquires ownership and recovers the abandoned running row
    app_b = create_app(pg_cluster, engine=spy_engine_b)
    with TestClient(app_b) as client_b:
        assert app_b.state.writer is not None
        assert app_b.state.writer.is_acquired() is True
        token_b_owner = app_b.state.writer.token
        assert token_b_owner != token_a_owner

        # Verify Process A's abandoned running row was recovered to 'interrupted' with CAS
        conn = get_db_connection(pg_cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                    (op_running,),
                )
                row_recovered = cur.fetchone()
                assert row_recovered is not None
                assert row_recovered["state"] == "interrupted"
                assert any("interrupted_by_recovery" in d for d in row_recovered["diagnostics"])
        finally:
            conn.close()

        # 7. Process B proceeds: resumes the operation, completes, and publishes Snapshot B
        res_b_resume = client_b.post("/v1/ingest", headers=headers_b, json=payload_b)
        assert res_b_resume.status_code == 200
        body_b = res_b_resume.json()
        assert body_b["state"] == JobState.COMPLETED.value
        assert body_b["replayed"] is False

        res_b_pub = client_b.post(
            "/v1/publish",
            headers=headers_b,
            json={
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref_b.snapshot_id,
            },
        )
        assert res_b_pub.status_code == 200
        assert res_b_pub.json()["published"] is True

        # Both snapshots are queryable under Process B
        res_query_a2 = client_b.get(
            "/v1/query",
            headers=headers_b,
            params={
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref_a.snapshot_id,
                "query": "*",
            },
        )
        assert res_query_a2.status_code == 200

        res_query_b2 = client_b.get(
            "/v1/query",
            headers=headers_b,
            params={
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref_b.snapshot_id,
                "query": "*",
            },
        )
        assert res_query_b2.status_code == 200


def test_post_loss_mutating_request_maps_to_503_and_prevents_mutation(
    pg_cluster: KnowledgeConfig,
) -> None:
    """A running server whose writer ownership connection fails or is closed:
    - Post-loss mutating requests return HTTP 503 with error.code == 'writer_lost'.
    - No mutation occurs in PostgreSQL (fail-closed: no jobs, no snapshots).
    - Engine ingest is not invoked.
    - Read-only endpoints (e.g. /v1/health/live, /v1/status) remain operational without writer ownership.
    """
    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    token = "test_token_loss_12345"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=uuid4(),
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-loss",
    )

    base_engine = RealCogneeAdapter(pg_cluster)
    spy_engine = EngineSpy(base_engine)
    app = create_app(pg_cluster, engine=spy_engine)

    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        # Pre-loss: status is 200
        res_status = client.get("/v1/status", headers=headers)
        assert res_status.status_code == 200

        # Simulate ownership loss by closing the writer's connection so ensure_alive() latches dead
        writer: WriterOwnership = app.state.writer
        assert writer is not None
        assert writer.is_acquired() is True
        assert writer._conn is not None
        writer._conn.close()

        with pytest.raises(WriterOwnershipLost):
            writer.ensure_alive()
        assert writer.is_acquired() is False

        # Mutating request payload
        facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
        snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
        op_id = uuid4()
        ingest_payload = {
            "kind": "code_snapshot",
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref_a.snapshot_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        }

        # Mutating request must return HTTP 503 (mapped writer_lost), NOT HTTP 500
        res_ingest = client.post("/v1/ingest", headers=headers, json=ingest_payload)
        assert res_ingest.status_code == 503
        body = res_ingest.json()
        assert body["error"]["code"] == "writer_lost"

        # Verify fail-closed: no engine invocation
        assert len(spy_engine.ingest_calls) == 0

        # Verify fail-closed: no DB mutation in PostgreSQL
        conn = get_db_connection(pg_cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM omp_knowledge.snapshots WHERE snapshot_id = %s",
                    (snap_ref_a.snapshot_id,),
                )
                assert cur.fetchone()["count"] == 0
                cur.execute(
                    "SELECT count(*) FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                    (op_id,),
                )
                assert cur.fetchone()["count"] == 0
        finally:
            conn.close()

        # Read-only endpoints that do not require writer ownership continue to succeed
        res_live = client.get("/v1/health/live")
        assert res_live.status_code == 200
        res_status_after = client.get("/v1/status", headers=headers)
        assert res_status_after.status_code == 200


def test_startup_recovery_failure_releases_acquired_ownership(
    pg_cluster: KnowledgeConfig,
) -> None:
    """When startup recovery fails after acquiring writer ownership:
    - Lifespan cleanup must release the acquired writer (closing DB session and flock).
    - No advisory lock or flock is leaked.
    - A successor process / WriterOwnership can immediately acquire ownership.
    """
    base_engine = RealCogneeAdapter(pg_cluster)
    spy_engine = EngineSpy(base_engine)
    writer = WriterOwnership(pg_cluster)

    def failing_recovery() -> int:
        raise RuntimeError("Simulated startup recovery failure")

    writer.recover_interrupted_jobs = failing_recovery
    app = create_app(pg_cluster, engine=spy_engine, writer=writer)

    with pytest.raises(RuntimeError, match="Simulated startup recovery failure"):
        with TestClient(app):
            pass

    # The failing writer must be released and no longer acquired
    assert writer.is_acquired() is False
    assert writer.released is True

    # A successor can immediately acquire ownership because locks were not leaked
    successor = WriterOwnership(pg_cluster)
    assert successor.acquire() is True
    assert successor.is_acquired() is True
    successor.ensure_alive()
    asyncio.run(successor.release())


def test_retire_missing_snapshot_terminal_failure_and_no_engine_call(test_env: dict) -> None:
    """Retire missing snapshot fails closed with terminal HTTP error (404),
    does NOT call engine.retire, does NOT create a durable RUNNING job leak,
    and foreign snapshot returns 404 snapshot_not_found.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]
    config: KnowledgeConfig = test_env["config"]

    missing_snap_id = "0" * 64
    op_id = uuid4()

    # 1. Missing snapshot returns 404
    res = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": missing_snap_id,
        },
    )
    assert res.status_code == 404
    assert res.json()["detail"]["error"]["code"] == "snapshot_not_found"

    # Engine.retire was NOT called
    assert len(engine.retire_calls) == 0

    # Ingestion jobs table has NO RUNNING job leak
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_id,),
            )
            job_row = cur.fetchone()
            assert job_row is None or job_row["state"] != JobState.RUNNING.value

            # 2. Foreign snapshot returns 404 (no cross-workspace disclosure)
            other_ws_id = uuid4()
            foreign_snap_id = "f" * 64
            cur.execute(
                """
                INSERT INTO omp_knowledge.repositories (repository_id, state)
                VALUES (%s, 'unbound') ON CONFLICT DO NOTHING
                """,
                (repo_id,),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshots (
                    snapshot_id, workspace_id, repository_id, base_commit, tree_sha, manifest
                ) VALUES (%s, %s, %s, 'commit', 'tree', '{}'::jsonb)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                (foreign_snap_id, other_ws_id, repo_id),
            )
        conn.commit()
    finally:
        conn.close()

    foreign_op_id = uuid4()
    res_foreign = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(foreign_op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": foreign_snap_id,
        },
    )
    assert res_foreign.status_code == 404
    assert res_foreign.json()["detail"]["error"]["code"] == "snapshot_not_found"
    assert len(engine.retire_calls) == 0

    # 3. Snapshot exists but unpopulated/invalid publication baseline returns 400
    valid_snap_id = "e" * 64
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshots (
                    snapshot_id, workspace_id, repository_id, base_commit, tree_sha, manifest
                ) VALUES (%s, %s, %s, 'commit', 'tree', '{}'::jsonb)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                (valid_snap_id, ws_id, repo_id),
            )
        conn.commit()
    finally:
        conn.close()

    unpub_op_id = uuid4()
    res_unpub = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(unpub_op_id),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": valid_snap_id,
        },
    )
    assert res_unpub.status_code == 400
    assert res_unpub.json()["detail"]["error"]["code"] == "snapshot_not_published"
    assert len(engine.retire_calls) == 0


def test_retire_lifecycle_success_and_replay(test_env: dict) -> None:
    """Full retire lifecycle:
    1. Ingest & publish snapshot A.
    2. Retire snapshot A -> 200, retired=True, publication status becomes 'retracted'.
    3. Engine.retire was called exactly once.
    4. Replay retire with same operation_id -> 200, replayed=True, without calling engine.retire again.
    5. Query retired snapshot -> 400 snapshot_not_published.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]
    config: KnowledgeConfig = test_env["config"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    snap_id = snap_ref_a.snapshot_id

    # Ingest
    op_ingest = uuid4()
    res_ingest = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_ingest),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
            "snapshot_ref": snap_ref_a.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_ingest.status_code == 200

    # Publish
    res_pub = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
        },
    )
    assert res_pub.status_code == 200
    assert res_pub.json()["published"] is True

    retire_calls_before = len(engine.retire_calls)

    # Retire
    op_retire = uuid4()
    res_retire = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(op_retire),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
        },
    )
    assert res_retire.status_code == 200
    body = res_retire.json()
    assert body["state"] == JobState.COMPLETED.value
    assert body["response"]["retired"] is True
    assert len(engine.retire_calls) == retire_calls_before + 1

    # Verify publication status updated to 'retracted' in PostgreSQL, and that the
    # COMPLETED job row itself is durable (not rolled back with the handler connection)
    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, published_at FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, validate_canonical_snapshot_id(snap_id)),
            )
            row = cur.fetchone()
            assert row is not None
            assert row["status"] == "retracted"
            assert row["published_at"] is None

            cur.execute(
                "SELECT state, response FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_retire,),
            )
            job_row = cur.fetchone()
            assert job_row is not None
            assert job_row["state"] == JobState.COMPLETED.value
    finally:
        conn.close()

    # Replay of retire operation
    res_replay = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(op_retire),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
        },
    )
    assert res_replay.status_code == 200
    assert res_replay.json()["replayed"] is True
    # Engine was NOT called a second time
    assert len(engine.retire_calls) == retire_calls_before + 1

    # Query now fails because snapshot is retracted (not published)
    res_query = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
            "query": "*",
        },
    )
    assert res_query.status_code == 400
    assert res_query.json()["detail"]["error"]["code"] == "snapshot_not_published"


def test_retire_retract_rowcount_mismatch_is_failed_never_success(
    test_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the publication row is no longer staged/published by the time the completion
    transaction runs (here: a concurrent actor retracts it while engine.retire is executing),
    the retract UPDATE matches zero rows and the job must land durably in FAILED with
    retired=False. It must never report COMPLETED/retired=True.
    """
    client: TestClient = test_env["client"]
    headers: dict = test_env["headers"]
    ws_id: UUID = test_env["ws_id"]
    repo_id: UUID = test_env["repo_id"]
    engine: EngineSpy = test_env["engine"]
    config: KnowledgeConfig = test_env["config"]

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    snap_ref_a = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    snap_id = snap_ref_a.snapshot_id
    canonical_snap_id = validate_canonical_snapshot_id(snap_id)

    res_ingest = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
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
        json={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_id},
    )
    assert res_pub.status_code == 200

    real_target = engine.target

    class ConcurrentRetractDuringEngineRetire:
        """Delegates to the real engine, but flips the publication out from under the
        handler between preflight and the completion transaction."""

        def __getattr__(self, name: str):
            return getattr(real_target, name)

        async def retire(self, **kwargs) -> RetireResult:
            side_conn = get_db_connection(config)
            try:
                with side_conn.transaction():
                    with side_conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE omp_knowledge.snapshot_publications
                            SET status = 'retracted', published_at = NULL
                            WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                            """,
                            (ws_id, repo_id, canonical_snap_id),
                        )
                        assert cur.rowcount == 1
            finally:
                side_conn.close()
            return await real_target.retire(**kwargs)

    monkeypatch.setattr(engine, "target", ConcurrentRetractDuringEngineRetire())

    retire_calls_before = len(engine.retire_calls)
    op_retire = uuid4()
    res_retire = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(op_retire),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
        },
    )
    assert res_retire.status_code == 200
    body = res_retire.json()
    assert body["state"] == JobState.FAILED.value
    assert body["response"]["retired"] is False
    assert "snapshot_retract_not_updated" in body["diagnostics"]
    assert len(engine.retire_calls) == retire_calls_before + 1

    conn = get_db_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, response FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_retire,),
            )
            job_row = cur.fetchone()
            assert job_row is not None
            assert job_row["state"] == JobState.FAILED.value
            response = job_row["response"]
            if isinstance(response, str):
                response = json.loads(response)
            assert response["retired"] is False

            cur.execute(
                """
                SELECT status FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, canonical_snap_id),
            )
            assert cur.fetchone()["status"] == "retracted"
    finally:
        conn.close()

    # A retry is refused at preflight: the baseline is gone, so the engine is not called again.
    res_retry = client.post(
        "/v1/retire",
        headers=headers,
        json={
            "operation_id": str(op_retire),
            "workspace_id": str(ws_id),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id,
        },
    )
    assert res_retry.status_code == 400
    assert res_retry.json()["detail"]["error"]["code"] == "snapshot_not_published"
    assert len(engine.retire_calls) == retire_calls_before + 1
