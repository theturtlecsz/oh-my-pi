import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import {
	type ExtensionAPI,
	type ExtensionCommandContext,
	type ExtensionContext,
	type SessionEntry,
} from "@oh-my-pi/pi-coding-agent";
import {
	type CommandEnvelope,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type StoredOperation,
	type UUID,
	payloadHash,
} from "@oh-my-pi/pi-work-client";
import { getProjectDir, setProjectDir } from "@oh-my-pi/pi-utils";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";
import { createWorkBackend } from "../extensions/workflow/work";

type StateChange = Parameters<WorkflowBackend["setExecutionState"]>[0];
type SkipItemCall = Parameters<WorkflowBackend["skipActiveItem"]>[0];
type ActivateItemCall = Parameters<WorkflowBackend["activateExecutionItem"]>[0];
type CommandHandler = (args: string, ctx: ExtensionCommandContext) => Promise<void>;
type InputHandler = (event: { originalText: string; source: string }, ctx: ExtensionContext) => Promise<unknown>;

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001" as UUID;
const OWNER_ID = "00000000-0000-7000-8000-000000000002" as UUID;
const GRANT_ID = "00000000-0000-7000-8000-000000000010";
const BASE_URL = "http://127.0.0.1:9999";

const directories: string[] = [];
let originalProjectDir: string;

beforeEach(() => {
	originalProjectDir = getProjectDir();
});

