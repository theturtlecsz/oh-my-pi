from __future__ import annotations

import sys
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .v1.canonical import canonical_json, sha256
from .v1.models import (
    Candidate,
    EvidenceReceipt,
    StrictModel,
    WorkRevision,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

KNOWLEDGE_CONTRACT_VERSION = "knowledge.omp.dev/v1"
SNAPSHOT_HASH_ALGORITHM = "knowledge.omp.dev/v1/snapshot-sha256"
# v2: the encoding tag is itself a key of the hashed identity payload, so bundles compiled
# under an older representation keep their historical bundle_id and are never reconstructed.
CONTEXT_BUNDLE_IDENTITY_ENCODING = "omp-context-bundle-identity/v2"


class RepositoryAliasKind(StrEnum):
    CANONICAL_REMOTE = "canonical_remote"
    ROOT_COMMIT = "root_commit"
    WORKTREE_COMMON_DIR = "worktree_common_dir"


class RepositoryBindingState(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"


class RepositoryAlias(StrictModel):
    kind: RepositoryAliasKind
    value: str = Field(min_length=1)
    verified_by: str = Field(min_length=1)
    verified_at: datetime


class RepositoryBinding(StrictModel):
    """Binds a knowledge-service repository UUID to an authoritative native repository UUID.
    Unbound repositories hold observations/snapshots but cannot participate in cross-repository
    retrieval or native acceptance.
    """

    repository_id: UUID
    native_repository_id: UUID | None = None
    state: RepositoryBindingState = RepositoryBindingState.UNBOUND
    aliases: tuple[RepositoryAlias, ...] = ()
    bound_at: datetime | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> RepositoryBinding:
        if self.state == RepositoryBindingState.BOUND and self.native_repository_id is None:
            raise ValueError("bound repositories must carry native_repository_id")
        if self.state == RepositoryBindingState.UNBOUND and self.native_repository_id is not None:
            raise ValueError("unbound repositories must not carry native_repository_id")
        return self


class SourceRef(StrictModel):
    """Full provenance reference for a source observation or fact."""

    workspace_id: UUID
    repository_id: UUID
    work_id: UUID | None = None
    run_id: str | None = None
    stage: str | None = None
    attempt_id: UUID | None = None
    revision_id: UUID | None = None
    revision_number: int | None = Field(default=None, ge=1)
    candidate_id: UUID | None = None
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_revision: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_locator: str | None = None
    producer: str = Field(min_length=1)
    observed_at: datetime
    native_validity_ref: str | None = None
    repository_bound: bool = False
    native_event_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    previous_event_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    sequence: int | None = Field(default=None, ge=0)


class ExtractorInfo(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    binary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ExcludedPath(StrictModel):
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ExtractionCoverage(StrictModel):
    files_seen: int = Field(ge=0)
    files_parsed: int = Field(ge=0)
    files_skipped: int = Field(ge=0)
    parse_errors: int = Field(ge=0)
    unsupported_features: tuple[str, ...] = ()


class SnapshotRef(StrictModel):
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    tree_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidate_tree_sha: str | None = None
    included_untracked: tuple[str, ...] = ()
    excluded: tuple[ExcludedPath, ...] = ()
    extractor: ExtractorInfo
    coverage: ExtractionCoverage
    created_at: datetime


class ProviderRoute(StrictModel):
    role: Literal["graph_engine", "embedding", "reranking", "generation"]
    provider: str = Field(min_length=1)
    model_id: str | None = None
    endpoint: str | None = None
    graph_only: bool = True
    model_inferred: bool = False
    active: bool = True


class ObservationKind(StrEnum):
    EXECUTION_TRACE = "execution_trace"
    TEST_FAILURE = "test_failure"
    CODE_FACT = "code_fact"
    REVIEW_FINDING = "review_finding"
    TOOL_OUTPUT = "tool_output"
    HUMAN_FEEDBACK = "human_feedback"


class SourceObservation(StrictModel):
    """Raw observation captured from workflow execution or analysis, separately typed
    from derived procedure proposals.
    """

    observation_id: UUID
    source: SourceRef
    kind: ObservationKind
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_payload_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    relevance_tags: tuple[str, ...] = ()
    observed_at: datetime


class ProposalState(StrEnum):
    """Service-owned proposal states. Native accepted/disputed/withdrawn is NOT
    owned by this service.
    """

    PROPOSAL = "proposal"
    HISTORICAL_OBSERVATION = "historical_observation"


class ProposalAttribution(StrictModel):
    actor_id: UUID
    actor_kind: str
    generator: Literal["exact_facts", "model"]
    route: ProviderRoute | None = None


class EvidenceLineage(StrictModel):
    receipt_id: UUID
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID | None = None


class ApplicabilityResult(StrictModel):
    policy_id: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    acceptance: Literal["accepted", "not_accepted", "superseded_revision", "unverifiable"]
    reasons: tuple[str, ...] = ()
    native_readback_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProcedureProposal(StrictModel):
    """Derived engineering lesson or procedure proposal. Never an authoritative
    native acceptance claim.
    """

    proposal_id: UUID
    workspace_id: UUID
    repository_id: UUID
    state: ProposalState = ProposalState.PROPOSAL
    source_observation_ids: tuple[UUID, ...] = ()
    supporting_evidence: tuple[SourceRef, ...] = ()
    title: str = Field(min_length=1)
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = Field(min_length=1)
    expected_observations: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    applicability_scope: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    derived_at: datetime
    attribution: ProposalAttribution | None = None
    evidence_binding: str | None = None
    enrichment_status: str | None = None
    supporting_lineage: tuple[EvidenceLineage, ...] = ()
    invalidated_at: datetime | None = None
    invalidation_id: UUID | None = None


class BudgetSpec(StrictModel):
    method: Literal["utf8_bytes", "tokens"]
    limit: int = Field(gt=0)
    tokenizer_id: str | None = None


class BudgetActual(StrictModel):
    method: Literal["utf8_bytes", "tokens"]
    limit: int = Field(gt=0)
    used: int = Field(ge=0)
    mandatory_used: int = Field(ge=0)
    dropped_optional: tuple[str, ...] = ()


class ContextBundle(StrictModel):
    bundle_id: UUID
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    repository_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID | None = None
    stage: str = Field(min_length=1)
    snapshot_id: str | None = None
    mandatory: dict[str, Any] = Field(default_factory=dict)
    optional: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetActual
    enrichment_status: Literal["applied", "unavailable", "degraded"]
    proposal_lineage: tuple[UUID, ...] = ()
    receipt_lineage: tuple[UUID, ...] = ()
    excluded_proposal_ids: tuple[UUID, ...] = ()
    compiled_at: datetime
    identity_encoding: str | None = None
    identity_canonical_json: str | None = None
    # Exact canonical_json({"mandatory", "optional"}) bytes the compiler budgeted and
    # embedded in identity_canonical_json. Consumers inject these bytes verbatim; None on
    # legacy rows means the bundle is unverifiable.
    content_canonical_json: str | None = None


class ProposalSupportSummary(StrictModel):
    proposal_id: UUID
    uses: int = Field(ge=0)
    distinct_lineages: int = Field(ge=0)
    independent_support: int = Field(ge=0)
    same_source_task_uses: int = Field(ge=0)
    lineage_keys: tuple[str, ...] = ()


class KnowledgeUseOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not_evaluated"


class KnowledgeUseRecord(StrictModel):
    """Tracks whether a proposal was supplied to and used by a task, and the resulting
    independent outcome.
    """

    use_id: UUID
    proposal_id: UUID
    workspace_id: UUID
    repository_id: UUID
    task_work_id: UUID
    task_revision_id: UUID
    task_candidate_id: UUID | None = None
    supplied_at: datetime
    worker_used: bool = False
    outcome: KnowledgeUseOutcome = KnowledgeUseOutcome.NOT_EVALUATED
    outcome_receipt_id: UUID | None = None
    recorded_at: datetime
    bundle_id: UUID | None = None
    outcome_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome_lineage_key: str | None = None
    outcome_recorded_at: datetime | None = None
    independent: bool | None = None



class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NO_LESSON = "no_lesson"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"


class JobCheckpoint(StrictModel):
    checkpoint_id: UUID
    operation_id: UUID
    step_name: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class IngestionJob(StrictModel):
    operation_id: UUID
    canonical_operation_id: UUID | None = None
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str | None = None
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: JobState = JobState.QUEUED
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    attempts: int = Field(default=1, ge=1)
    owner_token: str | None = None
    conflict_count: int = Field(default=0, ge=0)
    diagnostics: tuple[str, ...] = Field(default=(), max_length=8)
    created_at: datetime
    updated_at: datetime


class PublicationStatus(StrEnum):
    STAGED = "staged"
    PUBLISHED = "published"
    FAILED = "failed"
    RETRACTED = "retracted"


class SnapshotPublicationReceipt(StrictModel):
    """Derived staging/publication receipt. Does NOT grant native authoritative acceptance."""

    publication_id: UUID
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    status: PublicationStatus = PublicationStatus.STAGED
    fact_count: int = Field(ge=0)
    insight_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_at: datetime
    published_at: datetime | None = None
    diagnostics: tuple[str, ...] = ()


class FactLookupKey(StrictModel):
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    fact_id: str = Field(min_length=1)
    file_path: str | None = None


class KnowledgeQuery(StrictModel):
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class FactRecord(StrictModel):
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


def compute_snapshot_sha256(
    *,
    repository_id: UUID,
    base_commit: str,
    tree_sha: str,
    included_untracked: Iterable[str],
    excluded: Iterable[ExcludedPath | dict[str, str]],
    extractor: ExtractorInfo | dict[str, Any],
) -> str:
    payload = {
        "algorithm": SNAPSHOT_HASH_ALGORITHM,
        "repository_id": str(repository_id),
        "base_commit": base_commit,
        "tree_sha": tree_sha,
        "included_untracked": sorted(included_untracked),
        "excluded": sorted(
            [
                e if isinstance(e, dict) else e.model_dump(mode="json")
                for e in excluded
            ],
            key=lambda x: x["path"],
        ),
        "extractor": (
            extractor
            if isinstance(extractor, dict)
            else extractor.model_dump(mode="json")
        ),
    }
    return sha256(payload)


def compute_proposal_sha256(proposal_data: dict[str, Any]) -> str:
    payload = {
        key: proposal_data[key]
        for key in sorted(proposal_data.keys())
        if key not in ("proposal_id", "proposal_sha256", "derived_at")
    }
    return sha256(payload)


def compute_ingestion_request_sha256(request_payload: dict[str, Any]) -> str:
    return sha256(request_payload)


def generate_knowledge_schema() -> dict[str, object]:
    module = sys.modules[__name__]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contract_version": KNOWLEDGE_CONTRACT_VERSION,
        "models": {
            name: model.model_json_schema()
            for name, model in vars(module).items()
            if isinstance(model, type)
            and hasattr(model, "model_json_schema")
            and issubclass(model, StrictModel)
        },
    }


__all__ = [
    "CONTEXT_BUNDLE_IDENTITY_ENCODING",
    "KNOWLEDGE_CONTRACT_VERSION",
    "SNAPSHOT_HASH_ALGORITHM",
    "ExcludedPath",
    "ExtractionCoverage",
    "ExtractorInfo",
    "FactLookupKey",
    "FactRecord",
    "IngestionJob",
    "JobCheckpoint",
    "JobState",
    "KnowledgeQuery",
    "KnowledgeUseOutcome",
    "KnowledgeUseRecord",
    "ObservationKind",
    "ProcedureProposal",
    "ProposalState",
    "ProviderRoute",
    "PublicationStatus",
    "ApplicabilityResult",
    "BudgetActual",
    "BudgetSpec",
    "ContextBundle",
    "EvidenceLineage",
    "ProposalAttribution",
    "ProposalSupportSummary",
    "RepositoryAlias",
    "RepositoryAliasKind",
    "RepositoryBinding",
    "RepositoryBindingState",
    "SnapshotPublicationReceipt",
    "SnapshotRef",
    "SourceObservation",
    "SourceRef",
    "compute_ingestion_request_sha256",
    "compute_proposal_sha256",
    "compute_snapshot_sha256",
    "generate_knowledge_schema",
]
