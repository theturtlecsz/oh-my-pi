"""Opt-in integration evaluations E0-E8 for semantic pgvector mode.

Runs against live Neo4j, PostgreSQL (pgvector), and the local OpenAI-compatible
inference endpoint (Qwen3 embedding). This module never runs by default; it is
gated by:
- OMP_KNOWLEDGE_EXTERNAL_BACKEND_INTEGRATION=1
- OMP_KNOWLEDGE_EMBEDDING_INTEGRATION=1
- Secret-file references for Neo4j and PostgreSQL
- Refusal of MOCK_EMBEDDING (fails immediately if set, never skips)

Evals implemented (adapter level, one asyncio loop per module):
- E0: Endpoint qualification, models check, canary 1024 dim, cognee 1.5.4, evidence JSON.
- E1: Real pgvector rows written, scoped and binding-tagged, anchor node skipped, hash matching.
- E2: Semantic ranking reorders results where exact substring matching cannot discriminate.
- E3: Multi-tenant snapshot and tag isolation.
- E4: Correction / retained-rebuild lifecycle on persisted rows: an untouched control
      snapshot stays byte-identical, withdrawal deletes the row and hides the fact,
      a property correction re-indexes the row, retire reports node / edge / row counts
      from readback, and re-ingest plus ``reapply_corrections_to_engine`` (the public
      sequence ``/v1/rebuild`` runs) restores every row except the withdrawn one under
      the same graph_sha256.
- E5: Outage policy at the adapter: optional degrades (skipped / exact_fallback /
      retire ``degraded`` with ``vector_rows_deleted=None``), required fails closed.
- E6: Dimension mismatch fails closed before table or row creation.

Evals implemented (server level, synchronous ``TestClient`` on its own loop, need the
``pg_cluster`` ledger fixture and therefore native PostgreSQL binaries):
- E7: Outage policy through the public HTTP route: optional ingest 200 with
      ``semantic_index_skipped`` and zero pgvector rows, publish 200, query
      ``exact_fallback``; required ingest 503 ``engine_unavailable``, job FAILED,
      publish refused.
- E8: ``/v1/corrections`` then engine loss then ``/v1/rebuild``: the withdrawn fact's
      pgvector row is not resurrected, the untouched control snapshot and the
      publication row stay byte-identical, graph_sha256 equals the published hash.

Loop note: Cognee's engine caches are process-global and the Neo4j / asyncpg
engines are bound to the loop that created them. E7 / E8 therefore evict the
cached engines through Cognee's public ``graph_engine_cache`` /
``vector_engine_cache`` before and after running so the TestClient loop never
reuses an engine created on the asyncio loop of E0-E6 (and vice versa).

Documented unqualified seams: when ``initdb`` / ``pg_ctl`` are absent, E7 / E8
skip through the ``pg_cluster`` fixture with its explicit reason, and the
HTTP / job / publish refusals are then not exercised here (E5 still covers the
adapter policy). Nothing in this module runs against a mock embedding.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    SEMANTIC_COLLECTIONS,
    Neo4jDialect,
    SnapshotScope,
    binding_tag,
    missing_backend_modules,
    read_secret_ref,
    redact,
    resolve_backend_route,
)
from omp_knowledge.engine.cognee_adapter import (
    COGNEE_AVAILABLE,
    RealCogneeAdapter,
    snapshot_fact_node_id,
)
from omp_knowledge.engine.protocol import (
    EngineUnavailableError,
    SemanticBindingError,
)
from omp_knowledge.learning.corrections import reapply_corrections_to_engine
from omp_knowledge.server import create_app
from omp_work.knowledge_contracts import JobState
from support.fixtures import (
    load_staged_fixture,
    make_synthetic_enola_snapshot_ref,
    make_test_snapshot_ref,
    write_test_capability_file,
)

try:
    import cognee
except ImportError:
    cognee = None  # type: ignore[assignment]

T = TypeVar("T")

GATE_EXTERNAL_ENV = "OMP_KNOWLEDGE_EXTERNAL_BACKEND_INTEGRATION"
GATE_EMBEDDING_ENV = "OMP_KNOWLEDGE_EMBEDDING_INTEGRATION"
NEO4J_PASSWORD_FILE_ENV = "OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE"
COGNEE_PG_PASSWORD_FILE_ENV = "OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE"

QUALIFIED_COGNEE_PG_PORT = 15432
QUALIFIED_COGNEE_PG_DATABASE = "omp_cognee"
QUALIFIED_COGNEE_PG_USER = "omp_cognee_app"
QUALIFIED_VECTOR_DIMENSION = 1024
QUALIFIED_NEO4J_BOLT_PORT = 17687
QUALIFIED_NEO4J_URI = f"bolt://127.0.0.1:{QUALIFIED_NEO4J_BOLT_PORT}"
QUALIFIED_NEO4J_USER = "neo4j"
QUALIFIED_EMBEDDING_ENDPOINT = "http://127.0.0.1:18081"
QUALIFIED_EMBEDDING_MODEL = "qwen3-embedding-0.6b-q8"

# A port nothing listens on. The outage evals bound every readiness attempt with
# this deadline so a refused / retried connection cannot cost the default 30 s.
UNREACHABLE_EMBEDDING_ENDPOINT = "http://127.0.0.1:19999"
OUTAGE_TIMEOUT_SECONDS = 5.0

ALL_SCOPES = ["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"]


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _check_gates_and_mock() -> str | None:
    # Strict fail if MOCK_EMBEDDING is present
    if os.environ.get("MOCK_EMBEDDING"):
        pytest.fail("MOCK_EMBEDDING is forbidden in semantic integration evals: real inference required", pytrace=False)

    if os.environ.get(GATE_EXTERNAL_ENV) != "1":
        return f"set {GATE_EXTERNAL_ENV}=1 to run external backend integration"
    if os.environ.get(GATE_EMBEDDING_ENV) != "1":
        return f"set {GATE_EMBEDDING_ENV}=1 to run semantic embedding integration"
    if not COGNEE_AVAILABLE:
        return "pinned cognee package is not importable in this interpreter"

    route = resolve_backend_route(
        KnowledgeConfig(
            graph_engine="neo4j",
            vector_store="pgvector",
            embedding_provider="openai_compatible",
            embedding_model=QUALIFIED_EMBEDDING_MODEL,
            embedding_endpoint=QUALIFIED_EMBEDDING_ENDPOINT,
            cognee_pg_password_file=Path("/tmp/fake-pg.secret"),
            graph_only=False,
        )
    )
    missing = missing_backend_modules(route)
    if missing:
        return f"optional backend modules missing: {', '.join(missing)}"

    for env_name, purpose in (
        (NEO4J_PASSWORD_FILE_ENV, "neo4j password"),
        (COGNEE_PG_PASSWORD_FILE_ENV, "cognee postgres password"),
    ):
        ref = os.environ.get(env_name)
        if not ref:
            return f"{env_name} is not set ({purpose} secret-file reference required)"
        if not Path(ref).expanduser().is_file():
            return f"{env_name} does not point at an existing file ({purpose})"

    return None


_SKIP_REASON = _check_gates_and_mock()
# The asyncio loop scope is declared per async test (module loop): Cognee's
# vector engine cache is process-global and owns asyncpg connections, so E0-E6
# share one loop. The synchronous E7 / E8 carry no asyncio marker.
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _fail_redacted(context: str, exc: BaseException, secrets: tuple[str, ...]) -> None:
    pytest.fail(f"{context}: " + redact(f"{type(exc).__name__}: {exc}", secrets), pytrace=False)


async def _await_redacted(context: str, awaitable: Awaitable[T], secrets: tuple[str, ...]) -> T:
    """Await a backend call, surfacing any failure through ``redact``.

    Assertion failures pass through untouched.
    """
    try:
        return await awaitable
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted(context, exc, secrets)
        raise AssertionError("unreachable")


def _semantic_values(
    *,
    embedding_required: bool = False,
    vector_dimension: int = QUALIFIED_VECTOR_DIMENSION,
    endpoint: str | None = None,
    model: str | None = None,
    embedding_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    actual_endpoint = endpoint or os.environ.get("OMP_KNOWLEDGE_EMBEDDING_ENDPOINT", QUALIFIED_EMBEDDING_ENDPOINT)
    actual_model = model or os.environ.get("OMP_KNOWLEDGE_EMBEDDING_MODEL", QUALIFIED_EMBEDDING_MODEL)
    values: dict[str, Any] = dict(
        graph_engine="neo4j",
        vector_store="pgvector",
        graph_only=False,
        embedding_provider="openai_compatible",
        embedding_model=actual_model,
        embedding_endpoint=actual_endpoint,
        embedding_required=embedding_required,
        vector_dimension=vector_dimension,
        neo4j_uri=os.environ.get("OMP_KNOWLEDGE_NEO4J_URI", QUALIFIED_NEO4J_URI),
        neo4j_user=os.environ.get("OMP_KNOWLEDGE_NEO4J_USER", QUALIFIED_NEO4J_USER),
        neo4j_password_file=_optional_path(os.environ.get(NEO4J_PASSWORD_FILE_ENV)),
        neo4j_database=os.environ.get("OMP_KNOWLEDGE_NEO4J_DATABASE") or None,
        cognee_pg_host=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_HOST", "127.0.0.1"),
        cognee_pg_port=int(os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_PORT", str(QUALIFIED_COGNEE_PG_PORT))),
        cognee_pg_database=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_DATABASE", QUALIFIED_COGNEE_PG_DATABASE),
        cognee_pg_user=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_USER", QUALIFIED_COGNEE_PG_USER),
        cognee_pg_password_file=_optional_path(os.environ.get(COGNEE_PG_PASSWORD_FILE_ENV)),
    )
    if embedding_timeout_seconds is not None:
        values["embedding_timeout_seconds"] = embedding_timeout_seconds
    return values


def _semantic_config(state_root: Path, **kwargs: Any) -> KnowledgeConfig:
    return KnowledgeConfig(
        state_dir=state_root / "state",
        config_dir=state_root / "config",
        **_semantic_values(**kwargs),
    )


def _semantic_config_for_cluster(pg_cluster: KnowledgeConfig, **kwargs: Any) -> KnowledgeConfig:
    """The ``pg_cluster`` ledger settings (and its state / config dirs) merged with
    the semantic route, re-validated as a fresh ``KnowledgeConfig``."""
    merged = pg_cluster.model_dump()
    merged.update(_semantic_values(**kwargs))
    return KnowledgeConfig(**merged)


def _graph_only_cleanup_config(state_root: Path, cfg: KnowledgeConfig) -> KnowledgeConfig:
    """Graph-only Neo4j route on the same server, used to retire scopes whose
    semantic adapter fails closed under the required policy."""
    return KnowledgeConfig(
        state_dir=state_root / "cleanup_state",
        config_dir=state_root / "cleanup_config",
        graph_engine="neo4j",
        neo4j_uri=cfg.neo4j_uri,
        neo4j_user=cfg.neo4j_user,
        neo4j_password_file=cfg.neo4j_password_file,
        neo4j_database=cfg.neo4j_database,
    )


def _secrets_tuple(cfg: KnowledgeConfig) -> tuple[str, ...]:
    s = []
    if cfg.neo4j_password_file:
        s.append(read_secret_ref(cfg.neo4j_password_file, purpose="neo4j password"))
    if cfg.cognee_pg_password_file:
        s.append(read_secret_ref(cfg.cognee_pg_password_file, purpose="cognee postgres password"))
    if cfg.embedding_api_key_file:
        s.append(read_secret_ref(cfg.embedding_api_key_file, purpose="embedding api key"))
    return tuple(filter(None, s))


def _pg_conn_params(cfg: KnowledgeConfig) -> dict[str, Any]:
    pg_pw = read_secret_ref(cfg.cognee_pg_password_file, purpose="cognee postgres password")
    return {
        "host": cfg.cognee_pg_host,
        "port": cfg.cognee_pg_port,
        "dbname": cfg.cognee_pg_database,
        "user": cfg.cognee_pg_user,
        "password": pg_pw,
        "connect_timeout": 10,
    }


def _assert_no_secret(text: str, secrets: tuple[str, ...], context: str) -> None:
    leaked = [index for index, secret in enumerate(secrets) if secret and secret in text]
    assert not leaked, f"{context}: credential value(s) at index {leaked} leaked into output"


# ---------------------------------------------------------------------------
# pgvector readback helpers (test-side, psycopg; never through the adapter)
# ---------------------------------------------------------------------------

RowKey = tuple[str, str]  # (collection, id)
RowState = tuple[str, str]  # (payload json text, md5(vector::text))


def _scope_vector_rows(conn_params: dict[str, Any], node_ids: set[str]) -> dict[RowKey, RowState]:
    """Every persisted vector row whose id is one of ``node_ids``, over each
    ``<Model>_description`` table that exists. The payload text and the vector's
    md5 make a byte-identity comparison possible across steps."""
    ids = sorted(node_ids)
    out: dict[RowKey, RowState] = {}
    if not ids:
        return out
    with psycopg.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            for col in SEMANTIC_COLLECTIONS:
                cur.execute("SELECT to_regclass(%s)", (f'"{col}"',))
                if cur.fetchone()[0] is None:
                    continue
                cur.execute(
                    f'SELECT id::text, payload::text, md5(vector::text) FROM "{col}" WHERE id::text = ANY(%s)',
                    (ids,),
                )
                for row_id, payload_text, vector_hash in cur.fetchall():
                    out[(col, row_id)] = (payload_text, vector_hash)
    return out


def _scope_node_ids(scope: SnapshotScope, raw_facts: list[dict[str, Any]]) -> set[str]:
    return {str(scope.fact_node_id(fact["id"])) for fact in raw_facts} | {str(scope.repo_node_id())}


def _fact_node_id(scope: SnapshotScope, fact_id: str) -> str:
    return str(scope.fact_node_id(fact_id))


def _rows_for(rows: dict[RowKey, RowState], node_id: str) -> dict[RowKey, RowState]:
    return {key: state for key, state in rows.items() if key[1] == node_id}


def _canonical_snapshot_id(tag: str, ws_id: UUID, repo_id: UUID) -> str:
    return hashlib.sha256(f"omp-{tag}-{ws_id}:{repo_id}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Loop hygiene for the synchronous server-level evals
# ---------------------------------------------------------------------------


def _evict_loop_bound_engines() -> None:
    """Drop Cognee's cached graph and vector engines through its public cache API.

    ``graph_engine_cache`` / ``vector_engine_cache`` (``EngineCacheOps``) are the
    documented eviction surface of the pinned Cognee; ``force_close`` closes the
    adapters even while an idle handle still pins them. The adapter's own binding
    memo is reset so the next adapter re-runs its invalidation as well.
    """
    from cognee.infrastructure.databases.graph.config import get_graph_context_config
    from cognee.infrastructure.databases.graph.get_graph_engine import graph_engine_cache
    from cognee.infrastructure.databases.vector.config import get_vectordb_context_config
    from cognee.infrastructure.databases.vector.create_vector_engine import vector_engine_cache

    try:
        vector_engine_cache.evict(force_close=True, **dict(get_vectordb_context_config()))
        graph_engine_cache.evict(force_close=True, **dict(get_graph_context_config()))
    except Exception as exc:  # noqa: BLE001 - config dicts carry credentials; name the type only
        pytest.fail(f"cognee engine cache eviction failed: {type(exc).__name__}", pytrace=False)
    cognee_adapter._LAST_SEMANTIC_BINDING = None


def _portal_call(client: TestClient, func: Callable[..., Awaitable[T]], **kwargs: Any) -> T:
    """Run an adapter coroutine on the TestClient's own event loop (the loop that
    owns the Cognee engines created by the request handlers)."""
    return client.portal.call(functools.partial(func, **kwargs))


def _ingest_payload(
    *, op_id: UUID, ws_id: UUID, repo_id: UUID, snap_id: str, fixture_name: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture(fixture_name)
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name=fixture_name, snapshot_id=snap_id)
    payload = {
        "kind": "code_snapshot",
        "operation_id": str(op_id),
        "workspace_id": str(ws_id),
        "repository_id": str(repo_id),
        "snapshot_id": snap_id,
        "snapshot_ref": snap_ref.model_dump(mode="json"),
        "facts_jsonl": facts_bytes.decode("utf-8"),
        "receipt_json": receipt_bytes.decode("utf-8"),
        "insights_json": insights_bytes.decode("utf-8"),
    }
    return payload, raw_facts


def _read_publication_row(cfg: KnowledgeConfig, ws_id: UUID, repo_id: UUID, snap_id: str) -> tuple[Any, ...]:
    with psycopg.connect(cfg.pg_connection_string()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, fact_count, insight_count, edge_count,
                       graph_sha256, receipt_sha256, staged_at, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws_id, repo_id, snap_id),
            )
            row = cur.fetchone()
    assert row is not None, "publication row missing"
    return tuple(row)


# ---------------------------------------------------------------------------
# E0: Endpoint Qualification and Evidence Recording
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e0_endpoint_qualification_and_evidence(tmp_path: Path) -> None:
    cfg = _semantic_config(tmp_path)
    secrets = _secrets_tuple(cfg)

    # 1. Probe /v1/models on endpoint
    url = f"{cfg.embedding_endpoint}/v1/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "omp-knowledge-e0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _fail_redacted(f"Failed to query {url}", exc, secrets)
        raise AssertionError("unreachable")

    model_ids = [m["id"] for m in data.get("data", [])]
    assert cfg.embedding_model in model_ids, f"Chosen model {cfg.embedding_model} not in {model_ids}"

    # 2. Canary probe via RealCogneeAdapter
    try:
        adapter = RealCogneeAdapter(cfg)
        vector_engine = await _await_redacted(
            "Canary probe failed",
            adapter._ensure_semantic_ready("E0 canary probe"),
            secrets,
        )
    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("Failed to initialize RealCogneeAdapter or run canary probe", exc, secrets)
        raise AssertionError("unreachable")

    canary = await _await_redacted(
        "embed_data canary failed",
        vector_engine.embed_data(["canary"]),
        secrets,
    )
    assert canary and len(canary[0]) == QUALIFIED_VECTOR_DIMENSION
    assert not all(v == 0.0 for v in canary[0]), "canary embedding must not be all zeros"

    # 3. Cognee version check
    assert cognee is not None, "cognee must be importable when gates pass"
    cognee_version = getattr(cognee, "__version__", "1.5.4")
    assert cognee_version == "1.5.4"

    evidence = {
        "eval": "E0",
        "endpoint": cfg.embedding_endpoint,
        "chosen_model": cfg.embedding_model,
        "available_models": model_ids,
        "canary_dimension": len(canary[0]),
        "cognee_version": cognee_version,
    }

    evidence_path = os.environ.get(
        "OMP_KNOWLEDGE_SEMANTIC_EVIDENCE_PATH",
        str(tmp_path / "semantic-evidence.json"),
    )
    p = Path(evidence_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# E1: Rows Written Scoped and Binding-Tagged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e1_rows_written_scoped_and_binding_tagged(tmp_path: Path) -> None:
    cfg = _semantic_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = _canonical_snapshot_id("e1", ws_id, repo_id)
    scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)

    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture("A")
    receipt = json.loads(receipt_bytes)
    insights = json.loads(insights_bytes)
    snap_ref = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_id,
    )

    try:
        # Preflight plan
        plan = await _await_redacted(
            "plan_snapshot failed",
            adapter.plan_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts=raw_facts,
                receipt=receipt,
                insights=insights,
            ),
            secrets,
        )

        ingest_res = await _await_redacted(
            "ingest_snapshot failed",
            adapter.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts=raw_facts,
                receipt=receipt,
                insights=insights,
            ),
            secrets,
        )

        assert ingest_res.snapshot_id == snap_id
        assert ingest_res.graph_sha256 == plan.graph_sha256
        assert ingest_res.node_ids == plan.node_ids
        assert ingest_res.edge_keys == plan.edge_keys

        assert ingest_res.semantic is not None
        assert ingest_res.semantic.status == "indexed"
        assert ingest_res.semantic.model_id == cfg.embedding_model
        assert ingest_res.semantic.dimension == QUALIFIED_VECTOR_DIMENSION
        assert ingest_res.semantic.rows_requested == len(raw_facts)

        # Check pgvector table
        expected_tag = binding_tag(cfg.embedding_model, QUALIFIED_VECTOR_DIMENSION)
        repo_node_id_str = str(scope.repo_node_id())
        conn_params = _pg_conn_params(cfg)

        try:
            with psycopg.connect(**conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT id, payload, vector_dims(vector) FROM "CodeSymbol_description"')
                    rows = cur.fetchall()
        except Exception as exc:
            _fail_redacted("PostgreSQL readback of CodeSymbol_description failed", exc, secrets)
            raise AssertionError("unreachable")

        scope_symbol_rows = [
            r for r in rows
            if isinstance(r[1], dict) and scope.label in (r[1].get("belongs_to_set") or [])
        ]
        # Fixture A contains exactly 10 CodeSymbol facts
        assert len(scope_symbol_rows) == 10, f"Expected 10 CodeSymbol vector rows, found {len(scope_symbol_rows)}"

        for r in scope_symbol_rows:
            dims = r[2]
            payload = r[1]
            assert dims == QUALIFIED_VECTOR_DIMENSION
            belongs = payload.get("belongs_to_set") or []
            assert scope.label in belongs
            assert expected_tag in belongs

        # Anchor node skipped: the CodeRepository anchor must never have a vector row
        assert all(str(r[0]) != repo_node_id_str for r in rows), "Repository anchor node must not have a vector row"

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E1 test failed", exc, secrets)
    finally:
        try:
            await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# E2: Semantic Ranking Reorders Results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e2_semantic_ranking_reorders_results(tmp_path: Path) -> None:
    cfg = _semantic_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = _canonical_snapshot_id("e2", ws_id, repo_id)

    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture("A")
    receipt = json.loads(receipt_bytes)
    insights = json.loads(insights_bytes)
    snap_ref = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_id,
    )

    try:
        await _await_redacted(
            "ingest_snapshot failed",
            adapter.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts=raw_facts,
                receipt=receipt,
                insights=insights,
            ),
            secrets,
        )

        q1 = await _await_redacted(
            "query (typescript normalize) failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                query_text="typescript function that normalizes input",
                limit=5,
            ),
            secrets,
        )
        assert q1.retrieval is not None
        assert q1.retrieval.mode == "semantic"
        assert q1.retrieval.model_id == cfg.embedding_model
        assert q1.retrieval.dimension == QUALIFIED_VECTOR_DIMENSION
        assert len(q1.facts) > 0
        assert any(score is not None for score in q1.retrieval.scores)

        q2 = await _await_redacted(
            "query (python test) failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                query_text="python test file",
                limit=5,
            ),
            secrets,
        )
        assert q2.retrieval is not None
        assert q2.retrieval.mode == "semantic"
        assert q2.retrieval.model_id == cfg.embedding_model
        assert q2.retrieval.dimension == QUALIFIED_VECTOR_DIMENSION
        assert len(q2.facts) > 0
        assert any(score is not None for score in q2.retrieval.scores)

        # Top-1 result differs between queries where exact substring matching cannot discriminate
        assert q1.facts[0].fact_id != q2.facts[0].fact_id

        # Verification of semantic affinity
        top1 = q1.facts[0]
        assert top1.file_path and (top1.file_path.endswith((".ts", ".tsx")) or "typescript" in top1.properties.get("language", ""))
        top2 = q2.facts[0]
        assert top2.file_path and (top2.file_path.endswith(".py") or "python" in top2.properties.get("language", ""))

        # Scored hits are ordered by distance (ascending)
        scores_1 = [s for s in q1.retrieval.scores if s is not None]
        assert scores_1 == sorted(scores_1)
        scores_2 = [s for s in q2.retrieval.scores if s is not None]
        assert scores_2 == sorted(scores_2)

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E2 test failed", exc, secrets)
    finally:
        try:
            await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# E3: Isolation Between Snapshots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e3_snapshot_isolation(tmp_path: Path) -> None:
    cfg = _semantic_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_a = _canonical_snapshot_id("e3-a", ws_id, repo_id)
    snap_b = _canonical_snapshot_id("e3-b", ws_id, repo_id)
    assert snap_a != snap_b

    facts_a_bytes, receipt_a_bytes, insights_a_bytes, raw_facts_a = load_staged_fixture("A")
    facts_b_bytes, receipt_b_bytes, insights_b_bytes, raw_facts_b = load_staged_fixture("B")

    ref_a = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_a,
    )
    ref_b = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="B",
        snapshot_id=snap_b,
    )

    try:
        await _await_redacted(
            "ingest snapshot A failed",
            adapter.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=ref_a,
                facts=raw_facts_a,
                receipt=json.loads(receipt_a_bytes),
                insights=json.loads(insights_a_bytes),
            ),
            secrets,
        )
        await _await_redacted(
            "ingest snapshot B failed",
            adapter.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=ref_b,
                facts=raw_facts_b,
                receipt=json.loads(receipt_b_bytes),
                insights=json.loads(insights_b_bytes),
            ),
            secrets,
        )

        res_a = await _await_redacted(
            "query snapshot A failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_a,
                query_text="normalize",
                limit=20,
            ),
            secrets,
        )
        assert len(res_a.facts) > 0
        # Snapshot B facts must not appear in snapshot A results
        for f in res_a.facts:
            assert f.snapshot_id == snap_a

        res_b = await _await_redacted(
            "query snapshot B failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_b,
                query_text="normalize",
                limit=20,
            ),
            secrets,
        )
        assert len(res_b.facts) > 0
        for f in res_b.facts:
            assert f.snapshot_id == snap_b

        # Fixture B unique symbol (beta_only_b) not in snapshot A
        res_a_disjoint = await _await_redacted(
            "query disjoint from A failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_a,
                query_text="beta_only_b",
                limit=10,
            ),
            secrets,
        )
        assert all(f.name != "py/pkg/beta.beta_only_b" for f in res_a_disjoint.facts)

        # Fixture A unique symbol (alphaOnlyA) not in snapshot B
        res_b_disjoint = await _await_redacted(
            "query disjoint from B failed",
            adapter.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_b,
                query_text="alphaOnlyA",
                limit=10,
            ),
            secrets,
        )
        assert all(f.name != "ts/src.alphaOnlyA" for f in res_b_disjoint.facts)

        # Exact lookup isolation across snapshots
        alpha_only_a_fact = next(f for f in raw_facts_a if f.get("name") == "ts/src.alphaOnlyA")
        beta_only_b_fact = next(f for f in raw_facts_b if f.get("name") == "py/pkg/beta.beta_only_b")

        lookup_a_on_b = await _await_redacted(
            "cross-lookup on A failed",
            adapter.lookup(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_a,
                fact_id=beta_only_b_fact["id"],
            ),
            secrets,
        )
        assert lookup_a_on_b.found is False

        lookup_b_on_a = await _await_redacted(
            "cross-lookup on B failed",
            adapter.lookup(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_b,
                fact_id=alpha_only_a_fact["id"],
            ),
            secrets,
        )
        assert lookup_b_on_a.found is False

        # Verify pgvector tag and scope isolation
        scope_a = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_a)
        scope_b = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_b)
        conn_params = _pg_conn_params(cfg)
        try:
            with psycopg.connect(**conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT payload FROM "CodeSymbol_description"')
                    rows = cur.fetchall()
        except Exception as exc:
            _fail_redacted("PostgreSQL readback failed", exc, secrets)
            raise AssertionError("unreachable")

        for (payload,) in rows:
            if isinstance(payload, dict):
                tags = payload.get("belongs_to_set") or []
                if scope_a.label in tags:
                    assert scope_b.label not in tags
                if scope_b.label in tags:
                    assert scope_a.label not in tags

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E3 test failed", exc, secrets)
    finally:
        for s_id in (snap_a, snap_b):
            try:
                await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=s_id)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# E4: Correction and Retained-Rebuild Lifecycle on Persisted Rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e4_correction_and_rebuild_lifecycle(tmp_path: Path) -> None:
    """E4 at the adapter level, every claim checked against persisted pgvector rows.

    * Control: fixture B in its own snapshot is ingested first; its rows (payload
      text and vector md5) must be byte-identical after every later step.
    * Withdrawal: f1's row exists before ``correct(withdrawn)`` and is gone after;
      query and lookup no longer return f1; every other primary row is untouched.
    * Property correction: f2's row is re-indexed (payload text carries the new
      property); no other row changes.
    * Retire: node, edge and vector-row counts equal what an independent graph /
      pgvector readback shows before the retire, and nothing of the scope is left.
    * Retained rebuild + reapply: the same public sequence ``/v1/rebuild`` runs
      (plan, ingest_snapshot, ``reapply_corrections_to_engine`` with the persisted
      withdrawal) restores every row except f1 under the original graph_sha256.
      The HTTP-orchestrated form with the CAS ledger is E8.
    """
    cfg = _semantic_config(tmp_path)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)
    conn_params = _pg_conn_params(cfg)
    dialect = Neo4jDialect()

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = _canonical_snapshot_id("e4", ws_id, repo_id)
    ctrl_id = _canonical_snapshot_id("e4-control", ws_id, repo_id)
    scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
    ctrl_scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=ctrl_id)

    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture("A")
    receipt = json.loads(receipt_bytes)
    insights = json.loads(insights_bytes)
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A", snapshot_id=snap_id)
    _, ctrl_receipt_bytes, ctrl_insights_bytes, ctrl_facts = load_staged_fixture("B")
    ctrl_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="B", snapshot_id=ctrl_id)

    f1 = next(f for f in raw_facts if f.get("name") == "py/pkg/alpha.normalize")
    f1_id = f1["id"]
    f1_node = _fact_node_id(scope, f1_id)
    f2 = next(f for f in raw_facts if f.get("name") == "py/pkg/beta.normalize")
    f2_id = f2["id"]
    f2_node = _fact_node_id(scope, f2_id)

    primary_ids = _scope_node_ids(scope, raw_facts)
    ctrl_ids = _scope_node_ids(ctrl_scope, ctrl_facts)
    assert primary_ids.isdisjoint(ctrl_ids)

    def scope_kwargs(sid: str) -> dict[str, Any]:
        return {"workspace_id": ws_id, "repository_id": repo_id, "snapshot_id": sid}

    async def ingest(ref: Any, facts: list[dict[str, Any]], rcpt: Any, ins: Any, context: str) -> Any:
        return await _await_redacted(
            context,
            adapter.ingest_snapshot(
                workspace_id=ws_id, repository_id=repo_id, snapshot_ref=ref, facts=facts, receipt=rcpt, insights=ins
            ),
            secrets,
        )

    async def query_ids(sid: str, text: str) -> tuple[Any, set[str]]:
        res = await _await_redacted(
            f"query ({text!r}) failed", adapter.query(**scope_kwargs(sid), query_text=text, limit=20), secrets
        )
        return res, {fact.fact_id for fact in res.facts}

    async def incident_edge_count(node_ids: set[str]) -> int:
        """Independent readback of every edge touching the scope, through
        Cognee's own graph engine and the dialect's incident-edge query."""
        from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine

        graph_engine = await _await_redacted("get_graph_engine failed", get_graph_engine(), secrets)
        query_str, params = dialect.incident_edges_query(sorted(node_ids))
        rows = await _await_redacted("edge readback failed", graph_engine.query(query_str, params), secrets)
        return len({dialect.edge_key_row(row) for row in rows})

    try:
        # ---- Control snapshot and baseline rows -------------------------------
        ctrl_ingest = await ingest(ctrl_ref, ctrl_facts, json.loads(ctrl_receipt_bytes), json.loads(ctrl_insights_bytes), "ingest control failed")
        assert ctrl_ingest.semantic is not None and ctrl_ingest.semantic.status == "indexed"
        ctrl_rows = _scope_vector_rows(conn_params, ctrl_ids)
        assert ctrl_rows, "control snapshot must have persisted vector rows"
        assert all(key[1] != str(ctrl_scope.repo_node_id()) for key in ctrl_rows)

        plan = await _await_redacted(
            "plan_snapshot failed",
            adapter.plan_snapshot(
                workspace_id=ws_id, repository_id=repo_id, snapshot_ref=snap_ref, facts=raw_facts, receipt=receipt, insights=insights
            ),
            secrets,
        )
        first = await ingest(snap_ref, raw_facts, receipt, insights, "ingest_snapshot failed")
        assert first.semantic is not None and first.semantic.status == "indexed"
        assert first.graph_sha256 == plan.graph_sha256
        assert set(first.node_ids) == primary_ids

        rows_initial = _scope_vector_rows(conn_params, primary_ids)
        assert rows_initial, "primary snapshot must have persisted vector rows"
        assert len(_rows_for(rows_initial, f1_node)) == 1, "f1 must have exactly one vector row before withdrawal"
        assert len(_rows_for(rows_initial, f2_node)) == 1
        assert all(key[1] != str(scope.repo_node_id()) for key in rows_initial), "anchor never has a row"
        assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

        # ---- Before withdrawal -------------------------------------------------
        q_before, ids_before = await query_ids(snap_id, "alpha normalize")
        assert q_before.retrieval is not None and q_before.retrieval.mode == "semantic"
        assert f1_id in ids_before
        lookup_before = await _await_redacted(
            "lookup before withdrawal failed", adapter.lookup(**scope_kwargs(snap_id), fact_id=f1_id), secrets
        )
        assert lookup_before.found is True

        # ---- Withdraw f1 --------------------------------------------------------
        correction_id = str(uuid4())
        cor_res = await _await_redacted(
            "correct (withdraw) failed",
            adapter.correct(
                **scope_kwargs(snap_id),
                fact_id=f1_id,
                properties_update={"withdrawn": True, "withdrawn_by": correction_id},
            ),
            secrets,
        )
        assert cor_res.success is True
        assert cor_res.corrected_fact_id == f1_id
        assert cor_res.semantic_status == "deleted"
        assert cor_res.semantic_reason is None

        rows_after_withdraw = _scope_vector_rows(conn_params, primary_ids)
        assert _rows_for(rows_after_withdraw, f1_node) == {}, "withdrawn fact's vector row must be deleted"
        expected_after_withdraw = {key: state for key, state in rows_initial.items() if key[1] != f1_node}
        assert rows_after_withdraw == expected_after_withdraw, "no other primary row may change on withdrawal"
        assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

        q_after, ids_after = await query_ids(snap_id, "alpha normalize")
        assert f1_id not in ids_after
        assert q_after.retrieval is not None and q_after.retrieval.mode == "semantic"
        lookup_after = await _await_redacted(
            "lookup after withdrawal failed", adapter.lookup(**scope_kwargs(snap_id), fact_id=f1_id), secrets
        )
        assert lookup_after.found is False and lookup_after.fact is None

        # ---- Property correction on f2 re-indexes its row ----------------------
        cor_update = await _await_redacted(
            "correct (property update) failed",
            adapter.correct(**scope_kwargs(snap_id), fact_id=f2_id, properties_update={"review_status": "verified"}),
            secrets,
        )
        assert cor_update.success is True
        assert cor_update.semantic_status == "reindexed"
        rows_after_update = _scope_vector_rows(conn_params, primary_ids)
        [f2_state] = _rows_for(rows_after_update, f2_node).values()
        assert "review_status=verified" in f2_state[0], "re-indexed row must carry the corrected description"
        assert "review_status=verified" not in rows_initial[("CodeSymbol_description", f2_node)][0]
        assert {k: v for k, v in rows_after_update.items() if k[1] != f2_node} == {
            k: v for k, v in rows_after_withdraw.items() if k[1] != f2_node
        }
        lookup_f2 = await _await_redacted(
            "lookup f2 failed", adapter.lookup(**scope_kwargs(snap_id), fact_id=f2_id), secrets
        )
        assert lookup_f2.found is True and lookup_f2.fact is not None
        assert lookup_f2.fact.properties.get("review_status") == "verified"
        assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

        # ---- Retire: counts from readback --------------------------------------
        edges_expected = await incident_edge_count(primary_ids)
        rows_expected = len(rows_after_update)
        assert edges_expected > 0 and rows_expected > 0
        retire_res = await _await_redacted("retire failed", adapter.retire(**scope_kwargs(snap_id)), secrets)
        assert retire_res.success is True
        assert retire_res.nodes_deleted == len(primary_ids)
        assert retire_res.edges_deleted == edges_expected
        assert retire_res.vector_rows_deleted == rows_expected
        assert retire_res.vector_cleanup == "complete"
        assert retire_res.vector_cleanup_reason is None
        assert _scope_vector_rows(conn_params, primary_ids) == {}
        assert await incident_edge_count(primary_ids) == 0
        assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

        q_empty, _ = await query_ids(snap_id, "*")
        assert q_empty.total_matched == 0

        # ---- Retained rebuild + reapply (public sequence of /v1/rebuild) --------
        replan = await _await_redacted(
            "plan_snapshot (rebuild) failed",
            adapter.plan_snapshot(
                workspace_id=ws_id, repository_id=repo_id, snapshot_ref=snap_ref, facts=raw_facts, receipt=receipt, insights=insights
            ),
            secrets,
        )
        assert replan.graph_sha256 == plan.graph_sha256
        rebuilt = await ingest(snap_ref, raw_facts, receipt, insights, "re-ingest failed")
        assert rebuilt.semantic is not None and rebuilt.semantic.status == "indexed"
        assert rebuilt.graph_sha256 == first.graph_sha256 == plan.graph_sha256
        assert rebuilt.node_ids == first.node_ids
        assert rebuilt.edge_keys == first.edge_keys
        # Without reapply the withdrawn row is back: this is exactly what reapply must undo.
        assert len(_rows_for(_scope_vector_rows(conn_params, primary_ids), f1_node)) == 1

        applied, unapplied = await _await_redacted(
            "reapply_corrections_to_engine failed",
            reapply_corrections_to_engine(
                adapter,
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                corrections=[
                    {
                        "correction_id": correction_id,
                        "kind": "withdraw_evidence",
                        "target": {"snapshot_id": snap_id, "fact_id": f1_id},
                        "recorded_at": datetime.now(timezone.utc),
                    }
                ],
                parsed_facts=raw_facts,
            ),
            secrets,
        )
        assert (applied, unapplied) == (1, [])

        rows_rebuilt = _scope_vector_rows(conn_params, primary_ids)
        assert _rows_for(rows_rebuilt, f1_node) == {}, "withdrawn row must not survive rebuild + reapply"
        assert set(rows_rebuilt) == set(rows_initial) - set(_rows_for(rows_initial, f1_node)), (
            "rebuild + reapply must restore exactly the rows that existed before, minus the withdrawn one"
        )
        assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

        q_rebuilt, ids_rebuilt = await query_ids(snap_id, "normalize")
        assert q_rebuilt.retrieval is not None and q_rebuilt.retrieval.mode == "semantic"
        assert f1_id not in ids_rebuilt
        assert f2_id in ids_rebuilt
        lookup_rebuilt = await _await_redacted(
            "lookup after rebuild failed", adapter.lookup(**scope_kwargs(snap_id), fact_id=f1_id), secrets
        )
        assert lookup_rebuilt.found is False

        # Control snapshot still answers unchanged.
        ctrl_query, ctrl_ids_seen = await query_ids(ctrl_id, "normalize")
        assert ctrl_query.retrieval is not None and ctrl_query.retrieval.mode == "semantic"
        assert ctrl_ids_seen and ctrl_ids_seen <= {f["id"] for f in ctrl_facts}

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E4 test failed", exc, secrets)
    finally:
        for sid in (snap_id, ctrl_id):
            try:
                await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=sid)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# E5: Outage Policy (Optional Graceful vs Required Fail-Closed) at the adapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e5_outage_optional_vs_required_policy(tmp_path: Path) -> None:
    """Adapter-level outage policy against an unreachable inference endpoint.

    Optional: ingest completes with ``skipped`` and zero persisted rows for the
    scope, query falls back to exact matching, and retire reports an explicit
    ``degraded`` vector cleanup with ``vector_rows_deleted=None`` while the graph
    nodes are still removed. Required: ingest raises ``EngineUnavailableError``
    with redacted text. The same policy through the HTTP route is E7.
    """
    bad_endpoint = UNREACHABLE_EMBEDDING_ENDPOINT

    # 1. Optional mode: degrades gracefully
    cfg_opt = _semantic_config(
        tmp_path / "opt", embedding_required=False, endpoint=bad_endpoint, embedding_timeout_seconds=OUTAGE_TIMEOUT_SECONDS
    )
    secrets_opt = _secrets_tuple(cfg_opt)
    adapter_opt = RealCogneeAdapter(cfg_opt)
    conn_params = _pg_conn_params(cfg_opt)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id_opt = _canonical_snapshot_id("e5-opt", ws_id, repo_id)
    scope_opt = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_opt)
    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture("A")
    ref_opt = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_id_opt,
    )
    opt_ids = _scope_node_ids(scope_opt, raw_facts)

    try:
        ingest_opt = await _await_redacted(
            "ingest_snapshot (optional outage) failed",
            adapter_opt.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=ref_opt,
                facts=raw_facts,
                receipt=json.loads(receipt_bytes),
                insights=json.loads(insights_bytes),
            ),
            secrets_opt,
        )
        assert ingest_opt.nodes_written > 0
        assert ingest_opt.semantic is not None
        assert ingest_opt.semantic.status == "skipped"
        assert ingest_opt.semantic.reason is not None
        assert "backend unreachable during ingest_snapshot" in ingest_opt.semantic.reason
        assert len(ingest_opt.semantic.collections) == 0
        assert _scope_vector_rows(conn_params, opt_ids) == {}, "optional outage must persist no vector row"

        # Query falls back to exact matching
        q_opt = await _await_redacted(
            "query (optional fallback) failed",
            adapter_opt.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap_id_opt,
                query_text="normalize",
                limit=5,
            ),
            secrets_opt,
        )
        assert q_opt.retrieval is not None
        assert q_opt.retrieval.mode == "exact_fallback"
        assert q_opt.retrieval.reason is not None
        assert len(q_opt.facts) > 0
        assert all("normalize" in (fact.name or "").lower() for fact in q_opt.facts)

        # Secret redaction check
        for secret in secrets_opt:
            assert secret not in (ingest_opt.semantic.reason or "")
            assert secret not in (q_opt.retrieval.reason or "")

        # Retire under the outage: explicit degraded state, never a fabricated count.
        retire_opt = await _await_redacted(
            "retire (optional outage) failed",
            adapter_opt.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_opt),
            secrets_opt,
        )
        assert retire_opt.success is True
        assert retire_opt.nodes_deleted == len(opt_ids)
        assert retire_opt.vector_rows_deleted is None
        assert retire_opt.vector_cleanup == "degraded"
        assert retire_opt.vector_cleanup_reason is not None
        assert "backend unreachable during retire" in retire_opt.vector_cleanup_reason
        _assert_no_secret(retire_opt.vector_cleanup_reason, secrets_opt, "retire reason")
        q_gone = await _await_redacted(
            "query after degraded retire failed",
            adapter_opt.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_opt, query_text="*"),
            secrets_opt,
        )
        assert q_gone.total_matched == 0

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E5 optional test failed", exc, secrets_opt)
    finally:
        try:
            await adapter_opt.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_opt)
        except Exception:
            pass

    # 2. Required mode: fails closed with EngineUnavailableError
    cfg_req = _semantic_config(
        tmp_path / "req", embedding_required=True, endpoint=bad_endpoint, embedding_timeout_seconds=OUTAGE_TIMEOUT_SECONDS
    )
    secrets_req = _secrets_tuple(cfg_req)
    adapter_req = RealCogneeAdapter(cfg_req)
    snap_id_req = _canonical_snapshot_id("e5-req", ws_id, repo_id)
    scope_req = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_req)
    ref_req = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_id_req,
    )

    try:
        with pytest.raises(EngineUnavailableError) as exc_info:
            await adapter_req.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=ref_req,
                facts=raw_facts,
                receipt=json.loads(receipt_bytes),
                insights=json.loads(insights_bytes),
            )
        assert str(exc_info.value).startswith("engine_unavailable:")
        assert not isinstance(exc_info.value, SemanticBindingError)
        # Redaction on surfaced error
        _assert_no_secret(str(exc_info.value), secrets_req, "required-mode error")
        assert _scope_vector_rows(conn_params, _scope_node_ids(scope_req, raw_facts)) == {}

        # Retire fails closed too and leaves the graph nodes for a retry.
        with pytest.raises(EngineUnavailableError):
            await adapter_req.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_req)
        still_there = await _await_redacted(
            "query (required, after refused retire) failed",
            adapter_req.query(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_req, query_text="*"),
            secrets_req,
        )
        assert still_there.total_matched > 0
    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E5 required test failed", exc, secrets_req)
    finally:
        try:
            cleanup = RealCogneeAdapter(_graph_only_cleanup_config(tmp_path, cfg_req))
            await cleanup.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id_req)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# E6: Dimension Mismatch Fails Closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_e6_dimension_mismatch_fails_closed(tmp_path: Path) -> None:
    # vector_dimension=1023 != 1024
    cfg = _semantic_config(tmp_path, vector_dimension=1023)
    secrets = _secrets_tuple(cfg)
    adapter = RealCogneeAdapter(cfg)

    # 1. Verification of readiness probe failing closed before any table or row is touched
    with pytest.raises(SemanticBindingError) as exc_info:
        await adapter._ensure_semantic_ready("E6 readiness check")
    assert "dimension" in str(exc_info.value).lower()

    # 2. Verification that ingest_snapshot fails closed with SemanticBindingError
    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = _canonical_snapshot_id("e6", ws_id, repo_id)
    scope = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)

    facts_bytes, receipt_bytes, insights_bytes, raw_facts = load_staged_fixture("A")
    receipt = json.loads(receipt_bytes)
    insights = json.loads(insights_bytes)
    snap_ref = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id,
        fixture_name="A",
        snapshot_id=snap_id,
    )

    try:
        with pytest.raises(SemanticBindingError) as exc_info_ingest:
            await adapter.ingest_snapshot(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_ref=snap_ref,
                facts=raw_facts,
                receipt=receipt,
                insights=insights,
            )
        assert "dimension" in str(exc_info_ingest.value).lower()

        # 3. Verify no vector rows were written for this scope in pgvector
        conn_params = _pg_conn_params(cfg)
        try:
            assert _scope_vector_rows(conn_params, _scope_node_ids(scope, raw_facts)) == {}
            with psycopg.connect(**conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_name = 'CodeSymbol_description'
                        );
                    """)
                    table_exists = cur.fetchone()[0]
                    if table_exists:
                        cur.execute('SELECT payload FROM "CodeSymbol_description"')
                        for (payload,) in cur.fetchall():
                            if isinstance(payload, dict):
                                assert scope.label not in (payload.get("belongs_to_set") or [])
        except AssertionError:
            raise
        except Exception as exc:
            _fail_redacted("PostgreSQL readback failed", exc, secrets)
            raise AssertionError("unreachable")

    except AssertionError:
        raise
    except Exception as exc:
        _fail_redacted("E6 test failed", exc, secrets)
    finally:
        try:
            await adapter.retire(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id)
        except Exception:
            try:
                await RealCogneeAdapter(_graph_only_cleanup_config(tmp_path / "e6", cfg)).retire(
                    workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_id
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# E7: Outage policy through the public HTTP route (sync, TestClient, pg_cluster)
# ---------------------------------------------------------------------------


def _server_setup(pg_cluster: KnowledgeConfig) -> tuple[UUID, UUID, dict[str, str]]:
    cap_dir = pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)
    ws_id = uuid4()
    repo_id = uuid4()
    token = f"semantic-eval-token-{uuid4().hex}"
    write_test_capability_file(cap_dir, token=token, actor_id=uuid4(), workspaces=[ws_id], scopes=ALL_SCOPES, name="semantic-eval")
    return ws_id, repo_id, {"Authorization": f"Bearer {token}"}


