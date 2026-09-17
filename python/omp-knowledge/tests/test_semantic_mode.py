"""Unit tests for semantic pgvector mode in omp-knowledge.

Seam-double tests only: no live database or inference endpoint, and no fake
embedding standing in for capability (see ``tests/support/fake_cognee.py``).
Graph rows are fed in the tuple shape the embedded Ladybug engine returns, so
the r5 ``LadybugDialect`` is exercised unchanged.

Covered contracts:

* ``KnowledgeConfig`` fail-closed validator matrix for ``graph_only=False``;
* runtime refusal of ``MOCK_EMBEDDING``;
* Cognee setter payloads and readback verification for ``set_embedding_config``;
* ``semantic_text`` determinism and 512-character truncation;
* ``_ensure_semantic_ready`` fail-closed cases (canary dimension, zero vector,
  mock engine, reported vector size, reflected table dimension), and that a
  binding mismatch is never degraded by the optional policy;
* query hydration strictly from graph rows, withdrawn facts hidden, exact matches
  appended after semantic hits, ``semantic_empty`` reporting;
* outage policy: ``exact_fallback`` / ``skipped`` when optional,
  ``EngineUnavailableError`` when required;
* ``correct`` recomputes description and tags and re-indexes unless withdrawn;
* ``retire`` deletes vector rows before graph nodes;
* the embedded route is unchanged (``details.vector_indexing`` False, ``graph_only`` True).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig, load_config
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    SEMANTIC_COLLECTIONS,
    SnapshotScope,
    binding_tag,
    semantic_collection,
    semantic_text,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineUnavailableError
from omp_work.knowledge_contracts import FactRecord, SourceRef
from support.fake_cognee import (
    FakeCogneeConfig,
    FakeVectorEngine,
    install_fake_cognee,
    install_fake_vector_engine,
    secret_file,
)

EMBEDDING_MODEL = "qwen3-embedding-0.6b-q8"
SNAPSHOT_ID = "test-snap"


def _make_semantic_config(
    tmp_path: Path,
    *,
    graph_only: bool = False,
    vector_store: str = "pgvector",
    embedding_provider: str = "openai_compatible",
    embedding_model: str | None = EMBEDDING_MODEL,
    embedding_endpoint: str | None = "http://127.0.0.1:18081",
    embedding_required: bool = False,
    vector_dimension: int = 1024,
    has_secret: bool = True,
) -> KnowledgeConfig:
    pw_file = secret_file(tmp_path, "pg.secret", "secret-pg-pw") if has_secret else None
    return KnowledgeConfig(
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        graph_only=graph_only,
        vector_store=vector_store,  # type: ignore[arg-type]
        embedding_provider=embedding_provider,  # type: ignore[arg-type]
        embedding_model=embedding_model,
        embedding_endpoint=embedding_endpoint,
        embedding_required=embedding_required,
        vector_dimension=vector_dimension,
        cognee_pg_password_file=pw_file,
        cognee_pg_host="127.0.0.1",
        cognee_pg_port=15432,
    )


# ---------------------------------------------------------------------------
# Graph-side seam doubles (vector doubles live in support.fake_cognee)
# ---------------------------------------------------------------------------


class FakePoint:
    """Records the keyword fields the adapter hands to the Cognee model class."""

    def __init__(self, **fields: Any) -> None:
        self.fields = dict(fields)
        self.id = fields["id"]
        self.name = fields.get("name")
        self.kind = fields.get("kind")
        self.belongs_to_set = fields.get("belongs_to_set")


class FakeGraphEngine:
    """Graph seam double answering the adapter's dialect-owned Ladybug queries.

    ``rows`` are returned for the scope / retire queries in the tuple shape the
    embedded engine produces, minus any row whose node was deleted through
    ``delete_nodes`` (the embedded engine's ``DETACH DELETE`` removes the node and
    its edges, so the readback the adapter performs afterwards sees neither).
    Per-node edge capture / verify queries see no incident edges. ``get_node``
    serves ``nodes`` with ``properties`` as one JSON string, exactly like the
    embedded engine.
    """

    def __init__(self, rows: list[tuple] | None = None, nodes: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.nodes = dict(nodes or {})
        self.deleted: list[list[str]] = []
        self.deleted_ids: set[str] = set()
        self.added_edges: list[Any] = []

    async def query(self, query_str: str, params: dict[str, Any] | None = None) -> list[Any]:
        if params and "scope_json" in params:
            return [row for row in self.rows if str(row[0]) not in self.deleted_ids]
        return []

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(str(node_id))

    async def delete_nodes(self, node_ids: list[str]) -> None:
        self.deleted.append([str(node_id) for node_id in node_ids])
        for node_id in node_ids:
            self.nodes.pop(str(node_id), None)
            self.deleted_ids.add(str(node_id))

    async def add_edges(self, edges: list[Any]) -> None:
        self.added_edges.extend(edges)


def _scope(workspace_id: UUID, repository_id: UUID) -> SnapshotScope:
    return SnapshotScope(workspace_id=workspace_id, repository_id=repository_id, snapshot_id=SNAPSHOT_ID)


def _tags(scope: SnapshotScope) -> list[str]:
    return [scope.label, binding_tag(EMBEDDING_MODEL, 1024)]


def _graph_row(node_id: UUID, name: str, props: dict[str, Any]) -> tuple[str, str, str]:
    """One scope-query row as the embedded engine returns it: (id, name, JSON properties)."""
    return (str(node_id), name, json.dumps(props))


def _point(node_id: UUID, scope: SnapshotScope, kind: str = "symbol") -> SimpleNamespace:
    """A stored vector row as the adapter tags it: fact node id + [scope label, binding tag]."""
    return SimpleNamespace(id=node_id, kind=kind, belongs_to_set=_tags(scope))


def _fact_record(scope: SnapshotScope, fact_id: str, name: str) -> FactRecord:
    return FactRecord(
        node_id=scope.fact_node_id(fact_id),
        fact_id=fact_id,
        name=name,
        kind="symbol",
        file_path="src/helper.py",
        line=1,
        end_line=4,
        properties={},
        snapshot_id=scope.snapshot_id,
        repository_id=scope.repository_id,
        workspace_id=scope.workspace_id,
    )


def _source_ref(scope: SnapshotScope) -> SourceRef:
    return SourceRef(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        producer="test-semantic-mode",
        observed_at=datetime.now(timezone.utc),
    )


def _wire_graph_seam(monkeypatch: pytest.MonkeyPatch, engine: FakeGraphEngine) -> list[FakePoint]:
    """Point the adapter's graph seams at ``engine`` and return the recorded DataPoints."""
    written: list[FakePoint] = []

    async def fake_get_graph_engine() -> FakeGraphEngine:
        return engine

    async def fake_add_data_points(data_points: list[FakePoint], graph_only: bool = False) -> None:
        assert graph_only is True, "graph writes must stay graph_only"
        for point in data_points:
            written.append(point)
            engine.nodes[str(point.id)] = {
                "id": str(point.id),
                "name": point.name,
                "properties": json.dumps(point.fields, default=str),
            }

    monkeypatch.setattr(cognee_adapter, "get_graph_engine", fake_get_graph_engine, raising=False)
    monkeypatch.setattr(cognee_adapter, "add_data_points", fake_add_data_points, raising=False)
    monkeypatch.setattr(cognee_adapter, "_get_model_class", lambda kind: FakePoint)
    return written


