"""Unit tests for the adapter's write-path repairs, run against a fake graph seam.

No Cognee, Neo4j or PostgreSQL is needed. The fake graph engine answers exactly
the dialect-owned Cypher the adapter issues (``edges_query`` and the Neo4j
``capability_query``) and implements the supported ``get_node`` / ``delete_nodes``
/ ``add_edges`` calls; ``add_data_points`` is replaced by a recorder so the exact
DataPoint the adapter rebuilds can be inspected.

Covered:

* ``correct`` keeps top-level node fields, updates only a copy of the stored
  ``fact_properties`` and never nests node metadata into it;
* ``correct`` captures every incident edge (``part_of`` anchor edge, outgoing and
  incoming relation edges) before the delete and restores it through ``add_edges``;
* ``correct`` raises ``EngineError`` instead of reporting success when the
  restored edges do not read back;
* the external Neo4j route fails closed before any write when the server lacks
  ``apoc.create.addLabels`` / ``apoc.merge.relationship``, and the probe runs once;
* driver connection failures on the external route become
  ``EngineUnavailableError`` with redacted text and the original cause attached,
  other errors propagate unchanged, and the embedded route is not mapped at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine import cognee_adapter
from omp_knowledge.engine.backends import (
    REQUIRED_NEO4J_PROCEDURES,
    LadybugDialect,
    Neo4jDialect,
    SnapshotScope,
    is_backend_connection_failure,
)
from omp_knowledge.engine.cognee_adapter import RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineError, EngineUnavailableError
from support.fake_cognee import FakeCogneeConfig, install_fake_cognee, secret_file

FACT_ID = "0123456789abcdef0123456789abcdef"
SNAPSHOT_ID = "d" * 64
ORIGINAL_PROPS = {"symbol_kind": "class", "visibility": "public"}
CORRECTION = {"symbol_kind": "dataclass", "review_note": "unit correction"}


class FakePoint:
    """Records the keyword fields the adapter hands to the Cognee model class."""

    def __init__(self, **fields: Any) -> None:
        self.fields = dict(fields)
        self.id = fields["id"]
        self.name = fields.get("name")


class FakeGraphEngine:
    """In-memory graph answering the adapter's dialect-owned queries.

    Nodes are stored the way the embedded engine returns them from ``get_node``
    (``properties`` as one JSON string). Edges are ``(source, target, name, props)``.
    """

    def __init__(self, *, procedures: tuple[str, ...] = REQUIRED_NEO4J_PROCEDURES) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str, dict[str, Any]]] = []
        self.procedures = tuple(procedures)
        self.probe_calls = 0
        self.drop_edges_on_add = False
        self.fail_with: BaseException | None = None

    def seed_node(self, node_id: UUID, properties: dict[str, Any]) -> None:
        self.nodes[str(node_id)] = {
            "id": str(node_id),
            "name": properties.get("name"),
            "type": "CodeSymbol",
            # Cognee serializes DataPoint fields (UUID ids included) into one JSON column.
            "properties": json.dumps(properties, default=str),
        }

    def stored_properties(self, node_id: UUID) -> dict[str, Any]:
        return json.loads(self.nodes[str(node_id)]["properties"])

    def edge_keys(self) -> set[tuple[str, str, str]]:
        return {(source, target, name) for source, target, name, _ in self.edges}

    async def query(self, query: str, params: dict[str, Any]) -> list[Any]:
        if self.fail_with is not None:
            raise self.fail_with
        if query.startswith("SHOW PROCEDURES"):
            self.probe_calls += 1
            return [{"name": name} for name in self.procedures if name in params["names"]]
        node_id = params.get("node_id")
        ladybug_query, _ = LadybugDialect().edges_query(node_id)
        neo4j_query, _ = Neo4jDialect().edges_query(node_id)
        incident = [edge for edge in self.edges if node_id in (edge[0], edge[1])]
        if query == ladybug_query:
            return [(source, target, name, json.dumps(props)) for source, target, name, props in incident]
        if query == neo4j_query:
            return [
                {"source_id": source, "target_id": target, "relationship_name": name, "properties": dict(props)}
                for source, target, name, props in incident
            ]
        if query.startswith("MATCH (n) WHERE n.repo = $scope") or query.startswith("MATCH (n:Node)"):
            return []
        raise AssertionError(f"unexpected query: {query}")

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        if self.fail_with is not None:
            raise self.fail_with
        return self.nodes.get(node_id)

    async def delete_nodes(self, node_ids: list[str]) -> None:
        for node_id in node_ids:
            self.nodes.pop(node_id, None)
        self.edges = [edge for edge in self.edges if edge[0] not in node_ids and edge[1] not in node_ids]

    async def add_edges(self, edges: list[tuple[Any, Any, str, dict[str, Any]]]) -> None:
        if self.drop_edges_on_add:
            return
        for source, target, name, props in edges:
            self.edges.append((str(source), str(target), str(name), dict(props)))


def _cfg(tmp_path: Path, **overrides: object) -> KnowledgeConfig:
    return KnowledgeConfig(state_dir=tmp_path / "state", config_dir=tmp_path / "config", **overrides)


def _wire_graph_seam(monkeypatch: pytest.MonkeyPatch, engine: FakeGraphEngine) -> list[FakePoint]:
    """Point the adapter's Cognee seams at ``engine`` and return the recorded DataPoints."""
    written: list[FakePoint] = []

    async def fake_get_graph_engine() -> FakeGraphEngine:
        return engine

    async def fake_add_data_points(data_points: list[FakePoint], graph_only: bool = False) -> None:
        assert graph_only is True, "writes must stay graph_only"
        for point in data_points:
            written.append(point)
            engine.seed_node(point.id, point.fields)

    monkeypatch.setattr(cognee_adapter, "get_graph_engine", fake_get_graph_engine, raising=False)
    monkeypatch.setattr(cognee_adapter, "add_data_points", fake_add_data_points, raising=False)
    monkeypatch.setattr(cognee_adapter, "_get_model_class", lambda kind: FakePoint)
    return written


