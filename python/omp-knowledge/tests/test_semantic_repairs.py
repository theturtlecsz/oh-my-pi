"""Unit tests for the semantic-route repairs (no live backend, no real Cognee state).

Every test drives the real adapter through seam doubles and asserts on observed
behaviour: exceptions raised, results returned, and the exact calls recorded by
the doubles. Nothing here inspects source text or claims semantic capability.

Covered:

* ``embedding_endpoint`` is refused at the config boundary (``KnowledgeConfig``)
  and at the backend boundary (``resolve_backend_route``) when it carries
  userinfo or a non-http(s) scheme; error text carries the sanitized endpoint only;
* the optional embedding policy degrades only an outage (``EngineUnavailableError``
  produced by ``_semantic_call``): programming / schema / data errors from a
  reachable backend propagate unchanged from ingest, query, correct and retire, a
  binding mismatch always propagates, and ``is_embedding_failure`` never treats a
  data error as an outage;
* binding invalidation goes through the pinned Cognee's public
  ``vector_engine_cache.evict(**config)`` path with the vector config read back
  at that moment (before and after the setters), the semantic route fails closed
  with the pinned contract when that path is absent, and the embedded and
  graph-only routes never need it;
* ``retire`` reports node, edge and vector-row counts as readback differences,
  raises when anything survives the delete, reports ``degraded`` /
  ``not_checked`` / ``partial`` explicitly and never a fabricated zero;
* ``correct`` reverts to the prior node and edges when the rebuild fails, or
  raises an explicit, retryable ``CorrectionFailedError`` describing the partial
  state, and never reports success without a complete readback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    EMBEDDING_ENDPOINT_SCHEMES,
    BackendConfigError,
    SnapshotScope,
    binding_tag,
    is_embedding_failure,
    resolve_backend_route,
    validate_embedding_endpoint,
)
from omp_knowledge.engine.cognee_adapter import SEMANTIC_CACHE_CONTRACT, RealCogneeAdapter
from omp_knowledge.engine.protocol import (
    CorrectionFailedError,
    EngineError,
    EngineUnavailableError,
    SemanticBindingError,
)
from omp_work.knowledge_contracts import FactRecord, SourceRef
from support.fake_cognee import (
    FakeCogneeConfig,
    FakeVectorEngine,
    FakeVectorEngineCache,
    install_fake_cognee,
    install_fake_vector_engine,
    secret_file,
)

EMBEDDING_MODEL = "qwen3-embedding-0.6b-q8"
GOOD_ENDPOINT = "http://127.0.0.1:18081"
SNAPSHOT_ID = "b" * 64
PG_SECRET = "pg-secret-value-91ab"


# ---------------------------------------------------------------------------
# Config and seam doubles
# ---------------------------------------------------------------------------


def _semantic_config(tmp_path: Path, **overrides: Any) -> KnowledgeConfig:
    values: dict[str, Any] = dict(
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        graph_only=False,
        vector_store="pgvector",
        embedding_provider="openai_compatible",
        embedding_model=EMBEDDING_MODEL,
        embedding_endpoint=GOOD_ENDPOINT,
        embedding_required=False,
        vector_dimension=1024,
        cognee_pg_password_file=secret_file(tmp_path, "pg.secret", PG_SECRET),
    )
    values.update(overrides)
    return KnowledgeConfig(**values)


def _embedded_config(tmp_path: Path) -> KnowledgeConfig:
    return KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config")


class FakePoint:
    """Records the keyword fields the adapter hands to the Cognee model class."""

    def __init__(self, **fields: Any) -> None:
        self.fields = dict(fields)
        self.id = fields["id"]
        self.name = fields.get("name")
        self.kind = fields.get("kind")
        self.belongs_to_set = fields.get("belongs_to_set")


class GraphDouble:
    """In-memory graph answering every dialect-owned query the adapter issues.

    Nodes are stored the way the embedded engine returns them (``properties`` as
    one JSON string). Edges are ``(source, target, name, props)``. ``delete_nodes``
    behaves like ``DETACH DELETE``: the node and every incident edge disappear, so
    the scope / retire / incident-edge readbacks the adapter performs afterwards
    no longer see them. Failure injection: ``fail_delete_with`` raises from
    ``delete_nodes``; ``add_edges_failures`` raises from that many ``add_edges``
    calls; ``drop_edges_on_add`` silently ignores ``add_edges``;
    ``refuse_delete`` makes ``delete_nodes`` a no-op.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str, dict[str, Any]]] = []
        self.deleted: list[list[str]] = []
        self.fail_delete_with: BaseException | None = None
        self.add_edges_failures = 0
        self.drop_edges_on_add = False
        self.refuse_delete = False

    def seed_node(self, node_id: UUID | str, properties: dict[str, Any]) -> None:
        self.nodes[str(node_id)] = {
            "id": str(node_id),
            "name": properties.get("name"),
            "type": "CodeSymbol",
            "properties": json.dumps(properties, default=str),
        }

    def stored_properties(self, node_id: UUID | str) -> dict[str, Any]:
        return json.loads(self.nodes[str(node_id)]["properties"])

    def edge_keys(self) -> set[tuple[str, str, str]]:
        return {(source, target, name) for source, target, name, _ in self.edges}

    async def query(self, query: str, params: dict[str, Any] | None = None) -> list[Any]:
        params = params or {}
        if "scope_json" in params:
            scope_label = json.loads(params["scope_json"])
            repo_node_id = params.get("repo_node_id")
            out = []
            for node_id, node in self.nodes.items():
                props = json.loads(node["properties"])
                if props.get("repo") == scope_label or node_id == repo_node_id:
                    out.append((node_id, node["name"], node["properties"]))
            return out
        if "node_ids" in params:
            wanted = set(params["node_ids"])
            return [(s, t, n) for s, t, n, _ in self.edges if s in wanted or t in wanted]
        if "node_id" in params:
            node_id = str(params["node_id"])
            return [(s, t, n, json.dumps(p)) for s, t, n, p in self.edges if node_id in (s, t)]
        return []

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(str(node_id))

    async def delete_nodes(self, node_ids: list[str]) -> None:
        if self.fail_delete_with is not None:
            raise self.fail_delete_with
        ids = [str(node_id) for node_id in node_ids]
        self.deleted.append(ids)
        if self.refuse_delete:
            return
        for node_id in ids:
            self.nodes.pop(node_id, None)
        self.edges = [edge for edge in self.edges if edge[0] not in ids and edge[1] not in ids]

    async def add_edges(self, edges: list[tuple[Any, Any, str, dict[str, Any]]]) -> None:
        if self.add_edges_failures > 0:
            self.add_edges_failures -= 1
            raise RuntimeError("simulated add_edges failure")
        if self.drop_edges_on_add:
            return
        for source, target, name, props in edges:
            self.edges.append((str(source), str(target), str(name), dict(props)))


