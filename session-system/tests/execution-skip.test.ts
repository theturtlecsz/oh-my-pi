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
import type { ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { getProjectDir, setProjectDir } from "@oh-my-pi/pi-utils";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

type StateChange = Parameters<WorkflowBackend["setExecutionState"]>[0];
type SkipItemCall = Parameters<WorkflowBackend["skipActiveItem"]>[0];
type ActivateItemCall = Parameters<WorkflowBackend["activateExecutionItem"]>[0];
type CommandHandler = (args: string, ctx: ExtensionCommandContext) => Promise<void>;

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

async function makeSkipHarness(options?: {
	bound?: boolean;
	items?: ExecutionGrantItemView[];
	grantState?: ExecutionSnapshot["grant"]["state"];
}) {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "execution-skip-"));
	directories.push(directory);

	const defaultItem1: ExecutionGrantItemView = {
		item_id: "item-1",
		workspace_id: "workspace-1",
		grant_id: "grant-1",
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

	const items = options?.items ? structuredClone(options.items) : [defaultItem1];
	const activeItem = items.find(i => i.phase === "executing" || i.phase === "criteria_pending" || i.phase === "planning") ?? items[0] ?? null;

	let execution: ExecutionSnapshot = {
		grant: {
			grant_id: "grant-1",
			workspace_id: "workspace-1",
			owner_id: "owner-1",
			repository: directory,
			remote_ref: "refs/heads/execution/omp-1",
			state: options?.grantState ?? "active",
			mode: items.length > 1 ? "queue" : "single",
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
		activeItem: options?.grantState === "stopped" || options?.grantState === "canceled" ? null : activeItem,
	};

	const workspace = {
		grantId: "grant-1",
		key: "OMP-1",
		primaryRoot: directory,
		path: directory,
		branch: "execution/omp-1",
		baseline: defaultItem1.initial_git_baseline,
		reused: false,
	};

	const bound = options?.bound ?? true;
	const branch: SessionEntry[] = bound
		? [
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
			]
		: [];

	const skipCalls: SkipItemCall[] = [];
	const activateCalls: ActivateItemCall[] = [];
	const stateCalls: StateChange[] = [];
	const sentMessages: unknown[] = [];
	const notifications: Array<{ message: string; type?: string }> = [];

	let sessionId = "session-1";
	let hostSessionId = "session-1";
	let cwd = directory;

	const issues = new Map<string, { id: string; key: string; title: string }>([
		["work-1", { id: "work-1", key: "OMP-1", title: "Task 1" }],
		["work-2", { id: "work-2", key: "OMP-2", title: "Task 2" }],
		["OMP-1", { id: "work-1", key: "OMP-1", title: "Task 1" }],
		["OMP-2", { id: "work-2", key: "OMP-2", title: "Task 2" }],
	]);

	let activateShouldThrow = false;

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
		skipActiveItem: async (input: SkipItemCall) => {
			skipCalls.push(input);
			const idx = execution.items.findIndex(i => i.work_id === input.workId);
			if (idx >= 0) {
				execution.items[idx] = {
					...execution.items[idx]!,
					phase: "skipped",
					terminal_reason: input.reason,
				};
			}
			execution.grant.grant_version += 1;
			execution.activeItem = null;
			return structuredClone(execution);
		},
		activateExecutionItem: async (input: ActivateItemCall) => {
			activateCalls.push(input);
			if (activateShouldThrow) {
				throw new Error("Activation rejected by remote service");
			}
			const idx = execution.items.findIndex(i => i.work_id === input.workId);
			if (idx >= 0) {
				execution.items[idx] = {
					...execution.items[idx]!,
					phase: "executing",
					current_git_baseline: input.gitBaseline,
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
	} as unknown as WorkflowBackend;

	const commands = new Map<string, CommandHandler>();
	const pi = {
		zod: z,
		registerTool: () => {},
		registerCommand: (name: string, definition: { handler: CommandHandler }) => {
			commands.set(name, definition.handler);
		},
		registerFlag: () => {},
		registerMessageRenderer: () => {},
		on: () => {},
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

	let manager = {
		getCwd: () => cwd,
		getSessionId: () => sessionId,
		getBranch: () => branch,
		moveTo: async (target: string) => {
			cwd = target;
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
		context,
		branch,
		directory,
		workspace,
		skipCalls,
		activateCalls,
		stateCalls,
		sentMessages,
		notifications,
		getSnapshot: () => structuredClone(execution),
		setGrant: (patch: Partial<ExecutionSnapshot["grant"]>) => {
			Object.assign(execution.grant, patch);
		},
		setActiveItem: (item: ExecutionGrantItemView | null) => {
			execution.activeItem = item;
		},
		setCwd: (c: string) => {
			cwd = c;
		},
		setHostSession: (s: string) => {
			hostSessionId = s;
		},
		setManager: (m: typeof manager) => {
			manager = m;
		},
		setActivateShouldThrow: (val: boolean) => {
			activateShouldThrow = val;
		},
		command: async (args: string) => {
			const handler = commands.get("execute");
			if (!handler) throw new Error("execute command missing");
			await handler(args, context);
		},
	};
}

describe("/execute skip refusals (exact error notifications and zero mutation)", () => {
	test("wrong session (unbound session) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness({ bound: false });
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: this session does not own the active execution", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("wrong session (cwd drift from witness) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		h.setCwd("/foreign/directory");
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: this session does not own the active execution", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("wrong session (session ID drift from witness) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		h.setHostSession("foreign-session");
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: this session does not own the active execution", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("no grant (grant not active) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness({ grantState: "paused" });
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: no execution grant found", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("no grant (active item missing) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		h.setActiveItem(null);
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: no execution grant found", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("wrong item (active item is OMP-1, command target is OMP-2) refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		await h.command("skip OMP-2");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: active item is OMP-1, not OMP-2", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("dirty execution worktree refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		vi.spyOn(gitModule, "dirtyPaths").mockReturnValue(["modified-file.ts"]);
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: execution worktree is not clean", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("no HEAD commit refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		vi.spyOn(gitModule, "headCommit").mockReturnValue(null as unknown as string);
		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: no HEAD commit", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});

	test("pre-mutation ownership loss refuses with exact message and zero mutation", async () => {
		const h = await makeSkipHarness();
		let callCount = 0;
		// Return clean dirt, but drift session during ownership revalidation
		vi.spyOn(gitModule, "dirtyPaths").mockImplementation(() => {
			callCount++;
			return [];
		});
		vi.spyOn(gitModule, "headCommit").mockImplementation(() => {
			h.setCwd("/drifted/directory");
			return "1".repeat(40);
		});

		await h.command("skip OMP-1");
		expect(h.notifications).toEqual([
			{ message: "Cannot skip: this session does not own the active execution", type: "error" },
		]);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});
});

describe("/execute skip mid-queue advancement", () => {
	test("mid-queue advance calls skipActiveItem, clears post-skip state, activates next item, rebinds NOW, and delivers continuation", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });
		await h.command("skip OMP-1 skip for test reason");

		// skipActiveItem called with trimmed reason, position 0, work-1
		expect(h.skipCalls).toHaveLength(1);
		expect(h.skipCalls[0]).toMatchObject({
			grantId: "grant-1",
			expectedGrantVersion: 3,
			position: 0,
			workId: "work-1",
			reason: "skip for test reason",
			expectedFocusVersion: 5,
		});

		// activateExecutionItem called for next item (position 1, work-2)
		expect(h.activateCalls).toHaveLength(1);
		expect(h.activateCalls[0]).toMatchObject({
			grantId: "grant-1",
			position: 1,
			workId: "work-2",
			expectedRevisionId: "revision-2",
			gitBaseline: "1".repeat(40),
		});

		// Post-skip state cleared, and NOW rebound to OMP-2
		const lastBinding = h.branch.findLast(
			entry => entry.type === "custom" && entry.customType === "work-now",
		);
		expect(lastBinding?.type === "custom" ? lastBinding.data : undefined).toMatchObject({
			identifier: "OMP-2",
			issueId: "work-2",
			executingIssue: undefined,
			approvedPlan: undefined,
			obligationHandoff: undefined,
			obligationReview: undefined,
			carrier: undefined,
			executionWorkspace: expect.objectContaining({
				key: "OMP-2",
				grantId: "grant-1",
			}),
		});

		// Info notification and execution continuation delivered
		expect(h.notifications.some(n => n.message.includes("Skipped OMP-1") && n.type === "info")).toBe(true);
		expect(h.sentMessages.some((m: any) => m.customType === "work-execute")).toBe(true);
	});

	test("default reason is owner_skip when none provided", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });
		await h.command("skip OMP-1");

		expect(h.skipCalls[0]?.reason).toBe("owner_skip");
	});
});

