/**
 * workflow/knowledge-bridge.ts — bounded core TypeScript knowledge bridge.
 *
 * Connects existing workflow execution events to staged Knowledge APIs:
 * 1. Settled tool_result / agent_end capture -> POST /v1/ingest (kind: 'native_record')
 * 2. Fresh task delivery -> POST /v1/context/compile, bundle injection, POST /v1/uses
 * 3. Exact native audit receipt outcome -> POST /v1/uses/{use_id}/outcome after settle
 */
import type {
	AgentEndEvent,
	BeforeAgentStartEvent,
	BeforeAgentStartEventResult,
	ExtensionAPI,
	ExtensionContext,
	ToolCallEvent,
	ToolCallEventResult,
	ToolResultEvent,
} from "@oh-my-pi/pi-coding-agent";
import {
	canonicalJson,
	payloadHash,
	sha256Hex,
	type EvidenceReceipt,
	type Fetch,
	type UUID,
	type WorkflowView,
} from "@oh-my-pi/pi-work-client";
import type { CloseAttemptOutcome, NowRef, WorkflowBackend, WorkStateCarrier } from "./backend";
import {
	DEFAULT_KNOWLEDGE_BUDGET_BYTES,
	loadKnowledgeBearer,
	loadKnowledgeConfig,
	type KnowledgeClientConfig,
	type WorkClientConfig,
} from "./config";

export interface ExecutionWorkspaceRef {
	grantId: string;
	key: string;
	path: string;
	primaryRoot: string;
	branch: string;
	baseline: string;
	reused?: boolean;
	cleanupReady?: boolean;
}

export interface OwnExecutionWitness {
	readonly sessionId: string;
	readonly cwd: string;
	readonly workspace: Readonly<ExecutionWorkspaceRef>;
}

export interface KnowledgeExecutionIdentity {
	workspaceId: string;
	repositoryId: string;
	workId: string;
	revisionId: string;
	candidateId?: string | null;
	candidateSha256?: string | null;
	sourceRevision?: string | null;
	/** Exact immutable knowledge snapshot selected by candidate/source binding. */
	snapshotId?: string | null;
	stage: string;
}

export interface RecordedProposalUse {
	proposal_id: string;
	use_id: string;
}

export interface KnowledgeBundleDetails {
	bundle_id: string;
	bundle_sha256: string;
	enrichment_status?: string;
	proposal_lineage: string[];
	receipt_lineage: string[];
	task_work_id?: string;
	task_revision_id?: string;
	task_candidate_id?: string | null;
	stage?: string;
}

export interface InjectedBundleDetails {
	bundle_id: string;
	bundle_sha256?: string;
	enrichment_status?: string;
	proposal_lineage?: string[];
	receipt_lineage?: string[];
	task_work_id?: string;
	task_revision_id?: string;
	task_candidate_id?: string | null;
	stage?: string;
	[key: string]: unknown;
}

/** v2: the tag is also a key inside the hashed identity payload (Python is the sole hash authority). */
export const CONTEXT_BUNDLE_IDENTITY_ENCODING = "omp-context-bundle-identity/v2";

export interface KnowledgeBudgetSpec {
	method: "utf8_bytes";
	limit: number;
	tokenizer_id?: string | null;
}

/** Mirrors Python BudgetActual: used/mandatory_used/dropped_optional are always emitted; tokenizer_id is not. */
export interface KnowledgeBudgetActual {
	method: "utf8_bytes";
	limit: number;
	used: number;
	mandatory_used: number;
	dropped_optional: string[];
	tokenizer_id?: string | null;
}

export interface KnowledgeBundlePayload {
	bundle_id: string;
	bundle_sha256: string;
	workspace_id: string;
	repository_id: string;
	work_id: string;
	revision_id: string;
	candidate_id: string | null;
	stage: string;
	snapshot_id: string | null;
	budget: KnowledgeBudgetActual;
	mandatory: Record<string, unknown>;
	optional: Record<string, unknown>;
	enrichment_status?: string;
	proposal_lineage: string[];
	receipt_lineage: string[];
	identity_encoding: string;
	identity_canonical_json: string;
	/** Exact Python canonical bytes of {mandatory, optional}; injected verbatim, never rebuilt in TS. */
	content_canonical_json: string;
}

export interface KnowledgeBridgeDeps {
	backend: WorkflowBackend;
	workConfig?: WorkClientConfig;
	knowledgeConfig?: KnowledgeClientConfig | null;
	fetchImpl?: Fetch;
	getExecutionWitness?: (ctx: ExtensionContext) => OwnExecutionWitness | undefined;
	getCarrier?: () => WorkStateCarrier;
	getPlanTarget?: () => NowRef | undefined;
	resolveExecutionIdentity?: (ctx: ExtensionContext) => Promise<KnowledgeExecutionIdentity | null>;
	notices?: string[];
	registerBeforeAgentStart?: boolean;
}

export interface KnowledgeBridge {
	/** Compile a stage-scoped bundle for a native worker. The returned bytes are
	 * task data and must be delimited in the worker task input, never promoted
	 * into a system instruction. */
	prepareStageContext(
		identity: KnowledgeExecutionIdentity,
		ctx: ExtensionContext,
	): Promise<{ content: string; bundle: KnowledgeBundlePayload } | null>;
	onAuditorSettle(
		workKey: string,
		launchId: string,
		settleOutcome: CloseAttemptOutcome,
		ctx?: ExtensionContext,
	): Promise<void>;
	handleToolCall(
		event: ToolCallEvent,
		ctx: ExtensionContext,
	): Promise<ToolCallEventResult | undefined>;
	handleBeforeAgentStart(
		event: BeforeAgentStartEvent,
		ctx: ExtensionContext,
	): Promise<BeforeAgentStartEventResult | undefined>;
	handleToolResult(
		event: ToolResultEvent,
		ctx: ExtensionContext,
	): Promise<void>;
	handleAgentEnd(
		event: AgentEndEvent,
		ctx: ExtensionContext,
	): Promise<void>;
	handleSessionStart(
		ctx: ExtensionContext,
	): Promise<void>;
	recordMissingUses?: (
		identity: KnowledgeExecutionIdentity,
		bundleId: string,
		proposalLineage: readonly string[],
		ctx: ExtensionContext,
		bundleSha256?: string | null,
	) => Promise<void>;
}