class AddDataPointsDouble:
    """Replacement for ``cognee.tasks.storage.add_data_points`` writing into a GraphDouble.

    ``failures`` raises from that many calls; ``write_nowhere`` accepts the call
    without storing anything (a rebuilt node that never reads back).
    """

    def __init__(self, graph: GraphDouble) -> None:
        self.graph = graph
        self.written: list[FakePoint] = []
        self.failures = 0
        self.write_nowhere = False

    async def __call__(self, data_points: list[FakePoint], graph_only: bool = False) -> None:
        assert graph_only is True, "graph writes must stay graph_only"
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("simulated add_data_points failure")
        for point in data_points:
            self.written.append(point)
            if not self.write_nowhere:
                self.graph.seed_node(point.id, point.fields)


def _wire_graph(monkeypatch: pytest.MonkeyPatch, graph: GraphDouble) -> AddDataPointsDouble:
    writer = AddDataPointsDouble(graph)

    async def fake_get_graph_engine() -> GraphDouble:
        return graph

    monkeypatch.setattr(cognee_adapter, "get_graph_engine", fake_get_graph_engine, raising=False)
    monkeypatch.setattr(cognee_adapter, "add_data_points", writer, raising=False)
    monkeypatch.setattr(cognee_adapter, "_get_model_class", lambda kind: FakePoint)
    return writer


def _semantic_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vector_engine: FakeVectorEngine, **config_overrides: Any
) -> RealCogneeAdapter:
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    install_fake_vector_engine(monkeypatch, vector_engine)
    return RealCogneeAdapter(_semantic_config(tmp_path, **config_overrides), available=True)


def _scope() -> SnapshotScope:
    return SnapshotScope(workspace_id=uuid4(), repository_id=uuid4(), snapshot_id=SNAPSHOT_ID)


def _kwargs(scope: SnapshotScope) -> dict[str, Any]:
    return {"workspace_id": scope.workspace_id, "repository_id": scope.repository_id, "snapshot_id": scope.snapshot_id}


def _seed_fact(graph: GraphDouble, scope: SnapshotScope, fact_id: str, name: str, props: dict[str, Any]) -> UUID:
    node_id = scope.fact_node_id(fact_id)
    graph.seed_node(
        node_id,
        {
            "id": str(node_id),
            "name": name,
            "kind": "symbol",
            "file_path": "src/lib.py",
            "line": 3,
            "end_line": 9,
            "repo": scope.label,
            "enola_id": fact_id,
            "fact_properties": dict(props),
        },
    )
    return node_id


def _seed_edges(graph: GraphDouble, scope: SnapshotScope, node_id: UUID) -> set[tuple[str, str, str]]:
    anchor = str(scope.repo_node_id())
    module_node = str(scope.fact_node_id("module-fact"))
    caller_node = str(scope.fact_node_id("caller-fact"))
    graph.edges.extend(
        [
            (str(node_id), anchor, "part_of", {"relationship_name": "part_of"}),
            (str(node_id), module_node, "declares", {"relationship_name": "declares"}),
            (caller_node, str(node_id), "calls", {"relationship_name": "calls"}),
        ]
    )
    return {(s, t, n) for s, t, n, _ in graph.edges}


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
        producer="test-semantic-repairs",
        observed_at=datetime.now(timezone.utc),
    )


def _vector_point(node_id: UUID, scope: SnapshotScope) -> SimpleNamespace:
    return SimpleNamespace(id=node_id, kind="symbol", belongs_to_set=[scope.label, binding_tag(EMBEDDING_MODEL, 1024)])