def _semantic_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_engine: FakeVectorEngine,
    **config_overrides: Any,
) -> RealCogneeAdapter:
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    install_fake_vector_engine(monkeypatch, fake_engine)
    return RealCogneeAdapter(_make_semantic_config(tmp_path, **config_overrides), available=True)


# ---------------------------------------------------------------------------
# 1. Config validator matrix
# ---------------------------------------------------------------------------


def test_semantic_config_valid_matrix(tmp_path: Path) -> None:
    cfg = _make_semantic_config(tmp_path)
    assert not cfg.graph_only
    assert cfg.vector_store == "pgvector"
    assert cfg.embedding_provider == "openai_compatible"
    assert cfg.embedding_model == EMBEDDING_MODEL
    assert cfg.embedding_endpoint == "http://127.0.0.1:18081"
    assert cfg.vector_dimension == 1024


def test_semantic_config_requires_pgvector(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph_only=False requires vector_store='pgvector'"):
        _make_semantic_config(tmp_path, vector_store="lancedb")


def test_semantic_config_requires_openai_compatible(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph_only=False requires embedding_provider='openai_compatible'"):
        _make_semantic_config(tmp_path, embedding_provider="none")


def test_semantic_config_requires_non_empty_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph_only=False requires a non-empty embedding_model"):
        _make_semantic_config(tmp_path, embedding_model="")

    with pytest.raises(ValueError, match="graph_only=False requires a non-empty embedding_model"):
        _make_semantic_config(tmp_path, embedding_model=None)


def test_semantic_config_requires_non_empty_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph_only=False requires a non-empty embedding_endpoint"):
        _make_semantic_config(tmp_path, embedding_endpoint="")

    with pytest.raises(ValueError, match="graph_only=False requires a non-empty embedding_endpoint"):
        _make_semantic_config(tmp_path, embedding_endpoint=None)


def test_semantic_config_requires_cognee_pg_password_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph_only=False requires cognee_pg_password_file"):
        _make_semantic_config(tmp_path, has_secret=False)


# ---------------------------------------------------------------------------
# 2. Env vars and MOCK_EMBEDDING refusal
# ---------------------------------------------------------------------------


def test_load_config_parses_embedding_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pw = secret_file(tmp_path, "pg.secret", "secret-pw")
    api_key_file = secret_file(tmp_path, "key.secret", "api-key")
    monkeypatch.setenv("OMP_KNOWLEDGE_GRAPH_ONLY", "0")
    monkeypatch.setenv("OMP_KNOWLEDGE_VECTOR_STORE", "pgvector")
    monkeypatch.setenv("OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_MODEL", "custom-model")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_API_KEY_FILE", str(api_key_file))
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_REQUIRED", "1")
    monkeypatch.setenv("OMP_KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", "42.5")

    cfg = load_config()
    assert not cfg.graph_only
    assert cfg.embedding_provider == "openai_compatible"
    assert cfg.embedding_model == "custom-model"
    assert cfg.embedding_endpoint == "http://127.0.0.1:9999"
    assert cfg.embedding_api_key_file == api_key_file
    assert cfg.embedding_required is True
    assert cfg.embedding_timeout_seconds == 42.5


def test_runtime_refusal_of_mock_embedding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOCK_EMBEDDING", "1")
    cfg = _make_semantic_config(tmp_path)
    install_fake_cognee(monkeypatch, FakeCogneeConfig())

    with pytest.raises(EngineUnavailableError, match="MOCK_EMBEDDING is refused in semantic mode"):
        RealCogneeAdapter(cfg, available=True)


# ---------------------------------------------------------------------------
# 3. Setter payloads and readback
# ---------------------------------------------------------------------------


def test_adapter_binds_set_embedding_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_config = FakeCogneeConfig()
    install_fake_cognee(monkeypatch, fake_config)
    cfg = _make_semantic_config(tmp_path)

    RealCogneeAdapter(cfg, available=True)

    calls = dict(fake_config.calls)
    assert "set_embedding_config" in calls
    emb_payload = calls["set_embedding_config"]
    assert emb_payload["embedding_provider"] == "openai_compatible"
    assert emb_payload["embedding_model"] == EMBEDDING_MODEL
    assert emb_payload["embedding_dimensions"] == 1024
    assert emb_payload["embedding_endpoint"] == "http://127.0.0.1:18081"
    assert emb_payload["embedding_api_key"] == "no-key-required"

    # Semantic mode passes explicit pgvector connection details to Cognee.
    assert "set_vector_db_config" in calls
    vec_payload = calls["set_vector_db_config"]
    assert vec_payload["vector_db_host"] == "127.0.0.1"
    assert vec_payload["vector_db_port"] == 15432


def test_adapter_fails_if_set_embedding_config_drops_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_config = FakeCogneeConfig(ignore_keys={"set_embedding_config": ("embedding_model",)})
    install_fake_cognee(monkeypatch, fake_config)
    cfg = _make_semantic_config(tmp_path)

    with pytest.raises(EngineUnavailableError, match="readback mismatch for keys: embedding_model"):
        RealCogneeAdapter(cfg, available=True)


# ---------------------------------------------------------------------------
# 4. Semantic text and mapping helpers
# ---------------------------------------------------------------------------


def test_semantic_text_sorting_and_determinism() -> None:
    text1 = semantic_text("Function", "compute", "src/math.py", {"b": 2, "a": 1, "nested": [1, 2]})
    text2 = semantic_text("Function", "compute", "src/math.py", {"a": 1, "b": 2, "nested": [3, 4]})
    assert text1 == text2
    assert text1 == "Function compute in src/math.py; a=1, b=2"


def test_semantic_text_truncation_512() -> None:
    long_val = "x" * 600
    res = semantic_text("Class", "Huge", "src/huge.py", {"desc": long_val})
    assert len(res) == 512


def test_semantic_collection_and_binding_tag_mapping() -> None:
    assert semantic_collection("symbol") == "CodeSymbol_description"
    assert semantic_collection("CodeSymbol") == "CodeSymbol_description"
    assert semantic_collection("route") == "ApiEndpoint_description"
    assert binding_tag(EMBEDDING_MODEL, 1024) == "omp-emb:qwen3-embedding-0.6b-q8@1024"
    assert len(SEMANTIC_COLLECTIONS) == 13


# ---------------------------------------------------------------------------
# 5. _ensure_semantic_ready fail-closed cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_wrong_vector_length_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, embed_override=lambda texts: [[0.1] * 512 for _ in texts])
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)

    with pytest.raises(EngineUnavailableError, match="canary embedding returned dimension 512 != expected 1024"):
        await adapter._ensure_semantic_ready()


@pytest.mark.asyncio
async def test_canary_zero_vector_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, embed_override=lambda texts: [[0.0] * 1024 for _ in texts])
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)

    with pytest.raises(EngineUnavailableError, match="canary embedding vector is all zeros"):
        await adapter._ensure_semantic_ready()