/** RFC-4122 deterministic UUID from payload hash */
export function stableId(...parts: unknown[]): UUID {
	const hex = payloadHash(parts);
	const variant = ((parseInt(hex[16]!, 16) & 0x3) | 0x8).toString(16);
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/** Extract SingleResult outputPath / patchPath for task tool results */
export function extractArtifactLocator(toolName: string, details: unknown): string | undefined {
	if (toolName !== "task" || !details || typeof details !== "object") return undefined;
	const d = details as Record<string, unknown>;
	if (Array.isArray(d.results) && d.results.length > 0) {
		const r0 = d.results[0] as Record<string, unknown> | undefined;
		if (typeof r0?.outputPath === "string" && r0.outputPath) return r0.outputPath;
		if (typeof r0?.patchPath === "string" && r0.patchPath) return r0.patchPath;
	}
	if (Array.isArray(d.outputPaths) && typeof d.outputPaths[0] === "string" && d.outputPaths[0]) {
		return d.outputPaths[0];
	}
	return undefined;
}

/** Build deterministic SourceRef without forbidden native lineage fields */
export function buildSourceRef(
	identity: KnowledgeExecutionIdentity,
	sessionId: string | undefined,
	contentSha256: string,
	artifactLocator?: string,
	producer = "session-system/work-now:tool_result",
	observedAt?: string,
): Record<string, unknown> {
	const ref: Record<string, unknown> = {
		workspace_id: identity.workspaceId,
		repository_id: identity.repositoryId,
		work_id: identity.workId,
		revision_id: identity.revisionId,
		observed_at: observedAt ?? new Date().toISOString(),
		producer,
		repository_bound: Boolean(identity.repositoryId && identity.sourceRevision),
	};
	if (identity.candidateId) ref.candidate_id = identity.candidateId;
	if (identity.candidateSha256) ref.candidate_sha256 = identity.candidateSha256;
	if (identity.sourceRevision) ref.source_revision = identity.sourceRevision;
	if (identity.snapshotId) ref.snapshot_id = identity.snapshotId;
	if (sessionId) ref.run_id = sessionId;
	if (identity.stage) ref.stage = identity.stage;
	if (contentSha256) ref.content_sha256 = contentSha256;
	if (artifactLocator) ref.artifact_locator = artifactLocator;
	return ref;
}

/** Build deterministic SourceObservation */
export function buildSourceObservation(
	identity: KnowledgeExecutionIdentity,
	sessionId: string | undefined,
	kind: "tool_output" | "execution_trace",
	payload: Record<string, unknown>,
	artifactLocator?: string,
	producer = "session-system/work-now:tool_result",
	observationId?: string,
	observedAt?: string,
): {
	operationId: string;
	observationId: string;
	contentSha256: string;
	sourceRef: Record<string, unknown>;
	observation: Record<string, unknown>;
} {
	const contentSha256 = sha256Hex(canonicalJson(payload));
	const toolCallId =
		typeof payload.toolCallId === "string"
			? payload.toolCallId
			: (payload.turnId as string | undefined) ?? "direct";
	const operationId = stableId("knowledge-capture", sessionId ?? "no-session", toolCallId, contentSha256);
	const obsId = observationId ?? stableId("observation", sessionId ?? "no-session", toolCallId, contentSha256);
	const sourceRef = buildSourceRef(identity, sessionId, contentSha256, artifactLocator, producer, observedAt);
	const observation: Record<string, unknown> = {
		observation_id: obsId,
		source: sourceRef,
		kind,
		payload,
		payload_sha256: contentSha256,
		relevance_tags: [],
		observed_at: sourceRef.observed_at,
	};
	return { operationId, observationId: obsId, contentSha256, sourceRef, observation };
}

/** Build full NativeRecordIngestRequest */
export function buildNativeRecordIngestRequest(
	identity: KnowledgeExecutionIdentity,
	sessionId: string | undefined,
	kind: "tool_output" | "execution_trace",
	payload: Record<string, unknown>,
	artifactLocator?: string,
	producer = "session-system/work-now:tool_result",
	observationId?: string,
	observedAt?: string,
) {
	const { operationId, sourceRef, observation } = buildSourceObservation(
		identity,
		sessionId,
		kind,
		payload,
		artifactLocator,
		producer,
		observationId,
		observedAt,
	);
	return {
		kind: "native_record" as const,
		operation_id: operationId,
		workspace_id: identity.workspaceId,
		repository_id: identity.repositoryId,
		source_ref: sourceRef,
		observation,
	};
}

/**
 * Strict matcher for persisted custom transcript entries.
 * Matches (type === "custom" || type === "custom_message") with exact customType.
 * Returns the entry's data or details Record if present, or undefined.
 */
export function getPersistedCustomEntry(
	entry: unknown,
	customType: string,
): Record<string, unknown> | undefined {
	if (!entry || typeof entry !== "object") return undefined;
	const e = entry as Record<string, unknown>;
	if (
		(e.type === "custom" || e.type === "custom_message") &&
		e.customType === customType
	) {
		const d = (e.data ?? e.details) as Record<string, unknown> | undefined;
		if (d && typeof d === "object" && !Array.isArray(d)) {
			return d;
		}
	}
	return undefined;
}

/** Check if a tool result has already been captured in transcript */
export function isToolResultCaptured(
	branch: readonly unknown[],
	toolCallId: string,
	contentSha256: string,
	operationId?: string,
): boolean {
	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-capture");
		if (!d) continue;
		if (operationId && d.operation_id === operationId) {
			return true;
		}
		if (d.toolCallId === toolCallId && d.content_sha256 === contentSha256) {
			return true;
		}
	}
	return false;
}

export interface PersistedPendingCapture {
	operation_id: string;
	observation_id: string;
	toolCallId: string;
	content_sha256: string;
	content_hash?: string;
	identity?: KnowledgeExecutionIdentity;
	request_payload?: Record<string, unknown>;
	request_body?: string;
	task_work_id?: string;
	task_revision_id?: string;
	task_candidate_id?: string | null;
	workspace_id?: string;
	repository_id?: string;
	work_id?: string;
	revision_id?: string;
	candidate_id?: string | null;
	stage?: string;
	[key: string]: unknown;
}

/** Find persisted pending capture marker in branch */
export function findPendingCapture(
	branch: readonly unknown[],
	toolCallId: string,
	contentSha256: string,
	operationId?: string,
): PersistedPendingCapture | undefined {
	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-pending-capture");
		if (!d) continue;
		if (operationId && d.operation_id === operationId) {
			return d as unknown as PersistedPendingCapture;
		}
		if (
			typeof d.operation_id === "string" &&
			d.toolCallId === toolCallId &&
			(d.content_sha256 === contentSha256 || d.content_hash === contentSha256)
		) {
			return d as unknown as PersistedPendingCapture;
		}
	}
	return undefined;
}

/** Find all pending captures in branch that do not yet have a matching work-now-capture marker */
export function pendingCapturesInBranch(
	branch: readonly unknown[],
): PersistedPendingCapture[] {
	const capturedOperations = new Set<string>();
	const capturedToolCalls = new Set<string>();

	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-capture");
		if (d) {
			if (typeof d.operation_id === "string") {
				capturedOperations.add(d.operation_id);
			}
			if (typeof d.toolCallId === "string" && typeof d.content_sha256 === "string") {
				capturedToolCalls.add(`${d.toolCallId}:${d.content_sha256}`);
			}
		}
	}

	const seen = new Set<string>();
	const pending: PersistedPendingCapture[] = [];

	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-pending-capture");
		if (
			d &&
			typeof d.operation_id === "string" &&
			typeof d.toolCallId === "string" &&
			typeof d.content_sha256 === "string"
		) {
			const opId = d.operation_id as string;
			const key = `${d.toolCallId}:${d.content_sha256}`;
			if (!capturedOperations.has(opId) && !capturedToolCalls.has(key) && !seen.has(opId)) {
				seen.add(opId);
				pending.push(d as unknown as PersistedPendingCapture);
			}
		}
	}

	return pending;
}

/** Retry pending captures on session start from reloaded transcript */
export async function retryPendingCaptures(
	ctx: ExtensionContext,
	deps: KnowledgeBridgeDeps,
	pi: ExtensionAPI,
	cachedKnowledgeConfig?: KnowledgeClientConfig | null,
	onCaptureOutage?: (msg: string) => void,
): Promise<void> {
	let knowledgeConfig = cachedKnowledgeConfig ?? deps.knowledgeConfig;
	if (knowledgeConfig === undefined) {
		try {
			knowledgeConfig = loadKnowledgeConfig();
		} catch (err) {
			knowledgeConfig = null;
			const msg = `[knowledge] client config invalid (${(err as Error).message ?? String(err)})`;
			if (deps.notices) deps.notices.push(msg);
			else pi.logger?.warn(msg);
		}
	}
	if (!knowledgeConfig) return;

	const branch = ctx.sessionManager?.getBranch?.() ?? [];
	const pending = pendingCapturesInBranch(branch);
	if (pending.length === 0) return;

	const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);
	const token = loadKnowledgeBearer(knowledgeConfig, "knowledge.ingest");
	const headers: Record<string, string> = {
		"Content-Type": "application/json",
		...(token ? { Authorization: `Bearer ${token}` } : {}),
	};
	const url = `${knowledgeConfig.baseUrl.replace(/\/+$/, "")}/v1/ingest`;

	for (const p of pending) {
		const reqBodyBytes =
			typeof p.request_body === "string"
				? p.request_body
				: JSON.stringify(p.request_payload);
		const operationId = p.operation_id;
		const observationId = p.observation_id;
		const toolCallId = p.toolCallId;
		const contentSha256 = p.content_sha256;

		try {
			const res = await fetchFn(url, {
				method: "POST",
				headers,
				body: reqBodyBytes,
				signal: AbortSignal.timeout(10_000),
			});

			if (res.ok || res.status === 409) {
				try {
					pi.appendEntry("work-now-capture", {
						observation_id: observationId,
						operation_id: operationId,
						toolCallId,
						content_sha256: contentSha256,
					});
				} catch (err) {
					const detail = (err as Error)?.message || "marker_append_failed";
					const msg = `[knowledge] capture degraded (${detail.includes("marker") ? detail : `marker_append_failed: ${detail}`})`;
					if (onCaptureOutage) onCaptureOutage(msg);
					else if (deps.notices) deps.notices.push(msg);
					else pi.logger?.warn(msg);
				}
			} else {
				const msg = `[knowledge] capture degraded (${res.status})`;
				if (onCaptureOutage) onCaptureOutage(msg);
				else if (deps.notices) deps.notices.push(msg);
				else pi.logger?.warn(msg);
			}
		} catch {
			const msg = "[knowledge] capture degraded (native_unavailable)";
			if (onCaptureOutage) onCaptureOutage(msg);
			else if (deps.notices) deps.notices.push(msg);
			else pi.logger?.warn(msg);
		}
	}
}