# ---------------------------------------------------------------------------
# 1. Embedding endpoint rejection at both boundaries
# ---------------------------------------------------------------------------


def test_validate_embedding_endpoint_accepts_http_and_https_and_returns_sanitized() -> None:
    assert EMBEDDING_ENDPOINT_SCHEMES == frozenset({"http", "https"})
    assert validate_embedding_endpoint("http://127.0.0.1:18081") == "http://127.0.0.1:18081"
    assert validate_embedding_endpoint("https://embed.internal/v1") == "https://embed.internal/v1"
    assert validate_embedding_endpoint("HTTPS://embed.internal") == "HTTPS://embed.internal"


@pytest.mark.parametrize(
    "endpoint, sanitized",
    [
        ("http://svc-user-3f1a:svc-pass-8c2e@embed.internal:18081/v1", "http://embed.internal:18081/v1"),
        ("https://only-user-77aa@embed.internal", "https://embed.internal"),
    ],
)
def test_validate_embedding_endpoint_rejects_userinfo_without_leaking_it(endpoint: str, sanitized: str) -> None:
    with pytest.raises(BackendConfigError) as excinfo:
        validate_embedding_endpoint(endpoint)
    message = str(excinfo.value)
    assert "embedding_endpoint" in message
    assert sanitized in message
    assert "svc-user-3f1a" not in message
    assert "svc-pass-8c2e" not in message
    assert "only-user-77aa" not in message
    assert "@" not in message.split("got endpoint", 1)[1]


@pytest.mark.parametrize(
    "endpoint, expected_scheme",
    [
        ("ftp://embed.internal:21", "ftp"),
        ("bolt://embed.internal:7687", "bolt"),
        ("file:///tmp/embeddings", "file"),
        ("embed.internal:18081", "<none>"),
    ],
)
def test_validate_embedding_endpoint_rejects_unsupported_scheme(endpoint: str, expected_scheme: str) -> None:
    with pytest.raises(BackendConfigError) as excinfo:
        validate_embedding_endpoint(endpoint)
    message = str(excinfo.value)
    assert "must use one of ['http', 'https']" in message
    assert f"got scheme {expected_scheme!r}" in message


def test_validate_embedding_endpoint_rejects_missing_host_and_empty() -> None:
    with pytest.raises(BackendConfigError, match="must name a host"):
        validate_embedding_endpoint("http:///v1")
    with pytest.raises(BackendConfigError, match="non-empty"):
        validate_embedding_endpoint("   ")


def test_config_boundary_rejects_userinfo_endpoint_without_leaking_password(tmp_path: Path) -> None:
    password = "cfg-pass-5d9e"
    with pytest.raises(ValidationError) as excinfo:
        _semantic_config(tmp_path, embedding_endpoint=f"http://cfg-user:{password}@embed.internal:18081")
    # The validator's own message names the setting and the sanitized endpoint only
    # (pydantic's rendering of the raw input is not part of this contract).
    messages = [error["msg"] for error in excinfo.value.errors()]
    assert any("embedding_endpoint" in msg and "http://embed.internal:18081" in msg for msg in messages)
    assert all(password not in msg and "cfg-user" not in msg for msg in messages)


