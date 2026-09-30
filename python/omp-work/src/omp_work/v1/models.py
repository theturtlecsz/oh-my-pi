from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


hex64 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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
    INTAKE_PUBLICATION = "intake_publication"
    INTAKE_ADMISSION = "intake_admission"
    EXTERNAL_DELIVERY = "external_delivery"


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
    candidate_id: UUID | None = None
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
    def validate_candidate_binding(self) -> EvidenceReceipt:
        if self.candidate_id is None and self.kind is not EvidenceKind.EXTERNAL_DELIVERY:
            raise ValueError(
                "only external_delivery receipts may omit a candidate_id"
            )
        return self

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


class RecordExternalDeliveryPayload(StrictModel):
    """OMP-283: owner-recorded proof that a work item was delivered outside the
    ledger. evidence is kept verbatim; it must be non-blank, at most 4096 UTF-8
    bytes, and free of NUL and line breaks."""

    work_id: UUID
    revision_id: UUID
    evidence: str

    @field_validator("evidence")
    @classmethod
    def _evidence_bounds(cls, v: str) -> str:
        if len(v.encode()) > 4096:
            raise ValueError("external delivery evidence exceeds 4096 UTF-8 bytes")
        if not v.strip():
            raise ValueError("external delivery evidence must not be blank")
        if any(c in v for c in ("\x00", "\n", "\r")):
            raise ValueError(
                "external delivery evidence must not contain NUL or line breaks"
            )
        return v


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


class RecordAlarmSignalPayload(StrictModel):
    signal: Literal[
        "cost_threshold",
        "budget_exceeded",
        "safety_check_failed",
        "credential_appeared",
    ]
    work_id: UUID | None = None
    subject: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=500)

    @field_validator("subject", mode="before")
    @classmethod
    def _strip_subject(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("subject must not be blank")
        return v


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
    evidence: CompletionEvidence
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkipActiveItemPayload(StrictModel):
    """OMP-219: owner-only deferral of the grant's active item — it supersedes any
    open close attempt, the work item stays open, and the grant advances or
    completes without spending a close-attempt budget slot."""

    grant_id: UUID
    expected_grant_version: int = Field(ge=1)
    position: int = Field(ge=0)
    work_id: UUID
    expected_focus_version: int = Field(ge=0)
    judge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("skip reason must not be blank")
        if len(trimmed) > 240:
            raise ValueError("skip reason exceeds 240 characters")
        return trimmed


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


class SkipActiveItemCommand(StrictModel):
    type: Literal["skip_active_item"]
    payload: SkipActiveItemPayload


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


class RecordExternalDeliveryCommand(StrictModel):
    type: Literal["record_external_delivery"]
    payload: RecordExternalDeliveryPayload


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


class RecordAlarmSignalCommand(StrictModel):
    type: Literal["record_alarm_signal"]
    payload: RecordAlarmSignalPayload


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


class AssessBoundedIntakePayload(StrictModel):
    draft: BoundedIntakeDraft


class AssessBoundedIntakeCommand(StrictModel):
    type: Literal["assess_bounded_intake"]
    payload: AssessBoundedIntakePayload


class RecordFableAdviceCommand(StrictModel):
    type: Literal["record_fable_advice"]
    payload: RecordFableAdvicePayload


class AttestIntakeAdmissionCommand(StrictModel):
    type: Literal["attest_intake_admission"]
    payload: AttestIntakeAdmissionPayload


class PublishBoundedIntakePayload(StrictModel):
    draft: BoundedIntakeDraft
    assessment_operation_id: UUID
    ratified_semantic_sha256: hex64
    admission_work_id: UUID
    admission_revision_id: UUID
    admission_receipt_id: UUID


class PublishBoundedIntakeCommand(StrictModel):
    type: Literal["publish_bounded_intake"]
    payload: PublishBoundedIntakePayload


class AnswerIntakeDecisionPayload(StrictModel):
    work_id: UUID
    answer: Literal["approve"]


class AnswerIntakeDecisionCommand(StrictModel):
    type: Literal["answer_intake_decision"]
    payload: AnswerIntakeDecisionPayload


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

RESEARCH_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024
_RESEARCH_ARTIFACT_MAX_BASE64 = 4 * ((RESEARCH_ARTIFACT_MAX_BYTES + 2) // 3)
MediaType = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
    ),
]


