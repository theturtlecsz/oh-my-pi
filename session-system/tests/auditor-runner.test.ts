import * as fs from "node:fs";
import type { ToolCallEventResult } from "@oh-my-pi/pi-coding-agent/extensibility/shared-events";
import * as os from "node:os";
import { spawnSync } from "node:child_process";
import { WORK_CONTRACT_SHA256, sha256Hex, type Candidate, type WorkClient, type ExecutionProvenanceEnvelope, type RecordStagePreflightPayload, type BeginStagePreflightPayload, type BeginStagePreflightResult, type AdmitStagePreflightPayload, type AdmitStagePreflightResult, type CancelStagePreflightPayload, type CancelStagePreflightResult } from "@oh-my-pi/pi-work-client";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import * as path from "node:path";
import { z } from "zod";
import { Agent } from "@oh-my-pi/pi-agent-core";
import { type Model, type Usage, AssistantMessageEventStream } from "@oh-my-pi/pi-ai";
import * as ai from "@oh-my-pi/pi-ai";
import { AgentSession, SessionManager, Settings, type CustomEntry, type ExtensionAPI, type ExtensionCommandContext, type ExtensionContext, type PersistedTurnContinuationRequest } from "@oh-my-pi/pi-coding-agent";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import * as executorModule from "@oh-my-pi/pi-coding-agent/task/executor";
import * as managedGit from "@oh-my-pi/pi-coding-agent/utils/git";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { getProjectDir, setProjectDir } from "@oh-my-pi/pi-utils";
import { applyExtensionNewSessionSetup } from "../../packages/coding-agent/src/modes/controllers/extension-ui-controller";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { Effort } from "@oh-my-pi/pi-catalog/effort";
import { createExtensionModelQuery } from "../../packages/coding-agent/src/extensibility/extensions/model-api";
import { prepareNativeAuditRunner, prepareNativeStageRunner, type NativeStagePreflightAttempt } from "../extensions/workflow/auditor-runner";
import { resolveAuditPolicy } from "../extensions/workflow/audit-policy";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";
import type { CloseAttemptSnapshot, CloseAttemptSession, ExecutionItemPhase, ExecutionSnapshot } from "../extensions/workflow/backend";
import {
	confirmWrite,
	RECEIPT_TTL_MS,
	resetConfirmations,
} from "../extensions/workflow/confirm";
import * as gitModule from "../extensions/workflow/git";
import type { ExecutionWorkspace } from "../extensions/workflow/git";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";
import { headCommit } from "../extensions/workflow/git";
import {
	computeExecutionNoticeDetails,
	expandExecutionPlanClosure,
	renderExecutionTerminalBanner,
	renderNextActionBanner,
	renderSummaryResumeDigest,
	resolveAnchorKey,
} from "../extensions/workflow/host";

// Host tests provide installed-agent discovery and isolated caches explicitly so
// they do not depend on an existing OMP installation in the developer home.
const fixtureCaches: string[] = [];
function temporaryCacheFile(): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-auditor-cache-"));
	fixtureCaches.push(dir);
	return path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(dir, "cache.json"));
}

/** Component fixture only; installed qualification must obtain this through actual /execute. */
function executionOwnershipEntry(exec: ExecutionSnapshot, cwd: string, key: string, overrides: Partial<ExecutionWorkspace> = {}): CustomEntry {
	return { type: "custom", customType: "work-now", id: crypto.randomUUID(), parentId: null, timestamp: new Date().toISOString(), data: {
		backend: "work", executionWorkspace: { grantId: exec.grant.grant_id, key, primaryRoot: exec.grant.repository, path: cwd, branch: `execution/${key.toLowerCase()}`, baseline: exec.activeItem?.initial_git_baseline, reused: false, ...overrides },
	} };
}

function createAuditorTestModel(overrides: Partial<Model> = {}): Model {
	return buildModel({
		id: "gpt-5.6-sol",
		name: "GPT 5.6 Sol",
		api: "openai-codex-responses",
		provider: "openai-codex",
		baseUrl: "https://example.test",
		reasoning: true,
		thinking: {
			mode: "effort",
			efforts: [Effort.Low, Effort.Medium, Effort.High],
			effortRouting: { medium: "gpt-5.6-sol" },
		},
		input: ["text"],
		cost: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 200000,
		maxTokens: 8192,
		...overrides,
	});
}

function createAuditorTestModelQuery(settingsOverride?: Settings, additionalModels: Model[] = []) {
	const sol = createAuditorTestModel();
	const available = [sol, ...additionalModels] as Model<Api>[];
	const registry = {
		getAvailable: () => available,
	} as unknown as ModelRegistry;
	const settings = settingsOverride ?? Settings.isolated({
		modelRoles: {
			audit: "openai-codex/gpt-5.6-sol:medium",
		},
	});
	return createExtensionModelQuery(registry, settings, () => sol);
}

interface InMemoryStageLaunch {
	launch_id: string;
	work_key: string;
	role: string;
	status: "reserved" | "handed_off" | "settled" | "canceled" | "interrupted";
	request_sha256?: string;
	tool_call_id?: string;
	task_sha256?: string;
	outcome_sha256?: string;
	outcome?: unknown;
	served_model?: string | null;
	served_selector?: string | null;
	resolved_model?: string | null;
	resolved_selector?: string | null;
	created_at: string;
	updated_at: string;
}

interface StageReservationInput {
	workKey?: string;
	work_key?: string;
	workId?: string;
	revisionId?: string;
	candidateId?: string | null;
	attemptId?: string | null;
	grantId?: string | null;
	role: string;
	requestSha256?: string;
	request_sha256?: string;
	toolCallId?: string;
	tool_call_id?: string;
	taskSha256?: string;
	task_sha256?: string;
	resolvedModel?: string | null;
	resolved_model?: string | null;
	resolvedSelector?: string | null;
	resolved_selector?: string | null;
}

interface StageSettleInput {
	launchId: string;
	outcomeSha256?: string;
	outcome?: unknown;
	servedModel?: string | null;
	servedSelector?: string | null;
}

interface StageLaunchTransitions {
	callLog?: string[];
	onReserve?: (launch: InMemoryStageLaunch) => void;
	onHandoff?: (launchId: string, taskSha256?: string) => void;
	onSettle?: (input: StageSettleInput) => void;
	onCancel?: (launchId: string, reason?: unknown) => void;
	onReconcile?: (launchId: string, reason?: string) => void;
}

function attachStageLaunchFixture<T extends Record<string, unknown>>(
	mockBackend: T,
	defaultRepoId = "00000000-0000-7000-8000-000000000005",
	transitions?: StageLaunchTransitions,
): T {
	const stageLaunches = new Map<string, InMemoryStageLaunch>();
	const stagePreflights: Record<string, unknown>[] = [];
	let nextLaunchId = 1;
	let nextPreflightId = 1;

	const backendRecord = mockBackend as Record<string, unknown>;
	const workClient = backendRecord.workClient as Record<string, unknown> | undefined;

	if (workClient) {
		const origWorkItem = typeof workClient.workItem === "function"
			? (workClient.workItem as (key: string) => Promise<Record<string, unknown> | undefined>).bind(workClient)
			: undefined;
		workClient.workItem = async (workKey: string) => {
			const existing = origWorkItem ? await origWorkItem(workKey) : undefined;
			return {
				work_id: existing?.work_id ?? `work-${workKey}`,
				workspace_id: backendRecord.workspaceId ?? "ws-1",
				project_id: existing?.project_id ?? "proj-1",
				repository_id: existing?.repository_id ?? defaultRepoId,
				revision: existing?.revision ?? {
					revision_id: `rev-${workKey}`,
					work_id: existing?.work_id ?? `work-${workKey}`,
					revision_number: 1,
					title: "test",
					description: "test",
					scope: "repo",
					acceptance_criteria: [],
					content_sha256: "0".repeat(64),
					created_by: "test",
					created_at: new Date().toISOString(),
				},
				candidate: existing?.candidate ?? null,
				state: existing?.state ?? "executing",
				archived: false,
				...existing,
			};
		};

		const origWorkflow = typeof workClient.workflow === "function"
			? (workClient.workflow as (key: string) => Promise<Record<string, unknown>>).bind(workClient)
			: undefined;
		workClient.workflow = async (workKey: string) => {
			const existing = origWorkflow ? await origWorkflow(workKey) : {};
			const rows = Array.from(stageLaunches.values());
			const existingRows = Array.isArray(existing.stage_launches) ? existing.stage_launches : [];
			const existingPreflights = Array.isArray(existing.stage_preflights) ? existing.stage_preflights : [];
			return {
				...existing,
				stage_launches: [...existingRows, ...rows],
				stage_preflights: [...existingPreflights, ...stagePreflights],
			};
		};
	}

	backendRecord.stageLaunches = stageLaunches;
	backendRecord.stagePreflights = stagePreflights;

	let lastPreflightIntent: any = undefined;
	backendRecord.beginStagePreflight = async (payload: BeginStagePreflightPayload): Promise<BeginStagePreflightResult> => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("beginStagePreflight");
		const transport_attempt_id = crypto.randomUUID();
		const intent = {
			intent_id: `intent-${nextPreflightId}`,
			workspace_id: (backendRecord.workspaceId as string) ?? "ws-1",
			work_id: payload.work_id,
			revision_id: payload.revision_id,
			candidate_id: payload.candidate_id ?? null,
			attempt_id: payload.attempt_id ?? null,
			grant_id: payload.grant_id ?? null,
			role: payload.role,
			tool_call_id: payload.tool_call_id,
			task_sha256: payload.task_sha256,
			probe_sha256: payload.probe_sha256,
			transport_attempt_id,
			ordinal: payload.ordinal,
			requested_selector: payload.requested_selector,
			requested_provider: payload.requested_provider,
			requested_model: payload.requested_model,
			requested_api: payload.requested_api,
			requested_effort: payload.requested_effort ?? null,
			requested_wire_model: payload.requested_wire_model,
			is_fallback: payload.is_fallback,
			logical_sha256: "0".repeat(64),
			group_sha256: "0".repeat(64),
			host_owner_id: "00000000-0000-0000-0000-000000000001",
			dispatched_at: null,
			dispatch_operation_id: null,
			dispatch_owner_id: null,
			cancelled_at: null,
			cancelled_by: null,
			cancel_reason: null,
			status: "begun" as const,
			created_at: new Date().toISOString(),
			settled_at: null,
		};
		lastPreflightIntent = intent;
		return {
			type: "begin_stage_preflight",
			status: "applied",
			intent,
			preflight: undefined,
		};
	};

	backendRecord.admitStagePreflight = async (payload: AdmitStagePreflightPayload): Promise<AdmitStagePreflightResult> => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("admitStagePreflight");
		const intent = {
			...(lastPreflightIntent ?? {
				intent_id: `intent-${nextPreflightId}`,
				workspace_id: (backendRecord.workspaceId as string) ?? "ws-1",
				work_id: "work-1",
				revision_id: null,
				candidate_id: null,
				attempt_id: null,
				grant_id: null,
				role: "audit",
				tool_call_id: "call-1",
				task_sha256: "0".repeat(64),
				probe_sha256: "0".repeat(64),
				transport_attempt_id: payload.transport_attempt_id,
				ordinal: 0,
				requested_selector: "gemini:gemini-3.8-flash",
				requested_provider: "google-antigravity",
				requested_model: "gemini-3.8-flash",
				requested_api: "google-gemini-cli",
				requested_effort: null,
				requested_wire_model: "gemini-3.8-flash",
				is_fallback: false,
				logical_sha256: payload.logical_sha256,
				group_sha256: "0".repeat(64),
				host_owner_id: "00000000-0000-0000-0000-000000000001",
				created_at: new Date().toISOString(),
				settled_at: null,
			}),
			transport_attempt_id: payload.transport_attempt_id,
			logical_sha256: payload.logical_sha256,
			status: "dispatched" as const,
			dispatched_at: new Date().toISOString(),
			dispatch_operation_id: "00000000-0000-0000-0000-000000000001",
			dispatch_owner_id: "00000000-0000-0000-0000-000000000001",
		};
		lastPreflightIntent = intent;
		return {
			type: "admit_stage_preflight",
			status: "applied",
			intent,
			preflight: undefined,
		};
	};

	backendRecord.cancelStagePreflight = async (payload: CancelStagePreflightPayload): Promise<CancelStagePreflightResult> => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("cancelStagePreflight");
		const intent = {
			...(lastPreflightIntent ?? {
				intent_id: `intent-${nextPreflightId}`,
				workspace_id: (backendRecord.workspaceId as string) ?? "ws-1",
				work_id: "work-1",
				revision_id: null,
				candidate_id: null,
				attempt_id: null,
				grant_id: null,
				role: "audit",
				tool_call_id: "call-1",
				task_sha256: "0".repeat(64),
				probe_sha256: "0".repeat(64),
				transport_attempt_id: payload.transport_attempt_id,
				ordinal: 0,
				requested_selector: "gemini:gemini-3.8-flash",
				requested_provider: "google-antigravity",
				requested_model: "gemini-3.8-flash",
				requested_api: "google-gemini-cli",
				requested_effort: null,
				requested_wire_model: "gemini-3.8-flash",
				is_fallback: false,
				logical_sha256: payload.logical_sha256,
				group_sha256: "0".repeat(64),
				host_owner_id: "00000000-0000-0000-0000-000000000001",
				dispatched_at: null,
				dispatch_operation_id: null,
				dispatch_owner_id: null,
				created_at: new Date().toISOString(),
				settled_at: null,
			}),
			transport_attempt_id: payload.transport_attempt_id,
			logical_sha256: payload.logical_sha256,
			status: "cancelled_undispatched" as const,
			cancelled_at: new Date().toISOString(),
			cancelled_by: "00000000-0000-0000-0000-000000000001",
			cancel_reason: payload.reason,
		};
		lastPreflightIntent = intent;
		return {
			type: "cancel_stage_preflight",
			status: "applied",
			intent,
			preflight: undefined,
		};
	};

	backendRecord.recordStagePreflight = async (payload: RecordStagePreflightPayload) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("recordStagePreflight");
		const preflight_id = `preflight-${nextPreflightId++}`;
		const preflight = {
			preflight_id,
			workspace_id: (backendRecord.workspaceId as string) ?? "ws-1",
			observed_at: new Date().toISOString(),
			...payload,
		};
		stagePreflights.push(preflight);
		return preflight;
	};

	backendRecord.reserveStageLaunch = async (input: StageReservationInput) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("reserveStageLaunch");
		const launch_id = `launch-${nextLaunchId++}`;
		const row: InMemoryStageLaunch = {
			launch_id,
			work_key: input.workKey ?? input.work_key ?? "OMP-1",
			role: input.role,
			status: "reserved",
			request_sha256: input.requestSha256 ?? input.request_sha256,
			tool_call_id: input.toolCallId ?? input.tool_call_id,
			task_sha256: input.taskSha256 ?? input.task_sha256,
			resolved_model: input.resolvedModel ?? input.resolved_model,
			resolved_selector: input.resolvedSelector ?? input.resolved_selector,
			created_at: new Date().toISOString(),
			updated_at: new Date().toISOString(),
		};
		stageLaunches.set(launch_id, row);
		transitions?.onReserve?.(row);
		return {
			...row,
			workspace_id: backendRecord.workspaceId ?? "ws-1",
			work_id: input.workId ?? input.work_key,
			revision_id: input.revisionId ?? "rev-1",
			candidate_id: input.candidateId ?? null,
			attempt_id: input.attemptId ?? null,
			grant_id: input.grantId ?? null,
			status: "reserved" as const,
		};
	};

	backendRecord.handoffStageLaunch = async (launchId: string, taskSha256?: string) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push(`handoffStageLaunch:${launchId}`);
		const row = stageLaunches.get(launchId);
		if (row) {
			row.status = "handed_off";
			row.task_sha256 = taskSha256;
			row.updated_at = new Date().toISOString();
		}
		transitions?.onHandoff?.(launchId, taskSha256);
		return {
			launch_id: launchId,
			status: "handed_off" as const,
			task_sha256: taskSha256,
		};
	};

	backendRecord.settleStageLaunch = async (input: StageSettleInput) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push("settleStageLaunch");
		const row = stageLaunches.get(input.launchId);
		if (row) {
			row.status = "settled";
			row.outcome_sha256 = input.outcomeSha256;
			row.outcome = input.outcome;
			row.served_model = input.servedModel;
			row.served_selector = input.servedSelector;
			row.updated_at = new Date().toISOString();
		}
		transitions?.onSettle?.(input);
		return {
			launch_id: input.launchId,
			status: "settled" as const,
			outcome_sha256: input.outcomeSha256 ?? null,
			outcome: input.outcome ?? null,
			served_model: input.servedModel ?? null,
			served_selector: input.servedSelector ?? null,
		};
	};

	backendRecord.cancelStageLaunch = async (launchId: string, reason?: unknown) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push(`cancelStageLaunch:${launchId}`);
		const row = stageLaunches.get(launchId);
		if (row) {
			row.status = "canceled";
			row.outcome = reason;
			row.updated_at = new Date().toISOString();
		}
		transitions?.onCancel?.(launchId, reason);
		return {
			launch_id: launchId,
			status: "canceled" as const,
		};
	};

	backendRecord.reconcileStageLaunch = async (launchId: string, reason?: string) => {
		const callLog = transitions?.callLog ?? (backendRecord.callLog as string[] | undefined);
		callLog?.push(`reconcileStageLaunch:${launchId}`);
		const row = stageLaunches.get(launchId);
		if (row) {
			row.status = "interrupted";
			row.outcome = reason;
			row.updated_at = new Date().toISOString();
		}
		transitions?.onReconcile?.(launchId, reason);
		return {
			launch_id: launchId,
			status: "interrupted" as const,
		};
	};

	return mockBackend;
}
beforeEach(() => {
	vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
		agents: [{
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			output: { properties: { report: { type: "string" } } },
			source: "bundled",
		}],
		projectAgentsDir: null,
	});
	// OMP-251: prepareNativeAuditRunner now runs a live transport probe via
	// completeSimple before any launch reservation — default it to success so
	// existing preparation/host flows stay green without network access.
	vi.spyOn(ai, "completeSimple").mockResolvedValue({
		stopReason: "stop",
		content: [{ type: "text", text: "OK" }],
	} as never);
});

describe("execution recovery identity guards", () => {
	type ContinuationIdentity = {
		grantId: string;
		sessionId: string;
		workId: string;
		revisionId: string;
		preReservationVersion: number;
		postVersion: number;
		messageId: string;
	};
	type OutboxEntry = {
		type: "custom";
		customType: "work-now-execute-outbox";
		data: ContinuationIdentity & { status: "pending" | "queued"; at: string };
	};
	type InjectedEntry = {
		attribution?: "agent" | "user";
		id?: string;
		type: "custom_message";
		customType: "work-execute";
		details: { executionContinuation: ContinuationIdentity };
	};
	type AttemptBinding = {
		attempt_id: string;
		work_id: string;
		execution_grant_id: string;
		authorization_kind: "execution";
		revision_id: string;
		candidate_id: string;
		candidate_sha256: string;
		candidate_commit: string;
		state: string;
	};

	async function recoveryFixture() {
		const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "recovery-bindings-"));
		fixtureCaches.push(cwd);
		const sessionId = "recovery-binding-session";
		const baseline = "1".repeat(40);
		let head = baseline;
		const exec = makeSnapshot("active", "single", [{ position: 0, work_id: "work-recovery", phase: "executing" }]);
		exec.grant.repository = cwd;
		exec.grant.grant_version = 2;
		exec.grant.expires_at = new Date(Date.now() + 86400000).toISOString();
		exec.activeItem!.initial_git_baseline = baseline;
		exec.activeItem!.current_git_baseline = baseline;
		exec.activeItem!.criteria_revision_id = "criteria-revision";
		exec.activeItem!.project_id = null;
		const item = {
			work_id: "work-recovery",
			state: "IN_PROGRESS",
			project_id: null,
			revision: { revision_id: "criteria-revision" },
			candidate: undefined as Candidate | undefined,
		};
		const attempts: AttemptBinding[] = [];
		const entries: Array<OutboxEntry | InjectedEntry | CustomEntry | { id: string; type: "message"; message: {
			role: "assistant"; content: Array<{type:"toolCall";id:string;name:string;arguments:Record<string,unknown>}>;
		} }> = [];
		const sent: Array<{ customType?: string; details?: { executionContinuation?: ContinuationIdentity } }> = [];
		const notices: string[] = [];
		const continuations: PersistedTurnContinuationRequest[] = [];
		const handlers: Array<(event: unknown, ctx: ExtensionContext) => Promise<void>> = [];
		const taskHandlers: Array<(event: unknown, ctx: ExtensionContext) => Promise<unknown>> = [];
		const issue = { id: item.work_id, key: "OMP-246", title: "Recovery", project: "Bookends" };
		const reserve = vi.fn(async () => {
			exec.grant.grant_version++;
			return exec;
		});
		const backend = {
			cacheFile: temporaryCacheFile(), markerFile: ".work-project", evidenceKinds: ["verification", "closeout"], scopeFix: "",
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			findIssue: async () => issue,
			currentNow: async () => issue,
			pendingDeliveries: async () => [],
			getPendingExecutionClaims: async () => [],
			setExecutionState: reserve,
			workClient: {
				healthReady: async () => ({ service_fingerprint: "recovery-fixture-service" }),
				workItem: async () => item,
				workflow: async () => ({ item, relations: [], close_attempts: attempts }),
			},
		} as unknown as WorkflowBackend;
		const ctx = {
			cwd, taskDepth: 0,
			models: createAuditorTestModelQuery(),
			sessionManager: { getBranch: () => entries, getCwd: () => cwd, getSessionId: () => sessionId, getLeafId: () => "persisted-entry" },
			ui: { notify: (text: string) => notices.push(text), theme: { fg: (_color: string, text: string) => text }, setStatus: () => {} },
		} as unknown as ExtensionContext;
		const pi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {}, registerCommand: () => {}, registerFlag: () => {}, registerMessageRenderer: () => {},
			on: (event: string, handler: (event: unknown, ctx: ExtensionContext) => Promise<void>) => { if (event === "session_start") handlers.push(handler); if(event === "tool_call") taskHandlers.push(handler); },
			getSessionId: () => sessionId,
			requestPersistedTurnContinuation: (request: PersistedTurnContinuationRequest) => { continuations.push(request); return { status: "scheduled" }; },
			sendMessage: (message: typeof sent[number]) => sent.push(message),
			appendEntry: () => {}, zod: z,
		} as unknown as ExtensionAPI;
		createWorkflowHost({ backend, teamNoun: "the ledger", entryType: "work-now", acceptEntry: () => true, executionWorkspaceManager: identityExecutionWorkspaceManager })(pi);
		exec.grant.judge_sha256 = (await computeAuditTcb(ctx, backend.workClient!)).judgeSha256;
		vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		vi.spyOn(gitModule, "headCommit").mockImplementation(() => head);
		const intent: ContinuationIdentity = {
			grantId: exec.grant.grant_id, sessionId, workId: item.work_id, revisionId: item.revision.revision_id,
			preReservationVersion: 1, postVersion: 2, messageId: "queued-recovery",
		};
		entries.push({ type: "custom", customType: "work-now-execute-outbox", data: { ...intent, status: "queued", at: new Date().toISOString() } });
		entries.push(executionOwnershipEntry(exec, cwd, issue.key));
		return {
			intent, entries, sent, reserve, notices, attempts, item, exec, continuations,
			backend,
			taskCall: async (origin?: {sessionId:string;promptEntryId:string}) => {
				let result: ToolCallEventResult | undefined;
				for(const handler of taskHandlers) result = await handler({type:"tool_call",toolName:"task",toolCallId:"original-task",input:{},taskResultOrigin:origin},ctx) as ToolCallEventResult | undefined;
				return result;
			},
			setHead: (value: string) => { head = value; },
			start: async () => {
				if (!handlers.length) throw new Error("workflow host did not register startup recovery");
				for (const handler of handlers) await handler({}, ctx);
			},
		};
	}

	test.each([
		["foreign session", { sessionId: "another-session" }],
		["different work item", { workId: "another-item" }],
		["stale revision", { revisionId: "claimed-revision-before-criteria" }],
		["future reservation", { postVersion: 3 }],
	] as const)("queued intent with %s starts no turn or reservation", async (_name, changed) => {
		const fixture = await recoveryFixture();
		const queued = fixture.entries[0] as OutboxEntry;
		Object.assign(queued.data, changed);
		await fixture.start();
		expect(fixture.sent.filter(message => message.customType === "work-execute")).toHaveLength(0);
		expect(fixture.reserve).not.toHaveBeenCalled();
	});

	test("persisted intent resumes its original entry and rechecks authority without another reservation", async () => {
		const fixture = await recoveryFixture();
		fixture.entries.push({ id: "persisted-entry", type: "custom_message", customType: "work-execute", details: { executionContinuation: fixture.intent } });
		await fixture.start();
		expect(fixture.continuations).toHaveLength(1);
		expect(fixture.continuations[0]).toMatchObject({ sessionId: fixture.intent.sessionId, entryId: "persisted-entry", expectedLeafId: "persisted-entry" });
		expect(fixture.sent.filter(message => message.customType === "work-execute")).toHaveLength(0);
		expect(fixture.reserve).not.toHaveBeenCalled();
		expect(await fixture.continuations[0].validateDispatch()).toEqual({ ok: true });
		fixture.exec.grant.state = "canceled";
		expect(await fixture.continuations[0].validateDispatch()).toMatchObject({ ok: false });
	});

	test("original task authority requires the actual agent execution prompt and revalidates before processing",async()=>{
		const f = await recoveryFixture();
		const prompt: InjectedEntry = {id:"original-prompt",type:"custom_message",customType:"work-execute",attribution:"agent",details:{executionContinuation:f.intent}};
		f.entries.push(prompt,{id:"original-assistant",type:"message",message:{role:"assistant",content:[{type:"toolCall",id:"original-task",name:"task",arguments:{}}]}});
		expect(await f.taskCall()).toBeUndefined();
		prompt.attribution = "user";
		expect(await f.taskCall({sessionId:f.intent.sessionId,promptEntryId:"original-prompt"})).toBeUndefined();
		prompt.attribution = "agent";
		const result = await f.taskCall({sessionId:f.intent.sessionId,promptEntryId:"original-prompt"});
		if(!result?.taskResultAuthority) throw new Error("Matching execution prompt did not provide an authority validator");
		const actual = {sessionId:f.intent.sessionId,promptEntryId:"original-prompt",assistantEntryId:"original-assistant",toolCallId:"original-task"};
		expect(await result.taskResultAuthority(actual)).toEqual({ok:true});
		expect(await result.taskResultAuthority({...actual,assistantEntryId:"foreign-assistant"})).toMatchObject({ok:false});
		f.exec.grant.state = "canceled";
		expect(await result.taskResultAuthority(actual)).toMatchObject({ok:false});
		expect(f.reserve).not.toHaveBeenCalled();
	});

	test("original task authority refuses an unreadable pending execution journal",async()=>{
		const f = await recoveryFixture();
		f.entries.push({id:"original-prompt",type:"custom_message",customType:"work-execute",attribution:"agent",details:{executionContinuation:f.intent}},
			{id:"original-assistant",type:"message",message:{role:"assistant",content:[{type:"toolCall",id:"original-task",name:"task",arguments:{}}]}});
		const result = await f.taskCall({sessionId:f.intent.sessionId,promptEntryId:"original-prompt"});
		if(!result?.taskResultAuthority) throw new Error("Authority validator unavailable");
		f.backend.getPendingExecutionClaims = async()=>{throw new Error("Pending journal unreadable");};
		expect(await result.taskResultAuthority({sessionId:f.intent.sessionId,promptEntryId:"original-prompt",assistantEntryId:"original-assistant",toolCallId:"original-task"})).toMatchObject({ok:false});
		expect(f.reserve).not.toHaveBeenCalled();
	});

	test("duplicate active-branch continuation identity refuses instead of resuming either copy", async () => {
		const fixture = await recoveryFixture();
		for (const id of ["first-entry", "second-entry"]) fixture.entries.push({ id, type: "custom_message", customType: "work-execute", details: { executionContinuation: fixture.intent } });
		await fixture.start();
		expect(fixture.continuations).toHaveLength(0);
		expect(fixture.sent.filter(message => message.customType === "work-execute")).toHaveLength(0);
		expect(fixture.reserve).not.toHaveBeenCalled();
		expect(fixture.notices.some(notice => notice.includes("multiple active-branch messages"))).toBe(true);
	});

	test("a different item's persisted message cannot suppress the current queued intent", async () => {
		const fixture = await recoveryFixture();
		fixture.entries.push({ type: "custom_message", customType: "work-execute", details: { executionContinuation: { ...fixture.intent, workId: "other-item" } } });
		await fixture.start();
		const deliveries = fixture.sent.filter(message => message.customType === "work-execute");
		expect(deliveries).toHaveLength(1);
		expect(deliveries[0]?.details?.executionContinuation?.messageId).toBe(fixture.intent.messageId);
		expect(fixture.reserve).not.toHaveBeenCalled();
	});

	test.each([
		["another grant", { execution_grant_id: "foreign-grant" }],
		["another revision", { revision_id: "foreign-revision" }],
		["another candidate", { candidate_id: "foreign-candidate" }],
		["changed candidate bytes", { candidate_sha256: "f".repeat(64) }],
		["another commit", { candidate_commit: "e".repeat(40) }],
		["terminal attempt", { state: "completed" }],
	] as const)("an executing candidate bound to %s cannot authorize recovery at its HEAD", async (_name, changed) => {
		const fixture = await recoveryFixture();
		const commit = "2".repeat(40);
		fixture.item.candidate = {
			candidate_id: "candidate-recovery", work_id: fixture.item.work_id, revision_id: fixture.item.revision.revision_id,
			candidate_sha256: "c".repeat(64), commit_sha: commit, kind: "final", allocated_at: new Date().toISOString(),
		};
		fixture.setHead(commit);
		fixture.attempts.push({
			attempt_id: "attempt-recovery", work_id: fixture.item.work_id, execution_grant_id: fixture.exec.grant.grant_id,
			authorization_kind: "execution", revision_id: fixture.item.revision.revision_id, candidate_id: fixture.item.candidate.candidate_id,
			candidate_sha256: fixture.item.candidate.candidate_sha256, candidate_commit: commit, state: "active", ...changed,
		});
		await fixture.start();
		expect(fixture.sent.filter(message => message.customType === "work-execute")).toHaveLength(0);
		expect(fixture.reserve).not.toHaveBeenCalled();
		expect(fixture.notices.some(notice => notice.includes("HEAD commit mismatch"))).toBe(true);
	});
});

