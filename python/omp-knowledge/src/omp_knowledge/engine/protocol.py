from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omp_work.knowledge_contracts import ProviderRoute, SourceRef

from ..errors import EngineTimeoutError, EngineUnavailableError


class FactRecord(BaseModel):
    """Normalized fact projection returned from knowledge queries and lookups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: UUID
    fact_id: str
    name: str
    kind: str
    file_path: str | None = None
    line: int | None = None
    end_line: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    snapshot_id: str
    repository_id: UUID
    workspace_id: UUID


class IngestPlan(BaseModel):
    """Preflight projection of an ingest: the exact node/edge identity and graph hash
    that ingest_snapshot would produce for the same inputs, computed without mutating
    engine state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    node_ids: tuple[str, ...] = ()
    edge_keys: tuple[tuple[str, str, str], ...] = ()
    graph_sha256: str


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    nodes_written: int = Field(ge=0)
    edges_written: int = Field(ge=0)
    node_ids: tuple[str, ...] = ()
    edge_keys: tuple[tuple[str, str, str], ...] = ()
    graph_sha256: str
    route: ProviderRoute


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[FactRecord, ...] = ()
    total_matched: int = Field(ge=0)
    query: str
    snapshot_id: str
    route: ProviderRoute


class LookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: FactRecord | None = None
    found: bool
    snapshot_id: str
    route: ProviderRoute


class CorrectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    corrected_fact_id: str
    success: bool
    route: ProviderRoute


class RetireResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    nodes_deleted: int = Field(ge=0)
    edges_deleted: int = Field(ge=0)
    success: bool


class EngineStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    engine_name: str
    version: str | None = None
    graph_engine: str
    active_route: ProviderRoute
    details: dict[str, Any] = Field(default_factory=dict)


class KnowledgeEngine(Protocol):
    """Abstract protocol for knowledge graph engines."""

    async def ingest_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        facts: list[dict[str, Any]],
        receipt: dict[str, Any] | None = None,
        insights: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> IngestResult:
        ...

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
        ...

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
        ...

    async def correct(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        fact_id: str,
        properties_update: dict[str, Any],
    ) -> CorrectResult:
        ...

    async def status(self) -> EngineStatus:
        ...

    async def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> RetireResult:
        ...


__all__ = [
    "CorrectResult",
    "EngineStatus",
    "EngineTimeoutError",
    "EngineUnavailableError",
    "FactRecord",
    "IngestPlan",
    "IngestResult",
    "KnowledgeEngine",
    "LookupResult",
    "QueryResult",
    "RetireResult",
]
