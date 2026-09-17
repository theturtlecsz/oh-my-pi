from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineStatus, EngineUnavailableError, KnowledgeEngine
from omp_knowledge.native.consumer import NativeEventConsumer
from omp_knowledge.models import validate_canonical_snapshot_id
from omp_knowledge.server import create_app
from omp_knowledge.storage.db import get_retained_artifact, store_retained_artifact
from omp_work.knowledge_contracts import (
    JobState,
    ObservationKind,
    ProviderRoute,
    PublicationStatus,
    SnapshotRef,
    SourceObservation,
    SourceRef,
)
from omp_work.v1.canonical import sha256
from support.fixtures import make_test_snapshot_ref, write_test_capability_file
from support.native_fixture import insert_test_receipt
from support.null_engine import NullEngine


class ExplicitUnavailableEngine(KnowledgeEngine):
    """KnowledgeEngine double that explicitly raises EngineUnavailableError on all operations."""

    def __init__(self, message: str = "engine_unavailable: custom offline cluster") -> None:
        self.message = message
        self._route = ProviderRoute(
            role="graph_engine",
            provider="explicit-double",
            model_id=None,
            endpoint=None,
            graph_only=True,
            model_inferred=False,
            active=False,
        )

    async def plan_snapshot(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def ingest_snapshot(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def ingest_records(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def query(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def lookup(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def correct(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def retire(self, **kwargs: Any) -> Any:
        raise EngineUnavailableError(self.message)

    async def status(self) -> EngineStatus:
        return EngineStatus(
            available=False,
            engine_name="ExplicitUnavailableEngine",
            version=None,
            graph_engine="unavailable",
            active_route=self._route,
            details={"available": False, "reason": self.message},
        )


def _seed_published_snapshot(
    cfg: KnowledgeConfig,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    base_commit: str | None = None,
    tree_sha: str | None = None,
) -> None:
    bare_id = validate_canonical_snapshot_id(snapshot_id)
    snap_ref = make_test_snapshot_ref(
        repository_id=repository_id,
        snapshot_id=bare_id,
    )
    commit = base_commit or snap_ref.base_commit
    tree = tree_sha or snap_ref.tree_sha
    if commit != snap_ref.base_commit or tree != snap_ref.tree_sha:
        snap_ref = snap_ref.model_copy(update={"base_commit": commit, "tree_sha": tree})

    op_id = uuid4()
    req_hash = sha256({"snapshot_id": bare_id, "operation_id": str(op_id)})

    manifest_payload: dict[str, Any] = {
        "schema": "omp_knowledge.snapshot_manifest/v1",
        "snapshot_ref": snap_ref.model_dump(mode="json"),
        "artifacts": {
            "facts": f"blob:sha256:{'0' * 64}",
            "receipt": f"blob:sha256:{'0' * 64}",
            "insights": f"blob:sha256:{'0' * 64}",
        },
    }
    manifest_json = json.dumps(manifest_payload)
    manifest_hash = sha256(manifest_payload)
    graph_hash = "0" * 64
    receipt_hash = "0" * 64

    with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.repositories (repository_id, state)
                VALUES (%s, 'unbound')
                ON CONFLICT (repository_id) DO NOTHING
                """,
                (repository_id,),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.ingestion_jobs (
                    operation_id, workspace_id, repository_id, snapshot_id,
                    request_sha256, state, attempt_count, response, diagnostics, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 1, '{}'::jsonb, '{}', clock_timestamp(), clock_timestamp())
                ON CONFLICT (operation_id) DO NOTHING
                """,
                (
                    op_id,
                    workspace_id,
                    repository_id,
                    bare_id,
                    req_hash,
                    JobState.COMPLETED.value,
                ),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshots (
                    snapshot_id, workspace_id, repository_id, ingest_operation_id,
                    manifest_sha256, base_commit, tree_sha, candidate_tree_sha, manifest, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (snapshot_id) DO UPDATE
                    SET workspace_id = EXCLUDED.workspace_id,
                        repository_id = EXCLUDED.repository_id,
                        ingest_operation_id = EXCLUDED.ingest_operation_id,
                        manifest_sha256 = EXCLUDED.manifest_sha256,
                        base_commit = EXCLUDED.base_commit,
                        tree_sha = EXCLUDED.tree_sha,
                        candidate_tree_sha = EXCLUDED.candidate_tree_sha,
                        manifest = EXCLUDED.manifest
                """,
                (
                    bare_id,
                    workspace_id,
                    repository_id,
                    op_id,
                    manifest_hash,
                    snap_ref.base_commit,
                    snap_ref.tree_sha,
                    snap_ref.candidate_tree_sha,
                    manifest_json,
                ),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshot_publications (
                    publication_id, workspace_id, repository_id, snapshot_id,
                    status, fact_count, insight_count, edge_count,
                    graph_sha256, receipt_sha256, diagnostics, staged_at, published_at
                ) VALUES (%s, %s, %s, %s, %s, 1, 0, 0, %s, %s, '{}', clock_timestamp(), clock_timestamp())
                ON CONFLICT (workspace_id, repository_id, snapshot_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        fact_count = EXCLUDED.fact_count,
                        insight_count = EXCLUDED.insight_count,
                        edge_count = EXCLUDED.edge_count,
                        graph_sha256 = EXCLUDED.graph_sha256,
                        receipt_sha256 = EXCLUDED.receipt_sha256,
                        published_at = EXCLUDED.published_at
                """,
                (
                    uuid4(),
                    workspace_id,
                    repository_id,
                    bare_id,
                    PublicationStatus.PUBLISHED.value,
                    graph_hash,
                    receipt_hash,
                ),
            )


@pytest.fixture
def unavail_setup(pg_cluster: KnowledgeConfig) -> dict[str, Any]:
    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    actor_id = uuid4()
    token = "test_unavail_token_secret"
    snapshot_id = "e" * 64

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-unavail-actor",
    )

    _seed_published_snapshot(
        pg_cluster,
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
    )

    headers = {"Authorization": f"Bearer {token}"}
    return {
        "cfg": pg_cluster,
        "ws_id": ws_id,
        "repo_id": repo_id,
        "actor_id": actor_id,
        "snapshot_id": snapshot_id,
        "headers": headers,
    }


@pytest.fixture
def unavail_dual_setup(dual_pg_cluster: KnowledgeConfig) -> dict[str, Any]:
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    actor_id = uuid4()
    token = "test_unavail_dual_token"
    snapshot_id = "f" * 64

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-unavail-dual-actor",
    )

    # Ensure repository row exists in knowledge DB
    with psycopg.connect(dual_pg_cluster.pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.repositories (repository_id, state)
            VALUES (%s, 'unbound')
            ON CONFLICT (repository_id) DO NOTHING
            """,
            (repo_id,),
        )

    _seed_published_snapshot(
        dual_pg_cluster,
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
    )

    headers = {"Authorization": f"Bearer {token}"}
    return {
        "cfg": dual_pg_cluster,
        "ws_id": ws_id,
        "repo_id": repo_id,
        "actor_id": actor_id,
        "snapshot_id": snapshot_id,
        "headers": headers,
    }


# ==============================================================================
# Contract 1: Query and Lookup map EngineUnavailableError to structured 503
# ==============================================================================


def test_query_with_unavailable_null_engine_maps_to_structured_503(
    unavail_setup: dict[str, Any],
) -> None:
    """When engine.query raises EngineUnavailableError, /v1/query returns structured 503."""
    engine = NullEngine(available=False)
    app = create_app(unavail_setup["cfg"], engine=engine)

    with TestClient(app) as client:
        res = client.get(
            "/v1/query",
            headers=unavail_setup["headers"],
            params={
                "workspace_id": str(unavail_setup["ws_id"]),
                "repository_id": str(unavail_setup["repo_id"]),
                "snapshot_id": unavail_setup["snapshot_id"],
                "query": "TestClass",
            },
        )
        assert res.status_code == 503
        data = res.json()
        assert "error" in data
        assert data["error"]["code"] == "engine_unavailable"
        assert data["error"]["message"].startswith("engine_unavailable:")


def test_lookup_with_unavailable_null_engine_maps_to_structured_503(
    unavail_setup: dict[str, Any],
) -> None:
    """When engine.lookup raises EngineUnavailableError, /v1/lookup returns structured 503."""
    engine = NullEngine(available=False)
    app = create_app(unavail_setup["cfg"], engine=engine)

    with TestClient(app) as client:
        res = client.get(
            "/v1/lookup",
            headers=unavail_setup["headers"],
            params={
                "workspace_id": str(unavail_setup["ws_id"]),
                "repository_id": str(unavail_setup["repo_id"]),
                "snapshot_id": unavail_setup["snapshot_id"],
                "fact_id": "fact-12345",
            },
        )
        assert res.status_code == 503
        data = res.json()
        assert "error" in data
        assert data["error"]["code"] == "engine_unavailable"
        assert data["error"]["message"].startswith("engine_unavailable:")


def test_query_and_lookup_with_unavailable_real_cognee_adapter_maps_to_structured_503(
    unavail_setup: dict[str, Any],
) -> None:
    """RealCogneeAdapter configured unavailable maps query and lookup to structured 503."""
    adapter = RealCogneeAdapter(unavail_setup["cfg"], available=False)
    app = create_app(unavail_setup["cfg"], engine=adapter)

    with TestClient(app) as client:
        # Query path
        res_query = client.get(
            "/v1/query",
            headers=unavail_setup["headers"],
            params={
                "workspace_id": str(unavail_setup["ws_id"]),
                "repository_id": str(unavail_setup["repo_id"]),
                "snapshot_id": unavail_setup["snapshot_id"],
                "query": "SymbolQuery",
            },
        )
        assert res_query.status_code == 503
        q_data = res_query.json()
        assert q_data["error"]["code"] == "engine_unavailable"
        assert "cognee and ladybug packages must be installed" in q_data["error"]["message"]

        # Lookup path
        res_lookup = client.get(
            "/v1/lookup",
            headers=unavail_setup["headers"],
            params={
                "workspace_id": str(unavail_setup["ws_id"]),
                "repository_id": str(unavail_setup["repo_id"]),
                "snapshot_id": unavail_setup["snapshot_id"],
                "fact_id": "fact-sym-001",
            },
        )
        assert res_lookup.status_code == 503
        l_data = res_lookup.json()
        assert l_data["error"]["code"] == "engine_unavailable"
        assert "cognee and ladybug packages must be installed" in l_data["error"]["message"]


def test_explicit_unavailable_engine_message_preserved_in_503_error(
    unavail_setup: dict[str, Any],
) -> None:
    """Custom EngineUnavailableError message is preserved in 503 structured response."""
    custom_msg = "engine_unavailable: GPU cluster unreachable for embedded graph"
    engine = ExplicitUnavailableEngine(message=custom_msg)
    app = create_app(unavail_setup["cfg"], engine=engine)

    with TestClient(app) as client:
        res = client.get(
            "/v1/query",
            headers=unavail_setup["headers"],
            params={
                "workspace_id": str(unavail_setup["ws_id"]),
                "repository_id": str(unavail_setup["repo_id"]),
                "snapshot_id": unavail_setup["snapshot_id"],
                "query": "AnyQuery",
            },
        )
        assert res.status_code == 503
        data = res.json()
        assert data["error"]["code"] == "engine_unavailable"
        assert data["error"]["message"] == custom_msg


# ==============================================================================
# Contract 2: Native exact retained readback / capture remains usable
# ==============================================================================


def test_native_cas_retained_artifact_readback_usable_with_engine_unavailable(
    unavail_setup: dict[str, Any],
) -> None:
    """CAS artifact retention and exact readback operates independently of engine availability."""
    cfg = unavail_setup["cfg"]
    content = b"class AuthoritativeUser:\n    pass\n"
    content_hash = hashlib.sha256(content).hexdigest()

    with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as conn:
        locator = store_retained_artifact(
            conn,
            cfg,
            content_bytes=content,
            metadata={"filename": "authoritative_user.py"},
        )
        assert locator == f"blob:sha256:{content_hash}"

    # Exact byte readback
    retrieved = get_retained_artifact(cfg, content_hash)
    assert retrieved == content

    # Corruption detection remains functional
    disk_path = cfg.artifacts_dir / content_hash[:2] / content_hash
    disk_path.write_bytes(b"tampered-corrupted-bytes")
    with pytest.raises(ValueError, match="Artifact corruption"):
        get_retained_artifact(cfg, content_hash)


def test_native_event_capture_usable_with_engine_unavailable(
    unavail_dual_setup: dict[str, Any],
) -> None:
    """Native event capture from omp_audit.domain_events to knowledge DB succeeds
    when graph engine is offline.
    """
    cfg: KnowledgeConfig = unavail_dual_setup["cfg"]
    ws_id: UUID = unavail_dual_setup["ws_id"]
    repo_id: UUID = unavail_dual_setup["repo_id"]
    actor_id: UUID = unavail_dual_setup["actor_id"]

    event_id = uuid4()
    payload = {"task": "verify_engine_unavailable", "status": "executed"}
    payload_hash = sha256(payload)
    event_hash = sha256({
        "event_id": str(event_id),
        "payload_sha256": payload_hash,
        "previous_event_sha256": None,
    })

    # Insert domain event directly into native Postgres
    with psycopg.connect(
        host=cfg.native_pg_host,
        port=cfg.native_pg_port,
        user="postgres",
        dbname=cfg.native_pg_database,
        autocommit=True,
    ) as native_admin_conn:
        with native_admin_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (ws_id,),
            )
            cur.execute(
                """
                INSERT INTO omp_audit.domain_events (
                    event_id, workspace_id, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, actor_kind, capability_id, request_id,
                    correlation_id, operation_id, causation_id, event_type, outcome,
                    payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at
                ) VALUES (
                    %s, %s, 'test_item', %s,
                    1, %s, 'agent', %s, %s,
                    %s, %s, %s, 'task_executed', 'success',
                    %s, %s, NULL, %s, clock_timestamp()
                )
                """,
                (
                    event_id,
                    ws_id,
                    uuid4(),
                    actor_id,
                    uuid4(),
                    uuid4(),
                    uuid4(),
                    uuid4(),
                    uuid4(),
                    json.dumps(payload),
                    payload_hash,
                    event_hash,
                ),
            )

    # Consumer runs without touching KnowledgeEngine
    consumer = NativeEventConsumer(
        cfg,
        workspace_id=ws_id,
        actor_id=actor_id,
        repository_id=repo_id,
        consumer_name="unavail_test_consumer",
    )
    with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as knowledge_conn:
        consume_res = consumer.consume_batch(knowledge_conn)
        assert consume_res["events_processed"] >= 1

        # Verify observation captured in knowledge DB with preserved r15 separate hash semantics
        with knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT observation_id, payload_sha256, native_payload_sha256, payload
                FROM omp_knowledge.observations
                WHERE workspace_id = %s
                """,
                (ws_id,),
            )
            row = cur.fetchone()
            assert row is not None

            canonical_payload_sha = row["payload_sha256"] if isinstance(row, dict) else row[1]
            native_payload_sha = row["native_payload_sha256"] if isinstance(row, dict) else row[2]
            stored_payload = row["payload"] if isinstance(row, dict) else row[3]
            payload_data = json.loads(stored_payload) if isinstance(stored_payload, str) else stored_payload

            # Exact native payload hash identity preserved in native_payload_sha256
            assert native_payload_sha == payload_hash

            # Stored observation payload_sha256 hashes transformed observation payload
            assert canonical_payload_sha == sha256(payload_data)
            assert canonical_payload_sha != native_payload_sha
            assert len(canonical_payload_sha) == 64
            assert len(native_payload_sha) == 64
            assert payload_data.get("data") == payload


def test_native_record_ingest_endpoint_usable_with_engine_unavailable(
    unavail_setup: dict[str, Any],
) -> None:
    """POST /v1/ingest with NativeRecordIngestRequest persists observation without engine interaction."""
    engine = NullEngine(available=False)
    app = create_app(unavail_setup["cfg"], engine=engine)

    ws_id: UUID = unavail_setup["ws_id"]
    repo_id: UUID = unavail_setup["repo_id"]
    now = datetime.now(timezone.utc)

    source_ref = SourceRef(
        workspace_id=ws_id,
        repository_id=repo_id,
        producer="client_authoritative_test",
        observed_at=now,
    )
    obs = SourceObservation(
        observation_id=uuid4(),
        source=source_ref,
        kind=ObservationKind.CODE_FACT,
        payload={"fact_key": "native_exact_fact", "value": 42},
        payload_sha256=sha256({"fact_key": "native_exact_fact", "value": 42}),
        observed_at=now,
    )

    ingest_payload = {
        "kind": "native_record",
        "operation_id": str(uuid4()),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "source_ref": source_ref.model_dump(mode="json"),
        "observation": obs.model_dump(mode="json"),
    }

    with TestClient(app) as client:
        res = client.post("/v1/ingest", headers=unavail_setup["headers"], json=ingest_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["state"] == "completed"
        assert "observation_recorded" in data["diagnostics"]

    # Verify directly in DB
    with psycopg.connect(unavail_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT observation_id, payload_sha256 FROM omp_knowledge.observations WHERE observation_id = %s",
                (obs.observation_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[1] == obs.payload_sha256


def test_defined_explicit_degraded_behavior_proposal_creation(
    unavail_dual_setup: dict[str, Any],
) -> None:
    """When engine is unavailable, proposal creation completes with authoritative native readback
    verified, but marks enrichment_status explicitly as 'degraded'.
    """
    cfg: KnowledgeConfig = unavail_dual_setup["cfg"]
    ws_id: UUID = unavail_dual_setup["ws_id"]
    repo_id: UUID = unavail_dual_setup["repo_id"]

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=cfg.native_pg_host,
        port=cfg.native_pg_port,
        dbname=cfg.native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=ws_id,
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )

    proposal_body = {
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "title": "Degraded engine proposal creation",
        "steps": ["execute step with fallback"],
        "applicability_scope": "algorithms",
        "supporting_evidence": [
            {
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": str(cand_id),
                "producer": "work-service/auditor-settle",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": rec["payload_sha256"],
                "native_validity_ref": str(rec["receipt_id"]),
            }
        ],
    }

    # Initialize app with unavailable engine
    app = create_app(cfg, engine=NullEngine(available=False))
    with TestClient(app) as client:
        res = client.post("/v1/proposals", headers=unavail_dual_setup["headers"], json=proposal_body)
        assert res.status_code == 200
        data = res.json()
        assert data["evidence_binding"] == "verified"
        assert data["enrichment_status"] == "degraded"
        assert len(data["supporting_lineage"]) == 1


def test_defined_explicit_degraded_behavior_context_compilation(
    unavail_dual_setup: dict[str, Any],
) -> None:
    """When engine is unavailable, context compilation retains mandatory native inputs
    and explicitly marks enrichment_status as 'degraded'.
    """
    cfg: KnowledgeConfig = unavail_dual_setup["cfg"]
    ws_id: UUID = unavail_dual_setup["ws_id"]
    repo_id: UUID = unavail_dual_setup["repo_id"]

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=cfg.native_pg_host,
        port=cfg.native_pg_port,
        dbname=cfg.native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=ws_id,
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )

    compile_body = {
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "stage": "implementation",
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }

    app = create_app(cfg, engine=NullEngine(available=False))
    with TestClient(app) as client:
        res = client.post("/v1/context/compile", headers=unavail_dual_setup["headers"], json=compile_body)
        assert res.status_code == 200
        bundle = res.json()
        assert bundle["enrichment_status"] == "degraded"
        assert "work_revision" in bundle["mandatory"]
        assert bundle["mandatory"]["work_revision"]["work_id"] == str(work_id)
        assert len(bundle["mandatory"]["receipts"]) >= 1


# ==============================================================================
# Contract 3: Health/Ready reports degraded and inactive routes
# ==============================================================================


def test_health_ready_reports_degraded_and_inactive_routes_real_cognee_adapter(
    unavail_setup: dict[str, Any],
) -> None:
    """health/ready reports status 'degraded' and inactive routes without live vendor activation
    when RealCogneeAdapter is unavailable.
    """
    adapter = RealCogneeAdapter(unavail_setup["cfg"], available=False)
    app = create_app(unavail_setup["cfg"], engine=adapter)

    with TestClient(app) as client:
        res = client.get("/v1/health/ready")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "degraded"

        engine_data = data["engine"]
        assert engine_data["available"] is False
        assert engine_data["engine_name"] == "RealCogneeAdapter"
        assert engine_data["version"] is None

        route = engine_data["active_route"]
        assert route["active"] is False
        assert route["role"] == "graph_engine"
        assert route["provider"] == "ladybug-embedded"
        assert route["model_id"] is None
        assert route["endpoint"] == "embedded"
        assert route["graph_only"] is True
        assert route["model_inferred"] is False


def test_health_ready_reports_degraded_and_inactive_routes_null_engine(
    unavail_setup: dict[str, Any],
) -> None:
    """health/ready reports status 'degraded' and inactive routes when NullEngine is unavailable."""
    engine = NullEngine(available=False)
    app = create_app(unavail_setup["cfg"], engine=engine)

    with TestClient(app) as client:
        res = client.get("/v1/health/ready")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "degraded"

        engine_data = data["engine"]
        assert engine_data["available"] is False
        assert engine_data["engine_name"] == "NullEngineDouble"
        assert engine_data["version"] is None

        route = engine_data["active_route"]
        assert route["active"] is False
        assert route["role"] == "graph_engine"
        assert route["provider"] == "null-double"
        assert route["model_id"] is None
        assert route["graph_only"] is True
        assert route["model_inferred"] is False


def test_authenticated_status_endpoint_reports_inactive_routes_when_engine_unavailable(
    unavail_setup: dict[str, Any],
) -> None:
    """GET /v1/status reports engine available=False and inactive route when engine is unavailable."""
    adapter = RealCogneeAdapter(unavail_setup["cfg"], available=False)
    app = create_app(unavail_setup["cfg"], engine=adapter)

    with TestClient(app) as client:
        res = client.get("/v1/status", headers=unavail_setup["headers"])
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["actor_id"] == str(unavail_setup["actor_id"])

        engine_data = data["engine"]
        assert engine_data["available"] is False
        assert engine_data["active_route"]["active"] is False
        assert engine_data["active_route"]["model_id"] is None