class ResearchArtifactManifest(StrictModel):
    """Canonical custody declaration. The field set is the manifest identity."""

    contract_version: Literal["research-artifact.v1"]
    artifact_sha256: Sha256Hex
    size_bytes: int = Field(ge=0, le=RESEARCH_ARTIFACT_MAX_BYTES)
    media_type: MediaType
    name: str = Field(min_length=1, max_length=255)
    access_class: Literal["workspace"]
    issuer_kind: ResearchIssuerKind
    source_ref: str = Field(min_length=1, max_length=512)
    valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_name(self) -> ResearchArtifactManifest:
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.name):
            raise ValueError("name contains control characters")
        return self


class ResearchArtifact(StrictModel):
    workspace_id: UUID
    artifact_sha256: Sha256Hex
    manifest_sha256: Sha256Hex
    manifest: ResearchArtifactManifest
    registered_by: UUID
    registered_at: datetime


class RegisterResearchArtifactPayload(StrictModel):
    manifest_sha256: Sha256Hex
    manifest: ResearchArtifactManifest
    content_base64: str = Field(
        pattern=r"^[A-Za-z0-9+/]*={0,2}$", max_length=_RESEARCH_ARTIFACT_MAX_BASE64
    )


class RegisterResearchArtifactCommand(StrictModel):
    type: Literal["register_research_artifact"]
    payload: RegisterResearchArtifactPayload


class CollectResearchArtifactPayload(StrictModel):
    relative_path: str = Field(min_length=1, max_length=512)
    archive_member: str | None = Field(default=None, max_length=512)
    manifest_sha256: Sha256Hex
    manifest: ResearchArtifactManifest


class CollectResearchArtifactCommand(StrictModel):
    type: Literal["collect_research_artifact"]
    payload: CollectResearchArtifactPayload


class ResearchSourceAccess(StrictModel):
    access_class: Literal["workspace", "project"]
    project_id: UUID | None = None
    decision: Literal["allow", "deny"] | None = None

    @model_validator(mode="after")
    def project_pair(self) -> ResearchSourceAccess:
        if self.access_class == "workspace":
            if self.project_id is not None or self.decision is not None:
                raise ValueError("workspace access has no project decision")
        elif self.project_id is None or self.decision is None:
            raise ValueError("project access requires project_id and decision")
        return self


class ResearchSourceManifest(StrictModel):
    contract_version: Literal["research-source.v1"]
    source_id: UUID
    version: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=1024)
    status: Literal["ok", "inaccessible"]
    retention_until: AwareDatetime | None = None
    access: ResearchSourceAccess
    artifact_sha256: Sha256Hex | None = None


class ResearchSource(StrictModel):
    workspace_id: UUID
    source_id: UUID
    manifest_sha256: Sha256Hex
    manifest: ResearchSourceManifest
    status: Literal["ok", "inaccessible"]
    artifact_sha256: Sha256Hex | None = None
    registered_by: UUID
    registered_at: datetime


class RegisterResearchSourcePayload(StrictModel):
    manifest_sha256: Sha256Hex
    manifest: ResearchSourceManifest


class RegisterResearchSourceCommand(StrictModel):
    type: Literal["register_research_source"]
    payload: RegisterResearchSourcePayload


class ResearchDatasetManifest(StrictModel):
    contract_version: Literal["research-dataset.v1"]
    dataset_id: UUID
    snapshot_sha256: Sha256Hex
    source_id: UUID
    version: str = Field(min_length=1, max_length=128)
    retention_until: AwareDatetime | None = None
    access: ResearchSourceAccess
    artifact_sha256: Sha256Hex | None = None


class ResearchDataset(StrictModel):
    workspace_id: UUID
    dataset_id: UUID
    manifest_sha256: Sha256Hex
    manifest: ResearchDatasetManifest
    source_id: UUID
    artifact_sha256: Sha256Hex | None = None
    registered_by: UUID
    registered_at: datetime


