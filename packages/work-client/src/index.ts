/**
 * @oh-my-pi/pi-work-client — typed loopback client for the Work Ledger v1
 * service (work.omp.dev/v1). Types mirror python/omp-work/src/omp_work/v1/
 * models.py + api_models.py one-for-one; the service is the authority.
 */
import { WORK_CONTRACT_SHA256 } from "./contract";

export { WORK_CONTRACT_SHA256 } from "./contract";

export type UUID = string;

// ---- canonical encoding (mirrors v1/canonical.py byte-for-byte) ----

/** json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
 *  Undefined fields are dropped (Python models never emit them either). */
export function canonicalJson(value: unknown): string {
	if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
	if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
	const entries = Object.entries(value as Record<string, unknown>)
		.filter(([, v]) => v !== undefined)
		.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
		.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`);
	return `{${entries.join(",")}}`;
}

export function sha256Hex(text: string): string {
	return new Bun.CryptoHasher("sha256").update(text, "utf8").digest("hex");
}

/** sha256(canonical_json(value)) — the receipt payload hash the store verifies. */
export function payloadHash(value: unknown): string {
	return sha256Hex(canonicalJson(value));
}

/** Canonical candidate hash, pinned by decision 0004 + contracts/v1/candidate-hash.json.
 *  Mirrors canonical.py candidate_sha256 exactly: byte-order path sort, and refusal of
 *  empty sets, duplicates, `./`, trailing slash, backslash, `//`, and control chars.
 *  Non-UTF-8 path names must be refused BEFORE this runs (git.ts committedPaths). */
export function candidateSha256(commitSha: string, paths: string[]): string {
	if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commitSha))
		throw new Error("commit_sha must be a full lowercase hex object id (40 or 64 chars)");
	const utf8 = new TextEncoder();
	const ordered = [...paths].sort((a, b) => {
		const x = utf8.encode(a);
		const y = utf8.encode(b);
		const n = Math.min(x.length, y.length);
		for (let i = 0; i < n; i++) if (x[i] !== y[i]) return x[i] - y[i];
		return x.length - y.length;
	});
	if (!ordered.length) throw new Error("candidate path set must not be empty");
	let previous: string | null = null;
	for (const path of ordered) {
		if (!path || path.startsWith("./") || path.endsWith("/") || path.includes("\\") || path.includes("//")) {
			throw new Error(`candidate path is not a canonical repo-relative file path: ${path}`);
		}
		// eslint-disable-next-line no-control-regex
		if (/[\x00-\x1f\x7f]/.test(path)) throw new Error(`candidate path contains control characters: ${path}`);
		if (path === previous) throw new Error(`duplicate candidate path: ${path}`);
		previous = path;
	}
	return payloadHash({ algorithm: "work.omp.dev/v1/candidate-sha256", commit_sha: commitSha, paths: ordered });
}

// ---- receipts + shared entities ----

export type OperationState = "applied" | "replayed" | "rejected" | "pending_approval";
export type OperationReceipt = {
	operation_id: UUID;
	request_id: UUID;
	state: OperationState;
	request_sha256: string;
	result_sha256: string;
	diagnostics: string[];
};

export type RelationKind = "parent" | "blocks" | "duplicate_of" | "related";
export type EvidenceKind =
	| "plan"
	| "verification"
	| "audit"
	| "push"
	| "closeout"
	| "handoff"
	| "same_session_found_fixed";
export type ProjectHealth = "onTrack" | "atRisk" | "offTrack";
export type Verdict = "PASS" | "NEEDS_FIX" | "BLOCKED";

