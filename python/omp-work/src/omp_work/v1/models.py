from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkAlias(StrictModel):
    work_id: UUID
    key: str = Field(pattern=r"^(HOME|OMP)-[1-9][0-9]*$")
    primary: Literal[True]
    origin: Literal["imported", "local"]

    @model_validator(mode="after")
    def validate_origin_key(self) -> WorkAlias:
        if (self.origin == "imported") != self.key.startswith("HOME-"):
            raise ValueError(
                "imported aliases use HOME keys and local aliases use OMP keys"
            )
        return self


class RelationKind(StrEnum):
    PARENT = "parent"
    BLOCKS = "blocks"
    DUPLICATE_OF = "duplicate_of"
    RELATED = "related"


class EvidenceKind(StrEnum):
    PLAN = "plan"
    VERIFICATION = "verification"
    AUDIT = "audit"
    PUSH = "push"
    CLOSEOUT = "closeout"
    HANDOFF = "handoff"
    SAME_SESSION_FOUND_FIXED = "same_session_found_fixed"


class CloseAttemptState(StrEnum):
    ACTIVE = "active"
    AUDIT_READY = "audit_ready"
    AUDITOR_IN_FLIGHT = "auditor_in_flight"
    AUDITED = "audited"
    CLOSEOUT_REQUESTED = "closeout_requested"
    REMEDIATION_REQUIRED = "remediation_required"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


LIVE_CLOSE_ATTEMPT_STATES: frozenset[CloseAttemptState] = frozenset(
    {
        CloseAttemptState.ACTIVE,
        CloseAttemptState.AUDIT_READY,
        CloseAttemptState.AUDITOR_IN_FLIGHT,
        CloseAttemptState.AUDITED,
        CloseAttemptState.CLOSEOUT_REQUESTED,
    }
)

MAX_AUDITOR_LAUNCHES = 3
MAX_ACCEPTED_REPORTS = 2


class OperationState(StrEnum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"


class WorkRevision(StrictModel):
    revision_id: UUID
    work_id: UUID
    revision_number: int = Field(ge=1)
    title: str
    description: str
    scope: str
    acceptance_criteria: tuple[str, ...]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    created_at: datetime


class RelationEdge(StrictModel):
    workspace_id: UUID
    source_work_id: UUID
    target_work_id: UUID
    kind: RelationKind
    active: bool = True


class FocusSlot(StrictModel):
    workspace_id: UUID
    owner_id: UUID
    work_id: UUID | None = None
    version: int = Field(ge=0)


class Candidate(StrictModel):
    candidate_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_sha: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    kind: Literal["planned", "final"] = "planned"
    allocated_at: datetime

    @model_validator(mode="after")
    def validate_final_commit(self) -> Candidate:
        if self.kind == "final" and self.commit_sha is None:
            raise ValueError("final candidates bind an exact commit")
        return self


class EvidenceReceipt(StrictModel):
    receipt_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID
    kind: EvidenceKind
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    issuer: str
    issued_at: datetime
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    verdict: Literal["PASS", "NEEDS_FIX", "BLOCKED"] | None = None
    independent: bool = False
    remote_ref: str | None = None
    remote_commit: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )

    @model_validator(mode="after")
    def validate_payload_size(self) -> EvidenceReceipt:
        from .canonical import canonical_json

        if len(canonical_json(self.payload).encode()) > 1048576:
            raise ValueError("evidence payload exceeds 1 MiB")
        return self


class CloseAttempt(StrictModel):
    attempt_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID
    plan_receipt_id: UUID | None = None
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    owner_session_id: str | None = None
    owner_session_started_at: datetime | None = None
    owner_session_start_commit: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    repository: str | None = None
    diff_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    starting_dirty_paths: tuple[str, ...] | None = None
    execution_grant_id: UUID | None = None
    candidate_tree_sha: str | None = None
    original_request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    criteria_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_stamp_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    judge_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_kind: Literal["summary", "legacy", "execution"]
    authorization_ref: str = Field(min_length=1)
    launch_count: int = Field(ge=0)
    cancelled_launch_count: int = Field(default=0, ge=0)
    accepted_report_count: int = Field(ge=0, le=2)
    in_flight_launch_id: UUID | None = None
    state: CloseAttemptState
    terminal_reason: str | None = None
    requested_at: datetime
    closeout_requested_at: datetime | None = None
    completed_at: datetime | None = None
    completion_authorization_ref: str | None = None
    riders: tuple[SealedRider, ...] = ()


class AuditManifest(StrictModel):
    manifest_id: UUID
    work_id: UUID
    attempt_id: UUID
    manifest_version: Literal[1, 2, 3] = 1
    plan_receipt_id: UUID
    verification_receipt_id: UUID
    candidate_id: UUID
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    task_body: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_hashes: dict[str, str]
    created_at: datetime


class AuditorLaunch(StrictModel):
    launch_id: UUID
    attempt_id: UUID
    manifest_id: UUID
    launch_number: int = Field(ge=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1)
    reserved_at: datetime