const makeSnapshot = (
	state: "stopped" | "canceled" | "active" | "paused" | "completed",
	mode: "single" | "queue" = "queue",
	items: Array<{ position: number; work_id: string; phase: ExecutionItemPhase; completed_at?: string }> = [
		{ position: 0, work_id: "OMP-176", phase: "executing" },
		{ position: 1, work_id: "OMP-180", phase: "pending" },
		{ position: 2, work_id: "OMP-181", phase: "pending" },
	],
	terminal_reason: string | null = null,
): ExecutionSnapshot => {
	const mappedItems = items.map(it => ({
		item_id: `item-${it.position}`,
		workspace_id: "ws-1",
		grant_id: "ad5c45a7-1234-5678-9abc-def012345678",
		work_id: it.work_id,
		position: it.position,
		phase: it.phase,
		claimed_revision_id: "rev-1",
		initial_git_baseline: "commit-0",
		original_request: "req",
		original_request_sha256: "req-sha",
		close_attempts_started: 0,
		consecutive_no_progress: 0,
		completed_at: it.completed_at,
		...it,
	}));
	return {
		grant: {
			grant_id: "ad5c45a7-1234-5678-9abc-def012345678",
			workspace_id: "ws-1",
			owner_id: "owner-1",
			repository: "/tmp/oh-my-pi",
			remote_ref: "refs/heads/main",
			state,
			mode,
			grant_version: 1,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 0,
			terminal_reason,
			authorization_hash: "auth-hash",
			judge_sha256: "judge-sha",
			created_at: new Date().toISOString(),
			expires_at: new Date().toISOString(),
		},
		items: mappedItems,
		activeItem: mappedItems.find(it => it.phase === "executing") ?? null,
	};
};
afterEach(() => {
	vi.restoreAllMocks();
	for (const dir of fixtureCaches.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

const identityExecutionWorkspaceManager = {
	primaryRoot: async (cwd: string) => cwd,
	ensure: async (cwd: string, key: string, grantId: string, baseline: string) => ({
		primaryRoot: cwd,
		path: cwd,
		branch: `execution/${key.toLowerCase()}`,
		grantId,
		baseline,
		reused: false,
	}),
	cleanup: async () => ({ cleaned: true, detail: "identity cleanup" }),
};

describe("extension session relocation (OMP-213)", () => {
	test("setup moves SessionManager before refreshing cwd-derived TUI state", async () => {
		const source = fs.mkdtempSync(path.join(os.tmpdir(), "omp-213-source-"));
		const target = fs.mkdtempSync(path.join(os.tmpdir(), "omp-213-target-"));
		try {
			const sessionManager = SessionManager.inMemory(source);
			const applied: string[] = [];
			await applyExtensionNewSessionSetup(
				{
					sessionManager,
					applyCwdChange: async cwd => {
						expect(sessionManager.getCwd()).toBe(cwd);
						applied.push(cwd);
					},
				} as never,
				{ setup: manager => manager.moveTo(target) },
			);
			expect(sessionManager.getCwd()).toBe(path.resolve(target));
			expect(applied).toEqual([path.resolve(target)]);
		} finally {
			fs.rmSync(source, { recursive: true, force: true });
			fs.rmSync(target, { recursive: true, force: true });
		}
	});
});

describe("native auditor runner (OMP-168)", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	function nativeStageModel(overrides: Partial<Model>): Model {
		return {
			id: "unknown",
			name: "native stage test",
			api: "openai-completions",
			provider: "kimi-code",
			baseUrl: "https://example.invalid",
			reasoning: true,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: 1000,
			maxTokens: 100,
			compat: {} as Model["compat"],
			thinking: { mode: "effort", efforts: ["high"] },
			...overrides,
		} as Model;
	}

	afterEach(() => {
		vi.restoreAllMocks();
	});

	test("prepareNativeAuditRunner fails if @audit role cannot be resolved", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const emptyQuery = createExtensionModelQuery(
			{ getAvailable: () => [] as Model<Api>[] } as unknown as ModelRegistry,
			Settings.isolated({}),
			() => undefined,
		);
		const fakeCtx = {
			cwd: repoRoot,
			models: emptyQuery,
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("@audit");
	});

	test("native stage defaults to installed role name and pins retry settings to allowlisted route", async () => {
		const implementer: AgentDefinition = {
			name: "implementer",
			description: "Implementer agent",
			systemPrompt: "Implement prompt",
			model: ["google-antigravity/gemini-3.8-flash:high"],
			tools: ["read", "grep", "glob", "lsp", "write"],
			output: { properties: { verification_body: { type: "string" } } },
			source: "bundled",
		};
		mockDiscovery(implementer);
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const gemini = nativeStageModel({
			id: "gemini-3.8-flash",
			provider: "google-antigravity",
			api: "google-gemini-cli",
			thinking: { mode: "google-level", efforts: ["low", "medium", "high"], effortRouting: { high: "gemini-3.8-flash-high" } },
		});
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-implement", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: JSON.stringify({ verification_body: "checked" }), stderr: "", truncated: false, durationMs: 1, tokens: 2, requests: 1,
			resolvedModel: "google-antigravity/gemini-3.8-flash:high",
		} as executorModule.SingleResult);
		const registry = { getApiKey: vi.fn().mockResolvedValue("gemini-token") };
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/gemini-3.8-flash") ? gemini : undefined },
			modelRegistry: registry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement", context: "sealed-context", writeRoots: [repoRoot] });
		await runner("edit", "native-implement");
		const options = runSubprocessSpy.mock.calls[0]?.[0];
		expect(options.agent.name).toBe("implementer");
		expect(options.modelOverride).toBe("google-antigravity/gemini-3.8-flash:high");
		expect(options.modelRole).toBe("implement");
		expect(options.resolvedModel).toBeUndefined();
		expect(options.thinkingLevel).toBeUndefined();
		expect(options.context).toBeUndefined();
		expect(options.task).toBe("edit\n\n<stage_context_data>\nsealed-context\n</stage_context_data>");
		expect(options.nativeStageWriteRoots).toEqual([repoRoot]);
		expect(options.parentActiveModelPattern).toBeUndefined();
		expect(options.settings.get("retry.modelFallback")).toBe(false);
		for (const chain of Object.values(options.settings.get("retry.fallbackChains"))) expect(chain).toEqual([]);
	});

	test("native implement preflight selects Luna when Gemini credentials are unavailable", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-fallback", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: "{}", stderr: "", truncated: false, durationMs: 1, tokens: 2, requests: 1,
			resolvedModel: "openai-codex/gpt-5.6-luna:high", resolvedModelIsFallback: true,
		} as executorModule.SingleResult);
		const registry = { getApiKey: vi.fn((model: Model) => Promise.resolve(model.provider === "openai-codex" ? "luna-token" : undefined)) };
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/") ? gemini : selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: registry, taskDepth: 0,
		} as unknown as ExtensionContext;

		const attempts: NativeStagePreflightAttempt[] = [];
		const runner = await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				attempts.push(attempt);
			},
		});
		await runner("edit", "native-fallback");
		expect(runSubprocessSpy.mock.calls[0]?.[0].modelOverride).toBe("openai-codex/gpt-5.6-luna:high");
		expect(registry.getApiKey).toHaveBeenCalledTimes(2);
		expect(attempts).toHaveLength(1);
		expect(attempts[0].ordinal).toBe(0);
		expect(attempts[0].route.model.provider).toBe("openai-codex");
		expect(attempts[0].outcome).toBe("selected");
	});

	test("native implement preflight falls back after primary transport failure", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		const probeSpy = vi.spyOn(ai, "completeSimple").mockRejectedValueOnce(new Error("503 no capacity")).mockResolvedValueOnce({ stopReason: "stop", content: [{ type: "text", text: "OK" }] } as never);
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-probe-fallback", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: "{}", stderr: "", truncated: false, durationMs: 1, tokens: 2, requests: 1,
			resolvedModel: "openai-codex/gpt-5.6-luna:high", resolvedModelIsFallback: true,
		} as executorModule.SingleResult);
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/") ? gemini : selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement" });
		await runner("edit", "native-probe-fallback");
		expect(probeSpy).toHaveBeenCalledTimes(2);
		expect(runSubprocessSpy.mock.calls[0]?.[0].modelOverride).toBe("openai-codex/gpt-5.6-luna:high");
	});

	test("native stage rejects cancellation before trying another route", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const controller = new AbortController();
		controller.abort();
		const registry = { getApiKey: vi.fn() };
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => undefined },
			modelRegistry: registry, taskDepth: 0,
		} as unknown as ExtensionContext;
		const attempts: NativeStagePreflightAttempt[] = [];
		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				attempts.push(attempt);
			},
		}, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
		expect(registry.getApiKey).not.toHaveBeenCalled();
		expect(attempts).toHaveLength(0);
	});

	test("native transport preflight records ordered distinct attempt UUIDs and awaits callback before selection or execution", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		vi.spyOn(ai, "completeSimple")
			.mockRejectedValueOnce(new Error("503 no capacity"))
			.mockResolvedValueOnce({
				stopReason: "stop",
				content: [{ type: "text", text: "OK" }],
				responseId: "resp-2",
				usage: {
					input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12,
					cost: { input: 0.001, output: 0.001, cacheRead: 0, cacheWrite: 0, total: 0.002 },
				},
			} as never);
		const order: string[] = [];
		const attempts: NativeStagePreflightAttempt[] = [];
		let selectedRoute: NativeStageRoute | undefined;
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async () => {
			order.push("subprocess");
			return {
				index: 0, id: "test-run", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
				output: "{}", stderr: "", truncated: false, durationMs: 1, tokens: 2, requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-luna:high", resolvedModelIsFallback: true,
			} as executorModule.SingleResult;
		});
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/") ? gemini : selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				order.push(`attempt:${attempt.ordinal}`);
				attempts.push(attempt);
			},
			onRouteSelected: route => {
				order.push("routeSelected");
				selectedRoute = route;
			},
		});
		await runner("edit", "test-run");

		expect(order).toEqual(["attempt:0", "attempt:1", "routeSelected", "subprocess"]);
		expect(attempts).toHaveLength(2);
		const [att0, att1] = attempts;
		expect(att0.ordinal).toBe(0);
		expect(att0.outcome).toBe("failed");
		expect(att0.stopReason).toBeNull();
		expect(att0.error).toContain("503 no capacity");
		expect(att0.requests).toBeNull();
		expect(att0.usage).toBeNull();
		expect(att0.providerRequestId).toBeNull();

		expect(att1.ordinal).toBe(1);
		expect(att1.outcome).toBe("selected");
		expect(att1.stopReason).toBe("stop");
		expect(att1.error).toBeNull();
		expect(att1.requests).toBeNull();
		expect(att1.providerRequestId).toBe("resp-2");
		expect(att1.usage).toEqual({ input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12 });
		expect("cost" in (att1.usage as Record<string, unknown>)).toBe(false);

		const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
		expect(att0.transportAttemptId).toMatch(uuidPattern);
		expect(att1.transportAttemptId).toMatch(uuidPattern);
		expect(att0.transportAttemptId).not.toBe(att1.transportAttemptId);
		expect(selectedRoute?.model.id).toBe("gpt-5.6-luna");
	});

	test("in-band preflight error preserves returned cost-free usage including zero, while thrown failure has null usage", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });

		vi.spyOn(ai, "completeSimple")
			.mockResolvedValueOnce({
				stopReason: "error",
				errorMessage: "rate limit exceeded",
				responseId: "resp-err",
				usage: {
					input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
					cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
				},
			} as never)
			.mockRejectedValueOnce(new Error("socket hang up"));

		const attempts: NativeStagePreflightAttempt[] = [];
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/") ? gemini : selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				attempts.push(attempt);
			},
		})).rejects.toThrow("socket hang up");

		expect(attempts).toHaveLength(2);
		const [errAtt, throwAtt] = attempts;
		expect(errAtt.outcome).toBe("failed");
		expect(errAtt.stopReason).toBe("error");
		expect(errAtt.error).toContain("rate limit exceeded");
		expect(errAtt.usage).toEqual({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 });
		expect("cost" in (errAtt.usage as Record<string, unknown>)).toBe(false);
		expect(errAtt.providerRequestId).toBe("resp-err");
		expect(errAtt.requests).toBeNull();

		expect(throwAtt.outcome).toBe("failed");
		expect(throwAtt.stopReason).toBeNull();
		expect(throwAtt.error).toContain("socket hang up");
		expect(throwAtt.usage).toBeNull();
		expect(throwAtt.providerRequestId).toBeNull();
		expect(throwAtt.requests).toBeNull();
	});

	test("abort after send records cancelled preflight attempt before rejection", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const controller = new AbortController();
		vi.spyOn(ai, "completeSimple").mockImplementation(async () => {
			controller.abort();
			throw new DOMException("The operation was aborted", "AbortError");
		});

		const attempts: NativeStagePreflightAttempt[] = [];
		let routeSelected = false;
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				attempts.push(attempt);
			},
			onRouteSelected: () => {
				routeSelected = true;
			},
		}, controller.signal)).rejects.toMatchObject({ name: "AbortError" });

		expect(attempts).toHaveLength(1);
		expect(attempts[0].outcome).toBe("cancelled");
		expect(attempts[0].stopReason).toBeNull();
		expect(attempts[0].error).toBe("native implement transport preflight cancelled for google-antigravity/gemini-3.8-flash");
		expect(attempts[0].requests).toBeNull();
		expect(attempts[0].usage).toBeNull();
		expect(routeSelected).toBe(false);
	});

	test("record callback failure prevents route selection and subprocess", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "OK" }],
		} as never);
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess");
		let routeSelected = false;
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: async () => {
				throw new Error("WorkService unavailable");
			},
			onRouteSelected: () => {
				routeSelected = true;
			},
		})).rejects.toThrow("WorkService unavailable");

		expect(routeSelected).toBe(false);
		expect(runSubprocessSpy).not.toHaveBeenCalled();
	});

	test("probe hash matches exact sent content", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		let sentContent = "";
		vi.spyOn(ai, "completeSimple").mockImplementation(async (_model, context) => {
			sentContent = context.messages[0]?.content as string;
			return { stopReason: "stop", content: [{ type: "text", text: "OK" }] } as never;
		});
		let attemptRecorded: NativeStagePreflightAttempt | undefined;
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			onPreflightAttempt: attempt => {
				attemptRecorded = attempt;
			},
		});

		expect(sentContent).toBe("Transport preflight. Reply with the single word OK.");
		expect(attemptRecorded?.probeSha256).toBe("968ec1efc411d085cb287574524382c932fb0a435154bb15fa175aaf1e471617");
		expect(attemptRecorded?.probeSha256).toBe(sha256Hex(sentContent));
	});

	test("preflight executes in order: begin -> admit -> provider probe -> record with returned transport attempt ID", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const events: string[] = [];
		vi.spyOn(ai, "completeSimple").mockImplementation(async () => {
			events.push("probe");
			return { stopReason: "stop", content: [{ type: "text", text: "OK" }] } as never;
		});
		let recordedAttempt: NativeStagePreflightAttempt | undefined;
		const serviceTransportAttemptId = "55555555-5555-4555-8555-555555555555";

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					events.push("begin");
					return {
						type: "begin_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: serviceTransportAttemptId,
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: null,
							dispatch_operation_id: null,
							dispatch_owner_id: null,
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "begun",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				admit: async ({ transportAttemptId, logicalSha256 }) => {
					events.push("admit");
					return {
						type: "admit_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: "0".repeat(64),
							transport_attempt_id: transportAttemptId,
							ordinal: 0,
							requested_selector: "gemini:gemini-3.8-flash",
							requested_provider: "google-antigravity",
							requested_model: "gemini-3.8-flash",
							requested_api: "google-gemini-cli",
							requested_effort: null,
							requested_wire_model: "gemini-3.8-flash",
							is_fallback: false,
							logical_sha256: logicalSha256,
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: new Date().toISOString(),
							dispatch_operation_id: "op-1",
							dispatch_owner_id: "owner-1",
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "dispatched",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				cancel: async () => {
					throw new Error("cancel should not be called in happy path");
				},
				record: async attempt => {
					events.push("record");
					recordedAttempt = attempt;
				},
			},
		});

		expect(events).toEqual(["begin", "admit", "probe", "record"]);
		expect(recordedAttempt?.transportAttemptId).toBe(serviceTransportAttemptId);
		expect(recordedAttempt?.outcome).toBe("selected");
	});

	test("replayed settled preflight skips provider probe and produces correct route result", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const probeSpy = vi.spyOn(ai, "completeSimple");
		const events: string[] = [];
		let selectedRoute: NativeStageRoute | undefined;

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					events.push("begin");
					return {
						type: "begin_stage_preflight",
						status: "replayed",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: "77777777-7777-4777-8777-777777777777",
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							status: "settled",
							created_at: new Date().toISOString(),
							settled_at: new Date().toISOString(),
						},
						preflight: {
							preflight_id: "preflight-existing",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							session_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: "77777777-7777-4777-8777-777777777777",
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							outcome: "selected",
							stop_reason: "stop",
							error: null,
							requests: null,
							usage: null,
							provider_request_id: "prev-req-1",
							observed_at: new Date().toISOString(),
						},
					};
				},
				admit: async () => {
					throw new Error("admit should not be called on settled replay");
				},
				cancel: async () => {
					throw new Error("cancel should not be called on settled replay");
				},
				record: async () => {
					events.push("record");
				},
			},
			onRouteSelected: route => {
				selectedRoute = route;
			},
		});

		expect(events).toEqual(["begin"]);
		expect(probeSpy).not.toHaveBeenCalled();
		expect(selectedRoute).toBeDefined();
		expect(selectedRoute?.model.id).toBe("gemini-3.8-flash");
	});

	test("cancel-and-reissue on replayed begun produces one probe with second transport ID and next ordinal", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const probeSpy = vi.spyOn(ai, "completeSimple").mockImplementation(async () => {
			return { stopReason: "stop", content: [{ type: "text", text: "OK" }] } as never;
		});

		const firstTransportAttemptId = "11111111-1111-4111-8111-111111111111";
		const secondTransportAttemptId = "22222222-2222-4222-8222-222222222222";
		const cancelledIds: string[] = [];
		const begunOrdinals: number[] = [];
		let admittedTransportId: string | undefined;
		let recordedAttempt: NativeStagePreflightAttempt | undefined;

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					begunOrdinals.push(ordinal);
					if (ordinal === 0) {
						return {
							type: "begin_stage_preflight",
							status: "replayed",
							intent: {
								intent_id: "00000000-0000-4000-8000-000000000001",
								workspace_id: "ws-1",
								work_id: "work-1",
								revision_id: "rev-1",
								candidate_id: null,
								attempt_id: null,
								grant_id: null,
								role: "implement",
								tool_call_id: "call-1",
								task_sha256: "0".repeat(64),
								probe_sha256: probeSha256,
								transport_attempt_id: firstTransportAttemptId,
								ordinal: 0,
								requested_selector: route.requestedSelector,
								requested_provider: route.model.provider,
								requested_model: route.model.id,
								requested_api: route.model.api,
								requested_effort: route.effort ?? null,
								requested_wire_model: route.model.id,
								is_fallback: route.isFallback,
								logical_sha256: "0".repeat(64),
								group_sha256: "0".repeat(64),
								host_owner_id: "owner-1",
								dispatched_at: null,
								dispatch_operation_id: null,
								dispatch_owner_id: null,
								cancelled_at: null,
								cancelled_by: null,
								cancel_reason: null,
								status: "begun",
								created_at: new Date().toISOString(),
								settled_at: null,
							},
							preflight: undefined,
						};
					}
					return {
						type: "begin_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000002",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: secondTransportAttemptId,
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: null,
							dispatch_operation_id: null,
							dispatch_owner_id: null,
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "begun",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				cancel: async ({ transportAttemptId, logicalSha256, reason }) => {
					cancelledIds.push(transportAttemptId);
					return {
						type: "cancel_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: "0".repeat(64),
							transport_attempt_id: transportAttemptId,
							ordinal: 0,
							requested_selector: "gemini:gemini-3.8-flash",
							requested_provider: "google-antigravity",
							requested_model: "gemini-3.8-flash",
							requested_api: "google-gemini-cli",
							requested_effort: null,
							requested_wire_model: "gemini-3.8-flash",
							is_fallback: false,
							logical_sha256: logicalSha256,
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: null,
							dispatch_operation_id: null,
							dispatch_owner_id: null,
							cancelled_at: new Date().toISOString(),
							cancelled_by: "owner-1",
							cancel_reason: reason,
							status: "cancelled_undispatched",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				admit: async ({ transportAttemptId, logicalSha256 }) => {
					admittedTransportId = transportAttemptId;
					return {
						type: "admit_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000002",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: "0".repeat(64),
							transport_attempt_id: transportAttemptId,
							ordinal: 1,
							requested_selector: "gemini:gemini-3.8-flash",
							requested_provider: "google-antigravity",
							requested_model: "gemini-3.8-flash",
							requested_api: "google-gemini-cli",
							requested_effort: null,
							requested_wire_model: "gemini-3.8-flash",
							is_fallback: false,
							logical_sha256: logicalSha256,
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: new Date().toISOString(),
							dispatch_operation_id: "op-2",
							dispatch_owner_id: "owner-1",
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "dispatched",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				record: async attempt => {
					recordedAttempt = attempt;
				},
			},
		});

		expect(cancelledIds).toEqual([firstTransportAttemptId]);
		expect(begunOrdinals).toEqual([0, 1]);
		expect(admittedTransportId).toBe(secondTransportAttemptId);
		expect(probeSpy).toHaveBeenCalledTimes(1);
		expect(recordedAttempt?.transportAttemptId).toBe(secondTransportAttemptId);
		expect(recordedAttempt?.ordinal).toBe(1);
	});

	test("replayed dispatched preflight is blocked with uncertainty error and sends zero probes", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const probeSpy = vi.spyOn(ai, "completeSimple");

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					return {
						type: "begin_stage_preflight",
						status: "replayed",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: "88888888-8888-4888-8888-888888888888",
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: new Date().toISOString(),
							dispatch_operation_id: "op-1",
							dispatch_owner_id: "owner-1",
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "dispatched",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				admit: async () => { throw new Error("should not be called"); },
				cancel: async () => { throw new Error("should not be called"); },
				record: async () => {},
			},
		})).rejects.toThrow(/provider effect uncertain, trusted provider reconciliation required/);

		expect(probeSpy).not.toHaveBeenCalled();
	});

	test("cancel-throws and admit-throws fail closed and send zero probes", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const probeSpy = vi.spyOn(ai, "completeSimple");

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		// Subcase 1: cancel throws during begun replay recovery
		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					return {
						type: "begin_stage_preflight",
						status: "replayed",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000001",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: "88888888-8888-4888-8888-888888888888",
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: null,
							dispatch_operation_id: null,
							dispatch_owner_id: null,
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "begun",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				cancel: async () => {
					throw new Error("network partition during cancel");
				},
				admit: async () => { throw new Error("should not be reached"); },
				record: async () => {},
			},
		})).rejects.toThrow("network partition during cancel");

		expect(probeSpy).not.toHaveBeenCalled();

		// Subcase 2: admit throws during regular dispatch admission
		await expect(prepareNativeStageRunner(fakeCtx, {
			role: "implement",
			preflight: {
				begin: async ({ route, ordinal, probeSha256 }) => {
					return {
						type: "begin_stage_preflight",
						status: "applied",
						intent: {
							intent_id: "00000000-0000-4000-8000-000000000002",
							workspace_id: "ws-1",
							work_id: "work-1",
							revision_id: "rev-1",
							candidate_id: null,
							attempt_id: null,
							grant_id: null,
							role: "implement",
							tool_call_id: "call-1",
							task_sha256: "0".repeat(64),
							probe_sha256: probeSha256,
							transport_attempt_id: "99999999-9999-4999-8999-999999999999",
							ordinal,
							requested_selector: route.requestedSelector,
							requested_provider: route.model.provider,
							requested_model: route.model.id,
							requested_api: route.model.api,
							requested_effort: route.effort ?? null,
							requested_wire_model: route.model.id,
							is_fallback: route.isFallback,
							logical_sha256: "0".repeat(64),
							group_sha256: "0".repeat(64),
							host_owner_id: "owner-1",
							dispatched_at: null,
							dispatch_operation_id: null,
							dispatch_owner_id: null,
							cancelled_at: null,
							cancelled_by: null,
							cancel_reason: null,
							status: "begun",
							created_at: new Date().toISOString(),
							settled_at: null,
						},
						preflight: undefined,
					};
				},
				cancel: async () => { throw new Error("should not be reached"); },
				admit: async () => {
					throw new Error("admit RPC failed with 503");
				},
				record: async () => {},
			},
		})).rejects.toThrow("admit RPC failed with 503");

		expect(probeSpy).not.toHaveBeenCalled();
	});

	test("abort after begin attempts cancel and throws AbortError", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const probeSpy = vi.spyOn(ai, "completeSimple");

		const controller = new AbortController();
		let cancelCalled = false;
		let cancelledAttemptId: string | undefined;

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => gemini },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") }, taskDepth: 0,
		} as unknown as ExtensionContext;

		const transportAttemptId = "33333333-3333-4333-8333-333333333333";

		await expect(prepareNativeStageRunner(
			fakeCtx,
			{
				role: "implement",
				preflight: {
					begin: async ({ route, ordinal, probeSha256 }) => {
						controller.abort();
						return {
							type: "begin_stage_preflight",
							status: "applied",
							intent: {
								intent_id: "00000000-0000-4000-8000-000000000001",
								workspace_id: "ws-1",
								work_id: "work-1",
								revision_id: "rev-1",
								candidate_id: null,
								attempt_id: null,
								grant_id: null,
								role: "implement",
								tool_call_id: "call-1",
								task_sha256: "0".repeat(64),
								probe_sha256: probeSha256,
								transport_attempt_id: transportAttemptId,
								ordinal,
								requested_selector: route.requestedSelector,
								requested_provider: route.model.provider,
								requested_model: route.model.id,
								requested_api: route.model.api,
								requested_effort: route.effort ?? null,
								requested_wire_model: route.model.id,
								is_fallback: route.isFallback,
								logical_sha256: "0".repeat(64),
								group_sha256: "0".repeat(64),
								host_owner_id: "owner-1",
								dispatched_at: null,
								dispatch_operation_id: null,
								dispatch_owner_id: null,
								cancelled_at: null,
								cancelled_by: null,
								cancel_reason: null,
								status: "begun",
								created_at: new Date().toISOString(),
								settled_at: null,
							},
							preflight: undefined,
						};
					},
					cancel: async ({ transportAttemptId }) => {
						cancelCalled = true;
						cancelledAttemptId = transportAttemptId;
						return {
							type: "cancel_stage_preflight",
							status: "applied",
							intent: {
								intent_id: "00000000-0000-4000-8000-000000000001",
								workspace_id: "ws-1",
								work_id: "work-1",
								revision_id: "rev-1",
								candidate_id: null,
								attempt_id: null,
								grant_id: null,
								role: "implement",
								tool_call_id: "call-1",
								task_sha256: "0".repeat(64),
								probe_sha256: "0".repeat(64),
								transport_attempt_id: transportAttemptId,
								ordinal: 0,
								requested_selector: "gemini:gemini-3.8-flash",
								requested_provider: "google-antigravity",
								requested_model: "gemini-3.8-flash",
								requested_api: "google-gemini-cli",
								requested_effort: null,
								requested_wire_model: "gemini-3.8-flash",
								is_fallback: false,
								logical_sha256: "0".repeat(64),
								group_sha256: "0".repeat(64),
								host_owner_id: "owner-1",
								dispatched_at: null,
								dispatch_operation_id: null,
								dispatch_owner_id: null,
								cancelled_at: new Date().toISOString(),
								cancelled_by: "owner-1",
								cancel_reason: "aborted before preflight dispatch admission",
								status: "cancelled_undispatched",
								created_at: new Date().toISOString(),
								settled_at: null,
							},
							preflight: undefined,
						};
					},
					admit: async () => { throw new Error("admit should not be called when aborted"); },
					record: async () => {},
				},
			},
			controller.signal,
		)).rejects.toThrow(/operation was aborted|AbortError/);

		expect(cancelCalled).toBe(true);
		expect(cancelledAttemptId).toBe(transportAttemptId);
		expect(probeSpy).not.toHaveBeenCalled();
	});

	test("partial preflight callback set fails upfront before discovery or begin, making zero begin or provider calls", async () => {
		const probeSpy = vi.spyOn(ai, "completeSimple");
		const beginSpy = vi.fn();
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => ({ id: "m", provider: "p", api: "a" }) },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("token") },
			taskDepth: 0,
		} as unknown as ExtensionContext;

		// Missing admit and cancel
		await expect(
			prepareNativeStageRunner(fakeCtx, {
				role: "implement",
				preflight: {
					begin: beginSpy,
					record: async () => {},
				} as any,
			}),
		).rejects.toThrow("preflight option requires begin, admit, cancel, and record callbacks");

		// Missing cancel
		await expect(
			prepareNativeStageRunner(fakeCtx, {
				role: "implement",
				preflight: {
					begin: beginSpy,
					admit: async () => ({} as any),
					record: async () => {},
				} as any,
			}),
		).rejects.toThrow("preflight option requires begin, admit, cancel, and record callbacks");

		// Missing admit
		await expect(
			prepareNativeStageRunner(fakeCtx, {
				role: "implement",
				preflight: {
					begin: beginSpy,
					cancel: async () => ({} as any),
					record: async () => {},
				} as any,
			}),
		).rejects.toThrow("preflight option requires begin, admit, cancel, and record callbacks");

		// Missing record
		await expect(
			prepareNativeStageRunner(fakeCtx, {
				role: "implement",
				preflight: {
					begin: beginSpy,
					admit: async () => ({} as any),
					cancel: async () => ({} as any),
				} as any,
			}),
		).rejects.toThrow("preflight option requires begin, admit, cancel, and record callbacks");

		expect(beginSpy).not.toHaveBeenCalled();
		expect(probeSpy).not.toHaveBeenCalled();
	});

	test("native stage refuses a subprocess that reports an unallowlisted served model", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-disallowed", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: "{}", stderr: "", truncated: false, durationMs: 1, tokens: 2, requests: 1,
			resolvedModel: "openai-codex/gpt-5.6-sol:high",
		} as executorModule.SingleResult);
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("luna-token") }, taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement" });
		const result = await runner("edit", "native-disallowed");
		expect(runSubprocessSpy).toHaveBeenCalledTimes(1);
		expect(result.error).toMatch(/served disallowed model/);
		expect(result.payload).toBeUndefined();
	});

	test("runner returns all measured usage buckets and request count while omitting cost", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const gemini = nativeStageModel({ id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"], effortRouting: { high: "gemini-3.8-flash-high" } } });
		const measuredUsage: Usage = {
			input: 120,
			output: 45,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 180,
			contextTokens: 250,
			orchestration: { input: 15, cacheRead: 5, output: 10 },
			premiumRequests: 1,
			reasoningTokens: 20,
			cttl: { ephemeral5m: 5 },
			server: { webSearch: 2, webFetch: 1 },
			cost: { input: 0.001, output: 0.002, cacheRead: 0.0001, cacheWrite: 0.0002, total: 0.0033 },
		};
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-usage", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: JSON.stringify({ verification_body: "done" }), stderr: "", truncated: false, durationMs: 10, tokens: 180, requests: 3,
			resolvedModel: "google-antigravity/gemini-3.8-flash:high",
			usage: measuredUsage,
		} as executorModule.SingleResult);
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("google-antigravity/") ? gemini : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("gemini-token") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement" });
		const result = await runner("edit", "native-usage");
		expect(runSubprocessSpy).toHaveBeenCalledTimes(1);
		expect(result.started).toBe(true);
		expect(result.requests).toBe(3);
		expect(result.usage).toEqual({
			input: 120,
			output: 45,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 180,
			contextTokens: 250,
			orchestration: { input: 15, cacheRead: 5, output: 10 },
			premiumRequests: 1,
			reasoningTokens: 20,
			cttl: { ephemeral5m: 5 },
			server: { webSearch: 2, webFetch: 1 },
		});
		expect("cost" in (result.usage as Record<string, unknown>)).toBe(false);
		expect(measuredUsage.cost.total).toBe(0.0033);
	});

	test("disallowed served-model return retains measured usage and request count while omitting cost", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		const measuredUsage: Usage = {
			input: 80,
			output: 30,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 110,
			cost: { input: 0.0008, output: 0.0012, cacheRead: 0, cacheWrite: 0, total: 0.002 },
		};
		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0, id: "native-disallowed-usage", agent: "implementer", agentSource: "bundled", task: "edit", exitCode: 0,
			output: "{}", stderr: "", truncated: false, durationMs: 1, tokens: 110, requests: 2,
			resolvedModel: "openai-codex/gpt-5.6-sol:high",
			usage: measuredUsage,
		} as executorModule.SingleResult);
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("luna-token") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement" });
		const result = await runner("edit", "native-disallowed-usage");
		expect(result.error).toMatch(/served disallowed model/);
		expect(result.payload).toBeUndefined();
		expect(result.requests).toBe(2);
		expect(result.usage).toEqual({
			input: 80,
			output: 30,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 110,
		});
		expect("cost" in (result.usage as Record<string, unknown>)).toBe(false);
	});

	test("runner catch-before-result keeps usage and requests absent", async () => {
		const implementer: AgentDefinition = {
			name: "implementer", description: "Implementer", systemPrompt: "Implement", model: ["@implement"],
			output: { properties: { verification_body: { type: "string" } } }, source: "bundled",
		};
		mockDiscovery(implementer);
		const luna = nativeStageModel({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
		vi.spyOn(executorModule, "runSubprocess").mockRejectedValue(new Error("process spawn error"));
		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: (selector: string) => selector.startsWith("openai-codex/") ? luna : undefined },
			modelRegistry: { getApiKey: vi.fn().mockResolvedValue("luna-token") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeStageRunner(fakeCtx, { role: "implement" });
		const result = await runner("edit", "native-catch");
		expect(result.started).toBe(false);
		expect(result.error).toBe("process spawn error");
		expect(result.usage).toBeUndefined();
		expect(result.requests).toBeUndefined();
	});

	test("prepareNativeAuditRunner returns a runner when preconditions exist", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);
		expect(typeof runner).toBe("function");
	});

	test("prepareNativeAuditRunner fails before any probe when @audit credentials are missing (OMP-251)", async () => {
		mockDiscovery();
		const completeSpy = vi.spyOn(ai, "completeSimple");
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess");
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve(undefined) },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("No provider credentials configured for @audit model openai-codex/gpt-5.6-sol");
		expect(completeSpy).not.toHaveBeenCalled();
		expect(runSubprocessSpy).not.toHaveBeenCalled();
	});

	test("prepareNativeAuditRunner fails when the transport probe reports an in-band provider error (OMP-251)", async () => {
		mockDiscovery();
		vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "error",
			errorMessage: "401 unauthorized",
			content: [],
		} as never);
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess");
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow(
			"@audit transport preflight error for openai-codex/gpt-5.6-sol: 401 unauthorized",
		);
		expect(runSubprocessSpy).not.toHaveBeenCalled();
	});

	test("prepareNativeAuditRunner qualifies a rejected transport probe with the audit model (OMP-251)", async () => {
		mockDiscovery();
		vi.spyOn(ai, "completeSimple").mockRejectedValue(new Error("ECONNREFUSED 127.0.0.1:443"));
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess");
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow(
			"@audit transport preflight failed for openai-codex/gpt-5.6-sol: ECONNREFUSED 127.0.0.1:443",
		);
		expect(runSubprocessSpy).not.toHaveBeenCalled();
	});

	test("runner returns started:false when cancelled before start", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);

		const abortController = new AbortController();
		abortController.abort(); // already aborted

		const result = await runner("test task", "attempt-1", abortController.signal);
		expect(result.started).toBe(false);
		expect(result.payload).toBeUndefined();
	});
	test("forwards effective settings to the native auditor subprocess", async () => {
		const sentinelSettings = Settings.isolated({ modelRoles: { audit: "openai-codex/gpt-5.6-sol:medium" } });
		const settingsSpy = vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(sentinelSettings);

		const sentinelOutputSchema = { properties: { report: { type: "string" } } };
		const fakeAgent: AgentDefinition = {
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			output: sentinelOutputSchema,
			source: "bundled",
		};
		const discoverSpy = mockDiscovery(fakeAgent);
		let capturedOptions: executorModule.ExecutorOptions | undefined;
		const wrappedPayload = JSON.stringify({
			report: "VERDICT: PASS\nAll acceptance criteria verified.",
		});
		const runSubprocessSpy = vi
			.spyOn(executorModule, "runSubprocess")
			.mockImplementation(async (options) => {
				capturedOptions = options;
				return {
					index: options.index,
					id: options.id,
					agent: options.agent.name,
					agentSource: options.agent.source,
					task: options.task,
					exitCode: 0,
					output: wrappedPayload,
					stderr: "",
					truncated: false,
					durationMs: 120,
					tokens: 450,
					requests: 1,
				} as executorModule.SingleResult;
			});

		const sentinelRegistry = { getApiKey: () => Promise.resolve("key") };
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(sentinelSettings),
			modelRegistry: sentinelRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(fakeCtx);
		const result = await runner("Run audit on OMP-173", "attempt-123");

		expect(settingsSpy).toHaveBeenCalledWith({
			cwd: repoRoot,
			agentDir: expect.any(String),
		});
		expect(discoverSpy).toHaveBeenCalledWith(repoRoot);
		expect(runSubprocessSpy).toHaveBeenCalledTimes(1);
		expect(capturedOptions).toBeDefined();
		expect(capturedOptions?.settings).toBe(sentinelSettings);
		expect(capturedOptions?.modelOverride).toBe("openai-codex/gpt-5.6-sol:medium");
		expect(capturedOptions?.resolvedModel).toBeDefined();
		expect(capturedOptions?.resolvedModel?.provider).toBe("openai-codex");
		expect(capturedOptions?.resolvedModel?.id).toBe("gpt-5.6-sol");
		expect(capturedOptions?.thinkingLevel).toBe("medium");
		expect(capturedOptions?.modelRole).toBe("audit");
		expect(capturedOptions?.modelRegistry).toBe(sentinelRegistry);
		expect(capturedOptions?.outputSchema).toBe(sentinelOutputSchema);
		expect(capturedOptions?.outputSchemaSource).toBe("agent");
		expect(capturedOptions?.outputSchemaMode).toBe("strict");
		expect(result.started).toBe(true);
		expect(result.payload).toBe(wrappedPayload);
	});

	test("forwards exact bound route model reference and effort to runSubprocess for audit stage", async () => {
		const sentinelSettings = Settings.isolated({ modelRoles: { audit: "openai-codex/gpt-5.6-sol:medium" } });
		vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(sentinelSettings);
		mockDiscovery();

		let capturedOptions: executorModule.ExecutorOptions | undefined;
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options) => {
			capturedOptions = options;
			return {
				index: options.index,
				id: options.id,
				agent: options.agent.name,
				agentSource: options.agent.source,
				task: options.task,
				exitCode: 0,
				output: JSON.stringify({ report: "PASS" }),
				stderr: "",
				truncated: false,
				durationMs: 50,
				tokens: 100,
				requests: 1,
				resolvedModel: options.modelOverride as string,
			} as executorModule.SingleResult;
		});

		const repoRoot = path.resolve(import.meta.dir, "../..");
		const modelQuery = createAuditorTestModelQuery(sentinelSettings);
		const { route: boundRoute } = resolveAuditPolicy(modelQuery);
		const fakeCtx = {
			cwd: repoRoot,
			models: modelQuery,
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(fakeCtx, undefined, boundRoute);
		const result = await runner("Audit contract check", "attempt-bound-model-1");

		expect(result.started).toBe(true);
		expect(capturedOptions).toBeDefined();
		// Exact reference identity from the frozen bound route
		expect(capturedOptions?.resolvedModel).toBe(boundRoute.model);
		expect(capturedOptions?.thinkingLevel).toBe(boundRoute.effort);
		expect(capturedOptions?.modelOverride).toBe(boundRoute.requestedSelector);
	});

	test("forwards authStorage and getApiKey resolver for OAuth-backed @audit models (OMP-176)", async () => {
		const sentinelSettings = Settings.isolated({ modelRoles: { audit: "openai-codex/gpt-5.6-sol:medium" } });
		vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(sentinelSettings);
		mockDiscovery();

		let capturedOptions: executorModule.ExecutorOptions | undefined;
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options) => {
			capturedOptions = options;
			return {
				index: options.index,
				id: options.id,
				agent: options.agent.name,
				agentSource: options.agent.source,
				task: options.task,
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: PASS\nAll ACs verified." }),
				stderr: "",
				truncated: false,
				durationMs: 100,
				tokens: 200,
				requests: 1,
			} as executorModule.SingleResult;
		});

		const fakeAuthStorage = { hasOAuth: () => true };
		const fakeResolver = vi.fn().mockReturnValue(async () => "oauth-bearer-token");
		const sentinelRegistry = {
			getApiKey: () => Promise.resolve("key"),
			authStorage: fakeAuthStorage,
			resolver: fakeResolver,
		};
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(sentinelSettings),
			modelRegistry: sentinelRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(fakeCtx);
		const result = await runner("Run audit on OMP-176", "attempt-oauth-1");

		expect(result.started).toBe(true);
		expect(capturedOptions).toBeDefined();
		expect(capturedOptions?.modelRegistry).toBe(sentinelRegistry as unknown as executorModule.ExecutorOptions["modelRegistry"]);
		expect(capturedOptions?.authStorage).toBe(fakeAuthStorage as unknown as executorModule.ExecutorOptions["authStorage"]);
		expect(typeof capturedOptions?.getApiKey).toBe("function");

		const testModel = createAuditorTestModel();
		const resolvedKey = await capturedOptions?.getApiKey?.(testModel);
		expect(fakeResolver).toHaveBeenCalledWith(testModel, "attempt-oauth-1");
		expect(typeof resolvedKey).toBe("function");
	});
	test("behavioral: child AgentSession prompt resolves OAuth token when static registry key is unavailable (OMP-176)", async () => {
		const testModel = {
			id: "k3",
			provider: "kimi-code",
			api: "openai-completions",
			baseUrl: "https://api.kimi.com/coding/v1",
			name: "Kimi k3",
		} as unknown as Model;

		const fakeResolver = vi.fn().mockReturnValue(async () => "oauth-valid-bearer-token");
		const mockRegistry = {
			getApiKey: vi.fn().mockResolvedValue(undefined),
			authStorage: { hasOAuth: () => true },
			resolver: fakeResolver,
		} as unknown as ModelRegistry;

		const agent = new Agent({
			initialState: { model: testModel, systemPrompt: ["test"], tools: [] },
			getApiKey: requestModel => mockRegistry.resolver(requestModel, "attempt-oauth-behavioral"),
			streamFn: async () => {
				const stream = new AssistantMessageEventStream();
				queueMicrotask(() => {
					stream.push({ type: "text_delta", delta: "OK" });
					stream.end({ role: "assistant", content: [{ type: "text", text: "OK" }], stopReason: "stop" });
				});
				return stream;
			},
		});

		const session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings: Settings.isolated(),
			modelRegistry: mockRegistry,
		});

		// Calling session.prompt executes real AgentSession.prototype.prompt and validates API key using getApiKey
		await session.prompt("Run audit check");
		await session.waitForIdle();

		expect(fakeResolver).toHaveBeenCalledWith(testModel, "attempt-oauth-behavioral");
		await session.dispose();
	});

	test("fails if the auditor output schema is missing", async () => {
		const fakeAgent: AgentDefinition = {
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			source: "bundled",
		};
		mockDiscovery(fakeAgent);
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("output schema");
	});

	test("grant state guard denies remediation on stopped or canceled grants (OMP-186)", async () => {
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;
		let grantState = "stopped";
		let terminalReason: string | null = "budget_exhausted";
		let getExecutionCallCount = 0;
		let mockJudge = "judge-sha";
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "work-1", key: "OMP-186", title: "Test", project: "Bookends" }),
			issueDetail: async () => ({
				key: "OMP-186",
				attemptSnapshot: { attemptId: "att-1", state: "audit_ready", candidateCommit: "commit-1", hasManifest: true },
			}),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => {
				getExecutionCallCount++;
				return {
					grant: { grant_id: "grant-1", grant_version: 1, state: grantState, terminal_reason: terminalReason, judge_sha256: mockJudge, repository: path.resolve(import.meta.dir, "../..") },
					items: [{ position: 0, work_id: "work-1", phase: "executing", plan_stamp: { paths: [] } }],
					activeItem: { position: 0, work_id: "work-1", phase: "executing", plan_stamp: { paths: [] }, close_attempts_started: 0 },
				};
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => ({ status: "reserved", launchId: "launch-1" }),
			settleAuditorLaunch: async () => ({ verdict: "NEEDS_FIX", event: { renderedText: "AC-1 failed" } }),
			workClient: {
				healthReady: async () => ({ contract_sha256: WORK_CONTRACT_SHA256, service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({
					close_attempts: [{ attempt_id: "att-1", revision_id: "rev-1", candidate_id: "cand-1", candidate_sha256: "sha-1", candidate_commit: "commit-1" }],
					item: { current_revision_id: "rev-1", candidate: { candidate_id: "cand-1", candidate_sha256: "sha-1", commit_sha: "commit-1" } },
				}),
			},
		} as unknown as WorkflowBackend;

		attachStageLaunchFixture(mockBackend, path.resolve(import.meta.dir, "../.."));
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		expect(registeredExecute).toBeDefined();

		mockDiscovery();
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-1",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: NEEDS_FIX\nAC-1 failed" }),
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
			} as executorModule.SingleResult;
		});

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
			ui: { notify: () => {} },
		} as unknown as ExtensionContext;

		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
			mockJudge = tcb.judgeSha256;

			// 1. Stopped grant returns denial with terminal reason
			grantState = "stopped";
			terminalReason = "budget_exhausted";
			const stoppedResult = await registeredExecute!("call-1", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(stoppedResult.content[0].text).toContain("Execution grant stopped (budget_exhausted)");
			expect(stoppedResult.content[0].text).not.toContain("Update plan, stamp plan");

			// 2. Canceled grant returns denial with terminal reason
			grantState = "canceled";
			terminalReason = "owner_canceled";
			const canceledResult = await registeredExecute!("call-2", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(canceledResult.content[0].text).toContain("Execution grant canceled (owner_canceled)");
			expect(canceledResult.content[0].text).not.toContain("Update plan, stamp plan");

			// 3. Active grant returns remediation instruction
			grantState = "active";
			terminalReason = null;
			const activeResult = await registeredExecute!("call-3", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(activeResult.content[0].text).toContain("Update plan, stamp plan, fix findings, and rerun review.");
		} finally {
			dirtySpy.mockRestore();
		}
	});
	test("pause notice argument round-trips through execute resume lookup", async () => {
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "pause-notice-cache-"));
		const cacheFile = path.relative(
			path.join(os.homedir(), ".omp", "agent"),
			path.join(cacheDir, "work-cache.json"),
		);
		type OutboxData = {
			grantId: string;
			sessionId: string;
			workId: string;
			revisionId: string;
			preReservationVersion: number;
			postVersion: number;
			messageId: string;
			status: "pending" | "queued" | "delivered";
			at: string;
		};
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<unknown>>>();
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const notifications: string[] = [];
		const sentMessages: Array<{ customType?: string; content?: string }> = [];
		const lookupArgs: Array<string | undefined> = [];
		const appendedEntries: string[] = [];
		const appendedRecords: Array<{ customType: string; data?: OutboxData }> = [];
		let branchEntries: Array<{ type: "custom"; customType: string; data: OutboxData }> = [];
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: (event: string, handler: (event: unknown, ctx: ExtensionContext) => Promise<unknown>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (message: { customType?: string; content?: string }) => {
				sentMessages.push(message);
			},
			appendEntry: (customType: string, data?: unknown) => {
				appendedEntries.push(customType);
				appendedRecords.push({
					customType,
					...(data && typeof data === "object" ? { data: data as OutboxData } : {}),
				});
			},
			getSessionId: () => "pause-notice-session",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "pause-notice-repo-"));
		spawnSync("git", ["init", "-b", "main"], { cwd });
		spawnSync("git", ["config", "user.name", "Test"], { cwd });
		spawnSync("git", ["config", "user.email", "test@example.com"], { cwd });
		fs.writeFileSync(path.join(cwd, "foreign.txt"), "tracked foreign source\n");
		spawnSync("git", ["add", "."], { cwd });
		spawnSync("git", ["commit", "-m", "seed foreign source"], { cwd });
		const head = headCommit(cwd) ?? "0".repeat(40);
		const workId = "c7a904ca-c979-4cc9-aa87-6063da842aec";
		const revisionId = "f7eed1b9-7f74-4998-85d6-2bce9862067c";
		const issue = { id: workId, key: "OMP-190", title: "Pause notice", project: "Bookends" };
		const exec = {
			grant: {
				grant_id: "28a3950b-0abe-4a0c-a3ec-18438b8b3267",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: cwd,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 1,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [],
			activeItem: {
				item_id: "item-1",
				workspace_id: "ws-1",
				grant_id: "28a3950b-0abe-4a0c-a3ec-18438b8b3267",
				work_id: workId,
				position: 0,
				phase: "executing",
				claimed_revision_id: revisionId,
				project_id: null,
				original_request: "Pause notice",
				original_request_sha256: "0".repeat(64),
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: head,
				current_git_baseline: head,
			},
		} as unknown as ExecutionSnapshot;
		exec.items = [exec.activeItem!];
		const ownershipEntry = executionOwnershipEntry(exec, cwd, issue.key);

		let failNextIssueLookup = false;
		let suppressNextDelivery = false;
		let keyedLookupsBeforeSuppression = 0;
		const workItemView = () => ({
			work_id: workId,
			state: "IN_PROGRESS",
			project_id: null,
			revision: { revision_id: revisionId },
		});
		const mockBackend = {
			cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async (selector?: string) => {
				lookupArgs.push(selector);
				if (selector === exec.grant.grant_id && suppressNextDelivery && keyedLookupsBeforeSuppression-- <= 0) {
					suppressNextDelivery = false;
					return {
						...exec,
						grant: { ...exec.grant, state: "completed" },
						activeItem: null,
					};
				}
				return selector === undefined
					|| selector === issue.key
					|| selector === workId
					|| selector === exec.grant.grant_id
					? exec
					: null;
			},
			findIssue: async () => {
				if (failNextIssueLookup) {
					failNextIssueLookup = false;
					return null;
				}
				return issue;
			},
			setExecutionState: async (input: { targetState: "active" | "paused" }) => {
				exec.grant.state = input.targetState;
				exec.grant.grant_version++;
				return exec;
			},
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: WORK_CONTRACT_SHA256,
					service_fingerprint: "service-fp",
					judge_manifest: { judge_sha256: "judge-sha" },
				}),
				workItem: async () => workItemView(),
				workflow: async () => ({ item: workItemView(), relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: identityExecutionWorkspaceManager,
		})(fakePi);
		mockDiscovery();

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			abort: () => {},
			models: createAuditorTestModelQuery(),
			sessionManager: { getBranch: () => [ownershipEntry, ...branchEntries], getSessionId: () => "pause-notice-session", getCwd: () => cwd },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_color: string, text: string) => text },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;
		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		let dirt: string[] = [];
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockImplementation(() => dirt);

		const pauseAndResume = async (fallback: boolean, suppressDelivery = false): Promise<string> => {
			failNextIssueLookup = fallback;
			const inputHandler = handlers.get("input")?.[0];
			expect(inputHandler).toBeDefined();
			await inputHandler!({ source: "user", originalText: "owner interjection" }, fakeCtx);
			expect(exec.grant.state).toBe("paused");

			const beforeStarts = handlers.get("before_agent_start") ?? [];
			expect(beforeStarts.length).toBeGreaterThan(0);
			let notice = "";
			for (const beforeStart of beforeStarts) {
				const injected = await beforeStart({}, fakeCtx);
				const content = String(injected?.message?.content ?? "");
				if (content.includes("Execution grant paused")) notice = content;
			}
			const match = notice.match(/\/execute resume ([^']+)'/);
			expect(match).not.toBeNull();
			const suggested = match![1]!;

			const resume = commands.get("execute");
			expect(resume).toBeDefined();
			if (suppressDelivery) { suppressNextDelivery = true; keyedLookupsBeforeSuppression = 0; }
			await resume!(`resume ${suggested}`, fakeCtx);
			expect(exec.grant.state).toBe("active");
			expect(notifications.at(-1)).toContain("Execution grant resumed");
			return suggested;
		};

		try {
			const pendingReplay = (messageId: string): { type: "custom"; customType: string; data: OutboxData } => ({
				type: "custom",
				customType: "work-now-execute-outbox",
				data: {
					grantId: exec.grant.grant_id,
					sessionId: "pause-notice-session",
					workId,
					revisionId,
					preReservationVersion: 0,
					postVersion: exec.grant.grant_version,
					messageId,
					status: "pending",
					at: new Date().toISOString(),
				},
			});
			const sessionStarts = handlers.get("session_start") ?? [];
			expect(sessionStarts.length).toBeGreaterThan(0);

			branchEntries = [pendingReplay("active-replay")];
			for (const start of sessionStarts) await start({}, fakeCtx);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(1);
			expect(appendedRecords.filter(record =>
				record.customType === "work-now-execute-outbox"
				&& record.data?.status === "queued"
				&& record.data?.messageId === "active-replay"
			)).toHaveLength(1);
			expect(appendedRecords.filter(record =>
				record.customType === "work-now-execute-outbox"
				&& record.data?.status === "pending"
			)).toHaveLength(0);

			sentMessages.length = 0;
			appendedEntries.length = 0;
			appendedRecords.length = 0;
			branchEntries = [pendingReplay("completed-replay")];
			suppressNextDelivery = true;
			keyedLookupsBeforeSuppression = 1; // Startup ownership lookup precedes the delivery-seam re-fetch.
			for (const start of sessionStarts) await start({}, fakeCtx);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			expect(appendedRecords.filter(record =>
				record.customType === "work-now-execute-outbox"
			)).toHaveLength(0);
			branchEntries = [];
			expect(await pauseAndResume(false)).toBe(issue.key);
			expect(lookupArgs).toContain(issue.key);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(1);
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(2);

			sentMessages.length = 0;
			appendedEntries.length = 0;
			expect(await pauseAndResume(true)).toBe(workId);
			expect(lookupArgs).toContain(workId);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(1);
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(2);

			sentMessages.length = 0;
			appendedEntries.length = 0;
			expect(await pauseAndResume(false, true)).toBe(issue.key);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(0);

			const resume = commands.get("execute");
			expect(resume).toBeDefined();
			exec.activeItem!.plan_stamp = { paths: ["src/sealed.ts"] };
			for (const phase of ["executing", "remediating"] as const) {
				exec.activeItem!.phase = phase;
				exec.grant.state = "paused";
				dirt = ["src/sealed.ts"];
				await resume!(`resume ${issue.key}`, fakeCtx);
				expect(exec.grant.state).toBe("active");
				expect(notifications.at(-1)).toContain("Execution grant resumed");
			}

			exec.activeItem!.phase = "remediating";
			exec.grant.state = "paused";
			dirt = ["src/sealed.ts", "foreign.txt"];
			await resume!(`resume ${issue.key}`, fakeCtx);
			expect(exec.grant.state).toBe("paused");
			expect(notifications.at(-1)).toContain("dirty worktree outside sealed paths: foreign.txt");
			expect(notifications.at(-1)).not.toContain("src/sealed.ts");

			fs.mkdirSync(path.join(cwd, "src"), { recursive: true });
			fs.renameSync(path.join(cwd, "foreign.txt"), path.join(cwd, "src/sealed.ts"));
			exec.grant.state = "paused";
			dirt = ["src/sealed.ts"]; // dirtyPaths staging view intentionally omits rename source
			await resume!(`resume ${issue.key}`, fakeCtx);
			expect(exec.grant.state).toBe("paused");
			expect(notifications.at(-1)).toContain("dirty worktree outside sealed paths: foreign.txt");
			expect(notifications.at(-1)).not.toContain("src/sealed.ts");
		} finally {
			dirtySpy.mockRestore();
			fs.rmSync(cacheDir, { recursive: true, force: true });
			fs.rmSync(cwd, { recursive: true, force: true });
		}
	});

});