export type WorkAlias = { work_id: UUID; key: string; primary: true; origin: "imported" | "local" };
export type WorkRevision = {
	revision_id: UUID;
	work_id: UUID;
	revision_number: number;
	title: string;
	description: string;
	scope: string;
	acceptance_criteria: string[];
	content_sha256: string;
	created_by: string;
	created_at: string;
};
export type RelationEdge = {
	workspace_id: UUID;
	source_work_id: UUID;
	target_work_id: UUID;
	kind: RelationKind;
	active: boolean;
};
export type FocusSlot = { workspace_id: UUID; owner_id: UUID; work_id: UUID | null; version: number };
export type Candidate = {
	candidate_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_sha256: string;
	commit_sha: string | null;
	kind: "planned" | "final";
	allocated_at: string;
};
export type EvidenceReceipt = {
	receipt_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	kind: EvidenceKind;
	payload: Record<string, unknown>;
	payload_sha256: string;
	artifact_sha256?: string | null;
	issuer: string;
	issued_at: string;
	candidate_sha256?: string | null;
	candidate_commit?: string | null;
	verdict?: Verdict | null;
	independent?: boolean;
	remote_ref?: string | null;
	remote_commit?: string | null;
};

// ---- close attempts (OMP-47) ----

export type CloseAttemptState =
	| "active"
	| "audit_ready"
	| "auditor_in_flight"
	| "audited"
	| "closeout_requested"
	| "remediation_required"
	| "blocked"
	| "budget_exhausted"
	| "superseded"
	| "completed";
/** OMP-93: one historical work item riding a close attempt (owner ruling 2026-08-22). */
export type RiderProof = {
	work_id: UUID;
	revision_id: UUID;
	evidence: string;
};
/** OMP-111: one historical work item canceled atomically with /done (owner ruling 2026-08-23). */
export type CancellationProof = {
	work_id: UUID;
	revision_id: UUID;
	reason: string;
};
/** Rider as sealed at begin: title/criteria snapshot + service-computed evidence digest. */
export type SealedRider = RiderProof & {
	title: string;
	criteria?: string[];
	evidence_sha256: string;
};
export type CloseAttempt = {
	attempt_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	plan_receipt_id: UUID | null;
	candidate_sha256: string | null;
	candidate_commit: string | null;
	owner_session_id: string | null;
	owner_session_started_at: string | null;
	owner_session_start_commit: string | null;
	repository: string | null;
	diff_sha256: string | null;
	starting_dirty_paths: string[] | null;
	authorization_kind: "summary" | "legacy" | "execution";
	execution_grant_id?: UUID | null;
	candidate_tree_sha?: string | null;
	original_request_sha256?: string | null;
	criteria_sha256?: string | null;
	plan_stamp_sha256?: string | null;
	judge_sha256?: string | null;
	authorization_ref: string;
	launch_count: number;
	cancelled_launch_count: number;
	accepted_report_count: number;
	in_flight_launch_id: UUID | null;
	state: CloseAttemptState;
	terminal_reason: string | null;
	requested_at: string;
	closeout_requested_at: string | null;
	completed_at: string | null;
	completion_authorization_ref: string | null;
	riders?: SealedRider[];
};
export type AuditManifest = {
	manifest_id: UUID;
	work_id: UUID;
	attempt_id: UUID;
	/** v2 = v1 + Riders (OMP-93); v3 = v2 + Original request (OMP-180). */
	manifest_version: 1 | 2 | 3;
	plan_receipt_id: UUID;
	verification_receipt_id: UUID;
	candidate_id: UUID;
	candidate_sha256: string;
	candidate_commit: string;
	task_body: string;
	task_sha256: string;
	section_hashes: Record<string, string>;
	created_at: string;
};
export type AuditorLaunch = {
	launch_id: UUID;
	attempt_id: UUID;
	manifest_id: UUID;
	launch_number: number;
	task_sha256: string;
	tool_call_id: string;
	reserved_at: string;
};
export type CloseAttemptEvent = {
	event_id: UUID;
	sequence: number | null;
	work_id: UUID;
	attempt_id: UUID | null;
	launch_id: UUID | null;
	event_type: string;
	reason_code: string;
	reason: string;
	legal_next_actions: string[];
	remaining_launches: number;
	remaining_reports: number;
	requires_fresh_authorization: boolean;
	rendered_text: string;
	rendered_sha256: string;
	requires_delivery: boolean;
	created_at: string;
};
export type CheckpointDelivery = {
	delivery_id: UUID;
	event_id: UUID;
	delivery_sequence: number;
	owner_session_id: string;
	rendered_sha256: string;
	status: "delivered" | "failed" | "waived";
	authorization_ref: string | null;
	created_at: string;
};