class RegisterResearchDatasetPayload(StrictModel):
    manifest_sha256: Sha256Hex
    manifest: ResearchDatasetManifest


class RegisterResearchDatasetCommand(StrictModel):
    type: Literal["register_research_dataset"]
    payload: RegisterResearchDatasetPayload


class RecordResearchCachePayload(StrictModel):
    cache_key: str = Field(min_length=1, max_length=256)
    artifact_sha256: Sha256Hex


class RecordResearchCacheCommand(StrictModel):
    type: Literal["record_research_cache"]
    payload: RecordResearchCachePayload


class ClaimResearchReplicatePayload(StrictModel):
    artifact_sha256: Sha256Hex


class ClaimResearchReplicateCommand(StrictModel):
    type: Literal["claim_research_replicate"]
    payload: ClaimResearchReplicatePayload


class BindResearchReceiptManifestPayload(StrictModel):
    receipt_id: UUID
    artifact_sha256: Sha256Hex
    manifest_sha256: Sha256Hex


class BindResearchReceiptManifestCommand(StrictModel):
    type: Literal["bind_research_receipt_manifest"]
    payload: BindResearchReceiptManifestPayload


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


class StopReasonPayload(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


class EngageStopCommand(StrictModel):
    type: Literal["engage_stop"]
    payload: StopReasonPayload


class ReleaseStopCommand(StrictModel):
    type: Literal["release_stop"]
    payload: StopReasonPayload


class MissionStatus(StrEnum):
    """D29 status set. WorkService enforces the transitions between these."""

    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


MissionKind = Literal[
    "research.run",
    "engineering.execute",
    "architecture.review",
    "change.review",
]


class MissionDraft(StrictModel):
    """D29 mission record: the objective, its envelope, and the policies that
    govern it. ``created_by``/``created_at`` are service-stamped, never sent.

    ``risk_policy``, ``approval_policy``, and ``effort_policy`` are policy ids
    (OMP-417/418); ``budget_policy`` is the mission's envelope, from which item
    budgets (OMP-404) are drawn.
    """

    project_id: UUID
    objective: str = Field(min_length=1, max_length=4000)
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    artifact_expectations: tuple[str, ...] = ()
    requested_capabilities: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    approval_classes: tuple[str, ...] = ()
    risk_policy: str = Field(min_length=1, max_length=200)
    approval_policy: str = Field(min_length=1, max_length=200)
    effort_policy: str = Field(min_length=1, max_length=200)
    budget_policy: ItemBudget | None = None
    priority: int = Field(default=2, ge=0, le=3)
    continuation_of: UUID | None = None
    parent_mission: UUID | None = None
    kind: MissionKind = "engineering.execute"


class SubmitMissionPayload(StrictModel):
    mission_id: UUID
    draft: MissionDraft


class ReviseMissionPayload(StrictModel):
    """A revision is material only under the D29 rule; a model may propose a
    classification, and anything the rule cannot decide is treated as material."""

    mission_id: UUID
    base_revision: int = Field(ge=1)
    draft: MissionDraft
    proposed_classification: Literal["material", "not_material"] | None = None


class ApproveMissionPayload(StrictModel):
    """Approval binds a revision to its basis: an answered decision record or
    the project's standing mandate id (OMP-418). Recorded, not verified."""

    mission_id: UUID
    revision: int = Field(ge=1)
    basis_kind: Literal["decision", "standing_mandate"]
    basis_id: str = Field(min_length=1)


MissionTransitionTarget = Literal[
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "abandoned",
]


class SetMissionStatusPayload(StrictModel):
    """A status transition records its cause: a principal, a policy rule id, or
    a decision id — the id belonging to its kind, and no other."""

    mission_id: UUID
    target_status: MissionTransitionTarget
    cause_kind: Literal["principal", "policy_rule", "decision"]
    policy_rule_id: str | None = Field(default=None, min_length=1)
    decision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_cause(self) -> SetMissionStatusPayload:
        if self.cause_kind == "principal":
            if self.policy_rule_id is not None or self.decision_id is not None:
                raise ValueError("a principal cause carries no policy rule or decision id")
            return self
        if self.cause_kind == "policy_rule":
            if self.policy_rule_id is None or self.decision_id is not None:
                raise ValueError("a policy_rule cause carries only its policy_rule_id")
            return self
        if self.decision_id is None or self.policy_rule_id is not None:
            raise ValueError("a decision cause carries only its decision_id")
        return self


class LinkMissionWorkPayload(StrictModel):
    mission_id: UUID
    work_id: UUID


class SubmitMissionCommand(StrictModel):
    type: Literal["submit_mission"]
    payload: SubmitMissionPayload


class ReviseMissionCommand(StrictModel):
    type: Literal["revise_mission"]
    payload: ReviseMissionPayload


class ApproveMissionCommand(StrictModel):
    type: Literal["approve_mission"]
    payload: ApproveMissionPayload


class SetMissionStatusCommand(StrictModel):
    type: Literal["set_mission_status"]
    payload: SetMissionStatusPayload


class LinkMissionWorkCommand(StrictModel):
    type: Literal["link_mission_work"]
    payload: LinkMissionWorkPayload


# D16/D35 (ADR 0005): a decision carries a non-null action_class exactly for
# tier-3 high-risk authorization, whose answer needs the owner's signature.
# The ten class ids are exactly ``action_tiers.TIER3``. D40: an action no tier
# lists is tier 3, so "unlisted" is a tier-3 class too, and a contract-version
# change is tier 3 by D30 ("contract_hash").
DecisionActionClass = Literal[
    "merge_protected_branch",
    "production_deploy",
    "destructive_infra",
    "credential_change",
    "delete_persistent_data",
    "billing_change",
    "publish_as_owner",
    "broaden_scope",
    "outside_secrets",
    "disable_safeguards",
    "contract_hash",
    "unlisted",
]


class CreateDecisionPayload(StrictModel):
    """One owner-facing decision record: the question, why it blocks progress,
    the bounded option set, and — for tier-3 actions — the action class."""

    decision_id: UUID
    project_id: UUID
    mission_id: str | None = Field(default=None, min_length=1)
    question: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    risk_of_delay: str = Field(min_length=1)
    options: tuple[str, ...] = Field(min_length=2, max_length=10)
    evidence_refs: tuple[str, ...] = ()
    default_if_any: str | None = None
    risk_of_each_choice: dict[str, str]
    action_class: DecisionActionClass | None = None
    target_sha256: hex64 | None = None
    resume_state: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_decision(self) -> CreateDecisionPayload:
        if not self.question.strip() or not self.why_it_matters.strip():
            raise ValueError("question and why_it_matters must carry non-blank text")
        if not self.risk_of_delay.strip():
            raise ValueError("risk_of_delay must carry non-blank text")
        if len(set(self.options)) != len(self.options):
            raise ValueError("options must be unique")
        if self.default_if_any is not None and self.default_if_any not in self.options:
            raise ValueError("default_if_any must name one of the options")
        if set(self.risk_of_each_choice) != set(self.options):
            raise ValueError("risk_of_each_choice keys must match options exactly")
        if any(not risk.strip() for risk in self.risk_of_each_choice.values()):
            raise ValueError("every option needs a non-blank risk")
        return self


class AnswerDecisionPayload(StrictModel):
    decision_id: UUID
    answer: str = Field(min_length=1)
    owner_signature: str | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = None


class CreateDecisionCommand(StrictModel):
    type: Literal["create_decision"]
    payload: CreateDecisionPayload


class AnswerDecisionCommand(StrictModel):
    type: Literal["answer_decision"]
    payload: AnswerDecisionPayload


class InstructionProvenance(StrictModel):
    """Where an owner instruction was received."""

    channel: str = Field(min_length=1, max_length=100)
    message_ref: str = Field(min_length=1, max_length=500)
    received_at: datetime


class OwnerInstruction(StrictModel):
    """The owner's words and the message they arrived on."""

    text: str = Field(min_length=1, max_length=8000)
    provenance: InstructionProvenance


class MissionIntakeScope(StrictModel):
    """MissionDraft without objective, acceptance_criteria, or constraints.

    Those three are what the owner confirms. Sending any of them on a scope
    is rejected.
    """

    project_id: UUID
    context_refs: tuple[str, ...] = ()
    artifact_expectations: tuple[str, ...] = ()
    requested_capabilities: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    approval_classes: tuple[str, ...] = ()
    risk_policy: str = Field(min_length=1, max_length=200)
    approval_policy: str = Field(min_length=1, max_length=200)
    effort_policy: str = Field(min_length=1, max_length=200)
    budget_policy: ItemBudget | None = None
    priority: int = Field(default=2, ge=0, le=3)
    continuation_of: UUID | None = None
    parent_mission: UUID | None = None
    kind: MissionKind = "engineering.execute"


class MissionDraftOptionAnswer(StrictModel):
    """``confirm`` accepts the draft. ``reject`` abandons the mission."""

    kind: Literal["option"]
    option: Literal["confirm", "reject"]


class MissionDraftEditedAnswer(StrictModel):
    """The owner replaced the draft with a full MissionDraft."""

    kind: Literal["edited_draft"]
    draft: MissionDraft


class MissionDraftNoteAnswer(StrictModel):
    """A note is recorded and never applies a draft."""

    kind: Literal["note"]
    text: str = Field(min_length=1, max_length=8000)


MissionDraftAnswer = Annotated[
    MissionDraftOptionAnswer | MissionDraftEditedAnswer | MissionDraftNoteAnswer,
    Field(discriminator="kind"),
]


class DraftMissionIntakePayload(StrictModel):
    """A bounded intake plus the mission envelope, minus the scope the owner confirms."""

    mission_id: UUID
    base_revision: int | None = Field(default=None, ge=1)
    intake: BoundedIntakeDraft
    scope: MissionIntakeScope
    instruction: OwnerInstruction | None = None


class AnswerMissionDraftPayload(StrictModel):
    """The owner's answer. ``instruction`` is required."""

    decision_id: UUID
    mission_id: UUID
    revision: int = Field(ge=1)
    answer: MissionDraftAnswer
    instruction: OwnerInstruction


class DraftMissionIntakeCommand(StrictModel):
    type: Literal["draft_mission_intake"]
    payload: DraftMissionIntakePayload


class AnswerMissionDraftCommand(StrictModel):
    type: Literal["answer_mission_draft"]
    payload: AnswerMissionDraftPayload


# The eight mission-event types. ops.alarm and ops.digest are subscription
# streams, not members of this tuple.
MissionEventType = Literal[
    "mission.started",
    "mission.blocked",
    "mission.completed",
    "mission.failed",
    "mission.progressed",
    "decision.required",
    "budget.threshold_reached",
    "important_finding",
]

MISSION_EVENT_TYPES: tuple[MissionEventType, ...] = (
    "mission.started",
    "mission.blocked",
    "mission.completed",
    "mission.failed",
    "mission.progressed",
    "decision.required",
    "budget.threshold_reached",
    "important_finding",
)

FindingSeverity = Literal["low", "medium", "high", "critical"]

_OPS_SUBSCRIPTION_STREAMS = frozenset({("ops.alarm",), ("ops.digest",)})


def validate_subscription_event_types(types: object) -> tuple[str, ...]:
    """Unique non-empty subset of the eight mission event types, or exactly
    one ops stream: ``["ops.alarm"]`` or ``["ops.digest"]``."""
    if isinstance(types, (str, bytes)) or not isinstance(types, (list, tuple)):
        raise ValueError("event_types must be a sequence of event types")
    values = tuple(types)
    if not values:
        raise ValueError("event_types must not be empty")
    if any(not isinstance(item, str) for item in values):
        raise ValueError("event_types must be strings")
    if len(set(values)) != len(values):
        raise ValueError("event_types must be unique")
    if values in _OPS_SUBSCRIPTION_STREAMS:
        return values
    allowed = frozenset(MISSION_EVENT_TYPES)
    if not set(values) <= allowed:
        raise ValueError(
            "event_types must be mission event types or exactly one ops stream"
        )
    return values


class RecordFinding(StrictModel):
    """One finding on a mission. evidence_refs are opaque strings."""

    finding_id: UUID
    mission_id: UUID
    severity: FindingSeverity
    title: str = Field(min_length=1, max_length=200)
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = (
        Field(min_length=1, max_length=20)
    )


class PutEventSubscription(StrictModel):
    subscription_id: UUID
    client_id: UUID | None = None
    push_url: str | None = None
    event_types: tuple[str, ...]

    @field_validator("event_types")
    @classmethod
    def check_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_subscription_event_types(value)


class DeleteEventSubscription(StrictModel):
    subscription_id: UUID


class AdvanceEventCursor(StrictModel):
    subscription_id: UUID
    after_sequence: int = Field(ge=0)


class RecordFindingCommand(StrictModel):
    type: Literal["record_finding"]
    payload: RecordFinding


class PutEventSubscriptionCommand(StrictModel):
    type: Literal["put_event_subscription"]
    payload: PutEventSubscription


class DeleteEventSubscriptionCommand(StrictModel):
    type: Literal["delete_event_subscription"]
    payload: DeleteEventSubscription


class AdvanceEventCursorCommand(StrictModel):
    type: Literal["advance_event_cursor"]
    payload: AdvanceEventCursor


RelayIntent = Literal[
    "pause",
    "resume",
    "request_cancellation",
    "change_priority",
    "confirm_scope",
    "edit_scope",
    "answer_decision",
]


class RelayedInstruction(StrictModel):
    text: str = Field(min_length=1, max_length=8000)
    source_message_ref: str = Field(min_length=1, max_length=500)
    owner_authored: Literal[True]
    received_at: AwareDatetime


class RelayOwnerIntentPayload(StrictModel):
    intent: RelayIntent
    instruction: RelayedInstruction
    owner_signature: str | None = Field(default=None, min_length=1)
    mission_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=0, le=3)
    decision_id: UUID | None = None
    draft: MissionDraft | None = None
    answer: str | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_relay_fields(self) -> RelayOwnerIntentPayload:
        if self.intent in ("pause", "resume", "request_cancellation"):
            if self.mission_id is None:
                raise ValueError(f"mission_id is required for {self.intent}")
            if any(
                v is not None
                for v in (
                    self.revision,
                    self.priority,
                    self.decision_id,
                    self.draft,
                    self.answer,
                    self.expires_at,
                )
            ):
                raise ValueError(f"unused field for {self.intent}")
        elif self.intent == "change_priority":
            if self.mission_id is None or self.revision is None or self.priority is None:
                raise ValueError(
                    "mission_id, revision, and priority are required for change_priority"
                )
            if any(
                v is not None
                for v in (
                    self.decision_id,
                    self.draft,
                    self.answer,
                    self.expires_at,
                )
            ):
                raise ValueError("unused field for change_priority")
        elif self.intent == "confirm_scope":
            if self.mission_id is None or self.decision_id is None or self.revision is None:
                raise ValueError(
                    "mission_id, decision_id, and revision are required for confirm_scope"
                )
            if any(
                v is not None
                for v in (
                    self.priority,
                    self.draft,
                    self.answer,
                    self.expires_at,
                )
            ):
                raise ValueError("unused field for confirm_scope")
        elif self.intent == "edit_scope":
            if (
                self.mission_id is None
                or self.decision_id is None
                or self.revision is None
                or self.draft is None
            ):
                raise ValueError(
                    "mission_id, decision_id, revision, and draft are required for edit_scope"
                )
            if any(
                v is not None
                for v in (
                    self.priority,
                    self.answer,
                    self.expires_at,
                )
            ):
                raise ValueError("unused field for edit_scope")
        elif self.intent == "answer_decision":
            if self.decision_id is None or self.answer is None:
                raise ValueError(
                    "decision_id and answer are required for answer_decision"
                )
            if any(
                v is not None
                for v in (
                    self.mission_id,
                    self.revision,
                    self.priority,
                    self.draft,
                )
            ):
                raise ValueError("unused field for answer_decision")
        return self


