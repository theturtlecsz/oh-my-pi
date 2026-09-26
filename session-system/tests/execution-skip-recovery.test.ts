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
import type { ExecutionGrantItemView, ExecutionGrantView } from "@oh-my-pi/pi-work-client";
import { getProjectDir, setProjectDir } from "@oh-my-pi/pi-utils";
import { z } from "zod";
import type { CommittedSkipClaim, ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

type StateChange = Parameters<WorkflowBackend["setExecutionState"]>[0];
type SkipItemCall = Parameters<WorkflowBackend["skipActiveItem"]>[0];
type ActivateItemCall = Parameters<WorkflowBackend["activateExecutionItem"]>[0];
type InputHandler = (event: { originalText: string; source: string }, ctx: ExtensionContext) => Promise<unknown>;

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

function createMockGrant(patch?: Partial<ExecutionGrantView>): ExecutionGrantView {
	return {
		grant_id: "grant-1",
		workspace_id: "workspace-1",
		owner_id: "owner-1",
		repository: "/tmp/repo",
		remote_ref: "refs/heads/execution/omp-1",
		state: "active",
		mode: "queue",
		grant_version: 4,
		max_continuations: 8,
		max_close_attempts: 5,
		max_no_progress: 3,
		continuations_scheduled: 1,
		authorization_hash: "3".repeat(64),
		judge_sha256: "4".repeat(64),
		created_at: "2026-09-25T00:00:00Z",
		expires_at: "2026-09-26T00:00:00Z",
		...patch,
	};
}

function createMockItem(patch?: Partial<ExecutionGrantItemView>): ExecutionGrantItemView {
	return {
		item_id: "item-1",
		workspace_id: "workspace-1",
		grant_id: "grant-1",
		work_id: "work-1",
		position: 0,
		phase: "skipped",
		claimed_revision_id: "revision-1",
		initial_git_baseline: "1".repeat(40),
		original_request: "Implement feature 1",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
		...patch,
	};
}

function createDefaultSkipClaim(patch?: {
	claimId?: string;
	payload?: Partial<CommittedSkipClaim["command"]["payload"]>;
	result?: Partial<CommittedSkipClaim["result"]>;
	resultGrant?: Partial<ExecutionGrantView>;
	resultItem?: Partial<ExecutionGrantItemView>;
}): CommittedSkipClaim {
	const grant = createMockGrant(patch?.resultGrant);
	const item = createMockItem({ position: 0, work_id: "work-1", phase: "skipped", ...patch?.resultItem });
	return {
		claimId: patch?.claimId ?? "claim-1",
		command: {
			type: "skip_active_item",
			payload: {
				grant_id: "grant-1",
				expected_grant_version: 3,
				position: 0,
				work_id: "work-1",
				expected_focus_version: 5,
				judge_sha256: "4".repeat(64),
				reason: "owner_skip",
				...patch?.payload,
			},
		},
		result: {
			type: "skip_active_item",
			grant,
			item,
			reason: "owner_skip",
			...patch?.result,
		},
	};
}

interface HarnessOptions {
	grantState?: ExecutionSnapshot["grant"]["state"];
	grantVersion?: number;
	items?: ExecutionGrantItemView[];
	activeItem?: ExecutionGrantItemView | null;
	claims?: CommittedSkipClaim[];
	claimsThrow?: Error;
}

async function makeRecoveryHarness(options?: HarnessOptions) {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "execution-skip-recovery-"));
	directories.push(directory);

	const defaultItem1 = createMockItem({ position: 0, work_id: "work-1", phase: "skipped" });
	const defaultItem2 = createMockItem({
		item_id: "item-2",
		work_id: "work-2",
		position: 1,
		phase: "pending",
		claimed_revision_id: "revision-2",
	});

	const items = options?.items ? structuredClone(options.items) : [defaultItem1, defaultItem2];
	const grant = createMockGrant({
		repository: directory,
		state: options?.grantState ?? "active",
		grant_version: options?.grantVersion ?? 4,
	});

	let execution: ExecutionSnapshot = {
		grant,
		items,
		activeItem: options?.activeItem !== undefined ? options.activeItem : null,
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
	const acknowledgedClaims: string[] = [];

	let claims: CommittedSkipClaim[] = options?.claims !== undefined
		? structuredClone(options.claims)
		: [createDefaultSkipClaim()];

	let claimsThrow = options?.claimsThrow;

	let hostSessionId = "session-1";
	const sessionId = "session-1";
	let cwd = directory;

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
		getCommittedSkipClaims: async (_grantId: string) => {
			if (claimsThrow) throw claimsThrow;
			return structuredClone(claims);
		},
		acknowledgeSkipClaim: async (claimId: string) => {
			acknowledgedClaims.push(claimId);
			claims = claims.filter(c => c.claimId !== claimId);
		},
		skipActiveItem: async (input: SkipItemCall) => {
			skipCalls.push(input);
			return structuredClone(execution);
		},
		activateExecutionItem: async (input: ActivateItemCall) => {
			activateCalls.push(input);
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
		workClient: {
			healthReady: async () => ({ ready: true, service_fingerprint: "5".repeat(64) }),
			workItem: async () => ({ work_id: "work-1", state: "IN_PROGRESS", project_id: null, revision: { revision_id: "revision-1" } }),
			workflow: async () => ({ item: { work_id: "work-1", state: "IN_PROGRESS", project_id: null, revision: { revision_id: "revision-1" } }, relations: [] }),
		},
		currentNow: async () => ({ id: "work-1", key: "OMP-1", title: "Task 1" }),
	} as unknown as WorkflowBackend;

	const startHandlers: InputHandler[] = [];
	const pi = {
		zod: z,
		registerTool: () => {},
		registerCommand: () => {},
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

	// Default spies: clean worktree, valid HEAD
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
		acknowledgedClaims,
		getSnapshot: () => structuredClone(execution),
		setClaims: (newClaims: CommittedSkipClaim[]) => {
			claims = newClaims;
		},
		setClaimsThrow: (err: Error) => {
			claimsThrow = err;
		},
		setActiveItem: (item: ExecutionGrantItemView | null) => {
			execution.activeItem = item;
		},
		setGrantState: (state: ExecutionSnapshot["grant"]["state"]) => {
			execution.grant.state = state;
		},
		setGrantVersion: (version: number) => {
			execution.grant.grant_version = version;
		},
		start: async () => {
			for (const handler of startHandlers) {
				await handler({ originalText: "", source: "startup" }, context);
			}
		},
	};
}