// ---- command payloads ----

export type CreateWorkInput = {
	client_ref: string; // ^[A-Za-z0-9._-]+$, ≤64
	title: string;
	description?: string;
	scope?: string;
	acceptance_criteria?: string[];
	state?: string; // default BACKLOG; never DONE
	project_id?: UUID;
};
export type CreateBatchRelation = { source_ref: string; target_ref: string; kind: RelationKind };
export type CreateWorkBatchPayload = { items: CreateWorkInput[]; relations?: CreateBatchRelation[] };
/** OMP-139: one atomic same-session found-and-fixed filing — child, parent
 *  edge, and typed receipt land in one serializable service transaction. */
export type CreateSameSessionChildPayload = {
	parent_work_id: UUID;
	attempt_id: UUID;
	owner_session_id: string;
	item: CreateWorkInput;
	finding: string;
	verification: string;
};
export type ReviseWorkPayload = { work_id: UUID; expected_revision_id: UUID; revision: WorkRevision };
export type SetWorkStatePayload = { work_id: UUID; state: string };
export type PutRelationPayload = { relation: RelationEdge };
export type RemoveRelationPayload = { relation: RelationEdge };
export type SetFocusPayload = { slot: FocusSlot; expected_version: number };
export type ClearFocusPayload = { workspace_id: UUID; owner_id: UUID; expected_version: number };
export type AppendEvidencePayload = { receipt: EvidenceReceipt };
export type FinalizeCandidatePayload = {
	work_id: UUID;
	revision_id: UUID;
	planned_candidate_id: UUID;
	candidate_id: UUID;
	candidate_sha256: string;
	commit_sha: string;
};
export type RecordCloseoutReviewPayload = { receipt: EvidenceReceipt; attempt_id: UUID; authorization_ref: string };
export type CompletionInput = {
	work_id: UUID;
	current_revision_id: UUID;
	candidate: Candidate;
	receipts: EvidenceReceipt[];
	closeout_requested: boolean;
};
export type CompleteWorkPayload = {
	input: CompletionInput;
	attempt_id: UUID;
	done_authorization_ref: string;
	satisfied_work_ids?: UUID[];
	cancellations?: CancellationProof[];
};
export type BeginCloseAttemptPayload = {
	work_id: UUID;
	attempt_id: UUID;
	authorization_ref: string;
	owner_session_id: string;
	owner_session_started_at: string;
	owner_session_start_commit: string;
	repository: string;
	diff_sha256: string;
	riders?: RiderProof[];
	starting_dirty_paths?: string[];
	authorization_kind?: "summary" | "legacy" | "execution";
	execution_grant_id?: UUID | null;
	candidate_tree_sha?: string | null;
	original_request_sha256?: string | null;
	criteria_sha256?: string | null;
	plan_stamp_sha256?: string | null;
	judge_sha256?: string | null;
};
export type SealAuditManifestPayload = { attempt_id: UUID; verification_receipt_id: UUID };
export type ReserveAuditorLaunchPayload = { attempt_id: UUID; task_sha256: string; tool_call_id: string };
export type CancelAuditorLaunchPayload = { attempt_id: UUID; launch_id: UUID };
export type SettleAuditorLaunchPayload = {
	attempt_id: UUID;
	launch_id: UUID;
	/** UNTOUCHED transport payload — any JSON shape; WorkService owns normalization. */
	transport_payload?: unknown;
	transport_failed?: boolean;
};
export type AttestCheckpointDeliveryPayload = {
	event_id: UUID;
	owner_session_id: string;
	rendered_sha256: string;
	status: "delivered" | "failed" | "waived";
	authorization_ref?: string | null;
};
export type RecordProjectHealthPayload = { project_id: UUID; health: ProjectHealth };