class RelayOwnerIntentCommand(StrictModel):
    type: Literal["relay_owner_intent"]
    payload: RelayOwnerIntentPayload


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
    | RecordExternalDeliveryCommand
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
    | CompleteExecutionItemCommand
    | SkipActiveItemCommand
    | AssessBoundedIntakeCommand
    | RecordFableAdviceCommand
    | AttestIntakeAdmissionCommand
    | PublishBoundedIntakeCommand
    | AnswerIntakeDecisionCommand
    | RegisterResearchArtifactCommand
    | CollectResearchArtifactCommand
    | RegisterResearchSourceCommand
    | RegisterResearchDatasetCommand
    | RecordResearchCacheCommand
    | ClaimResearchReplicateCommand
    | BindResearchReceiptManifestCommand
    | RegisterResearchComponentCommand
    | CreateResearchCampaignCommand
    | AdmitResearchCampaignCommand
    | CancelResearchCampaignCommand
    | ProposeResearchTrialCommand
    | RecordResearchObservationCommand
    | BindResearchDeliverableCommand
    | SetResearchCampaignStateCommand
    | ConcludeResearchCampaignCommand
    | RecordAlarmSignalCommand
    | EngageStopCommand
    | ReleaseStopCommand
    | SubmitMissionCommand
    | ReviseMissionCommand
    | ApproveMissionCommand
    | SetMissionStatusCommand
    | LinkMissionWorkCommand
    | CreateDecisionCommand
    | AnswerDecisionCommand
    | DraftMissionIntakeCommand
    | AnswerMissionDraftCommand
    | RecordFindingCommand
    | PutEventSubscriptionCommand
    | DeleteEventSubscriptionCommand
    | AdvanceEventCursorCommand
    | RelayOwnerIntentCommand,
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
    stop_client_scopes: tuple[Literal["work.stop"], ...]
    client_scopes: tuple[str, ...]
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