class StageLaunchRole(StrEnum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    FRONTIER = "frontier"
    AUDIT = "audit"


class StageLaunchStatus(StrEnum):
    RESERVED = "reserved"
    HANDED_OFF = "handed_off"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"


class StagePreflightOutcome(StrEnum):
    SELECTED = "selected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StagePreflightDisposition(StrEnum):
    INDETERMINATE = "indeterminate"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFIRMED_ABSENT = "confirmed_absent"


class BudgetScopeKind(StrEnum):
    ACCOUNT = "account"
    SESSION = "session"
    WORK = "work"
    TOURNAMENT = "tournament"
    ROLE = "role"


class BudgetReservationState(StrEnum):
    RESERVED_UNSENT = "reserved_unsent"
    POTENTIALLY_SENT = "potentially_sent"
    SETTLED = "settled"
    CANCELLED_UNSENT = "cancelled_unsent"
    UNRESOLVED = "unresolved"


class BudgetResource(StrEnum):
    CASH = "cash"
    INCLUDED_CREDIT = "included_credit"
    NATIVE_QUOTA = "native_quota"
    LOCAL_COMPUTE = "local_compute"


class RateCardQualification(StrEnum):
    UNQUALIFIED = "unqualified"
    QUALIFIED = "qualified"


class RateCard(StrictModel):
    rate_card_id: UUID
    workspace_id: UUID
    provider: str = Field(min_length=1)
    version: str = Field(min_length=1)
    billing_modes: tuple[Literal["subscription", "metered", "purchased_credit", "local"], ...]
    effective_from: datetime
    effective_until: datetime | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_prices: dict[str, str]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_source: str = Field(min_length=1)
    observed_at: datetime
    qualification: RateCardQualification
    registered_at: datetime


class ProviderAccount(StrictModel):
    account_id: UUID
    workspace_id: UUID
    provider: str = Field(min_length=1)
    account_identity: str = Field(min_length=1)
    entitlement_evidence: str = Field(min_length=1)
    evidence_observed_at: datetime
    billing_mode: Literal["subscription", "metered", "purchased_credit", "local"]
    rate_card_version: str | None = None
    observed_balance: str | None = None
    balance_provenance: Literal["provider_observed", "locally_estimated", "unknown"]
    reset_at: datetime | None = None
    concurrency_limit: int = Field(ge=1)
    budget_resource: BudgetResource | None = None


class BudgetQuote(StrictModel):
    quote_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    launch_id: UUID | None = None
    account_id: UUID
    account_evidence_observed_at: datetime
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    rate_card_id: UUID
    rate_card_version: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    usage_ceiling: dict[str, int]
    worst_case_amount: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quoted_at: datetime
    resource: BudgetResource | None = None
    scope_id: UUID | None = None


class BudgetScope(StrictModel):
    scope_id: UUID
    workspace_id: UUID
    parent_scope_id: UUID | None = None
    kind: BudgetScopeKind
    policy_version: str = Field(min_length=1)
    work_id: UUID | None = None
    session_id: str | None = None
    limits: dict[str, str]
    held: dict[str, str]
    spent: dict[str, str]
    unresolved: dict[str, str]


class DispatchReservation(StrictModel):
    reservation_id: UUID
    scope_id: UUID
    account_id: UUID
    logical_call_id: UUID
    transport_attempt_id: UUID
    fence: int = Field(ge=1)
    state: BudgetReservationState
    resource: BudgetResource
    worst_case_drawdown: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    context_limit: int = Field(gt=0)
    output_limit: int = Field(gt=0)
    expires_at: datetime


class UsageSettlement(StrictModel):
    reservation_id: UUID
    transport_attempt_id: UUID
    state: Literal["settled", "unresolved"]
    actual_drawdown: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    usage: dict[str, int] = Field(default_factory=dict)
    provenance: Literal["provider_observed", "locally_estimated", "unknown"]
    provider_request_id: str | None = None
    outcome: Literal["success", "error", "timeout", "cancelled", "unknown"]


class FrontierException(StrictModel):
    exception_id: UUID
    scope_id: UUID
    question: str = Field(min_length=1)
    route: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    context_limit: int = Field(gt=0)
    output_limit: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    remaining_attempts: int = Field(ge=0)
    resource: BudgetResource
    resource_limit: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    expires_at: datetime


class StageLaunch(StrictModel):
    launch_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    resolved_selector: str | None = None
    resolved_provider: str | None = None
    resolved_model: str | None = None
    served_selector: str | None = None
    served_model: str | None = None
    is_fallback: bool = False
    fallback_reason: str | None = None
    status: StageLaunchStatus
    outcome_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: dict[str, Any] | None = None
    reserved_at: datetime
    handed_off_at: datetime | None = None
    settled_at: datetime | None = None


class StagePreflightOrchestrationUsage(StrictModel):
    input: int | None = Field(default=None, ge=0, strict=True)
    cacheRead: int | None = Field(default=None, ge=0, strict=True)
    output: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_not_empty(self) -> StagePreflightOrchestrationUsage:
        if self.input is None and self.cacheRead is None and self.output is None:
            raise ValueError("orchestration cannot be empty")
        return self


class StagePreflightCttlUsage(StrictModel):
    ephemeral5m: int | None = Field(default=None, ge=0, strict=True)
    ephemeral1h: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_not_empty(self) -> StagePreflightCttlUsage:
        if self.ephemeral5m is None and self.ephemeral1h is None:
            raise ValueError("cttl cannot be empty")
        return self


class StagePreflightServerUsage(StrictModel):
    webSearch: int | None = Field(default=None, ge=0, strict=True)
    webFetch: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_not_empty(self) -> StagePreflightServerUsage:
        if self.webSearch is None and self.webFetch is None:
            raise ValueError("server cannot be empty")
        return self


class StagePreflightUsage(StrictModel):
    input: int = Field(ge=0, strict=True)
    output: int = Field(ge=0, strict=True)
    cacheRead: int = Field(ge=0, strict=True)
    cacheWrite: int = Field(ge=0, strict=True)
    totalTokens: int = Field(ge=0, strict=True)
    contextTokens: int | None = Field(default=None, ge=0, strict=True)
    premiumRequests: int | None = Field(default=None, ge=0, strict=True)
    reasoningTokens: int | None = Field(default=None, ge=0, strict=True)
    orchestration: StagePreflightOrchestrationUsage | None = None
    cttl: StagePreflightCttlUsage | None = None
    server: StagePreflightServerUsage | None = None


class StagePreflight(StrictModel):
    preflight_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    session_id: str | None = None
    role: StageLaunchRole
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport_attempt_id: UUID
    ordinal: int = Field(ge=0, strict=True)
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    is_fallback: bool = False
    outcome: StagePreflightOutcome
    stop_reason: str | None = None
    error: str | None = None
    requests: int | None = Field(default=None, ge=0, strict=True)
    usage: StagePreflightUsage | None = None
    provider_request_id: str | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def validate_outcome_and_error(self) -> StagePreflight:
        if self.outcome == StagePreflightOutcome.SELECTED:
            if self.error is not None:
                raise ValueError("selected preflight cannot have error")
        else:
            if self.error is None or not self.error.strip():
                raise ValueError(f"{self.outcome.value} preflight requires error")
        return self


class StagePreflightIntentStatus(StrEnum):
    BEGUN = "begun"
    DISPATCHED = "dispatched"
    CANCELLED_UNDISPATCHED = "cancelled_undispatched"
    SETTLED = "settled"


class StagePreflightIntent(StrictModel):
    intent_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport_attempt_id: UUID
    ordinal: int = Field(ge=0, strict=True)
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    is_fallback: bool = False
    logical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_owner_id: UUID | None = None
    status: StagePreflightIntentStatus
    created_at: datetime
    settled_at: datetime | None = None
    dispatched_at: datetime | None = None
    dispatch_operation_id: UUID | None = None
    dispatch_owner_id: UUID | None = None
    cancelled_at: datetime | None = None
    cancelled_by: UUID | None = None
    cancel_reason: str | None = None


class StagePreflightReconciliation(StrictModel):
    reconciliation_id: UUID
    workspace_id: UUID
    transport_attempt_id: UUID
    account_id: UUID
    observation_id: UUID
    disposition: StagePreflightDisposition
    observed_at: datetime
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_id: str | None = None
    requests: int | None = Field(default=None, ge=0, strict=True)
    usage: StagePreflightUsage | None = None
    stop_reason: str | None = None
    error: str | None = None
    reconciled_at: datetime


class CandidateSourceVersion(StrictModel):
    candidate_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID
    repository_id: UUID
    source_version_id: str = Field(min_length=1)
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    analyzed_commit: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    tree_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    association_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer: str = Field(min_length=1)
    producer_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class CloseAttemptEvent(StrictModel):
    event_id: UUID
    sequence: int | None = Field(default=None, ge=1)
    work_id: UUID
    attempt_id: UUID | None = None
    launch_id: UUID | None = None
    event_type: str
    reason_code: str
    reason: str
    legal_next_actions: tuple[str, ...]
    remaining_launches: int = Field(ge=0, le=3)
    remaining_reports: int = Field(ge=0, le=2)
    requires_fresh_authorization: bool
    rendered_text: str
    rendered_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requires_delivery: bool
    created_at: datetime


class CheckpointDelivery(StrictModel):
    delivery_id: UUID
    event_id: UUID
    delivery_sequence: int = Field(ge=1)
    owner_session_id: str
    rendered_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["delivered", "failed", "waived"]
    authorization_ref: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_waiver_authorization(self) -> CheckpointDelivery:
        if (self.status == "waived") != (self.authorization_ref is not None):
            raise ValueError(
                "waived deliveries carry an owner authorization reference; others never do"
            )
        return self


class SameSessionFoundFixedPayload(StrictModel):
    """Typed payload contract for kind=same_session_found_fixed receipts (OMP-52):
    binds the child's fix to one parent close attempt's owner session, baseline
    commit, and final candidate — validated at append AND at complete_work."""

    attempt_id: UUID
    owner_session_id: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    fix_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding: str = Field(min_length=1)
    verification: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_substance(self) -> SameSessionFoundFixedPayload:
        if not self.finding.strip() or not self.verification.strip():
            raise ValueError("finding and verification must carry non-blank text")
        return self


class CompletionInput(StrictModel):
    work_id: UUID
    current_revision_id: UUID
    candidate: Candidate
    receipts: tuple[EvidenceReceipt, ...]
    closeout_requested: bool


class CompletionBlocker(StrictModel):
    code: Literal[
        "plan_missing",
        "verification_missing",
        "audit_missing",
        "push_unverified",
        "stale_evidence",
        "closeout_missing",
        "candidate_not_final",
        "attempt_missing",
        "attempt_not_requested",
        "delivery_pending",
        "child_receipt_invalid",
        "completion_evidence_invalid",
    ]
    detail: str



class CompletionRunnerIdentity(StrictModel):
    issuer: Literal["work-service/auditor-settle"] = "work-service/auditor-settle"
    launch_id: UUID
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CompletionSubject(StrictModel):
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class CompletionCheckDefinition(StrictModel):
    definition: Literal["sealed_audit_manifest"] = "sealed_audit_manifest"
    version: Literal[1, 2, 3] = 1
    manifest_id: UUID
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompletionArtifactReference(StrictModel):
    receipt_id: UUID
    kind: Literal["verification", "audit", "push"]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CompletionDeliveryBinding(StrictModel):
    repository: str = Field(min_length=1)
    remote_url: str = Field(min_length=1)
    remote_ref: str = Field(min_length=1)
    candidate_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    remote_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class CompletionEvidence(StrictModel):
    runner: CompletionRunnerIdentity
    subject: CompletionSubject
    check: CompletionCheckDefinition
    result: Literal["PASS"] = "PASS"
    artifacts: tuple[CompletionArtifactReference, ...]
    delivery: CompletionDeliveryBinding

    @model_validator(mode="after")
    def _validate_artifacts(self) -> CompletionEvidence:
        receipt_ids = [ref.receipt_id for ref in self.artifacts]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("duplicate artifact receipt_id")
        kinds = {ref.kind for ref in self.artifacts}
        if kinds != {"verification", "audit", "push"} or len(self.artifacts) != 3:
            raise ValueError(
                "artifacts must contain exactly one verification, audit, and push reference"
            )
        return self

class OperationReceipt(StrictModel):
    operation_id: UUID
    request_id: UUID
    state: OperationState
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostics: tuple[str, ...] = Field(default=(), max_length=8)


class AuditEvent(StrictModel):
    event_id: UUID
    sequence: int = Field(ge=1)
    workspace_id: UUID
    occurred_at: datetime
    actor_id: UUID
    actor_kind: str
    capability_id: UUID
    request_id: UUID
    correlation_id: UUID
    operation_id: UUID
    work_id: UUID | None = None
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    event_type: str
    outcome: str
    payload_schema_version: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    redaction_class: str
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Anomaly(StrictModel):
    code: Literal[
        "pagination_count_hash_gap",
        "duplicate_uuid_key_mapping",
        "missing_relation_endpoint",
        "relation_cycle",
        "multiple_focus_slots",
        "source_local_conflict",
        "legacy_authority_claim",
        "attachment_content_unavailable",
        "unsupported_non_workflow_object",
    ]
    disposition: Literal["blocking", "quarantined"]


class ReconciliationCounts(StrictModel):
    worlds: int = Field(ge=0)
    surfaces: int = Field(ge=0)
    promises: int = Field(ge=0)
    work_items: int = Field(ge=0)
    states: int = Field(ge=0)
    labels: int = Field(ge=0)
    relations: int = Field(ge=0)
    comments: int = Field(ge=0)
    attachments: int = Field(ge=0)
    users: int = Field(ge=0)


class ReconciliationHashes(StrictModel):
    worlds: str = Field(pattern=r"^[0-9a-f]{64}$")
    surfaces: str = Field(pattern=r"^[0-9a-f]{64}$")
    promises: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_items: str = Field(pattern=r"^[0-9a-f]{64}$")
    states: str = Field(pattern=r"^[0-9a-f]{64}$")
    labels: str = Field(pattern=r"^[0-9a-f]{64}$")
    relations: str = Field(pattern=r"^[0-9a-f]{64}$")
    comments: str = Field(pattern=r"^[0-9a-f]{64}$")
    attachments: str = Field(pattern=r"^[0-9a-f]{64}$")
    users: str = Field(pattern=r"^[0-9a-f]{64}$")


class CommandSmokeResult(StrictModel):
    command_type: str
    passed: bool


class CutoverManifest(StrictModel):
    epoch_id: UUID
    contract_version: str
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transform_version: str
    transform_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_boundary: str
    source_watermark: str
    raw_export_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_batch_id: UUID
    dimension_counts: ReconciliationCounts
    dimension_hashes: ReconciliationHashes
    parity_groups: dict[str, str]
    anomalies: tuple[Anomaly, ...]
    parity_differences: tuple[str, ...] = ()
    backup_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restore_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_smoke_results: tuple[CommandSmokeResult, ...]
    code_fingerprint: str
    config_fingerprint: str
    freeze_at: datetime
    linear_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_name: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_work_id: UUID
    first_mutation_request_id: UUID
    activated_at: datetime | None = None
    revoked_at: datetime | None = None
    actor: str


class CreateWorkInput(StrictModel):
    client_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1)
    description: str = ""
    scope: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    state: str = "BACKLOG"
    project_id: UUID | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> CreateWorkInput:
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if not self.state.strip() or self.state == "DONE":
            raise ValueError("initial state must be non-empty and not DONE")
        return self


