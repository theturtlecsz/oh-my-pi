"""Explicit storage-backend routing for the Cognee-backed knowledge engine.

This module is the single place that knows how a ``KnowledgeConfig`` maps onto
Cognee's supported storage providers:

* graph:      ``ladybug`` (embedded, default) or ``neo4j`` (external server)
* vector:     ``lancedb`` (embedded, default) or ``pgvector`` (PostgreSQL + pgvector)
* metadata:   Cognee's relational store, ``sqlite`` embedded or ``postgres`` when
              pgvector is selected (Cognee derives the pgvector connection from
              its relational configuration).

It deliberately contains no I/O against the stores themselves. Credentials are
resolved from secret-file references at engine construction time, handed to
Cognee's own configuration setters, and never appear in status output, route
descriptions, argv, or error messages produced here. A ``neo4j_uri`` that embeds
userinfo (``bolt://user:password@host``) is rejected outright at route
resolution: sanitizing the endpoint for reporting would still hand the embedded
credential to the driver and keep it in process memory / config dumps, so the only
supported credential path is ``neo4j_user`` plus ``neo4j_password_file``.

The identity scheme for graph content (workspace / repository / snapshot scope
and per-fact node ids) is backend independent so that a published snapshot's
``graph_sha256`` verifies identically regardless of which backend holds it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_OID, UUID, uuid5

from .protocol import RerankBindingError, RerankContractError

if TYPE_CHECKING:
    from omp_work.knowledge_contracts import FactRecord

    from ..config import KnowledgeConfig

GraphBackend = Literal["ladybug", "neo4j"]
VectorBackend = Literal["lancedb", "pgvector"]
MetadataBackend = Literal["sqlite", "postgres"]

GRAPH_BACKENDS: tuple[str, ...] = ("ladybug", "neo4j")
VECTOR_BACKENDS: tuple[str, ...] = ("lancedb", "pgvector")

# Identity scheme version for graph node ids. It is reported in status so that an
# operator can tell which scheme a backend's content was written under.
IDENTITY_SCHEME = "omp-fact/v1"

NEO4J_URI_SCHEMES: frozenset[str] = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
)

# The semantic route sends fact text (kind, name, file path, scalar properties)
# to the embedding endpoint, so only the OpenAI-compatible HTTP transport the
# pinned Cognee engine actually speaks is accepted. Credentials for that
# endpoint come from ``embedding_api_key_file`` only, never from the URI.
EMBEDDING_ENDPOINT_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Python modules the pinned Cognee (1.5.4) imports when an external provider is
# selected. Each entry is the exact module its adapter imports, so a missing
# package is reported here instead of surfacing as an ImportError from inside
# Cognee on first use:
#
# * ``neo4j``  -> ``cognee.infrastructure.databases.graph.neo4j_driver.adapter``
#                 imports ``neo4j`` (``AsyncGraphDatabase`` / ``AsyncSession``).
#                 Shipped by the ``cognee[neo4j]`` extra.
# * ``pgvector`` -> ``cognee.infrastructure.databases.vector.pgvector.PGVectorAdapter``
#                 imports ``pgvector.sqlalchemy`` (``Vector`` column type) and
#                 ``sqlalchemy.ext.asyncio`` (``create_async_engine``); the relational
#                 engine it shares is built on a ``postgresql+asyncpg://`` URL, so the
#                 ``asyncpg`` dialect driver must import as well. ``pgvector`` and
#                 ``asyncpg`` come from the ``cognee[postgres]`` extra; ``sqlalchemy``
#                 is a core Cognee dependency.
#
# The embedded route (ladybug + lancedb) requires nothing beyond the pinned
# ``cognee`` / ``ladybug`` / ``lancedb`` distributions and is never checked here.
REQUIRED_MODULES: dict[str, tuple[str, ...]] = {
    "neo4j": ("neo4j",),
    "pgvector": ("pgvector.sqlalchemy", "sqlalchemy.ext.asyncio", "asyncpg"),
}

# Server-side procedures the pinned Cognee Neo4j adapter calls on every write.
# ``add_nodes`` labels merged nodes through ``apoc.create.addLabels`` and
# ``add_edges`` merges relationships through ``apoc.merge.relationship``; a Neo4j
# server without the APOC core plugin fails only at the first write, so the
# adapter probes for these before touching the graph. Not needed on the embedded
# Ladybug route, which has no plugin model.
REQUIRED_NEO4J_PROCEDURES: tuple[str, ...] = (
    "apoc.create.addLabels",
    "apoc.merge.relationship",
)

# Exception classes (by top-level package and class name anywhere in the MRO) that
# the external drivers raise when the backend cannot be reached, refuses the
# credentials, or drops the connection. Anything not listed propagates unchanged:
# Cypher / SQL errors from a reachable backend are engine errors, not outages.
_CONNECTION_FAILURE_TYPES: dict[str, frozenset[str]] = {
    "neo4j": frozenset(
        {
            "DriverError",
            "ServiceUnavailable",
            "SessionExpired",
            "AuthError",
            "TokenExpired",
            "TransientError",
            "DatabaseUnavailable",
            "ConfigurationError",
        }
    ),
    "asyncpg": frozenset(
        {
            "InterfaceError",
            "ConnectionDoesNotExistError",
            "ConnectionFailureError",
            "ConnectionRejectionError",
            "ClientCannotConnectError",
            "CannotConnectNowError",
            "PostgresConnectionError",
            "InvalidPasswordError",
            "InvalidAuthorizationSpecificationError",
            "InvalidCatalogNameError",
            "TooManyConnectionsError",
        }
    ),
    "sqlalchemy": frozenset({"OperationalError", "InterfaceError", "DisconnectionError", "TimeoutError"}),
}


def is_backend_connection_failure(exc: BaseException) -> bool:
    """True when ``exc`` is a Neo4j / PostgreSQL connectivity or authentication failure.

    Matches the builtin socket-level errors (``ConnectionError``, ``TimeoutError``,
    ``socket.gaierror``) and the driver exception families listed in
    ``_CONNECTION_FAILURE_TYPES`` without importing the optional driver packages.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        return True
    for cls in type(exc).__mro__:
        package = (cls.__module__ or "").split(".", 1)[0]
        names = _CONNECTION_FAILURE_TYPES.get(package)
        if names and cls.__name__ in names:
            return True
    return False