describe("renderNextActionBanner table-driven coverage (OMP-168)", () => {
	const snapshot = (state: string): CloseAttemptSnapshot => ({
		attemptId: "att-1",
		state,
		remainingLaunches: 3,
		remainingReports: 2,
		hasManifest: true,
		isLaunchable: state === "audit_ready",
		nextAction: "",
	});

	test("active state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("active"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT active",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"verification"',
			'BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT active");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audit_ready state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audit_ready"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audit_ready",
			'NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"',
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audit_ready");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("auditor_in_flight state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("auditor_in_flight"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT auditor_in_flight",
			"NEXT REQUIRED ACTION: wait for the current native run to settle and use get_work only for recovery",
			"BLOCKED ACTIONS: run_audit, append_evidence, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT auditor_in_flight");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (authorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"',
			"BLOCKED ACTIONS: run_audit, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (unauthorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), false);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			"NEXT REQUIRED ACTION: owner /summary must be entered in this session to authorize closeout review",
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("closeout_requested state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("closeout_requested"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT closeout_requested",
			"NEXT REQUIRED ACTION: owner /done closes this work",
			"BLOCKED ACTIONS: run_audit, append_evidence",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT closeout_requested");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("terminal or missing snapshot returns empty array", () => {
		expect(renderNextActionBanner("HOME-1", undefined, true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("completed"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("superseded"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("budget_exhausted"), true)).toEqual([]);
	});

	test("renderSummaryResumeDigest contains banner and 5 compact review sections", () => {
		const digest = renderSummaryResumeDigest("HOME-1", snapshot("audited"));
		expect(digest).toContain("STATUS: CLOSE ATTEMPT audited");
		expect(digest).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"');
		expect(digest).toContain("Satisfied steps must NOT be repeated");
		expect(digest).toContain('Call `work action:"get_work", work:"HOME-1"`');
		expect(digest).toContain('1. Verbatim `work action:"my_now"` completion tree');
		expect(digest).toContain("2. MOVED");
		expect(digest).toContain("3. PROOF");
		expect(digest).toContain("4. UNVERIFIED / BLOCKED");
		expect(digest).toContain("5. NEXT SESSION");
	});
});

describe("confirmation lifecycle (OMP-168)", () => {
	test("same-transcript identical payload approves once; consumed receipt refuses retry", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create an issue";
		const detail = "title: test";
		const params = { title: "test" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		expect(match).not.toBeNull();
		const confirmationId = match![1];

		// Second call with confirm:true and confirmation_id approves
		const second = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(second.approved).toBe(true);

		// Third call with already consumed id is refused
		const third = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(third.approved).toBe(false);
		if (!third.approved) {
			expect(third.preview).toContain("already consumed");
		}
	});

	test("59-minute receipt remains usable", () => {
		resetConfirmations({ resetShared: true });
		const action = "revise_work";
		const question = "Model wants to revise";
		const detail = "new title";
		const params = { title: "revised" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		const confirmationId = match![1];

		// Advance time by 59 minutes
		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 59 * 60_000;
			const outcome = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: confirmationId,
			});
			expect(outcome.approved).toBe(true);
		} finally {
			Date.now = originalNow;
		}
	});

	test(">60-minute expired receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "set_now";
		const question = "Model wants to set now";
		const detail = "HOME-1";
		const params = { work: "HOME-1" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 61 * 60_000;
			const retry = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: oldId,
			});
			expect(retry.approved).toBe(false);
			if (!retry.approved) {
				expect(retry.preview).toContain("CONFIRM REQUIRED");
				const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
				expect(newId).not.toBe(oldId);
			}
		} finally {
			Date.now = originalNow;
		}
	});

	test("foreign transcript receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "queue_work";
		const question = "Model wants to queue";
		const detail = "HOME-2";
		const params = { work: "HOME-2", question: "Is this done?" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Switch session / rotate transcript without clearing unconsumed receipts
		resetConfirmations({ resetShared: true });

		const retry = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: oldId,
		});
		expect(retry.approved).toBe(false);
		if (!retry.approved) {
			expect(retry.preview).toContain("CONFIRM REQUIRED");
			const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
			expect(newId).not.toBe(oldId);
		}
	});

	test("changed payload and unknown receipt stay refused", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create";
		const detail = "title A";
		const params = { title: "title A" };

		const first = confirmWrite(action, question, detail, params);
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Changed payload
		const changed = confirmWrite(action, question, "title B", {
			title: "title B",
			confirm: true,
			confirmation_id: id,
		});
		expect(changed.approved).toBe(false);
		if (!changed.approved) {
			expect(changed.preview).toContain("payload changed");
		}

		// Unknown ID
		const unknown = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: "cf-unknown0000",
		});
		expect(unknown.approved).toBe(false);
		if (!unknown.approved) {
			expect(unknown.preview).toContain("unknown or already-used");
		}
	});

	test("subagent reset (resetShared:false) never invalidates owner receipts", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Owner create";
		const detail = "owner details";
		const params = { title: "owner task" };

		const first = confirmWrite(action, question, detail, params, { isSubagent: false });
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Subagent session resets local confirmations
		resetConfirmations({ resetShared: false });

		// Owner confirmation call still approves
		const confirmCall = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: id,
		});
		expect(confirmCall.approved).toBe(true);
	});
});