def test_config_boundary_rejects_unsupported_scheme(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must use one of"):
        _semantic_config(tmp_path, embedding_endpoint="ftp://embed.internal:21")


def test_backend_boundary_rejects_even_when_config_validation_is_bypassed(tmp_path: Path) -> None:
    """``resolve_backend_route`` applies the same rule independently of the config
    validator, so a config assembled without validation cannot reach Cognee."""
    valid = _semantic_config(tmp_path)
    data = valid.model_dump()
    data["embedding_endpoint"] = "http://bypass-user:bypass-pass-1c3a@embed.internal:18081"
    bypassed = KnowledgeConfig.model_construct(**data)
    assert bypassed.embedding_endpoint.startswith("http://bypass-user:")

    with pytest.raises(BackendConfigError) as excinfo:
        resolve_backend_route(bypassed)
    assert "bypass-pass-1c3a" not in str(excinfo.value)
    assert "http://embed.internal:18081" in str(excinfo.value)

    data["embedding_endpoint"] = "ws://embed.internal:18081"
    with pytest.raises(BackendConfigError, match="got scheme 'ws'"):
        resolve_backend_route(KnowledgeConfig.model_construct(**data))


def test_adapter_construction_refuses_userinfo_endpoint_before_touching_cognee(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCogneeConfig()
    install_fake_cognee(monkeypatch, fake)
    valid = _semantic_config(tmp_path)
    data = valid.model_dump()
    data["embedding_endpoint"] = "http://adapter-user:adapter-pass-7b7b@embed.internal:18081"

    with pytest.raises(BackendConfigError) as excinfo:
        RealCogneeAdapter(KnowledgeConfig.model_construct(**data), available=True)
    assert "adapter-pass-7b7b" not in str(excinfo.value)
    # No Cognee setter ran: the endpoint never reached Cognee's embedding config.
    assert fake.calls == []


def test_route_reports_sanitized_endpoint_for_valid_semantic_config(tmp_path: Path) -> None:
    route = resolve_backend_route(_semantic_config(tmp_path, embedding_endpoint="https://embed.internal:8443/v1"))
    assert route.semantic is True
    assert route.embedding_endpoint == "https://embed.internal:8443/v1"
    assert route.as_dict()["embedding_endpoint"] == "https://embed.internal:8443/v1"


def test_graph_only_route_ignores_inert_embedding_endpoint(tmp_path: Path) -> None:
    cfg = KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config", embedding_endpoint="ftp://unused")
    route = resolve_backend_route(cfg)
    assert route.semantic is False
    assert route.embedding_endpoint is None


# ---------------------------------------------------------------------------
# 2. Only outages are degraded by the optional policy
# ---------------------------------------------------------------------------


class _CogneeShaped(Exception):
    """Base for exceptions shaped like the pinned Cognee's, without importing it."""


class EmbeddingException(_CogneeShaped):
    pass


class EmbeddingCredentialsError(EmbeddingException):
    pass


class EmbeddingContextWindowTooSmallError(EmbeddingException):
    pass


for _cls in (_CogneeShaped, EmbeddingException, EmbeddingCredentialsError, EmbeddingContextWindowTooSmallError):
    _cls.__module__ = "cognee.infrastructure.databases.exceptions.exceptions"


class APIConnectionError(Exception):
    pass


APIConnectionError.__module__ = "openai"


class _Row(BaseModel):
    dim: int


def _pydantic_error() -> ValidationError:
    try:
        _Row(dim="not-an-int")  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


def test_is_embedding_failure_classifies_outages_only() -> None:
    assert is_embedding_failure(ConnectionRefusedError("refused"))
    assert is_embedding_failure(TimeoutError("timed out"))
    assert is_embedding_failure(EmbeddingException("Cannot connect to embedding endpoint"))
    assert is_embedding_failure(EmbeddingCredentialsError("401"))
    assert is_embedding_failure(APIConnectionError("connection error"))

    # Data and programming errors from a reachable backend are never outages.
    assert not is_embedding_failure(EmbeddingContextWindowTooSmallError("too long"))
    assert not is_embedding_failure(ValueError("bad vector payload"))
    assert not is_embedding_failure(TypeError("unexpected keyword"))
    assert not is_embedding_failure(KeyError("index_fields"))
    assert not is_embedding_failure(_pydantic_error())
    assert not is_embedding_failure(OSError("disk quota exceeded"))

    class SqlProgrammingError(Exception):
        pass

    SqlProgrammingError.__module__ = "sqlalchemy.exc"
    assert not is_embedding_failure(SqlProgrammingError("column does not exist"))


@pytest.mark.asyncio
async def test_optional_ingest_propagates_non_outage_and_degrades_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    record = _fact_record(scope, "f1", "helper_func")

    # A schema / programming error from a reachable backend propagates unchanged,
    # even though embedding_required=False.
    broken = FakeVectorEngine(vector_dimension=1024, embed_error=ValueError("payload::jsonb cast failed"))
    adapter = _semantic_adapter(monkeypatch, tmp_path / "broken", broken, embedding_required=False)
    _wire_graph(monkeypatch, GraphDouble())
    with pytest.raises(ValueError, match="payload::jsonb cast failed"):
        await adapter.ingest_records(**_kwargs(scope), records=[record], source_ref=_source_ref(scope))

    over_length = FakeVectorEngine(vector_dimension=1024, embed_error=EmbeddingContextWindowTooSmallError("too long"))
    adapter_data = _semantic_adapter(monkeypatch, tmp_path / "data", over_length, embedding_required=False)
    _wire_graph(monkeypatch, GraphDouble())
    with pytest.raises(EmbeddingContextWindowTooSmallError):
        await adapter_data.ingest_records(**_kwargs(scope), records=[record], source_ref=_source_ref(scope))

    # The same call under an outage is degraded with a redacted reason.
    down = FakeVectorEngine(vector_dimension=1024, embed_error=EmbeddingException(f"Cannot connect (pw {PG_SECRET})"))
    adapter_down = _semantic_adapter(monkeypatch, tmp_path / "down", down, embedding_required=False)
    _wire_graph(monkeypatch, GraphDouble())
    res = await adapter_down.ingest_records(**_kwargs(scope), records=[record], source_ref=_source_ref(scope))
    assert res.semantic is not None
    assert res.semantic.status == "skipped"
    assert res.semantic.reason is not None
    assert "backend unreachable during ingest_records" in res.semantic.reason
    assert PG_SECRET not in res.semantic.reason
    assert "***" in res.semantic.reason


@pytest.mark.asyncio
async def test_optional_query_propagates_non_outage_and_degrades_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()

    def graph_with_fact() -> GraphDouble:
        graph = GraphDouble()
        _seed_fact(graph, scope, "f1", "helper_func", {})
        return graph

    broken = FakeVectorEngine(vector_dimension=1024, embed_error=TypeError("embed_text() got an unexpected keyword"))
    adapter = _semantic_adapter(monkeypatch, tmp_path / "broken", broken, embedding_required=False)
    _wire_graph(monkeypatch, graph_with_fact())
    with pytest.raises(TypeError, match="unexpected keyword"):
        await adapter.query(**_kwargs(scope), query_text="helper", limit=10)

    down = FakeVectorEngine(vector_dimension=1024, embed_error=ConnectionRefusedError("refused"))
    adapter_down = _semantic_adapter(monkeypatch, tmp_path / "down", down, embedding_required=False)
    _wire_graph(monkeypatch, graph_with_fact())
    res = await adapter_down.query(**_kwargs(scope), query_text="helper", limit=10)
    assert res.retrieval is not None
    assert res.retrieval.mode == "exact_fallback"
    assert [fact.fact_id for fact in res.facts] == ["f1"]


@pytest.mark.asyncio
async def test_optional_correct_propagates_non_outage_and_reports_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()

    broken = FakeVectorEngine(vector_dimension=1024, embed_error=_pydantic_error())
    adapter = _semantic_adapter(monkeypatch, tmp_path / "broken", broken, embedding_required=False)
    graph = GraphDouble()
    _seed_fact(graph, scope, "f1", "helper_func", {"version": 1})
    _wire_graph(monkeypatch, graph)
    with pytest.raises(ValidationError):
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update={"version": 2})

    down = FakeVectorEngine(vector_dimension=1024, embed_error=EmbeddingException(f"timeout ({PG_SECRET})"))
    adapter_down = _semantic_adapter(monkeypatch, tmp_path / "down", down, embedding_required=False)
    graph2 = GraphDouble()
    _seed_fact(graph2, scope, "f1", "helper_func", {"version": 1})
    _wire_graph(monkeypatch, graph2)
    res = await adapter_down.correct(**_kwargs(scope), fact_id="f1", properties_update={"version": 2})
    assert res.success is True
    assert res.semantic_status == "failed"
    assert res.semantic_reason is not None
    assert "backend unreachable during correct" in res.semantic_reason
    assert PG_SECRET not in res.semantic_reason


@pytest.mark.asyncio
async def test_optional_retire_propagates_non_outage_and_leaves_graph_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    broken = FakeVectorEngine(vector_dimension=1024, embed_error=KeyError("index_fields"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, broken, embedding_required=False)
    graph = GraphDouble()
    _seed_fact(graph, scope, "f1", "helper_func", {})
    _wire_graph(monkeypatch, graph)

    with pytest.raises(KeyError):
        await adapter.retire(**_kwargs(scope))
    assert graph.deleted == [], "a non-outage error must not proceed to graph deletion"
    assert len(graph.nodes) == 1


@pytest.mark.asyncio
async def test_binding_mismatch_propagates_from_every_operation_under_optional_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    mismatched = FakeVectorEngine(vector_dimension=768)
    adapter = _semantic_adapter(monkeypatch, tmp_path, mismatched, embedding_required=False, vector_dimension=1024)
    graph = GraphDouble()
    _seed_fact(graph, scope, "f1", "helper_func", {})
    _wire_graph(monkeypatch, graph)

    with pytest.raises(SemanticBindingError):
        await adapter.query(**_kwargs(scope), query_text="helper")
    with pytest.raises(SemanticBindingError):
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update={"x": 1})
    with pytest.raises(SemanticBindingError):
        await adapter.retire(**_kwargs(scope))
    assert graph.deleted == []
    assert mismatched.indexed_points == []


@pytest.mark.asyncio
async def test_required_policy_raises_outage_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    down = FakeVectorEngine(vector_dimension=1024, embed_error=EmbeddingException("Cannot connect"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, down, embedding_required=True)
    graph = GraphDouble()
    _seed_fact(graph, scope, "f1", "helper_func", {})
    _wire_graph(monkeypatch, graph)

    with pytest.raises(EngineUnavailableError, match="backend unreachable during query"):
        await adapter.query(**_kwargs(scope), query_text="helper")
    with pytest.raises(EngineUnavailableError, match="backend unreachable during retire"):
        await adapter.retire(**_kwargs(scope))
    assert graph.deleted == []


# ---------------------------------------------------------------------------
# 3. Public cache binding
# ---------------------------------------------------------------------------


def _expected_vector_payload(cfg: KnowledgeConfig) -> dict[str, Any]:
    return {
        "vector_db_provider": "pgvector",
        "vector_db_host": cfg.cognee_pg_host,
        "vector_db_port": cfg.cognee_pg_port,
        "vector_db_name": cfg.cognee_pg_database,
        "vector_db_username": cfg.cognee_pg_user,
        "vector_db_password": PG_SECRET,
    }


def test_binding_change_evicts_through_public_cache_with_readback_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cognee_adapter, "_LAST_SEMANTIC_BINDING", None)
    fake_config = FakeCogneeConfig()
    cache = install_fake_cognee(monkeypatch, fake_config)
    install_fake_vector_engine(monkeypatch, FakeVectorEngine(vector_dimension=1024))
    cfg1 = _semantic_config(tmp_path / "c1")

    RealCogneeAdapter(cfg1, available=True)

    # First binding: one eviction before the setters (nothing applied yet, so the
    # readback config is empty) and one after with the freshly applied config.
    assert cache.evict_calls == [{}, _expected_vector_payload(cfg1)]

    # Same binding again: no eviction at all.
    RealCogneeAdapter(_semantic_config(tmp_path / "c1b"), available=True)
    assert len(cache.evict_calls) == 2

    # Changed embedding endpoint, same pgvector: the entry created under the old
    # binding is evicted with the config still applied at that moment (the same
    # key), then the entry for the new config; the cached engine is gone.
    cache.cached.append(_expected_vector_payload(cfg1))
    cfg2 = _semantic_config(tmp_path / "c2", embedding_endpoint="http://127.0.0.1:19999")
    RealCogneeAdapter(cfg2, available=True)
    assert cache.evict_calls[2] == _expected_vector_payload(cfg1)
    assert cache.evict_calls[3] == _expected_vector_payload(cfg2)
    assert cache.cached == []


def test_semantic_route_fails_closed_without_public_cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cognee_adapter, "_LAST_SEMANTIC_BINDING", None)
    fake_config = FakeCogneeConfig()
    install_fake_cognee(monkeypatch, fake_config)
    monkeypatch.setattr(cognee_adapter, "VECTOR_ENGINE_CACHE", None)

    with pytest.raises(EngineUnavailableError) as excinfo:
        RealCogneeAdapter(_semantic_config(tmp_path), available=True)
    message = str(excinfo.value)
    assert message.startswith("engine_unavailable:")
    assert SEMANTIC_CACHE_CONTRACT in message
    assert "cognee==1.5.4" in message
    # Failing closed happened before any setter could bind the new endpoint.
    assert fake_config.calls == []