describe("/execute skip last item arms cleanup", () => {
	test("last item skip calls localClear(ctx, false), keeps cleanupReady: true, no settleClosedIssue/terminalExecution", async () => {
		const h = await makeSkipHarness();
		await h.command("skip OMP-1");

		expect(h.skipCalls).toHaveLength(1);
		expect(h.activateCalls).toHaveLength(0);

		// Local clear performed: identifier/issueId cleared, lastDone NOT set
		const lastBinding = h.branch.findLast(
			entry => entry.type === "custom" && entry.customType === "work-now",
		);
		const data = (lastBinding?.type === "custom" ? lastBinding.data : undefined) as Record<string, unknown>;
		expect(data.identifier).toBeUndefined();
		expect(data.issueId).toBeUndefined();
		expect(data.lastDone).toBeUndefined();
		expect(data.executingIssue).toBeUndefined();
		expect(data.approvedPlan).toBeUndefined();
		expect(data.obligationHandoff).toBeUndefined();
		expect(data.obligationReview).toBeUndefined();
		expect(data.carrier).toBeUndefined();

		// cleanupReady: true is armed on the workspace binding
		expect(data.executionWorkspace).toMatchObject({
			grantId: "grant-1",
			cleanupReady: true,
		});

		// No terminalExecution recorded
		expect(data.terminalExecution).toBeUndefined();
		expect(h.stateCalls).toHaveLength(0);
	});
});