/** Find persisted work-knowledge-bundle message in branch matching execution identity */
export function findInjectedBundle(
	branch: readonly unknown[],
	identity: KnowledgeExecutionIdentity,
): InjectedBundleDetails | undefined {
	for (const entry of branch) {
		const details = getPersistedCustomEntry(entry, "work-knowledge-bundle") as InjectedBundleDetails | undefined;
		if (!details || typeof details !== "object" || typeof details.bundle_id !== "string") continue;

		const workId = details.task_work_id ?? (details as Record<string, unknown>).work_id;
		const revId = details.task_revision_id ?? (details as Record<string, unknown>).revision_id;
		const candId = details.task_candidate_id ?? (details as Record<string, unknown>).candidate_id ?? null;
		const stage = details.stage ?? (details as Record<string, unknown>).stage;

		const matchesWork = workId === identity.workId;
		const matchesRev = revId === identity.revisionId;
		const matchesCand = (candId ?? null) === (identity.candidateId ?? null);
		const matchesStage = stage === identity.stage;

		if (matchesWork && matchesRev && matchesCand && matchesStage) {
			return details;
		}
	}
	return undefined;
}

/** Aggregate recorded uses for a bundle across all work-now-bundle markers in branch */
export function recordedUsesForBundle(
	branch: readonly unknown[],
	bundleId: string,
): Map<string, string> {
	const map = new Map<string, string>();
	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-bundle");
		if (!d || d.bundle_id !== bundleId) continue;

		if (Array.isArray(d.uses)) {
			for (const u of d.uses) {
				if (
					u &&
					typeof u === "object" &&
					typeof (u as Record<string, unknown>).proposal_id === "string" &&
					typeof (u as Record<string, unknown>).use_id === "string"
				) {
					map.set(
						(u as Record<string, unknown>).proposal_id as string,
						(u as Record<string, unknown>).use_id as string,
					);
				}
			}
		}
	}
	return map;
}

/** Check if bundle has been replayed in transcript */
export function isBundleReplayed(
	branch: readonly unknown[],
	bundleId: string,
	proposalIds?: readonly string[],
): boolean {
	const recorded = recordedUsesForBundle(branch, bundleId);
	if (proposalIds !== undefined) {
		return proposalIds.every(pid => recorded.has(pid));
	}
	return recorded.size > 0;
}

/** Check if worker actually received and used the bundle */
export function wasWorkerUsed(
	branch: readonly unknown[],
	bundleId: string,
): boolean {
	for (const entry of branch) {
		const details = getPersistedCustomEntry(entry, "work-knowledge-bundle");
		if (details && details.bundle_id === bundleId) {
			return true;
		}
	}
	return false;
}

/** Default identity resolver deriving authority strictly from one ledger item */
export async function defaultResolveExecutionIdentity(
	ctx: ExtensionContext,
	deps: KnowledgeBridgeDeps,
): Promise<KnowledgeExecutionIdentity | null> {
	if (deps.resolveExecutionIdentity) {
		return deps.resolveExecutionIdentity(ctx);
	}

	const witness = deps.getExecutionWitness?.(ctx);
	if (ctx.taskDepth > 0 && !witness) return null;

	const workspaceId = deps.backend?.workspaceId ?? deps.workConfig?.workspaceId;
	if (!workspaceId) return null;

	const planTarget = deps.getPlanTarget?.();
	const witnessKey = witness?.workspace?.key;
	const planTargetKey = planTarget?.key;

	// Witness and planTarget must not disagree on work key
	if (witnessKey && planTargetKey && witnessKey !== planTargetKey) {
		return null;
	}

	const workKey = witnessKey ?? planTargetKey;
	if (!workKey) return null;

	const workClient = deps.backend?.workClient;
	if (!workClient) return null;

	let item: {
		work_id: string;
		revision?: { revision_id?: string };
		candidate?: { candidate_id?: string; candidate_sha256?: string } | null;
		repository_id?: string | null;
	};
	try {
		item = await workClient.workItem(workKey);
	} catch {
		return null;
	}

	if (!item || !item.work_id || !item.revision?.revision_id || !item.repository_id) {
		return null;
	}

	// Disagreement checks against planTarget
	if (planTarget) {
		if (planTarget.key && planTarget.key !== workKey) return null;
		if (planTarget.id && planTarget.id !== item.work_id) return null;
	}

	// Disagreement checks against carrier
	const carrier = deps.getCarrier?.();
	if (carrier) {
		if (carrier.revisionId && carrier.revisionId !== item.revision.revision_id) {
			return null;
		}
		if (carrier.candidateId && carrier.candidateId !== item.candidate?.candidate_id) {
			return null;
		}
		if (carrier.candidateSha && carrier.candidateSha !== item.candidate?.candidate_sha256) {
			return null;
		}
		if (
			carrier.plannedCandidateId &&
			item.candidate?.candidate_id &&
			carrier.plannedCandidateId !== item.candidate.candidate_id
		) {
			return null;
		}
	}

	let snapshotId: string | null = null;
	if (item.candidate?.candidate_id) {
		try {
			const workflow = await workClient.workflow(workKey);
			const association = workflow.candidate_source_versions.find(
				row => row.candidate_id === item.candidate?.candidate_id,
			);
			snapshotId = association?.snapshot_id ?? null;
		} catch {
			// Source association is optional for legacy candidates; context compiler
			// still receives an explicit null snapshot rather than guessing one.
		}
	}

	const stage = ctx.taskDepth > 0 ? "subagent" : planTarget ? "plan" : "execute";

	return {
		workspaceId,
		repositoryId: item.repository_id,
		workId: item.work_id,
		revisionId: item.revision.revision_id,
		candidateId: item.candidate?.candidate_id ?? null,
		candidateSha256: item.candidate?.candidate_sha256 ?? null,
		sourceRevision: witness?.workspace?.baseline ?? null,
		snapshotId,
		stage,
	};
}

/** Post outcome to /v1/uses/{use_id}/outcome, persisting pending identity on transient failure */
async function postOutcomeWithRetry(
	knowledgeConfig: KnowledgeClientConfig,
	useId: string,
	receiptId: string,
	workerUsed: boolean,
	identity: { workId: string; revisionId: string; candidateId?: string | null },
	deps: KnowledgeBridgeDeps,
	piRef?: ExtensionAPI,
	onRejected?: (status: number, useId: string) => void,
): Promise<void> {
	const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);
	const token = loadKnowledgeBearer(knowledgeConfig, "knowledge.ingest");
	const headers: Record<string, string> = {
		"Content-Type": "application/json",
		...(token ? { Authorization: `Bearer ${token}` } : {}),
	};
	const url = `${knowledgeConfig.baseUrl.replace(/\/+$/, "")}/v1/uses/${encodeURIComponent(useId)}/outcome`;
	const body = JSON.stringify({
		worker_used: workerUsed,
		outcome_receipt_id: receiptId,
	});

	try {
		const res = await fetchFn(url, {
			method: "POST",
			headers,
			body,
			signal: AbortSignal.timeout(10_000),
		});

		if (res.status === 200 || res.status === 409) {
			// 200: recorded; 409: already recorded (replay no-op)
			return;
		}
		if (res.status >= 400 && res.status < 500) {
			if (onRejected) {
				onRejected(res.status, useId);
			} else {
				const msg = `[knowledge] outcome rejected (${res.status}) use=${useId}`;
				if (deps.notices) deps.notices.push(msg);
				else piRef?.logger?.warn(msg);
			}
			return;
		}
		if (res.status === 503 || res.status >= 500) {
			// Transient failure - persist retry identity
			try {
				piRef?.appendEntry("work-now-pending-outcome", {
					use_id: useId,
					outcome_receipt_id: receiptId,
					worker_used: workerUsed,
					task_work_id: identity.workId,
					task_revision_id: identity.revisionId,
					task_candidate_id: identity.candidateId ?? null,
				});
			} catch {}
		}
	} catch {
		// Network/timeout failure -> persist retry identity
		try {
			piRef?.appendEntry("work-now-pending-outcome", {
				use_id: useId,
				outcome_receipt_id: receiptId,
				worker_used: workerUsed,
				task_work_id: identity.workId,
				task_revision_id: identity.revisionId,
				task_candidate_id: identity.candidateId ?? null,
			});
		} catch {}
	}
}

