from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping, TypeVar
from uuid import NAMESPACE_OID, UUID, uuid5

import httpx
from pydantic import ValidationError

from omp_work.knowledge_contracts import (
    FactRecord,
    ProviderRoute,
    SnapshotRef,
    SourceRef,
)

from ..config import KnowledgeConfig
from ..staging.manifest import compute_graph_sha256
from .backends import (
    INCIDENT_EDGE_QUERY_CHUNK,
    NEO4J_URI_SCHEMES,
    RERANK_PAIR_FORMAT,
    REQUIRED_NEO4J_PROCEDURES,
    SEMANTIC_COLLECTIONS,
    BackendConfigError,
    BackendRoute,
    CogneeBackendSettings,
    EdgeKey,
    EdgeRecord,
    GraphDialect,
    SnapshotScope,
    binding_tag,
    build_cognee_backend_settings,
    classify_rerank_status,
    dialect_for,
    is_backend_connection_failure,
    is_embedding_failure,
    missing_backend_modules,
    parse_rerank_response,
    read_secret_ref,
    redact,
    rerank_documents,
    rerank_request_sha256,
    resolve_backend_route,
    semantic_collection,
    semantic_text,
)
from .protocol import (
    CorrectionFailedError,
    CorrectResult,
    EngineError,
    EngineStatus,
    EngineUnavailableError,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RerankBindingError,
    RerankContractError,
    RerankReport,
    RetireResult,
    RetrievalReport,
    SemanticBindingError,
    SemanticIndexReport,
)

T = TypeVar("T")


def rerank_http_client(timeout: float) -> httpx.AsyncClient:
    """HTTP client the adapter uses for the reranker (``GET /v1/models``,
    ``POST /v1/rerank``), created per call and used as ``async with``.

    Module-level seam: unit tests monkeypatch this name to hand the adapter an
    ``httpx.AsyncClient`` over ``httpx.MockTransport``; production always speaks
    to the configured endpoint over the default transport.
    """
    return httpx.AsyncClient(timeout=timeout)

# Optional Cognee imports - strictly flagged so lack of Cognee raises EngineUnavailableError.
# Only public, documented names of the pinned cognee==1.5.4 are imported here; the
# embedded route (ladybug + lancedb, graph_only) depends on nothing beyond this block.
try:
    import cognee
    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine
    from cognee.infrastructure.databases.relational.config import get_relational_config
    from cognee.infrastructure.databases.relational.create_relational_engine import create_relational_engine
    from cognee.infrastructure.databases.vector.config import get_vectordb_config
    from cognee.infrastructure.databases.vector.embeddings.config import get_embedding_config
    from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import create_embedding_engine
    from cognee.infrastructure.databases.vector.get_vector_engine import get_vector_engine_async
    from cognee.infrastructure.engine.models.DataPoint import DataPoint
    from cognee.tasks.code_graph.models import (
        ApiEndpoint,
        CodeAssociation,
        CodeExtractionAccount,
        CodeInsight,
        CodeIntent,
        CodeLintFinding,
        CodeModule,
        CodeRepository,
        CodeService,
        CodeSymbol,
        CodeTestReference,
        CodeFileReference,
        ExternalDependency,
        StorageResource,
    )
    from cognee.tasks.storage.add_data_points import add_data_points
    from cognee.tasks.storage.index_data_points import index_data_points

    COGNEE_AVAILABLE = True
except ImportError:
    COGNEE_AVAILABLE = False
    DataPoint = Any  # type: ignore[misc,assignment]
    index_data_points = Any  # type: ignore[misc,assignment]
    get_vector_engine_async = Any  # type: ignore[misc,assignment]
    get_base_config = Any  # type: ignore[misc,assignment]
    get_graph_config = Any  # type: ignore[misc,assignment]
    get_relational_config = Any  # type: ignore[misc,assignment]
    create_relational_engine = Any  # type: ignore[misc,assignment]
    get_vectordb_config = Any  # type: ignore[misc,assignment]
    get_embedding_config = Any  # type: ignore[misc,assignment]
    create_embedding_engine = Any  # type: ignore[misc,assignment]

# Cognee's supported config getters, keyed by the setter whose effect they read
# back. Each ``config.set_*_db_config`` mutates the object its getter returns, so a
# value that does not read back identically was not applied. Imported separately
# from the block above so a missing getter never disables the embedded route; an
# external route with no getter for one of its setters fails closed instead.
CONFIG_READBACK_GETTERS: dict[str, Callable[[], Any]] = {}
if COGNEE_AVAILABLE:
    for _setter_name, _getter_module, _getter_name in (
        ("set_graph_db_config", "cognee.infrastructure.databases.graph.config", "get_graph_config"),
        ("set_relational_db_config", "cognee.infrastructure.databases.relational.config", "get_relational_config"),
        ("set_vector_db_config", "cognee.infrastructure.databases.vector.config", "get_vectordb_config"),
        ("set_embedding_config", "cognee.infrastructure.databases.vector.embeddings.config", "get_embedding_config"),
    ):
        try:
            _module = __import__(_getter_module, fromlist=[_getter_name])
            _getter = getattr(_module, _getter_name)
        except Exception:  # pragma: no cover - depends on the pinned cognee layout
            continue
        if callable(_getter):
            CONFIG_READBACK_GETTERS[_setter_name] = _getter

# Semantic-route cache invalidation contract against the pinned Cognee.
#
# Cognee caches vector engines in a ``closing_lru_cache`` keyed by the vector
# config only; the embedding engine is captured at creation. The public,
# documented way to drop such an entry is ``vector_engine_cache.evict(**config)``
# (an ``EngineCacheOps`` instance in ``create_vector_engine``) with the config
# dict ``get_vectordb_context_config()`` returns, the same dict Cognee's own
# ``get_vector_engine_async`` handle resolves through. No underscore-private
# factory is touched. Both names are imported separately from the block above so
# their absence can never disable the embedded route; a semantic route that cannot
# invalidate its binding fails closed with ``SEMANTIC_CACHE_CONTRACT`` in the
# message instead of silently reusing an engine bound to a previous endpoint.
SEMANTIC_CACHE_CONTRACT = (
    "cognee==1.5.4: cognee.infrastructure.databases.vector.create_vector_engine.vector_engine_cache "
    "(EngineCacheOps.evict keyed by the vector config dict) and "
    "cognee.infrastructure.databases.vector.config.get_vectordb_context_config"
)
VECTOR_ENGINE_CACHE: Any = None
VECTOR_CONFIG_READER: Callable[[], Mapping[str, Any]] | None = None
if COGNEE_AVAILABLE:
    try:
        from cognee.infrastructure.databases.vector.config import (
            get_vectordb_context_config as _get_vectordb_context_config,
        )
        from cognee.infrastructure.databases.vector.create_vector_engine import (
            vector_engine_cache as _vector_engine_cache,
        )
    except Exception:  # pragma: no cover - depends on the pinned cognee layout
        pass
    else:
        VECTOR_ENGINE_CACHE = _vector_engine_cache
        VECTOR_CONFIG_READER = _get_vectordb_context_config

_LAST_SEMANTIC_BINDING: tuple[Any, ...] | None = None


def evict_bound_vector_engine(context: str = "semantic binding change") -> bool:
    """Evict Cognee's cached vector engine for the currently applied vector config.

    Uses only the public cache path named in ``SEMANTIC_CACHE_CONTRACT``. Returns
    True when an entry existed. Raises ``EngineUnavailableError`` (never a silent
    no-op) when the pinned Cognee does not expose that path: a semantic route that
    cannot invalidate its engine binding must not come up. Error text never carries
    the config dict, which holds the pgvector password.
    """
    ops = VECTOR_ENGINE_CACHE
    reader = VECTOR_CONFIG_READER
    if ops is None or not callable(getattr(ops, "evict", None)) or not callable(reader):
        raise EngineUnavailableError(
            f"engine_unavailable: pinned cognee does not expose the public vector-engine cache path "
            f"required to invalidate the semantic binding during {context}; expected {SEMANTIC_CACHE_CONTRACT}"
        )
    try:
        config = dict(reader())
        return bool(ops.evict(**config))
    except Exception as exc:
        raise EngineUnavailableError(
            f"engine_unavailable: cognee vector_engine_cache.evict failed during {context}: {type(exc).__name__}"
        ) from exc