# Exception classes (by package and class name anywhere in the MRO) that the
# pinned Cognee embedding engines and the transports underneath them raise when
# the inference endpoint is unreachable, times out, throttles, or refuses the
# credentials. Cognee 1.5.4's ``OpenAICompatibleEmbeddingEngine`` wraps every
# transport failure into ``EmbeddingException`` (``EmbeddingCredentialsError``
# for 401/403); the raw openai / httpx classes are listed for the LiteLLM engine.
_EMBEDDING_FAILURE_NAMES: frozenset[str] = frozenset(
    {
        "EmbeddingException",
        "EmbeddingCredentialsError",
        "OpenAIError",
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "APIError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "NetworkError",
        "HTTPError",
        "HTTPStatusError",
        "RequestError",
        "RemoteProtocolError",
        "ServiceUnavailableError",
    }
)
_EMBEDDING_FAILURE_PACKAGES: frozenset[str] = frozenset(
    {"cognee", "openai", "httpx", "httpcore", "litellm", "aiohttp", "anyio"}
)
# Cognee failures that describe the data, not the endpoint: an input that
# exceeds the model's context window even after splitting. It subclasses
# ``EmbeddingException`` but must never be degraded into an outage.
_EMBEDDING_DATA_FAILURE_NAMES: frozenset[str] = frozenset({"EmbeddingContextWindowTooSmallError"})


def is_embedding_failure(exc: BaseException) -> bool:
    """True only when ``exc`` is an inference-endpoint or transport outage.

    Socket-level failures (``ConnectionError``, ``TimeoutError``, name resolution)
    and the provider / transport exception families in
    ``_EMBEDDING_FAILURE_NAMES`` from the packages in
    ``_EMBEDDING_FAILURE_PACKAGES`` qualify. Everything else is not an outage and
    must propagate: over-length inputs, pydantic validation errors, SQL errors
    from a reachable pgvector, and plain programming errors.
    """
    mro = type(exc).__mro__
    if any(cls.__name__ in _EMBEDDING_DATA_FAILURE_NAMES for cls in mro):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, socket.herror)):
        return True
    for cls in mro:
        pkg = (cls.__module__ or "").split(".", 1)[0]
        if pkg in _EMBEDDING_FAILURE_PACKAGES and cls.__name__ in _EMBEDDING_FAILURE_NAMES:
            return True
    return False


class BackendConfigError(ValueError):
    """The requested backend cannot be wired from the supplied configuration."""


class SecretRefError(BackendConfigError):
    """A credential secret-file reference is missing, unreadable, empty, or exposed."""


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotScope:
    """Namespace identity of one snapshot inside one workspace and repository.

    ``label`` is the scoping key stored on every projected node (``repo`` field) and
    is the value all query / lookup / retire operations filter on, so no operation
    can touch content outside its own workspace, repository and snapshot.
    """

    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str

    @property
    def label(self) -> str:
        return f"{self.workspace_id}:{self.repository_id}@{self.snapshot_id}"

    @property
    def repo_name(self) -> str:
        return f"repo-{self.repository_id}"

    def fact_node_id(self, fact_id: str) -> UUID:
        """Deterministic node UUID preserving the full Enola fact id under this scope."""
        return uuid5(NAMESPACE_OID, f"omp-fact:{self.label}:{fact_id}")

    def repo_node_id(self) -> UUID:
        return uuid5(NAMESPACE_OID, f"omp-repo:{self.label}:{self.repo_name}")


# --------------------------------------------------------------------------------------
# Secret references
# --------------------------------------------------------------------------------------


