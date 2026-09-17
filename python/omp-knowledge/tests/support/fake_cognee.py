"""Test doubles for the Cognee configuration and vector surface the adapter binds to.

These classes and helpers are seam control-flow doubles for fast in-memory unit
tests. They are NEVER evidence of semantic capability, embedding quality, or
real pgvector storage. Real pgvector integration tests run against live Postgres
and inference endpoints.

``FakeVectorEngine`` mirrors the shape of Cognee's ``PGVectorAdapter`` exactly as
the adapter consumes it: the embedding engine (``mock`` flag, ``get_vector_size``)
hangs off ``embedding_engine``; ``embed_data`` / ``has_collection`` / ``get_table``
/ ``search`` / ``retrieve`` / ``delete_data_points`` are coroutines; ``search``
honours the ``node_name`` + ``node_name_filter_operator`` tag filter over each
point's ``belongs_to_set``; ``get_table`` reflects a ``Vector(dim)`` column.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

import httpx
import pytest

from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import semantic_collection

SETTER_NAMES = (
    "set_graph_db_config",
    "set_relational_db_config",
    "set_vector_db_config",
    "set_embedding_config",
)


class FakeCogneeConfig:
    """Stand-in for ``cognee.config`` recording and applying setter invocations."""

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        fail_message: str = "",
        ignore_keys: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.applied: dict[str, SimpleNamespace] = {name: SimpleNamespace() for name in SETTER_NAMES}
        self._fail_on = fail_on
        self._fail_message = fail_message
        self._ignore_keys = dict(ignore_keys or {})

    def _record(self, name: str, payload: Mapping[str, Any]) -> None:
        self.calls.append((name, dict(payload)))
        if self._fail_on == name:
            raise AttributeError(self._fail_message)
        ignored = self._ignore_keys.get(name, ())
        for key, value in payload.items():
            if key not in ignored:
                setattr(self.applied[name], key, value)

    def set_graph_db_config(self, payload: Mapping[str, Any]) -> None:
        self._record("set_graph_db_config", payload)

    def set_relational_db_config(self, payload: Mapping[str, Any]) -> None:
        self._record("set_relational_db_config", payload)

    def set_vector_db_config(self, payload: Mapping[str, Any]) -> None:
        self._record("set_vector_db_config", payload)

    def set_embedding_config(self, payload: Mapping[str, Any]) -> None:
        self._record("set_embedding_config", payload)

    def get_graph_db_config(self) -> Any:
        return self.applied["set_graph_db_config"]

    def get_relational_db_config(self) -> Any:
        return self.applied["set_relational_db_config"]

    def get_vector_db_config(self) -> Any:
        return self.applied["set_vector_db_config"]

    def get_embedding_config(self) -> Any:
        return self.applied["set_embedding_config"]

    def getters(self) -> dict[str, Callable[[], Any]]:
        """Readback getters keyed like ``cognee_adapter.CONFIG_READBACK_GETTERS``."""
        return {name: (lambda name=name: self.applied[name]) for name in SETTER_NAMES}


class FakeVectorEngine:
    """Seam control-flow double for Cognee's pgvector engine (see module docstring).

    ``vector_dimension`` is what the embedding engine reports and what every
    reflected table declares unless ``table_dims`` overrides a collection
    (``None`` reflects a table without a ``vector`` column). ``embed_error`` is
    raised by ``embed_data``; ``embed_override`` replaces its result. Collections
    exist on demand (``auto_collections``) so readiness walks all of them.
    """

    def __init__(
        self,
        *,
        vector_dimension: int = 1024,
        mock: bool = False,
        table_dims: Mapping[str, int | None] | None = None,
        embed_error: Exception | None = None,
        embed_override: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        self.vector_dimension = vector_dimension
        self.embedding_engine = SimpleNamespace(mock=mock, get_vector_size=lambda: vector_dimension)
        self.table_dims = dict(table_dims or {})
        self.embed_error = embed_error
        self.embed_override = embed_override
        self.collections: dict[str, dict[str, Any]] = {}
        self.indexed_points: list[Any] = []
        self.deleted_calls: list[tuple[str, list[Any]]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.auto_collections: bool = True

    async def embed_data(self, texts: list[str]) -> list[list[float]]:
        if self.embed_error is not None:
            raise self.embed_error
        if self.embed_override is not None:
            return self.embed_override(texts)
        # Deterministic non-zero control-flow vectors; never an embedding.
        result: list[list[float]] = []
        for text in texts:
            val = float((hash(text) % 1000) + 1) / 1000.0
            result.append([val] * self.vector_dimension)
        return result

    async def has_collection(self, collection_name: str) -> bool:
        if self.auto_collections:
            return True
        return collection_name in self.collections

    async def get_table(self, collection_name: str) -> Any:
        dim = self.table_dims.get(collection_name, self.vector_dimension)
        if dim is None:
            return None
        return SimpleNamespace(c=SimpleNamespace(vector=SimpleNamespace(type=SimpleNamespace(dim=dim))))

    async def search(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 10,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "AND",
    ) -> list[Any]:
        self.search_calls.append(
            {
                "collection_name": collection_name,
                "query_vector_len": len(query_vector),
                "limit": limit,
                "node_name": node_name,
                "node_name_filter_operator": node_name_filter_operator,
            }
        )
        matches: list[Any] = []
        for point in self.collections.get(collection_name, {}).values():
            point_tags = list(getattr(point, "belongs_to_set", []) or [])
            if node_name:
                if node_name_filter_operator == "AND":
                    if not all(tag in point_tags for tag in node_name):
                        continue
                elif not any(tag in point_tags for tag in node_name):
                    continue
            matches.append(point)

        # Cosine distance: lower score ranks first, in insertion order.
        return [
            SimpleNamespace(id=pt.id, score=0.1 * (idx + 1), payload={"belongs_to_set": getattr(pt, "belongs_to_set", [])})
            for idx, pt in enumerate(matches[:limit])
        ]

    async def retrieve(self, collection_name: str, data_point_ids: list[str]) -> list[Any]:
        coll = self.collections.get(collection_name, {})
        return [coll[str(pid)] for pid in data_point_ids if str(pid) in coll]

    async def delete_data_points(self, collection_name: str, data_point_ids: list[Any]) -> None:
        self.deleted_calls.append((collection_name, list(data_point_ids)))
        coll = self.collections.get(collection_name, {})
        for pid in data_point_ids:
            coll.pop(str(pid), None)

    async def fake_index_data_points(self, points: list[Any], vector_engine: Any = None) -> None:
        """Recording stand-in for ``cognee.tasks.storage.index_data_points``.

        Rows land in the ``<Model>_description`` collection derived from the
        point's ``kind`` (as the adapter does), falling back to the class name
        the way the real task does.
        """
        target = vector_engine if isinstance(vector_engine, FakeVectorEngine) else self
        for pt in points:
            target.indexed_points.append(pt)
            kind = getattr(pt, "kind", None)
            coll_name = semantic_collection(kind) if kind else f"{type(pt).__name__}_description"
            target.collections.setdefault(coll_name, {})[str(pt.id)] = pt


class FakeVectorEngineCache:
    """Stand-in for the pinned Cognee's public ``vector_engine_cache`` (``EngineCacheOps``).

    Records every ``evict(**config)`` call with the exact config mapping the
    adapter passed (the vector config read back at that moment). ``cached`` holds
    the config mappings that count as live entries; ``evict`` returns True and
    drops the entry when it matches, mirroring ``EngineCacheOps.evict``.
    """

    def __init__(self, cached: list[Mapping[str, Any]] | None = None) -> None:
        self.cached: list[dict[str, Any]] = [dict(entry) for entry in (cached or [])]
        self.evict_calls: list[dict[str, Any]] = []

    def evict(self, force_close: bool = False, **kwargs: Any) -> bool:
        config = dict(kwargs)
        self.evict_calls.append(config)
        hit = config in self.cached
        self.cached = [entry for entry in self.cached if entry != config]
        return hit


def install_fake_cognee(
    monkeypatch: pytest.MonkeyPatch,
    fake_config: object,
    *,
    vector_engine_cache: FakeVectorEngineCache | None = None,
) -> FakeVectorEngineCache:
    """Route the adapter's Cognee binding at ``fake_config`` for the external route.

    Marks the optional driver modules as present and Cognee as importable so the
    setter / readback path runs without the real packages. The public
    vector-engine cache path the adapter uses for binding invalidation is pointed
    at ``vector_engine_cache`` (a fresh ``FakeVectorEngineCache`` by default) and
    its config reader at the vector config ``fake_config`` has applied, so the
    real pinned Cognee cache is never touched by unit tests. Returns the cache.
    """
    monkeypatch.setattr(cognee_adapter, "missing_backend_modules", lambda route: ())
    monkeypatch.setattr(cognee_adapter, "COGNEE_AVAILABLE", True)
    monkeypatch.setattr(cognee_adapter, "cognee", SimpleNamespace(config=fake_config), raising=False)
    getters = fake_config.getters() if hasattr(fake_config, "getters") else {}
    monkeypatch.setattr(cognee_adapter, "CONFIG_READBACK_GETTERS", getters)

    cache = vector_engine_cache if vector_engine_cache is not None else FakeVectorEngineCache()

    def _read_vector_config() -> dict[str, Any]:
        applied = getattr(fake_config, "applied", None)
        if isinstance(applied, dict) and "set_vector_db_config" in applied:
            return dict(vars(applied["set_vector_db_config"]))
        return {}

    monkeypatch.setattr(cognee_adapter, "VECTOR_ENGINE_CACHE", cache)
    monkeypatch.setattr(cognee_adapter, "VECTOR_CONFIG_READER", _read_vector_config)
    return cache


def install_fake_vector_engine(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeVectorEngine | Callable[..., Any] | Mapping[str, FakeVectorEngine] | None = None,
) -> Any:
    """Point the adapter's vector seams (``get_vector_engine_async`` and
    ``index_data_points``) at ``fake_engine``."""
    if fake_engine is None:
        fake_engine = FakeVectorEngine()

    async def _get_engine() -> FakeVectorEngine:
        if isinstance(fake_engine, Mapping):
            endpoint = ""
            getter = cognee_adapter.CONFIG_READBACK_GETTERS.get("set_embedding_config")
            if callable(getter):
                endpoint = getattr(getter(), "embedding_endpoint", "")
            if endpoint in fake_engine:
                return fake_engine[endpoint]
            if "default" in fake_engine:
                return fake_engine["default"]
            raise KeyError(f"No fake vector engine configured for endpoint {endpoint!r}")
        if callable(fake_engine):
            res = fake_engine()
            return await res if inspect.isawaitable(res) else res
        return fake_engine

    async def _fake_index(points: list[Any], vector_engine: Any = None) -> None:
        engine = vector_engine
        if engine is None:
            engine = await _get_engine()
        if hasattr(engine, "fake_index_data_points"):
            await engine.fake_index_data_points(points, vector_engine=engine)
        elif hasattr(engine, "indexed_points"):
            for pt in points:
                engine.indexed_points.append(pt)

    monkeypatch.setattr(cognee_adapter, "get_vector_engine_async", _get_engine)
    monkeypatch.setattr(cognee_adapter, "index_data_points", _fake_index)
    return fake_engine


FAKE_RERANK_MODEL = "qwen3-reranker-0.6b-q8"

# A responder is called with the parsed JSON request body and returns the
# ``httpx.Response`` to hand back (or a coroutine resolving to one). An exception
# instance is raised instead of answering, so transport failures can be staged.
RerankResponder = Callable[[dict[str, Any]], Any]


def rerank_response(
    scores: Sequence[float | Any],
    *,
    model: str | None = None,
    status_code: int = 200,
    indices: Sequence[Any] | None = None,
) -> httpx.Response:
    """A ``/v1/rerank`` body in the qualified shape (``results[].index`` +
    ``results[].relevance_score``). ``scores`` are control-flow values chosen by
    the test to drive a permutation; they are never a relevance judgement.
    ``indices`` overrides the index of each result (to stage duplicates, gaps or
    out-of-range values) and ``model`` adds the echoed ``model`` field."""
    idx = list(indices) if indices is not None else list(range(len(scores)))
    payload: dict[str, Any] = {
        "results": [{"index": i, "relevance_score": s} for i, s in zip(idx, scores)],
    }
    if model is not None:
        payload["model"] = model
    return httpx.Response(status_code, json=payload)


class RecordingRerankTransport:
    """Seam control-flow double for the reranker HTTP endpoint.

    Records every request the adapter makes (method, URL path, ``Authorization``
    header, parsed JSON body) in ``requests``. ``GET /v1/models`` lists ``models``
    (or delegates to ``models_responder``); ``POST /v1/rerank`` is answered by
    ``rerank`` (a responder callable, an exception instance to raise, or ``None``
    for a default that scores every document by its request index). Any other
    path is a 404. This double is NEVER evidence of reranking capability; the
    live evals in ``tests/test_rerank_integration.py`` are.
    """

    def __init__(
        self,
        *,
        models: Sequence[str] | None = (FAKE_RERANK_MODEL,),
        rerank: RerankResponder | BaseException | None = None,
        models_responder: Callable[[httpx.Request], Any] | BaseException | None = None,
    ) -> None:
        self.models = list(models or ())
        self.rerank = rerank
        self.models_responder = models_responder
        self.requests: list[dict[str, Any]] = []

    @property
    def rerank_requests(self) -> list[dict[str, Any]]:
        return [entry for entry in self.requests if entry["path"] == "/v1/rerank"]

    @property
    def models_requests(self) -> list[dict[str, Any]]:
        return [entry for entry in self.requests if entry["path"] == "/v1/models"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "json": body,
            }
        )
        if request.url.path == "/v1/models":
            if isinstance(self.models_responder, BaseException):
                raise self.models_responder
            if self.models_responder is not None:
                result = self.models_responder(request)
                return await result if inspect.isawaitable(result) else result
            return httpx.Response(200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})
        if request.url.path == "/v1/rerank":
            if isinstance(self.rerank, BaseException):
                raise self.rerank
            if self.rerank is not None:
                result = self.rerank(body if isinstance(body, dict) else {})
                return await result if inspect.isawaitable(result) else result
            documents = body.get("documents", []) if isinstance(body, dict) else []
            return rerank_response([float(i) for i in range(len(documents))])
        return httpx.Response(404, json={"error": "not found"})

    def mock_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def install_fake_reranker(
    monkeypatch: pytest.MonkeyPatch,
    transport: RecordingRerankTransport | httpx.MockTransport | None = None,
) -> RecordingRerankTransport | httpx.MockTransport:
    """Point ``cognee_adapter.rerank_http_client`` at an ``httpx.AsyncClient``
    over ``transport`` (a fresh ``RecordingRerankTransport`` by default) so no
    unit test can reach a real reranker. Returns the transport."""
    if transport is None:
        transport = RecordingRerankTransport()
    mock = transport.mock_transport() if isinstance(transport, RecordingRerankTransport) else transport

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=mock, timeout=timeout)

    monkeypatch.setattr(cognee_adapter, "rerank_http_client", _client)
    return transport


def secret_file(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
