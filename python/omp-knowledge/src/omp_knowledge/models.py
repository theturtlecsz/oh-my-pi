from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omp_work.knowledge_contracts import (
    BudgetSpec,
    SnapshotRef,
    SourceObservation,
    SourceRef,
)


_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_canonical_snapshot_id(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("snapshot_id must be a string")
    bare = v[len("sha256:") :] if v.startswith("sha256:") else v
    if not _SNAPSHOT_ID_RE.fullmatch(bare):
        raise ValueError(
            f"Invalid snapshot_id {v!r}: must be 64 lowercase hexadecimal characters or prefixed with 'sha256:'"
        )
    return bare


CanonicalSnapshotId = Annotated[str, AfterValidator(validate_canonical_snapshot_id)]


def canonical_snapshot_ref(v: SnapshotRef) -> SnapshotRef:
    canon_id = validate_canonical_snapshot_id(v.snapshot_id)
    if v.snapshot_id != canon_id:
        data = v.model_dump()
        data["snapshot_id"] = canon_id
        return SnapshotRef.model_validate(data)
    return v


CanonicalSnapshotRef = Annotated[SnapshotRef, AfterValidator(canonical_snapshot_ref)]


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: UUID
    actor_kind: str
    workspaces: frozenset[UUID]
    scopes: frozenset[str]


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_relative_path_without_traversal(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute() or v.startswith("/") or v.startswith("\\"):
            raise ValueError(f"path must be relative: {v!r}")
        if ".." in p.parts:
            raise ValueError(f"path traversal not permitted: {v!r}")
        return v


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["omp_knowledge.source_manifest/v1"] = Field(
        default="omp_knowledge.source_manifest/v1", alias="schema"
    )
    files: tuple[SourceFile, ...] = ()
    locator: str = Field(pattern=r"^blob:sha256:[0-9a-f]{64}$")


class ArtifactLocators(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: str = Field(pattern=r"^blob:sha256:[0-9a-f]{64}$")
    receipt: str = Field(pattern=r"^blob:sha256:[0-9a-f]{64}$")
    insights: str = Field(pattern=r"^blob:sha256:[0-9a-f]{64}$")


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_name: Literal["omp_knowledge.snapshot_manifest/v1"] = Field(default="omp_knowledge.snapshot_manifest/v1", alias="schema")
    snapshot_ref: CanonicalSnapshotRef
    artifacts: ArtifactLocators
    source_manifest: SourceManifest | None = None
    receipt_output_hashes: dict[str, str] = Field(default_factory=dict)
    unretained_outputs: tuple[str, ...] = ()


class CodeSnapshotIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["code_snapshot"] = "code_snapshot"
    operation_id: UUID
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: CanonicalSnapshotId
    snapshot_ref: CanonicalSnapshotRef
    facts_jsonl: str
    receipt_json: str
    insights_json: str = "[]"
    source_manifest: SourceManifest | None = None
    manifest_locator: str | None = None

    @model_validator(mode="after")
    def validate_snapshot_ref_consistency(self) -> CodeSnapshotIngestRequest:
        if self.snapshot_id != self.snapshot_ref.snapshot_id:
            raise ValueError(
                f"snapshot_id ({self.snapshot_id}) must match snapshot_ref.snapshot_id ({self.snapshot_ref.snapshot_id})"
            )
        if self.repository_id != self.snapshot_ref.repository_id:
            raise ValueError(
                f"repository_id ({self.repository_id}) must match snapshot_ref.repository_id ({self.snapshot_ref.repository_id})"
            )
        return self


class NativeRecordIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["native_record"] = "native_record"
    operation_id: UUID
    workspace_id: UUID
    repository_id: UUID
    source_ref: SourceRef
    observation: SourceObservation

    @model_validator(mode="after")
    def validate_source_ref_and_observation_consistency(
        self,
    ) -> NativeRecordIngestRequest:
        if (
            self.source_ref.native_validity_ref is not None
            or self.source_ref.native_event_sha256 is not None
            or self.source_ref.previous_event_sha256 is not None
            or self.source_ref.sequence is not None
            or self.source_ref.producer.startswith("native_event_consumer")
            or self.observation.native_payload_sha256 is not None
            or self.observation.source.native_validity_ref is not None
            or self.observation.source.native_event_sha256 is not None
            or self.observation.source.previous_event_sha256 is not None
            or self.observation.source.sequence is not None
            or self.observation.source.producer.startswith("native_event_consumer")
        ):
            raise ValueError(
                "Client-supplied native lineage fields are forbidden on native_record ingest requests"
            )
        if self.workspace_id != self.source_ref.workspace_id:
            raise ValueError(
                f"workspace_id ({self.workspace_id}) must match source_ref.workspace_id ({self.source_ref.workspace_id})"
            )
        if self.repository_id != self.source_ref.repository_id:
            raise ValueError(
                f"repository_id ({self.repository_id}) must match source_ref.repository_id ({self.source_ref.repository_id})"
            )
        if self.workspace_id != self.observation.source.workspace_id:
            raise ValueError(
                f"workspace_id ({self.workspace_id}) must match observation.source.workspace_id ({self.observation.source.workspace_id})"
            )
        if self.repository_id != self.observation.source.repository_id:
            raise ValueError(
                f"repository_id ({self.repository_id}) must match observation.source.repository_id ({self.observation.source.repository_id})"
            )
        if self.observation.source != self.source_ref:
            raise ValueError("observation.source must match request source_ref")
        return self


IngestRequest = Annotated[
    Union[CodeSnapshotIngestRequest, NativeRecordIngestRequest],
    Field(discriminator="kind"),
]


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    repository_id: UUID
    snapshot_id: CanonicalSnapshotId


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: CanonicalSnapshotId


class RetireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: CanonicalSnapshotId


class ProposalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    repository_id: UUID
    title: str = Field(min_length=1)
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = Field(min_length=1)
    expected_observations: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    applicability_scope: str = Field(min_length=1)
    source_observation_ids: tuple[UUID, ...] = ()
    supporting_evidence: tuple[SourceRef, ...] = Field(min_length=1)
    generator: Literal["exact_facts", "model"] = "exact_facts"


class ApplicabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID | None = None
    policy_id: str = "native_acceptance"
    policy_version: int = Field(default=1, ge=1)


class ContextCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    repository_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID | None = None
    stage: str = Field(min_length=1)
    snapshot_id: CanonicalSnapshotId | None = None
    budget: BudgetSpec


class RecordUseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: UUID
    workspace_id: UUID
    repository_id: UUID
    task_work_id: UUID
    task_revision_id: UUID
    task_candidate_id: UUID | None = None
    bundle_id: UUID


class RecordOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_used: bool
    outcome_receipt_id: UUID


class CorrectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    repository_id: UUID
    kind: Literal["withdraw_evidence", "supersede_proposal"]
    target: dict[str, Any]
    reason: str = Field(min_length=1)


class NativeConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    repository_id: UUID
    consumer_name: str = "native_event_consumer"
    batch_size: int = Field(default=100, ge=1, le=1000)


__all__ = [
    "ApplicabilityRequest",
    "ArtifactLocators",
    "CanonicalSnapshotId",
    "CanonicalSnapshotRef",
    "CodeSnapshotIngestRequest",
    "ContextCompileRequest",
    "CorrectionCreateRequest",
    "IngestRequest",
    "NativeConsumeRequest",
    "NativeRecordIngestRequest",
    "Principal",
    "ProposalCreateRequest",
    "PublishRequest",
    "RebuildRequest",
    "RecordOutcomeRequest",
    "RecordUseRequest",
    "RetireRequest",
    "SnapshotManifest",
    "SourceFile",
    "SourceManifest",
	"canonical_snapshot_ref",
	"validate_canonical_snapshot_id",
]