def _seed_fact(engine: FakeGraphEngine, scope: SnapshotScope) -> tuple[UUID, dict[str, Any]]:
    node_id = scope.fact_node_id(FACT_ID)
    stored = {
        "id": str(node_id),
        "name": "User",
        "kind": "symbol",
        "file_path": "src/models/user.py",
        "line": 10,
        "end_line": 50,
        "repo": scope.label,
        "enola_id": FACT_ID,
        "fact_properties": dict(ORIGINAL_PROPS),
        "symbol_kind": "class",
    }
    engine.seed_node(node_id, stored)
    return node_id, stored


def _seed_edges(engine: FakeGraphEngine, scope: SnapshotScope, node_id: UUID) -> set[tuple[str, str, str]]:
    anchor = str(scope.repo_node_id())
    module_node = str(scope.fact_node_id("module-fact"))
    caller_node = str(scope.fact_node_id("caller-fact"))
    engine.edges = [
        (str(node_id), anchor, "part_of", {"relationship_name": "part_of"}),
        (str(node_id), module_node, "declares", {"relationship_name": "declares", "snapshot_label": scope.label}),
        (caller_node, str(node_id), "calls", {"relationship_name": "calls", "snapshot_label": scope.label}),
    ]
    return engine.edge_keys()


def _external_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str) -> RealCogneeAdapter:
    install_fake_cognee(monkeypatch, FakeCogneeConfig())
    cfg = _cfg(
        tmp_path,
        graph_engine="neo4j",
        neo4j_uri="bolt://graph.internal:7687",
        neo4j_password_file=secret_file(tmp_path, "neo4j.secret", secret),
    )
    return RealCogneeAdapter(cfg, available=True)


def _scope() -> SnapshotScope:
    return SnapshotScope(workspace_id=uuid4(), repository_id=uuid4(), snapshot_id=SNAPSHOT_ID)


def _kwargs(scope: SnapshotScope) -> dict[str, Any]:
    return {
        "workspace_id": scope.workspace_id,
        "repository_id": scope.repository_id,
        "snapshot_id": scope.snapshot_id,
    }


# ==============================================================================
# correct(): property layout and edge preservation (embedded dialect)
# ==============================================================================


async def test_correct_updates_only_fact_properties_and_keeps_node_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    written = _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    node_id, before = _seed_fact(engine, scope)
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=True)

    result = await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))

    assert result.success is True
    assert result.corrected_fact_id == FACT_ID
    assert len(written) == 1
    fields = written[0].fields

    # Top-level node fields survive the rebuild unchanged.
    assert fields["id"] == node_id
    assert fields["name"] == before["name"]
    assert fields["kind"] == before["kind"]
    assert fields["file_path"] == before["file_path"]
    assert fields["line"] == before["line"]
    assert fields["end_line"] == before["end_line"]
    assert fields["repo"] == scope.label
    assert fields["enola_id"] == FACT_ID
    assert fields["symbol_kind"] == "dataclass"

    # fact_properties is the stored mapping plus the update, and nothing else.
    assert fields["fact_properties"] == {**ORIGINAL_PROPS, **CORRECTION}
    # Regression: no node metadata nested into the fact properties.
    for metadata_key in ("fact_properties", "enola_id", "repo", "kind", "file_path", "name", "id", "updated_at"):
        assert metadata_key not in fields["fact_properties"], f"{metadata_key} leaked into fact_properties"

    # The stored node reflects the same layout, and the original mapping was not mutated in place.
    stored = engine.stored_properties(node_id)
    assert stored["fact_properties"] == {**ORIGINAL_PROPS, **CORRECTION}
    assert stored["enola_id"] == FACT_ID
    assert before["fact_properties"] == ORIGINAL_PROPS