/** Retry pending outcomes on session start from reloaded transcript */
export async function retryPendingOutcomes(
	ctx: ExtensionContext,
	deps: KnowledgeBridgeDeps,
	pi: ExtensionAPI,
	cachedKnowledgeConfig?: KnowledgeClientConfig | null,
): Promise<void> {
	let knowledgeConfig = cachedKnowledgeConfig ?? deps.knowledgeConfig;
	if (knowledgeConfig === undefined) {
		try {
			knowledgeConfig = loadKnowledgeConfig();
		} catch (err) {
			knowledgeConfig = null;
			const msg = `[knowledge] client config invalid (${(err as Error).message ?? String(err)})`;
			if (deps.notices) deps.notices.push(msg);
			else pi.logger?.warn(msg);
		}
	}
	if (!knowledgeConfig) return;

	const branch = ctx.sessionManager?.getBranch?.() ?? [];
	const acks = new Set<string>();
	for (const entry of branch) {
		const ackData = getPersistedCustomEntry(entry, "work-now-outcome-ack");
		if (ackData && typeof ackData.use_id === "string") {
			acks.add(ackData.use_id);
		}
	}

	const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);
	const token = loadKnowledgeBearer(knowledgeConfig, "knowledge.ingest");
	const headers: Record<string, string> = {
		"Content-Type": "application/json",
		...(token ? { Authorization: `Bearer ${token}` } : {}),
	};

	const retried = new Set<string>();
	for (const entry of branch) {
		const d = getPersistedCustomEntry(entry, "work-now-pending-outcome");
		if (!d || typeof d.use_id !== "string" || acks.has(d.use_id) || retried.has(d.use_id)) continue;
		retried.add(d.use_id);

		const url = `${knowledgeConfig.baseUrl.replace(/\/+$/, "")}/v1/uses/${encodeURIComponent(d.use_id)}/outcome`;
		const body = JSON.stringify({
			worker_used: Boolean(d.worker_used),
			outcome_receipt_id: d.outcome_receipt_id,
		});

		try {
			const res = await fetchFn(url, {
				method: "POST",
				headers,
				body,
				signal: AbortSignal.timeout(10_000),
			});
			if (res.status === 200 || res.status === 409) {
				acks.add(d.use_id);
				try {
					pi.appendEntry("work-now-outcome-ack", { use_id: d.use_id });
				} catch {}
			} else if (res.status >= 400 && res.status < 500) {
				acks.add(d.use_id);
				try {
					pi.appendEntry("work-now-outcome-ack", {
						use_id: d.use_id,
						outcome_receipt_id: d.outcome_receipt_id,
						worker_used: Boolean(d.worker_used),
						task_work_id: d.task_work_id,
						task_revision_id: d.task_revision_id,
						task_candidate_id: d.task_candidate_id ?? null,
						status: res.status,
						rejected: true,
					});
				} catch {}
				const msg = `[knowledge] outcome rejected (${res.status}) use=${d.use_id}`;
				if (deps.notices) deps.notices.push(msg);
				else pi.logger?.warn(msg);
			}
		} catch {
			// Transient network/timeout failure: remains pending for next restart
		}
	}
}

/**
 * Select the exact audit receipt for an auditor launch from a workflow view.
 *
 * Rules:
 * 1. Find launch in view.auditor_launches by launchId; return null if absent or
 *    (expected.attemptId is provided and launch.attempt_id !== expected.attemptId).
 * 2. Filter view.receipts for kind === "audit", issuer === "work-service/auditor-settle",
 *    payload.launch_id === launchId, and non-empty work_id and revision_id.
 * 3. Return candidates[0] only if candidates.length === 1 and candidates[0].verdict === expected.verdict;
 *    otherwise return null on missing, ambiguous, or mismatch.
 */
export function selectAuditReceiptForLaunch(
	view: Pick<WorkflowView, "receipts" | "auditor_launches">,
	launchId: string,
	expected: { attemptId?: string; verdict: string },
): EvidenceReceipt | null {
	if (!view || !launchId || !expected?.verdict) return null;
	const launches = view.auditor_launches;
	if (!Array.isArray(launches)) return null;
	const launch = launches.find(l => l.launch_id === launchId);
	if (!launch) return null;
	if (expected.attemptId && launch.attempt_id !== expected.attemptId) return null;

	const receipts = view.receipts;
	if (!Array.isArray(receipts)) return null;
	const candidates = receipts.filter(
		r =>
			r.kind === "audit" &&
			r.issuer === "work-service/auditor-settle" &&
			(r.payload as Record<string, unknown> | undefined)?.launch_id === launchId &&
			Boolean(r.work_id) &&
			Boolean(r.revision_id),
	);

	if (candidates.length !== 1) return null;
	const candidate = candidates[0];
	if (candidate.verdict !== expected.verdict) return null;
	return candidate;
}

function isDeepEqual(a: unknown, b: unknown): boolean {
	if (a === b) return true;
	if (a === null || typeof a !== "object" || b === null || typeof b !== "object") return false;
	if (Array.isArray(a)) {
		if (!Array.isArray(b) || a.length !== b.length) return false;
		for (let i = 0; i < a.length; i++) {
			if (!isDeepEqual(a[i], b[i])) return false;
		}
		return true;
	}
	if (Array.isArray(b)) return false;
	const keysA = Object.keys(a as Record<string, unknown>);
	const keysB = Object.keys(b as Record<string, unknown>);
	if (keysA.length !== keysB.length) return false;
	for (const key of keysA) {
		if (!Object.prototype.hasOwnProperty.call(b, key)) return false;
		if (!isDeepEqual((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key])) return false;
	}
	return true;
}

/**
 * Positional [start, end) of a top-level key's value inside canonical JSON object text
 * (sorted keys, no whitespace). Walks depth-1 members only, skipping strings (with escapes)
 * and nested objects/arrays, so a same-named key nested inside a value can never match.
 * Never re-serializes: it only locates bytes Python already emitted.
 */
export function topLevelValueSpan(text: string, key: string): { start: number; end: number } | undefined {
	if (text[0] !== "{") return undefined;
	const skipString = (from: number): number => {
		for (let j = from + 1; j < text.length; j++) {
			if (text[j] === "\\") j++;
			else if (text[j] === '"') return j + 1;
		}
		return -1;
	};
	const skipValue = (from: number): number => {
		const first = text[from];
		if (first === '"') return skipString(from);
		if (first === "{" || first === "[") {
			let depth = 0;
			for (let j = from; j < text.length; ) {
				const c = text[j];
				if (c === '"') {
					j = skipString(j);
					if (j < 0) return -1;
					continue;
				}
				if (c === "{" || c === "[") depth++;
				else if (c === "}" || c === "]") {
					depth--;
					if (depth === 0) return j + 1;
				}
				j++;
			}
			return -1;
		}
		let j = from;
		while (j < text.length && text[j] !== "," && text[j] !== "}") j++;
		return j;
	};
	const literalKey = `${JSON.stringify(key)}:`;
	let i = 1;
	while (i < text.length && text[i] !== "}") {
		if (text[i] !== '"') return undefined;
		const keyEnd = skipString(i);
		if (keyEnd < 0 || text[keyEnd] !== ":") return undefined;
		const start = keyEnd + 1;
		const end = skipValue(start);
		if (end < 0) return undefined;
		if (text.slice(i, start) === literalKey) return { start, end };
		if (text[end] === ",") i = end + 1;
		else if (text[end] === "}") break;
		else return undefined;
	}
	return undefined;
}

