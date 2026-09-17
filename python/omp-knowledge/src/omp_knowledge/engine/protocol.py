from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omp_work.knowledge_contracts import (
    FactRecord,
    ProviderRoute,
    SnapshotRef,
    SourceRef,
)


class EngineUnavailableError(Exception):
    def __init__(self, message: str = "engine_unavailable: Cognee or Ladybug dependencies not available") -> None:
        super().__init__(message)


class SemanticBindingError(EngineUnavailableError):
    """The semantic binding (config dimension, embedding engine, stored collections)
    is inconsistent: mock engine, vector-size or canary dimension mismatch, or an
    existing collection reflected with a different ``Vector(dim)``.

    Raised before any vector table or row is created. Unlike an inference or
    database outage it is never degraded by the optional embedding policy: a
    mismatched binding would write rows that can never be searched correctly.
    """


class RerankBindingError(SemanticBindingError):
    """The reranker binding is inconsistent: the configured ``reranking_model`` is
    not listed by the endpoint's ``/v1/models``, or a ``/v1/rerank`` response
    echoes a different model than the one requested.

    Raised before any candidate order is changed. Like ``SemanticBindingError``
    it is never degraded by the optional reranking policy: an answer from a
    different model than the one the receipt names would be a false receipt.
    """


class EngineTimeoutError(Exception):
    def __init__(self, message: str = "engine_timeout: engine operation timed out") -> None:
        super().__init__(message)


class EngineError(Exception):
    pass


class RerankContractError(EngineError):
    """The reranker answered, but not in the ``/v1/rerank`` contract the adapter
    speaks: a non-outage HTTP status (anything but 2xx / 429 / 502 / 503 / 504), a
    non-JSON or non-object body, a missing or mis-sized ``results`` list, an
    index that is missing, duplicated or out of range, or a score that is not a
    finite number.

    Never degraded by the optional reranking policy and never reported as
    ``fallback``: a reachable endpoint that speaks a different protocol is a
    configuration or data error, not an outage.
    """


class CorrectionFailedError(EngineError):
    """A ``correct`` call failed after the node had been deleted for its rebuild.

    The adapter always attempts to put the prior node and its incident edges back
    before raising. ``reverted`` is True only when that revert read back complete
    (node present, every captured edge present): the graph is then exactly as it
    was before the call and the correction can simply be retried. Otherwise the
    graph is in an explicit partial state described by ``node_present`` and
    ``missing_edges``; the correction is still retryable, and a retry that finds
    the node absent reports ``success=False`` instead of guessing. Never raised
    for a failure of the initial delete itself (nothing changed then, and the
    original exception propagates unchanged). ``cause_text`` / ``revert_error``
    are already redacted.
    """

    def __init__(
        self,
        *,
        fact_id: str,
        snapshot_id: str,
        phase: str,
        reverted: bool,
        node_present: bool,
        missing_edges: tuple[tuple[str, str, str], ...],
        expected_edges: int,
        cause_text: str,
        revert_error: str | None = None,
    ) -> None:
        self.fact_id = fact_id
        self.snapshot_id = snapshot_id
        self.phase = phase
        self.reverted = reverted
        self.node_present = node_present
        self.missing_edges = tuple(missing_edges)
        self.expected_edges = expected_edges
        self.cause_text = cause_text
        self.revert_error = revert_error
        self.retryable = True
        if reverted:
            state = (
                f"prior node and {expected_edges} incident edge(s) restored and verified; "
                "no correction applied; safe to retry"
            )
        else:
            missing = ", ".join(f"{s}-[{n}]->{t}" for s, t, n in self.missing_edges) or "none"
            state = (
                f"partial state left in the graph: node_present={node_present}, "
                f"missing {len(self.missing_edges)} of {expected_edges} incident edge(s): {missing}"
            )
            if revert_error:
                state += f"; revert failed: {revert_error}"
            state += "; retry required"
        super().__init__(
            f"engine_error: correction of fact {fact_id} in snapshot {snapshot_id} failed during "
            f"{phase}: {cause_text}; {state}"
        )


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


class SemanticIndexReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["indexed", "skipped", "partial", "disabled"]
    rows_requested: int = Field(ge=0)
    collections: tuple[str, ...] = ()
    model_id: str | None = None
    dimension: int | None = None
    reason: str | None = None