afterEach(async () => {
	vi.restoreAllMocks();
	setProjectDir(originalProjectDir);
	await Promise.all(directories.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

async function makeClaimsRecoveryHarness() {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "claims-recovery-repo-"));
	const pendingDir = await fs.mkdtemp(path.join(os.tmpdir(), "claims-recovery-pending-"));
	directories.push(directory, pendingDir);

	const defaultItem1: ExecutionGrantItemView = {
		item_id: "00000000-0000-7000-8000-000000000021",
		workspace_id: WORKSPACE_ID,
		grant_id: GRANT_ID,
		work_id: "work-1",
		position: 0,
		phase: "executing",
		claimed_revision_id: "revision-1",
		initial_git_baseline: "1".repeat(40),
		original_request: "Implement feature 1",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
	};

	const defaultItem2: ExecutionGrantItemView = {
		item_id: "00000000-0000-7000-8000-000000000022",
		workspace_id: WORKSPACE_ID,
		grant_id: GRANT_ID,
		work_id: "work-2",
		position: 1,
		phase: "pending",
		claimed_revision_id: "revision-2",
		initial_git_baseline: "1".repeat(40),
		original_request: "Implement feature 2",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
	};

	const items = [structuredClone(defaultItem1), structuredClone(defaultItem2)];

	let execution: ExecutionSnapshot = {
		grant: {
			grant_id: GRANT_ID,
			workspace_id: WORKSPACE_ID,
			owner_id: OWNER_ID,
			repository: directory,
			remote_ref: "refs/heads/execution/omp-1",
			state: "active",
			mode: "queue",
			grant_version: 3,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 1,
			authorization_hash: "3".repeat(64),
			judge_sha256: "4".repeat(64),
			created_at: "2026-09-25T00:00:00Z",
			expires_at: "2026-09-26T00:00:00Z",
		},
		items,
		activeItem: items[0] ?? null,
	};

	const workspace = {
		grantId: GRANT_ID,
		key: "OMP-1",
		primaryRoot: directory,
		path: directory,
		branch: "execution/omp-1",
		baseline: defaultItem1.initial_git_baseline,
		reused: false,
	};

	const branch: SessionEntry[] = [
		{
			type: "custom",
			customType: "work-now",
			id: "entry-binding",
			parentId: null,
			timestamp: new Date().toISOString(),
			data: {
				backend: "work",
				identifier: "OMP-1",
				issueId: "work-1",
				executingIssue: { id: "work-1", key: "OMP-1", title: "Task 1" },
				approvedPlan: { hash: "plan-hash", at: Date.now() },
				obligationHandoff: { armed: true, blockedOnce: false },
				obligationReview: { armed: true },
				carrier: { step: 1 },
				executionWorkspace: workspace,
			},
		},
	];

	const skipCalls: SkipItemCall[] = [];
	const activateCalls: ActivateItemCall[] = [];
	const stateCalls: StateChange[] = [];
	const sentMessages: unknown[] = [];
	const notifications: Array<{ message: string; type?: string }> = [];

	let fetchMode: "lost_skip" | "committed_skip" | "mismatch_reason" = "lost_skip";
	let postCount = 0;
	let getCount = 0;
	let skipPostCount = 0;
	let lastEnvelope: CommandEnvelope | null = null;

	const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const url = String(input);
		const method = init?.method ?? "GET";

		if (method === "POST" || url.endsWith("/v1/commands")) {
			postCount++;
			const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
			if (body?.command?.type === "skip_active_item") {
				skipPostCount++;
				lastEnvelope = body as CommandEnvelope;
			}
			if (fetchMode === "lost_skip") {
				throw new TypeError("fetch failed: connection reset");
			}
			return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
		}

		if (url.includes("/v1/operations/")) {
			getCount++;
			if (fetchMode === "lost_skip") {
				return new Response(
					JSON.stringify({ error: { code: "internal_error", diagnostics: ["server fault"] } }),
					{ status: 500, headers: { "Content-Type": "application/json" } },
				);
			}
			if (fetchMode === "committed_skip" && lastEnvelope) {
				const appliedOp: StoredOperation = {
					receipt: {
						operation_id: lastEnvelope.operation_id,
						request_id: lastEnvelope.request_id,
						state: "applied",
						request_sha256: payloadHash({
							api_version: lastEnvelope.api_version,
							workspace_id: lastEnvelope.workspace_id,
							command: lastEnvelope.command,
						}),
						result_sha256: "r".repeat(64),
						diagnostics: [],
					},
					command_type: "skip_active_item",
					request_id: lastEnvelope.request_id,
					correlation_id: lastEnvelope.correlation_id,
					result: {
						type: "skip_active_item",
						grant: structuredClone(execution.grant),
						item: structuredClone(execution.items[0]!),
						reason: (lastEnvelope.command.payload as { reason?: string })?.reason ?? "defer",
					},
				};
				return new Response(JSON.stringify(appliedOp), {
					status: 200,
					headers: { "Content-Type": "application/json" },
				});
			}
			if (fetchMode === "mismatch_reason" && lastEnvelope) {
				const differentHash = payloadHash({
					api_version: "work.omp.dev/v1",
					workspace_id: WORKSPACE_ID,
					command: {
						type: "skip_active_item",
						payload: {
							...(lastEnvelope.command.payload as object),
							reason: "different_reason",
						},
					},
				});
				const appliedOp: StoredOperation = {
					receipt: {
						operation_id: lastEnvelope.operation_id,
						request_id: lastEnvelope.request_id,
						state: "applied",
						request_sha256: differentHash,
						result_sha256: "r".repeat(64),
						diagnostics: [],
					},
					command_type: "skip_active_item",
					request_id: lastEnvelope.request_id,
					correlation_id: lastEnvelope.correlation_id,
					result: {
						type: "skip_active_item",
						grant: structuredClone(execution.grant),
						item: structuredClone(execution.items[0]!),
						reason: "different_reason",
					},
				};
				return new Response(JSON.stringify(appliedOp), {
					status: 200,
					headers: { "Content-Type": "application/json" },
				});
			}
		}

		return new Response("not found", { status: 404 });
	};

	const realBackend = createWorkBackend(
		{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
		() => "mock-token",
		mockFetch as never,
		pendingDir,
	);

	const sessionId = "session-1";
	const hostSessionId = "session-1";
	const cwd = directory;

	const issues = new Map<string, { id: string; key: string; title: string }>([
		["work-1", { id: "work-1", key: "OMP-1", title: "Task 1" }],
		["work-2", { id: "work-2", key: "OMP-2", title: "Task 2" }],
		["OMP-1", { id: "work-1", key: "OMP-1", title: "Task 1" }],
		["OMP-2", { id: "work-2", key: "OMP-2", title: "Task 2" }],
	]);

	const backend = {
		name: "work",
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(directory, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		executionChildren: async () => ({ umbrella: false, children: [] }),
		getExecution: async () => structuredClone(execution),
		findIssue: async (keyOrId: string) => issues.get(keyOrId) ?? null,
		getFocusVersion: async () => 5,
		getCommittedSkipClaims: realBackend.getCommittedSkipClaims?.bind(realBackend),
		acknowledgeSkipClaim: realBackend.acknowledgeSkipClaim?.bind(realBackend),
		skipActiveItem: async (input: SkipItemCall) => {
			skipCalls.push(input);
			return realBackend.skipActiveItem!(input);
		},
		activateExecutionItem: async (input: ActivateItemCall) => {
			activateCalls.push(input);
			const idx = execution.items.findIndex(i => i.work_id === input.workId);
			if (idx >= 0) {
				execution.items[idx] = {
					...execution.items[idx]!,
					phase: "executing",
					initial_git_baseline: input.gitBaseline,
				};
				execution.activeItem = execution.items[idx]!;
			}
			execution.grant.grant_version += 1;
			return structuredClone(execution);
		},
		setExecutionState: async (input: StateChange) => {
			stateCalls.push(input);
			execution.grant.state = input.targetState;
			execution.grant.terminal_reason = input.reason ?? null;
			execution.grant.grant_version += 1;
			if (input.targetState === "stopped" || input.targetState === "canceled") {
				execution.activeItem = null;
			}
			return structuredClone(execution);
		},
		workClient: realBackend.workClient,
		projectScopeExists: async () => true,
		currentNow: async () => ({ id: "work-1", key: "OMP-1", title: "Task 1" }),
	} as unknown as WorkflowBackend;

	const commands = new Map<string, CommandHandler>();
	const startHandlers: InputHandler[] = [];

	const pi = {
		zod: z,
		registerTool: () => {},
		registerCommand: (name: string, definition: { handler: CommandHandler }) => {
			commands.set(name, definition.handler);
		},
		registerFlag: () => {},
		registerMessageRenderer: () => {},
		on: (name: string, handler: InputHandler) => {
			if (name === "session_start") startHandlers.push(handler);
		},
		appendEntry: (customType: string, data: unknown) => {
			branch.push({
				type: "custom",
				customType,
				data,
				id: crypto.randomUUID(),
				parentId: branch.at(-1)?.id ?? null,
				timestamp: new Date().toISOString(),
			});
		},
		sendMessage: (message: unknown) => {
			sentMessages.push(message);
		},
		getSessionId: () => hostSessionId,
		logger: { warn: () => {} },
	} as unknown as ExtensionAPI;

	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: data => data.backend === "work",
		executionWorkspaceManager: {
			primaryRoot: async c => c,
			ensure: async (_cwd, key, grantId, baseline) => ({
				primaryRoot: directory,
				path: directory,
				branch: `execution/${key.toLowerCase()}`,
				grantId,
				baseline,
				reused: true,
			}),
			cleanup: async () => ({ cleaned: true, detail: "fixture workspace" }),
		},
	})(pi);

	const manager = {
		getCwd: () => cwd,
		getSessionId: () => sessionId,
		getBranch: () => branch,
		moveTo: async (target: string) => {
			// cwd = target;
		},
	};

	const context = {
		get cwd() {
			return cwd;
		},
		taskDepth: 0,
		abort: () => {},
		get sessionManager() {
			return manager;
		},
		ui: {
			notify: (message: string, type?: string) => {
				notifications.push({ message, type });
			},
			setStatus: () => {},
			theme: { fg: (_color: string, text: string) => text },
		},
	} as unknown as ExtensionCommandContext;

	// Default spies: clean tree, valid HEAD
	vi.spyOn(gitModule, "inProgressGitOp").mockReturnValue(false);
	vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
	vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));

	return {
		backend,
		realBackend,
		context,
		branch,
		directory,
		pendingDir,
		workspace,
		skipCalls,
		activateCalls,
		stateCalls,
		sentMessages,
		notifications,
		getSnapshot: () => structuredClone(execution),
		setSnapshot: (patch: {
			grant?: Partial<ExecutionSnapshot["grant"]>;
			items?: ExecutionGrantItemView[];
			activeItem?: ExecutionGrantItemView | null;
		}) => {
			if (patch.grant) Object.assign(execution.grant, patch.grant);
			if (patch.items) execution.items = patch.items;
			if ("activeItem" in patch) execution.activeItem = patch.activeItem ?? null;
		},
		setFetchMode: (mode: "lost_skip" | "committed_skip" | "mismatch_reason") => {
			fetchMode = mode;
		},
		get postCount() {
			return postCount;
		},
		get getCount() {
			return getCount;
		},
		get skipPostCount() {
			return skipPostCount;
		},
		command: async (args: string) => {
			const handler = commands.get("execute");
			if (!handler) throw new Error("execute command missing");
			await handler(args, context);
		},
		start: async () => {
			for (const handler of startHandlers) {
				await handler({ originalText: "", source: "test" }, context);
			}
		},
	};
}