def test_embedded_and_graph_only_routes_do_not_need_the_vector_cache_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_config = FakeCogneeConfig()
    install_fake_cognee(monkeypatch, fake_config)
    monkeypatch.setattr(cognee_adapter, "VECTOR_ENGINE_CACHE", None)
    monkeypatch.setattr(cognee_adapter, "VECTOR_CONFIG_READER", None)

    embedded = RealCogneeAdapter(_embedded_config(tmp_path / "embedded"), available=True)
    assert embedded._backend_route.embedded is True

    graph_only = RealCogneeAdapter(
        KnowledgeConfig(
            state_dir=tmp_path / "g" / "state",
            config_dir=tmp_path / "g" / "config",
            graph_engine="neo4j",
            neo4j_password_file=secret_file(tmp_path / "g", "neo4j.secret", "graph-pw"),
        ),
        available=True,
    )
    assert graph_only._backend_route.semantic is False
    assert [name for name, _ in fake_config.calls] == ["set_graph_db_config"]


def test_evict_failure_is_reported_without_the_config_dict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cognee_adapter, "_LAST_SEMANTIC_BINDING", None)

    class RaisingCache:
        def evict(self, force_close: bool = False, **kwargs: Any) -> bool:
            raise ValueError(f"unknown parameter for config {kwargs}")

    install_fake_cognee(monkeypatch, FakeCogneeConfig(), vector_engine_cache=RaisingCache())  # type: ignore[arg-type]
    with pytest.raises(EngineUnavailableError) as excinfo:
        RealCogneeAdapter(_semantic_config(tmp_path), available=True)
    message = str(excinfo.value)
    assert "vector_engine_cache.evict failed" in message
    assert "ValueError" in message
    assert PG_SECRET not in message
    assert "vector_db_password" not in message


