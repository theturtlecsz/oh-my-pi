from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
            raise ValueError("imported aliases use HOME keys and local aliases use OMP keys")
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
    commit_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
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
    candidate_commit: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    verdict: Literal["PASS", "NEEDS_FIX", "BLOCKED"] | None = None
    independent: bool = False
    remote_ref: str | None = None
    remote_commit: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def validate_payload_size(self) -> EvidenceReceipt:
        from .canonical import canonical_json

        if len(canonical_json(self.payload).encode()) > 1048576:
            raise ValueError("evidence payload exceeds 1 MiB")
        return self


class CompletionInput(StrictModel):
    work_id: UUID
    current_revision_id: UUID
    candidate: Candidate
    receipts: tuple[EvidenceReceipt, ...]
    closeout_requested: bool


class CompletionBlocker(StrictModel):
    code: Literal["plan_missing", "verification_missing", "audit_missing", "push_unverified", "stale_evidence", "closeout_missing", "candidate_not_final"]
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
    code: Literal["pagination_count_hash_gap", "duplicate_uuid_key_mapping", "missing_relation_endpoint", "relation_cycle", "multiple_focus_slots", "source_local_conflict", "legacy_authority_claim", "attachment_content_unavailable", "unsupported_non_workflow_object"]
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
                raise ValueError("batch relations must reference items in the same request")
            if relation.source_ref == relation.target_ref:
                raise ValueError("batch relations must not be self edges")
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


class RequestCloseoutPayload(StrictModel):
    work_id: UUID


class CompleteWorkPayload(StrictModel):
    input: CompletionInput


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


class RequestCloseoutCommand(StrictModel):
    type: Literal["request_closeout"]
    payload: RequestCloseoutPayload


class CompleteWorkCommand(StrictModel):
    type: Literal["complete_work"]
    payload: CompleteWorkPayload


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


Command = Annotated[
    CreateWorkBatchCommand | ReviseWorkCommand | SetWorkStateCommand | PutRelationCommand | RemoveRelationCommand | SetFocusCommand | ClearFocusCommand | AppendEvidenceCommand | FinalizeCandidateCommand | RequestCloseoutCommand | CompleteWorkCommand | RecordProjectHealthCommand | StageImportBatchCommand | PromoteImportBatchCommand | ActivateCutoverCommand,
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
    summary: Literal["candidate finalization plus verification/audit/closeout evidence"] = Field(alias="/summary")
    done: Literal["complete_work"] = Field(alias="/done")


class SourceScope(StrictModel):
    include: tuple[str, ...]
    exclude: tuple[str, ...]


class SecurityPolicy(StrictModel):
    database_roles: tuple[Literal["omp_work_owner", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup"], ...]
    owner_host_scopes: tuple[str, ...]
    task_agent_scopes: tuple[Literal["work.candidate.read"], ...]
    auditor_scopes: tuple[Literal["work.candidate.read"], ...]
    importer_scopes: tuple[Literal["work.import"], ...]
    operator_scopes: tuple[Literal["work.operate"], ...]
    rls: Literal["force_workspace_actor_claims_no_public_no_bypassrls"]
    credentials: Literal["operator_managed_mode_0600_host_only_no_agent_or_postgres_dsn"]


class DependencyGraph(StrictModel):
    home_142: tuple[Literal["HOME-143", "HOME-144", "HOME-145"], ...] = Field(alias="HOME-142")
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
    closeout_intent: Literal["recoverable"]
    work_state: Literal["not_DONE"]


class CutoverExample(StrictModel):
    anomalies: tuple[Anomaly, ...]
    parity_differences: tuple[str, ...]


class ContractExamples(StrictModel):
    immutable_revision: ImmutableRevisionExample
    relation_cycle: RelationCycleExample
    idempotency: IdempotencyExample
    stale_evidence: StaleEvidenceExample
    pushed_branch: PushedBranchExample
    cutover: CutoverExample


class BindingManifest(StrictModel):
    paths: tuple[str, ...]


class Approval(StrictModel):
    contract_version: Literal["work.omp.dev/v1"]
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: Literal["owner"]
    approved_at: datetime
    issue: Literal["HOME-142", "HOME-147", "HOME-148"]