export type CommandSmokeResult = { command_type: string; passed: boolean };
export type ReconciliationCounts = {
	worlds: number;
	surfaces: number;
	promises: number;
	work_items: number;
	states: number;
	labels: number;
	relations: number;
	comments: number;
	attachments: number;
	users: number;
};
export type ReconciliationHashes = Record<keyof ReconciliationCounts, string>;
export type CutoverManifest = {
	epoch_id: UUID;
	contract_version: string;
	contract_sha256: string;
	schema_sha256: string;
	transform_version: string;
	transform_sha256: string;
	source_boundary: string;
	source_watermark: string;
	raw_export_sha256: string;
	import_batch_id: UUID;
	dimension_counts: ReconciliationCounts;
	dimension_hashes: ReconciliationHashes;
	parity_groups: Record<string, string>;
	anomalies: { code: string; disposition: string }[];
	parity_differences: string[];
	backup_receipt_sha256: string;
	restore_receipt_sha256: string;
	command_smoke_results: CommandSmokeResult[];
	code_fingerprint: string;
	config_fingerprint: string;
	freeze_at: string;
	activated_at: string | null;
	revoked_at: string | null;
	actor: string;
};
export type ActivateCutoverPayload = { manifest: CutoverManifest };

export type ExecutionState = "active" | "paused" | "stopped" | "completed" | "canceled";
export type ExecutionMode = "single" | "queue";
export type ExecutionItemPhase =
	| "pending"
	| "criteria_pending"
	| "planning"
	| "executing"
	| "reviewing"
	| "remediating"
	| "awaiting_contract_approval"
	| "completed"
	| "abandoned"
	| "skipped";

export type ExecutionProvenanceEnvelope = {
	owner_input_id: string;
	owner_session_id: string;
	normalized_command: string;
	workspace_id: UUID;
	repository: string;
	nonce: string;
	issued_at: string;
};

export type ExecutionGrantItemClaim = {
	work_id: UUID;
	revision_id: UUID;
	position: number;
	original_request: string;
	original_request_sha256: string;
	initial_git_baseline: string;
	project_id?: UUID | null;
	active_blocker_ids?: UUID[];
};

export type ExecutionJudgeManifest = {
	auditor_agent_sha256: string;
	host_sha256: string;
	adapter_sha256: string;
	freeze_sha256: string;
	runner_sha256: string;
	executor_sha256: string;
	contract_sha256: string;
	service_fingerprint: string;
	service_code_fingerprint: string;
	service_migration_sha256: string;
};

export type BeginExecutionPayload = {
	grant_id: UUID;
	provenance: ExecutionProvenanceEnvelope;
	remote_ref: string;
	mode: ExecutionMode;
	items: ExecutionGrantItemClaim[];
	expected_focus_version: number;
	judge_sha256: string;
	judge_manifest: ExecutionJudgeManifest;
};

export type ActivateExecutionItemPayload = {
	grant_id: UUID;
	expected_grant_version: number;
	position: number;
	work_id: UUID;
	expected_revision_id: UUID;
	git_baseline: string;
	judge_sha256: string;
	expected_focus_version: number;
	expected_project_id?: UUID | null;
	expected_blocker_ids?: UUID[];
};

export type SealExecutionCriteriaPayload = {
	grant_id: UUID;
	expected_grant_version: number;
	work_id: UUID;
	expected_revision_id: UUID;
	criteria: string[];
	description_sha256: string;
	judge_sha256: string;
};

export type StampExecutionPlanPayload = {
	grant_id: UUID;
	expected_grant_version: number;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	plan_file: string;
	plan_body: string;
	plan_sha256: string;
	approach: string[];
	verification: string[];
	paths: string[];
	candidate_sha256: string;
	judge_sha256: string;
};

