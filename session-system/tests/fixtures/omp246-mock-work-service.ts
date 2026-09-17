/**
 * OMP-246 fetch-level WorkService double keyed by operation_id.
 *
 * Only the operation journal under test is modelled: a POST whose
 * operation_id was already applied replays the stored result (receipt state
 * "replayed"), the same operation_id with different bytes is an
 * idempotency_conflict, and GET /v1/operations/:id serves the stored receipt
 * for pending-claim reconciliation. Command handling covers the three
 * execution mutations the lifecycle tests drive (set_execution_state,
 * complete_execution_item, activate_execution_item) with the grant-version
 * CAS the real service applies, refused with the native `revision_conflict`
 * code (store.py: "grant_version mismatch"), plus the first-turn submission
 * commands begin_execution_review issues (finalize_candidate,
 * append_evidence, begin_close_attempt) and the checkpoint attestation the
 * queued delivery settles with (attest_checkpoint_delivery). These accept
 * unconditionally: they prove controller-side admission, never native
 * finalize/attempt rules. Every other command is refused so a test can never
 * depend on behaviour this double does not implement. Read views project one
 * grant, its items, their work items, and the receipts/attempts/events the
 * submission commands recorded.
 */
import { randomUUID } from "node:crypto";
import {
	type Candidate,
	type CheckpointDelivery,
	type CloseAttempt,
	type CloseAttemptEvent,
	type Command,
	type CommandEnvelope,
	type CommandResult,
	type EvidenceReceipt,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type Fetch,
	payloadHash,
	sha256Hex,
	type StoredOperation,
	type WorkItemView,
} from "@oh-my-pi/pi-work-client";

export interface MockWorkServiceOptions {
	workspaceId: string;
	ownerId: string;
	grant: ExecutionGrantView;
	items: ExecutionGrantItemView[];
	workItems: WorkItemView[];
	/** Close attempts served on every workflow view (contract 1 binds one to the frozen candidate). */
	closeAttempts?: CloseAttempt[];
	/** Focus slot target; defaults to the position-0 item's work id. */
	focusWorkId?: string | null;
	serviceFingerprint?: string;
}

export type DropMode = "before_commit" | "after_commit";

type CommandType = Command["type"];

export interface MockWorkService {
	fetch: Fetch;
	/** Lose exactly the next POST response: before the command is recorded, or after it applied. */
	dropNextResponse(mode: DropMode): void;
	/** POST attempts (including dropped ones), optionally per command type. */
	posts(type?: CommandType): number;
	/** Commands that changed state, optionally per command type. */
	applied(type?: CommandType): number;
	replayed(): number;
	conflicts(): number;
	/** Requests that matched no modelled route — a good-path test asserts this stays empty. */
	unmatched(): string[];
	/** Operation ids read through GET /v1/operations/:id, in order — pending-claim reconciliation fetches each committed claim once. */
	operationLookups(): string[];
	/** Live references: tests read fields after mutations. */
	grant(): ExecutionGrantView;
	items(): ExecutionGrantItemView[];
	activeItem(): ExecutionGrantItemView | null;
	workItems(): WorkItemView[];
	operation(id: string): StoredOperation | undefined;
	/** The position-0 work item's candidate: finalize_candidate binds the frozen commit here. */
	candidate(): Candidate | null;
	/** Every close attempt served on the workflow view, including ones begun by begin_close_attempt. */
	closeAttempts(): CloseAttempt[];
}

const ACTIVE_PHASES = new Set<ExecutionGrantItemView["phase"]>([
	"criteria_pending",
	"planning",
	"executing",
	"reviewing",
	"remediating",
	"awaiting_contract_approval",
]);

