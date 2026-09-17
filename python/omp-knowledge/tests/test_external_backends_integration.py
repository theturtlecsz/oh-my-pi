"""Opt-in integration test for the external Cognee route (Neo4j graph + pgvector).

This module never runs by default. It is skipped, with an explicit reason, unless
ALL of the following hold:

* ``OMP_KNOWLEDGE_EXTERNAL_BACKEND_INTEGRATION=1`` is set.
* The pinned ``cognee`` package imports (``COGNEE_AVAILABLE``).
* Every optional module the route needs is importable (``neo4j``, ``pgvector``,
  ``sqlalchemy``, ``asyncpg``) as computed by ``missing_backend_modules``.
* Secret-file references exist for both backends:
  ``OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE`` and ``OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE``.

Connection details come from the same ``OMP_KNOWLEDGE_*`` variables ``load_config``
reads; when unset they fall back to the qualified external topology (PostgreSQL
port 15432, database ``omp_cognee``, role ``omp_cognee_app``, 1024-dim vectors;
Neo4j Bolt on port 17687, HTTP on 17474). Only the Bolt endpoint is exercised; the
HTTP port is documented for operators and never contacted by this test.

The optional modules ship with the ``omp-knowledge[external-backends]`` extra,
which pins exactly what the pinned Cognee's ``neo4j`` / ``postgres`` extras require.

When enabled the first test:

1. constructs ``RealCogneeAdapter`` with ``graph_engine="neo4j"`` and
   ``vector_store="pgvector"`` while spying on Cognee's supported
   ``config.set_graph_db_config`` / ``set_relational_db_config`` /
   ``set_vector_db_config`` setters (the spies call through to the real setters);
2. verifies the credential-free engine status and Cognee's own graph-config readback;
3. performs one real authenticated readback against each backend: a ``RETURN 1``
   round-trip through Cognee's graph engine, and a ``pg_extension`` probe of the
   Cognee PostgreSQL database with the app role.

The second test runs one real fact journey on the same route:

4. ingests a snapshot holding a single explicit Enola fact (full fact id and file
   path) plus an identical control snapshot, verifies query and exact lookup,
   applies a correction and reads the changed stored properties back (fact
   properties only, incident edges preserved), retires the primary snapshot
   (fact node and repository anchor, counted by the same predicate the dialect's
   retire query uses) and proves the control snapshot is untouched.

The Neo4j server must have the APOC core plugin: the adapter probes for
``apoc.create.addLabels`` / ``apoc.merge.relationship`` before its first write
and raises ``EngineUnavailableError`` when they are absent.

All writes are ``graph_only``: no embeddings are produced and no semantic model is
required, so pgvector is only ever checked as an extension / app-role readback.

No credential value is ever logged or embedded in an assertion message; backend
exceptions are re-raised through ``redact`` before pytest sees them.

Cognee configuration is process-global. Run this module in its own pytest process;
do not mix it with the embedded-route integration tests in one invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Mapping, TypeVar
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.backends import (
    Neo4jDialect,
    SnapshotScope,
    missing_backend_modules,
    read_secret_ref,
    redact,
    resolve_backend_route,
    sanitize_endpoint,
)
from omp_knowledge.engine.cognee_adapter import (
    COGNEE_AVAILABLE,
    RealCogneeAdapter,
    snapshot_fact_node_id,
)
from support.fixtures import make_test_snapshot_ref

T = TypeVar("T")

GATE_ENV = "OMP_KNOWLEDGE_EXTERNAL_BACKEND_INTEGRATION"
NEO4J_PASSWORD_FILE_ENV = "OMP_KNOWLEDGE_NEO4J_PASSWORD_FILE"
COGNEE_PG_PASSWORD_FILE_ENV = "OMP_KNOWLEDGE_COGNEE_PG_PASSWORD_FILE"

QUALIFIED_COGNEE_PG_PORT = 15432
QUALIFIED_COGNEE_PG_DATABASE = "omp_cognee"
QUALIFIED_COGNEE_PG_USER = "omp_cognee_app"
QUALIFIED_VECTOR_DIMENSION = 1024
# Qualified Neo4j exposes Bolt on 17687 and HTTP on 17474; Cognee's driver only
# speaks Bolt, so the HTTP port is never a default here.
QUALIFIED_NEO4J_BOLT_PORT = 17687
QUALIFIED_NEO4J_HTTP_PORT = 17474
QUALIFIED_NEO4J_URI = f"bolt://127.0.0.1:{QUALIFIED_NEO4J_BOLT_PORT}"
QUALIFIED_NEO4J_USER = "neo4j"

EXTERNAL_BACKENDS_EXTRA = "omp-knowledge[external-backends]"

COGNEE_SETTER_NAMES = (
    "set_graph_db_config",
    "set_relational_db_config",
    "set_vector_db_config",
)


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _external_config(state_root: Path) -> KnowledgeConfig:
    """Build the external-route config from env, defaulting to the qualified topology."""
    return KnowledgeConfig(
        state_dir=state_root / "state",
        config_dir=state_root / "config",
        graph_engine="neo4j",
        vector_store="pgvector",
        neo4j_uri=os.environ.get("OMP_KNOWLEDGE_NEO4J_URI", QUALIFIED_NEO4J_URI),
        neo4j_user=os.environ.get("OMP_KNOWLEDGE_NEO4J_USER", QUALIFIED_NEO4J_USER),
        neo4j_password_file=_optional_path(os.environ.get(NEO4J_PASSWORD_FILE_ENV)),
        neo4j_database=os.environ.get("OMP_KNOWLEDGE_NEO4J_DATABASE") or None,
        cognee_pg_host=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_HOST", "127.0.0.1"),
        cognee_pg_port=int(os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_PORT", str(QUALIFIED_COGNEE_PG_PORT))),
        cognee_pg_database=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_DATABASE", QUALIFIED_COGNEE_PG_DATABASE),
        cognee_pg_user=os.environ.get("OMP_KNOWLEDGE_COGNEE_PG_USER", QUALIFIED_COGNEE_PG_USER),
        cognee_pg_password_file=_optional_path(os.environ.get(COGNEE_PG_PASSWORD_FILE_ENV)),
        vector_dimension=int(os.environ.get("OMP_KNOWLEDGE_VECTOR_DIMENSION", str(QUALIFIED_VECTOR_DIMENSION))),
    )


def _skip_reason() -> str | None:
    """Explicit, ordered preconditions. The first unmet one is the skip reason."""
    if os.environ.get(GATE_ENV) != "1":
        return f"set {GATE_ENV}=1 to run the external Neo4j + pgvector integration test"
    if not COGNEE_AVAILABLE:
        return "pinned cognee package is not importable in this interpreter"

    # Module availability is evaluated on the same route the adapter resolves.
    route = resolve_backend_route(KnowledgeConfig(graph_engine="neo4j", vector_store="pgvector"))
    missing = missing_backend_modules(route)
    if missing:
        return (
            "optional backend modules not installed "
            f"(install the {EXTERNAL_BACKENDS_EXTRA!r} extra): " + ", ".join(missing)
        )

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


_SKIP_REASON = _skip_reason()

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _fail_redacted(context: str, exc: BaseException, secrets: tuple[str, ...]) -> None:
    pytest.fail(f"{context}: " + redact(f"{type(exc).__name__}: {exc}", secrets), pytrace=False)


def _row_value(row: Any, alias: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(alias)
    return row[0]


async def _await_redacted(context: str, awaitable: Awaitable[T], secrets: tuple[str, ...]) -> T:
    """Await a backend call, surfacing any failure through ``redact``.

    Assertion failures (and pytest's own outcome exceptions, which are not
    ``Exception`` subclasses) pass through untouched.
    """
    try:
        return await awaitable
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted(context, exc, secrets)
        raise AssertionError("unreachable")


# ----------------------------------------------------------------------------------
# Fact journey payload (explicit, valid, and tiny). No relations: the journey is
# about node identity and stored properties, not edge resolution.
# ----------------------------------------------------------------------------------

JOURNEY_REPO_NAME = "external-journey-repo"
JOURNEY_FILE_PATH = "src/models/user.py"
JOURNEY_SYMBOL_NAME = "User"
JOURNEY_ORIGINAL_PROPS: dict[str, Any] = {"symbol_kind": "class"}
JOURNEY_CORRECTION: dict[str, Any] = {
    "symbol_kind": "dataclass",
    "review_note": "external-journey correction",
}


def _journey_fact() -> dict[str, Any]:
    """One explicit Enola symbol fact.

    The ``id`` is the full Enola fact id (sha256 over repo / kind / name / file,
    truncated to 32 hex chars, the same derivation ``support.fixtures`` uses). The
    whole id and the file path must survive ingest, query, lookup and correction.
    """
    fact_id = hashlib.sha256(
        f"{JOURNEY_REPO_NAME}\0symbol\0{JOURNEY_SYMBOL_NAME}\0{JOURNEY_FILE_PATH}".encode()
    ).hexdigest()[:32]
    return {
        "id": fact_id,
        "kind": "symbol",
        "name": JOURNEY_SYMBOL_NAME,
        "repo": JOURNEY_REPO_NAME,
        "file": JOURNEY_FILE_PATH,
        "line": 10,
        "end_line": 50,
        "props": dict(JOURNEY_ORIGINAL_PROPS),
        "relations": [],
    }


def _journey_snapshot_id(repository_id: UUID, role: str) -> str:
    """Canonical 64-hex snapshot id, unique per repository and per journey role.

    The live backend is shared, so ids are derived from the fresh ``repository_id``
    rather than fixed constants; two journeys never collide on scope.
    """
    return hashlib.sha256(f"omp-knowledge-external-journey:{repository_id}:{role}".encode()).hexdigest()


@pytest.fixture
def external_cfg(tmp_path: Path) -> KnowledgeConfig:
    return _external_config(tmp_path)


@pytest.fixture
def secrets(external_cfg: KnowledgeConfig) -> tuple[str, ...]:
    """Resolved credential values, used only to redact error text."""
    neo4j_pw = read_secret_ref(external_cfg.neo4j_password_file, purpose="neo4j password")
    pg_pw = read_secret_ref(external_cfg.cognee_pg_password_file, purpose="cognee postgres password")
    return (neo4j_pw, pg_pw)


@pytest.fixture
def cognee_setter_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record calls to Cognee's supported setters while still applying them for real."""
    import cognee

    calls: list[tuple[str, dict[str, Any]]] = []
    cognee_config = cognee.config
    for name in COGNEE_SETTER_NAMES:
        real = getattr(cognee_config, name, None)
        if not callable(real):
            pytest.fail(f"pinned cognee does not expose config.{name}", pytrace=False)

        def _make(setter_name: str, target: Any):
            def _spy(payload: Mapping[str, Any]) -> Any:
                calls.append((setter_name, dict(payload)))
                return target(payload)

            return _spy

        monkeypatch.setattr(cognee_config, name, _make(name, real))
    return calls