def clear_semantic_engine_caches(context: str = "semantic binding change") -> None:
    """Drop Cognee's process-global vector engine, embedding engine and config caches.

    Invoked whenever the external semantic binding (endpoint, model, dimension,
    database config) changes so an engine bound to a previous endpoint or
    configuration is never reused. The vector engine is evicted first, through
    the public cache path, while the config it was created from is still the
    applied one (its cache key derives from that config); the ``lru_cache``
    getters and factories that follow are public pinned-Cognee functions.
    """
    evict_bound_vector_engine(context)
    for cache_fn in (
        create_embedding_engine,
        get_embedding_config,
        get_vectordb_config,
        create_relational_engine,
        get_relational_config,
    ):
        clear_fn = getattr(cache_fn, "cache_clear", None)
        if callable(clear_fn):
            clear_fn()


def snapshot_fact_node_id(snapshot_label: str, fact_id: str) -> UUID:
    """Deterministic node UUID that preserves the full original Enola fact.id
    (incorporating repo, kind, name, file) under the snapshot-isolated namespace.
    Unlike stock Cognee uuid5(repo, kind, name), same-named facts in different
    files NEVER collide.
    """
    key = f"omp-fact:{snapshot_label}:{fact_id}"
    return uuid5(NAMESPACE_OID, key)


def snapshot_repo_node_id(snapshot_label: str, repo: str) -> UUID:
    key = f"omp-repo:{snapshot_label}:{repo}"
    return uuid5(NAMESPACE_OID, key)


def _exact_match(fact: FactRecord, query_text: str) -> bool:
    """Exact (substring) match on a fact's name or kind; ``*`` matches everything.

    Shared by the graph-only route, the exact tail appended after semantic hits,
    and the optional-policy fallback so the three cannot drift.
    """
    if query_text == "*":
        return True
    query_lower = query_text.lower()
    return (bool(fact.name) and query_lower in str(fact.name).lower()) or query_lower in fact.kind.lower()


def _get_model_class(kind: str) -> Any:
    """Pinned Cognee code-graph model for a fact kind; ``semantic_collection`` in
    ``backends`` maps the same kinds to the ``<Model>_description`` collections."""
    if not COGNEE_AVAILABLE:
        return None
    models_map = {
        "module": CodeModule,
        "symbol": CodeSymbol,
        "route": ApiEndpoint,
        "storage": StorageResource,
        "dependency": ExternalDependency,
        "service": CodeService,
        "test_ref": CodeTestReference,
        "file_ref": CodeFileReference,
        "insight": CodeInsight,
        "intent": CodeIntent,
        "extraction": CodeExtractionAccount,
        "association": CodeAssociation,
        "lint": CodeLintFinding,
    }
    return models_map.get(kind)


def _is_withdrawn(props: Mapping[str, Any]) -> bool:
    return bool(props.get("withdrawn") or props.get("status") == "withdrawn" or props.get("withdrawn_by"))