describe("execution skip recovery at session_start", () => {
	test("mid-queue: zero skip calls, one activate, continuation delivered, claim acknowledged", async () => {
		const h = await makeRecoveryHarness();
		await h.start();

		// Zero skip calls
		expect(h.skipCalls).toHaveLength(0);

		// One activate call targeting item 2
		expect(h.activateCalls).toHaveLength(1);
		expect(h.activateCalls[0]).toMatchObject({
			grantId: "grant-1",
			position: 1,
			workId: "work-2",
			expectedRevisionId: "revision-2",
			gitBaseline: "1".repeat(40),
		});

		// Continuation delivered
		expect(h.sentMessages.length).toBeGreaterThan(0);
		expect(
			h.notifications.some(
				n => n.message.includes("Skipped OMP-1") && n.message.includes("Advanced to next queue item OMP-2"),
			),
		).toBe(true);

		// Claim acknowledged
		expect(h.acknowledgedClaims).toEqual(["claim-1"]);

		// Rebound to OMP-2
		const lastBinding = h.branch.findLast(
			entry => entry.type === "custom" && entry.customType === "work-now",
		);
		expect(lastBinding?.type === "custom" ? lastBinding.data : undefined).toMatchObject({
			identifier: "OMP-2",
			issueId: "work-2",
		});
	});

	test("last item (grant completed): cleanup armed, claim acknowledged", async () => {
		const item1 = createMockItem({ position: 0, work_id: "work-1", phase: "skipped" });
		const h = await makeRecoveryHarness({ items: [item1] });
		await h.start();

		// Zero skip calls and zero activate calls
		expect(h.skipCalls).toHaveLength(0);
		expect(h.activateCalls).toHaveLength(0);

		// Cleanup armed
		const lastBinding = h.branch.findLast(
			entry => entry.type === "custom" && entry.customType === "work-now",
		);
		const data = (lastBinding?.type === "custom" ? lastBinding.data : undefined) as {
			executionWorkspace?: { cleanupReady?: boolean };
		};
		expect(data?.executionWorkspace?.cleanupReady).toBe(true);
		expect(
			h.notifications.some(
				n => n.message.includes("Skipped OMP-1") && n.message.includes("Execution grant completed"),
			),
		).toBe(true);

		// Claim acknowledged
		expect(h.acknowledgedClaims).toEqual(["claim-1"]);
	});

	describe("mismatches: no mutation, claim kept, warning notified", () => {
		test("mismatch on grant", async () => {
			const claim = createDefaultSkipClaim({ payload: { grant_id: "other-grant" } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match grant" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
			// Session binding kept intact (not unbound to null)
			const lastBinding = h.branch.findLast(e => e.type === "custom" && e.customType === "work-now");
			expect(lastBinding?.data).not.toBeNull();
		});

		test("mismatch on version transition", async () => {
			// expected_grant_version is 3, so result grant_version should be 4; test with 5
			const claim = createDefaultSkipClaim({ resultGrant: { grant_version: 5 } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match version transition" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});

		test("mismatch on work_id", async () => {
			const claim = createDefaultSkipClaim({ resultItem: { work_id: "other-work-id" } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match work_id" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});

		test("mismatch on position", async () => {
			const claim = createDefaultSkipClaim({ resultItem: { position: 1 } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match position" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});

		test("mismatch on reason", async () => {
			const claim = createDefaultSkipClaim({ result: { reason: "unmatched-reason" } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match reason" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});

		test("mismatch on phase", async () => {
			const claim = createDefaultSkipClaim({ resultItem: { phase: "executing" } });
			const h = await makeRecoveryHarness({ claims: [claim] });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match phase" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});

		test("mismatch on current state (activeItem is still populated)", async () => {
			const item1 = createMockItem({ position: 0, work_id: "work-1", phase: "skipped" });
			const h = await makeRecoveryHarness({ activeItem: item1 });
			await h.start();

			expect(
				h.notifications.some(
					n => n.message === "Execution recovery skipped: committed skip does not match current state" && n.type === "warning",
				),
			).toBe(true);
			expect(h.acknowledgedClaims).toHaveLength(0);
			expect(h.skipCalls).toHaveLength(0);
			expect(h.activateCalls).toHaveLength(0);
			expect(h.stateCalls).toHaveLength(0);
		});
	});

	test("second session_start: zero skip/activate calls; consumed claim acknowledged only", async () => {
		// exec grant version is 5 (higher than result version 4), item 0 is still skipped, item 2 is executing
		const item1 = createMockItem({ position: 0, work_id: "work-1", phase: "skipped" });
		const item2 = createMockItem({ item_id: "item-2", work_id: "work-2", position: 1, phase: "executing" });
		const h = await makeRecoveryHarness({
			grantVersion: 5,
			items: [item1, item2],
			activeItem: item2,
		});

		await h.start();

		// Zero skip and activate calls
		expect(h.skipCalls).toHaveLength(0);
		expect(h.activateCalls).toHaveLength(0);

		// Consumed claim acknowledged only
		expect(h.acknowledgedClaims).toEqual(["claim-1"]);

		// Existing flow continues: no skip-recovery warning
		expect(h.notifications.some(n => n.message.includes("committed skip does not match"))).toBe(false);
	});

	test("claim read throws: warn, no mutation, keep binding, return", async () => {
		const h = await makeRecoveryHarness({
			claimsThrow: new Error("disk I/O error reading claims"),
		});
		await h.start();

		expect(
			h.notifications.some(
				n => n.message === "Execution recovery skipped: disk I/O error reading claims" && n.type === "warning",
			),
		).toBe(true);
		expect(h.acknowledgedClaims).toHaveLength(0);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.activateCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);

		// Binding preserved (not unbound)
		const lastBinding = h.branch.findLast(e => e.type === "custom" && e.customType === "work-now");
		expect(lastBinding?.data).not.toBeNull();
	});

	test("multiple claims: mismatch, claim kept, no mutation", async () => {
		const claim1 = createDefaultSkipClaim({ claimId: "claim-1" });
		const claim2 = createDefaultSkipClaim({ claimId: "claim-2" });
		const h = await makeRecoveryHarness({ claims: [claim1, claim2] });
		await h.start();

		expect(
			h.notifications.some(
				n => n.message.includes("Execution recovery skipped: committed skip does not match") && n.type === "warning",
			),
		).toBe(true);
		expect(h.acknowledgedClaims).toHaveLength(0);
		expect(h.skipCalls).toHaveLength(0);
		expect(h.activateCalls).toHaveLength(0);
		expect(h.stateCalls).toHaveLength(0);
	});
});