class RerankReport(BaseModel):
    """Receipt of the optional post-hydration reranking stage of one query.

    ``pre_rerank_order`` / ``post_rerank_order`` hold the fact node ids of the
    returned facts before and after the stage; they are always the same set in
    the same count (the stage is a permutation of the already scope-filtered,
    validity-filtered, hydrated, limit-cut candidate list and never adds or
    removes a candidate). ``scores`` are the endpoint's ``relevance_score``
    values aligned to ``post_rerank_order`` (empty unless ``applied``).
    ``request_sha256`` identifies the exact query / document pairs that were
    sent (``rerank_request_sha256`` in ``backends``), so a reviewer can recompute
    it from the returned facts. ``fallback`` carries the redacted outage text in
    ``reason``; ``not_attempted`` means fewer than two candidates existed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied", "fallback", "not_attempted"]
    provider: str
    model_id: str | None = None
    endpoint: str | None = None
    pair_format: Literal["rerank_v1/semantic_text"] = "rerank_v1/semantic_text"
    request_sha256: str | None = None
    document_count: int = Field(ge=0)
    pre_rerank_order: tuple[str, ...] = ()
    post_rerank_order: tuple[str, ...] = ()
    scores: tuple[float, ...] = ()
    reason: str | None = None


class RetrievalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["exact", "semantic", "semantic_empty", "exact_fallback"]
    model_id: str | None = None
    dimension: int | None = None
    scores: tuple[float | None, ...] = ()
    collections_searched: tuple[str, ...] = ()
    reason: str | None = None
    # Present only on routes with reranking enabled (and never for the ``*``
    # query, which bypasses ranking altogether). ``scores`` above are permuted
    # together with ``facts`` so they stay aligned after the stage.
    rerank: RerankReport | None = None


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    nodes_written: int = Field(ge=0)
    edges_written: int = Field(ge=0)
    node_ids: tuple[str, ...] = ()
    edge_keys: tuple[tuple[str, str, str], ...] = ()
    graph_sha256: str
    route: ProviderRoute
    semantic: SemanticIndexReport | None = None


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[FactRecord, ...] = ()
    total_matched: int = Field(ge=0)
    query: str
    snapshot_id: str
    route: ProviderRoute
    retrieval: RetrievalReport | None = None


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
    # Vector-row outcome on the semantic route, None on graph-only routes:
    # ``reindexed`` (row deleted and re-embedded from the corrected fact),
    # ``deleted`` (fact withdrawn; row deleted and not re-indexed), ``failed``
    # (optional-policy outage; graph correction succeeded, vector row not updated;
    # ``semantic_reason`` carries the redacted outage text).
    semantic_status: Literal["reindexed", "deleted", "failed"] | None = None
    semantic_reason: str | None = None


class RetireResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    # Both counts come from graph readback: the stale node set and the edges
    # incident to it are read before ``delete_nodes`` and re-read afterwards, and
    # the reported numbers are the difference. A retire that leaves any of them
    # behind raises ``EngineError`` instead of reporting success.
    nodes_deleted: int = Field(ge=0)
    edges_deleted: int = Field(ge=0)
    success: bool
    # Semantic route only. ``vector_rows_deleted`` is the number of rows that were
    # present before ``delete_data_points`` and absent afterwards (readback, never
    # a guess) and is None whenever the rows could not be checked:
    #   complete    every located row is gone;
    #   partial     rows still read back after the delete (count is the real
    #               difference; required policy raises instead);
    #   degraded    optional policy and the vector/embedding backend was
    #               unreachable, so nothing was deleted or counted;
    #   not_checked no stale graph node existed, so no row ids were looked up.
    vector_rows_deleted: int | None = None
    vector_cleanup: Literal["complete", "partial", "degraded", "not_checked"] | None = None
    vector_cleanup_reason: str | None = None


class EngineStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    engine_name: str
    version: str | None = None
    graph_engine: str
    active_route: ProviderRoute
    details: dict[str, Any] = Field(default_factory=dict)


class KnowledgeEngine(Protocol):
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
        """Pure preflight for ingest_snapshot: same inputs, same node ids, edge keys and
        graph_sha256, but no engine writes. Must be derived from the same code path as
        ingest_snapshot so the two cannot drift.
        """
        ...

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
        ...

    async def ingest_records(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        records: list[FactRecord],
        source_ref: SourceRef,
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

    async def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> RetireResult:
        ...

    async def status(self) -> EngineStatus:
        ...
