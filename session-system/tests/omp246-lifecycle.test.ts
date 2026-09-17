/**
 * OMP-246 lifecycle package: permission-boundary contracts driven through the
 * real workflow host, the real createWorkBackend pending-operation journal, a
 * real temp git repository, and a fetch-level WorkService double keyed by
 * operation_id (fixtures/omp246-mock-work-service.ts, native error codes). No
 * native service, provider, WebUI or GitHub write is touched.
 *
 * Contract 1: accepted report, then a same-response write. The worker-level
 *   terminal-yield fence is proved in
 *   packages/coding-agent/test/agent-session-submission-fence.test.ts. Here
 *   the CONTROLLER half runs on the production path: a real AgentSession over
 *   a real ExtensionRunner hosting the real workflow extension, the real
 *   `write` tool, and a scripted mock provider that emits
 *   [begin_execution_review, write] in ONE assistant response. The submission
 *   must be accepted (freeze, finalize, push, attempt begun) and the trailing
 *   write must receive the loop's "Tool was not executed" result with the
 *   frozen candidate and worktree unchanged; the early-write control orders
 *   [write, begin_execution_review] and expects both to run. The recovery
 *   dirty-refusal (dirt on a submitted candidate refuses automatic recovery
 *   without a ledger write) stays as before.
 * Contract 2: sealed-path edit, then crash before the report. A restart drains
 *   session start against the same journal and transcript branch: exactly one
 *   continuation, the committed reservation reused instead of duplicated, the
 *   edit still on disk, the item still executing, no accepted completion.
 * Contract 3: the exact completion command replayed by a fresh backend over
 *   the same journal applies once and hands back the stored result; an
 *   in-process duplicate costs zero POST. A whole-process controller restart
 *   reissue is NOT_ASSESSED (test.todo below).
 * Queue recovery: a committed completion whose response was lost is reconciled
 *   by operation identity at the next owned session start, the next item is
 *   activated exactly once under the grant-version CAS, one continuation is
 *   delivered, and a second restart over the same journal and transcript adds
 *   no completion, activation, or reservation.
 */
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import { Agent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import { createMockModel, type MockResponse } from "@oh-my-pi/pi-ai/providers/mock";
import type { CustomEntry, ExtensionAPI, ExtensionContext, PersistedTurnContinuationRequest, SessionEntry } from "@oh-my-pi/pi-coding-agent";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { ExtensionRuntime, loadExtensionFromFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import { ExtensionToolWrapper, wrapRegisteredTools } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/wrapper";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { AuthStorage, SqliteAuthCredentialStore } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { BashTool } from "@oh-my-pi/pi-coding-agent/tools/bash";
import { WriteTool } from "@oh-my-pi/pi-coding-agent/tools/write";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import {
	type Candidate,
	type CloseAttempt,
	type CommandEnvelope,
	type CompletionEvidence,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type ExecutionMode,
	sha256Hex,
	type WorkItemView,
	WorkError,
} from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import type { WorkClientConfig } from "../extensions/workflow/config";
import { dirtyPaths, headCommit } from "../extensions/workflow/git";
import { createWorkflowHost, type HostConfig } from "../extensions/workflow/host";
import { type PendingRecord, readPendingClaims } from "../extensions/workflow/pending-ops";
import { createWorkBackend } from "../extensions/workflow/work";
import { createMockWorkService, type MockWorkService } from "./fixtures/omp246-mock-work-service";

const SESSION_ID = "omp246-lifecycle-session";
const KEY = "OMP-246";
const NEXT_KEY = "OMP-247";
const SEALED_PATH = "src/feature.ts";
const UNSEALED_PATH = "README.md";
const BEFORE_BYTES = "export const feature = \"before\";\n";
const AFTER_BYTES = "export const feature = \"after\";\n";
/** Bytes a post-submission write would leave on the sealed path if the fence let it run. */
const LATE_BYTES = "export const feature = \"late\";\n";
const REQUEST = "Change feature from before to after.";

const cleanups: Array<() => void> = [];
const asyncCleanups: Array<() => Promise<void>> = [];

beforeEach(() => {
	// computeAuditTcb seals the installed auditor definition; host tests supply
	// it explicitly so they never depend on the developer home.
	vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
		agents: [
			{
				name: "auditor",
				description: "Auditor agent",
				systemPrompt: "Audit prompt",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "bundled",
			},
		],
		projectAgentsDir: null,
	});
});

afterEach(async () => {
	vi.restoreAllMocks();
	for (const cleanup of asyncCleanups.splice(0)) await cleanup();
	for (const cleanup of cleanups.splice(0)) cleanup();
});

function tempDir(prefix: string): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
	cleanups.push(() => fs.rmSync(dir, { recursive: true, force: true }));
	return dir;
}

function makeRepo(): { dir: string; baseline: string } {
	const dir = tempDir("omp246-repo-");
	const git = (...args: string[]): void => {
		const run = spawnSync("git", args, { cwd: dir, encoding: "utf8" });
		if (run.status !== 0) throw new Error(`git ${args.join(" ")} failed: ${run.stderr}`);
	};
	git("init", "-q", "-b", "main");
	git("config", "user.name", "OMP-246");
	git("config", "user.email", "omp246@example.com");
	git("config", "commit.gpgsign", "false");
	fs.mkdirSync(path.join(dir, "src"));
	fs.writeFileSync(path.join(dir, SEALED_PATH), BEFORE_BYTES);
	git("add", ".");
	git("commit", "-q", "-m", "baseline");
	// A bare origin so the review's candidate push has a real remote to verify against.
	const origin = tempDir("omp246-origin-");
	git("init", "-q", "--bare", origin);
	git("remote", "add", "origin", origin);
	const baseline = headCommit(dir);
	if (!baseline) throw new Error("baseline commit missing");
	return { dir, baseline };
}

interface Lane {
	repo: string;
	baseline: string;
	pendingDir: string;
	service: MockWorkService;
	/** A fresh backend on the same journal: one instance per simulated process. */
	backend(): WorkflowBackend;
	grantId: string;
	workId: string;
	/** Position-1 work id in queue mode. */
	nextWorkId: string;
	revisionId: string;
	candidateId: string;
	candidateSha256: string;
	attemptId: string;
	judgeSha256: string;
}

