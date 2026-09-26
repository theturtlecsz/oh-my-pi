from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import NAMESPACE_OID, UUID, uuid5

from pydantic import ValidationError

from omp_work.knowledge_contracts import ProviderRoute
from omp_work.v1.canonical import sha256

from ..config import KnowledgeConfig
from ..errors import EngineUnavailableError
from ..publication import PublicationManager
from .protocol import (
    CorrectResult,
    EngineStatus,
    FactRecord,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RetireResult,
)

# Optional Cognee imports - flagged so lack of Cognee raises EngineUnavailableError
try:
    from cognee.infrastructure.engine.models.DataPoint import DataPoint
    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine
    from cognee.tasks.storage.add_data_points import add_data_points
    from cognee.tasks.code_graph.models import (
        ApiEndpoint,
        CodeAssociation,
        CodeExtractionAccount,
        CodeFileReference,
        CodeInsight,
        CodeIntent,
        CodeLintFinding,
        CodeModule,
        CodeRepository,
        CodeService,
        CodeSymbol,
        CodeTestReference,
        ExternalDependency,
        StorageResource,
    )

    COGNEE_AVAILABLE = True
except ImportError:
    COGNEE_AVAILABLE = False
    DataPoint = Any  # type: ignore[misc,assignment]


def compute_graph_sha256(node_ids: Iterable[str], edge_keys: Iterable[tuple]) -> str:
    """Compute deterministic canonical hash of graph state."""
    payload = {
        "nodes": sorted(set(node_ids)),
        "edges": sorted([list(e) for e in edge_keys]),
    }
    return sha256(payload)


def snapshot_fact_node_id(snapshot_label: str, fact_id: str) -> UUID:
    """Deterministic node UUID that preserves the full original Enola fact.id
    under the snapshot-isolated namespace.
    """
    key = f"omp-fact:{snapshot_label}:{fact_id}"
    return uuid5(NAMESPACE_OID, key)


def snapshot_repo_node_id(snapshot_label: str, repo: str) -> UUID:
    key = f"omp-repo:{snapshot_label}:{repo}"
    return uuid5(NAMESPACE_OID, key)


def _get_model_class(kind: str) -> Any:
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
    return models_map.get(kind, CodeSymbol)