function json(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function refusal(code: string, status: number, ...diagnostics: string[]): Response {
	return json({ error: { code, request_id: null, correlation_id: null, diagnostics } }, status);
}

function closeEvent(workId: string, attemptId: string | null, eventType: string, requiresDelivery: boolean): CloseAttemptEvent {
	const text = `${eventType} for ${workId}`;
	return {
		event_id: randomUUID(),
		sequence: null,
		work_id: workId,
		attempt_id: attemptId,
		launch_id: null,
		event_type: eventType,
		reason_code: eventType,
		reason: text,
		legal_next_actions: [],
		remaining_launches: 3,
		remaining_reports: 2,
		requires_fresh_authorization: false,
		rendered_text: text,
		rendered_sha256: sha256Hex(text),
		requires_delivery: requiresDelivery,
		created_at: new Date().toISOString(),
	};
}

export function createMockWorkService(options: MockWorkServiceOptions): MockWorkService {
	const { workspaceId, ownerId, grant, items, workItems } = options;
	const closeAttempts = options.closeAttempts ?? [];
	const receipts: EvidenceReceipt[] = [];
	const closeAttemptEvents: CloseAttemptEvent[] = [];
	const checkpointDeliveries: CheckpointDelivery[] = [];
	const serviceFingerprint = options.serviceFingerprint ?? "omp246-mock-service";
	const focusWorkId = options.focusWorkId === undefined ? (items[0]?.work_id ?? null) : options.focusWorkId;
	let activeWorkId: string | null = items.find(item => ACTIVE_PHASES.has(item.phase))?.work_id ?? null;
	const operations = new Map<string, StoredOperation>();
	const postCounts = new Map<CommandType, number>();
	const appliedCounts = new Map<CommandType, number>();
	const unmatched: string[] = [];
	const operationLookups: string[] = [];
	let replayed = 0;
	let conflicts = 0;
	let drop: DropMode | undefined;

	const bump = (map: Map<CommandType, number>, type: CommandType): void => {
		map.set(type, (map.get(type) ?? 0) + 1);
	};
	const total = (map: Map<CommandType, number>, type?: CommandType): number =>
		type === undefined ? [...map.values()].reduce((sum, count) => sum + count, 0) : (map.get(type) ?? 0);
	const active = (): ExecutionGrantItemView | null =>
		activeWorkId === null ? null : (items.find(item => item.work_id === activeWorkId) ?? null);
	const findWorkItem = (ref: string): WorkItemView | undefined =>
		workItems.find(item => item.work_id === ref || item.alias.key === ref);

	function apply(command: Command): CommandResult | Response {
		switch (command.type) {
			case "set_execution_state": {
				const payload = command.payload;
				if (payload.grant_id !== grant.grant_id) return refusal("not_found", 404, "unknown grant");
				if (payload.expected_grant_version !== grant.grant_version) {
					return refusal(
						"revision_conflict",
						409,
						"grant_version mismatch",
					);
				}
				grant.grant_version += 1;
				grant.state = payload.target_state;
				grant.terminal_reason = payload.reason ?? null;
				if (payload.target_state === "active") grant.continuations_scheduled += 1;
				return { type: "set_execution_state", grant: structuredClone(grant) };
			}
			case "complete_execution_item": {
				const payload = command.payload;
				if (payload.grant_id !== grant.grant_id) return refusal("not_found", 404, "unknown grant");
				if (payload.expected_grant_version !== grant.grant_version) {
					return refusal(
						"revision_conflict",
						409,
						"grant_version mismatch",
					);
				}
				const item = active();
				if (!item || item.work_id !== payload.work_id) {
					return refusal("not_found", 404, "no matching active execution item");
				}
				const completedAt = new Date().toISOString();
				const receipt: EvidenceReceipt = {
					receipt_id: randomUUID(),
					work_id: item.work_id,
					revision_id: item.claimed_revision_id,
					candidate_id: payload.evidence.subject.candidate_id,
					kind: "closeout",
					payload: { attempt_id: payload.attempt_id },
					payload_sha256: sha256Hex(payload.attempt_id),
					issuer: "omp246-mock-service",
					issued_at: completedAt,
				};
				item.phase = "completed";
				item.completed_at = completedAt;
				item.closeout_receipt_id = receipt.receipt_id;
				activeWorkId = null;
				grant.grant_version += 1;
				if (grant.mode === "single") {
					grant.state = "completed";
					grant.completed_at = completedAt;
				}
				const work = findWorkItem(item.work_id);
				if (work) work.state = "DONE";
				return {
					type: "complete_execution_item",
					grant: structuredClone(grant),
					item: structuredClone(item),
					work_id: item.work_id,
					state: "DONE",
					closeout_receipt: receipt,
				};
			}
			case "activate_execution_item": {
				const payload = command.payload;
				if (payload.grant_id !== grant.grant_id) return refusal("not_found", 404, "unknown grant");
				if (payload.expected_grant_version !== grant.grant_version) {
					return refusal(
						"revision_conflict",
						409,
						"grant_version mismatch",
					);
				}
				const item = items.find(row => row.position === payload.position && row.work_id === payload.work_id);
				if (!item || item.phase !== "pending" || active() !== null) {
					return refusal("invalid_request", 409, "item is not pending or another item is active");
				}
				item.phase = "criteria_pending";
				item.activated_at = new Date().toISOString();
				item.current_git_baseline = payload.git_baseline;
				activeWorkId = item.work_id;
				grant.grant_version += 1;
				return { type: "activate_execution_item", grant: structuredClone(grant), item: structuredClone(item) };
			}
			case "finalize_candidate": {
				const payload = command.payload;
				const work = workItems.find(item => item.work_id === payload.work_id);
				if (!work) return refusal("not_found", 404, "unknown work item");
				work.candidate = {
					candidate_id: payload.candidate_id,
					work_id: payload.work_id,
					revision_id: payload.revision_id,
					candidate_sha256: payload.candidate_sha256,
					commit_sha: payload.commit_sha,
					kind: "final",
					allocated_at: new Date().toISOString(),
				};
				return { type: "finalize_candidate", candidate: structuredClone(work.candidate) };
			}
			case "append_evidence": {
				receipts.push(command.payload.receipt);
				return { type: "append_evidence", receipt: command.payload.receipt };
			}
			case "begin_close_attempt": {
				const payload = command.payload;
				const work = workItems.find(item => item.work_id === payload.work_id);
				if (!work?.candidate) return refusal("invalid_request", 409, "no finalized candidate to bind");
				const attempt: CloseAttempt = {
					attempt_id: payload.attempt_id,
					work_id: work.work_id,
					revision_id: work.revision.revision_id,
					candidate_id: work.candidate.candidate_id,
					plan_receipt_id: null,
					candidate_sha256: work.candidate.candidate_sha256,
					candidate_commit: work.candidate.commit_sha,
					owner_session_id: payload.owner_session_id,
					owner_session_started_at: payload.owner_session_started_at,
					owner_session_start_commit: payload.owner_session_start_commit,
					repository: payload.repository,
					diff_sha256: payload.diff_sha256,
					starting_dirty_paths: payload.starting_dirty_paths ?? [],
					authorization_kind: payload.authorization_kind ?? "summary",
					execution_grant_id: payload.execution_grant_id ?? null,
					candidate_tree_sha: payload.candidate_tree_sha ?? null,
					original_request_sha256: payload.original_request_sha256 ?? null,
					criteria_sha256: payload.criteria_sha256 ?? null,
					plan_stamp_sha256: payload.plan_stamp_sha256 ?? null,
					judge_sha256: payload.judge_sha256 ?? null,
					authorization_ref: payload.authorization_ref,
					launch_count: 0,
					cancelled_launch_count: 0,
					accepted_report_count: 0,
					in_flight_launch_id: null,
					state: "audit_ready",
					terminal_reason: null,
					requested_at: new Date().toISOString(),
					closeout_requested_at: null,
					completed_at: null,
					completion_authorization_ref: null,
				};
				closeAttempts.push(attempt);
				// The native service gates the audit on an attested delivery of this event (OMP-97).
				const event = closeEvent(work.work_id, attempt.attempt_id, "close_attempt_begun", true);
				closeAttemptEvents.push(event);
				return { type: "begin_close_attempt", status: "applied", attempt: structuredClone(attempt), event };
			}
			case "attest_checkpoint_delivery": {
				const payload = command.payload;
				const source = closeAttemptEvents.find(event => event.event_id === payload.event_id);
				if (!source) return refusal("not_found", 404, "unknown checkpoint event");
				const delivery: CheckpointDelivery = {
					delivery_id: randomUUID(),
					event_id: payload.event_id,
					delivery_sequence: checkpointDeliveries.filter(row => row.event_id === payload.event_id).length + 1,
					owner_session_id: payload.owner_session_id,
					rendered_sha256: payload.rendered_sha256,
					status: payload.status,
					authorization_ref: payload.authorization_ref ?? null,
					created_at: new Date().toISOString(),
				};
				checkpointDeliveries.push(delivery);
				return {
					type: "attest_checkpoint_delivery",
					status: "applied",
					delivery,
					event: closeEvent(source.work_id, source.attempt_id, "checkpoint_delivery_attested", false),
				};
			}
			default:
				return refusal("invalid_request", 400, `${command.type} is not modelled by the OMP-246 mock service`);
		}
	}

	async function post(init?: RequestInit): Promise<Response> {
		const envelope = JSON.parse(String(init?.body)) as CommandEnvelope;
		bump(postCounts, envelope.command.type);
		const mode = drop;
		drop = undefined;
		if (mode === "before_commit") throw new TypeError("connection reset before the command was recorded");
		const requestSha256 = payloadHash({
			api_version: envelope.api_version,
			workspace_id: envelope.workspace_id,
			command: envelope.command,
		});
		const prior = operations.get(envelope.operation_id);
		if (prior) {
			if (prior.receipt.request_sha256 !== requestSha256) {
				conflicts += 1;
				return refusal("idempotency_conflict", 409, "operation_id reused with different request bytes");
			}
			replayed += 1;
			if (mode === "after_commit") throw new TypeError("connection reset after the replay was served");
			return json({ receipt: { ...prior.receipt, state: "replayed" }, result: prior.result });
		}
		const outcome = apply(envelope.command);
		if (outcome instanceof Response) return outcome;
		bump(appliedCounts, envelope.command.type);
		const stored: StoredOperation = {
			receipt: {
				operation_id: envelope.operation_id,
				request_id: envelope.request_id,
				state: "applied",
				request_sha256: requestSha256,
				result_sha256: payloadHash(outcome),
				diagnostics: [],
			},
			command_type: envelope.command.type,
			request_id: envelope.request_id,
			correlation_id: envelope.correlation_id,
			result: outcome,
		};
		operations.set(envelope.operation_id, stored);
		if (mode === "after_commit") throw new TypeError("connection reset after the command committed");
		return json({ receipt: stored.receipt, result: outcome });
	}

	const executionPrefix = `/v1/workspaces/${workspaceId}/execution`;
	const fetchImpl: Fetch = async (input, init) => {
		const url = new URL(input instanceof Request ? input.url : String(input));
		const method = (init?.method ?? "GET").toUpperCase();
		const pathname = url.pathname;
		if (method === "POST") {
			if (pathname === "/v1/commands") return post(init);
			unmatched.push(`${method} ${pathname}`);
			return refusal("not_found", 404, pathname);
		}
		if (pathname === "/v1/health/ready" || pathname === "/v1/health/live") {
			return json({ live: true, ready: true, alerts: [], service_fingerprint: serviceFingerprint });
		}
		const operationMatch = /^\/v1\/operations\/([^/]+)$/.exec(pathname);
		if (operationMatch) {
			const operationId = decodeURIComponent(operationMatch[1] ?? "");
			operationLookups.push(operationId);
			const stored = operations.get(operationId);
			return stored ? json(stored) : refusal("not_found", 404, "unknown operation");
		}
		if (pathname === executionPrefix || pathname.startsWith(`${executionPrefix}/`)) {
			const selector = decodeURIComponent(pathname.slice(executionPrefix.length + 1));
			if (selector && selector !== grant.grant_id && !findWorkItem(selector)) {
				return refusal("not_found", 404, `no execution grant for ${selector}`);
			}
			return json({ grant, items, active_item: active() });
		}
		if (pathname === `/v1/workspaces/${workspaceId}/tree`) {
			return json({ workspace_id: workspaceId, items: workItems, relations: [], projects: [] });
		}
		if (pathname === `/v1/workspaces/${workspaceId}/focus/${ownerId}`) {
			return json({ workspace_id: workspaceId, owner_id: ownerId, work_id: focusWorkId, version: 1 });
		}
		const workItemMatch = /^\/v1\/work-items\/([^/]+)(\/workflow)?$/.exec(pathname);
		if (workItemMatch) {
			const item = findWorkItem(decodeURIComponent(workItemMatch[1] ?? ""));
			if (!item) return refusal("not_found", 404, "unknown work item");
			if (!workItemMatch[2]) return json(item);
			return json({
				item,
				relations: [],
				receipts: receipts.filter(receipt => receipt.work_id === item.work_id),
				close_attempts: closeAttempts.filter(attempt => attempt.work_id === item.work_id),
				audit_manifest: null,
				auditor_launches: [],
				close_attempt_events: closeAttemptEvents.filter(event => event.work_id === item.work_id),
				checkpoint_deliveries: checkpointDeliveries,
				project: null,
			});
		}
		unmatched.push(`${method} ${pathname}`);
		return refusal("not_found", 404, pathname);
	};

	return {
		fetch: fetchImpl,
		dropNextResponse: mode => {
			drop = mode;
		},
		posts: type => total(postCounts, type),
		applied: type => total(appliedCounts, type),
		replayed: () => replayed,
		conflicts: () => conflicts,
		unmatched: () => [...unmatched],
		operationLookups: () => [...operationLookups],
		grant: () => grant,
		items: () => items,
		activeItem: active,
		workItems: () => workItems,
		operation: id => operations.get(id),
		candidate: () => (items[0] ? (findWorkItem(items[0].work_id)?.candidate ?? null) : null),
		closeAttempts: () => closeAttempts,
	};
}