function workItemView(workspaceId: string, workId: string, key: string, candidate: Candidate | null): WorkItemView {
	const now = new Date().toISOString();
	return {
		work_id: workId,
		workspace_id: workspaceId,
		alias: { work_id: workId, key, primary: true, origin: "local" },
		state: "IN_PROGRESS",
		revision: {
			revision_id: randomUUID(),
			work_id: workId,
			revision_number: 1,
			title: `Lifecycle ${key}`,
			description: REQUEST,
			scope: "",
			acceptance_criteria: [],
			content_sha256: sha256Hex(REQUEST),
			created_by: "omp246",
			created_at: now,
		},
		candidate,
		project_id: null,
		archived: false,
	};
}

/** One execution grant on a real temp repo: position 0 executing (or submitted for review), queue mode adds a pending item. */
async function makeLane(mode: ExecutionMode, options: { submitted?: boolean } = {}): Promise<Lane> {
	const { dir: repo, baseline } = makeRepo();
	const pendingDir = tempDir("omp246-pending-");
	const cacheDir = tempDir("omp246-cache-");
	const workspaceId = randomUUID();
	const ownerId = randomUUID();
	const grantId = randomUUID();
	const workId = randomUUID();
	const nextWorkId = randomUUID();
	const plannedCandidateId = randomUUID();
	const candidateId = randomUUID();
	const candidateSha256 = sha256Hex(`candidate:${candidateId}`);
	const attemptId = randomUUID();
	const now = new Date().toISOString();
	const candidate: Candidate | null = options.submitted
		? {
				candidate_id: candidateId,
				work_id: workId,
				revision_id: "",
				candidate_sha256: candidateSha256,
				commit_sha: baseline,
				kind: "final",
				allocated_at: now,
			}
		: null;
	const workItems = [workItemView(workspaceId, workId, KEY, candidate)];
	const revisionId = workItems[0]!.revision.revision_id;
	if (candidate) candidate.revision_id = revisionId;
	if (mode === "queue") workItems.push(workItemView(workspaceId, nextWorkId, NEXT_KEY, null));
	const grant: ExecutionGrantView = {
		grant_id: grantId,
		workspace_id: workspaceId,
		owner_id: ownerId,
		repository: repo,
		remote_ref: `refs/heads/execution/${KEY.toLowerCase()}`,
		state: "active",
		mode,
		grant_version: 1,
		max_continuations: 8,
		max_close_attempts: 5,
		max_no_progress: 3,
		continuations_scheduled: 0,
		terminal_reason: null,
		authorization_hash: sha256Hex(`authorization:${grantId}`),
		judge_sha256: "",
		created_at: now,
		expires_at: new Date(Date.now() + 86_400_000).toISOString(),
	};
	const itemView = (position: number, itemWorkId: string, revision: string, phase: ExecutionGrantItemView["phase"]): ExecutionGrantItemView => ({
		item_id: randomUUID(),
		workspace_id: workspaceId,
		grant_id: grantId,
		work_id: itemWorkId,
		position,
		phase,
		claimed_revision_id: revision,
		project_id: null,
		active_blocker_ids: [],
		initial_git_baseline: baseline,
		current_git_baseline: baseline,
		criteria_revision_id: revision,
		original_request: REQUEST,
		original_request_sha256: sha256Hex(REQUEST),
		close_attempts_started: options.submitted ? 1 : 0,
		consecutive_no_progress: 0,
		plan_stamp: { candidate_id: plannedCandidateId, paths: [SEALED_PATH] },
	});
	const items = [itemView(0, workId, revisionId, options.submitted ? "reviewing" : "executing")];
	if (mode === "queue") items.push(itemView(1, nextWorkId, workItems[1]!.revision.revision_id, "pending"));
	const closeAttempts: CloseAttempt[] = options.submitted
		? [
				{
					attempt_id: attemptId,
					work_id: workId,
					revision_id: revisionId,
					candidate_id: candidateId,
					plan_receipt_id: null,
					candidate_sha256: candidateSha256,
					candidate_commit: baseline,
					owner_session_id: null,
					owner_session_started_at: null,
					owner_session_start_commit: null,
					repository: repo,
					diff_sha256: null,
					starting_dirty_paths: [],
					authorization_kind: "execution",
					execution_grant_id: grantId,
					candidate_tree_sha: candidateSha256,
					original_request_sha256: sha256Hex(REQUEST),
					criteria_sha256: null,
					plan_stamp_sha256: null,
					judge_sha256: null,
					authorization_ref: `execution:${grantId}:0:1`,
					launch_count: 0,
					cancelled_launch_count: 0,
					accepted_report_count: 0,
					in_flight_launch_id: null,
					state: "audit_ready",
					terminal_reason: null,
					requested_at: now,
					closeout_requested_at: null,
					completed_at: null,
					completion_authorization_ref: null,
				},
			]
		: [];
	const service = createMockWorkService({ workspaceId, ownerId, grant, items, workItems, closeAttempts });
	const config: WorkClientConfig = { baseUrl: "http://127.0.0.1:9", workspaceId, ownerId };
	// The real backend names the live cache under ~/.omp/agent; point it at a
	// disposable file so the package never touches the developer installation.
	const cacheFile = path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "work-now.json"));
	const backend = (): WorkflowBackend => ({
		...createWorkBackend(config, () => "omp246-token", service.fetch, pendingDir),
		cacheFile,
	});
	const probe = backend();
	if (!probe.workClient) throw new Error("real backend exposes no work client");
	const tcb = await computeAuditTcb({ cwd: repo } as ExtensionContext, probe.workClient);
	grant.judge_sha256 = tcb.judgeSha256;
	return {
		repo,
		baseline,
		pendingDir,
		service,
		backend,
		grantId,
		workId,
		nextWorkId,
		revisionId,
		candidateId,
		candidateSha256,
		attemptId,
		judgeSha256: tcb.judgeSha256,
	};
}