describe("audit judge TCB sealing (OMP-180)", () => {
	test("getExecutorSha fails closed when required yield-assembly source is unresolvable", async () => {
		const { getExecutorSha } = await import("../extensions/workflow/audit-tcb");
		expect(() => {
			getExecutorSha((specifier: string) => {
				if (specifier === "@oh-my-pi/pi-coding-agent/task/yield-assembly") return undefined;
				return import.meta.resolve(specifier);
			});
		}).toThrow("Failed to resolve required audit transport source: @oh-my-pi/pi-coding-agent/task/yield-assembly");
	});

	test("getExecutorSha returns stable 64-hex digest under standard resolver", async () => {
		const { getExecutorSha } = await import("../extensions/workflow/audit-tcb");
		const sha = getExecutorSha();
		expect(typeof sha).toBe("string");
		expect(sha).toMatch(/^[0-9a-f]{64}$/);
	});
});

describe("terminal execution grant closing notices and banners (OMP-196)", () => {

	test("closing notice keeps prose references out of commands (queue mode)", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
			{ position: 2, work_id: "OMP-181", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const notice = computeExecutionNoticeDetails(exec, "candidate_drift gate defect filed as OMP-195", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 0 completed, 3 skipped (of 3 items).");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-176 --queue");
		expect(notice.fullNotice).toBe([
			"Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.",
			"Items: 0 completed, 3 skipped (of 3 items).",
			"Next: /execute OMP-176 --queue",
		].join("\n"));
	});

	test("closing notice keeps prose references out of commands (single mode)", () => {
		const exec = makeSnapshot("stopped", "single", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
		], "blocked by OMP-195");

		const notice = computeExecutionNoticeDetails(exec, "blocked by OMP-195", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (blocked by OMP-195). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 0 completed, 1 skipped (of 1 item).");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-176");
	});

	test("closing notice on partial queue completion and stop", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-170", phase: "completed", completed_at: new Date().toISOString() },
			{ position: 1, work_id: "OMP-176", phase: "executing" },
			{ position: 2, work_id: "OMP-180", phase: "pending" },
		], "budget_exhausted");

		const notice = computeExecutionNoticeDetails(exec, "budget_exhausted", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (budget_exhausted). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 1 completed, 2 skipped (of 3 items).");
		expect(notice.nextCommandLine).toBe("Next: /now OMP-176 then /summary");
	});

	test("closing notice on contract approval requirement (paused vs terminal)", () => {
		const pausedExec = makeSnapshot("paused", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "contract_approval_required:1a5441d9");
		const pausedNotice = computeExecutionNoticeDetails(pausedExec, "contract_approval_required:1a5441d9", "OMP-180");
		expect(pausedNotice.nextCommandLine).toBe("Next: omp-work approve --issue OMP-180 then /execute resume");

		const stoppedExec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "contract_approval_required:1a5441d9");
		const stoppedNotice = computeExecutionNoticeDetails(stoppedExec, "contract_approval_required:1a5441d9", "OMP-180");
		expect(stoppedNotice.nextCommandLine).toBe("Next: omp-work approve --issue OMP-180 then /execute OMP-180 --queue");
	});

	test("closing notice on owner cancellation", () => {
		const exec = makeSnapshot("canceled", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "owner_cancel");

		const notice = computeExecutionNoticeDetails(exec, "owner_cancel", "OMP-180");
		expect(notice.causeLine).toBe("Execution grant canceled (owner_cancel). Grant is terminal; resume is impossible.");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-180 --queue");
	});

	test("renderExecutionTerminalBanner returns 4-line banner for stopped/canceled grants", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const banner = renderExecutionTerminalBanner(exec as any, "OMP-176");
		expect(banner).toEqual([
			"STATUS: EXECUTION GRANT stopped (terminal — resume impossible)",
			"CAUSE: candidate_drift gate defect filed as OMP-195",
			"ITEMS: 0 completed, 2 skipped (of 2 items).",
			"NEXT REQUIRED ACTION: /execute OMP-176 --queue",
		]);

		const activeExec = makeSnapshot("active", "queue");
		expect(renderExecutionTerminalBanner(activeExec, "OMP-176")).toEqual([]);
	});

	test("resolveAnchorKey resolves UUID to issue key via backend", async () => {
		const mockBackend = {
			findIssue: async (keyOrId: string) => {
				if (keyOrId === "uuid-176") return { key: "OMP-176" };
				if (keyOrId === "uuid-180") return { key: "OMP-180" };
				return null;
			},
		};
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "uuid-176", phase: "executing" },
			{ position: 1, work_id: "uuid-180", phase: "pending" },
		]);
		const key = await resolveAnchorKey(mockBackend, exec);
		expect(key).toBe("OMP-176");

		// Foreign targetKey that does not belong to the grant is ignored
		const foreignKey = await resolveAnchorKey(mockBackend, exec, "OMP-999");
		expect(foreignKey).toBe("OMP-176");

		// Member targetKey is accepted
		const memberKey = await resolveAnchorKey(mockBackend, exec, "uuid-180");
		expect(memberKey).toBe("OMP-180");
	});

	test("stop_execution tool action returns full closing notice with cause, tally, and next command", async () => {
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = makeSnapshot("active", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		]);

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async (input: { targetState: ExecutionSnapshot["grant"]["state"]; reason?: string }) => ({
				grant: { ...exec.grant, state: input.targetState, terminal_reason: input.reason },
			}),
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
		} as unknown as ExtensionContext;

		const result = await registeredExecute!(
			"call-stop",
			{ action: "stop_execution", body: "candidate_drift gate defect filed as OMP-195", work: "OMP-176" },
			new AbortController().signal,
			undefined,
			fakeCtx,
		);

		expect(result.content[0].text).toContain("Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.");
		expect(result.content[0].text).toContain("Items: 0 completed, 2 skipped (of 2 items).");
		expect(result.content[0].text).toContain("Next: /execute OMP-176 --queue");
	});

	test("stamp_execution_plan in executing phase refuses unsealed dirty paths and allows clean re-planning", async () => {
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = makeSnapshot("active", "single", [
			{
				position: 0,
				work_id: "OMP-176",
				phase: "executing",
				close_attempts_started: 0,
				plan_stamp: { paths: ["src/initial.ts"] },
			},
		]);

		let stampedPaths: string[] = [];
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			stampExecutionPlan: async (input: { paths: string[] }) => {
				stampedPaths = input.paths;
				return exec;
			},
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);
		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "stamp-test-"));
		spawnSync("git", ["init"], { cwd: testDir });
		fs.mkdirSync(path.join(testDir, "src"), { recursive: true });
		const planPath = path.join(os.tmpdir(), `stamp-plan-${crypto.randomUUID()}.md`);
		fs.writeFileSync(planPath, "## Approach\n1. Step\n\n## Verification\n1. Check\n");

		let mockDirt: string[] = ["src/initial.ts", "src/unsealed_dirt.ts"];
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockImplementation(() => mockDirt);
		let runGitSpy: { mockRestore(): void; mockImplementation(fn: (cwd: string, args: string[]) => { ok: boolean; out: string; raw: string; err: string }): unknown } | undefined;

		try {
			const fakeCtx = {
				cwd: testDir,
				taskDepth: 0,
				models: createAuditorTestModelQuery(),
				ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			} as unknown as ExtensionContext;

			// 1. Unsealed dirty path is refused
			const refused = await registeredExecute!(
				"call-stamp-1",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/unsealed_dirt.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(refused.content[0].text).toContain("Scope correction refused: worktree contains dirty unsealed path(s) [src/unsealed_dirt.ts]");

			// 2. Clean addition of new path succeeds
			mockDirt = ["src/initial.ts"]; // only already-sealed path is dirty
			const allowed = await registeredExecute!(
				"call-stamp-2",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/new_clean.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(allowed.content[0].text).toContain("plan stamped successfully");
			expect(stampedPaths).toEqual(["src/initial.ts", "src/new_clean.ts"]);

			// 3. Rename source detection: renaming unsealed file to sealed destination is refused
			runGitSpy = vi.spyOn(gitModule, "runGit");
			runGitSpy.mockImplementation((cwd, args) => {
				if (args[0] === "rev-parse" && args[1] === "--show-toplevel") {
					return { ok: true, out: testDir, raw: testDir, err: "" };
				}
				if (args[0] === "status") {
					// porcelain -z for rename: R  src/initial.ts\0src/unsealed_old.ts\0
					const rawStr = "R  src/initial.ts\0src/unsealed_old.ts\0";
					return { ok: true, out: rawStr, raw: rawStr, err: "" };
				}
				return { ok: true, out: "", raw: "", err: "" };
			});
			mockDirt = ["src/initial.ts"]; // dirtyPaths only sees destination
			const renameRefused = await registeredExecute!(
				"call-stamp-3",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/new_clean.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(renameRefused.content[0].text).toContain("Scope correction refused: worktree contains dirty unsealed path(s) [src/unsealed_old.ts]");

			// 3b. Unstaged worktree rename (column Y = R) also captures source path
			runGitSpy.mockImplementation((cwd, args) => {
				if (args[0] === "rev-parse" && args[1] === "--show-toplevel") {
					return { ok: true, out: testDir, raw: testDir, err: "" };
				}
				if (args[0] === "status") {
					const rawStr = " R src/initial.ts\0src/unsealed_unstaged.ts\0";
					return { ok: true, out: rawStr, raw: rawStr, err: "" };
				}
				return { ok: true, out: "", raw: "", err: "" };
			});
			const unstagedRenameRefused = await registeredExecute!(
				"call-stamp-3b",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/new_clean.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(unstagedRenameRefused.content[0].text).toContain("Scope correction refused: worktree contains dirty unsealed path(s) [src/unsealed_unstaged.ts]");
			// 4. Git status inspection failure fails closed
			runGitSpy.mockImplementation((cwd, args) => {
				if (args[0] === "status") return { ok: false, out: "", raw: "", err: "git failed" };
				return { ok: true, out: testDir, raw: testDir, err: "" };
			});
			const statusFailed = await registeredExecute!(
				"call-stamp-4",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/new_clean.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(statusFailed.content[0].text).toContain("Scope correction refused: unable to inspect complete touched path set");

			// 5. Post-resume in planning phase with existing plan stamp refuses unsealed dirt
			runGitSpy.mockRestore();
			runGitSpy = undefined;
			exec.activeItem!.phase = "planning"; // simulated phase after awaiting_contract_approval resume
			mockDirt = ["src/initial.ts", "src/post_resume_unsealed.ts"];
			const postResumeRefused = await registeredExecute!(
				"call-stamp-5",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/initial.ts", "src/post_resume_unsealed.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(postResumeRefused.content[0].text).toContain("Scope correction refused: worktree contains dirty unsealed path(s) [src/post_resume_unsealed.ts]");
		} finally {
			runGitSpy?.mockRestore();
			dirtySpy.mockRestore();
			fs.rmSync(planPath, { force: true });
			fs.rmSync(testDir, { recursive: true, force: true });
		}
	});

	test("expandExecutionPlanClosure automatically includes approval, schema, and client dependencies for contract paths", () => {
		const cwd = path.resolve(import.meta.dir, "../..");
		const nonContract = expandExecutionPlanClosure(["src/foo.ts", "session-system/tests/bar.ts"], cwd);
		expect(nonContract).toEqual(["src/foo.ts", "session-system/tests/bar.ts"]);

		const contractOnly = expandExecutionPlanClosure(["python/omp-work/src/omp_work/contracts/v1/contract.json"], cwd);
		expect(contractOnly).toContain("python/omp-work/src/omp_work/contracts/v1/contract.json");
		expect(contractOnly).toContain("python/omp-work/src/omp_work/contracts/v1/approval.json");
		expect(contractOnly).toContain("python/omp-work/src/omp_work/contracts/v1/schema.json");
		expect(contractOnly).toContain("python/omp-work/src/omp_work/contracts/v1/api-schema.json");
		expect(contractOnly).toContain("packages/work-client/src/contract.ts");

		const modelsOnly = expandExecutionPlanClosure(["python/omp-work/src/omp_work/v1/models.py"], cwd);
		expect(modelsOnly).toContain("python/omp-work/src/omp_work/contracts/v1/schema.json");
		expect(modelsOnly).toContain("python/omp-work/src/omp_work/contracts/v1/approval.json");

		const apiModelsOnly = expandExecutionPlanClosure(["python/omp-work/src/omp_work/v1/api_models.py"], cwd);
		expect(apiModelsOnly).toContain("python/omp-work/src/omp_work/contracts/v1/api-schema.json");
		expect(apiModelsOnly).toContain("python/omp-work/src/omp_work/contracts/v1/approval.json");
	});
	test("session_start populates footer status with terminal execution grant banner", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const statuses: Record<string, string> = {};
		const notifications: string[] = [];
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => {
				if (key === "OMP-404") return null;
				return { id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" };
			},
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			currentNow: async () => ({ id: "uuid-999", key: "OMP-999", title: "Unrelated NOW", project: "Bookends" }),
			setNowRemote: async () => {},
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: (t: string) => { notifications.push(t); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const startHandlers = handlers.get("session_start") ?? [];
		expect(startHandlers.length).toBeGreaterThan(0);
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
		expect(statuses["work-now"]).toContain("0 completed, 2 skipped (of 2 items).");
		expect(statuses["work-now"]).toContain("Next: /execute OMP-176 --queue");

		// Verify persistence across /now focus change to an unrelated issue
		const nowCmd = commands.get("now");
		if (nowCmd) {
			await nowCmd("OMP-999", fakeCtx);
			expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
			expect(statuses["work-now"]).toContain("Next: /execute OMP-176 --queue");
		}

		// Verify persistence across rejected /execute start (issue not found)
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const execCmd = commands.get("execute");
			if (execCmd) {
				await execCmd("OMP-404", fakeCtx);
				expect(notifications.some(n => n.includes("Issue OMP-404 not found"))).toBe(true);
				expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
			}
		} finally {
			dirtySpy.mockRestore();
		}
	});

	test("session_start with active or null grant clears cached terminal execution state", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const statuses: Record<string, string> = {};
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => null,
			currentNow: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test OMP-176", project: "Bookends" }),
			setNowRemote: async () => {},
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const startHandlers = handlers.get("session_start") ?? [];
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		expect(statuses["work-now"]).not.toContain("✕ Grant");
		expect(statuses["work-now"]).toContain("NOW · Bookends");
	});

	test("session_start relocates an active grant before recovery delivery (OMP-213)", async () => {
		const originalProjectDir = getProjectDir();
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const messages: Array<{ customType?: string; content?: string }> = [];
		const notifications: string[] = [];
		const appended: string[] = [];
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (event: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (message: { customType?: string; content?: string }) => {
				messages.push(message);
			},
			appendEntry: (customType: string) => {
				appended.push(customType);
			},
			getSessionId: () => "recovery-relocation-session",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "recovery-owner-"));
		const recoveredCwd = fs.mkdtempSync(path.join(os.tmpdir(), "recovery-worktree-"));
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "recovery-cache-"));
		spawnSync("git", ["init", "-b", "main"], { cwd });
		spawnSync("git", ["config", "user.name", "Test"], { cwd });
		spawnSync("git", ["config", "user.email", "test@example.com"], { cwd });
		fs.writeFileSync(path.join(cwd, "seed.txt"), "seed\n");
		spawnSync("git", ["add", "."], { cwd });
		spawnSync("git", ["commit", "-m", "seed"], { cwd });
		const head = headCommit(cwd) ?? "0".repeat(40);
		const exec = makeSnapshot("active", "single", [{ position: 0, work_id: "OMP-213", phase: "executing" }]);
		exec.grant.repository = cwd;
		exec.activeItem!.initial_git_baseline = head;
		exec.activeItem!.current_git_baseline = head;
		exec.items[0]!.initial_git_baseline = head;
		exec.items[0]!.current_git_baseline = head;
		const ensureCalls: Array<{ grantId: string; create: boolean | undefined }> = [];
		const workspaceManager = {
			primaryRoot: async () => cwd,
			ensure: async (_source: string, _key: string, grantId: string, baseline: string, options?: { create?: boolean }) => {
				ensureCalls.push({ grantId, create: options?.create });
				return {
					primaryRoot: cwd,
					path: recoveredCwd,
					branch: "execution/omp-213-recovery",
					grantId,
					baseline,
					reused: true,
				};
			},
			cleanup: async () => ({ cleaned: true, detail: "test cleanup" }),
		};
		const issue = { id: "OMP-213", key: "OMP-213", title: "Recovery relocation", project: "Bookends" };
		const workItemView = () => ({
			work_id: "OMP-213",
			state: "IN_PROGRESS",
			project_id: null,
			revision: { revision_id: "rev-1" },
		});
		const mockBackend = {
			cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "work-cache.json")),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			findIssue: async () => issue,
			currentNow: async () => issue,
			getPendingExecutionClaims: async () => [],
			setExecutionState: async () => {
				exec.grant.grant_version++;
				return exec;
			},
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: WORK_CONTRACT_SHA256,
					service_fingerprint: "service-fp",
					judge_manifest: { judge_sha256: "judge-sha" },
				}),
				workItem: async () => workItemView(),
				workflow: async () => ({ item: workItemView(), relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: workspaceManager,
		})(fakePi);
		const discoverSpy = vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [{
				name: "auditor",
				description: "Recovery test auditor",
				systemPrompt: "Audit",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "test",
			}],
			projectAgentsDir: null,
		});

		let activeCwd = cwd;
		const ownershipEntry = executionOwnershipEntry(exec, recoveredCwd, issue.key, { branch: "execution/omp-213-recovery" });
		const branchEntries = [{
			type: "custom",
			customType: "work-now-execute-outbox",
			data: {
				grantId: exec.grant.grant_id,
				sessionId: "recovery-relocation-session",
				workId: "OMP-213",
				revisionId: "rev-1",
				preReservationVersion: 0,
				postVersion: exec.grant.grant_version,
				messageId: "recovery-relocation",
				status: "pending",
				at: new Date().toISOString(),
			},
		}];
		const fakeCtx = {
			cwd,
			taskDepth: 0,
			models: createAuditorTestModelQuery(),
			sessionManager: {
				getBranch: () => [ownershipEntry, ...branchEntries],
				getSessionId: () => "recovery-relocation-session",
				getCwd: () => activeCwd,
				moveTo: async (nextCwd: string) => {
					activeCwd = nextCwd;
				},
			},
			ui: {
				notify: (text: string) => notifications.push(text),
				theme: { fg: (_color: string, text: string) => text },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;
		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		const headSpy = vi.spyOn(gitModule, "headCommit").mockReturnValue(head);
		try {
			const starts = handlers.get("session_start") ?? [];
			expect(starts.length).toBeGreaterThan(0);
			for (const start of starts) await start({}, fakeCtx);
			expect(activeCwd).toBe(recoveredCwd);
			expect(ensureCalls).toEqual([{ grantId: exec.grant.grant_id, create: false }]);
			expect(messages.some(message => message.customType === "work-execute")).toBe(true);
			expect(appended).toContain("work-now-execute-outbox");
			expect(notifications.some(message => message.includes("recovery skipped"))).toBe(false);
		} finally {
			dirtySpy.mockRestore();
			headSpy.mockRestore();
			discoverSpy.mockRestore();
			setProjectDir(originalProjectDir);
			expect(getProjectDir()).toBe(originalProjectDir);
			fs.rmSync(cacheDir, { recursive: true, force: true });
			fs.rmSync(recoveredCwd, { recursive: true, force: true });
			fs.rmSync(cwd, { recursive: true, force: true });
		}
	});

	test("/execute resume transitioning to stopped emits full notice and status message", async () => {
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const messages: Array<{ customType?: string; content?: string }> = [];
		const notifications: string[] = [];
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			sendMessage: (msg: { customType?: string; content?: string }) => {
				messages.push(msg);
			},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const repo = makeTempRepo();
		fixtureCaches.push(repo.dir, path.resolve(os.homedir(), ".omp", "agent", repo.cacheFile, ".."));
		const cwd = repo.dir;
		const head = repo.headSha;
		const exec = makeSnapshot("paused", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], null);
		exec.items[0]!.initial_git_baseline = head;
		exec.items[0]!.current_git_baseline = head;
		exec.activeItem!.initial_git_baseline = head;
		exec.activeItem!.current_git_baseline = head;
		exec.grant.repository = cwd;
		const ownershipEntry = executionOwnershipEntry(exec, cwd, "OMP-176");

		const workItemView = () => ({
			work_id: "OMP-176",
			state: "IN_PROGRESS",
			project_id: null,
			revision: { revision_id: "rev-1" },
		});
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: key, key, title: `Test ${key}`, project: "Bookends" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			currentNow: async () => ({ id: "OMP-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			setExecutionState: async () => ({
				grant: { ...exec.grant, state: "stopped" as const, terminal_reason: "max_continuations_exceeded" },
			}),
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => workItemView(),
				workflow: async () => ({ item: workItemView(), relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: identityExecutionWorkspaceManager,
		})(fakePi);

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			models: createAuditorTestModelQuery(),
			sessionManager: { getBranch: () => [ownershipEntry], getSessionId: () => "sess-1", getCwd: () => cwd },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const resumeCmd = commands.get("execute");
			expect(resumeCmd).toBeDefined();
			await resumeCmd!("resume OMP-176", fakeCtx);

			expect(notifications).toEqual(expect.arrayContaining([expect.stringContaining("Execution grant stopped: max_continuations_exceeded")]));
			expect(notifications.some(n => n.includes("Items: 0 completed, 2 skipped (of 2 items)."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Grant is terminal; resume is impossible."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Next: /execute OMP-176 --queue"))).toBe(true);
		} finally {
			dirtySpy.mockRestore();
		}
	});

	test("session_start recovery transitioning to stopped emits full notice and updates status", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const messages: Array<{ customType?: string; content?: string }> = [];
		const notifications: string[] = [];
		const statuses: Record<string, string> = {};
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (msg: { customType?: string; content?: string }) => {
				messages.push(msg);
			},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const repo = makeTempRepo();
		fixtureCaches.push(repo.dir, path.resolve(os.homedir(), ".omp", "agent", repo.cacheFile, ".."));
		const cwd = repo.dir;
		const head = repo.headSha;
		const exec = makeSnapshot("active", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], null);
		exec.items[0]!.initial_git_baseline = head;
		exec.items[0]!.current_git_baseline = head;
		exec.activeItem!.initial_git_baseline = head;
		exec.activeItem!.current_git_baseline = head;
		exec.grant.repository = cwd;
		const ownershipEntry = executionOwnershipEntry(exec, cwd, "OMP-176");

		const workItemView = () => ({
			work_id: "OMP-176",
			state: "IN_PROGRESS",
			project_id: null,
			revision: { revision_id: "rev-1" },
		});
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: key, key, title: `Test ${key}`, project: "Bookends" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			currentNow: async () => ({ id: "OMP-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			setExecutionState: async () => ({
				grant: { ...exec.grant, state: "stopped" as const, terminal_reason: "max_continuations_exceeded" },
			}),
			getPendingExecutionClaims: async () => [],
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => workItemView(),
				workflow: async () => ({ item: workItemView(), relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: identityExecutionWorkspaceManager,
		})(fakePi);

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			models: createAuditorTestModelQuery(),
			sessionManager: { getBranch: () => [ownershipEntry], getSessionId: () => "sess-1", getCwd: () => cwd },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const startHandlers = handlers.get("session_start") ?? [];
			for (const h of startHandlers) {
				await h({}, fakeCtx);
			}

			expect(notifications).toEqual(expect.arrayContaining([expect.stringContaining("Execution grant stopped: max_continuations_exceeded")]));
			expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (max_continuations_exceeded)");
			expect(statuses["work-now"]).toContain("0 completed, 2 skipped (of 2 items).");
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Grant is terminal; resume is impossible."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Next: /execute OMP-176 --queue"))).toBe(true);
		} finally {
			dirtySpy.mockRestore();
		}
	});
});

	function makeTempRepo(): { dir: string; cacheFile: string; headSha: string; cleanup: () => void } {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "service-refresh-test-"));
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "service-refresh-cache-"));
		spawnSync("git", ["init", "-b", "main"], { cwd: dir });
		spawnSync("git", ["config", "user.name", "Test"], { cwd: dir });
		spawnSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
		fs.writeFileSync(path.join(dir, ".work-project"), "The Bookends\n");
		fs.mkdirSync(path.join(dir, "python/omp-work/src/omp_work/v1"), { recursive: true });
		fs.writeFileSync(path.join(dir, "python/omp-work/src/omp_work/v1/store.py"), "# initial\n");
		const contractDir = path.join(dir, "python/omp-work/src/omp_work/contracts/v1");
		const realContractDir = path.resolve(import.meta.dir, "../../python/omp-work/src/omp_work/contracts/v1");
		fs.cpSync(realContractDir, contractDir, { recursive: true });
		// Disposable test repo approval follows candidate contract bytes; production
		// approval.json remains unchanged and is never used as fixture authority.
		fs.writeFileSync(
			path.join(contractDir, "approval.json"),
			JSON.stringify({ contract_version: "work.omp.dev/v1", contract_sha256: WORK_CONTRACT_SHA256, approved_by: "owner", approved_at: "2026-09-14T00:00:00Z", issue: "OMP-247" }) + "\n",
		);
		spawnSync("git", ["add", "."], { cwd: dir });
		spawnSync("git", ["commit", "-m", "initial commit"], { cwd: dir });
		const head = headCommit(dir) ?? "0".repeat(40);
		const cacheFile = path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "work-cache.json"));
		return {
			dir,
			cacheFile,
			headSha: head,
			cleanup: () => {
				fs.rmSync(dir, { recursive: true, force: true });
				fs.rmSync(cacheDir, { recursive: true, force: true });
			},
		};
	}