class ClientOperation(StrictModel):
    name: str
    method: Literal["GET", "POST"]
    path: str
    command: str | None = None
    scope: tuple[str, ...]
    request: str | None = None
    response: Literal["ClientResponse"]


class ClientContract(StrictModel):
    version: Literal["client.omp.dev/v1"]
    operations: tuple[ClientOperation, ...]


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
    client_contract: ClientContract


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
        "OMP-219",
        "OMP-222",
        "OMP-247",
        "OMP-266",
        "OMP-279",
        "OMP-283",
        "OMP-295",
        "OMP-322",
        "OMP-323",
        "OMP-404",
        "OMP-407",
        "OMP-406",
        "OMP-405",
        "OMP-413",
        "OMP-414",
        "OMP-403",
        "OMP-426",
        "OMP-403",
        "OMP-415",
        "OMP-416",
    ]
    attestation: hex64 | None = None


class IntakeSourceSpan(StrictModel):
    id: str
    start: int = Field(ge=0)
    end: int
    exact_text_sha256: hex64

    @model_validator(mode="after")
    def validate_span(self) -> IntakeSourceSpan:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class IntakeSource(StrictModel):
    text: str
    sha256: hex64
    spans: tuple[IntakeSourceSpan, ...] = ()

    @model_validator(mode="after")
    def validate_source(self) -> IntakeSource:
        from .canonical import text_sha256

        if text_sha256(self.text) != self.sha256:
            raise ValueError("text sha256 mismatch")

        seen_span_ids: set[str] = set()
        raw_bytes = self.text.encode("utf-8")
        for span in self.spans:
            if span.id in seen_span_ids:
                raise ValueError(f"duplicate span id: {span.id}")
            seen_span_ids.add(span.id)

            if span.start < 0 or span.end > len(raw_bytes):
                raise ValueError(f"span {span.id} out of bounds")

            span_bytes = raw_bytes[span.start : span.end]
            try:
                decoded_slice = span_bytes.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ValueError(
                    f"span {span.id} byte slice ({span.start}:{span.end}) does not decode as valid UTF-8"
                ) from err

            if text_sha256(decoded_slice) != span.exact_text_sha256:
                raise ValueError(
                    f"span {span.id} exact_text_sha256 mismatch: expected {text_sha256(decoded_slice)}, got {span.exact_text_sha256}"
                )

        return self