/** The transcript binding a real /execute admission writes; recovery witnesses ownership from it. */
function ownershipEntry(lane: Lane): CustomEntry {
	return {
		type: "custom",
		customType: "work-now",
		id: randomUUID(),
		parentId: null,
		timestamp: new Date().toISOString(),
		data: {
			backend: "work",
			executionWorkspace: {
				grantId: lane.grantId,
				key: KEY,
				primaryRoot: lane.repo,
				path: lane.repo,
				branch: `execution/${KEY.toLowerCase()}`,
				baseline: lane.baseline,
				reused: false,
			},
		},
	};
}

interface SentMessage {
	customType?: string;
	details?: {
		executionContinuation?: { grantId: string; preReservationVersion: number; postVersion: number; workId?: string; messageId?: string };
	};
}

interface HostRun {
	entries: SessionEntry[];
	sent: SentMessage[];
	notices: string[];
	continuations: PersistedTurnContinuationRequest[];
	/** Drain the host's session_start handlers: the controller restart. */
	start(): Promise<void>;
}

/** The production host wiring shared by the transcript-only host runs and the full controller session. */
function hostConfig(backend: WorkflowBackend): HostConfig {
	return {
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: data => data.backend === "work",
		executionWorkspaceManager: {
			primaryRoot: async cwd => cwd,
			ensure: async (cwd, key, grantId, baseline) => ({
				primaryRoot: cwd,
				path: cwd,
				branch: `execution/${key.toLowerCase()}`,
				grantId,
				baseline,
				reused: false,
			}),
			cleanup: async () => ({ cleaned: true, detail: "omp246 identity cleanup" }),
		},
	};
}

interface ScriptedCall {
	id: string;
	name: string;
	arguments: Record<string, unknown>;
}

/** One assistant response carrying these tool calls in this order. */
function toolCalls(...calls: ScriptedCall[]): MockResponse {
	return {
		content: calls.map(call => ({ type: "toolCall" as const, id: call.id, name: call.name, arguments: call.arguments })),
		stopReason: "toolUse",
	};
}

const trailingStop = (text: string): MockResponse => ({ content: [text], stopReason: "stop" });
/** The controller submission: the production workflow tool's begin_execution_review action. */
const reviewCall = (id: string): ScriptedCall => ({ id, name: "work", arguments: { action: "begin_execution_review", body: "bun test: 1 pass" } });
/** The production write tool aimed at the sealed path. */
const writeCall = (id: string, content: string): ScriptedCall => ({ id, name: "write", arguments: { path: SEALED_PATH, content } });
const bashCall = (id: string, content: string): ScriptedCall => ({ id, name: "bash", arguments: { command: `printf '%s' '${content.replaceAll("'", "'\\''")}' > ${SEALED_PATH}` } });

interface ControllerSession {
	session: AgentSession;
	/** Text and error flag of the persisted tool result for one scripted call id. */
	toolResult(toolCallId: string): { isError: boolean; text: string; details: unknown };
}

/**
 * A real controller process: AgentSession + ExtensionRunner hosting the real
 * workflow extension (its `work` tool registered through the real
 * ExtensionAPI) and the real `write` tool, driven by a scripted provider. The
 * session runs at task depth 0 (owner session) on the lane repo, with no
 * ownership witness in the transcript so session_start performs no recovery.
 */
async function controllerSession(lane: Lane, responses: MockResponse[]): Promise<ControllerSession> {
	const mock = createMockModel({ responses });
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		"todo.enabled": false,
		"todo.eager": "default",
		"todo.reminders": false,
	});
	settings.setModelRole("default", `${mock.provider}/${mock.id}`);
	const auth = new AuthStorage(new SqliteAuthCredentialStore(new Database(":memory:")));
	auth.setRuntimeApiKey("mock", "test-key");
	const registry = new ModelRegistry(auth);
	const manager = SessionManager.inMemory(lane.repo);
	const runtime = new ExtensionRuntime();
	const extension = await loadExtensionFromFactory(
		pi => createWorkflowHost(hostConfig(lane.backend()))(pi),
		lane.repo,
		new EventBus(),
		runtime,
		"omp246-controller",
	);
	const runner = new ExtensionRunner([extension], runtime, lane.repo, manager, registry, undefined, settings);
	const toolSession: ToolSession = {
		cwd: lane.repo,
		hasUI: false,
		settings,
		getSessionFile: () => null,
		getSessionSpawns: () => null,
		enableLsp: false,
	};
	const tools: AgentTool[] = [
		...wrapRegisteredTools(runner.getAllRegisteredTools(), runner),
		new ExtensionToolWrapper(new WriteTool(toolSession), runner) as AgentTool,
		new BashTool(toolSession),
	];
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: { model: mock, systemPrompt: ["Test"], tools, messages: [] },
		convertToLlm,
		streamFn: mock.stream,
	});
	const session = new AgentSession({
		agent,
		sessionManager: manager,
		settings,
		modelRegistry: registry,
		toolRegistry: new Map(tools.map(tool => [tool.name, tool])),
		extensionRunner: runner,
		autoApprove: true,
	});
	asyncCleanups.push(async () => {
		await session.dispose();
		auth.close();
	});
	await initializeExtensions(session, { reportSendError: () => {}, reportRuntimeError: () => {} });
	return {
		session,
		toolResult: toolCallId => {
			const message = session.agent.state.messages.find(
				candidate => candidate.role === "toolResult" && candidate.toolCallId === toolCallId,
			);
			if (message?.role !== "toolResult") throw new Error(`no tool result recorded for ${toolCallId}`);
			const text = message.content.flatMap(block => (block.type === "text" ? [block.text] : [])).join("\n");
			return { isError: message.isError === true, text, details: message.details };
		},
	};
}