@pytest.mark.asyncio
async def test_engine_mock_flag_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, mock=True)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)

    with pytest.raises(EngineUnavailableError, match="mock vector engine refused in semantic mode"):
        await adapter._ensure_semantic_ready()


@pytest.mark.asyncio
async def test_table_dimension_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, table_dims={"CodeSymbol_description": 512})
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)

    with pytest.raises(
        EngineUnavailableError,
        match="column dimension mismatch in table CodeSymbol_description: 512 != expected 1024",
    ):
        await adapter._ensure_semantic_ready()


@pytest.mark.asyncio
async def test_vector_engine_size_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=768)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine, vector_dimension=1024)

    with pytest.raises(EngineUnavailableError, match="vector dimension mismatch: engine reports 768 != expected 1024"):
        await adapter._ensure_semantic_ready()


@pytest.mark.asyncio
async def test_binding_mismatch_is_never_degraded_by_optional_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dimension mismatch is an identity failure, not an outage: even with
    embedding_required=False the ingest raises and no row is written."""
    fake_engine = FakeVectorEngine(vector_dimension=768)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine, embedding_required=False)
    _wire_graph_seam(monkeypatch, FakeGraphEngine())
    scope = _scope(uuid4(), uuid4())

    with pytest.raises(EngineUnavailableError, match="vector dimension mismatch: engine reports 768 != expected 1024"):
        await adapter.ingest_records(
            workspace_id=scope.workspace_id,
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            records=[_fact_record(scope, "f1", "helper_func")],
            source_ref=_source_ref(scope),
        )
    assert fake_engine.indexed_points == []


# ---------------------------------------------------------------------------
# 6. Query hydration, ordering, withdrawn filtering, exact append
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_hydration_from_graph_rows_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    node_id = scope.fact_node_id("fact-valid-001")
    ghost_id = uuid4()

    # The graph holds only node_id; the vector store also holds a ghost row.
    _wire_graph_seam(
        monkeypatch,
        FakeGraphEngine(
            rows=[
                _graph_row(
                    node_id,
                    "valid_symbol",
                    {
                        "enola_id": "fact-valid-001",
                        "kind": "symbol",
                        "file_path": "src/valid.py",
                        "line": 10,
                        "end_line": 20,
                        "fact_properties": {"signature": "def valid(): pass"},
                    },
                )
            ]
        ),
    )
    await fake_engine.fake_index_data_points([_point(node_id, scope), _point(ghost_id, scope)])

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="valid",
        limit=10,
    )

    assert res.retrieval is not None
    assert res.retrieval.mode == "semantic"
    assert res.retrieval.model_id == EMBEDDING_MODEL
    assert res.retrieval.dimension == 1024
    # The ghost hit is dropped: hydration comes only from graph rows.
    assert [fact.fact_id for fact in res.facts] == ["fact-valid-001"]
    assert res.facts[0].file_path == "src/valid.py"
    assert len(res.retrieval.scores) == 1 and res.retrieval.scores[0] is not None
    assert "CodeSymbol_description" in res.retrieval.collections_searched
    # Every search is scoped to this snapshot AND this model binding.
    assert fake_engine.search_calls
    for call in fake_engine.search_calls:
        assert call["node_name"] == _tags(scope)
        assert call["node_name_filter_operator"] == "AND"
        assert call["query_vector_len"] == 1024


@pytest.mark.asyncio
async def test_query_withdrawn_facts_hidden_from_semantic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    node_id = scope.fact_node_id("fact-withdrawn-001")

    _wire_graph_seam(
        monkeypatch,
        FakeGraphEngine(
            rows=[
                _graph_row(
                    node_id,
                    "withdrawn_symbol",
                    {
                        "enola_id": "fact-withdrawn-001",
                        "kind": "symbol",
                        "status": "withdrawn",
                        "fact_properties": {"withdrawn": True},
                    },
                )
            ]
        ),
    )
    # A stale vector row for the withdrawn fact must never resurrect it.
    await fake_engine.fake_index_data_points([_point(node_id, scope)])

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="symbol",
        limit=10,
    )

    assert res.facts == ()
    assert res.retrieval is not None
    assert res.retrieval.mode == "semantic_empty"


@pytest.mark.asyncio
async def test_query_exact_matches_appended(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    n1 = scope.fact_node_id("f1")
    n2 = scope.fact_node_id("f2")

    _wire_graph_seam(
        monkeypatch,
        FakeGraphEngine(
            rows=[
                _graph_row(n1, "apple_pie", {"enola_id": "f1", "kind": "symbol", "fact_properties": {}}),
                _graph_row(n2, "apple_juice", {"enola_id": "f2", "kind": "symbol", "fact_properties": {}}),
            ]
        ),
    )
    # Only n1 has a vector row; n2 is reachable by exact substring only.
    await fake_engine.fake_index_data_points([_point(n1, scope)])

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="apple",
        limit=10,
    )

    assert [fact.fact_id for fact in res.facts] == ["f1", "f2"]
    assert res.retrieval is not None
    assert res.retrieval.mode == "semantic"
    assert res.retrieval.scores[0] is not None
    assert res.retrieval.scores[1] is None


@pytest.mark.asyncio
async def test_query_semantic_empty_reports_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    _wire_graph_seam(monkeypatch, FakeGraphEngine(rows=[]))

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="nonexistent",
        limit=10,
    )

    assert res.facts == ()
    assert res.retrieval is not None
    assert res.retrieval.mode == "semantic_empty"
    assert res.retrieval.scores == ()
    assert set(res.retrieval.collections_searched) == set(SEMANTIC_COLLECTIONS)


@pytest.mark.asyncio
async def test_query_star_returns_exact_without_embedding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, embed_error=OSError("must not be called"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine, embedding_required=True)
    scope = _scope(uuid4(), uuid4())
    n1 = scope.fact_node_id("f1")
    _wire_graph_seam(
        monkeypatch,
        FakeGraphEngine(rows=[_graph_row(n1, "anything", {"enola_id": "f1", "kind": "symbol", "fact_properties": {}})]),
    )

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="*",
        limit=10,
    )

    assert [fact.fact_id for fact in res.facts] == ["f1"]
    assert res.retrieval is not None
    assert res.retrieval.mode == "exact"
    assert fake_engine.search_calls == []


# ---------------------------------------------------------------------------
# 7. Outage policy: optional graceful fallback vs required fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_outage_optional_falls_back_to_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, embed_error=TimeoutError("inference timeout"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine, embedding_required=False)
    scope = _scope(uuid4(), uuid4())
    n1 = scope.fact_node_id("f1")
    _wire_graph_seam(
        monkeypatch,
        FakeGraphEngine(rows=[_graph_row(n1, "helper_func", {"enola_id": "f1", "kind": "symbol", "fact_properties": {}})]),
    )

    res = await adapter.query(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query_text="helper",
        limit=10,
    )

    assert res.retrieval is not None
    assert res.retrieval.mode == "exact_fallback"
    assert res.retrieval.reason is not None
    assert "backend unreachable during query" in res.retrieval.reason
    assert [fact.fact_id for fact in res.facts] == ["f1"]


@pytest.mark.asyncio
async def test_query_outage_required_raises_engine_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024, embed_error=TimeoutError("inference timeout"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine, embedding_required=True)
    scope = _scope(uuid4(), uuid4())
    _wire_graph_seam(monkeypatch, FakeGraphEngine(rows=[]))

    with pytest.raises(EngineUnavailableError, match="backend unreachable during query"):
        await adapter.query(
            workspace_id=scope.workspace_id,
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            query_text="helper",
            limit=10,
        )


@pytest.mark.asyncio
async def test_ingest_outage_optional_skips_required_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good_endpoint = "http://127.0.0.1:18081"
    bad_endpoint = "http://127.0.0.1:19999"
    good_engine = FakeVectorEngine(vector_dimension=1024)
    # A refused TCP connection is what an unreachable inference endpoint raises;
    # only such transport outages are degraded (see is_embedding_failure).
    bad_engine = FakeVectorEngine(vector_dimension=1024, embed_error=ConnectionRefusedError("connection refused"))
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    install_fake_vector_engine(
        monkeypatch,
        {
            good_endpoint: good_engine,
            bad_endpoint: bad_engine,
        },
    )
    graph = FakeGraphEngine()
    written = _wire_graph_seam(monkeypatch, graph)
    scope = _scope(uuid4(), uuid4())
    record = _fact_record(scope, "f1", "helper_func")

    # 1. Healthy endpoint: indexes successfully with good engine.
    adapter_good = RealCogneeAdapter(
        _make_semantic_config(tmp_path / "good", embedding_endpoint=good_endpoint, embedding_required=False),
        available=True,
    )
    res_good = await adapter_good.ingest_records(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        records=[record],
        source_ref=_source_ref(scope),
    )
    assert res_good.nodes_written == 1
    assert len(written) == 1
    assert res_good.semantic is not None
    assert res_good.semantic.status == "indexed"
    assert len(good_engine.indexed_points) == 1
    assert adapter_good._cached_vector_engine is good_engine

    # 2. Changed bad endpoint with optional policy:
    # Does NOT reuse good_engine; re-resolves bad_engine and reports skipped outage.
    adapter_opt = RealCogneeAdapter(
        _make_semantic_config(tmp_path / "opt", embedding_endpoint=bad_endpoint, embedding_required=False),
        available=True,
    )
    res_opt = await adapter_opt.ingest_records(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        records=[record],
        source_ref=_source_ref(scope),
    )
    assert adapter_opt._cached_vector_engine is not good_engine
    # Failed readiness is never cached; retaining bad_engine would make a later
    # route silently reuse an unavailable endpoint.
    assert adapter_opt._cached_vector_engine is None
    assert res_opt.nodes_written == 1
    assert res_opt.semantic is not None
    assert res_opt.semantic.status == "skipped"
    assert res_opt.semantic.rows_requested == 1
    assert res_opt.semantic.reason is not None
    assert "backend unreachable during ingest_records" in res_opt.semantic.reason
    assert bad_engine.indexed_points == []
    assert len(good_engine.indexed_points) == 1

    # 3. Bad endpoint with required policy: the same outage fails the ingest closed.
    adapter_req = RealCogneeAdapter(
        _make_semantic_config(tmp_path / "req", embedding_endpoint=bad_endpoint, embedding_required=True),
        available=True,
    )
    with pytest.raises(EngineUnavailableError, match="backend unreachable during ingest_records"):
        await adapter_req.ingest_records(
            workspace_id=scope.workspace_id,
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            records=[record],
            source_ref=_source_ref(scope),
        )
    assert bad_engine.indexed_points == []
    assert len(good_engine.indexed_points) == 1


@pytest.mark.asyncio
async def test_semantic_route_binding_cache_invalidation_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    install_fake_vector_engine(monkeypatch, FakeVectorEngine(vector_dimension=1024))

    cleared_count = 0
    original_clear = cognee_adapter.clear_semantic_engine_caches

    def _spy_clear(context: str | None = None) -> None:
        nonlocal cleared_count
        cleared_count += 1
        if context is not None:
            original_clear(context)
        else:
            original_clear()

    monkeypatch.setattr(cognee_adapter, "clear_semantic_engine_caches", _spy_clear)
    monkeypatch.setattr(cognee_adapter, "_LAST_SEMANTIC_BINDING", None)

    # 1. Initial semantic adapter config triggers invalidation
    cfg1 = _make_semantic_config(tmp_path / "c1", embedding_endpoint="http://127.0.0.1:18081")
    RealCogneeAdapter(cfg1, available=True)
    assert cleared_count == 1
    binding1 = cognee_adapter._LAST_SEMANTIC_BINDING
    assert binding1 is not None

    # 2. Identical configuration does NOT clear again
    cfg1_same = _make_semantic_config(tmp_path / "c1_same", embedding_endpoint="http://127.0.0.1:18081")
    RealCogneeAdapter(cfg1_same, available=True)
    assert cleared_count == 1
    assert cognee_adapter._LAST_SEMANTIC_BINDING == binding1

    # 3. Changed endpoint triggers invalidation
    cfg2 = _make_semantic_config(tmp_path / "c2", embedding_endpoint="http://127.0.0.1:19999")
    RealCogneeAdapter(cfg2, available=True)
    assert cleared_count == 2
    assert cognee_adapter._LAST_SEMANTIC_BINDING != binding1

    # 4. Changed model triggers invalidation
    cfg3 = _make_semantic_config(
        tmp_path / "c3", embedding_endpoint="http://127.0.0.1:19999", embedding_model="other-model"
    )
    RealCogneeAdapter(cfg3, available=True)
    assert cleared_count == 3

    # 5. Changed dimension triggers invalidation
    cfg4 = _make_semantic_config(
        tmp_path / "c4",
        embedding_endpoint="http://127.0.0.1:19999",
        embedding_model="other-model",
        vector_dimension=512,
    )
    RealCogneeAdapter(cfg4, available=True)
    assert cleared_count == 4

    # 6. Graph-only route does NOT trigger semantic cache invalidation and leaves binding untouched
    current_binding = cognee_adapter._LAST_SEMANTIC_BINDING
    cfg_graph = KnowledgeConfig(
        state_dir=tmp_path / "graph_state",
        config_dir=tmp_path / "graph_config",
        graph_only=True,
            vector_store="lancedb",
        neo4j_uri="bolt://127.0.0.1:17687",
        neo4j_password_file=secret_file(tmp_path, "neo.pw", "neo-pw"),
    )
    RealCogneeAdapter(cfg_graph, available=True)
    assert cleared_count == 4
    assert cognee_adapter._LAST_SEMANTIC_BINDING == current_binding

    # 7. Embedded route does NOT trigger semantic cache invalidation
    cfg_embedded = KnowledgeConfig(
        state_dir=tmp_path / "emb_state",
        config_dir=tmp_path / "emb_config",
        graph_only=True,
            vector_store="lancedb",
    )
    RealCogneeAdapter(cfg_embedded, available=True)
    assert cleared_count == 4
    assert cognee_adapter._LAST_SEMANTIC_BINDING == current_binding


# ---------------------------------------------------------------------------
# 8. Correct and retire operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_recomputes_and_skips_reindex_when_withdrawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    node_id = scope.fact_node_id("fact-123")

    stored = {
        "id": str(node_id),
        "name": "my_symbol",
        "kind": "symbol",
        "file_path": "src/lib.py",
        "line": 3,
        "end_line": 9,
        "repo": scope.label,
        "enola_id": "fact-123",
        "fact_properties": {"version": 1},
    }
    graph = FakeGraphEngine(nodes={str(node_id): {"id": str(node_id), "name": "my_symbol", "properties": json.dumps(stored)}})
    _wire_graph_seam(monkeypatch, graph)

    # 1. Active fact: the stale row is deleted and the rebuilt point re-indexed
    #    with a recomputed description and the scope / binding tags.
    res_active = await adapter.correct(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        fact_id="fact-123",
        properties_update={"version": 2},
    )
    assert res_active.success is True
    assert res_active.semantic_status == "reindexed"
    assert fake_engine.deleted_calls == [("CodeSymbol_description", [node_id])]
    [reindexed] = fake_engine.indexed_points
    assert reindexed.id == node_id
    assert reindexed.fields["fact_properties"] == {"version": 2}
    assert reindexed.fields["description"] == semantic_text("symbol", "my_symbol", "src/lib.py", {"version": 2})
    assert reindexed.fields["metadata"] == {"index_fields": ["description"]}
    assert reindexed.fields["belongs_to_set"] == _tags(scope)

    # 2. Withdrawn fact: the row is deleted and not re-indexed.
    res_withdrawn = await adapter.correct(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        fact_id="fact-123",
        properties_update={"withdrawn": True, "withdrawn_by": "correction-1"},
    )
    assert res_withdrawn.success is True
    assert res_withdrawn.semantic_status == "deleted"
    assert len(fake_engine.deleted_calls) == 2
    assert len(fake_engine.indexed_points) == 1
    assert fake_engine.collections["CodeSymbol_description"] == {}


@pytest.mark.asyncio
async def test_retire_deletes_vector_rows_before_nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_engine = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, fake_engine)
    scope = _scope(uuid4(), uuid4())
    node_id = scope.fact_node_id("f1")

    await fake_engine.fake_index_data_points([_point(node_id, scope)])
    graph = FakeGraphEngine(rows=[(str(node_id),)])
    _wire_graph_seam(monkeypatch, graph)

    vector_rows_deleted_before_graph_delete: list[int] = []
    original_delete_nodes = graph.delete_nodes

    async def observing_delete_nodes(node_ids: list[str]) -> None:
        vector_rows_deleted_before_graph_delete.append(len(fake_engine.deleted_calls))
        await original_delete_nodes(node_ids)

    monkeypatch.setattr(graph, "delete_nodes", observing_delete_nodes)

    res = await adapter.retire(
        workspace_id=scope.workspace_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
    )

    assert res.success is True
    assert res.nodes_deleted == 1
    assert res.edges_deleted == 0
    assert res.vector_rows_deleted == 1
    assert res.vector_cleanup == "complete"
    assert res.vector_cleanup_reason is None
    assert fake_engine.deleted_calls == [("CodeSymbol_description", [node_id])]
    assert graph.deleted == [[str(node_id)]]
    assert vector_rows_deleted_before_graph_delete == [1]


# ---------------------------------------------------------------------------
# 9. Embedded route invariance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedded_route_vector_indexing_false(tmp_path: Path) -> None:
    # Default config: graph_only=True on the embedded ladybug + lancedb route.
    cfg = KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config")
    adapter = RealCogneeAdapter(cfg, available=False)
    status = await adapter.status()

    assert status.details["vector_indexing"] is False
    assert status.details["semantic"] is False
    assert status.details["binding_tag"] is None
    assert status.active_route.graph_only is True
    assert status.active_route.model_id is None
    assert status.active_route.provider == "ladybug-embedded"
    assert "embedding" not in status.details