class CreateBatchRelation(StrictModel):
    source_ref: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    kind: RelationKind


class CreateWorkBatchPayload(StrictModel):
    items: tuple[CreateWorkInput, ...] = Field(min_length=1)
    relations: tuple[CreateBatchRelation, ...] = ()

    @model_validator(mode="after")
    def validate_refs(self) -> CreateWorkBatchPayload:
        refs = tuple(item.client_ref for item in self.items)
        if len(set(refs)) != len(refs):
            raise ValueError("client_ref values must be unique within a batch")
        known = set(refs)
        for relation in self.relations:
            if relation.source_ref not in known or relation.target_ref not in known:
                raise ValueError(
                    "batch relations must reference items in the same request"
                )
            if relation.source_ref == relation.target_ref:
                raise ValueError("batch relations must not be self edges")
        return self


class CreateSameSessionChildPayload(StrictModel):
    """OMP-139: one atomic same-session found-and-fixed filing — the BACKLOG
    child, its active child→parent edge, and the typed same_session_found_fixed
    receipt bound to the live attempt's identity land in ONE serializable
    transaction or not at all."""

    parent_work_id: UUID
    attempt_id: UUID
    owner_session_id: str = Field(min_length=1)
    item: CreateWorkInput
    finding: str = Field(min_length=1)
    verification: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_substance(self) -> CreateSameSessionChildPayload:
        if not self.finding.strip() or not self.verification.strip():
            raise ValueError("finding and verification must carry non-blank text")
        return self


class ReviseWorkPayload(StrictModel):
    work_id: UUID
    expected_revision_id: UUID
    revision: WorkRevision


class SetWorkStatePayload(StrictModel):
    work_id: UUID
    state: str


class PutRelationPayload(StrictModel):
    relation: RelationEdge


class RemoveRelationPayload(StrictModel):
    relation: RelationEdge


class SetFocusPayload(StrictModel):
    slot: FocusSlot
    expected_version: int = Field(ge=0)


class ClearFocusPayload(StrictModel):
    workspace_id: UUID
    owner_id: UUID
    expected_version: int = Field(ge=0)


class AppendEvidencePayload(StrictModel):
    receipt: EvidenceReceipt


class FinalizeCandidatePayload(StrictModel):
    work_id: UUID
    revision_id: UUID
    planned_candidate_id: UUID
    candidate_id: UUID
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RecordCloseoutReviewPayload(StrictModel):
    receipt: EvidenceReceipt
    attempt_id: UUID
    authorization_ref: str = Field(min_length=1)


class CancellationProof(StrictModel):
    """Owner ruling 2026-08-23 (staged cancel batches, OMP-111): one historical work
    item canceled atomically with the primary's completion."""

    work_id: UUID
    revision_id: UUID
    reason: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _reason_bounds(self) -> CancellationProof:
        if len(self.reason.encode("utf-8")) > 4096:
            raise ValueError("cancellation reason exceeds 4096 UTF-8 bytes")
        if "\x00" in self.reason:
            raise ValueError("cancellation reason must not contain NUL")
        if not self.reason.strip():
            raise ValueError("cancellation reason must not be blank")
        return self


class CompleteWorkPayload(StrictModel):
    input: CompletionInput
    attempt_id: UUID
    done_authorization_ref: str = Field(min_length=1)
    evidence: CompletionEvidence
    satisfied_work_ids: tuple[UUID, ...] = ()
    cancellations: tuple[CancellationProof, ...] = Field(default=(), max_length=128)
    @model_validator(mode="after")
    def _cancellations_unique(self) -> CompleteWorkPayload:
        ids = [proof.work_id for proof in self.cancellations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate cancellation work_id")
        return self


class RiderProof(StrictModel):
    """Owner ruling 2026-08-22 (close asymmetry, OMP-93): one historical work
    item riding a close attempt. evidence is batch-owned proof text; it enters
    the audited task body as indented data so the accepted report attests it."""

    work_id: UUID
    revision_id: UUID
    evidence: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _evidence_bounds(self) -> RiderProof:
        if len(self.evidence.encode("utf-8")) > 4096:
            raise ValueError("rider evidence exceeds 4096 UTF-8 bytes")
        if "\x00" in self.evidence:
            raise ValueError("rider evidence must not contain NUL")
        return self


class SealedRider(RiderProof):
    """Rider as sealed into the attempt at begin: title and acceptance-criteria
    snapshot plus the service-computed evidence digest. Completion requires
    this exact tuple."""

    title: str = Field(min_length=1)
    criteria: tuple[str, ...] = ()
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BeginCloseAttemptPayload(StrictModel):
    """Host-issued at literal owner /summary, AFTER candidate finalization: every
    field is host-computed identity, never model-supplied task text."""

    work_id: UUID
    attempt_id: UUID
    authorization_ref: str = Field(min_length=1)
    owner_session_id: str = Field(min_length=1)
    owner_session_started_at: datetime
    owner_session_start_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    repository: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    starting_dirty_paths: tuple[str, ...] = ()
    riders: tuple[RiderProof, ...] = Field(default=(), max_length=32)
    authorization_kind: Literal["summary", "legacy", "execution"] = "summary"
    execution_grant_id: UUID | None = None
    candidate_tree_sha: str | None = None
    original_request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    criteria_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_stamp_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    judge_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SealAuditManifestPayload(StrictModel):
    """manifest_id is server-minted inside the transaction; operation-envelope
    replay preserves it (same for launch and delivery ids below)."""

    attempt_id: UUID
    verification_receipt_id: UUID


class ReserveAuditorLaunchPayload(StrictModel):
    attempt_id: UUID
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1)