/** A fresh workflow host on a fresh backend over a given transcript branch: one controller process. */
function runHost(lane: Lane, backend: WorkflowBackend, entries: SessionEntry[]): HostRun {
	const sent: SentMessage[] = [];
	const notices: string[] = [];
	const continuations: PersistedTurnContinuationRequest[] = [];
	const startHandlers: Array<(event: unknown, ctx: ExtensionContext) => Promise<void>> = [];
	const pi = {
		logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
		zod: z,
		registerTool: () => {},
		registerCommand: () => {},
		registerMessageRenderer: () => {},
		on: (event: string, handler: (event: unknown, ctx: ExtensionContext) => Promise<void>) => {
			if (event === "session_start") startHandlers.push(handler);
		},
		getSessionId: () => SESSION_ID,
		requestPersistedTurnContinuation: (request: PersistedTurnContinuationRequest) => {
			continuations.push(request);
			return { status: "scheduled" };
		},
		sendMessage: (message: SentMessage) => {
			sent.push(message);
		},
		deliverMessage: async () => {},
		appendEntry: (customType: string, data: unknown) => {
			entries.push({
				type: "custom",
				customType,
				data,
				id: randomUUID(),
				parentId: entries.at(-1)?.id ?? null,
				timestamp: new Date().toISOString(),
			});
		},
	} as unknown as ExtensionAPI;
	createWorkflowHost(hostConfig(backend))(pi);
	const ctx = {
		cwd: lane.repo,
		taskDepth: 0,
		sessionManager: {
			getBranch: () => entries,
			getCwd: () => lane.repo,
			getSessionId: () => SESSION_ID,
			getLeafId: () => entries.at(-1)?.id,
		},
		ui: {
			notify: (text: string) => {
				notices.push(text);
			},
			theme: { fg: (_color: string, text: string) => text },
			setStatus: () => {},
		},
	} as unknown as ExtensionContext;
	return {
		entries,
		sent,
		notices,
		continuations,
		start: async () => {
			if (startHandlers.length === 0) throw new Error("workflow host registered no session_start recovery");
			for (const handler of startHandlers) await handler({}, ctx);
		},
	};
}

const continuationsOf = (run: HostRun) => run.sent.filter(message => message.customType === "work-execute");

const envelopeOf = (claim: { record: PendingRecord }): CommandEnvelope => claim.record.envelope as CommandEnvelope;

/** The reservation the pre-crash controller made; the service commits it but the response is lost. */
async function reserveThenCrash(lane: Lane): Promise<void> {
	lane.service.dropNextResponse("after_commit");
	const failure = await lane
		.backend()
		.setExecutionState({
			grantId: lane.grantId,
			expectedGrantVersion: 1,
			targetState: "active",
			reason: "session_start_recovery",
			judgeSha256: lane.judgeSha256,
		})
		.then(
			() => undefined,
			(error: unknown) => error,
		);
	if (!(failure instanceof WorkError)) throw new Error(`lost response must surface as WorkError, got ${String(failure)}`);
	expect(failure.status).toBe(0);
	expect(lane.service.grant().grant_version).toBe(2);
	expect(lane.service.posts("set_execution_state")).toBe(1);
	const { claims } = await readPendingClaims(lane.pendingDir);
	expect(claims.map(claim => claim.record.result)).toEqual([undefined]);
}

interface CompletionCommandInput {
	grantId: string;
	expectedGrantVersion: number;
	workId: string;
	attemptId: string;
	evidence: CompletionEvidence;
	judgeSha256: string;
}

/** The exact complete_execution_item the controller issues for position 0 at grant version 1. */
function completionInput(lane: Lane): CompletionCommandInput {
	const evidence: CompletionEvidence = {
		runner: {
			issuer: "work-service/auditor-settle",
			launch_id: randomUUID(),
			tool_call_id: "review-1",
			task_sha256: sha256Hex("task"),
			judge_sha256: lane.judgeSha256,
		},
		subject: {
			work_id: lane.workId,
			revision_id: lane.revisionId,
			candidate_id: lane.candidateId,
			candidate_sha256: lane.candidateSha256,
			candidate_commit: lane.baseline,
		},
		check: { definition: "sealed_audit_manifest", version: 3, manifest_id: randomUUID(), task_sha256: sha256Hex("task") },
		result: "PASS",
		artifacts: [
			{ receipt_id: randomUUID(), kind: "verification", payload_sha256: sha256Hex("verification"), artifact_sha256: null },
			{ receipt_id: randomUUID(), kind: "audit", payload_sha256: sha256Hex("audit"), artifact_sha256: sha256Hex("report") },
			{ receipt_id: randomUUID(), kind: "push", payload_sha256: sha256Hex("push"), artifact_sha256: null },
		],
		delivery: {
			repository: lane.repo,
			remote_url: "",
			remote_ref: `refs/heads/execution/${KEY.toLowerCase()}`,
			candidate_commit: lane.baseline,
			remote_commit: lane.baseline,
		},
	};
	return {
		grantId: lane.grantId,
		expectedGrantVersion: 1,
		workId: lane.workId,
		attemptId: lane.attemptId,
		evidence,
		judgeSha256: lane.judgeSha256,
	};
}

/** The completion the pre-crash controller issued; its response is lost before or after the service commits it. */
async function completeAndLoseResponse(lane: Lane, input: CompletionCommandInput, mode: "before_commit" | "after_commit"): Promise<void> {
	lane.service.dropNextResponse(mode);
	const failure = await lane
		.backend()
		.completeExecutionItem(input)
		.then(
			() => undefined,
			(error: unknown) => error,
		);
	if (!(failure instanceof WorkError)) throw new Error(`lost response must surface as WorkError, got ${String(failure)}`);
	expect(failure.status).toBe(0);
	expect(lane.service.posts("complete_execution_item")).toBe(1);
	const { claims } = await readPendingClaims(lane.pendingDir);
	expect(claims.map(claim => claim.record.result)).toEqual([undefined]);
}