describe("service refresh during autonomous execution review (OMP-199)", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	afterEach(() => {
		vi.restoreAllMocks();
	});

	test.each([
		{ label: "legacy candidate refresh rebinds and restarts before audit and completion", allowRefresh: true },
		{ label: "pinned runtime reviews candidate Python edits without restarting service or rebinding grant", allowRefresh: false },
	])("$label", async ({ allowRefresh }) => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		// Modify sealed python file in real git repo
		fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified source\n");

		const callLog: string[] = [];
		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/execution/omp-199",
				state: "active",
				mode: "single",
				grant_version: 3,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "old-judge-sha-00000000000000000000000000000000000000000000000000000000",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (keyOrId: string) => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async (input: { grantId: string; expectedGrantVersion: number; targetState: string; reason?: string | null; judgeSha256: string }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				exec.grant.grant_version++;
				exec.grant.judge_sha256 = input.judgeSha256;
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async (_issue: unknown, kind: string) => {
				callLog.push(`appendEvidence:${kind}`);
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			recordCloseoutReview: async () => {
				callLog.push("recordCloseoutReview");
				return { status: "applied" };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: {
				healthReady: async () => {
					callLog.push("healthReady");
					return { ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } };
				},
				workflow: async () => ({
					receipts: [
						{ receipt_id: "verif-199", kind: "verification", payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "audit-199", kind: "audit", verdict: "PASS", independent: true, issuer: "work-service/auditor-settle", payload: { manifest_id: "man-199", launch_id: "launch-199" }, payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "receipt-199", kind: "push", payload: { repository: "theturtlecsz/oh-my-pi", remote_url: "https://github.com/theturtlecsz/oh-my-pi.git" }, payload_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", remote_ref: "refs/heads/execution/omp-199", remote_commit: "1".repeat(40) },
					],
					auditor_launches: [
						{ launch_id: "launch-199", tool_call_id: "call-1", task_sha256: "0".repeat(64), manifest_id: "man-199", attempt_id: "att-199" },
					],
					audit_manifest: {
						manifest_id: "man-199",
						manifest_version: 1,
						verification_receipt_id: "verif-199",
						task_sha256: "0".repeat(64),
						attempt_id: "att-199",
					},
					item: {
						work_id: "uuid-199",
						revision: { revision_id: "rev-199" },
						candidate: {
							candidate_id: "cand-199",
							candidate_sha256: "cand-sha",
							commit_sha: "1".repeat(40),
							kind: "final",
						},
					},
					close_attempts: [
						{ attempt_id: "att-199", candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", judge_sha256: exec.grant.judge_sha256 },
					],
				}),
			},
		} as unknown as WorkflowBackend;

		const restartMock = vi.fn(async () => {
			callLog.push("restartWorkService");
		});

		attachStageLaunchFixture(mockBackend, repo.dir);
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
			allowCandidateServiceRefresh: allowRefresh,
		})(fakePi);

		expect(registeredExecute).toBeDefined();

		mockDiscovery();
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: PASS\n(none)" }),
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
			} as executorModule.SingleResult;
		});

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/execution/omp-199", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "verifyMergeConfirmation").mockReturnValue({ confirmed: true, detail: "PR merged and origin/main contains candidate" });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		if (!allowRefresh) {
			exec.grant.judge_sha256 = (await computeAuditTcb(fakeCtx, mockBackend.workClient!)).judgeSha256;
		}
		const admittedJudge = exec.grant.judge_sha256;
		try {
			const res = await registeredExecute!("call-1", {
				action: "begin_execution_review",
				work: "OMP-199",
				body: "pytest passed: 53 passed",
			}, new AbortController().signal, () => {}, fakeCtx);

			expect(res.content[0]?.text).toContain("Execution grant completed");
			if (allowRefresh) {
				expect(callLog).toContain("setExecutionState:service_refresh");
				expect(callLog).toContain("restartWorkService");
			} else {
				expect(callLog).not.toContain("setExecutionState:service_refresh");
				expect(restartMock).not.toHaveBeenCalled();
				expect(exec.grant.judge_sha256).toBe(admittedJudge);
			}
			expect(callLog).toContain("healthReady");
			expect(callLog).toContain("finalizeExecutionCandidate");
			expect(callLog).toContain("beginCloseAttempt");
			expect(callLog).toContain("completeExecutionItem");

			// Verify exact call order: rebind -> restart -> post-restart health -> freeze -> beginCloseAttempt -> final health -> complete.
			const rebindIdx = callLog.indexOf("setExecutionState:service_refresh");
			const restartIdx = callLog.indexOf("restartWorkService");
			const initialHealthIdx = callLog.indexOf("healthReady");
			const postRestartHealthIdx = restartIdx >= 0 ? callLog.indexOf("healthReady", restartIdx + 1) : -1;
			const lastHealthIdx = callLog.lastIndexOf("healthReady");
			const freezeIdx = callLog.indexOf("finalizeExecutionCandidate");
			const attemptIdx = callLog.indexOf("beginCloseAttempt");
			const completeIdx = callLog.indexOf("completeExecutionItem");

			if (allowRefresh) {
				expect(rebindIdx).toBeLessThan(restartIdx);
				expect(restartIdx).toBeLessThan(postRestartHealthIdx);
			}
			expect(initialHealthIdx).toBeLessThan(freezeIdx);
			expect(freezeIdx).toBeLessThan(attemptIdx);
			expect(attemptIdx).toBeLessThan(lastHealthIdx);
			expect(lastHealthIdx).toBeLessThan(completeIdx);
		} finally {
			repo.cleanup();
		}
	});

	test("autonomous review cancels the reserved launch instead of settling when the auditor never starts (OMP-251)", async () => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		// Modify sealed python file in real git repo
		fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified source\n");

		const callLog: string[] = [];
		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-251",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 3,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "old-judge-sha-00000000000000000000000000000000000000000000000000000000",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-251",
					workspace_id: "ws-1",
					grant_id: "grant-251",
					work_id: "uuid-251",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-251" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-251",
				workspace_id: "ws-1",
				grant_id: "grant-251",
				work_id: "uuid-251",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-251" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-251", key: "OMP-251", title: "Test 251", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-251", attemptSnapshot: undefined }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async (input: { grantId: string; expectedGrantVersion: number; targetState: string; reason?: string | null; judgeSha256: string }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				exec.grant.grant_version++;
				exec.grant.judge_sha256 = input.judgeSha256;
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-251", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async (_issue: unknown, kind: string) => {
				callLog.push(`appendEvidence:${kind}`);
				return { receipt_id: "receipt-251" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-251", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-251" };
			},
			cancelAuditorLaunch: async (_key: string, launchId: string) => {
				callLog.push(`cancelAuditorLaunch:${launchId}`);
				return { status: "applied", event: { requiresDelivery: false, renderedText: "launch cancelled" } };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			recordCloseoutReview: async () => {
				callLog.push("recordCloseoutReview");
				return { status: "applied" };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-251", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({ receipts: [], auditor_launches: [], item: null, close_attempts: [] }),
			},
		} as unknown as WorkflowBackend;

		const restartMock = vi.fn(async () => {
			callLog.push("restartWorkService");
		});

		attachStageLaunchFixture(mockBackend, repo.dir, { callLog });
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		expect(registeredExecute).toBeDefined();

		mockDiscovery();
		// The auditor subprocess never dispatches a model request (requests: 0).
		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-251",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 1,
			output: "",
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 0,
			requests: 0,
			error: "transport dispatch failed",
		} as executorModule.SingleResult);

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/execution/omp-251", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "verifyMergeConfirmation").mockReturnValue({ confirmed: true, detail: "PR merged and origin/main contains candidate" });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-251");

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		try {
			const res = await registeredExecute!("call-1", {
				action: "begin_execution_review",
				work: "OMP-251",
				body: "pytest passed: 53 passed",
			}, new AbortController().signal, () => {}, fakeCtx);

			expect(res.content[0]?.text).toContain("Auditor launch failed before start: transport dispatch failed");
			expect(callLog).toContain("reserveStageLaunch");
			expect(callLog).toContain("cancelStageLaunch:launch-1");
			expect(callLog).not.toContain("settleStageLaunch");
			expect(callLog).not.toContain("completeExecutionItem");
		} finally {
			repo.cleanup();
		}
	});

	test("service refresh refusal cases perform zero freeze, push, or audit calls", async () => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		const freezeSpy = vi.spyOn(gitModule, "freezeCandidateCommit");
		const pushSpy = vi.spyOn(gitModule, "pushCandidate");

		const callLog: string[] = [];
		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 3,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "old-judge-sha-00000000000000000000000000000000000000000000000000000000",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		let shouldFailRestart = false;
		let healthFp = "prospective-fp-199";
		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async (input: { reason?: string | null; judgeSha256: string }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async () => {
				callLog.push("appendEvidence");
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: healthFp, judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({ receipts: [], auditor_launches: [], item: null, close_attempts: [] }),
			},
		} as unknown as WorkflowBackend;

		const restartMock = vi.fn(async () => {
			if (shouldFailRestart) throw new Error("systemctl restart failed");
			callLog.push("restartWorkService");
		});

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		expect(registeredExecute).toBeDefined();
		mockDiscovery();

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		try {
			// 1. Refusal: unsealed dirt in real repo
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified\n");
			fs.writeFileSync(path.join(repo.dir, "unsealed.txt"), "unsealed dirt\n");
			let res = await registeredExecute!("call-1", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("unsealed dirty paths");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 2. Refusal: migration dirt in real repo (only migration file dirty)
			fs.rmSync(path.join(repo.dir, "unsealed.txt"), { force: true });
			spawnSync("git", ["checkout", "--", "python/omp-work/src/omp_work/v1/store.py"], { cwd: repo.dir });
			fs.mkdirSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations"), { recursive: true });
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations/0024_test.sql"), "-- mig\n");
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await registeredExecute!("call-2", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("migrations directory contains changes");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 3. Refusal: non-service TCB drift (clean repo, mismatched judge)
			fs.rmSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations/0024_test.sql"), { force: true });
			spawnSync("git", ["checkout", "."], { cwd: repo.dir });
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await registeredExecute!("call-3", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("judge TCB drift");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 4. Refusal: restart failure
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified\n");
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			shouldFailRestart = true;
			res = await registeredExecute!("call-4", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("WorkService restart failed");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 5. Refusal: post-restart fingerprint mismatch
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			shouldFailRestart = false;
			let healthCallCount = 0;
			mockBackend.workClient!.healthReady = async () => {
				healthCallCount++;
				const fp = healthCallCount === 1 ? "prospective-fp-199" : "mismatched-fp-999";
				return { ready: true, contract_sha256: "contract-sha", service_fingerprint: fp, judge_manifest: { judge_sha256: "judge-sha" } };
			};
			res = await registeredExecute!("call-5", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("service_fingerprint mismatch");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 6. Refusal: preflight unit unloaded refusal
			let preflightRegisteredExecute: typeof registeredExecute;
			const preflightPi = {
				zod: z,
				registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
					if (spec.name === "work") preflightRegisteredExecute = spec.execute;
				},
				registerMessageRenderer: () => {},
				registerCommand: () => {},
				registerFlag: () => {},
				on: () => {},
				sendMessage: () => {},
				appendEntry: () => {},
			} as unknown as ExtensionAPI;
			createWorkflowHost({
				backend: mockBackend,
				teamNoun: "the ledger",
				entryType: "work-now",
				acceptEntry: () => true,
				preflightWorkService: async () => { throw new Error("omp-work-service.service is not loaded"); },
				restartWorkService: restartMock,
			})(preflightPi);
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await preflightRegisteredExecute!("call-6", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("omp-work-service.service is not loaded");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c.startsWith("setExecutionState"))).toHaveLength(0);
		} finally {
			repo.cleanup();
		}
	});

	test("execution delivery checkpoint race and crash-retry use one guarded continuation", async () => {
		const repo = makeTempRepo();
		let ownershipEntry: CustomEntry | undefined;
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		type ContinuationIdentity = { grantId: string; sessionId: string; workId: string; revisionId: string; preReservationVersion: number; postVersion: number; messageId: string };
		type OutboxRecord = ContinuationIdentity & { status: "pending" | "queued"; at: string };
		const sentMessages: Array<{ customType?: string; content?: string; details?: { executionContinuation?: ContinuationIdentity } }> = [];
		const appendedEntries: string[] = [];
		const appendedOutbox: OutboxRecord[] = [];
		// The checkpoint side of the seam: what the receipt-backed delivery API
		// received, and what the service was asked to attest for it.
		const deliveredCheckpoints: Array<{ customType?: string; content?: string }> = [];
		const attestations: Array<{ eventId: string; sessionId: string; renderedSha256: string; status: string }> = [];
		// One ordered log of everything that crosses the seam, in the order it
		// really happened: the installed journey's defect was purely one of order.
		const seam: string[] = [];
		const notifications: string[] = [];
		// The receipt-backed delivery API resolves only after real injection into
		// the transcript, which cannot happen before the tool turn yields (OMP-97).
		// The gate stands in for that yield: nothing that must follow the
		// checkpoint may be observable before the test releases it.
		let injection = Promise.withResolvers<void>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			getSessionId: () => "checkpoint-recovery-session",
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: (message: typeof sentMessages[number]) => {
				sentMessages.push(message);
				seam.push(`sendMessage:${message.customType}`);
			},
			deliverMessage: async (message: { customType?: string; content?: string }) => {
				deliveredCheckpoints.push(message);
				seam.push(`deliverMessage:${message.customType}`);
				await injection.promise;
				seam.push("injected");
			},
			appendEntry: (customType: string, data?: unknown) => {
				appendedEntries.push(customType);
				if (customType === "work-now-execute-outbox" && data && typeof data === "object") {
					appendedOutbox.push(data as OutboxRecord);
					seam.push(`outbox:${(data as OutboxRecord).status}`);
				}
			},
		} as unknown as ExtensionAPI;
		/** Let the settlement chain (attestation, then the guarded continuation) run to completion. */
		const settleDeliveries = async () => {
			for (let i = 0; i < 4; i++) await new Promise<void>(resolve => setTimeout(resolve, 0));
		};

		// Modify sealed python file
		fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified source\n");

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => ownershipEntry ? [ownershipEntry] : [], getSessionId: () => "checkpoint-recovery-session", getCwd: () => repo.dir },
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		mockDiscovery();
		const tcb = await computeAuditTcb(fakeCtx, {
			healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
		} as unknown as WorkClient);

		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/execution/omp-199",
				state: "active",
				mode: "single",
				grant_version: 4,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				// Grant already carries the new judge SHA (as if previous turn set it before crash)
				judge_sha256: tcb.judgeSha256,
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const callLog: string[] = [];
		ownershipEntry = executionOwnershipEntry(exec, repo.dir, "OMP-199");
		// The view shape the real backend hands to checkpoint delivery (CloseEventView).
		let pendingEvents = [
			{
				eventId: "ev-pending-1",
				eventType: "close_attempt_started",
				reasonCode: "started",
				renderedText: "close attempt started",
				renderedSha256: "0".repeat(64),
				requiresDelivery: true,
				requiresFreshAuthorization: false,
			},
		];
		let suppressCheckpointDelivery = false;
		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => pendingEvents,
			attestDelivery: async (eventId: string, sessionId: string, renderedSha256: string, status: string) => {
				attestations.push({ eventId, sessionId, renderedSha256, status });
				seam.push(`attestDelivery:${status}`);
				return { status: "applied", event: { eventId, eventType: "close_attempt_started", reasonCode: "delivery_attested", renderedText: "attested", renderedSha256, requiresDelivery: false, requiresFreshAuthorization: false } };
			},
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async (selector?: string) => {
				if (selector === exec.grant.grant_id) seam.push("refetch:grant");
				if (suppressCheckpointDelivery && selector === exec.grant.grant_id) {
					suppressCheckpointDelivery = false;
					return {
						...exec,
						grant: { ...exec.grant, state: "completed" },
						activeItem: null,
					};
				}
				return exec;
			},
			setExecutionState: async (input: { reason?: string | null }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async (_issue: unknown, kind: string) => {
				callLog.push(`appendEvidence:${kind}`);
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			recordCloseoutReview: async () => {
				callLog.push("recordCloseoutReview");
				return { status: "applied" };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({
					receipts: [
						{ receipt_id: "verif-199", kind: "verification", payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "audit-199", kind: "audit", verdict: "PASS", independent: true, issuer: "work-service/auditor-settle", payload: { manifest_id: "man-199", launch_id: "launch-199" }, payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "receipt-199", kind: "push", payload: { repository: "theturtlecsz/oh-my-pi", remote_url: "https://github.com/theturtlecsz/oh-my-pi.git" }, payload_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", remote_ref: "refs/heads/execution/omp-199", remote_commit: "1".repeat(40) },
					],
					auditor_launches: [
						{ launch_id: "launch-199", tool_call_id: "call-1", task_sha256: "0".repeat(64), manifest_id: "man-199", attempt_id: "att-199" },
					],
					audit_manifest: {
						manifest_id: "man-199",
						manifest_version: 1,
						verification_receipt_id: "verif-199",
						task_sha256: "0".repeat(64),
						attempt_id: "att-199",
					},
					item: {
						work_id: "uuid-199",
						revision: { revision_id: "rev-199" },
						candidate: {
							candidate_id: "cand-199",
							candidate_sha256: "cand-sha",
							commit_sha: "1".repeat(40),
							kind: "final",
						},
					},
					close_attempts: [
						{ attempt_id: "att-199", candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", judge_sha256: exec.grant.judge_sha256 },
					],
				}),
			},
		} as unknown as WorkflowBackend;
		let restartCount = 0;
		const restartMock = vi.fn(async () => {
			restartCount++;
			callLog.push("restartWorkService");
		});

		attachStageLaunchFixture(mockBackend, repo.dir);
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: PASS\n(none)" }),
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
			} as executorModule.SingleResult;
		});

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/execution/omp-199", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "verifyMergeConfirmation").mockReturnValue({ confirmed: true, detail: "PR merged and origin/main contains candidate" });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		try {
			// First run: the handler returns while the checkpoint is still in flight.
			const res1 = await registeredExecute!("call-1", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res1.content[0]?.text).toContain("queued for delivery");
			expect(res1.content[0]?.text).toContain("END YOUR TURN NOW");
			expect(res1.content[0]?.text).not.toContain("no execution continuation prompt was sent");
			// The result is the terminal marker the loop fences later same-response calls on (OMP-246).
			expect((res1 as unknown as { details?: { endTurn?: unknown } }).details).toMatchObject({ endTurn: true });
			expect(restartCount).toBe(1);
			expect(callLog.filter(c => c === "setExecutionState:service_refresh")).toHaveLength(1);
			expect(callLog).not.toContain("finalizeExecutionCandidate");
			expect(gitModule.dirtyPaths(repo.dir)).toContain("python/omp-work/src/omp_work/v1/store.py");
			// The installed journey's defect, stated as the negative: with the
			// checkpoint handed to the receipt-backed API but not yet injected, NO
			// continuation has been sent, NO outbox record exists, and the grant has
			// not been re-fetched for a send. The exact rendered text did reach the
			// delivery API before the handler returned.
			await settleDeliveries();
			expect(deliveredCheckpoints).toEqual([{ customType: "close-attempt-checkpoint", content: "close attempt started" }]);
			expect(attestations).toEqual([]);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(0);
			expect(seam).toEqual(["deliverMessage:close-attempt-checkpoint"]);

			// Re-entry while that checkpoint is still settling (the model calling
			// begin_execution_review again before the yield lands): the event is
			// deduped in flight and the pending schedule is joined, not doubled.
			const reentered = await registeredExecute!("call-1b", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(reentered.content[0]?.text).toContain("queued for delivery");
			expect((reentered as unknown as { details?: { endTurn?: unknown } }).details).toMatchObject({ endTurn: true });
			expect(restartCount).toBe(1);
			await settleDeliveries();
			expect(deliveredCheckpoints).toHaveLength(1);
			expect(attestations).toEqual([]);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			expect(seam).toEqual(["deliverMessage:close-attempt-checkpoint"]);

			// The turn yields: the checkpoint injects, the attestation returns, and
			// ONLY THEN is exactly one continuation sent through the guarded helper.
			injection.resolve();
			await settleDeliveries();
			expect(attestations).toEqual([{ eventId: "ev-pending-1", sessionId: "checkpoint-recovery-session", renderedSha256: "0".repeat(64), status: "delivered" }]);
			// Real order across the seam: injection, attestation, the grant re-fetch
			// at send time, the fail-closed pending record, the prompt, the queued record.
			expect(seam).toEqual([
				"deliverMessage:close-attempt-checkpoint",
				"injected",
				"attestDelivery:delivered",
				"refetch:grant",
				"outbox:pending",
				"sendMessage:work-execute",
				"outbox:queued",
			]);
			// Exactly one continuation for the two handler calls, bound to the
			// refreshed grant at its current version (no reservation is spent here,
			// so pre == post), this session, the active item, and its claimed revision.
			const continuations = sentMessages.filter(message => message.customType === "work-execute");
			expect(continuations).toHaveLength(1);
			const identity = continuations[0]?.details?.executionContinuation;
			expect(identity).toMatchObject({
				grantId: "grant-199",
				sessionId: "checkpoint-recovery-session",
				workId: "uuid-199",
				revisionId: "rev-1",
				preReservationVersion: exec.grant.grant_version,
				postVersion: exec.grant.grant_version,
			});
			expect(typeof identity?.messageId).toBe("string");
			// One paired outbox: the fail-closed pending record before the send, the
			// queued record after it, both carrying the delivered message id.
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(2);
			expect(appendedOutbox.map(record => record.status)).toEqual(["pending", "queued"]);
			expect(new Set(appendedOutbox.map(record => record.messageId))).toEqual(new Set([identity?.messageId]));
			expect(notifications.filter(text => text.includes("no execution continuation prompt was sent"))).toHaveLength(0);

			// Original production race: checkpoint queued while active, but grant is
			// completed at the delivery-seam re-fetch. That re-fetch now happens only
			// after the checkpoint settles, so while it is in flight nothing has been
			// decided: the suppression is unconsumed, no prompt, no outbox.
			sentMessages.length = 0;
			appendedEntries.length = 0;
			appendedOutbox.length = 0;
			seam.length = 0;
			injection = Promise.withResolvers<void>();
			suppressCheckpointDelivery = true;
			const suppressed = await registeredExecute!("call-2", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(suppressed.content[0]?.text).toContain("queued for delivery");
			expect((suppressed as unknown as { details?: { endTurn?: unknown } }).details).toMatchObject({ endTurn: true });
			await settleDeliveries();
			expect(suppressCheckpointDelivery).toBe(true);
			expect(seam).toEqual(["deliverMessage:close-attempt-checkpoint"]);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			// The checkpoint is still delivered and attested a second time; the
			// guarded send then sees the completed grant: no prompt, no outbox, and
			// the no-continuation outcome is surfaced as a notice instead.
			injection.resolve();
			await settleDeliveries();
			expect(suppressCheckpointDelivery).toBe(false);
			expect(deliveredCheckpoints).toHaveLength(2);
			expect(attestations).toHaveLength(2);
			expect(attestations[1]).toMatchObject({ eventId: "ev-pending-1", status: "delivered" });
			expect(seam).toEqual(["deliverMessage:close-attempt-checkpoint", "injected", "attestDelivery:delivered", "refetch:grant"]);
			expect(sentMessages.filter(message => message.customType === "work-execute")).toHaveLength(0);
			expect(appendedEntries.filter(type => type === "work-now-execute-outbox")).toHaveLength(0);
			expect(appendedOutbox).toEqual([]);
			expect(notifications.filter(text => text.includes("no execution continuation prompt was sent after checkpoint attestation"))).toHaveLength(1);

			// Third run: working tree is STILL dirty, but cached marker prevents a second refresh/restart.
			pendingEvents = [];
			const res2 = await registeredExecute!("call-3", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res2.content[0]?.text).toContain("Execution grant completed");
			expect(restartCount).toBe(1);
			expect(callLog.filter(c => c === "setExecutionState:service_refresh")).toHaveLength(1);
		} finally {
			repo.cleanup();
		}
	});
});