class CancelAuditorLaunchPayload(StrictModel):
    attempt_id: UUID
    launch_id: UUID


class SettleAuditorLaunchPayload(StrictModel):
    """transport_payload is deliberately untyped: canonical text, {"report"},
    {"text"}, arrays, extra-key objects, nested wrappers, and error envelopes
    must ALL reach WorkService normalization and earn a stable typed refusal —
    envelope validation never rejects a malformed report shape first."""

    attempt_id: UUID
    launch_id: UUID
    transport_payload: Any = None
    transport_failed: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> SettleAuditorLaunchPayload:
        if self.transport_failed and self.transport_payload is not None:
            raise ValueError("a failed transport carries no payload bytes")
        return self


class ReserveStageLaunchPayload(StrictModel):
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    resolved_selector: str | None = None
    resolved_provider: str | None = None
    resolved_model: str | None = None
    is_fallback: bool = False
    fallback_reason: str | None = None


class HandoffStageLaunchPayload(StrictModel):
    launch_id: UUID
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SettleStageLaunchPayload(StrictModel):
    launch_id: UUID
    outcome_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: dict[str, Any] = Field(default_factory=dict)
    served_selector: str | None = None
    served_model: str | None = None


class CancelStageLaunchPayload(StrictModel):
    launch_id: UUID
    reason: str = Field(min_length=1)


class ReconcileStageLaunchPayload(StrictModel):
    launch_id: UUID
    reason: str = Field(min_length=1)


class BeginStagePreflightPayload(StrictModel):
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=0, strict=True)
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    is_fallback: bool = False


class AdmitStagePreflightPayload(StrictModel):
    transport_attempt_id: UUID
    logical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CancelStagePreflightPayload(StrictModel):
    transport_attempt_id: UUID
    logical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)


class RecordStagePreflightPayload(StrictModel):
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    session_id: str | None = None
    role: StageLaunchRole
    tool_call_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport_attempt_id: UUID
    ordinal: int = Field(ge=0, strict=True)
    requested_selector: str = Field(min_length=1)
    requested_provider: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_api: str = Field(min_length=1)
    requested_effort: str = Field(min_length=1)
    requested_wire_model: str = Field(min_length=1)
    is_fallback: bool = False
    outcome: StagePreflightOutcome
    stop_reason: str | None = None
    error: str | None = None
    requests: int | None = Field(default=None, ge=0, strict=True)
    usage: StagePreflightUsage | None = None
    provider_request_id: str | None = None

    @model_validator(mode="after")
    def validate_outcome_and_error(self) -> RecordStagePreflightPayload:
        if self.outcome == StagePreflightOutcome.SELECTED:
            if self.error is not None:
                raise ValueError("selected preflight cannot have error")
        else:
            if self.error is None or not self.error.strip():
                raise ValueError(f"{self.outcome.value} preflight requires error")
        return self


class ReconcileStagePreflightPayload(StrictModel):
    transport_attempt_id: UUID
    logical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: UUID
    observation_id: UUID
    observed_at: AwareDatetime
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: StagePreflightDisposition
    requested_provider: str | None = None
    provider_request_id: str | None = None
    requests: int | None = Field(default=None, ge=0, strict=True)
    usage: StagePreflightUsage | None = None
    stop_reason: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_disposition_invariants(self) -> ReconcileStagePreflightPayload:
        if self.disposition == StagePreflightDisposition.COMPLETED:
            if not self.provider_request_id or not self.provider_request_id.strip():
                raise ValueError("completed preflight reconciliation requires provider_request_id")
            if self.error is not None:
                raise ValueError("completed preflight reconciliation cannot have error")
        elif self.disposition == StagePreflightDisposition.FAILED:
            if not self.provider_request_id or not self.provider_request_id.strip():
                raise ValueError("failed preflight reconciliation requires provider_request_id")
            if self.error is None or not self.error.strip():
                raise ValueError("failed preflight reconciliation requires error")
        elif self.disposition == StagePreflightDisposition.CONFIRMED_ABSENT:
            if self.provider_request_id is not None:
                raise ValueError("confirmed_absent preflight reconciliation cannot have provider_request_id")
            if self.usage is not None:
                raise ValueError("confirmed_absent preflight reconciliation cannot have usage")
            if self.requests is not None and self.requests != 0:
                raise ValueError("confirmed_absent preflight reconciliation requires known request count zero")
            if self.error is None or not self.error.strip():
                raise ValueError("confirmed_absent preflight reconciliation requires error explaining absence")
        return self


class CreateBudgetScopePayload(StrictModel):
    scope_id: UUID
    parent_scope_id: UUID | None = None
    kind: BudgetScopeKind
    policy_version: str = Field(min_length=1)
    work_id: UUID | None = None
    session_id: str | None = None
    limits: dict[str, str]


class ReserveBudgetPayload(StrictModel):
    scope_id: UUID
    account_id: UUID
    logical_call_id: UUID
    transport_attempt_id: UUID
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    resource: BudgetResource
    worst_case_drawdown: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    context_limit: int = Field(gt=0)
    output_limit: int = Field(gt=0)
    expires_at: datetime
    launch_id: UUID | None = None
    quote_id: UUID | None = None


class ClaimBudgetPayload(StrictModel):
    reservation_id: UUID
    fence: int = Field(ge=1)


class SettleBudgetPayload(StrictModel):
    reservation_id: UUID
    transport_attempt_id: UUID
    fence: int = Field(ge=1)
    state: Literal["settled", "unresolved"]
    actual_drawdown: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    usage: dict[str, int] = Field(default_factory=dict)
    provenance: Literal["provider_observed", "locally_estimated", "unknown"]
    provider_request_id: str | None = None
    outcome: Literal["success", "error", "timeout", "cancelled", "unknown"]


class CancelBudgetPayload(StrictModel):
    reservation_id: UUID
    fence: int = Field(ge=1)
    verified_unsent: bool = False


class ExpireBudgetPayload(StrictModel):
    reservation_id: UUID
    logical_call_id: UUID
    transport_attempt_id: UUID
    fence: int = Field(ge=1)
    expected_state: Literal["reserved_unsent", "potentially_sent"]


class IssueFrontierExceptionPayload(StrictModel):
    scope_id: UUID
    question: str = Field(min_length=1)
    route: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    context_limit: int = Field(gt=0)
    output_limit: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    resource: BudgetResource
    resource_limit: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    expires_at: datetime


class PutProviderAccountPayload(StrictModel):
    account_id: UUID
    provider: str = Field(min_length=1)
    account_identity: str = Field(min_length=1)
    entitlement_evidence: str = Field(min_length=1)
    evidence_observed_at: datetime
    billing_mode: Literal["subscription", "metered", "purchased_credit", "local"]
    rate_card_version: str | None = None
    observed_balance: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    balance_provenance: Literal["provider_observed", "locally_estimated", "unknown"]
    reset_at: datetime | None = None
    concurrency_limit: int = Field(gt=0)
    budget_resource: BudgetResource | None = None

    @model_validator(mode="after")
    def validate_invariants(self) -> PutProviderAccountPayload:
        if self.rate_card_version is not None and not self.rate_card_version.strip():
            raise ValueError("rate_card_version cannot be empty")
        if self.observed_balance is not None and self.balance_provenance == "unknown":
            raise ValueError("observed_balance cannot be set when balance_provenance is unknown")
        if self.reset_at is not None and self.reset_at <= self.evidence_observed_at:
            raise ValueError("reset_at must be in the future relative to evidence_observed_at")
        return self