describe("OMP-246 contract 1 (controller): accepted report then same-response write", () => {
	test("recovery dirty-refusal: dirt on a submitted candidate refuses automatic recovery without any ledger write", async () => {
		const lane = await makeLane("single", { submitted: true });
		const run = runHost(lane, lane.backend(), [ownershipEntry(lane)]);
		// The post-report write reached the worktree only; the ledger saw nothing.
		fs.writeFileSync(path.join(lane.repo, SEALED_PATH), AFTER_BYTES);

		await run.start();

		// The dirt cannot ride into the submitted candidate: the reviewing-phase
		// recovery preflight refuses instead of continuing, and nothing is spent.
		expect(run.notices.filter(notice => notice.includes("dirty worktree"))).toHaveLength(1);
		expect(continuationsOf(run)).toHaveLength(0);
		expect(run.continuations).toHaveLength(0);
		expect(lane.service.posts()).toBe(0);
		expect(headCommit(lane.repo)).toBe(lane.baseline);
		expect(fs.readFileSync(path.join(lane.repo, SEALED_PATH), "utf8")).toBe(AFTER_BYTES);
		expect(lane.service.unmatched()).toEqual([]);
	});

	test("production path: an accepted begin_execution_review fences a write emitted later in the same response and leaves the frozen candidate untouched", async () => {
		const lane = await makeLane("single");
		// The worker's edit on the sealed path: this is what the report submits.
		fs.writeFileSync(path.join(lane.repo, SEALED_PATH), AFTER_BYTES);
		const controller = await controllerSession(lane, [
			toolCalls(reviewCall("call-review"), writeCall("call-write", LATE_BYTES)),
			// Only reached if the fence is missing: the loop continues to a second
			// provider call, and the queued checkpoint delivery may cost a third.
			trailingStop("must not be reached"),
			trailingStop("must not be reached"),
		]);

		await controller.session.prompt("report, then try to write");
		await controller.session.waitForIdle();

		// Submission accepted through production wiring: frozen, finalized, pushed, attempt begun.
		const accepted = controller.toolResult("call-review");
		expect(accepted.isError).toBe(false);
		expect(accepted.details).toMatchObject({ endTurn: true });
		expect(accepted.text).toContain("Close attempt begun (candidate frozen and pushed)");
		expect(lane.service.applied("finalize_candidate")).toBe(1);
		expect(lane.service.applied("begin_close_attempt")).toBe(1);
		const frozen = headCommit(lane.repo);
		expect(frozen).not.toBe(lane.baseline);
		expect(lane.service.candidate()?.commit_sha).toBe(frozen);

		// Fence: the later write in the same response never executes. The loop
		// raises the batch fence synchronously on the `endTurn` result, so the
		// queued exclusive write is paired with the synthetic "not executed"
		// result before it can start — independent of the session's abort timing.
		const refused = controller.toolResult("call-write");
		expect(refused).toMatchObject({ isError: true, text: expect.stringContaining("Tool was not executed") });
		expect(refused.details).toMatchObject({ __synthetic: true, source: "interrupt_skipped", executed: false });

		// Candidate immutability: the submitted bytes stay, the worktree stays clean, HEAD stays the frozen commit.
		expect(fs.readFileSync(path.join(lane.repo, SEALED_PATH), "utf8")).toBe(AFTER_BYTES);
		expect(dirtyPaths(lane.repo)).toEqual([]);
		expect(headCommit(lane.repo)).toBe(frozen);

		// No ledger mutation beyond the submission's own four POSTs (finalize, verification
		// receipt, push receipt, begin attempt) plus at most one checkpoint attestation.
		expect(lane.service.posts() - lane.service.posts("attest_checkpoint_delivery")).toBe(4);
		expect(lane.service.posts("attest_checkpoint_delivery")).toBeLessThanOrEqual(1);
		expect(lane.service.unmatched()).toEqual([]);
	});

	test("early-write control: a write ordered before begin_execution_review runs and its bytes are what the review freezes", async () => {
		const lane = await makeLane("single");
		const controller = await controllerSession(lane, [
			toolCalls(writeCall("call-write", AFTER_BYTES), reviewCall("call-review")),
			trailingStop("done"),
			trailingStop("done"),
		]);

		await controller.session.prompt("write, then report");
		await controller.session.waitForIdle();

		// The exclusive write ran first (scheduled ahead of the shared work call); the review then froze it.
		expect(controller.toolResult("call-write").isError).toBe(false);
		const accepted = controller.toolResult("call-review");
		expect(accepted.isError).toBe(false);
		expect(accepted.text).toContain("Close attempt begun (candidate frozen and pushed)");
		expect(lane.service.applied("finalize_candidate")).toBe(1);
		expect(lane.service.candidate()?.commit_sha).toBe(headCommit(lane.repo));
		expect(headCommit(lane.repo)).not.toBe(lane.baseline);
		expect(fs.readFileSync(path.join(lane.repo, SEALED_PATH), "utf8")).toBe(AFTER_BYTES);
		expect(dirtyPaths(lane.repo)).toEqual([]);
		expect(lane.service.unmatched()).toEqual([]);
	});

	test("production path: a shared bash write emitted later in response is fenced", async () => {
		const lane = await makeLane("single");
		fs.writeFileSync(path.join(lane.repo, SEALED_PATH), AFTER_BYTES);
		const controller = await controllerSession(lane, [
			toolCalls(reviewCall("call-review"), bashCall("call-bash", LATE_BYTES)),
			trailingStop("must not be reached"),
		]);

		await controller.session.prompt("report, then try bash write");
		await controller.session.waitForIdle();

		const accepted = controller.toolResult("call-review");
		expect(accepted.isError).toBe(false);
		const refused = controller.toolResult("call-bash");
		expect(refused.isError).toBe(true);
		expect(refused.text).toContain("Tool was not executed");
		expect(fs.readFileSync(path.join(lane.repo, SEALED_PATH), "utf8")).toBe(AFTER_BYTES);
		expect(dirtyPaths(lane.repo)).toEqual([]);
	});

	test.todo(
		"service-side refusal of a second finalize_candidate on a submitted candidate needs the native WorkService — NOT_ASSESSED here",
	);
});

