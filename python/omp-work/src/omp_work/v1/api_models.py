from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from .models import (
    AuditManifest,
    AuditorLaunch,
    CandidateSourceVersion,
    StageLaunch,
    Candidate,
    CheckpointDelivery,
    CloseAttempt,
    CloseAttemptEvent,
    EvidenceReceipt,
    OperationReceipt,
    ProviderAccount,
    RelationEdge,
    StrictModel,
    WorkAlias,
    WorkRevision,
)


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
    repository_id: UUID | None = None
    archived: bool = False


class CloseAttemptResult(StrictModel):
    """Shared typed result for close-ritual commands: expected gate failures
    are refusals WITH an event, never generic exceptions (OMP-47)."""

    type: Literal[
        "begin_close_attempt",
        "seal_audit_manifest",
        "reserve_auditor_launch",
        "cancel_auditor_launch",
        "settle_auditor_launch",
        "attest_checkpoint_delivery",
    ]
    status: Literal["applied", "refused"]
    attempt: CloseAttempt | None = None
    manifest: AuditManifest | None = None
    launch: AuditorLaunch | None = None
    receipt: EvidenceReceipt | None = None
    delivery: CheckpointDelivery | None = None
    verdict: Literal["PASS", "NEEDS_FIX", "BLOCKED"] | None = None
    event: CloseAttemptEvent


class StageLaunchResult(StrictModel):
    type: Literal[
        "reserve_stage_launch",
        "handoff_stage_launch",
        "settle_stage_launch",
        "cancel_stage_launch",
        "reconcile_stage_launch",
    ]
    status: Literal["applied", "replayed", "refused"]
    launch: StageLaunch | None = None
    reason: str | None = None


class CandidateSourceAssociationResult(StrictModel):
    type: Literal["associate_candidate_source"]
    status: Literal["applied", "replayed", "refused"]
    association: CandidateSourceVersion | None = None


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
    stage_launches: tuple[StageLaunch, ...] = ()
    candidate_source_versions: tuple[CandidateSourceVersion, ...] = ()
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


class CreateSameSessionChildResult(StrictModel):
    """OMP-139: the atomic filing returns the created child and its minted receipt."""

    type: Literal["create_same_session_child"]
    item: CreatedWorkItem
    receipt: EvidenceReceipt


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
    canceled_work_ids: tuple[UUID, ...] = ()
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
    service_fingerprint: str | None = None


class ExecutionGrantView(StrictModel):
    grant_id: UUID
    workspace_id: UUID
    owner_id: UUID
    repository: str
    remote_ref: str
    state: str
    mode: str
    grant_version: int
    max_continuations: int
    max_close_attempts: int
    max_no_progress: int
    continuations_scheduled: int
    terminal_reason: str | None = None
    authorization_hash: str
    judge_sha256: str
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    paused_at: datetime | None = None
    stopped_at: datetime | None = None
    canceled_at: datetime | None = None


class ExecutionGrantItemView(StrictModel):
    item_id: UUID
    workspace_id: UUID
    grant_id: UUID
    work_id: UUID
    position: int
    phase: str
    claimed_revision_id: UUID
    project_id: UUID | None = None
    active_blocker_ids: tuple[UUID, ...] = ()
    initial_git_baseline: str
    current_git_baseline: str | None = None
    criteria_revision_id: UUID | None = None
    original_request: str
    original_request_sha256: str
    criteria_sha256: str | None = None
    plan_stamp_sha256: str | None = None
    plan_stamp: dict[str, Any] | None = None
    close_attempts_started: int
    consecutive_no_progress: int
    last_reviewed_tree_sha: str | None = None
    last_findings_hash: str | None = None
    push_receipt_id: UUID | None = None
    closeout_receipt_id: UUID | None = None
    activated_at: datetime | None = None
    completed_at: datetime | None = None
    abandoned_at: datetime | None = None
    skipped_at: datetime | None = None
    terminal_reason: str | None = None


class ExecutionView(StrictModel):
    grant: ExecutionGrantView
    items: tuple[ExecutionGrantItemView, ...]
    active_item: ExecutionGrantItemView | None = None


class BeginExecutionResult(StrictModel):
    type: Literal["begin_execution"]
    grant: ExecutionGrantView
    items: tuple[ExecutionGrantItemView, ...]


class ActivateExecutionItemResult(StrictModel):
    type: Literal["activate_execution_item"]
    grant: ExecutionGrantView
    item: ExecutionGrantItemView


class SealExecutionCriteriaResult(StrictModel):
    type: Literal["seal_execution_criteria"]
    grant: ExecutionGrantView
    item: ExecutionGrantItemView
    revision: WorkRevision


