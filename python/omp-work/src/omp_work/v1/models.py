from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    ]
    detail: str


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


class ExecutionJudgeManifest(StrictModel):
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


class BeginExecutionPayload(StrictModel):
    grant_id: UUID
    provenance: ExecutionProvenanceEnvelope
    remote_ref: str = Field(min_length=1)
    mode: Literal["single", "queue"]
    items: tuple[ExecutionGrantItemClaim, ...] = Field(min_length=1)
    expected_focus_version: int = Field(ge=0)
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_manifest: ExecutionJudgeManifest

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
    push_receipt_id: UUID
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


class ContractExamples(StrictModel):
    immutable_revision: ImmutableRevisionExample
    relation_cycle: RelationCycleExample
    idempotency: IdempotencyExample
    stale_evidence: StaleEvidenceExample
    pushed_branch: PushedBranchExample
    close_attempt: CloseAttemptExample
    same_session: SameSessionExample
    cutover: CutoverExample


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
    ]