describe("OMP-246 contract 2: sealed-path edit then crash before the report", () => {
	test("restart delivers one continuation from the committed reservation and keeps the edit, the phase, and the journal", async () => {
		const lane = await makeLane("single");
		fs.writeFileSync(path.join(lane.repo, SEALED_PATH), AFTER_BYTES);
		await reserveThenCrash(lane);

		const run = runHost(lane, lane.backend(), [ownershipEntry(lane)]);
		await run.start();

		const deliveries = continuationsOf(run);
		expect(deliveries).toHaveLength(1);
		expect(deliveries[0]?.details?.executionContinuation).toMatchObject({
			grantId: lane.grantId,
			preReservationVersion: 1,
			postVersion: 2,
			workId: lane.workId,
		});
		expect(run.continuations).toHaveLength(0);
		// The committed reservation was reused through GET /v1/operations: no second POST, no second budget slot.
		expect(lane.service.posts("set_execution_state")).toBe(1);
		expect(lane.service.grant().continuations_scheduled).toBe(1);
		expect(lane.service.grant().grant_version).toBe(2);
		const { claims } = await readPendingClaims(lane.pendingDir);
		expect(claims).toHaveLength(1);
		expect(claims[0]?.record.result).toMatchObject({ type: "set_execution_state" });
		// Durable interrupted artifact, still uncommitted on the grant baseline, item still executing, nothing accepted.
		expect(fs.readFileSync(path.join(lane.repo, SEALED_PATH), "utf8")).toBe(AFTER_BYTES);
		expect(headCommit(lane.repo)).toBe(lane.baseline);
		expect(lane.service.activeItem()?.phase).toBe("executing");
		expect(lane.service.posts("complete_execution_item")).toBe(0);
		expect(lane.service.posts("finalize_candidate")).toBe(0);
		expect(lane.service.unmatched()).toEqual([]);
	});

	test("edit outside the sealed paths keeps the artifact but refuses automatic recovery without spending the reservation", async () => {
		const lane = await makeLane("single");
		fs.writeFileSync(path.join(lane.repo, UNSEALED_PATH), "# scratch\n");
		await reserveThenCrash(lane);

		const run = runHost(lane, lane.backend(), [ownershipEntry(lane)]);
		await run.start();

		expect(run.notices.filter(notice => notice.includes(`dirty worktree outside sealed paths: ${UNSEALED_PATH}`))).toHaveLength(1);
		expect(continuationsOf(run)).toHaveLength(0);
		expect(lane.service.posts("set_execution_state")).toBe(1);
		expect(fs.readFileSync(path.join(lane.repo, UNSEALED_PATH), "utf8")).toBe("# scratch\n");
		expect(lane.service.activeItem()?.phase).toBe("executing");
		// Preflight refused before claim reconciliation: the committed reservation stays pending for a clean restart.
		const { claims } = await readPendingClaims(lane.pendingDir);
		expect(claims.map(claim => claim.record.result)).toEqual([undefined]);
	});

	test("known-bad: a deleted pending journal makes the restart mint a second reservation", async () => {
		const lane = await makeLane("single");
		fs.writeFileSync(path.join(lane.repo, SEALED_PATH), AFTER_BYTES);
		await reserveThenCrash(lane);
		for (const claim of (await readPendingClaims(lane.pendingDir)).claims) fs.rmSync(claim.path);

		const run = runHost(lane, lane.backend(), [ownershipEntry(lane)]);
		await run.start();

		// Without the journal the host cannot see the committed reservation and reserves again: the
		// duplicate the contract-2 assertion above discriminates (one POST, one budget slot).
		const deliveries = continuationsOf(run);
		expect(deliveries).toHaveLength(1);
		expect(deliveries[0]?.details?.executionContinuation).toMatchObject({ preReservationVersion: 2, postVersion: 3 });
		expect(lane.service.posts("set_execution_state")).toBe(2);
		expect(lane.service.grant().continuations_scheduled).toBe(2);
	});
});

describe("OMP-246 contract 3: exact completion command replay over the same journal (client replay; whole-process restart NOT_ASSESSED)", () => {
	test("response lost after commit: a fresh backend on the same journal replays the same bytes, the item completes once, the result is recovered", async () => {
		const lane = await makeLane("single");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "after_commit");
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		const completedAt = lane.service.items()[0]?.completed_at;
		expect(typeof completedAt).toBe("string");

		const recovered = await lane.backend().completeExecutionItem(input);

		expect(lane.service.posts("complete_execution_item")).toBe(2);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		expect(lane.service.replayed()).toBe(1);
		expect(lane.service.conflicts()).toBe(0);
		// One transition, one downstream effect: the single-mode grant completed exactly once.
		expect(lane.service.items()[0]?.completed_at).toBe(completedAt);
		expect(lane.service.grant()).toMatchObject({ state: "completed", grant_version: 2 });
		expect(recovered.grant).toMatchObject({ state: "completed", grant_version: 2 });
		expect(recovered.activeItem).toMatchObject({ work_id: lane.workId, phase: "completed", completed_at: completedAt });
		const { claims } = await readPendingClaims(lane.pendingDir);
		expect(claims.map(claim => (claim.record.result as { type?: string } | undefined)?.type)).toEqual(["complete_execution_item"]);
	});

	test("response lost before commit: a fresh backend on the same journal resends the same bytes and the service applies exactly once", async () => {
		const lane = await makeLane("single");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "before_commit");
		expect(lane.service.applied("complete_execution_item")).toBe(0);
		expect(lane.service.activeItem()?.phase).toBe("executing");

		const recovered = await lane.backend().completeExecutionItem(input);

		expect(lane.service.posts("complete_execution_item")).toBe(2);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		expect(lane.service.replayed()).toBe(0);
		expect(lane.service.grant()).toMatchObject({ state: "completed", grant_version: 2 });
		expect(recovered.activeItem?.phase).toBe("completed");
	});

	test("in-process duplicate completion hands back the stored result with zero additional POST", async () => {
		const lane = await makeLane("single");
		const input = completionInput(lane);
		const backend = lane.backend();
		const first = await backend.completeExecutionItem(input);
		expect(lane.service.posts("complete_execution_item")).toBe(1);

		const duplicate = await backend.completeExecutionItem(input);

		expect(lane.service.posts("complete_execution_item")).toBe(1);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		expect(duplicate.grant).toEqual(first.grant);
		expect(duplicate.activeItem).toEqual(first.activeItem);
	});

	test("known-bad: without the journal the replay mints a fresh operation id, the version CAS refuses it as revision_conflict, and the committed result is unrecoverable", async () => {
		const lane = await makeLane("single");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "after_commit");
		for (const claim of (await readPendingClaims(lane.pendingDir)).claims) fs.rmSync(claim.path);

		const failure = await lane
			.backend()
			.completeExecutionItem(input)
			.then(
				() => undefined,
				(error: unknown) => error,
			);

		if (!(failure instanceof WorkError)) throw new Error(`fresh operation must be refused, got ${String(failure)}`);
		expect(failure.code).toBe("revision_conflict");
		expect(failure.status).toBe(409);
		expect(lane.service.posts("complete_execution_item")).toBe(2);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		expect(lane.service.replayed()).toBe(0);
		// revision_conflict is raised inside the store transaction and rolled back: a
		// non-applying refusal, so the fresh claim is released instead of retained.
		expect((await readPendingClaims(lane.pendingDir)).claims).toEqual([]);
	});

	test("known-bad: altered claim bytes under the same operation id are an idempotency_conflict and the claim is retained", async () => {
		const lane = await makeLane("single");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "after_commit");
		const [claim] = (await readPendingClaims(lane.pendingDir)).claims;
		if (!claim) throw new Error("pending claim missing");
		const tampered = JSON.parse(fs.readFileSync(claim.path, "utf8")) as {
			envelope: { command: { payload: { judge_sha256: string } } };
		};
		tampered.envelope.command.payload.judge_sha256 = "f".repeat(64);
		fs.writeFileSync(claim.path, JSON.stringify(tampered));

		const failure = await lane
			.backend()
			.completeExecutionItem(input)
			.then(
				() => undefined,
				(error: unknown) => error,
			);

		if (!(failure instanceof WorkError)) throw new Error(`altered bytes must be refused, got ${String(failure)}`);
		expect(failure.code).toBe("idempotency_conflict");
		expect(lane.service.conflicts()).toBe(1);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		// Not a non-applying refusal: the claim stays on disk for manual repair instead of being dropped.
		expect(fs.existsSync(claim.path)).toBe(true);
		expect((await readPendingClaims(lane.pendingDir)).claims.map(row => row.record.result)).toEqual([undefined]);
	});

	test.todo(
		"whole-process controller restart reissue of complete_execution_item (SIGKILL, restart, native request-hash parity) needs the installed qualification in test_installed_execution_recovery.py, which has no complete_execution_item case — NOT_ASSESSED here",
	);

	test.todo(
		"end-to-end begin_execution_review completion path (freeze, push, auditor settle, complete) needs a git remote and the auditor — NOT_ASSESSED here",
	);
});