describe("/execute skip post-skip failure handling", () => {
	test("worktree becomes dirty post-skip causes CAS-stop with execution_worktree_not_clean and notice", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });

		let dirtyCount = 0;
		vi.spyOn(gitModule, "dirtyPaths").mockImplementation(() => {
			dirtyCount++;
			// First call (pre-mutation check) is clean; second call (post-skip recheck) is dirty
			if (dirtyCount > 1) return ["dirty-after-skip.ts"];
			return [];
		});

		await h.command("skip OMP-1");

		expect(h.skipCalls).toHaveLength(1);
		// CAS-stop called with execution_worktree_not_clean
		expect(h.stateCalls).toHaveLength(1);
		expect(h.stateCalls[0]).toMatchObject({
			grantId: "grant-1",
			targetState: "stopped",
			reason: "execution_worktree_not_clean",
		});

		// Warning notified and terminal status message sent
		expect(h.notifications.some(n => n.message.includes("execution_worktree_not_clean") && n.type === "warning")).toBe(true);
		expect(h.sentMessages.some((m: any) => m.customType === "work-execution-status")).toBe(true);
	});

	test("missing HEAD post-skip causes CAS-stop with no_head_commit and notice", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });

		let headCount = 0;
		vi.spyOn(gitModule, "headCommit").mockImplementation(() => {
			headCount++;
			// Pre-mutation HEAD is valid; post-skip recheck returns null
			if (headCount > 1) return null as unknown as string;
			return "1".repeat(40);
		});

		await h.command("skip OMP-1");

		expect(h.skipCalls).toHaveLength(1);
		expect(h.stateCalls).toHaveLength(1);
		expect(h.stateCalls[0]).toMatchObject({
			grantId: "grant-1",
			targetState: "stopped",
			reason: "no_head_commit",
		});
		expect(h.notifications.some(n => n.message.includes("no_head_commit") && n.type === "warning")).toBe(true);
	});

	test("activation handshake failure causes CAS-stop with activation_handshake_failed and notice", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });
		h.setActivateShouldThrow(true);

		await h.command("skip OMP-1");

		expect(h.skipCalls).toHaveLength(1);
		expect(h.stateCalls).toHaveLength(1);
		expect(h.stateCalls[0]).toMatchObject({
			grantId: "grant-1",
			targetState: "stopped",
			reason: "activation_handshake_failed",
		});
		expect(h.notifications.some(n => n.message.includes("activation_handshake_failed") && n.type === "warning")).toBe(true);
	});

	test("git baseline moved during activation causes CAS-stop with git_baseline_moved and notice", async () => {
		const item1: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-1",
			position: 0,
			phase: "executing",
			claimed_revision_id: "revision-1",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 1",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const item2: ExecutionGrantItemView = {
			item_id: "item-2",
			workspace_id: "workspace-1",
			grant_id: "grant-1",
			work_id: "work-2",
			position: 1,
			phase: "pending",
			claimed_revision_id: "revision-2",
			initial_git_baseline: "1".repeat(40),
			original_request: "Implement task 2",
			original_request_sha256: "3".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const h = await makeSkipHarness({ items: [item1, item2] });

		let headCount = 0;
		vi.spyOn(gitModule, "headCommit").mockImplementation(() => {
			headCount++;
			// 1: pre-mutation check ("111...")
			// 2: pre-activation recheck ("111...")
			// 3: post-activation verification ("999...") -> moved!
			if (headCount >= 3) return "9".repeat(40);
			return "1".repeat(40);
		});

		await h.command("skip OMP-1");

		expect(h.skipCalls).toHaveLength(1);
		expect(h.stateCalls).toHaveLength(1);
		expect(h.stateCalls[0]).toMatchObject({
			grantId: "grant-1",
			targetState: "stopped",
			reason: "git_baseline_moved",
		});
		expect(h.notifications.some(n => n.message.includes("git_baseline_moved") && n.type === "warning")).toBe(true);
	});
});