class RealCogneeAdapter(KnowledgeEngine):
    """Pinned Cognee adapter using embedded Ladybug graph engine and LanceDB.
    Provides snapshot-isolated code graph ingestion, scoped query, exact lookup,
    correction, and processing status.
    """

    def __init__(self, config: KnowledgeConfig, *, available: bool | None = None) -> None:
        self.config = config
        self._available = COGNEE_AVAILABLE if available is None else available
        self._route = ProviderRoute(
            provider="cognee",
            provider_version="1.5.4",
            operations=("ingest", "query", "lookup", "correct", "status"),
        )
        self.publications = PublicationManager(config.state_dir)
        self._configure_env()

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
            from cognee.base_config import get_base_config
            from cognee.infrastructure.databases.graph.config import get_graph_config

            get_base_config.cache_clear()
            get_graph_config.cache_clear()

    def _ensure_available(self) -> None:
        if not self._available:
            raise EngineUnavailableError(
                "engine_unavailable: cognee and ladybug packages must be installed"
            )

    def publish(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Mark snapshot as published."""
        return self.publications.publish(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

    def is_published(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Check whether snapshot is published."""
        return self.publications.is_published(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

    def abort(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Abort snapshot publication, ensuring it remains unpublished."""
        return self.publications.abort(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

    def _extract_fact_fields(self, fact: Any) -> tuple[str, str, str, str | None, int | None, int | None, dict[str, Any], list[dict[str, Any]]]:
        if isinstance(fact, dict):
            kind = str(fact.get("kind", "symbol"))
            name = str(fact.get("name", ""))
            fact_id = str(fact.get("id") or fact.get("fact_id") or "")
            file_path = fact.get("file") or fact.get("file_path")
            line = fact.get("line")
            end_line = fact.get("end_line")
            props = dict(fact.get("props") or fact.get("properties") or {})
            relations = list(fact.get("relations") or [])
        else:
            kind = getattr(fact, "kind", "symbol")
            name = getattr(fact, "name", "")
            fact_id = getattr(fact, "fact_id", "")
            file_path = getattr(fact, "file", None) or getattr(fact, "file_path", None)
            line = getattr(fact, "line", None)
            end_line = getattr(fact, "end_line", None)
            props = dict(getattr(fact, "properties", None) or getattr(fact, "props", None) or {})
            relations = list(getattr(fact, "relations", None) or [])
        return kind, name, fact_id, file_path, line, end_line, props, relations

    def _build_snapshot_graph(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        facts: list[Any],
    ) -> tuple[list[DataPoint], list[str], list[tuple], list[tuple[str, str, str]], str]:
        """Deterministic graph projection shared by plan_snapshot and ingest_snapshot."""
        self._ensure_available()

        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        repo_name = f"repo-{repository_id}"

        # 1. Primary repository node
        repo_node = CodeRepository(
            id=snapshot_repo_node_id(snapshot_label, repo_name),
            name=f"{repo_name}@{snapshot_id}",
            path=str(repository_id),
        )

        data_points: list[DataPoint] = [repo_node]
        node_ids: list[str] = [str(repo_node.id)]
        fact_id_to_node_id: dict[str, UUID] = {}
        # Name -> the fact ids that became graph nodes, built once per snapshot
        # so name-only relation targets resolve in O(1) instead of rescanning
        # every fact (and re-extracting its fields) per relation.
        name_to_fact_ids: dict[str, list[str]] = {}

        # 2. Map facts to DataPoints preserving full fact.id
        for fact in facts:
            kind, name, fact_id, file_path, line, end_line, props, _ = self._extract_fact_fields(fact)
            if not kind or not name or not fact_id:
                continue

            model_cls = _get_model_class(kind)
            if model_cls is None:
                continue

            node_id = snapshot_fact_node_id(snapshot_label, fact_id)
            fact_id_to_node_id[fact_id] = node_id
            name_to_fact_ids.setdefault(name, []).append(fact_id)
            node_ids.append(str(node_id))

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

            try:
                data_points.append(model_cls(**fields))
            except ValidationError:
                continue

        # 3. Build edges resolving original full fact IDs
        edges: list[tuple] = []
        edge_keys: list[tuple[str, str, str]] = []

        for fact in facts:
            _, _, fact_id, _, _, _, _, relations = self._extract_fact_fields(fact)
            if not fact_id or fact_id not in fact_id_to_node_id:
                continue

            source_id = fact_id_to_node_id[fact_id]
            for relation in relations:
                rel_kind = relation.get("kind") or relation.get("relationship") or "related_to"
                target_id = relation.get("target_id")

                target_node_id: UUID | None = None
                if target_id and target_id in fact_id_to_node_id:
                    target_node_id = fact_id_to_node_id[target_id]
                else:
                    target_name = relation.get("target")
                    if target_name:
                        matching_fact_ids = name_to_fact_ids.get(target_name, ())
                        if len(matching_fact_ids) == 1:
                            target_node_id = fact_id_to_node_id[matching_fact_ids[0]]

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

        graph_hash = compute_graph_sha256(node_ids, edge_keys)
        return data_points, node_ids, edges, edge_keys, graph_hash

    async def plan_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str | None = None,
        facts: list[Any],
        receipt: dict[str, Any] | None = None,
        insights: list[dict[str, Any]] | None = None,
        snapshot_ref: Any = None,
        **kwargs: Any,
    ) -> IngestPlan:
        sid = snapshot_id or getattr(snapshot_ref, "snapshot_id", str(snapshot_ref))
        _, node_ids, _, edge_keys, graph_hash = self._build_snapshot_graph(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=sid,
            facts=facts,
        )
        return IngestPlan(
            snapshot_id=sid,
            node_ids=tuple(node_ids),
            edge_keys=tuple(edge_keys),
            graph_sha256=graph_hash,
        )

    async def ingest_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str | None = None,
        facts: list[Any],
        receipt: dict[str, Any] | None = None,
        insights: list[dict[str, Any]] | None = None,
        snapshot_ref: Any = None,
        **kwargs: Any,
    ) -> IngestResult:
        """Ingest facts into the embedded Cognee code graph."""
        self._ensure_available()
        sid = snapshot_id or getattr(snapshot_ref, "snapshot_id", str(snapshot_ref))

        data_points, node_ids, edges, edge_keys, graph_hash = self._build_snapshot_graph(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=sid,
            facts=facts,
        )

        await add_data_points(data_points, graph_only=True)

        if edges:
            graph_engine = await get_graph_engine()
            await graph_engine.add_edges(edges)

        return IngestResult(
            snapshot_id=sid,
            nodes_written=len(data_points),
            edges_written=len(edges),
            node_ids=tuple(node_ids),
            edge_keys=tuple(edge_keys),
            graph_sha256=graph_hash,
            route=self._route,
        )

    async def query(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        query_text: str,
        limit: int = 20,
        require_published: bool = False,
    ) -> QueryResult:
        """Scoped query across snapshot facts in the graph engine."""
        self._ensure_available()
        if require_published:
            self.publications.verify_published(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
            )

        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        graph_engine = await get_graph_engine()
        query_str = (
            "MATCH (n:Node) "
            "WHERE json_extract(n.properties, '$.repo') = $scope_json "
            "RETURN n.id, n.name, n.properties"
        )
        scope_json = json.dumps(snapshot_label)
        rows = await graph_engine.query(query_str, {"scope_json": scope_json})

        matching: list[FactRecord] = []
        query_lower = query_text.lower()

        for row in rows:
            node_id = row[0]
            name = row[1]
            raw_props = row[2]
            if not raw_props:
                continue
            props = json.loads(raw_props) if isinstance(raw_props, str) else raw_props
            if not isinstance(props, dict):
                continue
            fact_props = props.get("fact_properties") or {}
            if not isinstance(fact_props, dict):
                fact_props = {}
            if (
                props.get("withdrawn")
                or fact_props.get("withdrawn")
                or props.get("status") == "withdrawn"
                or props.get("withdrawn_by")
            ):
                continue

            kind = str(props.get("kind", ""))
            if query_text == "*" or (name and query_lower in str(name).lower()) or query_lower in kind.lower():
                fact_id = props.get("enola_id") or str(node_id)
                matching.append(
                    FactRecord(
                        node_id=UUID(str(node_id)),
                        fact_id=str(fact_id),
                        name=name,
                        kind=kind,
                        file_path=props.get("file_path"),
                        line=props.get("line"),
                        end_line=props.get("end_line"),
                        properties=fact_props,
                        snapshot_id=snapshot_id,
                        repository_id=repository_id,
                        workspace_id=workspace_id,
                    )
                )
                if len(matching) >= limit:
                    break

        return QueryResult(
            facts=tuple(matching),
            total_matched=len(matching),
            query=query_text,
            snapshot_id=snapshot_id,
            route=self._route,
        )

    async def lookup(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        file_path: str | None = None,
        require_published: bool = False,
    ) -> LookupResult:
        """Exact lookup of a fact in the snapshot code graph."""
        self._ensure_available()
        if require_published:
            self.publications.verify_published(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
            )

        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        expected_node_id = snapshot_fact_node_id(snapshot_label, fact_id)

        graph_engine = await get_graph_engine()
        query_str = (
            "MATCH (n:Node) "
            "WHERE n.id = $node_id AND json_extract(n.properties, '$.repo') = $scope_json "
            "RETURN n.id, n.name, n.properties"
        )
        scope_json = json.dumps(snapshot_label)
        rows = await graph_engine.query(
            query_str,
            {"node_id": str(expected_node_id), "scope_json": scope_json},
        )

        if not rows:
            return LookupResult(
                fact=None,
                found=False,
                snapshot_id=snapshot_id,
                route=self._route,
            )

        row = rows[0]
        node_id = row[0]
        name = row[1]
        raw_props = row[2]
        props = json.loads(raw_props) if isinstance(raw_props, str) else (raw_props or {})
        fact_props = props.get("fact_properties") or {}
        if not isinstance(fact_props, dict):
            fact_props = {}
        if (
            props.get("withdrawn")
            or fact_props.get("withdrawn")
            or props.get("status") == "withdrawn"
            or props.get("withdrawn_by")
        ):
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
            properties=fact_props,
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

    async def correct(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        properties_update: dict[str, Any],
    ) -> CorrectResult:
        """Apply a correction/update to a fact's properties in the graph."""
        self._ensure_available()
        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
        node_id = snapshot_fact_node_id(snapshot_label, fact_id)

        graph_engine = await get_graph_engine()
        node = await graph_engine.get_node(str(node_id))
        if not isinstance(node, dict):
            return CorrectResult(
                snapshot_id=snapshot_id,
                corrected_fact_id=fact_id,
                success=False,
                route=self._route,
            )

        await graph_engine.delete_nodes([str(node_id)])
        props = node.get("properties") or {}
        if isinstance(props, str):
            props = json.loads(props)
        props.update(properties_update)
        props["updated_at"] = datetime.now(timezone.utc).isoformat()
        fact_props = props.get("fact_properties") or {}
        if isinstance(fact_props, dict):
            fact_props.update(properties_update)
            props["fact_properties"] = fact_props

        model_cls = _get_model_class(props.get("kind", "symbol")) or CodeSymbol
        updated_point = model_cls(
            id=node_id,
            name=props.get("name", fact_id),
            kind=props.get("kind", "symbol"),
            file_path=props.get("file_path"),
            line=props.get("line"),
            end_line=props.get("end_line"),
            repo=snapshot_label,
            enola_id=fact_id,
            fact_properties=props,
        )
        await add_data_points([updated_point], graph_only=True)

        return CorrectResult(
            snapshot_id=snapshot_id,
            corrected_fact_id=fact_id,
            success=True,
            route=self._route,
        )

    async def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> RetireResult:
        """Retire a snapshot by removing its nodes from the graph and marking it retired."""
        self._ensure_available()
        snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"

        graph_engine = await get_graph_engine()
        query_str = (
            "MATCH (n:Node) "
            "WHERE json_extract(n.properties, '$.repo') = $scope_json "
            "RETURN n.id"
        )
        scope_json = json.dumps(snapshot_label)
        rows = await graph_engine.query(query_str, {"scope_json": scope_json})

        stale_node_ids = [str(row[0]) for row in rows]
        if stale_node_ids:
            await graph_engine.delete_nodes(stale_node_ids)

        self.publications.retire(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )

        return RetireResult(
            snapshot_id=snapshot_id,
            nodes_deleted=len(stale_node_ids),
            edges_deleted=0,
            success=True,
        )

    async def status(self) -> EngineStatus:
        """Report engine status and supported routes."""
        details = {
            "cognee_available": self._available,
            "graph_engine": "ladybug",
            "embedded": True,
            "vector_indexing": False,
        }
        return EngineStatus(
            available=self._available,
            engine_name="RealCogneeAdapter",
            version="1.5.4" if self._available else None,
            graph_engine="ladybug==0.19.0",
            active_route=self._route,
            details=details,
        )


__all__ = [
    "COGNEE_AVAILABLE",
    "RealCogneeAdapter",
    "compute_graph_sha256",
    "snapshot_fact_node_id",
    "snapshot_repo_node_id",
]