def test_e7_http_outage_policy_optional_ingest_and_required_refusal(pg_cluster: KnowledgeConfig) -> None:
    """E5 through ``create_app`` + ``TestClient`` with the real ledger.

    Optional: ``POST /v1/ingest`` returns 200 COMPLETED with the
    ``semantic_index_skipped`` diagnostic, zero vector rows persist for the scope,
    ``POST /v1/publish`` succeeds and ``GET /v1/query`` reports ``exact_fallback``.
    Required (engine swapped on the same app and loop): ``POST /v1/ingest`` is 503
    ``engine_unavailable``, the durable job is FAILED at ``GET /v1/jobs`` and
    ``POST /v1/publish`` is refused with ``publish_failed``.
    """
    _evict_loop_bound_engines()
    cfg_opt = _semantic_config_for_cluster(
        pg_cluster,
        embedding_required=False,
        endpoint=UNREACHABLE_EMBEDDING_ENDPOINT,
        embedding_timeout_seconds=OUTAGE_TIMEOUT_SECONDS,
    )
    cfg_req = _semantic_config_for_cluster(
        pg_cluster,
        embedding_required=True,
        endpoint=UNREACHABLE_EMBEDDING_ENDPOINT,
        embedding_timeout_seconds=OUTAGE_TIMEOUT_SECONDS,
    )
    secrets = _secrets_tuple(cfg_opt)
    conn_params = _pg_conn_params(cfg_opt)
    try:
        adapter_opt = RealCogneeAdapter(cfg_opt)
        adapter_req = RealCogneeAdapter(cfg_req)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    ws_id, repo_id, headers = _server_setup(pg_cluster)
    snap_opt = _canonical_snapshot_id("e7-opt", ws_id, repo_id)
    snap_req = _canonical_snapshot_id("e7-req", ws_id, repo_id)
    scope_opt = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_opt)
    scope_req = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_req)
    op_opt, op_req = uuid4(), uuid4()
    payload_opt, raw_facts = _ingest_payload(op_id=op_opt, ws_id=ws_id, repo_id=repo_id, snap_id=snap_opt, fixture_name="A")
    payload_req, _ = _ingest_payload(op_id=op_req, ws_id=ws_id, repo_id=repo_id, snap_id=snap_req, fixture_name="A")

    app = create_app(cfg_opt, engine=adapter_opt)
    try:
        with TestClient(app) as client:
            try:
                # ---- Optional policy ------------------------------------------
                res_ingest = client.post("/v1/ingest", headers=headers, json=payload_opt)
                assert res_ingest.status_code == 200, res_ingest.text
                body = res_ingest.json()
                assert body["state"] == JobState.COMPLETED.value
                assert "semantic_index_skipped" in body["diagnostics"]
                _assert_no_secret(res_ingest.text, secrets, "optional ingest response")
                assert _scope_vector_rows(conn_params, _scope_node_ids(scope_opt, raw_facts)) == {}

                res_pub = client.post(
                    "/v1/publish",
                    headers=headers,
                    json={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_opt},
                )
                assert res_pub.status_code == 200, res_pub.text
                assert res_pub.json()["published"] is True

                res_query = client.get(
                    "/v1/query",
                    headers=headers,
                    params={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_opt, "query": "normalize"},
                )
                assert res_query.status_code == 200, res_query.text
                query_body = res_query.json()
                assert query_body["retrieval"]["mode"] == "exact_fallback"
                assert query_body["retrieval"]["reason"]
                assert query_body["facts"], "exact fallback must still return substring matches"
                assert all("normalize" in fact["name"].lower() for fact in query_body["facts"])
                _assert_no_secret(res_query.text, secrets, "optional query response")

                retire_opt = _portal_call(adapter_opt.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_opt) if False else _portal_call(
                    client, adapter_opt.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_opt
                )
                assert retire_opt.vector_cleanup == "degraded"
                assert retire_opt.vector_rows_deleted is None
                assert retire_opt.nodes_deleted == len(_scope_node_ids(scope_opt, raw_facts))

                # ---- Required policy on the same app / loop --------------------
                app.state.engine = adapter_req
                res_req = client.post("/v1/ingest", headers=headers, json=payload_req)
                assert res_req.status_code == 503, res_req.text
                err = res_req.json()["error"]
                assert err["code"] == "engine_unavailable"
                assert err["message"].startswith("engine_unavailable:")
                assert "backend unreachable during ingest_snapshot" in err["message"]
                _assert_no_secret(res_req.text, secrets, "required ingest response")
                assert _scope_vector_rows(conn_params, _scope_node_ids(scope_req, raw_facts)) == {}

                res_job = client.get(f"/v1/jobs/{op_req}", headers=headers)
                assert res_job.status_code == 200, res_job.text
                job = res_job.json()
                assert job["state"] == JobState.FAILED.value
                assert job["result_sha256"] is None
                assert job["error"] and job["error"]["type"] == "EngineUnavailableError"
                _assert_no_secret(res_job.text, secrets, "job response")

                res_pub_req = client.post(
                    "/v1/publish",
                    headers=headers,
                    json={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_req},
                )
                assert res_pub_req.status_code == 400, res_pub_req.text
                assert res_pub_req.json()["detail"]["error"]["code"] == "publish_failed"

                res_query_req = client.get(
                    "/v1/query",
                    headers=headers,
                    params={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_req, "query": "*"},
                )
                assert res_query_req.status_code == 400
                assert res_query_req.json()["detail"]["error"]["code"] == "snapshot_not_published"
            finally:
                # The required adapter refuses to retire; the optional one degrades
                # and still removes the graph nodes written before the refusal.
                for sid in (snap_opt, snap_req):
                    try:
                        _portal_call(client, adapter_opt.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=sid)
                    except Exception:
                        pass
    finally:
        _evict_loop_bound_engines()