def test_evict_bound_vector_engine_returns_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = FakeVectorEngineCache(cached=[{"vector_db_provider": "pgvector", "vector_db_name": "omp_cognee"}])
    monkeypatch.setattr(cognee_adapter, "VECTOR_ENGINE_CACHE", cache)
    monkeypatch.setattr(
        cognee_adapter,
        "VECTOR_CONFIG_READER",
        lambda: {"vector_db_provider": "pgvector", "vector_db_name": "omp_cognee"},
    )
    assert cognee_adapter.evict_bound_vector_engine("test") is True
    assert cognee_adapter.evict_bound_vector_engine("test") is False
    assert len(cache.evict_calls) == 2


# ---------------------------------------------------------------------------
# 4. Retire counts come from readback
# ---------------------------------------------------------------------------


def _seed_snapshot(graph: GraphDouble, scope: SnapshotScope) -> tuple[UUID, UUID]:
    """Two fact nodes, the repository anchor, and three edges among them."""
    anchor = scope.repo_node_id()
    graph.seed_node(anchor, {"id": str(anchor), "name": f"repo-{scope.repository_id}@{scope.snapshot_id}"})
    f1 = _seed_fact(graph, scope, "f1", "alpha", {})
    f2 = _seed_fact(graph, scope, "f2", "beta", {})
    graph.edges.extend(
        [
            (str(f1), str(anchor), "part_of", {}),
            (str(f2), str(anchor), "part_of", {}),
            (str(f1), str(f2), "calls", {}),
        ]
    )
    return f1, f2


