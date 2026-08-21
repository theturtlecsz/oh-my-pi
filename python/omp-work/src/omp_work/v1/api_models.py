from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .models import AuditManifest, AuditorLaunch, Candidate, CheckpointDelivery, CloseAttempt, CloseAttemptEvent, EvidenceReceipt, FocusSlot, OperationReceipt, RelationEdge, StrictModel, WorkAlias, WorkRevision


class AcceptanceCriterionView(StrictModel):
    criterion: str
    position: int


class WorkItemView(StrictModel):
    work_id: UUID
    workspace_id: UUID
    alias: WorkAlias
    state: str
    revision: WorkRevision
    candidate: Candidate | None = None
    project_id: UUID | None = None
    archived: bool = False


class CloseAttemptResult(StrictModel):
    """Shared typed result for close-ritual commands: expected gate failures
    are refusals WITH an event, never generic exceptions (OMP-47)."""
    type: Literal["begin_close_attempt", "seal_audit_manifest", "reserve_auditor_launch", "cancel_auditor_launch", "settle_auditor_launch", "attest_checkpoint_delivery"]
    status: Literal["applied", "refused"]
    attempt: CloseAttempt | None = None
    manifest: AuditManifest | None = None
    launch: AuditorLaunch | None = None
    receipt: EvidenceReceipt | None = None
    delivery: CheckpointDelivery | None = None
    verdict: Literal["PASS", "NEEDS_FIX", "BLOCKED"] | None = None
    event: CloseAttemptEvent


class ProjectView(StrictModel):
    project_id: UUID
    workspace_id: UUID
    key: str | None = None
    name: str
    health: Literal["onTrack", "atRisk", "offTrack"] | None = None
    health_updated_at: datetime | None = None


class WorkflowView(StrictModel):
    item: WorkItemView
    relations: tuple[RelationEdge, ...] = ()
    receipts: tuple[EvidenceReceipt, ...] = ()
    close_attempts: tuple[CloseAttempt, ...] = ()
    audit_manifest: AuditManifest | None = None
    auditor_launches: tuple[AuditorLaunch, ...] = ()
    close_attempt_events: tuple[CloseAttemptEvent, ...] = ()
    checkpoint_deliveries: tuple[CheckpointDelivery, ...] = ()
    project: ProjectView | None = None


class WorkspaceTree(StrictModel):
    workspace_id: UUID
    items: tuple[WorkItemView, ...]
    relations: tuple[RelationEdge, ...] = ()
    projects: tuple[ProjectView, ...] = ()


class ProjectHealthView(StrictModel):
    project_id: UUID
    workspace_id: UUID
    health: Literal["onTrack", "atRisk", "offTrack"]
    updated_at: datetime


class CreatedWorkItem(StrictModel):
    client_ref: str
    work_id: UUID
    revision_id: UUID
    key: str
    state: str
    row_version: int


class CreateWorkBatchResult(StrictModel):
    type: Literal["create_work_batch"]
    items: tuple[CreatedWorkItem, ...]


class ReviseWorkResult(StrictModel):
    type: Literal["revise_work"]
    revision_id: UUID
    changed: bool


class WorkItemResult(StrictModel):
    type: Literal["set_work_state"]
    work_id: UUID
    state: str
    row_version: int


class CompleteWorkResult(StrictModel):
    type: Literal["complete_work"]
    status: Literal["applied", "refused"]
    work_id: UUID
    state: str | None = None
    row_version: int | None = None
    completed_work_ids: tuple[UUID, ...] = ()
    event: CloseAttemptEvent | None = None


class RelationResult(StrictModel):
    type: Literal["put_relation", "remove_relation"]
    source_work_id: UUID
    target_work_id: UUID
    kind: Literal["parent", "blocks", "duplicate_of", "related"]
    active: bool


class FocusResult(StrictModel):
    type: Literal["set_focus", "clear_focus"]
    workspace_id: UUID
    owner_id: UUID
    work_id: UUID | None
    version: int


class EvidenceResult(StrictModel):
    type: Literal["append_evidence"]
    receipt: EvidenceReceipt
    event: CloseAttemptEvent | None = None


class FinalizeCandidateResult(StrictModel):
    type: Literal["finalize_candidate"]
    candidate: Candidate


class HealthView(StrictModel):
    live: bool
    ready: bool
    alerts: tuple[str, ...] = ()


class CloseoutResult(StrictModel):
    type: Literal["request_closeout"]
    status: Literal["applied", "refused"]
    attempt: CloseAttempt | None = None
    event: CloseAttemptEvent


class ProjectHealthResult(StrictModel):
    type: Literal["record_project_health"]
    health: ProjectHealthView


class ActivateCutoverResult(StrictModel):
    type: Literal["activate_cutover"]
    epoch_id: UUID
    authority: Literal["work"]
    candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: datetime


class AttestCutoverPlanResult(StrictModel):
    type: Literal["attest_cutover_plan"]
    epoch_id: UUID
    work_id: UUID
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuthorityView(StrictModel):
    authority: Literal["linear", "work"]
    epoch_id: UUID | None = None
    epoch_state: Literal["active", "sealed", "rolled_back"] | None = None
    activated_at: datetime | None = None
    first_work_mutation_at: datetime | None = None


CommandResult = Annotated[
    CreateWorkBatchResult | ReviseWorkResult | WorkItemResult | CompleteWorkResult | RelationResult | FocusResult | EvidenceResult | FinalizeCandidateResult | CloseAttemptResult | CloseoutResult | ProjectHealthResult | ActivateCutoverResult | AttestCutoverPlanResult,
    Field(discriminator="type"),
]


class StoredOperationView(StrictModel):
    receipt: OperationReceipt
    command_type: str
    request_id: UUID
    correlation_id: UUID
    result: CommandResult | None = None


class ApiError(StrictModel):
    code: str
    request_id: UUID | None = None
    correlation_id: UUID | None = None
    diagnostics: tuple[str, ...] = ()


class CommandResponse(StrictModel):
    receipt: OperationReceipt
    result: CommandResult