class RealCogneeAdapter(KnowledgeEngine):
    """Real Cognee adapter using embedded Ladybug graph + LanceDB + SQLite metadata.
    Provides snapshot-isolated code graph ingestion, query, and lookup without
    global sweep side effects or cross-file fact collapsing.
    """

    def __init__(self, config: KnowledgeConfig, *, available: bool | None = None) -> None:
        self.config = config
        self._available = COGNEE_AVAILABLE if available is None else available
        # Route resolution is pure and credential-free; it raises BackendConfigError
        # for an unsupported graph_engine / vector_store, a neo4j_uri or
        # embedding_endpoint carrying userinfo, or an unsupported endpoint scheme,
        # rather than falling back.
        self._backend_route: BackendRoute = resolve_backend_route(config)
        self._dialect: GraphDialect = dialect_for(self._backend_route)
        # Credential values resolved for the external route; held only to redact
        # error text and never serialized. Empty on the embedded route.
        self._secrets: tuple[str, ...] = ()
        # Set once the external graph server has proven the procedures Cognee's
        # writes need (see _ensure_graph_capabilities). Never consulted when embedded.
        self._graph_capabilities_verified = False

        if self._backend_route.semantic:
            self._refuse_mock_embedding()

        self._route = ProviderRoute(
            role="graph_engine",
            provider=self._backend_route.provider_name,
            model_id=self._backend_route.embedding_model if self._backend_route.semantic else None,
            endpoint=self._backend_route.graph_endpoint,
            graph_only=not self._backend_route.semantic,
            model_inferred=False,
            active=self._available,
        )
        self._semantic_ready_verified = False
        self._cached_vector_engine: Any = None
        self._known_collections: set[str] = set()
        # Reranker state: the API key (secret-file only) and whether the endpoint
        # has listed the configured model (see _ensure_rerank_ready).
        self._rerank_api_key: str | None = None
        self._rerank_ready_verified = False
        self._configure_env()
        if not self._backend_route.embedded:
            self._configure_external_backends()
        if self._backend_route.reranking and config.reranking_api_key_file is not None:
            # After _configure_external_backends, which (re)sets self._secrets.
            api_key = read_secret_ref(config.reranking_api_key_file, purpose="reranking api key")
            self._rerank_api_key = api_key
            self._secrets = (*self._secrets, api_key)

    def _configure_external_backends(self) -> None:
        """Wire a non-embedded route into Cognee via its supported ``config.set_*_db_config``
        setters. Every precondition failure raises; there is no fallback to the embedded
        route. Error messages never carry credential values.
        """
        route = self._backend_route
        if route.graph_engine == "neo4j":
            scheme = self.config.neo4j_uri.split("://", 1)[0].lower() if "://" in self.config.neo4j_uri else ""
            if scheme not in NEO4J_URI_SCHEMES:
                raise BackendConfigError(
                    f"neo4j_uri must use one of {sorted(NEO4J_URI_SCHEMES)}; got endpoint {route.graph_endpoint!r}"
                )

        missing = missing_backend_modules(route)
        if missing:
            raise EngineUnavailableError(
                "engine_unavailable: backend route "
                f"graph_engine={route.graph_engine} vector_store={route.vector_store} "
                f"requires python modules not installed: {', '.join(missing)}"
            )

        # Resolves secret-file references; raises SecretRefError / BackendConfigError.
        settings: CogneeBackendSettings = build_cognee_backend_settings(self.config, route)
        self._secrets = settings.secrets

        if not self._available:
            # Explicitly marked unavailable: configuration is validated above but no
            # Cognee state is touched.
            return
        if not COGNEE_AVAILABLE:
            raise EngineUnavailableError(
                "engine_unavailable: cognee package required to configure "
                f"graph_engine={route.graph_engine} vector_store={route.vector_store}"
            )

        binding_changed = False
        if route.semantic:
            global _LAST_SEMANTIC_BINDING
            semantic_binding = (
                tuple(sorted(settings.embedding.items())) if settings.embedding else (),
                tuple(sorted(settings.vector.items())) if settings.vector else (),
                tuple(sorted(settings.relational.items())) if settings.relational else (),
            )
            if _LAST_SEMANTIC_BINDING != semantic_binding:
                # Evicts the engine created under the previous binding (keyed by the
                # config still applied at this point) and drops the config caches the
                # setters below repopulate.
                clear_semantic_engine_caches("semantic binding change (before config setters)")
                _LAST_SEMANTIC_BINDING = semantic_binding
                self._cached_vector_engine = None
                self._semantic_ready_verified = False
                binding_changed = True

        cognee_config = getattr(cognee, "config", None)
        setters = (
            ("set_graph_db_config", settings.graph),
            ("set_relational_db_config", settings.relational),
            ("set_vector_db_config", settings.vector),
            ("set_embedding_config", settings.embedding),
        )
        for setter_name, payload in setters:
            if payload is None:
                continue
            setter = getattr(cognee_config, setter_name, None)
            if not callable(setter):
                raise EngineUnavailableError(
                    f"engine_unavailable: pinned cognee does not expose config.{setter_name}; "
                    f"cannot configure graph_engine={route.graph_engine} vector_store={route.vector_store}"
                )
            try:
                setter(dict(payload))
            except Exception as exc:  # AttributeError for keys the pinned Cognee lacks
                raise EngineUnavailableError(
                    f"engine_unavailable: cognee config.{setter_name} rejected settings for "
                    f"graph_engine={route.graph_engine} vector_store={route.vector_store}: "
                    + redact(f"{type(exc).__name__}: {exc}", settings.secrets)
                ) from None
            self._verify_config_readback(setter_name, payload)

        if route.semantic and binding_changed:
            # The vector-engine cache is keyed by the vector config alone and bakes
            # the embedding engine in at creation. An entry created under an earlier
            # embedding binding for the same pgvector connection would therefore
            # survive the pre-setter eviction (different key then) and keep its old
            # embedding engine, so the entry for the newly applied config is evicted
            # as well; the engine factories are cleared for the same reason.
            evict_bound_vector_engine("semantic binding change (after config setters)")
            for cache_fn in (create_embedding_engine, create_relational_engine):
                clear_fn = getattr(cache_fn, "cache_clear", None)
                if callable(clear_fn):
                    clear_fn()

    def _verify_config_readback(self, setter_name: str, payload: Mapping[str, Any]) -> None:
        """Read the applied settings back through Cognee's own getter and compare.

        A setter that silently ignores a key would leave Cognee on a different
        provider or endpoint than the route claims, so any mismatch is fatal. Error
        text names keys only; values may be credentials.
        """
        route = self._backend_route
        getter = CONFIG_READBACK_GETTERS.get(setter_name)
        if not callable(getter):
            raise EngineUnavailableError(
                f"engine_unavailable: pinned cognee exposes no readback getter for config.{setter_name}; "
                f"cannot verify graph_engine={route.graph_engine} vector_store={route.vector_store}"
            )
        try:
            applied = getter()
        except Exception as exc:
            raise EngineUnavailableError(
                f"engine_unavailable: cognee config readback for config.{setter_name} failed: "
                + redact(f"{type(exc).__name__}: {exc}", self._secrets)
            ) from None
        missing = object()
        mismatched = sorted(key for key, value in payload.items() if getattr(applied, key, missing) != value)
        if mismatched:
            raise EngineUnavailableError(
                f"engine_unavailable: cognee config.{setter_name} did not apply "
                f"graph_engine={route.graph_engine} vector_store={route.vector_store} settings; "
                f"readback mismatch for keys: {', '.join(mismatched)}"
            )

    def _unavailable(self, context: str, detail: BaseException | str) -> EngineUnavailableError:
        route = self._backend_route
        text = detail if isinstance(detail, str) else f"{type(detail).__name__}: {detail}"
        return EngineUnavailableError(
            f"engine_unavailable: graph_engine={route.graph_engine} vector_store={route.vector_store} "
            f"backend unreachable during {context}: " + redact(text, self._secrets)
        )

    def _redacted(self, exc: BaseException) -> str:
        return redact(f"{type(exc).__name__}: {exc}", self._secrets)

    @staticmethod
    def _refuse_mock_embedding() -> None:
        """Cognee's embedding engines honour ``MOCK_EMBEDDING`` by returning zero
        vectors; a semantic route must never index or search with those."""
        if os.environ.get("MOCK_EMBEDDING"):
            raise SemanticBindingError("engine_unavailable: MOCK_EMBEDDING is refused in semantic mode")

    async def _backend_call(self, context: str, awaitable: Awaitable[T]) -> T:
        """Await a Cognee call, mapping external connection failures to unavailability.

        On the embedded route the awaitable is returned untouched. On an external
        route only driver connectivity / authentication failures (see
        ``is_backend_connection_failure``) become ``EngineUnavailableError`` (the
        server maps that to 503); every other exception propagates unchanged, and
        the original exception stays attached as the cause.
        """
        if self._backend_route.embedded:
            return await awaitable
        try:
            return await awaitable
        except EngineUnavailableError:
            raise
        except Exception as exc:
            if not is_backend_connection_failure(exc):
                raise
            raise self._unavailable(context, exc) from exc

    async def _graph_engine(self, context: str) -> Any:
        return await self._backend_call(f"{context} (get_graph_engine)", get_graph_engine())

    async def _ensure_graph_capabilities(self, graph_engine: Any, context: str) -> None:
        """Fail closed before the first write if the external graph server lacks the
        procedures Cognee's writes call (``REQUIRED_NEO4J_PROCEDURES``).

        Uses the supported ``graph_engine.query`` API with dialect-owned Cypher. The
        embedded route returns no probe and is never checked. The result is cached
        per adapter after a successful probe; failures are never cached.
        """
        if self._graph_capabilities_verified:
            return
        probe = self._dialect.capability_query(REQUIRED_NEO4J_PROCEDURES)
        if probe is None:
            self._graph_capabilities_verified = True
            return
        query_str, params = probe
        route = self._backend_route
        try:
            rows = await self._backend_call(f"{context} (capability probe)", graph_engine.query(query_str, params))
        except EngineUnavailableError:
            raise
        except Exception as exc:
            raise EngineUnavailableError(
                f"engine_unavailable: graph_engine={route.graph_engine} capability probe failed: "
                + self._redacted(exc)
            ) from exc
        present = {self._dialect.capability_row(row) for row in rows or ()}
        missing = [name for name in REQUIRED_NEO4J_PROCEDURES if name not in present]
        if missing:
            raise EngineUnavailableError(
                f"engine_unavailable: graph_engine={route.graph_engine} at {route.graph_endpoint} "
                f"lacks required procedures (install the APOC core plugin): {', '.join(missing)}"
            )
        self._graph_capabilities_verified = True

    async def _node_edges(self, graph_engine: Any, node_id: UUID, context: str) -> list[EdgeRecord]:
        query_str, params = self._dialect.edges_query(node_id)
        rows = await self._backend_call(context, graph_engine.query(query_str, params))
        return [self._dialect.edge_row(row) for row in rows or ()]

    async def _incident_edge_keys(self, graph_engine: Any, node_ids: list[str], context: str) -> set[EdgeKey]:
        """Identity of every edge touching any of ``node_ids``, read through the
        supported ``graph_engine.query`` API in dialect-owned batches."""
        keys: set[EdgeKey] = set()
        for start in range(0, len(node_ids), INCIDENT_EDGE_QUERY_CHUNK):
            chunk = node_ids[start : start + INCIDENT_EDGE_QUERY_CHUNK]
            query_str, params = self._dialect.incident_edges_query(chunk)
            rows = await self._backend_call(context, graph_engine.query(query_str, params))
            keys.update(self._dialect.edge_key_row(row) for row in rows or ())
        return keys

    def _configure_env(self) -> None:
        cognee_dir = self.config.cognee_dir
        (cognee_dir / "system").mkdir(parents=True, exist_ok=True)
        (cognee_dir / "data").mkdir(parents=True, exist_ok=True)
        (cognee_dir / "cache").mkdir(parents=True, exist_ok=True)
        (cognee_dir / "logs").mkdir(parents=True, exist_ok=True)
        os.environ["SYSTEM_ROOT_DIRECTORY"] = str(cognee_dir / "system")
        os.environ["DATA_ROOT_DIRECTORY"] = str(cognee_dir / "data")
        os.environ["CACHE_ROOT_DIRECTORY"] = str(cognee_dir / "cache")
        os.environ["COGNEE_LOGS_DIR"] = str(cognee_dir / "logs")
        if self._available and COGNEE_AVAILABLE:
            for cache_fn in (get_base_config, get_graph_config):
                clear_fn = getattr(cache_fn, "cache_clear", None)
                if callable(clear_fn):
                    clear_fn()

    async def _semantic_call(self, context: str, awaitable: Awaitable[T], *, timeout: float | None = None) -> T:
        """Await a Cognee vector / embedding call under ``embedding_timeout_seconds``
        (or the explicit ``timeout``; the reranker passes ``reranking_timeout_seconds``).

        Inference and database outages (``is_embedding_failure`` /
        ``is_backend_connection_failure``) and a deadline expiry become
        ``EngineUnavailableError`` in the same redacted ``backend unreachable
        during <context>`` form the graph side uses. ``SemanticBindingError`` and
        every other exception (programming, schema and data errors included)
        propagate unchanged; only what leaves here as ``EngineUnavailableError`` is
        ever degraded by the optional embedding policy.
        """
        if timeout is None:
            timeout = self.config.embedding_timeout_seconds
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await awaitable
        except EngineUnavailableError:
            raise
        except Exception as exc:
            if deadline.expired():
                raise self._unavailable(context, f"timed out after {timeout}s") from exc
            if is_embedding_failure(exc) or is_backend_connection_failure(exc):
                raise self._unavailable(context, exc) from exc
            raise

    # ------------------------------------------------------------------
    # Reranking stage (semantic route, reranking_provider != "none")
    # ------------------------------------------------------------------

    async def _rerank_call(self, context: str, awaitable: Awaitable[T]) -> T:
        """``_semantic_call`` under ``reranking_timeout_seconds``: httpx transport
        failures and a deadline expiry become ``EngineUnavailableError``
        (``backend unreachable during <context>``); everything else propagates."""
        return await self._semantic_call(context, awaitable, timeout=self.config.reranking_timeout_seconds)

    def _rerank_url(self, path: str) -> str:
        endpoint = self.config.reranking_endpoint or ""
        return endpoint.rstrip("/") + path

    def _rerank_headers(self) -> dict[str, str]:
        if self._rerank_api_key:
            return {"Authorization": f"Bearer {self._rerank_api_key}"}
        return {}

    def _rerank_json(self, response: httpx.Response, context: str) -> Any:
        """Classify the HTTP status, then decode the body. Outage statuses raise
        ``EngineUnavailableError`` (policy applies); any other non-2xx status and a
        non-JSON body raise ``RerankContractError`` (never degraded). The adapter
        never calls ``raise_for_status`` so ``HTTPStatusError`` cannot masquerade
        as a transport outage."""
        status = classify_rerank_status(response.status_code)
        if status == "outage":
            raise self._unavailable(context, f"HTTP {response.status_code}")
        if status == "contract":
            raise RerankContractError(
                f"engine_error: reranker at {self._backend_route.reranking_endpoint} returned "
                f"HTTP {response.status_code} during {context}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RerankContractError(
                f"engine_error: reranker at {self._backend_route.reranking_endpoint} returned a non-JSON body during {context}"
            ) from exc

    async def _ensure_rerank_ready(self, context: str = "query (rerank readiness)") -> None:
        """Before the first ``/v1/rerank`` call, require ``GET /v1/models`` on the
        reranking endpoint to list the configured ``reranking_model``.

        A listing that omits the model raises ``RerankBindingError`` (never
        degraded): the endpoint may be an embedding server or serve a different
        model, and a rerank answer from it would carry the wrong receipt. Only a
        successful check is cached per adapter; failures are never cached.
        """
        if self._rerank_ready_verified:
            return
        route = self._backend_route
        expected = self.config.reranking_model
        call_context = f"{context} (GET /v1/models)"
        async with rerank_http_client(self.config.reranking_timeout_seconds) as client:
            response = await self._rerank_call(
                call_context, client.get(self._rerank_url("/v1/models"), headers=self._rerank_headers())
            )
        payload = self._rerank_json(response, call_context)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RerankContractError(
                f"engine_error: reranker at {route.reranking_endpoint} returned no 'data' list from /v1/models"
            )
        served = sorted({str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id") is not None})
        if expected not in served:
            raise RerankBindingError(
                f"engine_unavailable: reranking_model {expected!r} is not served by {route.reranking_endpoint} "
                f"(/v1/models lists: {', '.join(served) or 'nothing'})"
            )
        self._rerank_ready_verified = True

    async def _apply_rerank(
        self,
        query_text: str,
        matching: list[FactRecord],
        scores_list: list[float | None],
    ) -> tuple[list[FactRecord], list[float | None], RerankReport]:
        """Permute ``matching`` (and the aligned ``scores_list``) by the reranker's
        ``relevance_score`` and return the receipt.

        Runs only after scope filtering, withdrawn filtering, hydration and the
        limit cut, and only reorders: the returned list holds the same
        ``FactRecord`` objects, so candidate identity and ``total_matched`` cannot
        change. Documents are ``rerank_documents`` (the indexed ``semantic_text``),
        the query is the raw ``query_text``, and no ``top_n`` is sent so every
        index must come back. New order is ``(-score, pre-rank index)``.

        Outages (``EngineUnavailableError`` from ``_rerank_call`` / outage HTTP
        statuses) follow ``reranking_required``: optional -> ``fallback`` with
        the pre-rerank order and a redacted reason; required -> raise.
        ``RerankBindingError`` / ``RerankContractError`` and any other exception
        propagate unchanged under both policies.
        """
        route = self._backend_route
        pre_order = tuple(str(fact.node_id) for fact in matching)
        base: dict[str, Any] = {
            "provider": route.reranking_provider,
            "model_id": route.reranking_model,
            "endpoint": route.reranking_endpoint,
            "pair_format": RERANK_PAIR_FORMAT,
            "document_count": len(matching),
            "pre_rerank_order": pre_order,
        }
        if len(matching) < 2:
            report = RerankReport(
                status="not_attempted",
                request_sha256=None,
                post_rerank_order=pre_order,
                scores=(),
                reason="fewer than two candidates",
                **base,
            )
            return matching, scores_list, report

        documents = rerank_documents(query_text, matching)
        request_sha = rerank_request_sha256(query_text, documents)
        post_context = "query (rerank POST /v1/rerank)"
        try:
            await self._ensure_rerank_ready("query (rerank readiness)")
            body = {"model": route.reranking_model, "query": query_text, "documents": list(documents)}
            async with rerank_http_client(self.config.reranking_timeout_seconds) as client:
                response = await self._rerank_call(
                    post_context,
                    client.post(self._rerank_url("/v1/rerank"), json=body, headers=self._rerank_headers()),
                )
            payload = self._rerank_json(response, post_context)
            scores = parse_rerank_response(payload, expected_count=len(documents), expected_model=route.reranking_model)
        except (RerankBindingError, RerankContractError):
            raise
        except EngineUnavailableError as exc:
            if self.config.reranking_required:
                raise
            report = RerankReport(
                status="fallback",
                request_sha256=request_sha,
                post_rerank_order=pre_order,
                scores=(),
                reason=redact(str(exc), self._secrets),
                **base,
            )
            return matching, scores_list, report

        order = sorted(range(len(matching)), key=lambda i: (-scores[i], i))
        new_matching = [matching[i] for i in order]
        # ``scores_list`` is aligned with ``matching`` on the semantic modes and
        # empty on exact_fallback; only an aligned list is permuted.
        new_scores = [scores_list[i] for i in order] if len(scores_list) == len(matching) else list(scores_list)
        report = RerankReport(
            status="applied",
            request_sha256=request_sha,
            post_rerank_order=tuple(str(fact.node_id) for fact in new_matching),
            scores=tuple(scores[i] for i in order),
            **base,
        )
        return new_matching, new_scores, report

    @staticmethod
    def _reflected_vector_dim(table: Any) -> int | None:
        """``Vector(dim)`` of a reflected collection's ``vector`` column, if declared."""
        column = getattr(getattr(table, "c", None), "vector", None)
        dim = getattr(getattr(column, "type", None), "dim", None)
        return int(dim) if isinstance(dim, int) else None

    async def _ensure_semantic_ready(self, context: str = "semantic readiness") -> Any:
        """Verify the semantic binding before the first vector write or search.

        Checks, in order, through Cognee's supported surface: ``MOCK_EMBEDDING``
        unset; ``vector_engine.embedding_engine`` not mock and reporting
        ``get_vector_size() == config.vector_dimension``; a canary ``embed_data``
        call returning one non-zero vector of that dimension; and every existing
        ``<Model>_description`` collection reflecting ``Vector(vector_dimension)``.
        Any mismatch raises ``SemanticBindingError`` (never degraded); outages
        surface as ``EngineUnavailableError`` via ``_semantic_call``. A verified
        engine is cached per adapter; failures are never cached.
        """
        if self._semantic_ready_verified and self._cached_vector_engine is not None:
            return self._cached_vector_engine

        self._refuse_mock_embedding()
        expected_dim = self.config.vector_dimension

        vector_engine = await self._semantic_call(f"{context} (get_vector_engine_async)", get_vector_engine_async())
        embedding_engine = getattr(vector_engine, "embedding_engine", None)
        if embedding_engine is None:
            raise SemanticBindingError(
                "engine_unavailable: vector engine exposes no embedding_engine; cannot verify the semantic binding"
            )
        if getattr(embedding_engine, "mock", False):
            raise SemanticBindingError("engine_unavailable: mock vector engine refused in semantic mode")
        reported_dim = embedding_engine.get_vector_size()
        if reported_dim != expected_dim:
            raise SemanticBindingError(
                f"engine_unavailable: vector dimension mismatch: engine reports {reported_dim} != expected {expected_dim}"
            )

        canary = await self._semantic_call(f"{context} (canary embedding)", vector_engine.embed_data(["canary"]))
        canary_vec = canary[0] if canary else None
        if not canary_vec:
            raise SemanticBindingError("engine_unavailable: canary embedding returned an empty vector")
        if len(canary_vec) != expected_dim:
            raise SemanticBindingError(
                f"engine_unavailable: canary embedding returned dimension {len(canary_vec)} != expected {expected_dim}"
            )
        if all(v == 0.0 for v in canary_vec):
            raise SemanticBindingError("engine_unavailable: canary embedding vector is all zeros")

        for col in SEMANTIC_COLLECTIONS:
            if not await self._semantic_call(f"{context} (has_collection {col})", vector_engine.has_collection(col)):
                continue
            table = await self._semantic_call(f"{context} (get_table {col})", vector_engine.get_table(col))
            dim = self._reflected_vector_dim(table)
            if dim is not None and dim != expected_dim:
                raise SemanticBindingError(
                    f"engine_unavailable: column dimension mismatch in table {col}: {dim} != expected {expected_dim}"
                )
            self._known_collections.add(col)

        self._cached_vector_engine = vector_engine
        self._semantic_ready_verified = True
        return vector_engine

    async def _index_semantic_points(self, fact_points: list[DataPoint], context: str) -> SemanticIndexReport:
        """Delete then re-index this snapshot's vector rows for ``fact_points``.

        Callers invoke this only on the semantic route. Only an outage
        (``EngineUnavailableError`` from ``_semantic_call``) follows the configured
        policy: optional -> best-effort cleanup and ``skipped`` with a redacted
        reason; required -> the error propagates. A binding mismatch raises
        regardless of policy, and any other exception (programming, schema or
        data error from a reachable backend) propagates unchanged.
        """
        if not fact_points:
            return SemanticIndexReport(
                status="indexed",
                rows_requested=0,
                collections=(),
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
            )

        by_col: dict[str, list[UUID]] = {}
        for dp in fact_points:
            kind = getattr(dp, "kind", None) or type(dp).__name__
            col = semantic_collection(kind)
            uid = dp.id if isinstance(dp.id, UUID) else UUID(str(dp.id))
            by_col.setdefault(col, []).append(uid)

        try:
            vector_engine = await self._ensure_semantic_ready(context)
            for col, uids in by_col.items():
                if await self._semantic_call(f"{context} (has_collection {col})", vector_engine.has_collection(col)):
                    await self._semantic_call(f"{context} (delete_data_points {col})", vector_engine.delete_data_points(col, uids))
            await self._semantic_call(f"{context} (index_data_points)", index_data_points(fact_points, vector_engine=vector_engine))
            for col in by_col:
                self._known_collections.add(col)
            return SemanticIndexReport(
                status="indexed",
                rows_requested=len(fact_points),
                collections=tuple(sorted(by_col.keys())),
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
            )
        except SemanticBindingError:
            raise
        except EngineUnavailableError as exc:
            if self.config.embedding_required:
                raise
            reason = redact(str(exc), self._secrets)
            # A readiness outage wrote nothing; an outage inside index_data_points
            # may have left rows for some of the points. Remove those best-effort
            # and say so when even that fails, rather than hiding it.
            if self._cached_vector_engine is not None:
                try:
                    for col, uids in by_col.items():
                        if await self._cached_vector_engine.has_collection(col):
                            await self._cached_vector_engine.delete_data_points(col, uids)
                except Exception as cleanup_exc:
                    reason += f" (stale-row cleanup failed: {self._redacted(cleanup_exc)})"
            return SemanticIndexReport(
                status="skipped",
                rows_requested=len(fact_points),
                collections=(),
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
                reason=reason,
            )

    async def _delete_vector_rows(self, node_ids: list[str], context: str, progress: dict[str, int]) -> tuple[int, int]:
        """Delete every vector row whose id is in ``node_ids`` and count by readback.

        For each collection the rows are ``retrieve``d before ``delete_data_points``
        and again afterwards; ``progress["deleted"]`` accumulates the verified
        difference as it goes so a caller degrading on an outage can still report
        what had already been removed. Returns ``(deleted, remaining)`` where
        ``remaining`` counts rows that still read back after the delete.
        """
        vector_engine = await self._ensure_semantic_ready(context)
        remaining_total = 0
        for col in SEMANTIC_COLLECTIONS:
            if not await self._semantic_call(f"{context} (has_collection {col})", vector_engine.has_collection(col)):
                continue
            existing = await self._semantic_call(f"{context} (retrieve {col})", vector_engine.retrieve(col, node_ids))
            if not existing:
                continue
            del_ids = [UUID(str(row.id)) for row in existing]
            await self._semantic_call(f"{context} (delete_data_points {col})", vector_engine.delete_data_points(col, del_ids))
            remaining = await self._semantic_call(
                f"{context} (retrieve after delete {col})",
                vector_engine.retrieve(col, [str(uid) for uid in del_ids]),
            )
            remaining_count = len(remaining or ())
            progress["deleted"] += len(del_ids) - remaining_count
            remaining_total += remaining_count
        return progress["deleted"], remaining_total

    def _ensure_available(self) -> None:
        if not self._available:
            raise EngineUnavailableError(
                "engine_unavailable: cognee and ladybug packages must be installed"
            )

    def _build_snapshot_graph(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_ref: SnapshotRef,
        facts: list[dict[str, Any]],
    ) -> tuple[list[DataPoint], list[str], list[tuple], list[tuple[str, str, str]], str]:
        """Deterministic graph projection shared by plan_snapshot and ingest_snapshot.
        Returns (data_points, node_ids, edges, edge_keys, graph_sha256) without touching
        the engine, so the preflight hash is by construction the hash the write produces.
        """
        self._ensure_available()

        snapshot_id = snapshot_ref.snapshot_id
        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        repo_name = f"repo-{repository_id}"

        # 1. Primary repository node. The pinned Cognee CodeRepository model has no
        # ``repo`` field (unknown fields are ignored), so the anchor is not matched
        # by the ``repo`` scope predicate. Its id is deterministic from the scope
        # (snapshot_repo_node_id == SnapshotScope.repo_node_id) and the dialect
        # retire queries select it by that id, so retire removes it together with
        # its snapshot. It carries no ``enola_id`` and is never surfaced as a fact.
        repo_node = CodeRepository(
            id=snapshot_repo_node_id(snapshot_label, repo_name),
            name=f"{repo_name}@{snapshot_id}",
            path=str(repository_id),
        )

        data_points: list[DataPoint] = [repo_node]
        node_ids: list[str] = [str(repo_node.id)]
        fact_id_to_node_id: dict[str, UUID] = {}

        # 2. Map facts to DataPoints preserving full fact.id
        for fact in facts:
            kind = fact.get("kind")
            name = fact.get("name")
            fact_id = fact.get("id")

            if not kind or not name or not fact_id:
                continue

            model_cls = _get_model_class(kind)
            if model_cls is None:
                continue

            node_id = snapshot_fact_node_id(snapshot_label, fact_id)
            fact_id_to_node_id[fact_id] = node_id
            node_ids.append(str(node_id))

            props = fact.get("props") or {}
            file_path = fact.get("file")
            line = fact.get("line")
            end_line = fact.get("end_line")

            fields: dict[str, Any] = {
                "id": node_id,
                "name": name,
                "kind": kind,
                "file_path": file_path if isinstance(file_path, str) else None,
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "end_line": end_line if isinstance(end_line, int) and not isinstance(end_line, bool) else None,
                "repo": snapshot_label,
                "enola_id": fact_id,
                "fact_properties": props,
                "part_of": repo_node,
            }
            if model_cls is CodeSymbol and "symbol_kind" in props:
                fields["symbol_kind"] = props.get("symbol_kind")

            if self._backend_route.semantic:
                desc = semantic_text(kind=kind, name=name, file_path=fields["file_path"], props=props)
                tag = self._backend_route.binding_tag
                fields["description"] = desc
                fields["metadata"] = {"index_fields": ["description"]}
                fields["belongs_to_set"] = [snapshot_label, tag] if tag else [snapshot_label]

            try:
                data_points.append(model_cls(**fields))
            except ValidationError:
                continue

        # 3. Build edges resolving original full fact IDs
        edges: list[tuple] = []
        edge_keys: list[tuple[str, str, str]] = []

        for fact in facts:
            fact_id = fact.get("id")
            if not fact_id or fact_id not in fact_id_to_node_id:
                continue

            source_id = fact_id_to_node_id[fact_id]
            for relation in fact.get("relations") or []:
                rel_kind = relation.get("kind") or relation.get("relationship") or "related_to"
                target_id = relation.get("target_id")

                target_node_id: UUID | None = None
                if target_id and target_id in fact_id_to_node_id:
                    target_node_id = fact_id_to_node_id[target_id]
                else:
                    # Disambiguated target resolution by name within the same snapshot.
                    # Binds ONLY if the target name is unique across files; leaves unresolved if ambiguous.
                    target_name = relation.get("target")
                    if target_name:
                        matching_fact_ids = [
                            other_fact["id"]
                            for other_fact in facts
                            if other_fact.get("name") == target_name and other_fact.get("id") in fact_id_to_node_id
                        ]
                        if len(matching_fact_ids) == 1:
                            target_node_id = fact_id_to_node_id[matching_fact_ids[0]]
                        else:
                            target_node_id = None

                if target_node_id is not None:
                    edge_tuple = (
                        source_id,
                        target_node_id,
                        rel_kind,
                        {
                            "source_node_id": str(source_id),
                            "target_node_id": str(target_node_id),
                            "relationship_name": rel_kind,
                            "snapshot_label": snapshot_label,
                            "snapshot_id": snapshot_id,
                            "repository_id": str(repository_id),
                            "workspace_id": str(workspace_id),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    edges.append(edge_tuple)
                    edge_keys.append((str(source_id), str(target_node_id), rel_kind))

        # Compute graph state hash via canonical manifest helper
        graph_hash = compute_graph_sha256(node_ids, edge_keys)
        return data_points, node_ids, edges, edge_keys, graph_hash

    async def plan_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_ref: SnapshotRef,
        facts: list[dict[str, Any]],
        receipt: dict[str, Any] | None,
        insights: list[dict[str, Any]],
    ) -> IngestPlan:
        _, node_ids, _, edge_keys, graph_hash = self._build_snapshot_graph(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_ref=snapshot_ref,
            facts=facts,
        )
        return IngestPlan(
            snapshot_id=snapshot_ref.snapshot_id,
            node_ids=tuple(node_ids),
            edge_keys=tuple(edge_keys),
            graph_sha256=graph_hash,
        )

    async def ingest_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_ref: SnapshotRef,
        facts: list[dict[str, Any]],
        receipt: dict[str, Any] | None,
        insights: list[dict[str, Any]],
    ) -> IngestResult:
        data_points, node_ids, edges, edge_keys, graph_hash = self._build_snapshot_graph(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_ref=snapshot_ref,
            facts=facts,
        )

        graph_engine = await self._graph_engine("ingest_snapshot")
        await self._ensure_graph_capabilities(graph_engine, "ingest_snapshot")

        # Write nodes using real Cognee add_data_points seam (graph_only=True: no GPU/LLM)
        await self._backend_call("ingest_snapshot (add_data_points)", add_data_points(data_points, graph_only=True))

        if edges:
            await self._backend_call("ingest_snapshot (add_edges)", graph_engine.add_edges(edges))

        semantic_report: SemanticIndexReport | None = None
        if self._backend_route.semantic:
            # data_points[0] is the CodeRepository anchor (see _build_snapshot_graph);
            # it carries no fact and is never vector-indexed.
            semantic_report = await self._index_semantic_points(data_points[1:], "ingest_snapshot")

        return IngestResult(
            snapshot_id=snapshot_ref.snapshot_id,
            nodes_written=len(data_points),
            edges_written=len(edges),
            node_ids=tuple(node_ids),
            edge_keys=tuple(edge_keys),
            graph_sha256=graph_hash,
            route=self._route,
            semantic=semantic_report,
        )

    async def ingest_records(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        records: list[FactRecord],
        source_ref: SourceRef,
    ) -> IngestResult:
        self._ensure_available()
        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        graph_engine = await self._graph_engine("ingest_records")
        await self._ensure_graph_capabilities(graph_engine, "ingest_records")

        node_ids: list[str] = []
        data_points: list[DataPoint] = []
        for rec in records:
            model_cls = _get_model_class(rec.kind) or CodeSymbol
            fields: dict[str, Any] = {
                "id": rec.node_id,
                "name": rec.name,
                "kind": rec.kind,
                "file_path": rec.file_path,
                "line": rec.line,
                "end_line": rec.end_line,
                "repo": snapshot_label,
                "enola_id": rec.fact_id,
                "fact_properties": rec.properties,
            }
            if self._backend_route.semantic:
                desc = semantic_text(kind=rec.kind, name=rec.name, file_path=rec.file_path, props=rec.properties)
                tag = self._backend_route.binding_tag
                fields["description"] = desc
                fields["metadata"] = {"index_fields": ["description"]}
                fields["belongs_to_set"] = [snapshot_label, tag] if tag else [snapshot_label]
            dp = model_cls(**fields)
            data_points.append(dp)
            node_ids.append(str(rec.node_id))

        await self._backend_call("ingest_records (add_data_points)", add_data_points(data_points, graph_only=True))
        graph_hash = compute_graph_sha256(node_ids, ())

        semantic_report: SemanticIndexReport | None = None
        if self._backend_route.semantic:
            semantic_report = await self._index_semantic_points(data_points, "ingest_records")

        return IngestResult(
            snapshot_id=snapshot_id,
            nodes_written=len(data_points),
            edges_written=0,
            node_ids=tuple(node_ids),
            edge_keys=(),
            graph_sha256=graph_hash,
            route=self._route,
            semantic=semantic_report,
        )

    async def query(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        query_text: str,
        limit: int = 20,
    ) -> QueryResult:
        self._ensure_available()
        scope = SnapshotScope(workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id)

        graph_engine = await self._graph_engine("query")
        query_str, params = self._dialect.scope_query(scope)
        rows = await self._backend_call("query", graph_engine.query(query_str, params))

        valid_facts: dict[str, FactRecord] = {}
        for row in rows:
            node_id, name, props = self._dialect.node_row(row)
            if not props or not props.get("enola_id"):
                continue
            fact_props = props.get("fact_properties") or {}
            if not isinstance(fact_props, dict):
                fact_props = {}
            if _is_withdrawn(props) or _is_withdrawn(fact_props):
                continue
            nid_str = str(node_id)
            valid_facts[nid_str] = FactRecord(
                node_id=UUID(nid_str),
                fact_id=str(props.get("enola_id") or nid_str),
                name=name,
                kind=str(props.get("kind", "")),
                file_path=props.get("file_path"),
                line=props.get("line"),
                end_line=props.get("end_line"),
                properties=fact_props,
                snapshot_id=snapshot_id,
                repository_id=repository_id,
                workspace_id=workspace_id,
            )

        if not self._backend_route.semantic:
            matching = [fact for fact in valid_facts.values() if _exact_match(fact, query_text)][:limit]
            return QueryResult(
                facts=tuple(matching),
                total_matched=len(matching),
                query=query_text,
                snapshot_id=snapshot_id,
                route=self._route,
                retrieval=None,
            )

        if query_text == "*":
            matching = list(valid_facts.values())[:limit]
            retrieval = RetrievalReport(
                mode="exact",
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
                scores=(),
                collections_searched=(),
            )
            return QueryResult(
                facts=tuple(matching),
                total_matched=len(matching),
                query=query_text,
                snapshot_id=snapshot_id,
                route=self._route,
                retrieval=retrieval,
            )

        # Semantic ranking first, then the exact tail. Hydration comes only from the
        # scope rows above, so a vector hit whose graph node is missing or withdrawn
        # is dropped rather than surfaced.
        try:
            vector_engine = await self._ensure_semantic_ready("query")
            emb_res = await self._semantic_call("query (embed_data)", vector_engine.embed_data([query_text]))
            if not emb_res or not emb_res[0]:
                raise self._unavailable("query (embed_data)", "query embedding returned an empty vector")
            query_vec = emb_res[0]

            tag = self._backend_route.binding_tag
            node_filter = [scope.label, tag] if tag else [scope.label]
            searched_collections: list[str] = []
            scored_hits: list[tuple[float, str]] = []

            for col in SEMANTIC_COLLECTIONS:
                has_col = col in self._known_collections
                if not has_col:
                    has_col = await self._semantic_call(f"query (has_collection {col})", vector_engine.has_collection(col))
                    if has_col:
                        self._known_collections.add(col)
                if has_col:
                    searched_collections.append(col)
                    hits = await self._semantic_call(
                        f"query (search {col})",
                        vector_engine.search(
                            collection_name=col,
                            query_vector=query_vec,
                            limit=limit,
                            node_name=node_filter,
                            node_name_filter_operator="AND",
                        ),
                    )
                    for hit in hits or ():
                        hit_id = str(getattr(hit, "id", ""))
                        hit_score = float(getattr(hit, "score", 0.0))
                        scored_hits.append((hit_score, hit_id))

            # Lower cosine distance ranks first; the id tie-break keeps order stable.
            scored_hits.sort()

            matching = []
            scores_list: list[float | None] = []
            seen_ids: set[str] = set()

            for score, nid in scored_hits:
                if nid in valid_facts and nid not in seen_ids:
                    seen_ids.add(nid)
                    matching.append(valid_facts[nid])
                    scores_list.append(score)
                    if len(matching) >= limit:
                        break

            for nid, fact in valid_facts.items():
                if len(matching) >= limit:
                    break
                if nid not in seen_ids and _exact_match(fact, query_text):
                    seen_ids.add(nid)
                    matching.append(fact)
                    scores_list.append(None)

            retrieval = RetrievalReport(
                mode="semantic" if any(score is not None for score in scores_list) else "semantic_empty",
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
                scores=tuple(scores_list),
                collections_searched=tuple(searched_collections),
            )
        except SemanticBindingError:
            raise
        except EngineUnavailableError as exc:
            # Only an outage is degraded; any other error propagates unchanged.
            if self.config.embedding_required:
                raise

            matching = [fact for fact in valid_facts.values() if _exact_match(fact, query_text)][:limit]
            retrieval = RetrievalReport(
                mode="exact_fallback",
                model_id=self._backend_route.embedding_model,
                dimension=self._backend_route.vector_dimension,
                scores=(),
                collections_searched=(),
                reason=redact(str(exc), self._secrets),
            )

        if self._backend_route.reranking:
            # Permutation-only stage over the settled candidate list (semantic,
            # semantic_empty or exact_fallback); ``mode`` and identity are kept.
            matching, permuted_scores, rerank_report = await self._apply_rerank(
                query_text, list(matching), list(retrieval.scores)
            )
            retrieval = retrieval.model_copy(update={"scores": tuple(permuted_scores), "rerank": rerank_report})

        return QueryResult(
            facts=tuple(matching),
            total_matched=len(matching),
            query=query_text,
            snapshot_id=snapshot_id,
            route=self._route,
            retrieval=retrieval,
        )

    async def lookup(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        file_path: str | None = None,
    ) -> LookupResult:
        self._ensure_available()
        scope = SnapshotScope(workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id)
        expected_node_id = snapshot_fact_node_id(scope.label, fact_id)

        graph_engine = await self._graph_engine("lookup")
        query_str, params = self._dialect.lookup_query(scope, expected_node_id)
        rows = await self._backend_call("lookup", graph_engine.query(query_str, params))

        if not rows:
            return LookupResult(
                fact=None,
                found=False,
                snapshot_id=snapshot_id,
                route=self._route,
            )

        node_id, name, props = self._dialect.node_row(rows[0])
        fact_props = props.get("fact_properties") or {}
        if not isinstance(fact_props, dict):
            fact_props = {}
        if _is_withdrawn(props) or _is_withdrawn(fact_props):
            return LookupResult(
                fact=None,
                found=False,
                snapshot_id=snapshot_id,
                route=self._route,
            )

        if file_path and props.get("file_path") != file_path:
            return LookupResult(
                fact=None,
                found=False,
                snapshot_id=snapshot_id,
                route=self._route,
            )

        fact_record = FactRecord(
            node_id=UUID(str(node_id)),
            fact_id=fact_id,
            name=name,
            kind=str(props.get("kind", "")),
            file_path=props.get("file_path"),
            line=props.get("line"),
            end_line=props.get("end_line"),
            properties=props.get("fact_properties") or {},
            snapshot_id=snapshot_id,
            repository_id=repository_id,
            workspace_id=workspace_id,
        )

        return LookupResult(
            fact=fact_record,
            found=True,
            snapshot_id=snapshot_id,
            route=self._route,
        )

    def _rebuilt_point(
        self,
        *,
        node_id: UUID,
        fact_id: str,
        snapshot_label: str,
        kind: str,
        name: Any,
        stored: Mapping[str, Any],
        fact_props: dict[str, Any],
    ) -> Any:
        """The DataPoint ``correct`` writes: top-level node fields carried over from
        ``stored`` unchanged, ``fact_props`` as the fact properties mapping."""
        fields: dict[str, Any] = {
            "id": node_id,
            "name": name,
            "kind": kind,
            "file_path": stored.get("file_path"),
            "line": stored.get("line"),
            "end_line": stored.get("end_line"),
            "repo": snapshot_label,
            "enola_id": fact_id,
            "fact_properties": fact_props,
        }
        model_cls = _get_model_class(kind) or CodeSymbol
        # Mirror the ingest projection: a symbol's ``symbol_kind`` top-level field is
        # derived from its fact properties, so a corrected value is reflected there.
        if kind == "symbol" and "symbol_kind" in fact_props:
            fields["symbol_kind"] = fact_props.get("symbol_kind")

        if self._backend_route.semantic:
            desc = semantic_text(kind=kind, name=name, file_path=fields["file_path"], props=fact_props)
            tag = self._backend_route.binding_tag
            fields["description"] = desc
            fields["metadata"] = {"index_fields": ["description"]}
            fields["belongs_to_set"] = [snapshot_label, tag] if tag else [snapshot_label]

        return model_cls(**fields)

    async def _verify_rebuilt_node(
        self, graph_engine: Any, node_id: UUID, expected_keys: set[EdgeKey], context: str
    ) -> tuple[bool, list[EdgeKey]]:
        """Read the rebuilt node and its incident edges back: (node_present, lost edges)."""
        node_after = await self._backend_call(f"{context} (get_node)", graph_engine.get_node(str(node_id)))
        edges_after = await self._node_edges(graph_engine, node_id, f"{context} (edges)")
        restored_keys = {(source, target, name) for source, target, name, _ in edges_after}
        return isinstance(node_after, dict), sorted(expected_keys - restored_keys)

    async def _revert_correction(
        self,
        graph_engine: Any,
        *,
        node_id: UUID,
        original_point: Any,
        restored_edges: list[EdgeRecord],
        expected_keys: set[EdgeKey],
        fact_id: str,
        snapshot_id: str,
        phase: str,
        cause: BaseException,
    ) -> CorrectionFailedError:
        """Put the prior node and its edges back after a failed rebuild and describe
        the resulting graph state. Returns the error to raise; never raises itself
        so the caller's ``raise ... from cause`` keeps the original chain."""
        revert_error: str | None = None
        node_present = False
        missing: list[EdgeKey] = sorted(expected_keys)
        try:
            # Whatever half-state the failed phase left (corrected node, partial
            # edges) is removed first so the re-add is deterministic.
            await self._backend_call("correct (revert delete_nodes)", graph_engine.delete_nodes([str(node_id)]))
            await self._backend_call("correct (revert add_data_points)", add_data_points([original_point], graph_only=True))
            if restored_edges:
                await self._backend_call("correct (revert add_edges)", graph_engine.add_edges(restored_edges))
            node_present, missing = await self._verify_rebuilt_node(graph_engine, node_id, expected_keys, "correct (revert verify)")
        except Exception as revert_exc:  # noqa: BLE001 - reported verbatim (redacted), never swallowed
            revert_error = self._redacted(revert_exc)
        return CorrectionFailedError(
            fact_id=fact_id,
            snapshot_id=snapshot_id,
            phase=phase,
            reverted=node_present and not missing and revert_error is None,
            node_present=node_present,
            missing_edges=tuple(missing),
            expected_edges=len(expected_keys),
            cause_text=self._redacted(cause),
            revert_error=revert_error,
        )

    async def correct(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        properties_update: dict[str, Any],
    ) -> CorrectResult:
        """Apply ``properties_update`` to one fact's ``fact_properties``.

        The node is rebuilt in place (delete, re-add through ``add_data_points``)
        because Cognee has no partial node update. Top-level node fields (name,
        kind, file path, lines, scope label, Enola id) are carried over unchanged and
        the update touches only a copy of the stored ``fact_properties`` mapping, so
        node metadata is never nested into the fact properties. Every edge incident
        to the node (the ``part_of`` anchor edge and relation edges) is captured
        before the delete and restored through ``add_edges``.

        Failure handling after the delete is deterministic: the rebuilt node and its
        edges are read back, and if the re-add, the edge restore or that readback
        fails, the prior node and edges are re-added and read back again, then
        ``CorrectionFailedError`` is raised stating either that the prior state was
        restored (retry safe) or exactly which parts are missing. A failure of the
        delete itself changes nothing and propagates unchanged. ``success`` is never
        reported unless the corrected node and every captured edge read back.
        """
        self._ensure_available()
        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        node_id = snapshot_fact_node_id(snapshot_label, fact_id)

        graph_engine = await self._graph_engine("correct")
        await self._ensure_graph_capabilities(graph_engine, "correct")
        node = await self._backend_call("correct (get_node)", graph_engine.get_node(str(node_id)))
        if not isinstance(node, dict):
            return CorrectResult(
                snapshot_id=snapshot_id,
                corrected_fact_id=fact_id,
                success=False,
                route=self._route,
            )

        stored = self._dialect.node_properties(node)
        existing_fact_props = stored.get("fact_properties")
        original_props: dict[str, Any] = dict(existing_fact_props) if isinstance(existing_fact_props, dict) else {}
        fact_props: dict[str, Any] = dict(original_props)
        fact_props.update(properties_update)

        kind = str(stored.get("kind") or "symbol")
        name = stored.get("name") or node.get("name") or fact_id
        point_args = dict(node_id=node_id, fact_id=fact_id, snapshot_label=snapshot_label, kind=kind, name=name, stored=stored)
        updated_point = self._rebuilt_point(**point_args, fact_props=fact_props)
        # The exact prior node, used only if the rebuild has to be reverted.
        original_point = self._rebuilt_point(**point_args, fact_props=original_props)

        vector_engine: Any = None
        semantic_status: Literal["reindexed", "deleted", "failed"] | None = None
        semantic_reason: str | None = None
        if self._backend_route.semantic:
            try:
                vector_engine = await self._ensure_semantic_ready("correct")
            except SemanticBindingError:
                raise
            except EngineUnavailableError as exc:
                # Only an outage is degraded; any other error propagates unchanged.
                if self.config.embedding_required:
                    raise
                semantic_status = "failed"
                semantic_reason = redact(str(exc), self._secrets)

        # Capture every incident edge with its direction before the node disappears.
        edges_before = await self._node_edges(graph_engine, node_id, "correct (capture edges)")
        expected_keys: set[EdgeKey] = {(source, target, name_) for source, target, name_, _ in edges_before}
        restored_edges: list[EdgeRecord] = [(source, target, name_, dict(props)) for source, target, name_, props in edges_before]

        # Phase 1: delete. Nothing has changed if this raises, so it propagates as-is.
        await self._backend_call("correct (delete_nodes)", graph_engine.delete_nodes([str(node_id)]))

        # Phase 2: rebuild, restore edges, verify by readback; revert on any failure.
        phase = "add_data_points"
        try:
            await self._backend_call("correct (add_data_points)", add_data_points([updated_point], graph_only=True))
            if restored_edges:
                phase = "add_edges"
                await self._backend_call("correct (add_edges)", graph_engine.add_edges(restored_edges))
            phase = "verify"
            node_present, lost = await self._verify_rebuilt_node(graph_engine, node_id, expected_keys, "correct (verify)")
            if not node_present:
                raise EngineError(
                    f"engine_error: rebuilt node {node_id} for fact {fact_id} did not read back after add_data_points"
                )
            if lost:
                raise EngineError(
                    f"engine_error: {len(lost)} of {len(expected_keys)} edges did not read back after add_edges: "
                    + ", ".join(f"{source}-[{name_}]->{target}" for source, target, name_ in lost)
                )
        except Exception as exc:
            failure = await self._revert_correction(
                graph_engine,
                node_id=node_id,
                original_point=original_point,
                restored_edges=restored_edges,
                expected_keys=expected_keys,
                fact_id=fact_id,
                snapshot_id=snapshot_id,
                phase=phase,
                cause=exc,
            )
            raise failure from exc

        if self._backend_route.semantic and semantic_status != "failed":
            # The stale row is always deleted; it is re-embedded from the rebuilt
            # point (recomputed description and tags) unless the fact is withdrawn.
            # ``fact_props`` already carries ``properties_update`` merged in.
            withdrawn = _is_withdrawn(fact_props)
            try:
                col = semantic_collection(kind)
                if await self._semantic_call(f"correct (has_collection {col})", vector_engine.has_collection(col)):
                    await self._semantic_call(f"correct (delete_data_points {col})", vector_engine.delete_data_points(col, [node_id]))
                if withdrawn:
                    semantic_status = "deleted"
                else:
                    await self._semantic_call("correct (index_data_points)", index_data_points([updated_point], vector_engine=vector_engine))
                    semantic_status = "reindexed"
            except SemanticBindingError:
                raise
            except EngineUnavailableError as exc:
                # Only an outage is degraded; any other error propagates unchanged.
                if self.config.embedding_required:
                    raise
                semantic_status = "failed"
                semantic_reason = redact(str(exc), self._secrets)

        return CorrectResult(
            snapshot_id=snapshot_id,
            corrected_fact_id=fact_id,
            success=True,
            route=self._route,
            semantic_status=semantic_status,
            semantic_reason=semantic_reason,
        )

    async def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> RetireResult:
        """Retire a snapshot from the graph. Recomputes snapshot-specific node IDs
        and deletes only those nodes. Never calls _sweep_stale_code_graph or deletes
        other snapshots.

        Every reported count is a readback difference: the stale node set and the
        edges incident to it are read before ``delete_nodes`` and again afterwards,
        and vector rows are ``retrieve``d before and after ``delete_data_points``.
        Nodes or edges that survive the delete raise ``EngineError``. On the
        semantic route an outage under the optional policy yields
        ``vector_cleanup="degraded"`` with ``vector_rows_deleted=None`` and the
        graph nodes are still removed; under the required policy it raises before
        any graph node is touched, so the retire can be retried.
        """
        self._ensure_available()
        scope = SnapshotScope(workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id)

        graph_engine = await self._graph_engine("retire")
        query_str, params = self._dialect.retire_query(scope)
        rows = await self._backend_call("retire", graph_engine.query(query_str, params))

        stale_node_ids = sorted({self._dialect.id_row(row) for row in rows})
        vector_rows_deleted: int | None = None
        vector_cleanup: Literal["complete", "partial", "degraded", "not_checked"] | None = None
        vector_cleanup_reason: str | None = None

        if self._backend_route.semantic:
            if not stale_node_ids:
                # No graph node to derive row ids from, so nothing was looked up.
                vector_cleanup = "not_checked"
            else:
                # Vector rows go first so a failure here leaves the graph nodes (and
                # therefore the retire retry) intact under the required policy.
                progress = {"deleted": 0}
                try:
                    vector_rows_deleted, remaining = await self._delete_vector_rows(stale_node_ids, "retire", progress)
                    if remaining:
                        vector_cleanup = "partial"
                        vector_cleanup_reason = (
                            f"{remaining} vector row(s) still read back after delete_data_points; "
                            f"{vector_rows_deleted} verified deleted"
                        )
                        if self.config.embedding_required:
                            raise EngineError(
                                f"engine_error: retire of snapshot {snapshot_id} verified {vector_rows_deleted} vector "
                                f"row(s) deleted but {remaining} still read back; graph nodes left in place for a retry"
                            )
                    else:
                        vector_cleanup = "complete"
                except SemanticBindingError:
                    raise
                except EngineUnavailableError as exc:
                    # Only an outage is degraded; any other error propagates unchanged.
                    if self.config.embedding_required:
                        raise
                    vector_rows_deleted = None
                    vector_cleanup = "degraded"
                    vector_cleanup_reason = (
                        redact(str(exc), self._secrets)
                        + f"; vector rows not verified (rows verified deleted before the outage: {progress['deleted']})"
                    )

        nodes_deleted = 0
        edges_deleted = 0
        if stale_node_ids:
            edges_before = await self._incident_edge_keys(graph_engine, stale_node_ids, "retire (incident edges)")
            await self._backend_call("retire (delete_nodes)", graph_engine.delete_nodes(stale_node_ids))
            remaining_rows = await self._backend_call("retire (node readback)", graph_engine.query(query_str, params))
            remaining_ids = {self._dialect.id_row(row) for row in remaining_rows or ()} & set(stale_node_ids)
            edges_after = await self._incident_edge_keys(graph_engine, stale_node_ids, "retire (edge readback)")
            nodes_deleted = len(stale_node_ids) - len(remaining_ids)
            edges_deleted = len(edges_before - edges_after)
            if remaining_ids or edges_after:
                raise EngineError(
                    f"engine_error: retire of snapshot {snapshot_id} deleted {nodes_deleted} of {len(stale_node_ids)} "
                    f"nodes and {edges_deleted} of {len(edges_before)} incident edges; "
                    f"{len(remaining_ids)} node(s) and {len(edges_after)} edge(s) still read back; retry required"
                )

        return RetireResult(
            snapshot_id=snapshot_id,
            nodes_deleted=nodes_deleted,
            edges_deleted=edges_deleted,
            vector_rows_deleted=vector_rows_deleted,
            vector_cleanup=vector_cleanup,
            vector_cleanup_reason=vector_cleanup_reason,
            success=True,
        )

    async def status(self) -> EngineStatus:
        route = self._backend_route
        details: dict[str, Any] = {
            "cognee_available": self._available,
            **route.as_dict(),
            "vector_indexing": route.semantic,
        }
        if route.semantic:
            details["embedding"] = {
                "provider": route.embedding_provider,
                "model": route.embedding_model,
                "endpoint": route.embedding_endpoint,
                "dimension": route.vector_dimension,
            }
        if route.reranking:
            details["reranking"] = {
                "provider": route.reranking_provider,
                "model": route.reranking_model,
                "endpoint": route.reranking_endpoint,
                "required": self.config.reranking_required,
                "timeout_seconds": self.config.reranking_timeout_seconds,
            }
        return EngineStatus(
            available=self._available,
            engine_name="RealCogneeAdapter",
            version="1.5.4" if self._available else None,
            graph_engine="ladybug==0.19.0" if route.graph_engine == "ladybug" else route.graph_engine,
            active_route=self._route,
            details=details,
        )