@pytest.mark.asyncio
async def test_retire_reports_node_edge_and_vector_counts_from_readback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    vector = FakeVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path, vector)
    graph = GraphDouble()
    f1, f2 = _seed_snapshot(graph, scope)
    await vector.fake_index_data_points([_vector_point(f1, scope), _vector_point(f2, scope)])
    # A row of another snapshot in the same collection must survive.
    other = _vector_point(uuid4(), _scope())
    await vector.fake_index_data_points([other])
    _wire_graph(monkeypatch, graph)

    res = await adapter.retire(**_kwargs(scope))

    assert res.success is True
    assert res.nodes_deleted == 3
    assert res.edges_deleted == 3
    assert res.vector_rows_deleted == 2
    assert res.vector_cleanup == "complete"
    assert res.vector_cleanup_reason is None
    assert graph.nodes == {} and graph.edges == []
    assert set(vector.collections["CodeSymbol_description"]) == {str(other.id)}
    # Rows were deleted by id after a retrieve, never by scope guesswork.
    assert vector.deleted_calls == [("CodeSymbol_description", [f1, f2])] or vector.deleted_calls == [
        ("CodeSymbol_description", [f2, f1])
    ]


@pytest.mark.asyncio
async def test_retire_outage_optional_is_degraded_not_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    down = FakeVectorEngine(vector_dimension=1024, embed_error=EmbeddingException(f"Cannot connect ({PG_SECRET})"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, down, embedding_required=False)
    graph = GraphDouble()
    _seed_snapshot(graph, scope)
    _wire_graph(monkeypatch, graph)

    res = await adapter.retire(**_kwargs(scope))

    assert res.success is True
    assert res.nodes_deleted == 3
    assert res.edges_deleted == 3
    assert res.vector_rows_deleted is None, "an unverified cleanup must never report a count"
    assert res.vector_cleanup == "degraded"
    assert res.vector_cleanup_reason is not None
    assert "backend unreachable during retire" in res.vector_cleanup_reason
    assert "rows verified deleted before the outage: 0" in res.vector_cleanup_reason
    assert PG_SECRET not in res.vector_cleanup_reason
    assert graph.nodes == {}


@pytest.mark.asyncio
async def test_retire_outage_required_raises_before_graph_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    down = FakeVectorEngine(vector_dimension=1024, embed_error=ConnectionRefusedError("refused"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, down, embedding_required=True)
    graph = GraphDouble()
    _seed_snapshot(graph, scope)
    _wire_graph(monkeypatch, graph)

    with pytest.raises(EngineUnavailableError, match="backend unreachable during retire"):
        await adapter.retire(**_kwargs(scope))
    assert graph.deleted == []
    assert len(graph.nodes) == 3


class _StickyVectorEngine(FakeVectorEngine):
    """``delete_data_points`` is accepted but rows stay: the readback exposes it."""

    async def delete_data_points(self, collection_name: str, data_point_ids: list[Any]) -> None:
        self.deleted_calls.append((collection_name, list(data_point_ids)))


@pytest.mark.asyncio
async def test_retire_rows_surviving_delete_are_partial_or_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()

    sticky = _StickyVectorEngine(vector_dimension=1024)
    adapter = _semantic_adapter(monkeypatch, tmp_path / "opt", sticky, embedding_required=False)
    graph = GraphDouble()
    f1, f2 = _seed_snapshot(graph, scope)
    await sticky.fake_index_data_points([_vector_point(f1, scope), _vector_point(f2, scope)])
    _wire_graph(monkeypatch, graph)
    res = await adapter.retire(**_kwargs(scope))
    assert res.vector_cleanup == "partial"
    assert res.vector_rows_deleted == 0
    assert res.vector_cleanup_reason is not None
    assert "2 vector row(s) still read back" in res.vector_cleanup_reason
    assert res.nodes_deleted == 3

    sticky_req = _StickyVectorEngine(vector_dimension=1024)
    adapter_req = _semantic_adapter(monkeypatch, tmp_path / "req", sticky_req, embedding_required=True)
    graph_req = GraphDouble()
    g1, g2 = _seed_snapshot(graph_req, scope)
    await sticky_req.fake_index_data_points([_vector_point(g1, scope), _vector_point(g2, scope)])
    _wire_graph(monkeypatch, graph_req)
    with pytest.raises(EngineError, match="2 still read back"):
        await adapter_req.retire(**_kwargs(scope))
    assert graph_req.deleted == []


@pytest.mark.asyncio
async def test_retire_without_stale_nodes_reports_not_checked_and_touches_no_vector_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    untouchable = FakeVectorEngine(vector_dimension=1024, embed_error=RuntimeError("must not be called"))
    adapter = _semantic_adapter(monkeypatch, tmp_path, untouchable)
    _wire_graph(monkeypatch, GraphDouble())

    res = await adapter.retire(**_kwargs(scope))
    assert res.nodes_deleted == 0
    assert res.edges_deleted == 0
    assert res.vector_rows_deleted is None
    assert res.vector_cleanup == "not_checked"
    assert untouchable.deleted_calls == []


@pytest.mark.asyncio
async def test_retire_surviving_nodes_raise_instead_of_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    graph = GraphDouble()
    _seed_snapshot(graph, scope)
    graph.refuse_delete = True
    _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(EngineError, match="3 node\\(s\\) and 3 edge\\(s\\) still read back"):
        await adapter.retire(**_kwargs(scope))


@pytest.mark.asyncio
async def test_graph_only_retire_has_no_vector_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    graph = GraphDouble()
    _seed_snapshot(graph, scope)
    _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    res = await adapter.retire(**_kwargs(scope))
    assert (res.nodes_deleted, res.edges_deleted) == (3, 3)
    assert res.vector_rows_deleted is None
    assert res.vector_cleanup is None
    assert res.vector_cleanup_reason is None


# ---------------------------------------------------------------------------
# 5. Correction recovery
# ---------------------------------------------------------------------------

ORIGINAL = {"symbol_kind": "class", "visibility": "public"}
UPDATE = {"symbol_kind": "dataclass"}


@pytest.mark.asyncio
async def test_correct_reverts_prior_node_and_edges_when_rebuild_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    expected = _seed_edges(graph, scope, node_id)
    writer = _wire_graph(monkeypatch, graph)
    writer.failures = 1  # the corrected node cannot be written; the revert write succeeds
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(CorrectionFailedError) as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))

    err = excinfo.value
    assert isinstance(err, EngineError)
    assert err.reverted is True
    assert err.retryable is True
    assert err.phase == "add_data_points"
    assert err.node_present is True
    assert err.missing_edges == ()
    assert err.revert_error is None
    assert "restored and verified" in str(err) and "safe to retry" in str(err)
    assert "simulated add_data_points failure" in str(err)
    assert isinstance(err.__cause__, RuntimeError)

    # The graph is exactly as before: original properties, all three edges.
    assert graph.stored_properties(node_id)["fact_properties"] == ORIGINAL
    assert graph.edge_keys() == expected
    # Only the revert write reached the store; the corrected point never did.
    assert [p.fields["fact_properties"] for p in writer.written] == [ORIGINAL]

    # A retry applies the correction cleanly.
    retry = await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert retry.success is True
    assert graph.stored_properties(node_id)["fact_properties"] == {**ORIGINAL, **UPDATE}
    assert graph.edge_keys() == expected


