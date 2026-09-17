from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from omp_knowledge.engine.protocol import (
    CorrectResult,
    EngineStatus,
    EngineUnavailableError,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RetireResult,
)
from omp_work.knowledge_contracts import (
    FactRecord,
    ProviderRoute,
    SnapshotRef,
    SourceRef,
)
from omp_knowledge.staging.manifest import compute_graph_sha256
from omp_work.v1.canonical import sha256


class NullEngine(KnowledgeEngine):
    """Test double ONLY for unit tests of storage/auth/HTTP layers when Cognee is absent.
    Structurally placed under tests/support/ so it cannot be imported by production src/.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.snapshots: dict[str, list[dict[str, Any]]] = {}
        self._route = ProviderRoute(
            role="graph_engine",
            provider="null-double",
            model_id=None,
            endpoint=None,
            graph_only=True,
            model_inferred=False,
            active=available,
        )

    def _ensure_available(self) -> None:
        if not self.available:
            raise EngineUnavailableError("engine_unavailable: NullEngine test double marked unavailable")

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
        self._ensure_available()
        node_ids = [str(f.get("id")) for f in facts if f.get("id")]
        return IngestPlan(
            snapshot_id=snapshot_ref.snapshot_id,
            node_ids=tuple(node_ids),
            edge_keys=(),
            graph_sha256=compute_graph_sha256(node_ids, ()),
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
        plan = await self.plan_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_ref=snapshot_ref,
            facts=facts,
            receipt=receipt,
            insights=insights,
        )
        self.snapshots[plan.snapshot_id] = list(facts)
        return IngestResult(
            snapshot_id=plan.snapshot_id,
            nodes_written=len(facts),
            edges_written=0,
            node_ids=plan.node_ids,
            edge_keys=plan.edge_keys,
            graph_sha256=plan.graph_sha256,
            route=self._route,
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
        facts = [r.model_dump(mode="json") for r in records]
        self.snapshots[snapshot_id] = facts
        node_ids = [str(r.node_id) for r in records]
        g_hash = compute_graph_sha256(node_ids, ())
        return IngestResult(
            snapshot_id=snapshot_id,
            nodes_written=len(records),
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
    ) -> QueryResult:
        self._ensure_available()
        facts = self.snapshots.get(snapshot_id, [])
        matching: list[FactRecord] = []
        for f in facts:
            name = str(f.get("name", ""))
            if query_text.lower() in name.lower() or query_text == "*":
                matching.append(
                    FactRecord(
                        node_id=uuid4(),
                        fact_id=str(f.get("id", "fact_id")),
                        name=name,
                        kind=str(f.get("kind", "symbol")),
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
    ) -> LookupResult:
        self._ensure_available()
        facts = self.snapshots.get(snapshot_id, [])
        for f in facts:
            if f.get("id") == fact_id:
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
        facts = self.snapshots.get(snapshot_id, [])
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
        if snapshot_id in self.snapshots:
            count = len(self.snapshots.pop(snapshot_id))
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