export type SetExecutionStatePayload = {
	grant_id: UUID;
	expected_grant_version: number;
	target_state: "active" | "paused" | "stopped" | "canceled";
	reason?: string | null;
	judge_sha256: string;
};

export type CompleteExecutionItemPayload = {
	grant_id: UUID;
	expected_grant_version: number;
	work_id: UUID;
	attempt_id: UUID;
	push_receipt_id: UUID;
	judge_sha256: string;
};

export type Command =
	| { type: "create_work_batch"; payload: CreateWorkBatchPayload }
	| { type: "create_same_session_child"; payload: CreateSameSessionChildPayload }
	| { type: "revise_work"; payload: ReviseWorkPayload }
	| { type: "set_work_state"; payload: SetWorkStatePayload }
	| { type: "put_relation"; payload: PutRelationPayload }
	| { type: "remove_relation"; payload: RemoveRelationPayload }
	| { type: "set_focus"; payload: SetFocusPayload }
	| { type: "clear_focus"; payload: ClearFocusPayload }
	| { type: "append_evidence"; payload: AppendEvidencePayload }
	| { type: "finalize_candidate"; payload: FinalizeCandidatePayload }
	| { type: "begin_close_attempt"; payload: BeginCloseAttemptPayload }
	| { type: "seal_audit_manifest"; payload: SealAuditManifestPayload }
	| { type: "reserve_auditor_launch"; payload: ReserveAuditorLaunchPayload }
	| { type: "cancel_auditor_launch"; payload: CancelAuditorLaunchPayload }
	| { type: "settle_auditor_launch"; payload: SettleAuditorLaunchPayload }
	| { type: "attest_checkpoint_delivery"; payload: AttestCheckpointDeliveryPayload }
	| { type: "record_closeout_review"; payload: RecordCloseoutReviewPayload }
	| { type: "complete_work"; payload: CompleteWorkPayload }
	| { type: "record_project_health"; payload: RecordProjectHealthPayload }
	| { type: "activate_cutover"; payload: ActivateCutoverPayload }
	| { type: "begin_execution"; payload: BeginExecutionPayload }
	| { type: "activate_execution_item"; payload: ActivateExecutionItemPayload }
	| { type: "seal_execution_criteria"; payload: SealExecutionCriteriaPayload }
	| { type: "stamp_execution_plan"; payload: StampExecutionPlanPayload }
	| { type: "set_execution_state"; payload: SetExecutionStatePayload }
	| { type: "complete_execution_item"; payload: CompleteExecutionItemPayload };

// ---- command results ----