@pytest.mark.asyncio
async def test_correct_exposes_partial_state_when_revert_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    _seed_edges(graph, scope, node_id)
    writer = _wire_graph(monkeypatch, graph)
    writer.failures = 2  # rebuild write and revert write both fail
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(CorrectionFailedError) as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))

    err = excinfo.value
    assert err.reverted is False
    assert err.node_present is False
    assert len(err.missing_edges) == 3 and err.expected_edges == 3
    assert err.revert_error is not None and "simulated add_data_points failure" in err.revert_error
    assert "partial state" in str(err) and "node_present=False" in str(err) and "retry required" in str(err)
    assert str(node_id) not in graph.nodes

    # A retry against the now-absent node reports failure explicitly instead of
    # inventing a correction.
    retry = await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert retry.success is False


@pytest.mark.asyncio
async def test_correct_reverts_when_edge_restore_raises_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    expected = _seed_edges(graph, scope, node_id)
    graph.add_edges_failures = 1
    _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(CorrectionFailedError) as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert excinfo.value.phase == "add_edges"
    assert excinfo.value.reverted is True
    assert graph.stored_properties(node_id)["fact_properties"] == ORIGINAL
    assert graph.edge_keys() == expected


@pytest.mark.asyncio
async def test_correct_edge_loss_after_revert_is_explicit_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    _seed_edges(graph, scope, node_id)
    graph.drop_edges_on_add = True
    _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(CorrectionFailedError, match="3 of 3 edges did not read back") as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    err = excinfo.value
    assert err.phase == "verify"
    assert err.reverted is False
    assert err.node_present is True
    assert len(err.missing_edges) == 3
    assert "missing 3 of 3 incident edge(s)" in str(err)
    # The original node was put back even though its edges could not be.
    assert graph.stored_properties(node_id)["fact_properties"] == ORIGINAL


@pytest.mark.asyncio
async def test_correct_rebuilt_node_that_does_not_read_back_is_never_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    graph = GraphDouble()
    _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    writer = _wire_graph(monkeypatch, graph)
    writer.write_nowhere = True
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(CorrectionFailedError, match="did not read back after add_data_points") as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert excinfo.value.reverted is False
    assert excinfo.value.node_present is False


@pytest.mark.asyncio
async def test_correct_delete_failure_changes_nothing_and_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    expected = _seed_edges(graph, scope, node_id)
    graph.fail_delete_with = RuntimeError("delete refused")
    writer = _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    with pytest.raises(RuntimeError, match="delete refused"):
        await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert writer.written == []
    assert graph.stored_properties(node_id)["fact_properties"] == ORIGINAL
    assert graph.edge_keys() == expected


@pytest.mark.asyncio
async def test_correct_success_path_writes_only_the_corrected_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _scope()
    graph = GraphDouble()
    node_id = _seed_fact(graph, scope, "f1", "User", ORIGINAL)
    expected = _seed_edges(graph, scope, node_id)
    writer = _wire_graph(monkeypatch, graph)
    adapter = RealCogneeAdapter(_embedded_config(tmp_path), available=True)

    res = await adapter.correct(**_kwargs(scope), fact_id="f1", properties_update=dict(UPDATE))
    assert res.success is True
    assert res.semantic_status is None and res.semantic_reason is None
    assert [p.fields["fact_properties"] for p in writer.written] == [{**ORIGINAL, **UPDATE}]
    assert graph.edge_keys() == expected