describe("execution grant admission branch selection (OMP-212)", () => {
	test("OMP233 admission rejects foreign candidate beneath ordinary HEAD before grant or session effects", async () => {
		const repo = makeTempRepo();
		const remoteDir = `${repo.dir}-origin.git`;
		fixtureCaches.push(remoteDir);
		try {
			const clone = gitModule.runGit(repo.dir, ["clone", "--bare", repo.dir, remoteDir]);
			if (!clone.ok) throw new Error(clone.err);
			const remote = gitModule.runGit(repo.dir, ["remote", "add", "origin", remoteDir]);
			if (!remote.ok) throw new Error(remote.err);
			await Bun.write(path.join(repo.dir, "foreign.txt"), "foreign candidate\n");
			await managedGit.stage.files(repo.dir, ["foreign.txt"]);
			await managedGit.commit(repo.dir, "session candidate: OMP-999\n\nWork-Candidate: 00000000-0000-7000-8000-000000000099");
			await Bun.write(path.join(repo.dir, "ordinary.txt"), "ordinary follow-up\n");
			await managedGit.stage.files(repo.dir, ["ordinary.txt"]);
			await managedGit.commit(repo.dir, "Ordinary follow-up");
			const originalHead = await managedGit.head.sha(repo.dir);
			const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
			const notifications: string[] = [];
			const appendEntry = vi.fn();
			const sendMessage = vi.fn();
			const beginExecution = vi.fn();
			const health = vi.fn();
			const primaryRoot = vi.fn();
			const ensure = vi.fn();
			const newSession = vi.fn();
			const checks = vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: false, count: 0, detail: "unexpected protection lookup" });
			const pi = {
				zod: z, logger: { warn: () => {} }, registerTool: () => {}, registerMessageRenderer: () => {}, registerFlag: () => {}, on: () => {},
				registerCommand: (name: string, definition: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => { commands.set(name, definition.handler); },
				appendEntry, sendMessage, getSessionId: () => "admission-session",
			} as unknown as ExtensionAPI;
			const backend = {
				cacheFile: repo.cacheFile, markerFile: ".work-project", evidenceKinds: [], beginExecution,
				findIssue: async () => ({ id: "work-233", key: "OMP-233", title: "Next child" }),
				workClient: { healthReady: health, workItem: async () => ({ work_id: "work-233", revision: { revision_id: "rev-233", description: "Next child" } }), workflow: async () => ({ relations: [] }) },
			} as unknown as WorkflowBackend;
			attachStageLaunchFixture(backend, repo.dir);
			createWorkflowHost({ backend, teamNoun: "ledger", entryType: "work-now", acceptEntry: () => true, executionWorkspaceManager: { primaryRoot, ensure, cleanup: vi.fn() } })(pi);
			const context = { cwd: repo.dir, taskDepth: 0, models: createAuditorTestModelQuery(), newSession, ui: { notify: (text: string) => notifications.push(text) } } as unknown as ExtensionContext;
			await commands.get("execute")!("OMP-233", context);
			expect(notifications.join("\n")).toContain("OMP-999");
			expect(notifications.join("\n")).toContain("Cannot begin execution");
			for (const effect of [checks, beginExecution, health, primaryRoot, ensure, newSession, appendEntry, sendMessage]) expect(effect).not.toHaveBeenCalled();
			expect(await managedGit.head.sha(repo.dir)).toBe(originalHead);
			expect(gitModule.dirtyPaths(repo.dir)).toEqual([]);
		} finally { repo.cleanup(); }
	});

	test("binds dedicated execution branch ref when starting on default branch main", async () => {
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			appendEntry: () => {},
			sendMessage: () => {},
			getSessionId: () => "sess-1",
		} as unknown as ExtensionAPI;

		let capturedRemoteRef: string | undefined;
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-1",
			pendingDeliveries: async () => [],
			findIssue: async (_keyOrId: string) => ({ id: "uuid-212", key: "OMP-212", title: "Test 212", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-212", attemptSnapshot: undefined }),
			workflowState: async () => ({ open_blockers: [] }),
			getFocusVersion: async () => 1,
			beginExecution: async (input: { remoteRef: string }) => {
				capturedRemoteRef = input.remoteRef;
				return {
					grant: {
						grant_id: "grant-212",
						grant_version: 1,
						remote_ref: input.remoteRef,
						state: "active",
					},
					items: [],
					activeItem: null,
				};
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({
					work_id: "uuid-212",
					project_id: "proj-1",
					revision: {
						revision_id: "rev-212",
						description: "desc",
					},
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: identityExecutionWorkspaceManager,
		})(fakePi);

		const handler = registeredCommands.get("execute");
		expect(handler).toBeDefined();

		// Mock git operations
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		const headSpy = vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
		const refSpy = vi.spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/main");
		const upToDateSpy = vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		const checksSpy = vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 12, detail: "12 required status check context(s) on main" });
		try {
			const fakeCtx = {
				cwd: "/tmp/repo",
				taskDepth: 0,
				models: createAuditorTestModelQuery(),
				// Branch-routing fixture uses a nonpersistent SDK frame; native disk tests live separately.
				newSession: async (options: Parameters<ExtensionCommandContext["newSession"]>[0]) => {
					await options?.setup?.(SessionManager.inMemory("/tmp/repo"));
					return { cancelled: false };
				},
				ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			} as unknown as ExtensionContext;

			await handler!("OMP-212", fakeCtx);
			expect(capturedRemoteRef).toBe("refs/heads/execution/omp-212");
		} finally {
			dirtySpy.mockRestore();
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
		}
	});

	test("linked-worktree admission seals primary repository through audit and master completion", async () => {
		const repo = makeTempRepo();
		fixtureCaches.push(repo.dir, path.resolve(os.homedir(), ".omp", "agent", repo.cacheFile, ".."));
		const linked = `${repo.dir}-linked`;
		fixtureCaches.push(linked);
		await managedGit.branch.create(repo.dir, "execution/omp-212", repo.headSha);
		await managedGit.worktree.add(repo.dir, linked, "execution/omp-212");
		await Bun.write(path.join(linked, "test.txt"), "candidate only on managed execution branch\n");
		await managedGit.stage.files(linked, ["test.txt"]);
		await managedGit.commit(linked, "Create isolated audit candidate");
		const candidate = await managedGit.head.sha(linked);
		if (!candidate) throw new Error("Linked candidate commit missing");
		const inputCwd = path.join(linked, "python/omp-work/src");
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		let registeredExecuteTool: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: (update: unknown) => void, ctx: ExtensionContext) => Promise<{ content: Array<{ type: "text"; text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (def: { name: string; execute: typeof registeredExecuteTool }) => {
				if (def.name === "work") registeredExecuteTool = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			appendEntry: () => {},
			sendMessage: () => {},
			getSessionId: () => "sess-master",
		} as unknown as ExtensionAPI;

		let capturedRemoteRef: string | undefined;
		let verifyMergeCalledWith: { remoteRef: string; defaultBranch?: string } | undefined;
		let completedItemCalled = false;
		let pushRepository: unknown;
		let attemptRepository: string | undefined;

		const mockExec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-master",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: "/tmp/repo",
				remote_ref: "refs/heads/execution/omp-212",
				state: "active",
				mode: "single",
				grant_version: 1,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-master",
				judge_sha256: "0".repeat(64),
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-master",
					workspace_id: "ws-1",
					grant_id: "grant-master",
					work_id: "uuid-master",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-master",
					original_request: "test request master",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["test.txt"], candidate_id: "cand-master" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: "1".repeat(40),
					current_git_baseline: "1".repeat(40),
				},
			],
			activeItem: {
				item_id: "item-master",
				workspace_id: "ws-1",
				grant_id: "grant-master",
				work_id: "uuid-master",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-master",
				original_request: "test request master",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["test.txt"], candidate_id: "cand-master" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: "1".repeat(40),
				current_git_baseline: "1".repeat(40),
			},
		};

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-1",
			pendingDeliveries: async () => [],
			findIssue: async (_keyOrId: string) => ({ id: "uuid-master", key: "OMP-212", title: "Test Master", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-212", attemptSnapshot: undefined }),
			workflowState: async () => ({ open_blockers: [] }),
			getFocusVersion: async () => 1,
			beginExecution: async (input: { provenance: ExecutionProvenanceEnvelope; remoteRef: string; judgeSha256?: string }) => {
				capturedRemoteRef = input.remoteRef;
				mockExec.grant.repository = input.provenance.repository;
				mockExec.grant.remote_ref = input.remoteRef;
				if (input.judgeSha256) mockExec.grant.judge_sha256 = input.judgeSha256;
				return mockExec;
			},
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => mockExec,
			finalizeExecutionCandidate: async () => ({
				candidate_id: "cand-master",
				candidate_sha256: "cand-sha",
				commit_sha: "2".repeat(40),
			}),
			appendEvidence: async (_issue: unknown, kind: string, _body: string, metadata: { repository?: string }) => {
				if (kind === "push") pushRepository = metadata.repository;
				return { receipt_id: "receipt-master" };
			},
			beginCloseAttempt: async (_issue: unknown, session: CloseAttemptSession) => {
				attemptRepository = session.repository;
				return { status: "applied", attemptId: "att-master", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => ({ status: "applied" }),
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: JSON.stringify({ repository: attemptRepository, start: repo.headSha, final: candidate }) }),
			reserveAuditorLaunch: async () => ({ status: "reserved", launchId: "launch-master" }),
			settleAuditorLaunch: async () => ({ verdict: "PASS", event: { renderedText: "PASS" } }),
			recordCloseoutReview: async () => ({ status: "applied" }),
			completeExecutionItem: async () => {
				completedItemCalled = true;
				mockExec.activeItem!.phase = "completed";
				return mockExec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: mockExec.grant.judge_sha256 } }),
				workItem: async () => ({
					work_id: "uuid-master",
					project_id: "proj-1",
					repository_id: "uuid-master",
					revision: {
						revision_id: "rev-master",
						description: "desc",
					},
				}),
				workflow: async () => ({
					receipts: [
						{ receipt_id: "verif-1", kind: "verification", payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-master", revision_id: "rev-master", work_id: "uuid-master" },
						{ receipt_id: "audit-1", kind: "audit", verdict: "PASS", independent: true, issuer: "work-service/auditor-settle", payload: { manifest_id: "man-1", launch_id: "launch-master" }, payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-master", revision_id: "rev-master", work_id: "uuid-master" },
						{ receipt_id: "receipt-master", kind: "push", payload: { repository: "theturtlecsz/oh-my-pi", remote_url: "https://github.com/theturtlecsz/oh-my-pi.git" }, payload_sha256: "0".repeat(64), candidate_id: "cand-master", revision_id: "rev-master", work_id: "uuid-master", remote_ref: "refs/heads/execution/omp-212", remote_commit: "2".repeat(40) },
					],
					auditor_launches: [
						{ launch_id: "launch-master", tool_call_id: "call-1", task_sha256: "0".repeat(64), manifest_id: "man-1", attempt_id: "att-master" },
					],
					audit_manifest: {
						manifest_id: "man-1",
						manifest_version: 1,
						verification_receipt_id: "verif-1",
						task_sha256: "0".repeat(64),
						attempt_id: "att-master",
					},
					item: {
						work_id: "uuid-master",
						revision: { revision_id: "rev-master" },
						candidate: {
							candidate_id: "cand-master",
							candidate_sha256: "cand-sha",
							commit_sha: "2".repeat(40),
							kind: "final",
						},
					},
					close_attempts: [
						{ attempt_id: "att-master", candidate_id: "cand-master", revision_id: "rev-master", work_id: "uuid-master", judge_sha256: mockExec.grant.judge_sha256 },
					],
				}),
			},
		} as unknown as WorkflowBackend;

		attachStageLaunchFixture(mockBackend, "uuid-master");
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: { ...identityExecutionWorkspaceManager, primaryRoot: gitModule.executionPrimaryRoot },
		})(fakePi);

		const handler = registeredCommands.get("execute");
		expect(handler).toBeDefined();
		expect(registeredExecuteTool).toBeDefined();

		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		const headSpy = vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
		const refSpy = vi.spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/master");
		const upToDateSpy = vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		const checksSpy = vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 12, detail: "12 required status check context(s) on master" });
		const freezeSpy = vi.spyOn(gitModule, "freezeCandidateCommit").mockResolvedValue({
			commitSha: "2".repeat(40),
			treeSha: "tree-sha",
		} as any);
		const pushSpy = vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({
			status: "pushed",
			remoteRef: "refs/heads/execution/omp-212",
			remoteCommit: "2".repeat(40),
			priorTip: "1".repeat(40),
		});
		const verifySpy = vi.spyOn(gitModule, "verifyMergeConfirmation").mockImplementation((_root, _commit, remoteRef, defaultBranch) => {
			verifyMergeCalledWith = { remoteRef, defaultBranch };
			return { confirmed: true, detail: "PR merged to master" };
		});
		const rangeDiffSpy = vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-master");

		const discoverSpy = vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [{
				name: "auditor",
				description: "Auditor agent",
				systemPrompt: "Audit prompt",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "bundled",
			}],
			projectAgentsDir: null,
		});
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: any) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-master",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: PASS\n(none)" }),
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
			} as executorModule.SingleResult;
		});

		try {
			const fakeCtx = {
				cwd: inputCwd,
				taskDepth: 0,
				newSession: async (options: Parameters<ExtensionCommandContext["newSession"]>[0]) => {
					await options?.setup?.(SessionManager.inMemory(inputCwd));
					return { cancelled: false };
				},
				sessionManager: { getBranch: () => [] },
				models: createAuditorTestModelQuery(),
				modelRegistry: { getApiKey: () => Promise.resolve("key") },
				ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			} as unknown as ExtensionContext;

			// 1. Admission on master
			await handler!("OMP-212", fakeCtx);
			expect(capturedRemoteRef).toBe("refs/heads/execution/omp-212");

			// 2. Review and completion
			const res = await registeredExecuteTool!("call-master", {
				action: "begin_execution_review",
				work: "OMP-212",
				body: "verification passed",
			}, new AbortController().signal, () => {}, fakeCtx);

			expect(res.content[0]?.text).toContain("Execution grant completed");
			expect(verifyMergeCalledWith).toEqual({
				remoteRef: "refs/heads/execution/omp-212",
				defaultBranch: "refs/heads/master",
			});
			expect(completedItemCalled).toBe(true);
			const sealed = JSON.parse(runSubprocessSpy.mock.calls[0][0].task) as { repository: string; start: string; final: string };
			const canonicalRepository = await fs.promises.realpath(repo.dir);
			expect({ admission: mockExec.grant.repository, push: pushRepository, attempt: attemptRepository, auditor: sealed.repository }).toEqual({ admission: canonicalRepository, push: canonicalRepository, attempt: canonicalRepository, auditor: canonicalRepository });
			expect(await managedGit.head.sha(sealed.repository)).toBe(repo.headSha);
			expect((await managedGit.commitDetails(sealed.repository, sealed.start)).sha).toBe(repo.headSha);
			expect((await managedGit.commitDetails(sealed.repository, sealed.final)).sha).toBe(candidate);
			expect(await managedGit.diff(sealed.repository, { base: sealed.start, head: sealed.final })).toContain("+candidate only on managed execution branch");
		} finally {
			dirtySpy.mockRestore();
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
			freezeSpy.mockRestore();
			pushSpy.mockRestore();
			verifySpy.mockRestore();
			rangeDiffSpy.mockRestore();
			runSubprocessSpy.mockRestore();
			discoverSpy.mockRestore();
		}
	});

	// OMP-220: admission gates — behind-origin HEAD and empty required-check
	// config are refused at admission, before any grant is minted.
	test("refuses admission when HEAD is behind the origin default tip", async () => {
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			appendEntry: () => {},
			sendMessage: () => {},
			getSessionId: () => "sess-220a",
		} as unknown as ExtensionAPI;
		let beginCalled = false;
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-1",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-220", key: "OMP-220", title: "Test 220", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-220", attemptSnapshot: undefined }),
			workflowState: async () => ({ open_blockers: [] }),
			getFocusVersion: async () => 1,
			beginExecution: async () => {
				beginCalled = true;
				throw new Error("must not begin");
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({
					work_id: "uuid-220",
					project_id: "proj-1",
					revision: { revision_id: "rev-220", description: "desc" },
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;
		createWorkflowHost({ backend: mockBackend, teamNoun: "the ledger", entryType: "work-now", acceptEntry: () => true })(fakePi);
		const handler = registeredCommands.get("execute");
		expect(handler).toBeDefined();
		const notifications: string[] = [];
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		const headSpy = vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
		const refSpy = vi.spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/main");
		const upToDateSpy = vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({
			ok: false,
			detail: "HEAD is behind origin/main tip abcabcabcabc — run `git merge origin/main` (or pull) and retry so PASS candidates stay conflict-free",
		});
		const checksSpy = vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 12, detail: "12" });
		try {
			const fakeCtx = {
				cwd: "/tmp/repo",
				taskDepth: 0,
				ui: { notify: (msg: string) => notifications.push(msg), theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			} as unknown as ExtensionContext;
			await handler!("OMP-220", fakeCtx);
			expect(beginCalled).toBe(false);
			expect(notifications.some(n => n.includes("HEAD is behind origin/main")), `notifications: ${JSON.stringify(notifications)}`).toBe(true);
		} finally {
			dirtySpy.mockRestore();
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
		}
	});

	test("refuses admission when branch protection has zero required status checks", async () => {
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			appendEntry: () => {},
			sendMessage: () => {},
			getSessionId: () => "sess-220b",
		} as unknown as ExtensionAPI;
		let beginCalled = false;
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-1",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-220", key: "OMP-220", title: "Test 220", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-220", attemptSnapshot: undefined }),
			workflowState: async () => ({ open_blockers: [] }),
			getFocusVersion: async () => 1,
			beginExecution: async () => {
				beginCalled = true;
				throw new Error("must not begin");
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({
					work_id: "uuid-220",
					project_id: "proj-1",
					revision: { revision_id: "rev-220", description: "desc" },
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;
		createWorkflowHost({ backend: mockBackend, teamNoun: "the ledger", entryType: "work-now", acceptEntry: () => true })(fakePi);
		const handler = registeredCommands.get("execute");
		expect(handler).toBeDefined();
		const notifications: string[] = [];
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		const headSpy = vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
		const refSpy = vi.spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/main");
		const upToDateSpy = vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		const checksSpy = vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 0, detail: "0 required status check context(s) on main" });
		try {
			const fakeCtx = {
				cwd: "/tmp/repo",
				taskDepth: 0,
				ui: { notify: (msg: string) => notifications.push(msg), theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			} as unknown as ExtensionContext;
			await handler!("OMP-220", fakeCtx);
			expect(beginCalled).toBe(false);
			expect(notifications.some(n => n.includes("no required status checks")), `notifications: ${JSON.stringify(notifications)}`).toBe(true);
		} finally {
			dirtySpy.mockRestore();
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
		}
	});
});