class StampExecutionPlanResult(StrictModel):
    type: Literal["stamp_execution_plan"]
    grant: ExecutionGrantView
    item: ExecutionGrantItemView
    candidate: Candidate
    receipt: EvidenceReceipt


class SetExecutionStateResult(StrictModel):
    type: Literal["set_execution_state"]
    grant: ExecutionGrantView


class CompleteExecutionItemResult(StrictModel):
    type: Literal["complete_execution_item"]
    grant: ExecutionGrantView
    item: ExecutionGrantItemView
    work_id: UUID
    state: str
    closeout_receipt: EvidenceReceipt


class RecordCloseoutReviewResult(StrictModel):
    type: Literal["record_closeout_review"]
    status: Literal["applied", "refused"]
    receipt: EvidenceReceipt | None = None
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


class BudgetResult(StrictModel):
    type: Literal["create_budget_scope", "reserve_budget", "claim_budget", "settle_budget", "cancel_budget", "issue_frontier_exception", "expire_budget"]
    scope_id: UUID | None = None
    parent_scope_id: UUID | None = None
    reservation_id: UUID | None = None
    transport_attempt_id: UUID | None = None
    exception_id: UUID | None = None
    fence: int | None = None
    state: str | None = None
    remaining_attempts: int | None = None
    actual_drawdown: str | None = None
    overrun: bool | None = None
    replayed: bool | None = None


class ProviderAccountResult(StrictModel):
    type: Literal["put_provider_account"]
    status: Literal["inserted", "updated", "unchanged"]
    account_id: UUID
    account: ProviderAccount


class AuthorityView(StrictModel):
    authority: Literal["linear", "work"]
    epoch_id: UUID | None = None
    epoch_state: Literal["active", "sealed", "rolled_back"] | None = None
    activated_at: datetime | None = None
    first_work_mutation_at: datetime | None = None


CommandResult = Annotated[
    CreateWorkBatchResult
    | CreateSameSessionChildResult
    | ReviseWorkResult
    | WorkItemResult
    | CompleteWorkResult
    | RelationResult
    | FocusResult
    | EvidenceResult
    | FinalizeCandidateResult
    | CloseAttemptResult
    | StageLaunchResult
    | CandidateSourceAssociationResult
    | RecordCloseoutReviewResult
    | ProjectHealthResult
    | ActivateCutoverResult
    | AttestCutoverPlanResult
    | BudgetResult
    | ProviderAccountResult
    | BeginExecutionResult
    | ActivateExecutionItemResult
    | SealExecutionCriteriaResult
    | StampExecutionPlanResult
    | SetExecutionStateResult
    | CompleteExecutionItemResult,
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


class WorkRevisionListView(StrictModel):
    work_id: UUID
    key: str
    revisions: tuple[WorkRevision, ...]


class EvidenceReceiptView(EvidenceReceipt):
    issuer: str | None = None
    independent: bool | None = None


class WorkItemsPage(StrictModel):
    """Current row views of work items paginated by (created_at, work_id).

    NOTE: This enumerates current row views at query time, NOT an immutable
    point-in-time historical export while concurrent state changes occur.
    Returned exact revision IDs permit independent later resolution;
    event-based complete export integration remains a later approved slice.
    """

    items: tuple[WorkItemView, ...]
    next_cursor: str | None = None
    workspace_id: UUID
    limit: int
    exhausted: bool


class DomainEventView(StrictModel):
    event_id: UUID
    sequence: int = Field(ge=1)
    workspace_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    actor_id: UUID
    actor_kind: str
    capability_id: UUID
    request_id: UUID
    correlation_id: UUID
    operation_id: UUID
    causation_id: UUID
    event_type: str
    outcome: str
    payload: dict[str, Any]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime


class DomainEventsPage(StrictModel):
    items: tuple[DomainEventView, ...]
    workspace_id: UUID
    after_sequence: int
    through_sequence: int
    next_sequence: int = Field(
        description="resume exclusively with after_sequence=next_sequence (do not increment), empty page retains supplied after",
    )
    next_cursor: str | None = None
    limit: int
    exhausted: bool


class RepositoryView(StrictModel):
    repository_id: UUID
    workspace_id: UUID
    key: str
    name: str
    url: str
    archived: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RepositoryListView(StrictModel):
    workspace_id: UUID
    repositories: tuple[RepositoryView, ...]
    next_cursor: str | None = None
    limit: int
    exhausted: bool


class ProviderAccountListView(StrictModel):
    workspace_id: UUID
    accounts: tuple[ProviderAccount, ...]