export type CreatedWorkItem = {
	client_ref: string;
	work_id: UUID;
	revision_id: UUID;
	key: string;
	state: string;
	row_version: number;
};
export type CommandResult =
	| { type: "create_work_batch"; items: CreatedWorkItem[] }
	| { type: "create_same_session_child"; item: CreatedWorkItem; receipt: EvidenceReceipt }
	| { type: "revise_work"; revision_id: UUID; changed: boolean }
	| { type: "set_work_state"; work_id: UUID; state: string; row_version: number }
	| {
			type: "complete_work";
			status: "applied" | "refused";
			work_id: UUID;
			state?: string | null;
			row_version?: number | null;
			completed_work_ids?: UUID[];
			canceled_work_ids?: UUID[];
			event?: CloseAttemptEvent | null;
	  }
	| {
			type: "put_relation" | "remove_relation";
			source_work_id: UUID;
			target_work_id: UUID;
			kind: RelationKind;
			active: boolean;
	  }
	| { type: "set_focus" | "clear_focus"; workspace_id: UUID; owner_id: UUID; work_id: UUID | null; version: number }
	| { type: "append_evidence"; receipt: EvidenceReceipt; event?: CloseAttemptEvent | null }
	| { type: "finalize_candidate"; candidate: Candidate }
	| {
			type:
				| "begin_close_attempt"
				| "seal_audit_manifest"
				| "reserve_auditor_launch"
				| "cancel_auditor_launch"
				| "settle_auditor_launch"
				| "attest_checkpoint_delivery";
			status: "applied" | "refused";
			attempt?: CloseAttempt | null;
			manifest?: AuditManifest | null;
			launch?: AuditorLaunch | null;
			receipt?: EvidenceReceipt | null;
			delivery?: CheckpointDelivery | null;
			verdict?: Verdict | null;
			event: CloseAttemptEvent;
	  }
	| {
			type: "record_closeout_review";
			status: "applied" | "refused";
			receipt?: EvidenceReceipt | null;
			attempt?: CloseAttempt | null;
			event: CloseAttemptEvent;
	  }
	| { type: "record_project_health"; health: ProjectHealthView }
	| {
			type: "activate_cutover";
			epoch_id: UUID;
			authority: "work";
			candidate_manifest_sha256: string;
			activated_at: string;
	  }
	| { type: "begin_execution"; grant: ExecutionGrantView; items: ExecutionGrantItemView[] }
	| { type: "activate_execution_item"; grant: ExecutionGrantView; item: ExecutionGrantItemView }
	| {
			type: "seal_execution_criteria";
			grant: ExecutionGrantView;
			item: ExecutionGrantItemView;
			revision: WorkRevision;
	  }
	| {
			type: "stamp_execution_plan";
			grant: ExecutionGrantView;
			item: ExecutionGrantItemView;
			candidate: Candidate;
			receipt: EvidenceReceipt;
	  }
	| { type: "set_execution_state"; grant: ExecutionGrantView }
	| {
			type: "complete_execution_item";
			grant: ExecutionGrantView;
			item: ExecutionGrantItemView;
			work_id: UUID;
			state: string;
			closeout_receipt: EvidenceReceipt;
	  };

export type ExecutionGrantView = {
	grant_id: UUID;
	workspace_id: UUID;
	owner_id: UUID;
	repository: string;
	remote_ref: string;
	state: ExecutionState;
	mode: ExecutionMode;
	grant_version: number;
	max_continuations: number;
	max_close_attempts: number;
	max_no_progress: number;
	continuations_scheduled: number;
	terminal_reason?: string | null;
	authorization_hash: string;
	judge_sha256: string;
	created_at: string;
	expires_at: string;
	completed_at?: string | null;
	paused_at?: string | null;
	stopped_at?: string | null;
	canceled_at?: string | null;
};

export type ExecutionGrantItemView = {
	item_id: UUID;
	workspace_id: UUID;
	grant_id: UUID;
	work_id: UUID;
	position: number;
	phase: ExecutionItemPhase;
	claimed_revision_id: UUID;
	project_id?: UUID | null;
	active_blocker_ids?: UUID[];
	initial_git_baseline: string;
	current_git_baseline?: string | null;
	criteria_revision_id?: UUID | null;
	original_request: string;
	original_request_sha256: string;
	criteria_sha256?: string | null;
	plan_stamp_sha256?: string | null;
	plan_stamp?: Record<string, unknown> | null;
	close_attempts_started: number;
	consecutive_no_progress: number;
	last_reviewed_tree_sha?: string | null;
	last_findings_hash?: string | null;
	push_receipt_id?: UUID | null;
	closeout_receipt_id?: UUID | null;
	activated_at?: string | null;
	completed_at?: string | null;
	abandoned_at?: string | null;
	skipped_at?: string | null;
	terminal_reason?: string | null;
};

export type ExecutionView = {
	grant: ExecutionGrantView;
	items: ExecutionGrantItemView[];
	active_item?: ExecutionGrantItemView | null;
};

export type AuthorityView = {
	authority: "linear" | "work";
	epoch_id: UUID | null;
	epoch_state: "active" | "sealed" | "rolled_back" | null;
	activated_at: string | null;
	first_work_mutation_at: string | null;
};

// ---- read views ----