describe("dead execution context and terminal work suppression (OMP-247)", () => {
	test("execute admission immediately refuses terminal or archived target", async () => {
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (cmd: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(cmd, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		let beginCalled = false;
		const headSpy = vi.spyOn(gitModule, "headCommit");
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths");
		const inProgSpy = vi.spyOn(gitModule, "inProgressGitOp");
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-1",
			findIssue: async () => ({
				id: "uuid-247",
				key: "OMP-247",
				title: "Test 247",
				project: "The Bookends",
				state: "DONE",
			}),
			beginExecution: async () => {
				beginCalled = true;
				throw new Error("must not begin");
			},
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: "contract-sha",
					service_fingerprint: "fp",
					judge_manifest: { judge_sha256: "judge-sha" },
				}),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);
		const handler = registeredCommands.get("execute");
		expect(handler).toBeDefined();

		const notifications: string[] = [];
		try {
			const fakeCtx = {
				cwd: "/tmp/non-git-repo",
				taskDepth: 0,
				ui: {
					notify: (msg: string) => notifications.push(msg),
					theme: { fg: (_c: string, t: string) => t },
					setStatus: () => {},
				},
			} as unknown as ExtensionContext;

			await handler!("OMP-247", fakeCtx);
			expect(beginCalled).toBe(false);
			expect(headSpy).not.toHaveBeenCalled();
			expect(dirtySpy).not.toHaveBeenCalled();
			expect(inProgSpy).not.toHaveBeenCalled();
			expect(notifications.some(n => n.includes("closed work can't be NOW"))).toBe(true);
		} finally {
			headSpy.mockRestore();
			dirtySpy.mockRestore();
			inProgSpy.mockRestore();
		}
	});

	test("session_start with dead execution grant cleans stale state", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<unknown>>>();
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<unknown>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "dead-grant-repo-"));
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "dead-grant-cache-"));
		spawnSync("git", ["init", "-b", "main"], { cwd });
		fs.writeFileSync(path.join(cwd, "seed.txt"), "seed\n");
		spawnSync("git", ["add", "."], { cwd });
		spawnSync("git", ["commit", "-m", "seed"], { cwd });

		const cacheRelPath = path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "cache.json"));
		const cacheFullPath = path.join(os.homedir(), ".omp", "agent", cacheRelPath);
		fs.mkdirSync(path.dirname(cacheFullPath), { recursive: true });
		fs.writeFileSync(
			cacheFullPath,
			JSON.stringify({
				issueId: "uuid-247",
				identifier: "OMP-247",
				executingIssue: { id: "uuid-247", key: "OMP-247", title: "Test 247" },
				executionWorkspace: { grantId: "grant-1", key: "OMP-247", directory: cwd, branch: "execution/omp-247" },
				obligationHandoff: { armed: true },
			}),
		);

		const mockBackend = {
			cacheFile: cacheRelPath,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			findIssue: async () => ({ id: "uuid-247", key: "OMP-247", title: "Test 247", project: "The Bookends", state: "DONE" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => null,
			currentNow: async () => ({ id: "uuid-247", key: "OMP-247", title: "Test 247" }),
			projectScopeExists: async () => true,
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const sessionManager = SessionManager.inMemory(cwd);
		vi.spyOn(fakePi, "getSessionId").mockImplementation(() => sessionManager.getSessionId());
		const fakeCtx = {
			cwd,
			taskDepth: 0,
			sessionManager,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
		} as unknown as ExtensionContext;

		const startHandlers = handlers.get("session_start") ?? [];
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		const savedCache = JSON.parse(fs.readFileSync(cacheFullPath, "utf8")) as Record<string, unknown>;
		expect(savedCache.executingIssue).toBeUndefined();
		expect(savedCache.executionWorkspace).toBeUndefined();
		expect(savedCache.obligationHandoff).toBeUndefined();
	});

	test("session_stop with dead execution grant or terminal work emits no continuation", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<unknown>>>();
		const sentMessages: unknown[] = [];
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<unknown>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (msg: unknown) => {
				sentMessages.push(msg);
			},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "dead-stop-repo-"));
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "dead-stop-cache-"));
		spawnSync("git", ["init", "-b", "main"], { cwd });
		fs.writeFileSync(path.join(cwd, "seed.txt"), "seed\n");
		spawnSync("git", ["add", "."], { cwd });
		spawnSync("git", ["commit", "-m", "seed"], { cwd });

		const cacheRelPath = path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "cache.json"));
		const cacheFullPath = path.join(os.homedir(), ".omp", "agent", cacheRelPath);
		fs.mkdirSync(path.dirname(cacheFullPath), { recursive: true });
		fs.writeFileSync(
			cacheFullPath,
			JSON.stringify({
				issueId: "uuid-247",
				identifier: "OMP-247",
				executingIssue: { id: "uuid-247", key: "OMP-247", title: "Test 247" },
				executionWorkspace: { grantId: "grant-1", key: "OMP-247", directory: cwd, branch: "execution/omp-247" },
				obligationHandoff: { armed: true },
			}),
		);

		let activeGrant: ExecutionSnapshot | null = makeSnapshot("active", "single", [{ position: 0, work_id: "uuid-247", phase: "executing" }]);
		activeGrant.grant.grant_id = "grant-1";

		const mockBackend = {
			cacheFile: cacheRelPath,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			findIssue: async () => ({ id: "uuid-247", key: "OMP-247", title: "Test 247", project: "The Bookends", state: "DONE" }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => activeGrant,
			currentNow: async () => ({ id: "uuid-247", key: "OMP-247", title: "Test 247" }),
			projectScopeExists: async () => true,
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const sessionManager = SessionManager.inMemory(cwd);
		vi.spyOn(fakePi, "getSessionId").mockImplementation(() => sessionManager.getSessionId());
		const fakeCtx = {
			cwd,
			taskDepth: 0,
			sessionManager,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
		} as unknown as ExtensionContext;

		// 1. session_start runs while grant is active
		const startHandlers = handlers.get("session_start") ?? [];
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		// 2. Before session_stop, grant becomes dead / work becomes terminal
		activeGrant = null;

		const stopHandlers = handlers.get("session_stop") ?? [];
		expect(stopHandlers.length).toBeGreaterThan(0);

		let stopResult: unknown;
		for (const h of stopHandlers) {
			const res = await h({ stop_hook_active: false }, fakeCtx);
			if (res !== undefined) stopResult = res;
		}

		expect(stopResult).toBeUndefined();
		expect(sentMessages.length).toBe(0);

		const savedCache = JSON.parse(fs.readFileSync(cacheFullPath, "utf8")) as Record<string, unknown>;
		expect(savedCache.executingIssue).toBeUndefined();
		expect(savedCache.executionWorkspace).toBeUndefined();
		expect(savedCache.obligationHandoff).toBeUndefined();
	});
});