describe("execution-skip-recovery-claims", () => {
	test("steps 1-3: lost response, recovery on session_start, and second session_start idempotence", async () => {
		const h = await makeClaimsRecoveryHarness();

		// 1. /execute skip OMP-1 defer: fetch POST fails with status 0; operation GET returns 500.
		// The command rejects with outcome unknown; exactly one skip_active_item claim file stays unresolved in pendingDir.
		await expect(h.command("skip OMP-1 defer")).rejects.toThrow(/outcome unknown/);

		const pendingFiles = await fs.readdir(h.pendingDir);
		expect(pendingFiles).toHaveLength(1);
		const claimPath = path.join(h.pendingDir, pendingFiles[0]!);
		const unresolvedContent = JSON.parse(await Bun.file(claimPath).text()) as {
			envelope: CommandEnvelope;
			result?: unknown;
		};
		expect(unresolvedContent.envelope.command.type).toBe("skip_active_item");
		expect((unresolvedContent.envelope.command.payload as { reason?: string })?.reason).toBe("defer");
		expect(unresolvedContent.result).toBeUndefined();

		// 2. Server now commits (fake execution snapshot shows item skipped, version +1, no active item);
		// operation GET returns the applied stored op whose receipt request_sha256 = payloadHash of the envelope.
		// Run session_start: zero skip POSTs, one activation, claim file deleted.
		const currentSnapshot = h.getSnapshot();
		const skippedItem: ExecutionGrantItemView = {
			...currentSnapshot.items[0]!,
			phase: "skipped",
			terminal_reason: "defer",
		};
		h.setSnapshot({
			grant: {
				grant_version: currentSnapshot.grant.grant_version + 1, // version 3 -> 4
			},
			items: [skippedItem, currentSnapshot.items[1]!],
			activeItem: null,
		});
		h.setFetchMode("committed_skip");

		const skipPostsBeforeStart = h.skipPostCount;
		const activationsBeforeStart = h.activateCalls.length;

		await h.start();

		// Zero skip POSTs during session_start
		expect(h.skipPostCount - skipPostsBeforeStart).toBe(0);

		// One activation during session_start
		expect(h.activateCalls.length - activationsBeforeStart).toBe(1);
		expect(h.activateCalls[0]!.workId).toBe("work-2");
		expect(h.activateCalls[0]!.position).toBe(1);

		// Claim file deleted
		const remainingFiles = await fs.readdir(h.pendingDir);
		expect(remainingFiles).toHaveLength(0);

		// 3. Run session_start again: zero POSTs, zero operation GETs, zero activation calls, no claim file.
		const postsBeforeSecond = h.postCount;
		const getsBeforeSecond = h.getCount;
		const activationsBeforeSecond = h.activateCalls.length;

		await h.start();

		expect(h.postCount - postsBeforeSecond).toBe(0);
		expect(h.getCount - getsBeforeSecond).toBe(0);
		expect(h.activateCalls.length - activationsBeforeSecond).toBe(0);
		expect(await fs.readdir(h.pendingDir)).toHaveLength(0);
	});

	test("step 4: mismatch run: stored op for a different reason -> claim file bytes unchanged, zero mutations, Execution recovery skipped warning", async () => {
		const h = await makeClaimsRecoveryHarness();

		// Setup lost skip command
		await expect(h.command("skip OMP-1 defer")).rejects.toThrow(/outcome unknown/);

		const pendingFiles = await fs.readdir(h.pendingDir);
		expect(pendingFiles).toHaveLength(1);
		const claimPath = path.join(h.pendingDir, pendingFiles[0]!);
		const bytesBefore = await Bun.file(claimPath).text();

		// Server now commits, but operation GET returns applied stored op for a different reason
		const currentSnapshot = h.getSnapshot();
		const skippedItem: ExecutionGrantItemView = {
			...currentSnapshot.items[0]!,
			phase: "skipped",
			terminal_reason: "different_reason",
		};
		h.setSnapshot({
			grant: {
				grant_version: currentSnapshot.grant.grant_version + 1,
			},
			items: [skippedItem, currentSnapshot.items[1]!],
			activeItem: null,
		});
		h.setFetchMode("mismatch_reason");

		const skipCallsBefore = h.skipCalls.length;
		const activateCallsBefore = h.activateCalls.length;
		const stateCallsBefore = h.stateCalls.length;

		await h.start();

		// Claim file bytes unchanged
		const bytesAfter = await Bun.file(claimPath).text();
		expect(bytesAfter).toBe(bytesBefore);

		// Zero mutations
		expect(h.skipCalls.length - skipCallsBefore).toBe(0);
		expect(h.activateCalls.length - activateCallsBefore).toBe(0);
		expect(h.stateCalls.length - stateCallsBefore).toBe(0);
		const lastBinding = h.branch.findLast(e => e.type === "custom" && e.customType === "work-now");
		expect(lastBinding?.data).not.toBeNull();

		// "Execution recovery skipped" warning notified
		expect(
			h.notifications.some(
				n => n.message.includes("Execution recovery skipped") && n.type === "warning",
			),
		).toBe(true);
	});
});