describe("OMP-246 queue mode: lost completion response, then owned session-start recovery", () => {
	test("restart reconciles the committed completion by operation identity, activates the next item once at the post-completion version, delivers one continuation; a second restart adds nothing", async () => {
		const lane = await makeLane("queue");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "after_commit");
		// Service truth after the lost response: item 0 committed complete, grant still
		// active at version 2 with nothing active, item 1 still pending.
		expect(lane.service.items().map(item => item.phase)).toEqual(["completed", "pending"]);
		expect(lane.service.grant()).toMatchObject({ state: "active", grant_version: 2 });
		expect(lane.service.activeItem()).toBeNull();
		const pendingCompletion = (await readPendingClaims(lane.pendingDir)).claims.find(
			claim => envelopeOf(claim).command.type === "complete_execution_item",
		);
		if (!pendingCompletion) throw new Error("completion claim missing from the journal");
		const completionOperationId = envelopeOf(pendingCompletion).operation_id;
		expect(pendingCompletion.record.result).toBeUndefined();
		expect(lane.service.operationLookups()).toEqual([]);

		const entries: SessionEntry[] = [ownershipEntry(lane)];
		const first = runHost(lane, lane.backend(), entries);
		await first.start();

		// Reconciliation: the original operation was read exactly once by identity
		// and resolved the claim with the stored result; the completion was never re-POSTed.
		expect(lane.service.operationLookups()).toEqual([completionOperationId]);
		expect(lane.service.posts("complete_execution_item")).toBe(1);
		expect(lane.service.applied("complete_execution_item")).toBe(1);
		expect(lane.service.replayed()).toBe(0);
		expect(lane.service.conflicts()).toBe(0);
		const claimsAfterFirst = (await readPendingClaims(lane.pendingDir)).claims;
		const reconciled = claimsAfterFirst.find(claim => envelopeOf(claim).operation_id === completionOperationId);
		expect(reconciled?.record.result).toMatchObject({ type: "complete_execution_item", work_id: lane.workId });
		expect(typeof reconciled?.record.resolved_at).toBe("string");

		// Activation: exactly one, CAS'd on the post-completion version, baseline = the lane HEAD.
		expect(lane.service.posts("activate_execution_item")).toBe(1);
		expect(lane.service.applied("activate_execution_item")).toBe(1);
		const activation = claimsAfterFirst.map(envelopeOf).find(envelope => envelope.command.type === "activate_execution_item");
		if (!activation || activation.command.type !== "activate_execution_item") throw new Error("activation claim missing from the journal");
		expect(activation.command.payload).toMatchObject({
			grant_id: lane.grantId,
			expected_grant_version: 2,
			position: 1,
			work_id: lane.nextWorkId,
			git_baseline: lane.baseline,
			judge_sha256: lane.judgeSha256,
		});
		expect(lane.service.operation(activation.operation_id)?.receipt.state).toBe("applied");
		expect(lane.service.items().map(item => item.phase)).toEqual(["completed", "criteria_pending"]);
		expect(lane.service.activeItem()).toMatchObject({ work_id: lane.nextWorkId, position: 1, current_git_baseline: lane.baseline });

		// Continuation: the existing session-start path reserved one slot on the
		// post-activation version and delivered one execute prompt bound to the next item.
		expect(lane.service.posts("set_execution_state")).toBe(1);
		expect(lane.service.grant()).toMatchObject({ state: "active", grant_version: 4, continuations_scheduled: 1 });
		const deliveries = continuationsOf(first);
		expect(deliveries).toHaveLength(1);
		expect(deliveries[0]?.details?.executionContinuation).toMatchObject({
			grantId: lane.grantId,
			preReservationVersion: 3,
			postVersion: 4,
			workId: lane.nextWorkId,
		});
		expect(first.continuations).toHaveLength(0);
		expect(first.notices.filter(notice => notice.includes("Execution recovery") || notice.includes("Recovery blocked"))).toEqual([]);
		// The execution binding survives the restart: the newest work-now entry still names the grant.
		const binding = first.entries.findLast(entry => entry.type === "custom" && entry.customType === "work-now");
		if (binding?.type !== "custom") throw new Error("ownership binding missing after restart");
		expect((binding.data as { executionWorkspace?: { grantId?: string } }).executionWorkspace?.grantId).toBe(lane.grantId);
		expect(headCommit(lane.repo)).toBe(lane.baseline);
		expect(lane.service.unmatched()).toEqual([]);

		// Second restart over the SAME journal and transcript: zero duplicate effects —
		// no completion, activation, reservation, or operation re-fetch.
		const second = runHost(lane, lane.backend(), entries);
		await second.start();

		expect(lane.service.operationLookups()).toEqual([completionOperationId]);
		expect(lane.service.posts()).toBe(3);
		expect(lane.service.applied()).toBe(3);
		expect(lane.service.replayed()).toBe(0);
		expect(lane.service.conflicts()).toBe(0);
		expect(lane.service.grant()).toMatchObject({ state: "active", grant_version: 4, continuations_scheduled: 1 });
		expect(lane.service.items().map(item => item.phase)).toEqual(["completed", "criteria_pending"]);
		expect(lane.service.activeItem()?.work_id).toBe(lane.nextWorkId);
		// The only message is the outbox replay of the SAME continuation identity
		// (contract 2's not-yet-injected replay), never a second continuation.
		const replays = continuationsOf(second);
		expect(replays).toHaveLength(1);
		expect(replays[0]?.details?.executionContinuation).toMatchObject({
			grantId: lane.grantId,
			preReservationVersion: 3,
			postVersion: 4,
			workId: lane.nextWorkId,
			messageId: deliveries[0]?.details?.executionContinuation?.messageId,
		});
		expect(second.continuations).toHaveLength(0);
		expect(second.notices.filter(notice => notice.includes("Execution recovery") || notice.includes("Recovery blocked"))).toEqual([]);
		expect(lane.service.unmatched()).toEqual([]);
	});

	test("response lost before commit: restart finds the operation unknown, refuses recovery, activates nothing, delivers nothing, and keeps the unresolved claim", async () => {
		const lane = await makeLane("queue");
		const input = completionInput(lane);
		await completeAndLoseResponse(lane, input, "before_commit");
		// Service truth after the lost response: nothing was recorded, item 0 still
		// executing at version 1, item 1 still pending; only the journal remembers the attempt.
		expect(lane.service.applied("complete_execution_item")).toBe(0);
		expect(lane.service.items().map(item => item.phase)).toEqual(["executing", "pending"]);
		expect(lane.service.grant()).toMatchObject({ state: "active", grant_version: 1, continuations_scheduled: 0 });
		const pendingCompletion = (await readPendingClaims(lane.pendingDir)).claims.find(
			claim => envelopeOf(claim).command.type === "complete_execution_item",
		);
		if (!pendingCompletion) throw new Error("completion claim missing from the journal");
		const completionOperationId = envelopeOf(pendingCompletion).operation_id;
		expect(lane.service.operation(completionOperationId)).toBeUndefined();

		const entries: SessionEntry[] = [ownershipEntry(lane)];
		const run = runHost(lane, lane.backend(), entries);
		await run.start();

		// Fail closed: the operation was looked up once by identity, the service does
		// not know it, and the host must not guess that the completion committed.
		expect(lane.service.operationLookups()).toEqual([completionOperationId]);
		const blocked = run.notices.filter(notice => notice.includes("Recovery blocked by unreadable claim"));
		expect(blocked).toHaveLength(1);
		expect(blocked[0]).toContain(`unresolved pending claim ${pendingCompletion.path}`);
		expect(blocked[0]).toContain(completionOperationId);
		expect(blocked[0]).toContain("automatic recovery refused; use stop/cancel or repair the claim");
		// No re-POST of the completion, no activation, no reservation: the only POST is the pre-crash one.
		expect(lane.service.posts("complete_execution_item")).toBe(1);
		expect(lane.service.posts("activate_execution_item")).toBe(0);
		expect(lane.service.posts("set_execution_state")).toBe(0);
		expect(lane.service.posts()).toBe(1);
		expect(lane.service.applied()).toBe(0);
		expect(lane.service.replayed()).toBe(0);
		expect(lane.service.conflicts()).toBe(0);
		expect(lane.service.items().map(item => item.phase)).toEqual(["executing", "pending"]);
		expect(lane.service.activeItem()).toMatchObject({ work_id: lane.workId, position: 0, phase: "executing" });
		expect(lane.service.grant()).toMatchObject({ state: "active", grant_version: 1, continuations_scheduled: 0 });
		expect(continuationsOf(run)).toHaveLength(0);
		expect(run.continuations).toHaveLength(0);
		// The unresolved claim stays on disk, untouched, for stop/cancel or manual repair.
		expect(fs.existsSync(pendingCompletion.path)).toBe(true);
		const [retained] = (await readPendingClaims(lane.pendingDir)).claims;
		expect(retained?.path).toBe(pendingCompletion.path);
		expect(retained?.record.result).toBeUndefined();
		expect(retained?.record.resolved_at).toBeUndefined();
		// The execution binding is left intact for the retry; nothing else moved.
		const binding = run.entries.findLast(entry => entry.type === "custom" && entry.customType === "work-now");
		if (binding?.type !== "custom") throw new Error("ownership binding missing after blocked restart");
		expect((binding.data as { executionWorkspace?: { grantId?: string } }).executionWorkspace?.grantId).toBe(lane.grantId);
		expect(headCommit(lane.repo)).toBe(lane.baseline);
		expect(lane.service.unmatched()).toEqual([]);
	});
});
