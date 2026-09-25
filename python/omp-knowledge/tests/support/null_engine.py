from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from omp_work.knowledge_contracts import ProviderRoute
from omp_work.v1.canonical import sha256

from omp_knowledge.engine.cognee_adapter import compute_graph_sha256
from omp_knowledge.engine.protocol import (
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
from omp_knowledge.errors import EngineUnavailableError, SnapshotNotPublishedError


class NullEngine(KnowledgeEngine):
    """In-memory test double ONLY for unit tests when Cognee is unavailable.
    Structurally located under tests/support/ so it is never part of production src/.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.snapshots: dict[str, list[dict[str, Any]]] = {}
        self.published_snapshots: set[str] = set()
        self._route = ProviderRoute(
            provider="null-double",
            provider_version="test",
            operations=("ingest", "query", "lookup", "correct", "status"),
        )

    def _ensure_available(self) -> None:
        if not self.available:
            raise EngineUnavailableError("engine_unavailable: NullEngine test double marked unavailable")

    def publish(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        self.published_snapshots.add(key)
        return True

    def is_published(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        return key in self.published_snapshots

    def abort(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        self.published_snapshots.discard(key)
        return True

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
        self._ensure_available()
        sid = snapshot_id or getattr(snapshot_ref, "snapshot_id", str(snapshot_ref))
        node_ids = [str(f.get("id") or getattr(f, "fact_id", "")) for f in facts]
        return IngestPlan(
            snapshot_id=sid,
            node_ids=tuple(node_ids),
            edge_keys=(),
            graph_sha256=compute_graph_sha256(node_ids, ()),
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
        self._ensure_available()
        sid = snapshot_id or getattr(snapshot_ref, "snapshot_id", str(snapshot_ref))
        key = f"{workspace_id}:{repository_id}@{sid}"
        normalized_facts = []
        node_ids = []
        for f in facts:
            if isinstance(f, dict):
                normalized_facts.append(dict(f))
                node_ids.append(str(f.get("id") or f.get("fact_id") or uuid4()))
            else:
                data = {
                    "id": getattr(f, "fact_id", str(uuid4())),
                    "name": getattr(f, "name", ""),
                    "kind": getattr(f, "kind", "symbol"),
                    "file": getattr(f, "file", None),
                    "line": getattr(f, "line", None),
                    "end_line": getattr(f, "end_line", None),
                    "props": getattr(f, "properties", None) or getattr(f, "props", {}),
                }
                normalized_facts.append(data)
                node_ids.append(data["id"])

        self.snapshots[key] = normalized_facts
        g_hash = compute_graph_sha256(node_ids, ())

        return IngestResult(
            snapshot_id=sid,
            nodes_written=len(facts),
            edges_written=0,
            node_ids=tuple(node_ids),
            edge_keys=(),
            graph_sha256=g_hash,
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
        self._ensure_available()
        if require_published and not self.is_published(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ):
            raise SnapshotNotPublishedError(snapshot_id)

        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        facts = self.snapshots.get(key, [])
        matching: list[FactRecord] = []
        query_lower = query_text.lower()

        for f in facts:
            name = str(f.get("name", ""))
            kind = str(f.get("kind", "symbol"))
            if query_text == "*" or query_lower in name.lower() or query_lower in kind.lower():
                matching.append(
                    FactRecord(
                        node_id=uuid4(),
                        fact_id=str(f.get("id", "fact_id")),
                        name=name,
                        kind=kind,
                        file_path=f.get("file"),
                        line=f.get("line"),
                        end_line=f.get("end_line"),
                        properties=f.get("props") or {},
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
        self._ensure_available()
        if require_published and not self.is_published(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ):
            raise SnapshotNotPublishedError(snapshot_id)

        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        facts = self.snapshots.get(key, [])
        for f in facts:
            if f.get("id") == fact_id:
                if file_path and f.get("file") != file_path:
                    continue
                rec = FactRecord(
                    node_id=uuid4(),
                    fact_id=fact_id,
                    name=str(f.get("name", "")),
                    kind=str(f.get("kind", "symbol")),
                    file_path=f.get("file"),
                    line=f.get("line"),
                    end_line=f.get("end_line"),
                    properties=f.get("props") or {},
                    snapshot_id=snapshot_id,
                    repository_id=repository_id,
                    workspace_id=workspace_id,
                )
                return LookupResult(
                    fact=rec,
                    found=True,
                    snapshot_id=snapshot_id,
                    route=self._route,
                )

        return LookupResult(
            fact=None,
            found=False,
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
        self._ensure_available()
        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        facts = self.snapshots.get(key, [])
        for f in facts:
            if f.get("id") == fact_id:
                props = f.setdefault("props", {})
                props.update(properties_update)
                return CorrectResult(
                    snapshot_id=snapshot_id,
                    corrected_fact_id=fact_id,
                    success=True,
                    route=self._route,
                )
        return CorrectResult(
            snapshot_id=snapshot_id,
            corrected_fact_id=fact_id,
            success=False,
            route=self._route,
        )

    async def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> RetireResult:
        self._ensure_available()
        key = f"{workspace_id}:{repository_id}@{snapshot_id}"
        self.published_snapshots.discard(key)
        if key in self.snapshots:
            count = len(self.snapshots.pop(key))
            return RetireResult(
                snapshot_id=snapshot_id,
                nodes_deleted=count,
                edges_deleted=0,
                success=True,
            )
        return RetireResult(
            snapshot_id=snapshot_id,
            nodes_deleted=0,
            edges_deleted=0,
            success=False,
        )

    async def status(self) -> EngineStatus:
        return EngineStatus(
            available=self.available,
            engine_name="NullEngineDouble",
            version="test-double" if self.available else None,
            graph_engine="in-memory-test-double" if self.available else "unavailable",
            active_route=self._route,
            details={"is_test_double": True, "available": self.available},
        )


__all__ = ["NullEngine"]