export type WorkItemView = {
	work_id: UUID;
	workspace_id: UUID;
	alias: WorkAlias;
	state: string;
	revision: WorkRevision;
	candidate: Candidate | null;
	project_id: UUID | null;
	archived: boolean;
};
export type ProjectView = {
	project_id: UUID;
	workspace_id: UUID;
	key: string | null;
	name: string;
	health: ProjectHealth | null;
	health_updated_at: string | null;
};
export type WorkflowView = {
	item: WorkItemView;
	relations: RelationEdge[];
	receipts: EvidenceReceipt[];
	close_attempts: CloseAttempt[];
	audit_manifest: AuditManifest | null;
	auditor_launches: AuditorLaunch[];
	close_attempt_events: CloseAttemptEvent[];
	checkpoint_deliveries: CheckpointDelivery[];
	project: ProjectView | null;
	execution_grant?: ExecutionGrantView | null;
	execution_grant_item?: ExecutionGrantItemView | null;
};
export type WorkspaceTree = {
	workspace_id: UUID;
	items: WorkItemView[];
	relations: RelationEdge[];
	projects: ProjectView[];
};
export type ProjectHealthView = { project_id: UUID; workspace_id: UUID; health: ProjectHealth; updated_at: string };
export type StoredOperation = {
	receipt: OperationReceipt;
	command_type: string;
	request_id: UUID;
	correlation_id: UUID;
	result: CommandResult | null;
};
export type HealthView = { live: boolean; ready: boolean; alerts: string[]; service_fingerprint?: string | null };

/** /center recent-activity projection (OMP-25) — normalized event metadata
 *  only; receipt bodies and audit payloads never cross this read. */
export type ActivityKind = EvidenceKind | "close_proposed" | "completed" | "evidence";
export type ActivityEvent = {
	kind: ActivityKind;
	work_id: UUID;
	key: string;
	title: string;
	project_id: UUID | null;
	occurred_at: string;
};
export type ActivityView = { workspace_id: UUID; total: number; events: ActivityEvent[] };

export type CommandEnvelope = {
	api_version: "work.omp.dev/v1";
	workspace_id: UUID;
	operation_id: UUID;
	request_id: UUID;
	correlation_id: UUID;
	command: Command;
};
export type CommandResponse = { receipt: OperationReceipt; result: CommandResult };

export type WorkErrorBody = {
	error: { code: string; request_id: UUID | null; correlation_id: UUID | null; diagnostics: string[] };
};

export type Fetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

/** Error text is for notices/status lines: bearer material and absolute paths
 *  are stripped before it ever reaches the TUI. */