async def test_correct_preserves_incident_edges_through_add_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    node_id, _ = _seed_fact(engine, scope)
    expected = _seed_edges(engine, scope, node_id)
    assert len(expected) == 3
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=True)

    result = await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))

    assert result.success is True
    assert engine.edge_keys() == expected, "part_of, outgoing and incoming edges must all survive"
    # Direction and properties are restored verbatim.
    restored = {(source, target, name): props for source, target, name, props in engine.edges}
    assert restored[(str(node_id), str(scope.repo_node_id()), "part_of")] == {"relationship_name": "part_of"}
    assert restored[(str(scope.fact_node_id("caller-fact")), str(node_id), "calls")]["snapshot_label"] == scope.label


async def test_correct_fails_closed_when_edges_do_not_read_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    node_id, _ = _seed_fact(engine, scope)
    _seed_edges(engine, scope, node_id)
    engine.drop_edges_on_add = True
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=True)

    with pytest.raises(EngineError, match="3 of 3 edges did not read back"):
        await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))


async def test_correct_reports_missing_node_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    written = _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=True)

    result = await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))

    assert result.success is False
    assert written == []


# ==============================================================================
# External Neo4j route: APOC capability gate
# ==============================================================================


async def test_external_route_fails_closed_without_required_apoc_procedures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "apoc-probe-secret"
    engine = FakeGraphEngine(procedures=("apoc.merge.relationship",))
    written = _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    _seed_fact(engine, scope)
    adapter = _external_adapter(monkeypatch, tmp_path, secret)

    with pytest.raises(EngineUnavailableError) as excinfo:
        await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))

    message = str(excinfo.value)
    assert message.startswith("engine_unavailable:")
    assert "APOC" in message
    assert message.endswith("apoc.create.addLabels"), "only the absent procedure is reported"
    assert secret not in message
    assert written == [], "no write may happen before the capability probe passes"
    assert engine.nodes, "the existing node must not be deleted"


async def test_external_route_probe_passes_once_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    adapter = _external_adapter(monkeypatch, tmp_path, "probe-secret")

    first = await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))
    second = await adapter.correct(**_kwargs(scope), fact_id=FACT_ID, properties_update=dict(CORRECTION))

    assert first.success is False and second.success is False  # node absent; probe still ran
    assert engine.probe_calls == 1


# ==============================================================================
# Connection failure mapping
# ==============================================================================


class ServiceUnavailable(Exception):
    """Shaped like ``neo4j.exceptions.ServiceUnavailable`` without importing the driver."""


ServiceUnavailable.__module__ = "neo4j.exceptions"


class OperationalError(Exception):
    """Shaped like ``sqlalchemy.exc.OperationalError``."""


OperationalError.__module__ = "sqlalchemy.exc"


class CypherSyntaxError(Exception):
    """A reachable backend rejecting a query is not an outage."""


CypherSyntaxError.__module__ = "neo4j.exceptions"


def test_connection_failure_classification() -> None:
    assert is_backend_connection_failure(ServiceUnavailable("down"))
    assert is_backend_connection_failure(OperationalError("connection refused"))
    assert is_backend_connection_failure(ConnectionRefusedError())
    assert is_backend_connection_failure(TimeoutError())
    assert not is_backend_connection_failure(CypherSyntaxError("bad query"))
    assert not is_backend_connection_failure(ValueError("unrelated"))


async def test_external_route_maps_connection_failure_to_engine_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "conn-fail-secret"
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    adapter = _external_adapter(monkeypatch, tmp_path, secret)
    cause = ServiceUnavailable(f"Unable to retrieve routing information (auth {secret})")
    engine.fail_with = cause

    with pytest.raises(EngineUnavailableError) as excinfo:
        await adapter.query(**_kwargs(scope), query_text="*")

    message = str(excinfo.value)
    assert message.startswith("engine_unavailable:")
    assert "ServiceUnavailable" in message
    assert "***" in message
    assert secret not in message
    assert excinfo.value.__cause__ is cause


async def test_external_route_leaves_non_connection_errors_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    adapter = _external_adapter(monkeypatch, tmp_path, "other-secret")
    engine.fail_with = CypherSyntaxError("Invalid input")

    with pytest.raises(CypherSyntaxError):
        await adapter.query(**_kwargs(scope), query_text="*")


async def test_embedded_route_does_not_map_connection_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeGraphEngine()
    _wire_graph_seam(monkeypatch, engine)
    scope = _scope()
    adapter = RealCogneeAdapter(_cfg(tmp_path), available=True)
    engine.fail_with = ServiceUnavailable("embedded route error passes through")

    with pytest.raises(ServiceUnavailable):
        await adapter.query(**_kwargs(scope), query_text="*")