describe("native audit launch attribution parent-session entry (OMP-275 / M2-A)", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	async function setupM2Fixture(options: {
		onReserve?: () => { status: string; launchId: string };
	} = {}) {
		const repo = makeTempRepo();
		const sessionDir = fs.mkdtempSync(path.join(os.tmpdir(), "m2-sessions-"));
		const sessionManager = SessionManager.create(repo.dir, sessionDir);
		await sessionManager.ensureOnDisk();
		const sessionFile = sessionManager.getSessionFile()!;

		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const sentMessages: Array<{ customType?: string; content?: string }> = [];
		let currentPiSessionId: string | undefined;

		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			getSessionId: () => currentPiSessionId ?? sessionManager.getSessionId(),
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: (message: { customType?: string; content?: string }) => { sentMessages.push(message); },
			appendEntry: (customType: string, data?: unknown) => {
				sessionManager.appendCustomEntry(customType, data);
			},
		} as unknown as ExtensionAPI;

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		mockDiscovery();
		const tcb = await computeAuditTcb(fakeCtx, {
			healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
		} as unknown as WorkClient);

		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/execution/omp-199",
				state: "active",
				mode: "single",
				grant_version: 4,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: tcb.judgeSha256,
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const ownershipEntry = executionOwnershipEntry(exec, repo.dir, "OMP-199");
		sessionManager.appendCustomEntry(ownershipEntry.customType, ownershipEntry.data);

		const settleCalls: Array<{ work: unknown; launchId: unknown; payload: unknown }> = [];
		const cancelCalls: Array<{ work: unknown; launchId: unknown }> = [];
		let reserveIndex = 0;

		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async () => exec,
			finalizeExecutionCandidate: async () => ({ candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) }),
			appendEvidence: async () => ({ receipt_id: "receipt-199" }),
			beginCloseAttempt: async () => ({ status: "applied", attemptId: "att-199", event: { requiresDelivery: false } }),
			sealAuditManifest: async () => ({ status: "applied" }),
			sealedAuditTask: async () => ({ taskSha256: "task-sha-275", taskBody: "task body 275" }),
			reserveAuditorLaunch: async () => {
				reserveIndex++;
				if (options.onReserve) return options.onReserve();
				return { status: "reserved", launchId: `launch-${reserveIndex}` };
			},
			settleAuditorLaunch: async (work: unknown, launchId: unknown, payload: unknown) => {
				settleCalls.push({ work, launchId, payload });
				const payloadStr = typeof payload === "object" && payload !== null && "payload" in payload ? String((payload as { payload: unknown }).payload) : "";
				const verdict = payloadStr.includes("NEEDS_FIX") ? "NEEDS_FIX" : "PASS";
				return { verdict, event: { renderedText: verdict } };
			},
			cancelAuditorLaunch: async (work: unknown, launchId: unknown) => {
				cancelCalls.push({ work, launchId });
				return { status: "applied", event: { requiresDelivery: false } };
			},
			recordCloseoutReview: async () => ({ status: "applied" }),
			completeExecutionItem: async () => {
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({
					receipts: [
						{ receipt_id: "verif-199", kind: "verification", payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "audit-199", kind: "audit", verdict: "PASS", independent: true, issuer: "work-service/auditor-settle", payload: { manifest_id: "man-199", launch_id: "launch-1" }, payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "receipt-199", kind: "push", payload: { repository: "theturtlecsz/oh-my-pi", remote_url: "https://github.com/theturtlecsz/oh-my-pi.git" }, payload_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", remote_ref: "refs/heads/execution/omp-199", remote_commit: "1".repeat(40) },
					],
					auditor_launches: [
						{ launch_id: "launch-1", tool_call_id: "call-1", task_sha256: "0".repeat(64), manifest_id: "man-199", attempt_id: "att-199" },
					],
					audit_manifest: {
						manifest_id: "man-199",
						manifest_version: 1,
						verification_receipt_id: "verif-199",
						task_sha256: "0".repeat(64),
						attempt_id: "att-199",
					},
					item: {
						work_id: "uuid-199",
						revision: { revision_id: "rev-199" },
						candidate: {
							candidate_id: "cand-199",
							candidate_sha256: "cand-sha",
							commit_sha: "1".repeat(40),
							kind: "final",
						},
					},
					close_attempts: [
						{ attempt_id: "att-199", candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", judge_sha256: exec.grant.judge_sha256 },
					],
				}),
			},
		} as unknown as WorkflowBackend;

		attachStageLaunchFixture(mockBackend, repo.dir);
		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/execution/omp-199", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "verifyMergeConfirmation").mockReturnValue({ confirmed: true, detail: "PR merged and origin/main contains candidate" });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		return {
			repo: {
				...repo,
				cleanup: () => {
					fs.rmSync(sessionDir, { recursive: true, force: true });
					repo.cleanup();
				},
			},
			sessionManager,
			sessionFile,
			sessionDir,
			fakeCtx,
			fakePi,
			exec,
			settleCalls,
			cancelCalls,
			getRegisteredExecute: () => registeredExecute!,
			setPiSessionId: (id: string | undefined) => {
				currentPiSessionId = id;
			},
		};
	}

	test("persisted consumer contract after reload binds launch to selector attribution and payload sha", async () => {
		const f = await setupM2Fixture();
		const mockOutput = JSON.stringify({ report: "VERDICT: PASS\n(all clean)" });
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-sol:medium",
				resolvedModelIsFallback: false,
			} as executorModule.SingleResult;
		});

		try {
			const res = await f.getRegisteredExecute()("call-1", { action: "begin_execution_review", work: "OMP-199", body: "verification evidence" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("Execution grant completed");

			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(1);
			const data = launchEntries[0].data as Record<string, unknown>;
			expect(data.launch_id).toBe("launch-1");
			expect(data.attempt_id).toBe("att-199");
			expect(data.task_sha256).toBe("task-sha-275");
			expect(data.payload_sha256).toBe(sha256Hex(mockOutput));
			expect(data.resolvedModel).toBe("openai-codex/gpt-5.6-sol:medium");
			expect(data.resolvedModelIsFallback).toBe(false);
			expect(data.attribution).toBe("session-reported-selector");
			expect(data.session_id).toBe(f.sessionManager.getSessionId());
			expect(typeof data.at).toBe("string");

			// Acceptance criterion 5: Entry never feeds settleAuditorLaunch transport payload or verdict
			expect(f.settleCalls).toHaveLength(1);
			expect(f.settleCalls[0].payload).toEqual({ payload: mockOutput });
			expect("resolvedModel" in (f.settleCalls[0].payload as Record<string, unknown>)).toBe(false);
			expect("resolvedModelIsFallback" in (f.settleCalls[0].payload as Record<string, unknown>)).toBe(false);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("unknown fields remain absent on reload when runner omits model attribution", async () => {
		const f = await setupM2Fixture();
		const mockOutput = JSON.stringify({ report: "VERDICT: PASS\n(clean)" });
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
			} as executorModule.SingleResult;
		});

		try {
			const res = await f.getRegisteredExecute()("call-2", { action: "begin_execution_review", work: "OMP-199", body: "verification evidence" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("Execution grant completed");

			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(1);
			const data = launchEntries[0].data as Record<string, unknown>;
			expect("resolvedModel" in data).toBe(false);
			expect("resolvedModelIsFallback" in data).toBe(false);
			expect(data.launch_id).toBe("launch-1");
			expect(data.attempt_id).toBe("att-199");
			expect(data.task_sha256).toBe("task-sha-275");
			expect(data.payload_sha256).toBe(sha256Hex(mockOutput));
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("multiple launches under same attempt record distinct reloaded entries keyed by launch_id", async () => {
		let launchCounter = 0;
		const f = await setupM2Fixture({
			onReserve: () => {
				launchCounter++;
				return { status: "reserved", launchId: `launch-${launchCounter}` };
			},
		});
		const mockOutput = JSON.stringify({ report: "VERDICT: NEEDS_FIX\nFinding 1" });
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-sol:medium",
				resolvedModelIsFallback: false,
			} as executorModule.SingleResult;
		});

		try {
			// First launch returns NEEDS_FIX
			const res1 = await f.getRegisteredExecute()("call-3a", { action: "begin_execution_review", work: "OMP-199", body: "evidence 1" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res1.content[0]?.text).toContain("Finding 1");

			// Second launch under same attempt
			const res2 = await f.getRegisteredExecute()("call-3b", { action: "begin_execution_review", work: "OMP-199", body: "evidence 2" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res2.content[0]?.text).toContain("Finding 1");

			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(2);
			const data1 = launchEntries[0].data as Record<string, unknown>;
			const data2 = launchEntries[1].data as Record<string, unknown>;
			expect(data1.launch_id).toBe("launch-1");
			expect(data2.launch_id).toBe("launch-2");
			expect(data1.attempt_id).toBe("att-199");
			expect(data2.attempt_id).toBe("att-199");
			expect(data1.launch_id).not.toBe(data2.launch_id);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("before-start cancellation appends nothing and releases reservation without journal record", async () => {
		const f = await setupM2Fixture();
		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-199",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 0,
			output: "",
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 0,
			requests: 0, // started = false
		} as executorModule.SingleResult);

		try {
			const res = await f.getRegisteredExecute()("call-4", { action: "begin_execution_review", work: "OMP-199", body: "evidence" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("Auditor launch failed before start");

			expect(f.cancelCalls).toHaveLength(1);
			expect(f.cancelCalls[0].launchId).toBe("launch-1");

			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("real async session drift leaves zero entries in either session file", async () => {
		const f = await setupM2Fixture();
		const mockOutput = JSON.stringify({ report: "VERDICT: PASS\n(clean)" });
		const originalSessionFile = f.sessionFile;
		const originalSessionId = f.sessionManager.getSessionId();
		let newSessionFile: string | undefined;
		let newSessionId: string | undefined;

		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async () => {
			// Session ID drifts in-place while subprocess is in flight
			await f.sessionManager.newSession();
			await f.sessionManager.ensureOnDisk();
			newSessionFile = f.sessionManager.getSessionFile();
			newSessionId = f.sessionManager.getSessionId();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai/gpt-5.2",
			} as executorModule.SingleResult;
		});

		try {
			await f.getRegisteredExecute()("call-5", { action: "begin_execution_review", work: "OMP-199", body: "evidence" }, new AbortController().signal, () => {}, f.fakeCtx);

			expect(newSessionId).toBeDefined();
			expect(newSessionId).not.toBe(originalSessionId);
			expect(newSessionFile).toBeDefined();
			expect(newSessionFile).not.toBe(originalSessionFile);

			await f.sessionManager.flush();

			const originalOpened = await SessionManager.open(originalSessionFile);
			expect(originalOpened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch")).toHaveLength(0);

			const newOpened = await SessionManager.open(newSessionFile!);
			expect(newOpened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch")).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("manual run_audit refuses without owner /summary input and writes zero launch entries", async () => {
		const f = await setupM2Fixture();
		try {
			const res = await f.getRegisteredExecute()("call-6", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("REFUSED — a closeout audit requires Chris to literally enter /summary in this owner session");

			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("independent Pi session drift leaves zero audit-launch entries after reload", async () => {
		const f = await setupM2Fixture();
		const mockOutput = JSON.stringify({ report: "VERDICT: PASS\n(clean)" });

		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async () => {
			// Manager session remains fixed while Pi session ID drifts independently
			f.setPiSessionId("drifted-pi-session-id");
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai/gpt-5.2",
			} as executorModule.SingleResult;
		});

		try {
			await f.getRegisteredExecute()("call-7", { action: "begin_execution_review", work: "OMP-199", body: "evidence" }, new AbortController().signal, () => {}, f.fakeCtx);

			// Settlement proceeded, payload and budget remain untouched
			expect(f.settleCalls).toHaveLength(1);
			expect("resolvedModel" in (f.settleCalls[0].payload as Record<string, unknown>)).toBe(false);
			expect("resolvedModelIsFallback" in (f.settleCalls[0].payload as Record<string, unknown>)).toBe(false);

			// But no audit-launch entry was written to the session file
			await f.sessionManager.flush();
			const opened = await SessionManager.open(f.sessionFile);
			const launchEntries = opened.getEntries().filter(e => e.type === "custom" && e.customType === "work-now-audit-launch");
			expect(launchEntries).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});
});

describe("native audit preflight unification", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	async function setupUnificationFixture(options: {
		preflightBeginResult?: BeginStagePreflightResult;
		missingWorkClient?: boolean;
		onReserveAuditor?: () => { status: string; launchId: string };
		mode?: "owner" | "execution_review";
		boundAuditPolicySha?: string;
		stageLaunchStatus?: "reserved" | "handed_off" | "settled";
	} = {}) {
		const repo = makeTempRepo();
		const sessionDir = fs.mkdtempSync(path.join(os.tmpdir(), "unification-sessions-"));
		const sessionManager = SessionManager.create(repo.dir, sessionDir);
		await sessionManager.ensureOnDisk();
		const sessionFile = sessionManager.getSessionFile()!;

		const cachePath = path.join(os.homedir(), ".omp", "agent", repo.cacheFile);
		fs.mkdirSync(path.dirname(cachePath), { recursive: true });
		fs.writeFileSync(
			cachePath,
			JSON.stringify({
				issueId: "uuid-199",
				identifier: "OMP-199",
				title: "Test 199",
			}),
		);

		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const callLog: string[] = [];
		const handlers = new Map<string, Array<(e: unknown, ctx: ExtensionContext) => Promise<unknown>>>();

		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			getSessionId: () => sessionManager.getSessionId(),
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<unknown>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: (customType: string, data?: unknown) => {
				sessionManager.appendCustomEntry(customType, data);
			},
		} as unknown as ExtensionAPI;

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager,
			models: createAuditorTestModelQuery(),
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			abort: () => {},
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		mockDiscovery();
		const tcb = await computeAuditTcb(fakeCtx, {
			healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
		} as unknown as WorkClient);

		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/execution/omp-199",
				state: "active",
				mode: "single",
				grant_version: 4,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: tcb.judgeSha256,
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const ownershipEntry = executionOwnershipEntry(exec, repo.dir, "OMP-199");
		if (options.mode === "execution_review") {
			sessionManager.appendCustomEntry(ownershipEntry.customType, {
				...ownershipEntry.data,
				issueId: "uuid-199",
				identifier: "OMP-199",
				title: "Test 199",
				project: "The Bookends",
			});
		} else {
			sessionManager.appendCustomEntry("work-now", {
				backend: "work",
				issueId: "uuid-199",
				identifier: "OMP-199",
				title: "Test 199",
				project: "The Bookends",
			});
		}

		const settleCalls: Array<{ work: unknown; launchId: unknown; payload: unknown }> = [];
		const reserveCalls: Array<{ key: string; taskSha256: string; toolCallId: string }> = [];
		let reserveIndex = 0;

		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			currentNow: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({
				key: "OMP-199",
				attemptSnapshot: {
					attemptId: "att-199",
					state: "audit_ready",
					candidateId: "cand-199",
					candidateSha: "cand-sha",
					candidateCommit: "1".repeat(40),
					remainingLaunches: 3,
					remainingReports: 2,
					hasManifest: true,
					isLaunchable: true,
					nextAction: "run native audit",
				},
				auditTask: {
					attemptId: "att-199",
					attemptState: "audit_ready",
					taskBody: "audit task body 199",
					taskSha256: "task-sha-199",
				},
			}),
			executionChildren: async () => ({ umbrella: false, children: [] }),
			getExecution: async () => exec,
			setExecutionState: async () => exec,
			finalizeExecutionCandidate: async () => ({ candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) }),
			appendEvidence: async () => ({ receipt_id: "receipt-199" }),
			summaryGate: async () => ({
				ok: true,
				planHash: "plan-hash-199",
				auditBaseCommit: repo.headSha,
				auditBaseDirtyPaths: [],
				issue: { id: "uuid-199", key: "OMP-199" },
			}),
			readCarrier: (c?: unknown) => ({ commitSha: "1".repeat(40), ...(typeof c === "object" && c ? c : {}) }),
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha-199", taskBody: "audit task body 199" }),
			reserveAuditorLaunch: async (key: string, taskSha256: string, toolCallId: string) => {
				reserveIndex++;
				callLog.push("reserveAuditorLaunch");
				reserveCalls.push({ key, taskSha256, toolCallId });
				if (options.onReserveAuditor) return options.onReserveAuditor();
				return { status: "reserved", launchId: `auditor-launch-${reserveIndex}` };
			},
			settleAuditorLaunch: async (work: unknown, launchId: unknown, payload: unknown) => {
				callLog.push("settleAuditorLaunch");
				settleCalls.push({ work, launchId, payload });
				const payloadStr = typeof payload === "object" && payload !== null && "payload" in payload ? String((payload as { payload: unknown }).payload) : "";
				const verdict = payloadStr.includes("NEEDS_FIX") ? "NEEDS_FIX" : "PASS";
				return { verdict, event: { renderedText: verdict } };
			},
			cancelAuditorLaunch: async () => {
				callLog.push("cancelAuditorLaunch");
				return { status: "applied", event: { requiresDelivery: false } };
			},
			recordCloseoutReview: async () => ({ status: "applied" }),
			completeExecutionItem: async () => {
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: options.missingWorkClient ? undefined : {
				healthReady: async () => ({
					ready: true,
					contract_sha256: "contract-sha",
					service_fingerprint: "prospective-fp-199",
					judge_manifest: {
						judge_sha256: "judge-sha",
						audit_policy_sha256: options.boundAuditPolicySha ?? tcb.policySha256,
					},
				}),
				workflow: async () => ({
					receipts: [
						{ receipt_id: "verif-199", kind: "verification", payload_sha256: "0".repeat(64), artifact_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199" },
						{ receipt_id: "receipt-199", kind: "push", payload: { repository: "theturtlecsz/oh-my-pi", remote_url: "https://github.com/theturtlecsz/oh-my-pi.git" }, payload_sha256: "0".repeat(64), candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", remote_ref: "refs/heads/execution/omp-199", remote_commit: "1".repeat(40) },
					],
					auditor_launches: [],
					audit_manifest: {
						manifest_id: "man-199",
						manifest_version: 1,
						verification_receipt_id: "verif-199",
						task_sha256: "task-sha-199",
						attempt_id: "att-199",
					},
					item: {
						work_id: "uuid-199",
						revision: { revision_id: "rev-199" },
						candidate: {
							candidate_id: "cand-199",
							candidate_sha256: "cand-sha",
							commit_sha: "1".repeat(40),
							kind: "final",
						},
					},
					close_attempts: [
						{ attempt_id: "att-199", candidate_id: "cand-199", revision_id: "rev-199", work_id: "uuid-199", judge_sha256: tcb.judgeSha256 },
					],
				}),
			},
		} as unknown as WorkflowBackend;

		if (!options.missingWorkClient) {
			attachStageLaunchFixture(mockBackend, repo.dir, { callLog });
			if (options.stageLaunchStatus) {
				const origReserveStageLaunch = mockBackend.reserveStageLaunch.bind(mockBackend);
				mockBackend.reserveStageLaunch = async (input: Parameters<typeof mockBackend.reserveStageLaunch>[0]) => {
					const res = await origReserveStageLaunch(input);
					return {
						...res,
						status: options.stageLaunchStatus!,
					};
				};
			}
			if (options.preflightBeginResult) {
				mockBackend.beginStagePreflight = async () => {
					callLog.push("beginStagePreflight");
					return options.preflightBeginResult!;
				};
			}
		}

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const startHandlers = handlers.get("session_start") ?? [];
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/execution/omp-199", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "verifyMergeConfirmation").mockReturnValue({ confirmed: true, detail: "PR merged and origin/main contains candidate" });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		const authorizeSummary = async () => {
			const inputHandlers = handlers.get("input") ?? [];
			for (const h of inputHandlers) {
				await h({ source: "user", originalText: "/summary" }, fakeCtx);
			}
		};

		return {
			repo: {
				...repo,
				cleanup: () => {
					fs.rmSync(sessionDir, { recursive: true, force: true });
					repo.cleanup();
				},
			},
			sessionManager,
			sessionFile,
			fakeCtx,
			fakePi,
			exec,
			callLog,
			reserveCalls,
			settleCalls,
			authorizeSummary,
			getRegisteredExecute: () => registeredExecute!,
		};
	}

	test("contract: owner closeout audit uses native dispatch preflight and reserves auditor launch only after successful stage run", async () => {
		const f = await setupUnificationFixture();
		await f.authorizeSummary();

		const mockOutput = JSON.stringify({ report: "VERDICT: PASS\n(clean audit report)" });
		let providerCalled = false;
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			providerCalled = true;
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: mockOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-sol:medium",
				resolvedModelIsFallback: false,
			} as executorModule.SingleResult;
		});

		try {
			const res = await f.getRegisteredExecute()("call-owner-audit", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("PASS");
			expect(providerCalled).toBe(true);

			// Preflight lifecycle occurred through WorkService
			expect(f.callLog).toContain("beginStagePreflight");
			expect(f.callLog).toContain("admitStagePreflight");
			expect(f.callLog).toContain("recordStagePreflight");

			// Stage launch was reserved and settled before auditor launch reservation
			expect(f.callLog).toContain("reserveStageLaunch");
			expect(f.callLog).toContain("settleStageLaunch");
			expect(f.callLog).toContain("reserveAuditorLaunch");
			expect(f.callLog).toContain("settleAuditorLaunch");

			// Auditor launch reservation occurred strictly AFTER stage launch settled
			const settleStageIdx = f.callLog.indexOf("settleStageLaunch");
			const reserveAuditorIdx = f.callLog.indexOf("reserveAuditorLaunch");
			expect(settleStageIdx).toBeGreaterThan(-1);
			expect(reserveAuditorIdx).toBeGreaterThan(settleStageIdx);

			expect(f.reserveCalls).toHaveLength(1);
			expect(f.settleCalls).toHaveLength(1);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("contract: owner closeout audit denies before auditor reservation when stage run does not start", async () => {
		const f = await setupUnificationFixture();
		await f.authorizeSummary();

		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-199",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 1,
			output: "",
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 0,
			requests: 0,
			error: "transport connection reset",
		} as executorModule.SingleResult);

		try {
			const res = await f.getRegisteredExecute()("call-owner-audit-fail", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("Auditor launch failed before start: transport connection reset");
			expect(f.callLog).toContain("reserveStageLaunch");
			expect(f.callLog).toContain("cancelStageLaunch:launch-1");

			// Auditor launch is NEVER reserved if stage run did not start
			expect(f.callLog).not.toContain("reserveAuditorLaunch");
			expect(f.reserveCalls).toHaveLength(0);
			expect(f.settleCalls).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("contract: dispatched preflight replay yields zero provider calls and zero auditor reservations", async () => {
		const replayedPreflightResult: BeginStagePreflightResult = {
			type: "begin_stage_preflight",
			status: "replayed",
			intent: {
				intent_id: "intent-replayed",
				workspace_id: "ws-1",
				work_id: "uuid-199",
				revision_id: "rev-199",
				candidate_id: "cand-199",
				attempt_id: "att-199",
				grant_id: "grant-199",
				role: "audit",
				tool_call_id: "native-audit-att-199",
				task_sha256: "0".repeat(64),
				probe_sha256: "0".repeat(64),
				transport_attempt_id: crypto.randomUUID(),
				ordinal: 0,
				requested_selector: "openai-codex/gpt-5.6-sol:medium",
				requested_provider: "openai-codex",
				requested_model: "gpt-5.6-sol",
				requested_api: "openai-codex-responses",
				requested_effort: "medium",
				requested_wire_model: "gpt-5.6-sol",
				is_fallback: false,
				logical_sha256: "0".repeat(64),
				group_sha256: "0".repeat(64),
				host_owner_id: "00000000-0000-0000-0000-000000000001",
				dispatched_at: new Date().toISOString(),
				dispatch_operation_id: null,
				dispatch_owner_id: null,
				cancelled_at: null,
				cancelled_by: null,
				cancel_reason: null,
				status: "dispatched",
				created_at: new Date().toISOString(),
				settled_at: null,
			},
			preflight: undefined,
		};

		// 1. Owner closeout path
		const fOwner = await setupUnificationFixture({ preflightBeginResult: replayedPreflightResult });
		await fOwner.authorizeSummary();
		const runSubprocessSpyOwner = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resOwner = await fOwner.getRegisteredExecute()("call-replayed-owner", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, fOwner.fakeCtx);
			expect(resOwner.content[0]?.text).toContain("Native audit stage blocked");
			expect(resOwner.content[0]?.text).toContain("provider effect uncertain");
			expect(runSubprocessSpyOwner).not.toHaveBeenCalled();
			expect(fOwner.reserveCalls).toHaveLength(0);
			expect(fOwner.settleCalls).toHaveLength(0);
		} finally {
			await fOwner.sessionManager.close();
			fOwner.repo.cleanup();
		}

		// 2. Execution-review path
		const fExec = await setupUnificationFixture({ preflightBeginResult: replayedPreflightResult, mode: "execution_review" });
		const runSubprocessSpyExec = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resExec = await fExec.getRegisteredExecute()("call-replayed-exec", { action: "begin_execution_review", work: "OMP-199", body: "review body" }, new AbortController().signal, () => {}, fExec.fakeCtx);
			expect(resExec.content[0]?.text).toContain("Native audit stage blocked");
			expect(resExec.content[0]?.text).toContain("provider effect uncertain");
			expect(runSubprocessSpyExec).not.toHaveBeenCalled();
			expect(fExec.reserveCalls).toHaveLength(0);
			expect(fExec.settleCalls).toHaveLength(0);
		} finally {
			await fExec.sessionManager.close();
			fExec.repo.cleanup();
		}
	});

	test("contract: owner handed-off replay yields zero provider calls and zero auditor reservations", async () => {
		// 1. Owner closeout path
		const fOwner = await setupUnificationFixture({ stageLaunchStatus: "handed_off" });
		await fOwner.authorizeSummary();
		const runSubprocessSpyOwner = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resOwner = await fOwner.getRegisteredExecute()("call-handed-off-owner", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, fOwner.fakeCtx);
			expect(resOwner.content[0]?.text).toContain("Native audit stage blocked");
			expect(resOwner.content[0]?.text).toContain("uncertain after a committed handoff");
			expect(runSubprocessSpyOwner).not.toHaveBeenCalled();
			expect(fOwner.callLog).toContain("reserveStageLaunch");
			expect(fOwner.callLog).not.toContain("reserveAuditorLaunch");
			expect(fOwner.callLog).not.toContain("settleAuditorLaunch");
			expect(fOwner.reserveCalls).toHaveLength(0);
			expect(fOwner.settleCalls).toHaveLength(0);
		} finally {
			await fOwner.sessionManager.close();
			fOwner.repo.cleanup();
		}

		// 2. Execution-review path
		const fExec = await setupUnificationFixture({ stageLaunchStatus: "handed_off", mode: "execution_review" });
		const runSubprocessSpyExec = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resExec = await fExec.getRegisteredExecute()("call-handed-off-exec", { action: "begin_execution_review", work: "OMP-199", body: "review body" }, new AbortController().signal, () => {}, fExec.fakeCtx);
			expect(resExec.content[0]?.text).toContain("Native audit stage blocked");
			expect(resExec.content[0]?.text).toContain("uncertain after a committed handoff");
			expect(runSubprocessSpyExec).not.toHaveBeenCalled();
			expect(fExec.callLog).toContain("reserveStageLaunch");
			expect(fExec.callLog).not.toContain("reserveAuditorLaunch");
			expect(fExec.callLog).not.toContain("settleAuditorLaunch");
			expect(fExec.reserveCalls).toHaveLength(0);
			expect(fExec.settleCalls).toHaveLength(0);
		} finally {
			await fExec.sessionManager.close();
			fExec.repo.cleanup();
		}
	});

	test("contract: missing WorkService/native dispatch prerequisite refuses before provider/auditor reservation", async () => {
		// 1. Owner closeout path
		const fOwner = await setupUnificationFixture({ missingWorkClient: true });
		await fOwner.authorizeSummary();
		const runSubprocessSpyOwner = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resOwner = await fOwner.getRegisteredExecute()("call-missing-ws-owner", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, fOwner.fakeCtx);
			expect(resOwner.content[0]?.text).toContain("Native audit stage blocked: native stage dispatch requires WorkService");
			expect(runSubprocessSpyOwner).not.toHaveBeenCalled();
			expect(fOwner.reserveCalls).toHaveLength(0);
			expect(fOwner.settleCalls).toHaveLength(0);
		} finally {
			await fOwner.sessionManager.close();
			fOwner.repo.cleanup();
		}

		// 2. Execution-review path
		const fExec = await setupUnificationFixture({ missingWorkClient: true, mode: "execution_review" });
		const runSubprocessSpyExec = vi.spyOn(executorModule, "runSubprocess");

		try {
			const resExec = await fExec.getRegisteredExecute()("call-missing-ws-exec", { action: "begin_execution_review", work: "OMP-199", body: "review body" }, new AbortController().signal, () => {}, fExec.fakeCtx);
			expect(resExec.content[0]?.text).toContain("Native audit stage blocked: native stage dispatch requires WorkService");
			expect(runSubprocessSpyExec).not.toHaveBeenCalled();
			expect(fExec.reserveCalls).toHaveLength(0);
			expect(fExec.settleCalls).toHaveLength(0);
		} finally {
			await fExec.sessionManager.close();
			fExec.repo.cleanup();
		}
	});

	test("contract: execution-review has no direct provider fallback and retains bound route/policy drift refusal", async () => {
		// Policy drift refusal
		const f = await setupUnificationFixture({ mode: "execution_review" });
		const runSubprocessSpy = vi.spyOn(executorModule, "runSubprocess");

		const driftedSettings = Settings.isolated({
			modelRoles: {
				audit: "openai-codex/gpt-5.6-sol:high",
			},
		});
		const driftedModels = createAuditorTestModelQuery(driftedSettings);
		vi.spyOn(gitModule, "pushCandidate").mockImplementation(async () => {
			f.fakeCtx.models = driftedModels;
			return { status: "pushed", remoteRef: "refs/heads/execution/omp-199", remoteCommit: "1".repeat(40), priorTip: f.repo.headSha };
		});

		try {
			const res = await f.getRegisteredExecute()("call-drift", { action: "begin_execution_review", work: "OMP-199", body: "evidence" }, new AbortController().signal, () => {}, f.fakeCtx);
			expect(res.content[0]?.text).toContain("audit policy drifted during execution review");
			expect(runSubprocessSpy).not.toHaveBeenCalled();
			expect(f.reserveCalls).toHaveLength(0);
			expect(f.settleCalls).toHaveLength(0);
		} finally {
			await f.sessionManager.close();
			f.repo.cleanup();
		}
	});

	test("contract: successful audit still settles one auditor launch and returns existing verdict/report behavior", async () => {
		// 1. Owner closeout with PASS verdict
		const fOwner = await setupUnificationFixture();
		await fOwner.authorizeSummary();
		const passOutput = JSON.stringify({ report: "VERDICT: PASS\nAll criteria satisfied." });
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: passOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-sol:medium",
				resolvedModelIsFallback: false,
			} as executorModule.SingleResult;
		});

		try {
			const res = await fOwner.getRegisteredExecute()("call-owner-pass", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, fOwner.fakeCtx);
			expect(fOwner.settleCalls).toHaveLength(1);
			expect(fOwner.settleCalls[0].payload).toEqual({ payload: passOutput });
			expect(res.content[0]?.text).toBe("PASS");
		} finally {
			await fOwner.sessionManager.close();
			fOwner.repo.cleanup();
		}

		// 2. Owner closeout with NEEDS_FIX verdict (includes ## Auditor Report suffix)
		const fFix = await setupUnificationFixture();
		await fFix.authorizeSummary();
		const fixOutput = JSON.stringify({ report: "VERDICT: NEEDS_FIX\nFix issue A." });
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: { onNativeStageHandoff?: () => Promise<void> | void }) => {
			await options?.onNativeStageHandoff?.();
			return {
				index: 0,
				id: "att-199",
				agent: "auditor",
				agentSource: "bundled",
				task: "task",
				exitCode: 0,
				output: fixOutput,
				stderr: "",
				truncated: false,
				durationMs: 10,
				tokens: 10,
				requests: 1,
				resolvedModel: "openai-codex/gpt-5.6-sol:medium",
				resolvedModelIsFallback: false,
			} as executorModule.SingleResult;
		});

		try {
			const res = await fFix.getRegisteredExecute()("call-owner-fix", { action: "run_audit", work: "OMP-199" }, new AbortController().signal, () => {}, fFix.fakeCtx);
			expect(fFix.settleCalls).toHaveLength(1);
			expect(fFix.settleCalls[0].payload).toEqual({ payload: fixOutput });
			expect(res.content[0]?.text).toContain("NEEDS_FIX");
			expect(res.content[0]?.text).toContain("## Auditor Report");
			expect(res.content[0]?.text).toContain(fixOutput);
		} finally {
			await fFix.sessionManager.close();
			fFix.repo.cleanup();
		}
	});
});