function isCount(value: unknown): value is number {
	return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

/** Validate compile response before use or injection; fail closed on any defect */
export function validateKnowledgeBundlePayload(
	raw: unknown,
	expectedIdentity?: KnowledgeExecutionIdentity,
	expectedBudget?: KnowledgeBudgetSpec,
): {
	ok: true;
	bundle: KnowledgeBundlePayload;
	contentBytes: string;
} | {
	ok: false;
	reason: string;
} {
	if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
		return { ok: false, reason: "payload is not an object" };
	}
	const b = raw as Record<string, unknown>;
	if (typeof b.bundle_id !== "string" || !b.bundle_id.trim()) {
		return { ok: false, reason: "bundle_id must be a non-empty string" };
	}
	if (typeof b.bundle_sha256 !== "string" || !b.bundle_sha256.trim()) {
		return { ok: false, reason: "bundle_sha256 must be a non-empty string" };
	}
	if (typeof b.identity_encoding !== "string" || b.identity_encoding !== CONTEXT_BUNDLE_IDENTITY_ENCODING) {
		return { ok: false, reason: `identity_encoding must be "${CONTEXT_BUNDLE_IDENTITY_ENCODING}"` };
	}
	if (typeof b.identity_canonical_json !== "string" || !b.identity_canonical_json.trim()) {
		return { ok: false, reason: "identity_canonical_json must be a non-empty string" };
	}

	const computedSha256 = sha256Hex(b.identity_canonical_json);
	if (computedSha256 !== b.bundle_sha256) {
		return {
			ok: false,
			reason: `bundle_sha256 mismatch (expected ${computedSha256}, got ${String(b.bundle_sha256)})`,
		};
	}

	let ident: Record<string, unknown>;
	try {
		const parsed = JSON.parse(b.identity_canonical_json);
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
			return { ok: false, reason: "identity payload is not an object" };
		}
		ident = parsed as Record<string, unknown>;
	} catch {
		return { ok: false, reason: "malformed identity_canonical_json" };
	}

	if (!Array.isArray(b.proposal_lineage) || !b.proposal_lineage.every(p => typeof p === "string")) {
		return { ok: false, reason: "proposal_lineage must be an array of strings" };
	}
	if (!Array.isArray(b.receipt_lineage) || !b.receipt_lineage.every(r => typeof r === "string")) {
		return { ok: false, reason: "receipt_lineage must be an array of strings" };
	}
	if (
		b.mandatory === undefined ||
		typeof b.mandatory !== "object" ||
		b.mandatory === null ||
		Array.isArray(b.mandatory)
	) {
		return { ok: false, reason: "mandatory must be an object if provided" };
	}
	if (
		b.optional === undefined ||
		typeof b.optional !== "object" ||
		b.optional === null ||
		Array.isArray(b.optional)
	) {
		return { ok: false, reason: "optional must be an object if provided" };
	}
	const mandatory = b.mandatory as Record<string, unknown>;
	const optional = b.optional as Record<string, unknown>;

	if (typeof b.workspace_id !== "string" || !b.workspace_id.trim()) {
		return { ok: false, reason: "workspace_id must be a non-empty string" };
	}
	if (typeof b.repository_id !== "string" || !b.repository_id.trim()) {
		return { ok: false, reason: "repository_id must be a non-empty string" };
	}
	if (typeof b.work_id !== "string" || !b.work_id.trim()) {
		return { ok: false, reason: "work_id must be a non-empty string" };
	}
	if (typeof b.revision_id !== "string" || !b.revision_id.trim()) {
		return { ok: false, reason: "revision_id must be a non-empty string" };
	}
	if (typeof b.stage !== "string" || !b.stage.trim()) {
		return { ok: false, reason: "stage must be a non-empty string" };
	}
	if (b.candidate_id !== null && typeof b.candidate_id !== "string") {
		return { ok: false, reason: "candidate_id must be a string or null" };
	}
	if (b.snapshot_id !== null && typeof b.snapshot_id !== "string") {
		return { ok: false, reason: "snapshot_id must be a string or null" };
	}

	const budgetRaw = (b.budget ?? b.budget_spec) as Record<string, unknown> | undefined;
	if (!budgetRaw || typeof budgetRaw !== "object" || Array.isArray(budgetRaw)) {
		return { ok: false, reason: "budget must be an object" };
	}
	if (budgetRaw.method !== "utf8_bytes") {
		return { ok: false, reason: 'budget.method must be "utf8_bytes"' };
	}
	if (
		typeof budgetRaw.limit !== "number" ||
		!Number.isSafeInteger(budgetRaw.limit) ||
		budgetRaw.limit <= 0
	) {
		return { ok: false, reason: "budget.limit must be a positive safe integer" };
	}
	if (
		budgetRaw.tokenizer_id !== undefined &&
		budgetRaw.tokenizer_id !== null &&
		typeof budgetRaw.tokenizer_id !== "string"
	) {
		return { ok: false, reason: "budget.tokenizer_id must be a string or null" };
	}
	// BudgetActual fields the Python compiler always emits; a spec-only budget is not a compile result
	if (!isCount(budgetRaw.used)) {
		return { ok: false, reason: "budget.used must be a non-negative safe integer" };
	}
	if (!isCount(budgetRaw.mandatory_used)) {
		return { ok: false, reason: "budget.mandatory_used must be a non-negative safe integer" };
	}
	if (!Array.isArray(budgetRaw.dropped_optional) || !budgetRaw.dropped_optional.every(d => typeof d === "string")) {
		return { ok: false, reason: "budget.dropped_optional must be an array of strings" };
	}

	if (ident.identity_encoding !== CONTEXT_BUNDLE_IDENTITY_ENCODING) {
		return { ok: false, reason: `identity payload identity_encoding must be "${CONTEXT_BUNDLE_IDENTITY_ENCODING}"` };
	}
	if (ident.workspace_id !== b.workspace_id) {
		return { ok: false, reason: `workspace_id mismatch between identity and bundle (identity: ${String(ident.workspace_id)}, bundle: ${String(b.workspace_id)})` };
	}
	if (ident.repository_id !== b.repository_id) {
		return { ok: false, reason: `repository_id mismatch between identity and bundle (identity: ${String(ident.repository_id)}, bundle: ${String(b.repository_id)})` };
	}
	if (ident.work_id !== b.work_id) {
		return { ok: false, reason: `work_id mismatch between identity and bundle (identity: ${String(ident.work_id)}, bundle: ${String(b.work_id)})` };
	}
	if (ident.revision_id !== b.revision_id) {
		return { ok: false, reason: `revision_id mismatch between identity and bundle (identity: ${String(ident.revision_id)}, bundle: ${String(b.revision_id)})` };
	}
	if (ident.stage !== b.stage) {
		return { ok: false, reason: `stage mismatch between identity and bundle (identity: ${String(ident.stage)}, bundle: ${String(b.stage)})` };
	}
	if ((ident.candidate_id ?? null) !== (b.candidate_id ?? null)) {
		return { ok: false, reason: `candidate_id mismatch between identity and bundle (identity: ${String(ident.candidate_id ?? null)}, bundle: ${String(b.candidate_id ?? null)})` };
	}
	if ((ident.snapshot_id ?? null) !== (b.snapshot_id ?? null)) {
		return { ok: false, reason: `snapshot_id mismatch between identity and bundle (identity: ${String(ident.snapshot_id ?? null)}, bundle: ${String(b.snapshot_id ?? null)})` };
	}

	if (!ident.budget_spec || typeof ident.budget_spec !== "object" || Array.isArray(ident.budget_spec)) {
		return { ok: false, reason: "budget_spec in identity payload must be an object" };
	}
	const identBudget = ident.budget_spec as Record<string, unknown>;
	if (identBudget.method !== budgetRaw.method) {
		return { ok: false, reason: "budget.method mismatch between identity and bundle" };
	}
	if (identBudget.limit !== budgetRaw.limit) {
		return { ok: false, reason: "budget.limit mismatch between identity and bundle" };
	}

	if (!ident.content || typeof ident.content !== "object" || Array.isArray(ident.content)) {
		return { ok: false, reason: "content in identity payload must be an object" };
	}
	const identContent = ident.content as Record<string, unknown>;
	if (!isDeepEqual(identContent.mandatory, mandatory)) {
		return { ok: false, reason: "mandatory content mismatch between identity and bundle" };
	}
	if (!isDeepEqual(identContent.optional, optional)) {
		return { ok: false, reason: "optional content mismatch between identity and bundle" };
	}

	// Exact Python content bytes: must be the literal value range of the TOP-LEVEL `content` key
	// of identity_canonical_json (located positionally among the sorted top-level keys, never by
	// substring search that a nested value could satisfy). TS never rebuilds them.
	const contentCanonicalJson = b.content_canonical_json;
	if (typeof contentCanonicalJson !== "string" || !contentCanonicalJson.trim()) {
		return { ok: false, reason: "content_canonical_json must be a non-empty string (legacy bundle is unverifiable)" };
	}
	const identityText = b.identity_canonical_json as string;
	const contentSpan = topLevelValueSpan(identityText, "content");
	if (!contentSpan || identityText.slice(contentSpan.start, contentSpan.end) !== contentCanonicalJson) {
		return { ok: false, reason: "content_canonical_json is not the exact content byte range of identity_canonical_json" };
	}
	let retainedContent: Record<string, unknown>;
	try {
		const parsed = JSON.parse(contentCanonicalJson);
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
			return { ok: false, reason: "content_canonical_json is not an object" };
		}
		retainedContent = parsed as Record<string, unknown>;
	} catch {
		return { ok: false, reason: "malformed content_canonical_json" };
	}
	if (!isDeepEqual(retainedContent.mandatory, mandatory) || !isDeepEqual(retainedContent.optional, optional)) {
		return { ok: false, reason: "content_canonical_json does not match served mandatory/optional content" };
	}
	const contentByteLength = new TextEncoder().encode(contentCanonicalJson).length;
	if (contentByteLength !== budgetRaw.used) {
		return {
			ok: false,
			reason: `budget.used mismatch against content_canonical_json bytes (expected ${contentByteLength}, got ${String(budgetRaw.used)})`,
		};
	}

	if (expectedIdentity) {
		if (b.workspace_id !== expectedIdentity.workspaceId) {
			return {
				ok: false,
				reason: `workspace_id mismatch (expected ${expectedIdentity.workspaceId}, got ${String(b.workspace_id)})`,
			};
		}
		if (b.repository_id !== expectedIdentity.repositoryId) {
			return {
				ok: false,
				reason: `repository_id mismatch (expected ${expectedIdentity.repositoryId}, got ${String(b.repository_id)})`,
			};
		}
		if (b.work_id !== expectedIdentity.workId) {
			return {
				ok: false,
				reason: `work_id mismatch (expected ${expectedIdentity.workId}, got ${String(b.work_id)})`,
			};
		}
		if (b.revision_id !== expectedIdentity.revisionId) {
			return {
				ok: false,
				reason: `revision_id mismatch (expected ${expectedIdentity.revisionId}, got ${String(b.revision_id)})`,
			};
		}
		if (b.stage !== expectedIdentity.stage) {
			return {
				ok: false,
				reason: `stage mismatch (expected ${expectedIdentity.stage}, got ${String(b.stage)})`,
			};
		}
		if ((b.candidate_id ?? null) !== (expectedIdentity.candidateId ?? null)) {
			return {
				ok: false,
				reason: `candidate_id mismatch (expected ${expectedIdentity.candidateId ?? null}, got ${String(b.candidate_id ?? null)})`,
			};
		}
	}

	const servedBudget: KnowledgeBudgetActual = {
		method: "utf8_bytes",
		limit: budgetRaw.limit as number,
		used: budgetRaw.used,
		mandatory_used: budgetRaw.mandatory_used,
		dropped_optional: [...(budgetRaw.dropped_optional as string[])],
		...(budgetRaw.tokenizer_id !== undefined ? { tokenizer_id: budgetRaw.tokenizer_id as string | null } : {}),
	};

	if (expectedBudget) {
		if (servedBudget.method !== expectedBudget.method || servedBudget.limit !== expectedBudget.limit) {
			return {
				ok: false,
				reason: `budget mismatch (expected ${JSON.stringify(expectedBudget)}, got ${JSON.stringify(servedBudget)})`,
			};
		}
		if (expectedBudget.tokenizer_id !== undefined) {
			const expectedTok = expectedBudget.tokenizer_id ?? null;
			const identTok = (identBudget.tokenizer_id as string | null | undefined) ?? null;
			if (identTok !== expectedTok) {
				return {
					ok: false,
					reason: `budget tokenizer_id mismatch in identity payload (expected ${String(expectedTok)}, got ${String(identTok)})`,
				};
			}
		}
	}

	const contentBytes = contentCanonicalJson;

	const bundle: KnowledgeBundlePayload = {
		bundle_id: b.bundle_id as string,
		bundle_sha256: b.bundle_sha256 as string,
		workspace_id: b.workspace_id as string,
		repository_id: b.repository_id as string,
		work_id: b.work_id as string,
		revision_id: b.revision_id as string,
		candidate_id: (b.candidate_id as string | null) ?? null,
		stage: b.stage as string,
		snapshot_id: (b.snapshot_id as string | null) ?? null,
		budget: servedBudget,
		mandatory,
		optional,
		enrichment_status: typeof b.enrichment_status === "string" ? b.enrichment_status : undefined,
		proposal_lineage: b.proposal_lineage as string[],
		receipt_lineage: b.receipt_lineage as string[],
		identity_encoding: b.identity_encoding as string,
		identity_canonical_json: b.identity_canonical_json as string,
		content_canonical_json: contentCanonicalJson,
	};

	return { ok: true, bundle, contentBytes };
}