class RegisterRateCardPayload(StrictModel):
    rate_card_id: UUID
    provider: str = Field(min_length=1)
    version: str = Field(min_length=1)
    billing_modes: tuple[Literal["subscription", "metered", "purchased_credit", "local"], ...]
    effective_from: AwareDatetime
    effective_until: AwareDatetime | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_prices: dict[str, str]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_source: str = Field(min_length=1)
    observed_at: AwareDatetime
    qualification: RateCardQualification

    @model_validator(mode="after")
    def validate_invariants(self) -> RegisterRateCardPayload:
        if not self.provider.strip():
            raise ValueError("provider cannot be blank")
        if not self.version.strip():
            raise ValueError("version cannot be blank")
        if not self.evidence_source.strip():
            raise ValueError("evidence_source cannot be blank")
        if not self.billing_modes:
            raise ValueError("billing_modes cannot be empty")
        if len(self.billing_modes) != len(set(self.billing_modes)):
            raise ValueError("billing_modes cannot contain duplicate entries")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("effective_until must be after effective_from")
        if not self.unit_prices:
            raise ValueError("unit_prices cannot be empty")
        for key, val in self.unit_prices.items():
            if not key or not key.strip():
                raise ValueError("unit_prices keys cannot be blank")
            if not isinstance(val, str) or not re.match(r"^[0-9]+(?:\.[0-9]+)?$", val):
                raise ValueError(f"invalid decimal string for unit price '{key}': {val}")
            try:
                dec = Decimal(val)
            except InvalidOperation:
                raise ValueError(f"invalid decimal value for unit price '{key}': {val}")
            if not dec.is_finite() or dec < 0:
                raise ValueError(f"unit price '{key}' must be finite and non-negative")
        return self


class QuoteBudgetPayload(StrictModel):
    work_id: UUID
    revision_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    grant_id: UUID | None = None
    role: StageLaunchRole
    launch_id: UUID | None = None
    account_id: UUID | None = None
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    usage_ceiling: dict[str, int]

    @model_validator(mode="after")
    def validate_invariants(self) -> QuoteBudgetPayload:
        if not self.provider.strip():
            raise ValueError("provider cannot be blank")
        if not self.model.strip():
            raise ValueError("model cannot be blank")
        if not self.effort.strip():
            raise ValueError("effort cannot be blank")
        if not self.usage_ceiling:
            raise ValueError("usage_ceiling cannot be empty")
        has_positive = False
        for key, val in self.usage_ceiling.items():
            if not key or not key.strip():
                raise ValueError("usage_ceiling keys cannot be blank")
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ValueError(f"usage_ceiling value for '{key}' must be an integer >= 0")
            if val > 0:
                has_positive = True
        if not has_positive:
            raise ValueError("usage_ceiling must contain at least one value > 0")
        return self


class AssociateCandidateSourcePayload(StrictModel):
    candidate_id: UUID
    work_id: UUID
    revision_id: UUID
    repository_id: UUID
    source_version_id: str = Field(min_length=1)
    snapshot_id: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    analyzed_commit: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    tree_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    association_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer: str = Field(min_length=1)
    producer_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttestCheckpointDeliveryPayload(StrictModel):
    event_id: UUID
    owner_session_id: str = Field(min_length=1)
    rendered_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["delivered", "failed", "waived"]
    authorization_ref: str | None = None

    @model_validator(mode="after")
    def validate_waiver(self) -> AttestCheckpointDeliveryPayload:
        if (self.status == "waived") != (self.authorization_ref is not None):
            raise ValueError(
                "waived deliveries carry an owner authorization reference; others never do"
            )
        return self


class RecordProjectHealthPayload(StrictModel):
    project_id: UUID
    health: Literal["onTrack", "atRisk", "offTrack"]


class StageImportBatchPayload(StrictModel):
    import_batch_id: UUID
    raw_export_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromoteImportBatchPayload(StrictModel):
    import_batch_id: UUID


class ActivateCutoverPayload(StrictModel):
    manifest: CutoverManifest


class AttestCutoverPlanPayload(StrictModel):
    """The anointed first WorkService mutation: binds the approved plan bytes to the
    imported ledger item. Non-candidate-mutating so the gate-nominated request can
    never be rejected by domain candidate rules."""

    epoch_id: UUID
    work_id: UUID
    plan_name: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_artifact: str


class ExecutionProvenanceEnvelope(StrictModel):
    owner_input_id: str = Field(min_length=1)
    owner_session_id: str = Field(min_length=1)
    normalized_command: str = Field(min_length=1)
    workspace_id: UUID
    repository: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    issued_at: datetime


class ExecutionGrantItemClaim(StrictModel):
    work_id: UUID
    revision_id: UUID
    position: int = Field(ge=0)
    original_request: str = Field(min_length=1)
    original_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_git_baseline: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    project_id: UUID | None = None
    active_blocker_ids: tuple[UUID, ...] = ()


class ExecutionJudgeManifestV1(StrictModel):
    auditor_agent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_code_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_migration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionJudgeManifestV2(ExecutionJudgeManifestV1):
    manifest_version: Literal[2]
    audit_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_stage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


ExecutionJudgeManifest = ExecutionJudgeManifestV2 | ExecutionJudgeManifestV1


class BeginExecutionPayload(StrictModel):
    grant_id: UUID
    provenance: ExecutionProvenanceEnvelope
    remote_ref: str = Field(min_length=1)
    mode: Literal["single", "queue"]
    items: tuple[ExecutionGrantItemClaim, ...] = Field(min_length=1)
    expected_focus_version: int = Field(ge=0)
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_manifest: ExecutionJudgeManifestV2

    @field_validator("remote_ref")
    @classmethod
    def validate_remote_ref(cls, v: str) -> str:
        if not isinstance(v, str) or not v.startswith("refs/heads/"):
            raise ValueError("remote_ref must start with refs/heads/")
        branch = v[len("refs/heads/") :]
        if not branch:
            raise ValueError("remote_ref branch name cannot be empty")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError(
                "remote_ref cannot contain whitespace or control characters"
            )
        if ".." in v or "@{" in v or "//" in v:
            raise ValueError("remote_ref cannot contain .., @{, or consecutive slashes")
        if any(c in v for c in ("~", "^", ":", "?", "*", "[", "\\")):
            raise ValueError("remote_ref contains invalid git ref characters")
        parts = branch.split("/")
        for part in parts:
            if not part:
                raise ValueError("remote_ref components cannot be empty")
            if part.startswith(".") or part.endswith("."):
                raise ValueError("remote_ref components cannot start or end with a dot")
            if part.endswith(".lock"):
                raise ValueError("remote_ref components cannot end with .lock")
            if part == "@":
                raise ValueError("remote_ref cannot be '@'")
        if v.endswith(("/", ".")):
            raise ValueError("remote_ref cannot end with a slash or dot")
        return v


class ActivateExecutionItemPayload(StrictModel):
    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    position: int = Field(ge=0)
    work_id: UUID
    expected_revision_id: UUID
    git_baseline: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_focus_version: int = Field(ge=0)
    expected_project_id: UUID | None = None
    expected_blocker_ids: tuple[UUID, ...] = ()


class SealExecutionCriteriaPayload(StrictModel):
    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    work_id: UUID
    expected_revision_id: UUID
    criteria: tuple[str, ...] = Field(min_length=1)
    description_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StampExecutionPlanPayload(StrictModel):
    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    work_id: UUID
    revision_id: UUID
    candidate_id: UUID
    plan_file: str = Field(min_length=1)
    plan_body: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approach: tuple[str, ...] = Field(min_length=1)
    verification: tuple[str, ...] = Field(min_length=1)
    paths: tuple[str, ...]
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SetExecutionStatePayload(StrictModel):
    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    target_state: Literal["active", "paused", "stopped", "canceled"]
    reason: str | None = None
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompleteExecutionItemPayload(StrictModel):
    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    work_id: UUID
    attempt_id: UUID
    evidence: CompletionEvidence
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

class BeginExecutionCommand(StrictModel):
    type: Literal["begin_execution"]
    payload: BeginExecutionPayload


class ActivateExecutionItemCommand(StrictModel):
    type: Literal["activate_execution_item"]
    payload: ActivateExecutionItemPayload


class SealExecutionCriteriaCommand(StrictModel):
    type: Literal["seal_execution_criteria"]
    payload: SealExecutionCriteriaPayload


class StampExecutionPlanCommand(StrictModel):
    type: Literal["stamp_execution_plan"]
    payload: StampExecutionPlanPayload


class SetExecutionStateCommand(StrictModel):
    type: Literal["set_execution_state"]
    payload: SetExecutionStatePayload


class CompleteExecutionItemCommand(StrictModel):
    type: Literal["complete_execution_item"]
    payload: CompleteExecutionItemPayload


class CreateWorkBatchCommand(StrictModel):
    type: Literal["create_work_batch"]
    payload: CreateWorkBatchPayload


class ReviseWorkCommand(StrictModel):
    type: Literal["revise_work"]
    payload: ReviseWorkPayload