def read_secret_ref(path: str | os.PathLike[str] | None, *, purpose: str) -> str:
    """Read a credential from a secret-file reference.

    Only the trailing line break is stripped so credentials with leading or
    interior whitespace survive intact. Files readable by ``others`` are refused.
    Error messages carry the path and purpose only, never file content.
    """
    if path is None or str(path) == "":
        raise SecretRefError(f"{purpose}: no secret file reference configured")
    p = Path(path).expanduser()
    try:
        st = p.stat()
    except FileNotFoundError:
        raise SecretRefError(f"{purpose}: secret file not found: {p}") from None
    except OSError as exc:
        raise SecretRefError(f"{purpose}: secret file inaccessible: {p} ({exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretRefError(f"{purpose}: secret file is not a regular file: {p}")
    if st.st_mode & stat.S_IRWXO:
        raise SecretRefError(f"{purpose}: secret file is accessible to other users, refusing: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretRefError(f"{purpose}: secret file unreadable: {p} ({exc.strerror})") from None
    value = raw.rstrip("\r\n")
    if value == "":
        raise SecretRefError(f"{purpose}: secret file is empty: {p}")
    return value


def redact(message: str, secrets: Sequence[str]) -> str:
    """Replace any known secret value occurring in ``message``."""
    out = message
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def sanitize_endpoint(uri: str) -> str:
    """Strip any userinfo from a URI so it is safe to report."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<invalid-uri>"
    if "@" not in parts.netloc:
        return uri
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def reject_uri_userinfo(uri: str, *, setting: str = "neo4j_uri") -> None:
    """Raise ``BackendConfigError`` when ``uri`` carries userinfo (``user[:password]@``).

    Credentials belong in the dedicated user setting and a secret-file reference,
    never in the endpoint string: an embedded password would be passed verbatim
    to the driver and survive in Cognee's config object regardless of how the
    endpoint is reported. The error text carries only the sanitized endpoint.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        raise BackendConfigError(f"{setting} is not a valid URI") from None
    if "@" in parts.netloc:
        raise BackendConfigError(
            f"{setting} must not embed userinfo (username or password); configure the "
            f"dedicated user setting and a password secret-file reference instead; "
            f"got endpoint {sanitize_endpoint(uri)!r}"
        )


def validate_embedding_endpoint(uri: str, *, setting: str = "embedding_endpoint") -> str:
    """Accept an embedding endpoint only as a credential-free ``http(s)`` URL.

    Rejects (``BackendConfigError``, sanitized endpoint only in the message):
    userinfo in the authority (``http://user:secret@host``), any scheme outside
    ``EMBEDDING_ENDPOINT_SCHEMES``, and a missing host. Fact text is sent to this
    endpoint, so it must be an explicit HTTP inference server; the API key, when
    one is needed, comes from ``embedding_api_key_file``. Returns the sanitized
    endpoint for reporting.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise BackendConfigError(f"{setting} must be a non-empty http(s) URL")
    reject_uri_userinfo(uri, setting=setting)
    parts = urlsplit(uri)
    scheme = (parts.scheme.lower() if "://" in uri else "") or "<none>"
    if scheme not in EMBEDDING_ENDPOINT_SCHEMES:
        raise BackendConfigError(
            f"{setting} must use one of {sorted(EMBEDDING_ENDPOINT_SCHEMES)}; "
            f"got scheme {scheme!r} in endpoint {sanitize_endpoint(uri)!r}"
        )
    if not parts.hostname:
        raise BackendConfigError(f"{setting} must name a host; got endpoint {sanitize_endpoint(uri)!r}")
    return sanitize_endpoint(uri)


# --------------------------------------------------------------------------------------
# Route resolution and semantic helpers (no secrets)
# --------------------------------------------------------------------------------------


def binding_tag(model: str | None, dim: int) -> str:
    """Model and dimension binding tag for vector persistence and filtering."""
    return f"omp-emb:{model}@{dim}"


_KIND_TO_MODEL_NAME: dict[str, str] = {
    "module": "CodeModule",
    "symbol": "CodeSymbol",
    "route": "ApiEndpoint",
    "storage": "StorageResource",
    "dependency": "ExternalDependency",
    "service": "CodeService",
    "test_ref": "CodeTestReference",
    "file_ref": "CodeFileReference",
    "insight": "CodeInsight",
    "intent": "CodeIntent",
    "extraction": "CodeExtractionAccount",
    "association": "CodeAssociation",
    "lint": "CodeLintFinding",
}


def semantic_collection(kind: str) -> str:
    """Map a fact kind to its Cognee vector collection name."""
    model_name = _KIND_TO_MODEL_NAME.get(kind, kind)
    return f"{model_name}_description"


SEMANTIC_COLLECTIONS: tuple[str, ...] = (
    "ApiEndpoint_description",
    "CodeAssociation_description",
    "CodeExtractionAccount_description",
    "CodeFileReference_description",
    "CodeInsight_description",
    "CodeIntent_description",
    "CodeLintFinding_description",
    "CodeModule_description",
    "CodeService_description",
    "CodeSymbol_description",
    "CodeTestReference_description",
    "ExternalDependency_description",
    "StorageResource_description",
)


def semantic_text(
    kind: str,
    name: str,
    file_path: str | None,
    props: Mapping[str, Any] | None,
) -> str:
    """Build deterministic embeddable text for a fact node, truncated to 512 chars."""
    prefix = f"{kind} {name} in {file_path}; "
    scalar_parts: list[str] = []
    if props:
        for k in sorted(props.keys()):
            v = props[k]
            if isinstance(v, (str, int, float, bool)):
                scalar_parts.append(f"{k}={v}")
    full = prefix + ", ".join(scalar_parts)
    return full[:512]


# --------------------------------------------------------------------------------------
# Reranking helpers (pure; no I/O)
# --------------------------------------------------------------------------------------

# Pair format identifier recorded in every RerankReport: the query is the raw
# ``query_text`` and each document is the exact ``semantic_text`` that was indexed
# for the fact (no instruction prefix, no prompt template).
RERANK_PAIR_FORMAT = "rerank_v1/semantic_text"

# HTTP statuses from ``/v1/rerank`` (or ``/v1/models``) that describe a
# temporarily unavailable endpoint. Only these follow the outage policy; every
# other non-2xx status is a contract error and is never degraded.
RERANK_OUTAGE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


def rerank_documents(query_text: str, facts: Sequence["FactRecord"]) -> tuple[str, ...]:
    """The documents posted to ``/v1/rerank`` for ``facts`` in their pre-rerank order.

    Each document is ``semantic_text(kind, name, file_path, properties)`` of the
    hydrated fact, i.e. byte-identical to the ``description`` that was embedded
    and indexed for it. ``query_text`` is not folded into the documents (the
    endpoint receives it as the separate ``query`` field); it is part of the
    signature so the pair format has one place to change.
    """
    return tuple(semantic_text(fact.kind, fact.name, fact.file_path, fact.properties) for fact in facts)


def rerank_request_sha256(query_text: str, documents: Sequence[str]) -> str:
    """Identity of one rerank request: sha256 over the canonical JSON of the pairs."""
    canonical = json.dumps(
        {"query": query_text, "documents": list(documents)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_rerank_status(code: int) -> Literal["ok", "outage", "contract"]:
    """``ok`` for 2xx, ``outage`` for ``RERANK_OUTAGE_STATUSES``, ``contract`` otherwise."""
    if 200 <= code < 300:
        return "ok"
    if code in RERANK_OUTAGE_STATUSES:
        return "outage"
    return "contract"


def parse_rerank_response(payload: Any, *, expected_count: int, expected_model: str | None) -> tuple[float, ...]:
    """Strictly parse a ``/v1/rerank`` body into scores aligned to the request index.

    Accepts only: a JSON object whose ``results`` is a list of exactly
    ``expected_count`` objects, each with an integer ``index`` in range that
    occurs exactly once and a finite, non-boolean numeric ``relevance_score``.
    A ``model`` field, when present and not null, must equal ``expected_model``
    (``RerankBindingError`` otherwise). Every other deviation raises
    ``RerankContractError``. Nothing here is an outage.
    """
    if not isinstance(payload, dict):
        raise RerankContractError("engine_error: rerank response body is not a JSON object")
    echoed = payload.get("model")
    if echoed is not None and str(echoed) != expected_model:
        raise RerankBindingError(
            f"engine_unavailable: rerank response echoed model {str(echoed)!r} but {expected_model!r} was requested"
        )
    results = payload.get("results")
    if not isinstance(results, list):
        raise RerankContractError("engine_error: rerank response has no 'results' list")
    if len(results) != expected_count:
        raise RerankContractError(
            f"engine_error: rerank response returned {len(results)} result(s) for {expected_count} document(s)"
        )
    scores: list[float | None] = [None] * expected_count
    for position, item in enumerate(results):
        if not isinstance(item, dict):
            raise RerankContractError(f"engine_error: rerank result {position} is not a JSON object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise RerankContractError(f"engine_error: rerank result {position} has no integer 'index'")
        if not 0 <= index < expected_count:
            raise RerankContractError(
                f"engine_error: rerank result {position} index {index} is outside 0..{expected_count - 1}"
            )
        if scores[index] is not None:
            raise RerankContractError(f"engine_error: rerank response repeats index {index}")
        score = item.get("relevance_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankContractError(f"engine_error: rerank result for index {index} has no numeric 'relevance_score'")
        value = float(score)
        if not math.isfinite(value):
            raise RerankContractError(f"engine_error: rerank result for index {index} has a non-finite relevance_score")
        scores[index] = value
    # Exactly expected_count results, each index unique and in range: every slot is filled.
    return tuple(float(value) for value in scores if value is not None)


@dataclass(frozen=True)
class BackendRoute:
    """Resolved, credential-free description of where knowledge content lives."""

    graph_engine: str
    vector_store: str
    metadata_store: str
    graph_endpoint: str
    graph_database: str | None
    vector_endpoint: str
    vector_database: str | None
    vector_dimension: int
    embedded: bool
    provider_name: str
    semantic: bool
    embedding_provider: str
    embedding_model: str | None
    embedding_endpoint: str | None
    binding_tag: str | None
    identity_scheme: str = IDENTITY_SCHEME
    # Optional post-hydration reranking (semantic route only). ``reranking_endpoint``
    # is the sanitized form; credentials come from ``reranking_api_key_file`` only.
    reranking: bool = False
    reranking_provider: str = "none"
    reranking_model: str | None = None
    reranking_endpoint: str | None = None

    @property
    def required_modules(self) -> tuple[str, ...]:
        mods: list[str] = []
        mods.extend(REQUIRED_MODULES.get(self.graph_engine, ()))
        mods.extend(REQUIRED_MODULES.get(self.vector_store, ()))
        return tuple(dict.fromkeys(mods))

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_engine": self.graph_engine,
            "graph_endpoint": self.graph_endpoint,
            "graph_database": self.graph_database,
            "vector_store": self.vector_store,
            "vector_endpoint": self.vector_endpoint,
            "vector_database": self.vector_database,
            "vector_dimension": self.vector_dimension,
            "metadata_store": self.metadata_store,
            "embedded": self.embedded,
            "identity_scheme": self.identity_scheme,
            "semantic": self.semantic,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_endpoint": self.embedding_endpoint,
            "binding_tag": self.binding_tag,
            "reranking": self.reranking,
            "reranking_provider": self.reranking_provider,
            "reranking_model": self.reranking_model,
            "reranking_endpoint": self.reranking_endpoint,
        }


def resolve_backend_route(config: "KnowledgeConfig") -> BackendRoute:
    """Map configuration onto a concrete route. Pure; never touches secrets."""
    graph_engine = config.graph_engine
    vector_store = config.vector_store
    if graph_engine not in GRAPH_BACKENDS:
        raise BackendConfigError(f"unsupported graph_engine {graph_engine!r}; expected one of {GRAPH_BACKENDS}")
    if vector_store not in VECTOR_BACKENDS:
        raise BackendConfigError(f"unsupported vector_store {vector_store!r}; expected one of {VECTOR_BACKENDS}")

    if graph_engine == "neo4j":
        # Fail closed on embedded credentials before anything is derived from the
        # URI; sanitized reporting alone would still forward them to the driver.
        reject_uri_userinfo(config.neo4j_uri, setting="neo4j_uri")
        graph_endpoint = sanitize_endpoint(config.neo4j_uri)
        graph_database: str | None = config.neo4j_database
    else:
        graph_endpoint = "embedded"
        graph_database = None

    if vector_store == "pgvector":
        vector_endpoint = f"postgresql://{config.cognee_pg_host}:{config.cognee_pg_port}/{config.cognee_pg_database}"
        vector_database: str | None = config.cognee_pg_database
        metadata_store = "postgres"
    else:
        vector_endpoint = "embedded"
        vector_database = None
        metadata_store = "sqlite"

    embedded = graph_engine == "ladybug" and vector_store == "lancedb"
    provider_name = "ladybug-embedded" if graph_engine == "ladybug" else "neo4j"

    semantic = not config.graph_only
    embedding_provider = config.embedding_provider
    embedding_model = config.embedding_model if semantic else None
    # Same fail-closed rule as neo4j_uri: an endpoint carrying userinfo or an
    # unsupported scheme is refused here, before anything could forward it to
    # Cognee's embedding engine; the route only ever carries the sanitized form.
    embedding_endpoint = (
        validate_embedding_endpoint(config.embedding_endpoint) if (semantic and config.embedding_endpoint) else None
    )
    tag = binding_tag(config.embedding_model, config.vector_dimension) if (semantic and config.embedding_model) else None

    reranking_provider = config.reranking_provider
    reranking = reranking_provider != "none"
    if reranking and not semantic:
        # The config validator already refuses this; the route boundary fails
        # closed as well so a config built without the validator cannot enable
        # reranking on a graph-only route.
        raise BackendConfigError("reranking_provider requires the semantic route (graph_only=False)")
    reranking_endpoint = (
        validate_embedding_endpoint(config.reranking_endpoint, setting="reranking_endpoint")
        if (reranking and config.reranking_endpoint)
        else None
    )
    reranking_model = config.reranking_model if reranking else None

    return BackendRoute(
        graph_engine=graph_engine,
        vector_store=vector_store,
        metadata_store=metadata_store,
        graph_endpoint=graph_endpoint,
        graph_database=graph_database,
        vector_endpoint=vector_endpoint,
        vector_database=vector_database,
        vector_dimension=config.vector_dimension,
        embedded=embedded,
        provider_name=provider_name,
        semantic=semantic,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_endpoint=embedding_endpoint,
        binding_tag=tag,
        reranking=reranking,
        reranking_provider=reranking_provider,
        reranking_model=reranking_model,
        reranking_endpoint=reranking_endpoint,
    )


def missing_backend_modules(route: BackendRoute) -> tuple[str, ...]:
    """Python modules the route needs that are not importable in this interpreter.

    Dotted names are resolved the way an import would be: ``find_spec`` imports the
    parent package, so a broken parent (or one whose own dependencies are absent)
    is reported as missing rather than passing the check. The embedded route has
    no required modules and always returns an empty tuple.
    """
    missing: list[str] = []
    for mod in route.required_modules:
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError):
            # ImportError: parent package of a dotted name is absent or fails to
            # import; ValueError: a parent package has no usable __spec__.
            spec = None
        except Exception:
            # Importing a parent package can raise arbitrary errors when its own
            # dependencies are broken; treat that as missing to stay fail-closed.
            spec = None
        if spec is None:
            missing.append(mod)
    return tuple(missing)


# --------------------------------------------------------------------------------------
# Cognee settings (carry secrets; never serialized)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class CogneeBackendSettings:
    """Payloads for Cognee's ``config.set_*_db_config`` setters.

    Holds credential values in memory for the duration of engine configuration.
    ``repr`` / ``str`` are redacted so the object can never leak through logging.
    """

    graph: Mapping[str, Any] | None
    relational: Mapping[str, Any] | None
    vector: Mapping[str, Any] | None
    embedding: Mapping[str, Any] | None
    secrets: tuple[str, ...]

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "CogneeBackendSettings(<redacted>)"

    __str__ = __repr__


def build_cognee_backend_settings(config: "KnowledgeConfig", route: BackendRoute) -> CogneeBackendSettings:
    """Resolve secret references and build Cognee setter payloads for a non-embedded route.

    Raises ``SecretRefError`` / ``BackendConfigError`` without falling back; the
    caller decides how to surface unavailability.
    """
    secrets: list[str] = []
    graph: dict[str, Any] | None = None
    relational: dict[str, Any] | None = None
    vector: dict[str, Any] | None = None
    embedding: dict[str, Any] | None = None

    if route.graph_engine == "neo4j":
        password = read_secret_ref(config.neo4j_password_file, purpose="neo4j password")
        secrets.append(password)
        graph = {
            "graph_database_provider": "neo4j",
            "graph_database_url": config.neo4j_uri,
            "graph_database_username": config.neo4j_user,
            "graph_database_password": password,
        }
        if config.neo4j_database:
            # Only meaningful on Neo4j editions with multiple databases; Cognee versions
            # without this attribute are handled by the applier (see cognee_adapter).
            graph["graph_database_name"] = config.neo4j_database

    if route.vector_store == "pgvector":
        pg_password = ""
        if config.cognee_pg_password_file is not None:
            pg_password = read_secret_ref(config.cognee_pg_password_file, purpose="cognee postgres password")
            secrets.append(pg_password)
        relational = {
            "db_provider": "postgres",
            "db_host": config.cognee_pg_host,
            "db_port": str(config.cognee_pg_port),
            "db_name": config.cognee_pg_database,
            "db_username": config.cognee_pg_user,
            "db_password": pg_password,
        }
        if route.semantic:
            vector = {
                "vector_db_provider": "pgvector",
                "vector_db_host": config.cognee_pg_host,
                "vector_db_port": int(config.cognee_pg_port),
                "vector_db_name": config.cognee_pg_database,
                "vector_db_username": config.cognee_pg_user,
                "vector_db_password": pg_password,
            }
        else:
            vector = {"vector_db_provider": "pgvector"}

    if route.semantic:
        if config.embedding_api_key_file is not None:
            api_key = read_secret_ref(config.embedding_api_key_file, purpose="embedding api key")
            secrets.append(api_key)
        else:
            api_key = "no-key-required"
        embedding = {
            "embedding_provider": config.embedding_provider,
            "embedding_model": config.embedding_model,
            "embedding_dimensions": config.vector_dimension,
            "embedding_endpoint": config.embedding_endpoint,
            "embedding_api_key": api_key,
        }

    return CogneeBackendSettings(
        graph=graph,
        relational=relational,
        vector=vector,
        embedding=embedding,
        secrets=tuple(secrets),
    )


# --------------------------------------------------------------------------------------
# Graph query dialects
# --------------------------------------------------------------------------------------


def _coerce_json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _normalize_props(raw: Any) -> dict[str, Any]:
    """Return a node's property mapping as a plain dict.

    Handles the embedded engine (single JSON string column) and Neo4j (flat map in
    which nested dict fields such as ``fact_properties`` are stored JSON-encoded).
    """
    props = _coerce_json(raw)
    if not isinstance(props, dict):
        return {}
    props = dict(props)
    fact_props = props.get("fact_properties")
    if isinstance(fact_props, (str, bytes)):
        decoded = _coerce_json(fact_props)
        props["fact_properties"] = decoded if isinstance(decoded, dict) else {}
    return props


def _normalize_edge_props(raw: Any) -> dict[str, Any]:
    """Return an edge's property mapping as a plain, JSON-safe dict.

    The embedded engine stores edge properties as one JSON string; Neo4j returns a
    flat map that may carry driver temporal types. Values that are not JSON scalars
    are stringified so the mapping can be handed back to ``add_edges`` on either
    backend unchanged in meaning.
    """
    props = _coerce_json(raw)
    if not isinstance(props, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in props.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            out[str(key)] = value
        else:
            out[str(key)] = str(value)
    return out


# One captured edge: (source_node_id, target_node_id, relationship_name, properties).
EdgeRecord = tuple[str, str, str, dict[str, Any]]
# Identity of one edge without its properties.
EdgeKey = tuple[str, str, str]

# Node-id batch size for the incident-edge readback. Mirrors the chunking the
# pinned Cognee adapters apply to their own ``UNWIND`` / ``IN $ids`` statements.
INCIDENT_EDGE_QUERY_CHUNK = 500


class LadybugDialect:
    """Cypher and row handling for the embedded Ladybug engine (accepted r12 path).

    Scope and lookup query strings are kept byte-identical to the original embedded
    implementation. ``retire_query`` additionally selects the repository anchor by
    its deterministic id (see ``SnapshotScope.repo_node_id``).
    """

    name = "ladybug"

    def scope_query(self, scope: SnapshotScope) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (n:Node) "
            "WHERE json_extract(n.properties, '$.repo') = $scope_json "
            "RETURN n.id, n.name, n.properties",
            {"scope_json": json.dumps(scope.label)},
        )

    def lookup_query(self, scope: SnapshotScope, node_id: UUID) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (n:Node) "
            "WHERE n.id = $node_id AND json_extract(n.properties, '$.repo') = $scope_json "
            "RETURN n.id, n.name, n.properties",
            {"node_id": str(node_id), "scope_json": json.dumps(scope.label)},
        )

    def retire_query(self, scope: SnapshotScope) -> tuple[str, dict[str, Any]]:
        # Fact nodes carry the scope label in ``repo``. The CodeRepository anchor
        # does not (the pinned Cognee model ignores unknown fields), so it is
        # selected by its deterministic scope-derived id instead. Rows without
        # ``enola_id`` are still never surfaced as facts by query / lookup.
        return (
            "MATCH (n:Node) "
            "WHERE json_extract(n.properties, '$.repo') = $scope_json OR n.id = $repo_node_id "
            "RETURN n.id",
            {"scope_json": json.dumps(scope.label), "repo_node_id": str(scope.repo_node_id())},
        )

    def edges_query(self, node_id: UUID | str) -> tuple[str, dict[str, Any]]:
        # Cognee's embedded schema keeps every relationship in one ``EDGE`` table
        # with ``relationship_name`` and a JSON ``properties`` column. Direction is
        # returned explicitly (source, target) so edges can be re-added verbatim.
        return (
            "MATCH (s:Node)-[r:EDGE]->(t:Node) "
            "WHERE s.id = $node_id OR t.id = $node_id "
            "RETURN s.id, t.id, r.relationship_name, r.properties",
            {"node_id": str(node_id)},
        )

    def edge_row(self, row: Any) -> EdgeRecord:
        return str(row[0]), str(row[1]), str(row[2]), _normalize_edge_props(row[3])

    def incident_edges_query(self, node_ids: Sequence[str]) -> tuple[str, dict[str, Any]]:
        # Every edge touching any of ``node_ids`` (either endpoint). ``IN $ids``
        # over a list parameter is the idiom the pinned Cognee Ladybug adapter's
        # own ``delete_nodes`` uses. Only the identity columns are returned.
        return (
            "MATCH (s:Node)-[r:EDGE]->(t:Node) "
            "WHERE s.id IN $node_ids OR t.id IN $node_ids "
            "RETURN s.id, t.id, r.relationship_name",
            {"node_ids": [str(node_id) for node_id in node_ids]},
        )

    def edge_key_row(self, row: Any) -> EdgeKey:
        return str(row[0]), str(row[1]), str(row[2])

    def capability_query(self, procedures: Sequence[str]) -> tuple[str, dict[str, Any]] | None:
        # The embedded engine has no server-side procedure catalogue to probe.
        return None

    def capability_row(self, row: Any) -> str:
        return str(row[0])

    def node_row(self, row: Any) -> tuple[str, Any, dict[str, Any]]:
        node_id, name, raw_props = row[0], row[1], row[2]
        if not raw_props:
            return str(node_id), name, {}
        props = json.loads(raw_props) if isinstance(raw_props, str) else raw_props
        return str(node_id), name, props if isinstance(props, dict) else {}

    def id_row(self, row: Any) -> str:
        return str(row[0])

    def node_properties(self, node: Mapping[str, Any]) -> dict[str, Any]:
        props = node.get("properties") or {}
        if isinstance(props, str):
            props = json.loads(props)
        return props if isinstance(props, dict) else {}


class Neo4jDialect:
    """Cypher and row handling for Cognee's Neo4j adapter.

    Cognee stores each DataPoint field as a node property (nested dicts JSON-encoded)
    and returns ``session.run(...).data()`` rows, i.e. dicts keyed by RETURN alias.
    """

    name = "neo4j"

    def scope_query(self, scope: SnapshotScope) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (n) WHERE n.repo = $scope "
            "RETURN n.id AS id, n.name AS name, properties(n) AS properties",
            {"scope": scope.label},
        )

    def lookup_query(self, scope: SnapshotScope, node_id: UUID) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (n {id: $node_id}) WHERE n.repo = $scope "
            "RETURN n.id AS id, n.name AS name, properties(n) AS properties",
            {"node_id": str(node_id), "scope": scope.label},
        )

    def retire_query(self, scope: SnapshotScope) -> tuple[str, dict[str, Any]]:
        # Same anchor handling as LadybugDialect.retire_query: the CodeRepository
        # node has no ``repo`` property and is selected by its deterministic id.
        return (
            "MATCH (n) WHERE n.repo = $scope OR n.id = $repo_node_id RETURN n.id AS id",
            {"scope": scope.label, "repo_node_id": str(scope.repo_node_id())},
        )

    def edges_query(self, node_id: UUID | str) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (s)-[r]->(t) WHERE s.id = $node_id OR t.id = $node_id "
            "RETURN s.id AS source_id, t.id AS target_id, "
            "type(r) AS relationship_name, properties(r) AS properties",
            {"node_id": str(node_id)},
        )

    def edge_row(self, row: Any) -> EdgeRecord:
        if isinstance(row, Mapping):
            return (
                str(row.get("source_id")),
                str(row.get("target_id")),
                str(row.get("relationship_name")),
                _normalize_edge_props(row.get("properties")),
            )
        return str(row[0]), str(row[1]), str(row[2]), _normalize_edge_props(row[3])

    def incident_edges_query(self, node_ids: Sequence[str]) -> tuple[str, dict[str, Any]]:
        return (
            "MATCH (s)-[r]->(t) WHERE s.id IN $node_ids OR t.id IN $node_ids "
            "RETURN s.id AS source_id, t.id AS target_id, type(r) AS relationship_name",
            {"node_ids": [str(node_id) for node_id in node_ids]},
        )

    def edge_key_row(self, row: Any) -> EdgeKey:
        if isinstance(row, Mapping):
            return str(row.get("source_id")), str(row.get("target_id")), str(row.get("relationship_name"))
        return str(row[0]), str(row[1]), str(row[2])

    def capability_query(self, procedures: Sequence[str]) -> tuple[str, dict[str, Any]] | None:
        # ``SHOW PROCEDURES`` is plain Cypher (Neo4j 4.3+ / 5.x) and needs no plugin,
        # so it can report an absent APOC instead of failing like ``CALL apoc.*``.
        return (
            "SHOW PROCEDURES YIELD name WHERE name IN $names RETURN name",
            {"names": list(procedures)},
        )

    def capability_row(self, row: Any) -> str:
        if isinstance(row, Mapping):
            return str(row.get("name"))
        return str(row[0])

    def node_row(self, row: Any) -> tuple[str, Any, dict[str, Any]]:
        if isinstance(row, Mapping):
            node_id, name, raw_props = row.get("id"), row.get("name"), row.get("properties")
        else:
            node_id, name, raw_props = row[0], row[1], row[2]
        return str(node_id), name, _normalize_props(raw_props)

    def id_row(self, row: Any) -> str:
        if isinstance(row, Mapping):
            return str(row.get("id"))
        return str(row[0])

    def node_properties(self, node: Mapping[str, Any]) -> dict[str, Any]:
        if "properties" in node and isinstance(_coerce_json(node["properties"]), dict):
            return _normalize_props(node["properties"])
        return _normalize_props(dict(node))


GraphDialect = LadybugDialect | Neo4jDialect


def dialect_for(route: BackendRoute) -> GraphDialect:
    if route.graph_engine == "neo4j":
        return Neo4jDialect()
    return LadybugDialect()


__all__ = [
    "EMBEDDING_ENDPOINT_SCHEMES",
    "GRAPH_BACKENDS",
    "IDENTITY_SCHEME",
    "INCIDENT_EDGE_QUERY_CHUNK",
    "NEO4J_URI_SCHEMES",
    "RERANK_OUTAGE_STATUSES",
    "RERANK_PAIR_FORMAT",
    "REQUIRED_MODULES",
    "REQUIRED_NEO4J_PROCEDURES",
    "SEMANTIC_COLLECTIONS",
    "VECTOR_BACKENDS",
    "BackendConfigError",
    "BackendRoute",
    "CogneeBackendSettings",
    "EdgeKey",
    "EdgeRecord",
    "GraphDialect",
    "LadybugDialect",
    "Neo4jDialect",
    "SecretRefError",
    "SnapshotScope",
    "binding_tag",
    "build_cognee_backend_settings",
    "classify_rerank_status",
    "dialect_for",
    "is_backend_connection_failure",
    "is_embedding_failure",
    "missing_backend_modules",
    "parse_rerank_response",
    "read_secret_ref",
    "redact",
    "reject_uri_userinfo",
    "rerank_documents",
    "rerank_request_sha256",
    "resolve_backend_route",
    "sanitize_endpoint",
    "semantic_collection",
    "semantic_text",
    "validate_embedding_endpoint",
]