/**
 * Register knowledge bridge on pi.
 */
export function registerKnowledgeBridge(
	pi: ExtensionAPI,
	deps: KnowledgeBridgeDeps,
): KnowledgeBridge {
	let captureOutageNoticed = false;
	let compileOutageNoticed = false;
	let useOutageNoticed = false;
	let outcomeRejectedNoticed = false;
	const capturedInSession = new Set<string>();
	const pendingTaskBundles = new Map<
		string,
		{ bundle_id: string; bundle_sha256: string }
	>();

	const queueNotice = (msg: string) => {
		if (deps.notices) deps.notices.push(msg);
		else pi.logger?.warn(msg);
	};

	let cachedConfig: KnowledgeClientConfig | null | undefined = deps.knowledgeConfig;
	let configLoadAttempted = deps.knowledgeConfig !== undefined;

	const getKConfig = (): KnowledgeClientConfig | null => {
		if (!configLoadAttempted) {
			configLoadAttempted = true;
			try {
				cachedConfig = loadKnowledgeConfig();
			} catch (err) {
				cachedConfig = null;
				queueNotice(`[knowledge] client config invalid (${(err as Error).message ?? String(err)})`);
			}
		}
		return cachedConfig ?? null;
	};

	async function executeDurableCapture(
		ctx: ExtensionContext,
		kConfig: KnowledgeClientConfig,
		toolCallId: string,
		payload: Record<string, unknown>,
		kind: "tool_output" | "execution_trace",
		producer: string,
		artifactLocator?: string,
	): Promise<void> {
		const contentSha256 = sha256Hex(canonicalJson(payload));
		const sessionKey = `${toolCallId}:${contentSha256}`;

		if (capturedInSession.has(sessionKey)) return;

		const branch = ctx.sessionManager?.getBranch?.() ?? [];
		if (isToolResultCaptured(branch, toolCallId, contentSha256)) {
			capturedInSession.add(sessionKey);
			return;
		}

		let reqBodyBytes: string;
		let operationId: string;
		let observationId: string;

		const pending = findPendingCapture(branch, toolCallId, contentSha256);
		if (pending && (pending.request_payload || pending.request_body)) {
			if (isToolResultCaptured(branch, toolCallId, contentSha256, pending.operation_id)) {
				capturedInSession.add(sessionKey);
				return;
			}
			reqBodyBytes = typeof pending.request_body === "string"
				? pending.request_body
				: JSON.stringify(pending.request_payload);
			const parsed = (pending.request_payload ?? JSON.parse(reqBodyBytes)) as Record<string, unknown>;
			operationId = (pending.operation_id ?? parsed.operation_id) as string;
			observationId = (pending.observation_id ?? (parsed.observation as Record<string, unknown>)?.observation_id) as string;
		} else {
			const identity = await defaultResolveExecutionIdentity(ctx, deps);
			if (!identity) return;

			const sessionId = ctx.sessionManager?.getSessionId?.();
			const built = buildNativeRecordIngestRequest(
				identity,
				sessionId,
				kind,
				payload,
				artifactLocator,
				producer,
			);
			const reqBody = built as unknown as Record<string, unknown>;
			reqBodyBytes = JSON.stringify(reqBody);
			operationId = built.operation_id;
			observationId = built.observation.observation_id as string;

			try {
				pi.appendEntry("work-now-pending-capture", {
					operation_id: operationId,
					observation_id: observationId,
					toolCallId,
					content_sha256: contentSha256,
					content_hash: contentSha256,
					identity: {
						workspaceId: identity.workspaceId,
						repositoryId: identity.repositoryId,
						workId: identity.workId,
						revisionId: identity.revisionId,
						candidateId: identity.candidateId ?? null,
						candidateSha256: identity.candidateSha256 ?? null,
						sourceRevision: identity.sourceRevision ?? null,
						stage: identity.stage,
					},
					task_work_id: identity.workId,
					task_revision_id: identity.revisionId,
					task_candidate_id: identity.candidateId ?? null,
					workspace_id: identity.workspaceId,
					repository_id: identity.repositoryId,
					work_id: identity.workId,
					revision_id: identity.revisionId,
					candidate_id: identity.candidateId ?? null,
					stage: identity.stage,
					request_payload: reqBody,
					request_body: reqBodyBytes,
				});
			} catch (err) {
				if (!captureOutageNoticed) {
					captureOutageNoticed = true;
					const detail = (err as Error)?.message || "marker_append_failed";
					queueNotice(`[knowledge] capture degraded (${detail.includes("marker") ? detail : `marker_append_failed: ${detail}`})`);
				}
				return;
			}
		}

		const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);
		const token = loadKnowledgeBearer(kConfig, "knowledge.ingest");
		const headers: Record<string, string> = {
			"Content-Type": "application/json",
			...(token ? { Authorization: `Bearer ${token}` } : {}),
		};

		try {
			const res = await fetchFn(`${kConfig.baseUrl.replace(/\/+$/, "")}/v1/ingest`, {
				method: "POST",
				headers,
				body: reqBodyBytes,
				signal: AbortSignal.timeout(10_000),
			});

			if (res.ok || res.status === 409) {
				capturedInSession.add(sessionKey);
				try {
					pi.appendEntry("work-now-capture", {
						observation_id: observationId,
						operation_id: operationId,
						toolCallId,
						content_sha256: contentSha256,
					});
				} catch (err) {
					if (!captureOutageNoticed) {
						captureOutageNoticed = true;
						const detail = (err as Error)?.message || "marker_append_failed";
						queueNotice(`[knowledge] capture degraded (${detail.includes("marker") ? detail : `marker_append_failed: ${detail}`})`);
					}
				}
			} else {
				if (!captureOutageNoticed) {
					captureOutageNoticed = true;
					queueNotice(`[knowledge] capture degraded (${res.status})`);
				}
			}
		} catch {
			if (!captureOutageNoticed) {
				captureOutageNoticed = true;
				queueNotice("[knowledge] capture degraded (native_unavailable)");
			}
		}
	}

	async function handleToolResult(event: ToolResultEvent, ctx: ExtensionContext): Promise<void> {
		const kConfig = getKConfig();
		if (!kConfig) return;

		// Nested sessions without witness are skipped
		if (ctx.taskDepth > 0 && !deps.getExecutionWitness?.(ctx)) return;

		if (event.toolName === "task") {
			if (Boolean(event.isError)) {
				pendingTaskBundles.delete(event.toolCallId);
			} else {
				const pending = pendingTaskBundles.get(event.toolCallId);
				if (pending) {
					const branch = ctx.sessionManager?.getBranch?.() ?? [];
					const alreadyMarked = branch.some(entry => {
						const d = getPersistedCustomEntry(entry, "work-knowledge-bundle");
						return d && d.bundle_id === pending.bundle_id && d.toolCallId === event.toolCallId;
					});
					if (!alreadyMarked) {
						try {
							pi.appendEntry("work-knowledge-bundle", {
								bundle_id: pending.bundle_id,
								bundle_sha256: pending.bundle_sha256,
								toolCallId: event.toolCallId,
							});
						} catch {}
					}
					pendingTaskBundles.delete(event.toolCallId);
				}
			}
		}

		const payload = {
			toolCallId: event.toolCallId,
			toolName: event.toolName,
			input: event.input ?? {},
			content: event.content ?? [],
			details: event.details ?? null,
			isError: Boolean(event.isError),
		};
		const artifactLocator = extractArtifactLocator(event.toolName, event.details);
		const kind = event.toolName === "task" ? "execution_trace" : "tool_output";

		await executeDurableCapture(
			ctx,
			kConfig,
			event.toolCallId,
			payload,
			kind,
			"session-system/work-now:tool_result",
			artifactLocator,
		);
	}

	async function handleAgentEnd(event: AgentEndEvent, ctx: ExtensionContext): Promise<void> {
		const kConfig = getKConfig();
		if (!kConfig) return;

		if (ctx.taskDepth > 0 && !deps.getExecutionWitness?.(ctx)) return;
		if (event.willContinue === true) return;

		const sessionId = ctx.sessionManager?.getSessionId?.();
		const toolCallId = `agent_end_${sessionId ?? "session"}`;
		const payload = {
			turnId: toolCallId,
			messageCount: event.messages?.length ?? 0,
			willContinue: false,
		};

		await executeDurableCapture(
			ctx,
			kConfig,
			toolCallId,
			payload,
			"execution_trace",
			"session-system/work-now:agent_end",
			undefined,
		);
	}

	async function recordMissingUses(
		identity: KnowledgeExecutionIdentity,
		bundleId: string,
		proposalLineage: readonly string[],
		ctx: ExtensionContext,
		bundleSha256?: string | null,
	): Promise<void> {
		const kConfig = getKConfig();
		if (!kConfig) return;

		const branch = ctx.sessionManager?.getBranch?.() ?? [];
		const recorded = recordedUsesForBundle(branch, bundleId);

		const missingProposals = proposalLineage.filter(pid => !recorded.has(pid));
		if (missingProposals.length === 0) return;

		const ingestToken = loadKnowledgeBearer(kConfig, "knowledge.ingest");
		const ingestHeaders: Record<string, string> = {
			"Content-Type": "application/json",
			...(ingestToken ? { Authorization: `Bearer ${ingestToken}` } : {}),
		};
		const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);

		const newUses: Array<{ proposal_id: string; use_id: string }> = [];

		for (const proposalId of missingProposals) {
			const useReq = {
				proposal_id: proposalId,
				workspace_id: identity.workspaceId,
				repository_id: identity.repositoryId,
				task_work_id: identity.workId,
				task_revision_id: identity.revisionId,
				task_candidate_id: identity.candidateId ?? null,
				bundle_id: bundleId,
			};

			try {
				const useRes = await fetchFn(`${kConfig.baseUrl.replace(/\/+$/, "")}/v1/uses`, {
					method: "POST",
					headers: ingestHeaders,
					body: JSON.stringify(useReq),
					signal: AbortSignal.timeout(10_000),
				});
				if (useRes.ok) {
					const rec = (await useRes.json()) as { use_id?: string } | undefined;
					if (rec && typeof rec.use_id === "string" && rec.use_id) {
						newUses.push({ proposal_id: proposalId, use_id: rec.use_id });
					} else {
						if (!useOutageNoticed) {
							useOutageNoticed = true;
							queueNotice(`[knowledge] use recording degraded (${useRes.status})`);
						}
					}
				} else {
					if (!useOutageNoticed) {
						useOutageNoticed = true;
						queueNotice(`[knowledge] use recording degraded (${useRes.status})`);
					}
				}
			} catch {
				if (!useOutageNoticed) {
					useOutageNoticed = true;
					queueNotice("[knowledge] use recording degraded (native_unavailable)");
				}
			}
		}

		if (newUses.length > 0) {
			try {
				pi.appendEntry("work-now-bundle", {
					bundle_id: bundleId,
					...(bundleSha256 ? { bundle_sha256: bundleSha256 } : {}),
					uses: newUses,
					use_ids: newUses.map(u => u.use_id),
					task_work_id: identity.workId,
					task_revision_id: identity.revisionId,
					task_candidate_id: identity.candidateId ?? null,
				});
			} catch {}
		}
	}

	async function compileAndRecordUses(
		identity: KnowledgeExecutionIdentity,
		ctx: ExtensionContext,
	): Promise<{
		bundleMessage: {
			customType: string;
			content: string;
			details: KnowledgeBundleDetails;
		};
		bundle: KnowledgeBundlePayload;
	} | null> {
		const kConfig = getKConfig();
		if (!kConfig) return null;

		const budgetSpec: KnowledgeBudgetSpec = {
			method: "utf8_bytes",
			limit: kConfig.budgetLimit ?? DEFAULT_KNOWLEDGE_BUDGET_BYTES,
			tokenizer_id: null,
		};

		const compileBody = {
			workspace_id: identity.workspaceId,
			repository_id: identity.repositoryId,
			work_id: identity.workId,
			revision_id: identity.revisionId,
			candidate_id: identity.candidateId ?? null,
			stage: identity.stage,
			snapshot_id: identity.snapshotId ?? null,
			budget: budgetSpec,
		};

		const fetchFn = deps.fetchImpl ?? (globalThis.fetch as unknown as Fetch);
		const readToken = loadKnowledgeBearer(kConfig, "knowledge.read");
		const readHeaders: Record<string, string> = {
			"Content-Type": "application/json",
			...(readToken ? { Authorization: `Bearer ${readToken}` } : {}),
		};

		let compileRes: Response;
		try {
			compileRes = await fetchFn(`${kConfig.baseUrl.replace(/\/+$/, "")}/v1/context/compile`, {
				method: "POST",
				headers: readHeaders,
				body: JSON.stringify(compileBody),
				signal: AbortSignal.timeout(10_000),
			});
		} catch {
			if (!compileOutageNoticed) {
				compileOutageNoticed = true;
				queueNotice("[knowledge] context compile unavailable (native_unavailable)");
			}
			return null;
		}

		if (!compileRes.ok) {
			if (!compileOutageNoticed) {
				compileOutageNoticed = true;
				queueNotice(`[knowledge] context compile unavailable (${compileRes.status})`);
			}
			return null;
		}

		let raw: unknown;
		try {
			raw = await compileRes.json();
		} catch {
			if (!compileOutageNoticed) {
				compileOutageNoticed = true;
				queueNotice("[knowledge] context compile unavailable (malformed_response)");
			}
			return null;
		}

		const validated = validateKnowledgeBundlePayload(raw, identity, budgetSpec);
		if (!validated.ok) {
			if (!compileOutageNoticed) {
				compileOutageNoticed = true;
				queueNotice("[knowledge] context compile unavailable (malformed_response)");
			}
			return null;
		}

		const { bundle, contentBytes } = validated;

		const bundleMessage: {
			customType: string;
			content: string;
			details: KnowledgeBundleDetails;
		} = {
			customType: "work-knowledge-bundle",
			content: contentBytes,
			details: {
				bundle_id: bundle.bundle_id,
				bundle_sha256: bundle.bundle_sha256,
				enrichment_status: bundle.enrichment_status,
				proposal_lineage: bundle.proposal_lineage ?? [],
				receipt_lineage: bundle.receipt_lineage ?? [],
				task_work_id: identity.workId,
				task_revision_id: identity.revisionId,
				task_candidate_id: identity.candidateId ?? null,
				stage: identity.stage,
			},
		};

		await recordMissingUses(
			identity,
			bundle.bundle_id,
			bundle.proposal_lineage ?? [],
			ctx,
			bundle.bundle_sha256,
		);

		return { bundleMessage, bundle };
	}

	async function prepareStageContext(
		identity: KnowledgeExecutionIdentity,
		ctx: ExtensionContext,
	): Promise<{ content: string; bundle: KnowledgeBundlePayload } | null> {
		const compiled = await compileAndRecordUses(identity, ctx);
		if (!compiled) return null;
		return { content: compiled.bundleMessage.content, bundle: compiled.bundle };
	}

	async function handleBeforeAgentStart(
		_event: BeforeAgentStartEvent,
		ctx: ExtensionContext,
	): Promise<BeforeAgentStartEventResult | undefined> {
		if (ctx.taskDepth > 0) return undefined;

		const identity = await defaultResolveExecutionIdentity(ctx, deps);
		if (!identity) return undefined;

		const branch = ctx.sessionManager?.getBranch?.() ?? [];
		const persisted = findInjectedBundle(branch, identity);
		if (persisted) {
			await recordMissingUses(
				identity,
				persisted.bundle_id,
				persisted.proposal_lineage ?? [],
				ctx,
				persisted.bundle_sha256,
			);
			return undefined;
		}

		const compiled = await compileAndRecordUses(identity, ctx);
		if (!compiled) return undefined;

		return {
			message: compiled.bundleMessage,
		};
	}

	async function handleTaskToolCall(
		event: ToolCallEvent,
		ctx: ExtensionContext,
	): Promise<ToolCallEventResult | undefined> {
		if (event.toolName !== "task") return undefined;

		const witness = deps.getExecutionWitness?.(ctx);
		if (!witness) return undefined;

		const identity = await defaultResolveExecutionIdentity(ctx, deps);
		if (!identity) return undefined;

		// Force stage to 'subagent' for spawned fresh workers
		const subagentIdentity: KnowledgeExecutionIdentity = {
			...identity,
			stage: "subagent",
		};

		const compiled = await compileAndRecordUses(subagentIdentity, ctx);
		if (!compiled) return undefined;

		const bundleBytes = compiled.bundleMessage.content;
		const existingContext = typeof event.input?.context === "string" ? event.input.context : "";
		const prependedContext = existingContext ? `${bundleBytes}\n\n${existingContext}` : bundleBytes;

		pendingTaskBundles.set(event.toolCallId, {
			bundle_id: compiled.bundle.bundle_id,
			bundle_sha256: compiled.bundle.bundle_sha256,
		});

		return {
			input: {
				...event.input,
				context: prependedContext,
			},
		};
	}

	async function onAuditorSettle(
		workKey: string,
		launchId: string,
		settleOutcome: CloseAttemptOutcome,
		ctx?: ExtensionContext,
	): Promise<void> {
		if (settleOutcome.status === "refused") return;
		if (!settleOutcome.verdict || !["PASS", "NEEDS_FIX", "BLOCKED"].includes(settleOutcome.verdict)) return;

		const kConfig = getKConfig();
		if (!kConfig) return;

		if (settleOutcome.launchId && settleOutcome.launchId !== launchId) return;

		const workClient = deps.backend?.workClient;
		if (!workClient) return;

		let view: WorkflowView;
		try {
			view = await workClient.workflow(workKey);
		} catch {
			return;
		}

		const receipt = selectAuditReceiptForLaunch(view, launchId, {
			attemptId: settleOutcome.attemptId,
			verdict: settleOutcome.verdict,
		});
		if (!receipt) return;

		const manager = ctx?.sessionManager;
		const branch = manager?.getBranch?.() ?? [];

		for (const entry of branch) {
			const data = getPersistedCustomEntry(entry, "work-now-bundle");
			if (!data) continue;
			if (
				data.task_work_id === receipt.work_id &&
				data.task_revision_id === receipt.revision_id &&
				(data.task_candidate_id ?? null) === (receipt.candidate_id ?? null)
			) {
				const workerUsed = wasWorkerUsed(branch, data.bundle_id as string);
				const useIds: string[] = Array.isArray(data.use_ids) ? (data.use_ids as string[]) : [];
				for (const useId of useIds) {
					await postOutcomeWithRetry(
						kConfig,
						useId,
						receipt.receipt_id,
						workerUsed,
						{
							workId: receipt.work_id,
							revisionId: receipt.revision_id,
							candidateId: receipt.candidate_id,
						},
						deps,
						pi,
						(status, id) => {
							if (!outcomeRejectedNoticed) {
								outcomeRejectedNoticed = true;
								queueNotice(`[knowledge] outcome rejected (${status}) use=${id}`);
							}
						},
					);
				}
			}
		}
	}

	async function handleSessionStart(ctx: ExtensionContext): Promise<void> {
		captureOutageNoticed = false;
		compileOutageNoticed = false;
		useOutageNoticed = false;
		outcomeRejectedNoticed = false;
		capturedInSession.clear();
		pendingTaskBundles.clear();
		if (deps.knowledgeConfig === undefined) {
			configLoadAttempted = false;
		}
		const branch = ctx.sessionManager?.getBranch?.() ?? [];
		for (const entry of branch) {
			const d = getPersistedCustomEntry(entry, "work-now-capture");
			if (d && typeof d.toolCallId === "string" && typeof d.content_sha256 === "string") {
				capturedInSession.add(`${d.toolCallId}:${d.content_sha256}`);
			}
		}
		const kConfig = getKConfig();
		await retryPendingCaptures(ctx, deps, pi, kConfig, msg => {
			if (!captureOutageNoticed) {
				captureOutageNoticed = true;
				queueNotice(msg);
			}
		});
		await retryPendingOutcomes(ctx, deps, pi, kConfig);
	}

	// Register event listeners
	pi.on("session_start", async (_event, ctx) => {
		await handleSessionStart(ctx);
	});
	pi.on("session_switch", async (_event, ctx) => {
		captureOutageNoticed = false;
		compileOutageNoticed = false;
		useOutageNoticed = false;
		outcomeRejectedNoticed = false;
		capturedInSession.clear();
		pendingTaskBundles.clear();
		if (deps.knowledgeConfig === undefined) {
			configLoadAttempted = false;
		}
		const branch = ctx.sessionManager?.getBranch?.() ?? [];
		for (const entry of branch) {
			const d = getPersistedCustomEntry(entry, "work-now-capture");
			if (d && typeof d.toolCallId === "string" && typeof d.content_sha256 === "string") {
				capturedInSession.add(`${d.toolCallId}:${d.content_sha256}`);
			}
		}
		const kConfig = getKConfig();
		await retryPendingCaptures(ctx, deps, pi, kConfig, msg => {
			if (!captureOutageNoticed) {
				captureOutageNoticed = true;
				queueNotice(msg);
			}
		});
		await retryPendingOutcomes(ctx, deps, pi, kConfig);
	});
	if (deps.registerBeforeAgentStart ?? true) {
		pi.on("before_agent_start", async (event, ctx) => {
			return handleBeforeAgentStart(event, ctx);
		});
	}
	pi.on("tool_result", async (event, ctx) => {
		await handleToolResult(event, ctx);
	});
	pi.on("agent_end", async (event, ctx) => {
		await handleAgentEnd(event, ctx);
	});

	return {
		prepareStageContext,
		onAuditorSettle,
		handleToolCall: handleTaskToolCall,
		handleBeforeAgentStart,
		handleToolResult,
		handleAgentEnd,
		handleSessionStart,
		recordMissingUses,
	};
}