class SetWorkStateCommand(StrictModel):
    type: Literal["set_work_state"]
    payload: SetWorkStatePayload


class PutRelationCommand(StrictModel):
    type: Literal["put_relation"]
    payload: PutRelationPayload


class RemoveRelationCommand(StrictModel):
    type: Literal["remove_relation"]
    payload: RemoveRelationPayload


class SetFocusCommand(StrictModel):
    type: Literal["set_focus"]
    payload: SetFocusPayload


class ClearFocusCommand(StrictModel):
    type: Literal["clear_focus"]
    payload: ClearFocusPayload


class AppendEvidenceCommand(StrictModel):
    type: Literal["append_evidence"]
    payload: AppendEvidencePayload


class FinalizeCandidateCommand(StrictModel):
    type: Literal["finalize_candidate"]
    payload: FinalizeCandidatePayload


class RecordCloseoutReviewCommand(StrictModel):
    type: Literal["record_closeout_review"]
    payload: RecordCloseoutReviewPayload


class CompleteWorkCommand(StrictModel):
    type: Literal["complete_work"]
    payload: CompleteWorkPayload


class CreateSameSessionChildCommand(StrictModel):
    type: Literal["create_same_session_child"]
    payload: CreateSameSessionChildPayload


class BeginCloseAttemptCommand(StrictModel):
    type: Literal["begin_close_attempt"]
    payload: BeginCloseAttemptPayload


class SealAuditManifestCommand(StrictModel):
    type: Literal["seal_audit_manifest"]
    payload: SealAuditManifestPayload


class ReserveAuditorLaunchCommand(StrictModel):
    type: Literal["reserve_auditor_launch"]
    payload: ReserveAuditorLaunchPayload


class CancelAuditorLaunchCommand(StrictModel):
    type: Literal["cancel_auditor_launch"]
    payload: CancelAuditorLaunchPayload


class SettleAuditorLaunchCommand(StrictModel):
    type: Literal["settle_auditor_launch"]
    payload: SettleAuditorLaunchPayload


class ReserveStageLaunchCommand(StrictModel):
    type: Literal["reserve_stage_launch"]
    payload: ReserveStageLaunchPayload


class HandoffStageLaunchCommand(StrictModel):
    type: Literal["handoff_stage_launch"]
    payload: HandoffStageLaunchPayload


class SettleStageLaunchCommand(StrictModel):
    type: Literal["settle_stage_launch"]
    payload: SettleStageLaunchPayload


class CancelStageLaunchCommand(StrictModel):
    type: Literal["cancel_stage_launch"]
    payload: CancelStageLaunchPayload


class ReconcileStageLaunchCommand(StrictModel):
    type: Literal["reconcile_stage_launch"]
    payload: ReconcileStageLaunchPayload


class BeginStagePreflightCommand(StrictModel):
    type: Literal["begin_stage_preflight"]
    payload: BeginStagePreflightPayload


class RecordStagePreflightCommand(StrictModel):
    type: Literal["record_stage_preflight"]
    payload: RecordStagePreflightPayload


class AdmitStagePreflightCommand(StrictModel):
    type: Literal["admit_stage_preflight"]
    payload: AdmitStagePreflightPayload


class CancelStagePreflightCommand(StrictModel):
    type: Literal["cancel_stage_preflight"]
    payload: CancelStagePreflightPayload


class ReconcileStagePreflightCommand(StrictModel):
    type: Literal["reconcile_stage_preflight"]
    payload: ReconcileStagePreflightPayload


class CreateBudgetScopeCommand(StrictModel):
    type: Literal["create_budget_scope"]
    payload: CreateBudgetScopePayload


class ReserveBudgetCommand(StrictModel):
    type: Literal["reserve_budget"]
    payload: ReserveBudgetPayload


class ClaimBudgetCommand(StrictModel):
    type: Literal["claim_budget"]
    payload: ClaimBudgetPayload


class SettleBudgetCommand(StrictModel):
    type: Literal["settle_budget"]
    payload: SettleBudgetPayload


class CancelBudgetCommand(StrictModel):
    type: Literal["cancel_budget"]
    payload: CancelBudgetPayload


class ExpireBudgetCommand(StrictModel):
    type: Literal["expire_budget"]
    payload: ExpireBudgetPayload


class IssueFrontierExceptionCommand(StrictModel):
    type: Literal["issue_frontier_exception"]
    payload: IssueFrontierExceptionPayload


class PutProviderAccountCommand(StrictModel):
    type: Literal["put_provider_account"]
    payload: PutProviderAccountPayload


class RegisterRateCardCommand(StrictModel):
    type: Literal["register_rate_card"]
    payload: RegisterRateCardPayload


class QuoteBudgetCommand(StrictModel):
    type: Literal["quote_budget"]
    payload: QuoteBudgetPayload


class AssociateCandidateSourceCommand(StrictModel):
    type: Literal["associate_candidate_source"]
    payload: AssociateCandidateSourcePayload


class AttestCheckpointDeliveryCommand(StrictModel):
    type: Literal["attest_checkpoint_delivery"]
    payload: AttestCheckpointDeliveryPayload


class RecordProjectHealthCommand(StrictModel):
    type: Literal["record_project_health"]
    payload: RecordProjectHealthPayload


class StageImportBatchCommand(StrictModel):
    type: Literal["stage_import_batch"]
    payload: StageImportBatchPayload


class PromoteImportBatchCommand(StrictModel):
    type: Literal["promote_import_batch"]
    payload: PromoteImportBatchPayload


class ActivateCutoverCommand(StrictModel):
    type: Literal["activate_cutover"]
    payload: ActivateCutoverPayload


class AttestCutoverPlanCommand(StrictModel):
    type: Literal["attest_cutover_plan"]
    payload: AttestCutoverPlanPayload


class ResearchComponentKind(StrEnum):
    WORKER = "worker"
    EVALUATOR = "evaluator"
    POLICY = "policy"
    AUDIT = "audit"
    RELEASE = "release"
    ENVIRONMENT = "environment"


class ResearchRole(StrEnum):
    CAMPAIGN_PLANNER = "campaign_planner"
    RESEARCH_WORKER = "research_worker"
    HYPOTHESIS_GENERATOR = "hypothesis_generator"
    METHOD_CRITIC = "method_critic"
    IMPLEMENTER = "implementer"
    SELECTOR = "selector"
    ANALYST = "analyst"
    SCIENTIFIC_REVIEWER = "scientific_reviewer"
    HARNESS_RESEARCHER = "harness_researcher"
    NATIVE_AUDITOR = "native_auditor"
    SYNTHESIZER = "synthesizer"


ResearchCapability = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", max_length=128)
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _require_sorted_unique(values: tuple[Any, ...], label: str) -> None:
    lst = list(values)
    if len(lst) != len(set(lst)):
        raise ValueError(f"{label} must contain unique values")
    if lst != sorted(lst):
        raise ValueError(f"{label} must be sorted")


class ResearchComponentDescriptor(StrictModel):
    """Canonical, hash-defining declaration. Field set IS the identity."""

    contract_version: Literal["research-component.v1"]
    kind: ResearchComponentKind
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    artifact_sha256: Sha256Hex
    roles: tuple[ResearchRole, ...] = ()
    capabilities: tuple[ResearchCapability, ...] = ()

    @model_validator(mode="after")
    def validate_sorted_unique(self) -> ResearchComponentDescriptor:
        _require_sorted_unique(self.roles, "roles")
        _require_sorted_unique(self.capabilities, "capabilities")
        return self


class ResearchComponent(StrictModel):
    component_sha256: Sha256Hex
    workspace_id: UUID
    kind: ResearchComponentKind
    descriptor: ResearchComponentDescriptor
    registered_at: datetime


class ResearchCompatibilityManifest(StrictModel):
    contract_version: Literal["research-compatibility.v1"]
    workers: tuple[Sha256Hex, ...] = ()
    evaluators: tuple[Sha256Hex, ...] = ()
    audits: tuple[Sha256Hex, ...] = ()
    releases: tuple[Sha256Hex, ...] = ()
    environments: tuple[Sha256Hex, ...] = ()

    @model_validator(mode="after")
    def validate_sorted_unique(self) -> ResearchCompatibilityManifest:
        _require_sorted_unique(self.workers, "workers")
        _require_sorted_unique(self.evaluators, "evaluators")
        _require_sorted_unique(self.audits, "audits")
        _require_sorted_unique(self.releases, "releases")
        _require_sorted_unique(self.environments, "environments")
        return self


class RegisterResearchComponentPayload(StrictModel):
    component_sha256: Sha256Hex
    descriptor: ResearchComponentDescriptor