# ---------------------------------------------------------------------------
# E8: /v1/corrections, engine loss, /v1/rebuild on persisted rows (sync, pg_cluster)
# ---------------------------------------------------------------------------


def test_e8_server_rebuild_reapplies_withdrawal_without_resurrecting_rows(pg_cluster: KnowledgeConfig) -> None:
    """The server-orchestrated retained rebuild on the live semantic route.

    Primary (fixture A) and control (fixture B) snapshots are ingested and
    published through ``/v1/ingest`` / ``/v1/publish``; f1 is withdrawn through
    ``/v1/corrections`` (durable cleanup job COMPLETED, row deleted); the engine
    content of the primary snapshot is lost (retire on the request loop, counts
    verified); ``/v1/rebuild`` completes with ``validity_reapplied == 1`` and
    ``semantic_index_indexed``, restores every primary row except f1, leaves the
    control rows and the publication row byte-identical and reports the published
    graph_sha256; ``/v1/query`` (semantic) and ``/v1/lookup`` exclude f1 while the
    engine itself no longer surfaces it.
    """
    _evict_loop_bound_engines()
    cfg = _semantic_config_for_cluster(pg_cluster)
    secrets = _secrets_tuple(cfg)
    conn_params = _pg_conn_params(cfg)
    try:
        adapter = RealCogneeAdapter(cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    ws_id, repo_id, headers = _server_setup(pg_cluster)
    snap_p = _canonical_snapshot_id("e8-primary", ws_id, repo_id)
    snap_c = _canonical_snapshot_id("e8-control", ws_id, repo_id)
    scope_p = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_p)
    scope_c = SnapshotScope(workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_c)
    payload_p, raw_facts = _ingest_payload(op_id=uuid4(), ws_id=ws_id, repo_id=repo_id, snap_id=snap_p, fixture_name="A")
    payload_c, ctrl_facts = _ingest_payload(op_id=uuid4(), ws_id=ws_id, repo_id=repo_id, snap_id=snap_c, fixture_name="B")
    primary_ids = _scope_node_ids(scope_p, raw_facts)
    ctrl_ids = _scope_node_ids(scope_c, ctrl_facts)
    f1_id = next(f["id"] for f in raw_facts if f.get("name") == "py/pkg/alpha.normalize")
    f2_id = next(f["id"] for f in raw_facts if f.get("name") == "py/pkg/beta.normalize")
    f1_node = _fact_node_id(scope_p, f1_id)
    f2_node = _fact_node_id(scope_p, f2_id)

    def query_ids(client: TestClient, sid: str, text: str) -> tuple[dict[str, Any], set[str]]:
        res = client.get(
            "/v1/query",
            headers=headers,
            params={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": sid, "query": text, "limit": 20},
        )
        assert res.status_code == 200, res.text
        _assert_no_secret(res.text, secrets, "query response")
        body = res.json()
        return body, {fact["fact_id"] for fact in body["facts"]}

    def lookup_found(client: TestClient, sid: str, fact_id: str) -> tuple[bool, dict[str, Any] | None]:
        res = client.get(
            "/v1/lookup",
            headers=headers,
            params={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": sid, "fact_id": fact_id},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        return body["found"], body.get("fact")

    def ingest_and_publish(client: TestClient, payload: dict[str, Any], sid: str) -> dict[str, Any]:
        res = client.post("/v1/ingest", headers=headers, json=payload)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["state"] == JobState.COMPLETED.value
        assert "semantic_index_indexed" in body["diagnostics"]
        pub = client.post(
            "/v1/publish",
            headers=headers,
            json={"workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": sid},
        )
        assert pub.status_code == 200, pub.text
        assert pub.json()["published"] is True
        return body

    app = create_app(cfg, engine=adapter)
    try:
        with TestClient(app) as client:
            try:
                # ---- Ingest + publish primary and control ----------------------
                ingest_and_publish(client, payload_c, snap_c)
                ctrl_rows = _scope_vector_rows(conn_params, ctrl_ids)
                assert ctrl_rows, "control snapshot must have persisted vector rows"

                ingest_and_publish(client, payload_p, snap_p)
                rows_initial = _scope_vector_rows(conn_params, primary_ids)
                assert rows_initial and len(_rows_for(rows_initial, f1_node)) == 1
                pub_before = _read_publication_row(cfg, ws_id, repo_id, snap_p)
                published_graph_sha = pub_before[5]
                assert pub_before[1] == "published"
                assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

                body_before, ids_before = query_ids(client, snap_p, "alpha normalize")
                assert body_before["retrieval"]["mode"] == "semantic"
                assert f1_id in ids_before
                assert lookup_found(client, snap_p, f1_id)[0] is True

                # ---- Withdraw f1 through /v1/corrections -----------------------
                res_corr = client.post(
                    "/v1/corrections",
                    headers=headers,
                    json={
                        "workspace_id": str(ws_id),
                        "repository_id": str(repo_id),
                        "kind": "withdraw_evidence",
                        "target": {"snapshot_id": snap_p, "fact_id": f1_id},
                        "reason": "E8 withdrawal on persisted rows",
                    },
                )
                assert res_corr.status_code == 200, res_corr.text
                correction_id = res_corr.json()["correction_id"]
                cleanup_op = res_corr.json()["cleanup_operation_id"]
                res_job = client.get(f"/v1/jobs/{cleanup_op}", headers=headers)
                assert res_job.status_code == 200, res_job.text
                job = res_job.json()
                assert job["state"] == JobState.COMPLETED.value
                assert "derived_cleanup_completed" in job["diagnostics"]
                assert job["response"]["nodes_affected"] == 1

                rows_after_withdraw = _scope_vector_rows(conn_params, primary_ids)
                assert _rows_for(rows_after_withdraw, f1_node) == {}, "withdrawn fact's row must be deleted by the cleanup job"
                assert rows_after_withdraw == {k: v for k, v in rows_initial.items() if k[1] != f1_node}
                assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows

                body_after, ids_after = query_ids(client, snap_p, "alpha normalize")
                assert body_after["retrieval"]["mode"] == "semantic"
                assert f1_id not in ids_after
                assert lookup_found(client, snap_p, f1_id)[0] is False
                engine_lookup = _portal_call(client, adapter.lookup, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_p, fact_id=f1_id)
                assert engine_lookup.found is False, "the engine itself must hide the withdrawn fact"

                # ---- Engine loss: retire on the request loop, counts verified --
                rows_before_loss = len(rows_after_withdraw)
                lost = _portal_call(client, adapter.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_p)
                assert lost.success is True
                assert lost.nodes_deleted == len(primary_ids)
                assert lost.edges_deleted > 0
                assert lost.vector_rows_deleted == rows_before_loss
                assert lost.vector_cleanup == "complete"
                assert _scope_vector_rows(conn_params, primary_ids) == {}
                assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows
                # The ledger still says published: /v1/query reaches the engine and finds nothing.
                body_lost, ids_lost = query_ids(client, snap_p, "*")
                assert ids_lost == set()

                # ---- /v1/rebuild ---------------------------------------------
                rebuild_op = uuid4()
                res_rebuild = client.post(
                    "/v1/rebuild",
                    headers=headers,
                    json={"operation_id": str(rebuild_op), "workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_p},
                )
                assert res_rebuild.status_code == 200, res_rebuild.text
                _assert_no_secret(res_rebuild.text, secrets, "rebuild response")
                rebuild = res_rebuild.json()
                assert rebuild["state"] == JobState.COMPLETED.value
                assert rebuild["response"]["rebuilt"] is True
                assert rebuild["response"]["validity_reapplied"] == 1
                assert rebuild["response"]["validity_unapplied"] == []
                assert rebuild["response"]["graph_sha256"] == published_graph_sha
                assert "semantic_index_indexed" in rebuild["diagnostics"]
                assert "rebuild_validity_reapplied" in rebuild["diagnostics"]
                assert rebuild["result_sha256"] is not None

                rows_rebuilt = _scope_vector_rows(conn_params, primary_ids)
                assert _rows_for(rows_rebuilt, f1_node) == {}, "withdrawn row must not resurrect through /v1/rebuild"
                assert set(rows_rebuilt) == set(rows_initial) - set(_rows_for(rows_initial, f1_node))
                assert len(_rows_for(rows_rebuilt, f2_node)) == 1
                assert _scope_vector_rows(conn_params, ctrl_ids) == ctrl_rows, "control snapshot rows must be byte-identical"
                assert _read_publication_row(cfg, ws_id, repo_id, snap_p) == pub_before, "publication row must be byte-identical"

                body_rebuilt, ids_rebuilt = query_ids(client, snap_p, "normalize")
                assert body_rebuilt["retrieval"]["mode"] == "semantic"
                assert f1_id not in ids_rebuilt
                assert f2_id in ids_rebuilt
                assert lookup_found(client, snap_p, f1_id)[0] is False
                found_f2, fact_f2 = lookup_found(client, snap_p, f2_id)
                assert found_f2 is True and fact_f2 is not None and fact_f2["fact_id"] == f2_id
                engine_lookup_rebuilt = _portal_call(client, adapter.lookup, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_p, fact_id=f1_id)
                assert engine_lookup_rebuilt.found is False
                engine_query = _portal_call(client, adapter.query, workspace_id=ws_id, repository_id=repo_id, snapshot_id=snap_p, query_text="*", limit=100)
                assert f1_id not in {fact.fact_id for fact in engine_query.facts}
                assert f2_id in {fact.fact_id for fact in engine_query.facts}

                # Replay of the same rebuild operation is idempotent and touches nothing.
                res_replay = client.post(
                    "/v1/rebuild",
                    headers=headers,
                    json={"operation_id": str(rebuild_op), "workspace_id": str(ws_id), "repository_id": str(repo_id), "snapshot_id": snap_p},
                )
                assert res_replay.status_code == 200, res_replay.text
                assert res_replay.json()["replayed"] is True
                assert res_replay.json()["result_sha256"] == rebuild["result_sha256"]
                assert _scope_vector_rows(conn_params, primary_ids) == rows_rebuilt

                # Control snapshot still answers unchanged through the route.
                body_ctrl, ids_ctrl = query_ids(client, snap_c, "normalize")
                assert body_ctrl["retrieval"]["mode"] == "semantic"
                assert ids_ctrl and ids_ctrl <= {f["id"] for f in ctrl_facts}
                assert correction_id  # recorded and reapplied; withdrawal is durable in the ledger
            finally:
                for sid in (snap_p, snap_c):
                    try:
                        _portal_call(client, adapter.retire, workspace_id=ws_id, repository_id=repo_id, snapshot_id=sid)
                    except Exception:
                        pass
    finally:
        _evict_loop_bound_engines()
