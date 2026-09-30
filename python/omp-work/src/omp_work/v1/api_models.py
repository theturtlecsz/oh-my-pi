from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_serializer

from .models import (
    AuditManifest,
    AuditorLaunch,
    Candidate,
    CheckpointDelivery,
    CloseAttempt,
    CloseAttemptEvent,
    DecisionActionClass,
    EvidenceReceipt,
    FindingSeverity,
    IntakeBlockingQuestion,
    ItemBudget,
    MissionDraft,
    MissionEventType,
    MissionStatus,
    OperationReceipt,
    OwnerInstruction,
    RelationEdge,
    ResearchArtifact,
    ResearchCampaign,
    ResearchComponent,
    ResearchDataset,
    ResearchDeliverableBinding,
    ResearchObservation,
    ResearchSource,
    ResearchTrial,
    StrictModel,
    WorkAlias,
    WorkRevision,
    hex64,
    validate_subscription_event_types,
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


class SkipActiveItemResult(StrictModel):
    type: Literal["skip_active_item"]
    grant: ExecutionGrantView
    item: ExecutionGrantItemView
    reason: str


class RecordCloseoutReviewResult(StrictModel):
    type: Literal["record_closeout_review"]
    status: Literal["applied", "refused"]
    receipt: EvidenceReceipt | None = None
    attempt: CloseAttempt | None = None
    event: CloseAttemptEvent


class ProjectHealthResult(StrictModel):
    type: Literal["record_project_health"]
    health: ProjectHealthView


class AlarmSignalResult(StrictModel):
    type: Literal["record_alarm_signal"]
    signal: Literal[
        "cost_threshold",
        "budget_exceeded",
        "safety_check_failed",
        "credential_appeared",
    ]
    work_id: UUID | None = None
    subject: str
    detail: str = ""


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


class AssessBoundedIntakeResult(StrictModel):
    type: Literal["assess_bounded_intake"]
    semantic_sha256: hex64
    rule_bundle_sha256: hex64
    ready_for_ratification: bool
    issue_count: int = Field(ge=0)
    questions: tuple[IntakeBlockingQuestion, ...]


class RecordExternalDeliveryResult(StrictModel):
    type: Literal["record_external_delivery"]
    work_id: UUID
    revision_id: UUID
    receipt_id: UUID
    payload_sha256: hex64


class RecordFableAdviceResult(StrictModel):
    type: Literal["record_fable_advice"]
    receipt: EvidenceReceipt


class AttestIntakeAdmissionResult(StrictModel):
    type: Literal["attest_intake_admission"]
    receipt: EvidenceReceipt
    operator_actor_id: UUID


class PublishBoundedIntakeResult(StrictModel):
    type: Literal["publish_bounded_intake"]
    item: CreatedWorkItem
    receipt: EvidenceReceipt


class AnswerIntakeDecisionResult(StrictModel):
    type: Literal["answer_intake_decision"]
    work_id: UUID
    answer: Literal["approve"]
    answered_at: datetime


class CreateResearchCampaignResult(StrictModel):
    type: Literal["create_research_campaign"]
    status: Literal["applied", "replayed"]
    campaign: ResearchCampaign


class AdmitResearchCampaignResult(StrictModel):
    type: Literal["admit_research_campaign"]
    status: Literal["applied", "replayed"]
    campaign: ResearchCampaign


class CancelResearchCampaignResult(StrictModel):
    type: Literal["cancel_research_campaign"]
    status: Literal["applied", "replayed"]
    campaign: ResearchCampaign


class ProposeResearchTrialResult(StrictModel):
    type: Literal["propose_research_trial"]
    status: Literal["applied", "replayed"]
    trial: ResearchTrial


class RecordResearchObservationResult(StrictModel):
    type: Literal["record_research_observation"]
    status: Literal["applied", "replayed"]
    observation: ResearchObservation


class BindResearchDeliverableResult(StrictModel):
    type: Literal["bind_research_deliverable"]
    status: Literal["applied", "replayed"]
    deliverable_binding: ResearchDeliverableBinding


class SetResearchCampaignStateResult(StrictModel):
    type: Literal["set_research_campaign_state"]
    status: Literal["applied", "replayed"]
    campaign: ResearchCampaign


class RegisterResearchArtifactResult(StrictModel):
    type: Literal["register_research_artifact"]
    status: Literal["applied", "replayed"]
    artifact: ResearchArtifact


class CollectResearchArtifactResult(StrictModel):
    type: Literal["collect_research_artifact"]
    status: Literal["applied", "replayed"]
    artifact: ResearchArtifact


class ResearchArtifactContentView(StrictModel):
    artifact: ResearchArtifact
    content_base64: str


class RegisterResearchSourceResult(StrictModel):
    type: Literal["register_research_source"]
    status: Literal["applied", "replayed"]
    source: ResearchSource


class ResearchSourceView(StrictModel):
    source: ResearchSource
    content_base64: str | None = None


class RegisterResearchDatasetResult(StrictModel):
    type: Literal["register_research_dataset"]
    status: Literal["applied", "replayed"]
    dataset: ResearchDataset


class ResearchDatasetView(StrictModel):
    dataset: ResearchDataset
    content_base64: str | None = None


class RecordResearchCacheResult(StrictModel):
    type: Literal["record_research_cache"]
    status: Literal["applied", "replayed"]
    cache_key: str
    artifact_sha256: str


class ClaimResearchReplicateResult(StrictModel):
    type: Literal["claim_research_replicate"]
    status: Literal["applied", "replayed"]
    artifact_sha256: str


class BindResearchReceiptManifestResult(StrictModel):
    type: Literal["bind_research_receipt_manifest"]
    status: Literal["applied", "replayed"]
    receipt_id: UUID
    artifact_sha256: str
    manifest_sha256: str


class RegisterResearchComponentResult(StrictModel):
    type: Literal["register_research_component"]
    status: Literal["applied", "replayed"]
    component: ResearchComponent


class ConcludeResearchCampaignResult(StrictModel):
    type: Literal["conclude_research_campaign"]
    status: Literal["applied", "replayed"]
    campaign: ResearchCampaign


class ResearchView(StrictModel):
    work_id: UUID
    campaigns: tuple[ResearchCampaign, ...] = ()
    trials: tuple[ResearchTrial, ...] = ()
    observations: tuple[ResearchObservation, ...] = ()
    deliverable_bindings: tuple[ResearchDeliverableBinding, ...] = ()
    components: tuple[ResearchComponent, ...] = ()
    artifacts: tuple[ResearchArtifact, ...] = ()


class EngageStopResult(StrictModel):
    type: Literal["engage_stop"]
    stopped: bool
    reason: str


class ReleaseStopResult(StrictModel):
    type: Literal["release_stop"]
    stopped: bool
    reason: str


class CreateDecisionResult(StrictModel):
    type: Literal["create_decision"]
    decision_id: UUID
    project_id: UUID
    mission_id: str | None = None
    action_class: DecisionActionClass | None = None
    created_at: datetime


class AnswerDecisionResult(StrictModel):
    type: Literal["answer_decision"]
    decision_id: UUID
    mission_id: str | None = None
    answer: str
    resume_state: str | None = None
    expires_at: AwareDatetime | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_expiry(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("expires_at") is None:
            data.pop("expires_at", None)
        return data


class DecisionView(StrictModel):
    decision_id: UUID
    project_id: UUID
    mission_id: str | None = None
    status: Literal["pending", "answered"]
    question: str
    why_it_matters: str
    risk_of_delay: str
    options: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    default_if_any: str | None = None
    risk_of_each_choice: dict[str, str]
    action_class: DecisionActionClass | None = None
    target_sha256: hex64 | None = None
    answer: str | None = None
    answered_at: datetime | None = None
    expires_at: AwareDatetime | None = None


class DecisionsPage(StrictModel):
    decisions: tuple[DecisionView, ...] = ()
    next_created_at: datetime | None = None
    next_decision_id: UUID | None = None


class MissionTransition(StrictModel):
    from_status: MissionStatus | None = None
    to_status: MissionStatus
    cause_kind: Literal["principal", "policy_rule", "decision"]
    cause_id: str
    actor_id: UUID
    actor_kind: str
    at: datetime
    revision: int


class MissionApprovedScope(StrictModel):
    revision: int
    basis_kind: Literal["decision", "standing_mandate"]
    basis_id: str
    approved_by: UUID
    approved_by_actor_kind: str
    approved_at: datetime
    envelope: MissionDraft


class MissionLink(StrictModel):
    work_id: UUID
    budget: ItemBudget
    linked_at: datetime


class MissionDrawn(StrictModel):
    usd: str
    tokens: int = Field(ge=0)
    wall_clock_seconds: int = Field(ge=0)

    @field_validator("usd")
    @classmethod
    def validate_usd(cls, v: str) -> str:
        if isinstance(v, bool) or not isinstance(v, str):
            raise ValueError("usd must be a decimal string")
        try:
            val = Decimal(v)
        except (InvalidOperation, TypeError):
            raise ValueError("usd must be a valid decimal string")
        if not val.is_finite() or val < 0:
            raise ValueError("usd must be a non-negative decimal string")
        return v

    @field_validator("tokens", "wall_clock_seconds", mode="before")
    @classmethod
    def validate_int_type(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer, not a boolean or string")
        return v


class MissionView(MissionDraft):
    mission_id: UUID
    created_by: UUID
    created_at: datetime
    revision: int
    status: MissionStatus
    budget: ItemBudget | None = None
    budget_source: Literal["mission", "project"] | None = None
    hold_decision: dict[str, Any] | None = None
    approved_scope: MissionApprovedScope | None = None
    transitions: tuple[MissionTransition, ...] = ()
    links: tuple[MissionLink, ...] = ()
    drawn: MissionDrawn


class MissionResult(StrictModel):
    type: Literal[
        "submit_mission",
        "revise_mission",
        "approve_mission",
        "set_mission_status",
        "link_mission_work",
    ]
    mission: MissionView


class DraftMissionIntakeResult(StrictModel):
    type: Literal["draft_mission_intake"]
    mission_id: UUID
    outcome: Literal["clarify", "held", "awaiting_owner", "proceeded"]
    questions: tuple[IntakeBlockingQuestion, ...]
    mission: MissionView | None = None
    decision_id: UUID | None = None
    basis: Literal["approved_mission", "standing_mandate"] | None = None


class AnswerMissionDraftResult(StrictModel):
    type: Literal["answer_mission_draft"]
    decision_id: UUID
    mission_id: UUID
    outcome: Literal["approved", "rejected", "noted"]
    mission: MissionView
    next_decision_id: UUID | None = None
    instruction: OwnerInstruction


class EvidenceRef(StrictModel):
    kind: Literal["domain_event", "evidence", "decision", "finding", "work_item"]
    ref: str


class MissionEventView(StrictModel):
    mission_event_id: UUID
    sequence: int = Field(ge=1)
    mission_id: UUID
    type: MissionEventType
    trigger: str
    occurred_at: datetime
    source_event_id: UUID
    evidence_refs: tuple[EvidenceRef, ...]


class MissionEventsPage(StrictModel):
    events: tuple[MissionEventView, ...] = ()
    watermark_sequence: int
    next_after_sequence: int
    has_more: bool


class FindingView(StrictModel):
    finding_id: UUID
    mission_id: UUID
    severity: FindingSeverity
    title: str = Field(min_length=1, max_length=200)
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = (
        Field(min_length=1, max_length=20)
    )


class EventSubscriptionView(StrictModel):
    subscription_id: UUID
    client_id: UUID
    push_url: str | None = None
    event_types: tuple[str, ...]
    cursor_sequence: int = Field(ge=0)
    deleted: bool

    @field_validator("event_types")
    @classmethod
    def check_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_subscription_event_types(value)


class EventSubscriptionsPage(StrictModel):
    subscriptions: tuple[EventSubscriptionView, ...] = ()


class RecordFindingResult(StrictModel):
    type: Literal["record_finding"]
    finding: FindingView


class EventSubscriptionResult(StrictModel):
    type: Literal[
        "put_event_subscription",
        "delete_event_subscription",
        "advance_event_cursor",
    ]
    subscription: EventSubscriptionView


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
    | RecordCloseoutReviewResult
    | ProjectHealthResult
    | ActivateCutoverResult
    | AttestCutoverPlanResult
    | BeginExecutionResult
    | ActivateExecutionItemResult
    | SealExecutionCriteriaResult
    | StampExecutionPlanResult
    | SetExecutionStateResult
    | CompleteExecutionItemResult
    | SkipActiveItemResult
    | AssessBoundedIntakeResult
    | RecordExternalDeliveryResult
    | RecordFableAdviceResult
    | AttestIntakeAdmissionResult
    | PublishBoundedIntakeResult
    | AnswerIntakeDecisionResult
    | RegisterResearchArtifactResult
    | CollectResearchArtifactResult
    | RegisterResearchSourceResult
    | RegisterResearchDatasetResult
    | RecordResearchCacheResult
    | ClaimResearchReplicateResult
    | BindResearchReceiptManifestResult
    | RegisterResearchComponentResult
    | CreateResearchCampaignResult
    | AdmitResearchCampaignResult
    | CancelResearchCampaignResult
    | ProposeResearchTrialResult
    | RecordResearchObservationResult
    | BindResearchDeliverableResult
    | SetResearchCampaignStateResult
    | ConcludeResearchCampaignResult
    | AlarmSignalResult
    | EngageStopResult
    | ReleaseStopResult
    | CreateDecisionResult
    | AnswerDecisionResult
    | MissionResult
    | DraftMissionIntakeResult
    | AnswerMissionDraftResult
    | RecordFindingResult
    | EventSubscriptionResult,
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


class WorkItemSummary(StrictModel):
    work_id: UUID
    key: str
    state: str
    created_at: datetime


class WorkItemsPage(StrictModel):
    items: tuple[WorkItemSummary, ...] = ()
    next_created_at: datetime | None = None
    next_work_id: UUID | None = None


class DomainEventView(StrictModel):
    event_id: UUID
    sequence: int
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
    payload_sha256: str
    previous_event_sha256: str | None = None
    event_sha256: str
    occurred_at: datetime


class DomainEventsPage(StrictModel):
    events: tuple[DomainEventView, ...] = ()
    watermark_sequence: int
    next_after_sequence: int
    has_more: bool


class StopStatusView(StrictModel):
    workspace_id: UUID
    stopped: bool
    reason: str | None = None
    changed_at: datetime | None = None
    changed_by_actor_kind: str | None = None


class ClientResponse(StrictModel):
    outcome: Literal["read", "applied", "replayed", "pending_approval"]
    state: str | None = None
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    decisions: tuple[UUID, ...] = ()
    artifacts: tuple[str, ...] = ()
    operation: str = Field(min_length=1)
    contract: Literal["client.omp.dev/v1"] = "client.omp.dev/v1"
    result: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None