class RegisterResearchComponentCommand(StrictModel):
    type: Literal["register_research_component"]
    payload: RegisterResearchComponentPayload


class ResearchDomain(StrEnum):
    ENGINEERING = "engineering"
    OMP_HARNESS = "omp_harness"
    MACHINE_LEARNING = "machine_learning"
    LITERATURE = "literature"
    SIMULATION = "simulation"
    EXTERNAL_INSTRUMENT = "external_instrument"


class ResearchResourceVector(StrictModel):
    cpu_seconds: int | None = Field(default=None, ge=0)
    gpu_seconds: int | None = Field(default=None, ge=0)
    max_wall_seconds: int | None = Field(default=None, ge=0)
    memory_mib: int | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retrieval_requests: int | None = Field(default=None, ge=0)


class ResearchCampaignSpec(StrictModel):
    objective: str = Field(min_length=1)
    evaluation_protocol_id: str = Field(min_length=1)
    evaluation_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_policy_ref: str = Field(min_length=1)
    resource_vector: ResearchResourceVector | None = None
    authorized_data_classification: tuple[str, ...] = Field(default_factory=tuple)
    candidate_mapping_policy: str = Field(min_length=1)


ResearchCampaignState = Literal[
    "draft",
    "admitted",
    "running",
    "paused",
    "evaluating",
    "blocked",
    "concluded",
    "cancelled",
]
ResearchCampaignOutcome = Literal[
    "supported",
    "refuted",
    "inconclusive",
    "resource_exhausted",
    "externally_blocked",
]


class ResearchAction(StrEnum):
    RETRIEVE = "retrieve"
    DRAFT = "draft"
    REPAIR = "repair"
    REFINE = "refine"
    CHALLENGE = "challenge"
    COMBINE = "combine"
    EVALUATE = "evaluate"
    REPLICATE = "replicate"
    DEEPEN = "deepen"
    PRUNE = "prune"
    SYNTHESIZE = "synthesize"
    ESCALATE = "escalate"
    CONCLUDE = "conclude"


class ResearchBlockedDependency(StrictModel):
    kind: Literal["work_item", "budget_scope", "capability", "external"]
    ref: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2048)


class ResearchCampaign(StrictModel):
    campaign_id: UUID
    workspace_id: UUID
    work_id: UUID
    revision_id: UUID
    domain: ResearchDomain
    spec: ResearchCampaignSpec
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state: ResearchCampaignState
    cancel_reason: str | None = None
    created_at: datetime
    admitted_at: datetime | None = None
    cancelled_at: datetime | None = None
    outcome: ResearchCampaignOutcome | None = None
    outcome_reason: str | None = None
    concluded_at: datetime | None = None
    blocked_dependency: ResearchBlockedDependency | None = None
    blocked_from_state: (
        Literal["admitted", "running", "paused", "evaluating"] | None
    ) = None
    compatibility: ResearchCompatibilityManifest | None = None
    compatibility_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ResearchTrial(StrictModel):
    trial_id: UUID
    workspace_id: UUID
    campaign_id: UUID
    work_id: UUID
    decision_id: UUID
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int | None = None
    hardware_class: str | None = None
    resource_request: dict[str, object] | None = None
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["proposed", "archived"]
    archived_reason: str | None = None
    proposed_at: datetime
    archived_at: datetime | None = None
    action: ResearchAction | None = None
    reason: str | None = None


ResearchIssuerKind = Literal["legacy_autoresearch", "candidate_authored"]
ResearchExecutionStatus = Literal[
    "completed", "crashed", "timed_out", "canceled", "unknown"
]


class ResearchObservation(StrictModel):
    observation_id: UUID
    workspace_id: UUID
    campaign_id: UUID
    trial_id: UUID | None = None
    issuer_kind: ResearchIssuerKind
    source_ref: str = Field(min_length=1)
    execution_status: ResearchExecutionStatus
    commit_sha: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    payload: dict[str, object]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    recorded_at: datetime


class ResearchDeliverableBinding(StrictModel):
    trial_id: UUID
    workspace_id: UUID
    campaign_id: UUID
    work_id: UUID
    revision_id: UUID
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_candidate_id: UUID
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_at: datetime


class CreateResearchCampaignPayload(StrictModel):
    campaign_id: UUID
    work_id: UUID
    revision_id: UUID
    domain: ResearchDomain
    spec: ResearchCampaignSpec
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdmitResearchCampaignPayload(StrictModel):
    campaign_id: UUID
    work_id: UUID
    revision_id: UUID
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility: ResearchCompatibilityManifest
    compatibility_sha256: Sha256Hex


class CancelResearchCampaignPayload(StrictModel):
    campaign_id: UUID
    work_id: UUID
    reason: str = Field(min_length=1)


class ProposeResearchTrialPayload(StrictModel):
    trial_id: UUID
    campaign_id: UUID
    work_id: UUID
    decision_id: UUID
    action: ResearchAction
    reason: str | None = Field(default=None, max_length=2048)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int | None = None
    hardware_class: str | None = None
    resource_request: dict[str, object] | None = None
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecordResearchObservationPayload(StrictModel):
    observation_id: UUID
    campaign_id: UUID
    trial_id: UUID | None = None
    issuer_kind: ResearchIssuerKind
    source_ref: str = Field(min_length=1)
    execution_status: ResearchExecutionStatus
    commit_sha: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    payload: dict[str, object]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime


class BindResearchDeliverablePayload(StrictModel):
    trial_id: UUID
    work_id: UUID
    revision_id: UUID
    campaign_id: UUID
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_candidate_id: UUID
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SetResearchCampaignStatePayload(StrictModel):
    campaign_id: UUID
    work_id: UUID
    expected_state: Literal["admitted", "running", "paused", "evaluating", "blocked"]
    target_state: Literal["admitted", "running", "paused", "evaluating", "blocked"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blocked_dependency: ResearchBlockedDependency | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> SetResearchCampaignStatePayload:
        if self.expected_state == self.target_state:
            raise ValueError("expected_state and target_state must differ")
        if (self.target_state == "blocked") != (self.blocked_dependency is not None):
            raise ValueError(
                "blocked_dependency is required exactly when target_state is blocked"
            )
        return self


class ConcludeResearchCampaignPayload(StrictModel):
    campaign_id: UUID
    work_id: UUID
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: ResearchCampaignOutcome
    reason: str = Field(min_length=1, max_length=4096)


class CreateResearchCampaignCommand(StrictModel):
    type: Literal["create_research_campaign"]
    payload: CreateResearchCampaignPayload


class AdmitResearchCampaignCommand(StrictModel):
    type: Literal["admit_research_campaign"]
    payload: AdmitResearchCampaignPayload


class CancelResearchCampaignCommand(StrictModel):
    type: Literal["cancel_research_campaign"]
    payload: CancelResearchCampaignPayload


class ProposeResearchTrialCommand(StrictModel):
    type: Literal["propose_research_trial"]
    payload: ProposeResearchTrialPayload


class RecordResearchObservationCommand(StrictModel):
    type: Literal["record_research_observation"]
    payload: RecordResearchObservationPayload


class BindResearchDeliverableCommand(StrictModel):
    type: Literal["bind_research_deliverable"]
    payload: BindResearchDeliverablePayload


class SetResearchCampaignStateCommand(StrictModel):
    type: Literal["set_research_campaign_state"]
    payload: SetResearchCampaignStatePayload


class ConcludeResearchCampaignCommand(StrictModel):
    type: Literal["conclude_research_campaign"]
    payload: ConcludeResearchCampaignPayload


Command = Annotated[
    CreateWorkBatchCommand
    | CreateSameSessionChildCommand
    | ReviseWorkCommand
    | SetWorkStateCommand
    | PutRelationCommand
    | RemoveRelationCommand
    | SetFocusCommand
    | ClearFocusCommand
    | AppendEvidenceCommand
    | FinalizeCandidateCommand
    | BeginCloseAttemptCommand
    | SealAuditManifestCommand
    | ReserveAuditorLaunchCommand
    | CancelAuditorLaunchCommand
    | SettleAuditorLaunchCommand
    | ReserveStageLaunchCommand
    | HandoffStageLaunchCommand
    | SettleStageLaunchCommand
    | CancelStageLaunchCommand
    | ReconcileStageLaunchCommand
    | BeginStagePreflightCommand
    | AdmitStagePreflightCommand
    | CancelStagePreflightCommand
    | RecordStagePreflightCommand
    | ReconcileStagePreflightCommand
    | CreateBudgetScopeCommand
    | ReserveBudgetCommand
    | ClaimBudgetCommand
    | SettleBudgetCommand
    | CancelBudgetCommand
    | ExpireBudgetCommand
    | IssueFrontierExceptionCommand
    | PutProviderAccountCommand
    | RegisterRateCardCommand
    | QuoteBudgetCommand
    | AssociateCandidateSourceCommand
    | RegisterResearchComponentCommand
    | CreateResearchCampaignCommand
    | AdmitResearchCampaignCommand
    | CancelResearchCampaignCommand
    | ProposeResearchTrialCommand
    | RecordResearchObservationCommand
    | BindResearchDeliverableCommand
    | SetResearchCampaignStateCommand
    | ConcludeResearchCampaignCommand
    | AttestCheckpointDeliveryCommand
    | RecordCloseoutReviewCommand
    | CompleteWorkCommand
    | RecordProjectHealthCommand
    | StageImportBatchCommand
    | PromoteImportBatchCommand
    | ActivateCutoverCommand
    | AttestCutoverPlanCommand
    | BeginExecutionCommand
    | ActivateExecutionItemCommand
    | SealExecutionCriteriaCommand
    | StampExecutionPlanCommand
    | SetExecutionStateCommand
    | CompleteExecutionItemCommand,
    Field(discriminator="type"),
]


class CommandEnvelope(StrictModel):
    api_version: Literal["work.omp.dev/v1"]
    workspace_id: UUID
    operation_id: UUID
    request_id: UUID
    correlation_id: UUID
    command: Command


class WorkflowMapping(StrictModel):
    intake: Literal["create_work_batch"] = Field(alias="/intake")
    capture: Literal["create_work_batch"] = Field(alias="/capture")
    plan: Literal["candidate allocation plus plan evidence"] = Field(alias="/plan")
    now: Literal["focus reads/set/clear"] = Field(alias="/now")
    summary: Literal[
        "close attempt: finalize, seal manifest, bounded audit, closeout review"
    ] = Field(alias="/summary")
    done: Literal["complete_work"] = Field(alias="/done")
    execute: Literal["autonomous single or queue delivery cycle"] = Field(
        alias="/execute"
    )


class SourceScope(StrictModel):
    include: tuple[str, ...]
    exclude: tuple[str, ...]


class SecurityPolicy(StrictModel):
    database_roles: tuple[
        Literal[
            "omp_work_owner",
            "omp_work_migrator",
            "omp_work_app",
            "omp_work_importer",
            "omp_work_readonly",
            "omp_work_backup",
        ],
        ...,
    ]
    owner_host_scopes: tuple[str, ...]
    task_agent_scopes: tuple[Literal["work.candidate.read"], ...]
    auditor_scopes: tuple[Literal["work.candidate.read"], ...]
    importer_scopes: tuple[Literal["work.import"], ...]
    operator_scopes: tuple[Literal["work.operate"], ...]
    rls: Literal["force_workspace_actor_claims_no_public_no_bypassrls"]
    credentials: Literal[
        "operator_managed_mode_0600_host_only_no_agent_or_postgres_dsn"
    ]


class DependencyGraph(StrictModel):
    home_142: tuple[Literal["HOME-143", "HOME-144", "HOME-145"], ...] = Field(
        alias="HOME-142"
    )
    home_143: tuple[Literal["HOME-144", "HOME-146"], ...] = Field(alias="HOME-143")
    home_144: tuple[Literal["HOME-146", "HOME-147"], ...] = Field(alias="HOME-144")
    home_145: tuple[Literal["HOME-146"], ...] = Field(alias="HOME-145")
    home_146: tuple[Literal["HOME-148"], ...] = Field(alias="HOME-146")
    home_147: tuple[Literal["HOME-148"], ...] = Field(alias="HOME-147")
    home_148: tuple[Literal["HOME-149"], ...] = Field(alias="HOME-148")


class Contract(StrictModel):
    contract_version: Literal["work.omp.dev/v1"]
    transport: Literal["loopback_http"]
    reads: tuple[str, ...]
    command_types: tuple[str, ...]
    error_codes: tuple[str, ...]
    scopes: tuple[str, ...]
    workflow_mapping: WorkflowMapping
    source_scope: SourceScope
    dependency_graph: DependencyGraph
    security_policy: SecurityPolicy


class ImmutableRevisionExample(StrictModel):
    current_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_same_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_changed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    same_content: Literal["noop"]
    changed_content: Literal["append"]
    new_revision_number: Literal[2]


class RelationCycleExample(StrictModel):
    edges: tuple[str, ...]
    rejected: str
    error: Literal["relation_cycle"]
    related_triangle: Literal[True]


class IdempotencyExample(StrictModel):
    operation_id: UUID
    stored_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_state: Literal["replayed"]
    changed_body_error: Literal["idempotency_conflict"]


class StaleEvidenceExample(StrictModel):
    bound_revision: Literal[1]
    current_revision: Literal[2]
    error: Literal["stale_evidence"]


class PushedBranchExample(StrictModel):
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    matching_remote: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    mismatched_remote: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    matching_result: Literal["no_blockers"]
    mismatched_result: Literal["completion_blocked"]
    attempt_state: Literal["closeout_requested"]
    work_state: Literal["not_DONE"]


class CloseAttemptExample(StrictModel):
    """OMP-47: one live attempt per work item; every refusal is a typed event."""

    live_states: tuple[
        Literal[
            "active",
            "audit_ready",
            "auditor_in_flight",
            "audited",
            "closeout_requested",
        ],
        ...,
    ]
    terminal_states: tuple[
        Literal[
            "remediation_required",
            "blocked",
            "budget_exhausted",
            "superseded",
            "completed",
        ],
        ...,
    ]
    max_launches: Literal[3]
    max_accepted_reports: Literal[2]
    refusal_shape: Literal["status_refused_with_typed_event"]
    supersede_authority: Literal["new_literal_owner_summary_only"]


class SameSessionExample(StrictModel):
    """OMP-52: the child fix rides the parent attempt's audited candidate."""

    child_created: Literal["at_or_after_owner_session_start"]
    parent_relation: Literal["active_child_to_parent"]
    binds: tuple[
        Literal[
            "attempt_id",
            "owner_session_id",
            "base_commit",
            "fix_commit",
            "candidate_sha256",
        ],
        ...,
    ]
    replaces: tuple[Literal["plan", "candidate", "verification", "audit"], ...]
    never_bypasses: tuple[
        Literal[
            "owner_done", "parent_pass_audit", "delivery", "push", "candidate_freshness"
        ],
        ...,
    ]


class CutoverExample(StrictModel):
    anomalies: tuple[Anomaly, ...]
    parity_differences: tuple[str, ...]



class CompletionEvidenceExample(StrictModel):
    """OMP-247: typed completion claim validated against service-owned rows."""

    matching_result: Literal["no_blockers"]
    stale_candidate_result: Literal["completion_blocked"]
    foreign_receipt_result: Literal["completion_blocked"]
    self_asserted_audit_result: Literal["completion_blocked"]
    required_artifacts: tuple[Literal["verification", "audit", "push"], ...]

class ContractExamples(StrictModel):
    immutable_revision: ImmutableRevisionExample
    relation_cycle: RelationCycleExample
    idempotency: IdempotencyExample
    stale_evidence: StaleEvidenceExample
    pushed_branch: PushedBranchExample
    close_attempt: CloseAttemptExample
    same_session: SameSessionExample
    cutover: CutoverExample
    completion_evidence: CompletionEvidenceExample

class BindingManifest(StrictModel):
    paths: tuple[str, ...]


class Approval(StrictModel):
    contract_version: Literal["work.omp.dev/v1"]
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: Literal["owner"]
    approved_at: datetime
    issue: Literal[
        "HOME-142",
        "HOME-147",
        "HOME-148",
        "OMP-47",
        "OMP-67",
        "OMP-93",
        "OMP-99",
        "OMP-106",
        "OMP-123",
        "OMP-124",
        "OMP-140",
        "OMP-147",
        "OMP-180",
        "OMP-194",
        "OMP-222",
        "OMP-247",
        "OMP-279",
    ]


class EventsCursorPayload(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace_id: UUID
    after_sequence: int
    through_sequence: int


class RepositoryCursorPayload(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace_id: UUID
    created_at: AwareDatetime
    repository_id: UUID


class BudgetScopeCursorPayload(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace_id: UUID
    created_at: AwareDatetime
    scope_id: UUID


class WorkItemsCursorPayload(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace_id: UUID
    work_id: UUID
    created_at: AwareDatetime