class KnownIntakeValue(StrictModel):
    kind: Literal["known"] = "known"
    value: str | int | bool


class UnknownIntakeValue(StrictModel):
    kind: Literal["unknown"] = "unknown"


IntakeValue = KnownIntakeValue | UnknownIntakeValue


class IntakeClaim(StrictModel):
    id: str
    statement: str
    source_span_ids: tuple[str, ...] = ()


class IntakeGoal(IntakeClaim):
    pass


class IntakeConstraint(IntakeClaim):
    key: str
    value: IntakeValue
    polarity: Literal["positive", "negative"]


class IntakeUnknown(IntakeClaim):
    kind: Literal["authority_or_dependency", "routine_choice"]
    material: bool


class IntakeAcceptanceCriterion(IntakeClaim):
    observable_outcome: str
    oracle: (
        Literal[
            "automated_test",
            "static_check",
            "manual_inspection",
            "external_receipt",
        ]
        | None
    ) = None


class ItemBudget(StrictModel):
    usd: str
    tokens: int = Field(gt=0)
    wall_clock_seconds: int = Field(gt=0)
    max_subagents: int = Field(ge=0)

    @field_validator("usd")
    @classmethod
    def validate_usd(cls, v: str) -> str:
        if isinstance(v, bool) or not isinstance(v, str):
            raise ValueError("usd must be a decimal string")
        try:
            val = Decimal(v)
        except (InvalidOperation, TypeError):
            raise ValueError("usd must be a valid decimal string")
        if not val.is_finite() or val <= 0:
            raise ValueError("usd must be > 0")
        return v

    @field_validator("tokens", "wall_clock_seconds", "max_subagents", mode="before")
    @classmethod
    def validate_int_type(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer, not a boolean or string")
        return v


class BoundedIntakeDraft(StrictModel):
    archetype: Literal["small_code_change"] = "small_code_change"
    source: IntakeSource
    goal: IntakeGoal
    constraints: tuple[IntakeConstraint, ...] = ()
    unknowns: tuple[IntakeUnknown, ...] = ()
    acceptance_criteria: tuple[IntakeAcceptanceCriterion, ...] = ()
    budget: ItemBudget | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> BoundedIntakeDraft:
        all_claims = (
            self.goal,
            *self.constraints,
            *self.unknowns,
            *self.acceptance_criteria,
        )
        seen_claim_ids: set[str] = set()
        for claim in all_claims:
            if claim.id in seen_claim_ids:
                raise ValueError(f"duplicate claim id: {claim.id}")
            seen_claim_ids.add(claim.id)

        source_span_ids = {span.id for span in self.source.spans}
        for claim in all_claims:
            for span_ref in claim.source_span_ids:
                if span_ref not in source_span_ids:
                    raise ValueError(f"unknown span ref: {span_ref}")

        return self


class FableAdvicePayload(StrictModel):
    advisor_model_family: Literal["fable"] = "fable"
    advice_sha256: hex64
    disposition: Literal["considered"] = "considered"
    intake_semantic_sha256: hex64
    rule_bundle_sha256: hex64


class RecordFableAdvicePayload(StrictModel):
    work_id: UUID
    revision_id: UUID
    advice_sha256: hex64
    disposition: Literal["considered"]
    intake_semantic_sha256: hex64
    rule_bundle_sha256: hex64


class AttestIntakeAdmissionPayload(StrictModel):
    work_id: UUID
    revision_id: UUID
    plan_receipt_id: UUID
    fable_advice_receipt_id: UUID
    native_acceptance_receipt_id: UUID


class IntakeAdmissionReceiptPayload(AttestIntakeAdmissionPayload):
    qualified: Literal[True]
    natively_accepted: Literal[True]
    deterministic_floor_passed: Literal[True]
    rule_bundle_sha256: hex64
    operator_actor_id: UUID


class IntakeBlockingQuestion(StrictModel):
    rule_class: Literal[
        "contradictory_constraints",
        "missing_verification_oracle",
        "missing_consequential_authority_or_dependency",
    ]
    deduplication_key: str
    statement: str
    priority: int = Field(ge=0, le=2)
    claim_ids: tuple[str, ...] = ()


OWNER_APPROVAL_COMMAND_TYPES = frozenset(
    {"attest_intake_admission", "publish_bounded_intake"}
)
OWNER_APPROVAL_REFUSED_EVENT = "owner_approval_refused"