function redact(text: string): string {
	return (
		text
			.replace(/Bearer\s+\S+/gi, "Bearer …")
			// UUIDs and bare 40/64-hex content digests (commit SHAs, contract and
			// candidate hashes) are identities, not secrets — they survive so
			// contract_mismatch diagnostics stay actionable (OMP-143).
			.replace(/[A-Za-z0-9_-]{32,}/g, m =>
				/^[0-9a-f-]{36}$/.test(m) || /^[0-9a-f]{40}$/.test(m) || /^[0-9a-f]{64}$/.test(m) ? m : "…",
			)
			.replace(/\/home\/[^\s"']+/g, "~")
	);
}

export class WorkError extends Error {
	constructor(
		readonly code: string,
		readonly status: number,
		readonly diagnostics: string[],
		readonly requestId: UUID | null = null,
	) {
		super(redact(`${code} (HTTP ${status})${diagnostics.length ? `: ${diagnostics.join("; ")}` : ""}`));
		this.name = "WorkError";
	}
}

export class WorkClient {
	constructor(
		private readonly baseUrl: string,
		private readonly workspaceId: UUID,
		private readonly token: () => string | null,
		private readonly fetchImpl: Fetch = fetch,
	) {}

	private headers(): Record<string, string> {
		const token = this.token();
		if (!token) throw new WorkError("unauthenticated", 401, ["no bearer token configured"]);
		return {
			Authorization: `Bearer ${token}`,
			"X-OMP-Workspace-ID": this.workspaceId,
			// OMP-143 fail-first handshake: the digest this binary loaded; the
			// service refuses any mismatch with typed `contract_mismatch`.
			"X-OMP-Contract-SHA256": WORK_CONTRACT_SHA256,
			"Content-Type": "application/json",
		};
	}

	private async request(method: "GET" | "POST", path: string, body?: unknown, auth = true): Promise<unknown> {
		const headers = auth ? this.headers() : {};
		let response: Response;
		try {
			response = await this.fetchImpl(`${this.baseUrl}${path}`, {
				method,
				headers,
				...(body === undefined ? {} : { body: JSON.stringify(body) }),
				signal: AbortSignal.timeout(8000),
			});
		} catch (cause) {
			throw new WorkError("unavailable", 0, [redact(String(cause))]);
		}
		const text = await response.text();
		let parsed: unknown = {};
		try {
			parsed = text ? JSON.parse(text) : {};
		} catch {
			/* a non-JSON body is a service fault — fall through to the status check */
		}
		if (!response.ok) {
			const error = (parsed as Partial<WorkErrorBody>).error;
			throw new WorkError(
				error?.code ?? "invalid_request",
				response.status,
				(error?.diagnostics ?? []).map(redact),
				error?.request_id ?? null,
			);
		}
		return parsed;
	}

	execute(envelope: CommandEnvelope): Promise<CommandResponse> {
		return this.request("POST", "/v1/commands", envelope) as Promise<CommandResponse>;
	}

	workItem(key: string): Promise<WorkItemView> {
		return this.request("GET", `/v1/work-items/${encodeURIComponent(key)}`) as Promise<WorkItemView>;
	}

	workflow(key: string): Promise<WorkflowView> {
		return this.request("GET", `/v1/work-items/${encodeURIComponent(key)}/workflow`) as Promise<WorkflowView>;
	}

	tree(): Promise<WorkspaceTree> {
		return this.request("GET", `/v1/workspaces/${this.workspaceId}/tree`) as Promise<WorkspaceTree>;
	}

	focus(ownerId: UUID): Promise<FocusSlot> {
		return this.request("GET", `/v1/workspaces/${this.workspaceId}/focus/${ownerId}`) as Promise<FocusSlot>;
	}

	operation(operationId: UUID): Promise<StoredOperation> {
		return this.request("GET", `/v1/operations/${operationId}`) as Promise<StoredOperation>;
	}

	authority(): Promise<AuthorityView> {
		return this.request("GET", `/v1/workspaces/${this.workspaceId}/authority`) as Promise<AuthorityView>;
	}

	/** Bounded recent-activity read (OMP-25 /center) — newest-first, work.read only. */
	activity(options: { projectId?: UUID; limit?: number } = {}): Promise<ActivityView> {
		const params = new URLSearchParams();
		if (options.projectId !== undefined) params.set("project_id", options.projectId);
		if (options.limit !== undefined) params.set("limit", String(options.limit));
		const query = params.size > 0 ? `?${params}` : "";
		return this.request("GET", `/v1/workspaces/${this.workspaceId}/activity${query}`) as Promise<ActivityView>;
	}

	/** Liveness/readiness probes — unauthenticated by design, so a missing or
	 *  dead bearer still yields an honest status line. */
	healthLive(): Promise<HealthView> {
		return this.request("GET", "/v1/health/live", undefined, false) as Promise<HealthView>;
	}

	healthReady(): Promise<HealthView> {
		return this.request("GET", "/v1/health/ready", undefined, false) as Promise<HealthView>;
	}
	execution(grantIdOrKey?: string): Promise<ExecutionView> {
		const suffix = grantIdOrKey ? `/${encodeURIComponent(grantIdOrKey)}` : "";
		return this.request("GET", `/v1/workspaces/${this.workspaceId}/execution${suffix}`) as Promise<ExecutionView>;
	}
}