async def test_external_route_binds_cognee_setters_and_reads_back_from_live_backends(
    external_cfg: KnowledgeConfig,
    secrets: tuple[str, ...],
    cognee_setter_spy: list[tuple[str, dict[str, Any]]],
) -> None:
    neo4j_pw, pg_pw = secrets

    # ------------------------------------------------------------------
    # 1. Construction wires the route through Cognee's supported setters.
    # ------------------------------------------------------------------
    try:
        adapter = RealCogneeAdapter(external_cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    assert [name for name, _ in cognee_setter_spy] == list(COGNEE_SETTER_NAMES)
    payloads = dict(cognee_setter_spy)
    assert payloads["set_graph_db_config"]["graph_database_provider"] == "neo4j"
    assert payloads["set_graph_db_config"]["graph_database_url"] == external_cfg.neo4j_uri
    assert payloads["set_graph_db_config"]["graph_database_username"] == external_cfg.neo4j_user
    assert payloads["set_relational_db_config"]["db_provider"] == "postgres"
    assert payloads["set_relational_db_config"]["db_host"] == external_cfg.cognee_pg_host
    assert payloads["set_relational_db_config"]["db_port"] == str(external_cfg.cognee_pg_port)
    assert payloads["set_relational_db_config"]["db_name"] == external_cfg.cognee_pg_database
    assert payloads["set_relational_db_config"]["db_username"] == external_cfg.cognee_pg_user
    assert payloads["set_vector_db_config"] == {"vector_db_provider": "pgvector"}
    # Credentials reach Cognee only through the setter payloads. The comparisons are
    # reduced to booleans first so a failing assertion never echoes a secret value.
    graph_password_matches = payloads["set_graph_db_config"]["graph_database_password"] == neo4j_pw
    relational_password_matches = payloads["set_relational_db_config"]["db_password"] == pg_pw
    assert graph_password_matches, "neo4j password payload does not match the secret file"
    assert relational_password_matches, "cognee postgres password payload does not match the secret file"

    # ------------------------------------------------------------------
    # 2. Engine status is credential-free and names the external route.
    # ------------------------------------------------------------------
    status = await adapter.status()
    assert status.available is True
    assert status.engine_name == "RealCogneeAdapter"
    assert status.version == "1.5.4"
    assert status.graph_engine == "neo4j"
    assert status.active_route.provider == "neo4j"
    assert status.active_route.active is True
    assert status.active_route.endpoint == sanitize_endpoint(external_cfg.neo4j_uri)
    assert status.details["graph_engine"] == "neo4j"
    assert status.details["vector_store"] == "pgvector"
    assert status.details["metadata_store"] == "postgres"
    assert status.details["embedded"] is False
    assert status.details["vector_database"] == external_cfg.cognee_pg_database
    assert status.details["vector_dimension"] == external_cfg.vector_dimension
    assert status.details["vector_endpoint"] == (
        f"postgresql://{external_cfg.cognee_pg_host}:{external_cfg.cognee_pg_port}/{external_cfg.cognee_pg_database}"
    )

    serialized = json.dumps(status.model_dump(mode="json"))
    leaked_secret_indexes = [index for index, secret in enumerate(secrets) if secret in serialized]
    assert not leaked_secret_indexes, "engine status serialization contains a credential value"
    assert str(external_cfg.neo4j_password_file) not in serialized
    assert str(external_cfg.cognee_pg_password_file) not in serialized

    # Cognee's own graph configuration reflects what the setter applied. The adapter
    # clears this cached config before binding, so this is the live object.
    from cognee.infrastructure.databases.graph.config import get_graph_config

    graph_config = get_graph_config()
    assert getattr(graph_config, "graph_database_provider", None) == "neo4j"
    assert getattr(graph_config, "graph_database_url", None) == external_cfg.neo4j_uri

    # ------------------------------------------------------------------
    # 3a. Real authenticated readback through Cognee's Neo4j graph engine.
    # ------------------------------------------------------------------
    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine

    try:
        graph_engine = await get_graph_engine()
        rows = await graph_engine.query("RETURN 1 AS ok", {})
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("Neo4j readback via cognee graph engine failed", exc, secrets)
        raise AssertionError("unreachable")
    assert rows, "RETURN 1 produced no rows"
    assert _row_value(rows[0], "ok") == 1

    # A scope that was never written must read back empty through the adapter's
    # own snapshot-scoped query path (non-mutating).
    empty = await adapter.query(
        workspace_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id="0" * 64,
        query_text="*",
    )
    assert empty.total_matched == 0
    assert empty.route.provider == "neo4j"

    # ------------------------------------------------------------------
    # 3b. Real authenticated readback of the Cognee PostgreSQL / pgvector database.
    #     Uses the pinned psycopg driver as a test-side probe of the backend Cognee's
    #     pgvector adapter targets; it does not replace any Cognee adapter.
    # ------------------------------------------------------------------
    try:
        with psycopg.connect(
            host=external_cfg.cognee_pg_host,
            port=external_cfg.cognee_pg_port,
            dbname=external_cfg.cognee_pg_database,
            user=external_cfg.cognee_pg_user,
            password=pg_pw,
            connect_timeout=10,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, current_database()")
                who = cur.fetchone()
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                vector_ext = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("PostgreSQL readback of the cognee database failed", exc, secrets)
        raise AssertionError("unreachable")

    assert who is not None
    assert who[0] == external_cfg.cognee_pg_user
    assert who[1] == external_cfg.cognee_pg_database
    assert vector_ext is not None, "pgvector extension is not installed in the cognee database"


async def test_external_route_fact_journey_ingest_query_correct_retire(
    external_cfg: KnowledgeConfig,
    secrets: tuple[str, ...],
) -> None:
    """One real fact journey on the Neo4j + pgvector route.

    Ingests a primary snapshot holding a single Enola fact and an identical control
    snapshot (same fact id, same file path, different snapshot id), then proves:

    * query and exact lookup return the fact under the same workspace / repository
      / snapshot with its full Enola fact id and file path;
    * correction changes the stored node properties and lookup reflects it, while
      the control snapshot's properties are untouched;
    * retire removes only the primary snapshot: its scope reads back empty through
      the adapter and through a direct Cypher count, the control snapshot keeps
      exactly the nodes it had.

    Every write goes through ``add_data_points(..., graph_only=True)``. That means
    no embeddings are computed and no semantic model is contacted, so nothing here
    asserts on vector rows; pgvector is only checked as an extension / app-role
    readback (see 4b). Both scopes are retired in ``finally`` so a failed run does
    not leave nodes behind on the shared backend.
    """
    _, pg_pw = secrets

    try:
        adapter = RealCogneeAdapter(external_cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced redacted
        _fail_redacted("RealCogneeAdapter construction failed", exc, secrets)
        raise AssertionError("unreachable")

    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine

    graph_engine = await _await_redacted("get_graph_engine failed", get_graph_engine(), secrets)
    dialect = Neo4jDialect()

    async def cypher(context: str, query: str, params: dict[str, Any]) -> list[Any]:
        return await _await_redacted(context, graph_engine.query(query, params), secrets)

    async def scope_node_count(scope: SnapshotScope) -> int:
        # Same predicate the dialect's retire query uses: fact nodes carry the scope
        # label in ``repo``; the CodeRepository anchor has no ``repo`` property and
        # is selected by its deterministic id (SnapshotScope.repo_node_id).
        rows = await cypher(
            "scope count readback failed",
            "MATCH (n) WHERE n.repo = $scope OR n.id = $repo_node_id RETURN count(n) AS remaining",
            {"scope": scope.label, "repo_node_id": str(scope.repo_node_id())},
        )
        assert rows, "count query produced no rows"
        return int(_row_value(rows[0], "remaining"))

    async def count_by_id(node_id: UUID) -> int:
        rows = await cypher(
            "node id readback failed",
            "MATCH (n {id: $node_id}) RETURN count(n) AS remaining",
            {"node_id": str(node_id)},
        )
        assert rows, "count query produced no rows"
        return int(_row_value(rows[0], "remaining"))

    async def incident_edge_keys(node_id: UUID) -> set[tuple[str, str, str]]:
        query, params = dialect.edges_query(node_id)
        rows = await cypher("edge readback failed", query, params)
        return {(source, target, name) for source, target, name, _ in (dialect.edge_row(row) for row in rows)}

    # ------------------------------------------------------------------
    # Workspace / repository / canonical snapshot ids and refs.
    # ------------------------------------------------------------------
    workspace_id = uuid4()
    repository_id = uuid4()
    fact = _journey_fact()
    fact_id = fact["id"]

    primary_snapshot_id = _journey_snapshot_id(repository_id, "primary")
    control_snapshot_id = _journey_snapshot_id(repository_id, "control")
    assert primary_snapshot_id != control_snapshot_id
    primary_ref = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=primary_snapshot_id)
    control_ref = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=control_snapshot_id)
    assert primary_ref.snapshot_id == primary_snapshot_id
    assert primary_ref.repository_id == repository_id

    primary_scope = SnapshotScope(
        workspace_id=workspace_id, repository_id=repository_id, snapshot_id=primary_snapshot_id
    )
    control_scope = SnapshotScope(
        workspace_id=workspace_id, repository_id=repository_id, snapshot_id=control_snapshot_id
    )
    primary_node_id = primary_scope.fact_node_id(fact_id)
    control_node_id = control_scope.fact_node_id(fact_id)
    # The adapter's identity helper and the backend-independent scope agree.
    assert primary_node_id == snapshot_fact_node_id(primary_scope.label, fact_id)
    assert primary_node_id != control_node_id

    def primary_kwargs() -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "repository_id": repository_id,
            "snapshot_id": primary_snapshot_id,
        }

    def control_kwargs() -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "repository_id": repository_id,
            "snapshot_id": control_snapshot_id,
        }

    try:
        # ------------------------------------------------------------------
        # 4a. Ingest: preflight plan and real write agree on identity and hash.
        # ------------------------------------------------------------------
        plan = await adapter.plan_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_ref=primary_ref,
            facts=[fact],
            receipt=None,
            insights=[],
        )
        ingest = await _await_redacted(
            "ingest_snapshot (primary) failed",
            adapter.ingest_snapshot(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_ref=primary_ref,
                facts=[fact],
                receipt=None,
                insights=[],
            ),
            secrets,
        )
        assert ingest.snapshot_id == primary_snapshot_id
        assert ingest.nodes_written == 2, "expected the repository node plus exactly one fact node"
        assert ingest.edges_written == 0
        assert str(primary_node_id) in ingest.node_ids
        assert ingest.node_ids == plan.node_ids
        assert ingest.graph_sha256 == plan.graph_sha256
        assert ingest.route.provider == "neo4j"
        assert ingest.route.active is True
        # graph_only route: no embedding model is inferred or contacted.
        assert ingest.route.graph_only is True
        assert ingest.route.model_id is None
        assert ingest.route.model_inferred is False

        control_ingest = await _await_redacted(
            "ingest_snapshot (control) failed",
            adapter.ingest_snapshot(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_ref=control_ref,
                facts=[fact],
                receipt=None,
                insights=[],
            ),
            secrets,
        )
        assert control_ingest.nodes_written == 2
        assert str(control_node_id) in control_ingest.node_ids
        # Same fact id in two snapshots never shares a node id.
        assert set(control_ingest.node_ids).isdisjoint(ingest.node_ids)

        # The scope count covers the repository anchor plus the fact node: exactly
        # what ingest reported, so retire's nodes_deleted can be checked for the
        # anchor rather than only for the ``repo``-labelled fact nodes.
        primary_count_before = await scope_node_count(primary_scope)
        control_count_before = await scope_node_count(control_scope)
        assert primary_count_before == ingest.nodes_written == 2
        assert control_count_before == control_ingest.nodes_written == 2
        assert await count_by_id(primary_scope.repo_node_id()) == 1, "repository anchor must exist by id"
        assert await count_by_id(control_scope.repo_node_id()) == 1
        primary_edges_before = await incident_edge_keys(primary_node_id)

        # ------------------------------------------------------------------
        # 4b. Query and exact lookup find the fact with its full identity.
        # ------------------------------------------------------------------
        found = await _await_redacted(
            "query (primary) failed",
            adapter.query(**primary_kwargs(), query_text=JOURNEY_SYMBOL_NAME),
            secrets,
        )
        assert found.total_matched == 1
        assert found.route.provider == "neo4j"
        record = found.facts[0]
        assert record.fact_id == fact_id
        assert record.node_id == primary_node_id
        assert record.name == JOURNEY_SYMBOL_NAME
        assert record.kind == "symbol"
        assert record.file_path == JOURNEY_FILE_PATH
        assert record.line == fact["line"]
        assert record.end_line == fact["end_line"]
        assert record.properties == JOURNEY_ORIGINAL_PROPS
        assert record.workspace_id == workspace_id
        assert record.repository_id == repository_id
        assert record.snapshot_id == primary_snapshot_id

        lookup = await _await_redacted(
            "lookup (primary) failed",
            adapter.lookup(**primary_kwargs(), fact_id=fact_id, file_path=JOURNEY_FILE_PATH),
            secrets,
        )
        assert lookup.found is True
        assert lookup.fact is not None
        assert lookup.snapshot_id == primary_snapshot_id
        assert lookup.fact.model_dump(mode="json") == record.model_dump(mode="json")

        # Exact lookup honours the file path: the same fact id under another path is absent.
        wrong_path = await _await_redacted(
            "lookup (wrong file path) failed",
            adapter.lookup(**primary_kwargs(), fact_id=fact_id, file_path="src/auth/user.py"),
            secrets,
        )
        assert wrong_path.found is False
        assert wrong_path.fact is None

        # Control snapshot holds the same fact id under its own node id.
        control_lookup = await _await_redacted(
            "lookup (control) failed",
            adapter.lookup(**control_kwargs(), fact_id=fact_id),
            secrets,
        )
        assert control_lookup.found is True
        assert control_lookup.fact is not None
        assert control_lookup.fact.fact_id == fact_id
        assert control_lookup.fact.node_id == control_node_id
        assert control_lookup.fact.snapshot_id == control_snapshot_id
        assert control_lookup.fact.properties == JOURNEY_ORIGINAL_PROPS

        # pgvector limitation, stated explicitly: the ingest above was graph_only, so
        # Cognee computed no embeddings and wrote no vector rows. The only thing this
        # journey can and does verify on the PostgreSQL side is that the configured
        # app role still reaches the Cognee database and the pgvector extension is
        # installed there. No vector table, row count or similarity call is asserted.
        status = await adapter.status()
        assert status.details["vector_store"] == "pgvector"
        assert status.details["vector_indexing"] is False
        try:
            with psycopg.connect(
                host=external_cfg.cognee_pg_host,
                port=external_cfg.cognee_pg_port,
                dbname=external_cfg.cognee_pg_database,
                user=external_cfg.cognee_pg_user,
                password=pg_pw,
                connect_timeout=10,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT current_user, current_database(), "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                    )
                    who = cur.fetchone()
                    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                    vector_ext = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - surfaced redacted
            _fail_redacted("PostgreSQL readback of the cognee database failed", exc, secrets)
            raise AssertionError("unreachable")
        assert who is not None
        assert who[0] == external_cfg.cognee_pg_user
        assert who[1] == external_cfg.cognee_pg_database
        assert who[2] is False, "cognee app role must not be a superuser"
        assert vector_ext is not None, "pgvector extension is not installed in the cognee database"

        # ------------------------------------------------------------------
        # 4c. Correction changes stored properties; lookup reflects it; the
        #     control snapshot does not move.
        # ------------------------------------------------------------------
        corrected = await _await_redacted(
            "correct (primary) failed",
            adapter.correct(**primary_kwargs(), fact_id=fact_id, properties_update=dict(JOURNEY_CORRECTION)),
            secrets,
        )
        assert corrected.success is True
        assert corrected.corrected_fact_id == fact_id
        assert corrected.snapshot_id == primary_snapshot_id

        after = await _await_redacted(
            "lookup (after correction) failed",
            adapter.lookup(**primary_kwargs(), fact_id=fact_id, file_path=JOURNEY_FILE_PATH),
            secrets,
        )
        assert after.found is True
        assert after.fact is not None
        assert after.fact.node_id == primary_node_id
        assert after.fact.fact_id == fact_id
        assert after.fact.name == JOURNEY_SYMBOL_NAME
        assert after.fact.kind == "symbol"
        assert after.fact.file_path == JOURNEY_FILE_PATH
        for key, value in JOURNEY_CORRECTION.items():
            assert after.fact.properties.get(key) == value, f"correction key {key!r} not reflected by lookup"
        assert after.fact.properties != record.properties

        # Stored properties, read directly from the graph rather than via the adapter.
        stored_rows = await cypher(
            "stored property readback failed",
            "MATCH (n {id: $node_id}) WHERE n.repo = $scope "
            "RETURN n.id AS id, n.name AS name, properties(n) AS properties",
            {"node_id": str(primary_node_id), "scope": primary_scope.label},
        )
        assert len(stored_rows) == 1, "corrected fact must exist exactly once in its scope"
        stored_id, stored_name, stored_props = dialect.node_row(stored_rows[0])
        assert stored_id == str(primary_node_id)
        assert stored_name == JOURNEY_SYMBOL_NAME
        assert stored_props.get("enola_id") == fact_id
        assert stored_props.get("file_path") == JOURNEY_FILE_PATH
        assert stored_props.get("repo") == primary_scope.label
        stored_fact_props = stored_props.get("fact_properties")
        assert isinstance(stored_fact_props, dict)
        for key, value in JOURNEY_CORRECTION.items():
            assert stored_fact_props.get(key) == value, f"correction key {key!r} not stored on the node"
        # The correction touches only the fact properties: original keys survive, the
        # update is merged in, and no node metadata is nested into the mapping.
        assert stored_fact_props == {**JOURNEY_ORIGINAL_PROPS, **JOURNEY_CORRECTION}
        assert after.fact.properties == {**JOURNEY_ORIGINAL_PROPS, **JOURNEY_CORRECTION}
        for metadata_key in ("fact_properties", "enola_id", "repo", "kind", "file_path", "name", "id"):
            assert metadata_key not in stored_fact_props, f"{metadata_key} nested into fact_properties"

        # Correction replaces the node in place: still exactly one match, same scope size,
        # and every edge incident to the node (the part_of anchor edge included) is back.
        requery = await _await_redacted(
            "query (after correction) failed",
            adapter.query(**primary_kwargs(), query_text=JOURNEY_SYMBOL_NAME),
            secrets,
        )
        assert requery.total_matched == 1
        assert requery.facts[0].node_id == primary_node_id
        assert await scope_node_count(primary_scope) == primary_count_before
        assert await incident_edge_keys(primary_node_id) == primary_edges_before

        control_after_correction = await _await_redacted(
            "lookup (control, after correction) failed",
            adapter.lookup(**control_kwargs(), fact_id=fact_id),
            secrets,
        )
        assert control_after_correction.found is True
        assert control_after_correction.fact is not None
        assert control_after_correction.fact.properties == JOURNEY_ORIGINAL_PROPS
        assert await scope_node_count(control_scope) == control_count_before

        # ------------------------------------------------------------------
        # 4d. Retire removes only the primary snapshot.
        # ------------------------------------------------------------------
        retired = await _await_redacted(
            "retire (primary) failed",
            adapter.retire(**primary_kwargs()),
            secrets,
        )
        assert retired.success is True
        assert retired.snapshot_id == primary_snapshot_id
        # nodes_deleted covers the fact node and the repository anchor.
        assert retired.nodes_deleted == primary_count_before == 2

        gone_query = await _await_redacted(
            "query (primary, after retire) failed",
            adapter.query(**primary_kwargs(), query_text=JOURNEY_SYMBOL_NAME),
            secrets,
        )
        assert gone_query.total_matched == 0
        gone_all = await _await_redacted(
            "query (primary, wildcard after retire) failed",
            adapter.query(**primary_kwargs(), query_text="*"),
            secrets,
        )
        assert gone_all.total_matched == 0
        gone_lookup = await _await_redacted(
            "lookup (primary, after retire) failed",
            adapter.lookup(**primary_kwargs(), fact_id=fact_id),
            secrets,
        )
        assert gone_lookup.found is False
        assert await scope_node_count(primary_scope) == 0
        # The fact node and the repository anchor are gone outright, not merely
        # hidden behind the scope filter.
        assert await count_by_id(primary_node_id) == 0
        assert await count_by_id(primary_scope.repo_node_id()) == 0, "repository anchor must be retired by id"
        # The control snapshot's anchor is untouched.
        assert await count_by_id(control_scope.repo_node_id()) == 1

        # Control snapshot: same fact id, same workspace / repository, untouched.
        control_survivor = await _await_redacted(
            "lookup (control, after primary retire) failed",
            adapter.lookup(**control_kwargs(), fact_id=fact_id, file_path=JOURNEY_FILE_PATH),
            secrets,
        )
        assert control_survivor.found is True
        assert control_survivor.fact is not None
        assert control_survivor.fact.fact_id == fact_id
        assert control_survivor.fact.node_id == control_node_id
        assert control_survivor.fact.properties == JOURNEY_ORIGINAL_PROPS
        control_query = await _await_redacted(
            "query (control, after primary retire) failed",
            adapter.query(**control_kwargs(), query_text=JOURNEY_SYMBOL_NAME),
            secrets,
        )
        assert control_query.total_matched == 1
        assert await scope_node_count(control_scope) == control_count_before

        # ------------------------------------------------------------------
        # 4e. Retire the control snapshot too, leaving the shared backend clean.
        # ------------------------------------------------------------------
        control_retired = await _await_redacted(
            "retire (control) failed",
            adapter.retire(**control_kwargs()),
            secrets,
        )
        assert control_retired.success is True
        assert control_retired.nodes_deleted == control_count_before == 2
        assert await scope_node_count(control_scope) == 0
        assert await count_by_id(control_scope.repo_node_id()) == 0
    finally:
        # Safety net for a failed run: retire both scopes so nothing written by this
        # journey outlives it on the shared backend. Errors here must not mask the
        # original failure, so they are deliberately swallowed.
        for scope_kwargs in (primary_kwargs(), control_kwargs()):
            try:
                await adapter.retire(**scope_kwargs)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
