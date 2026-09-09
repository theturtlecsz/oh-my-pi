import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import { SessionManager, type ExtensionAPI, type ExtensionCommandContext, type ExtensionContext, type SessionEntry } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { getProjectDir, setProjectDir } from "@oh-my-pi/pi-utils";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";

type StateChange = Parameters<WorkflowBackend["setExecutionState"]>[0];
type InputHandler = (event: { originalText: string; source: string }, ctx: ExtensionContext) => Promise<unknown>;
type CommandHandler = (args: string, ctx: ExtensionCommandContext) => Promise<void>;
type ToolHandler = (
	id: string,
	params: { action: string; work?: string; body?: string },
	signal: AbortSignal,
	onUpdate: undefined,
	ctx: ExtensionContext,
) => Promise<{ content: Array<{ type: "text"; text: string }>; details: { success: boolean } }>;

const directories: string[] = [];
const nativeManagers: SessionManager[] = [];
let originalProjectDir: string;
beforeEach(() => { originalProjectDir = getProjectDir(); });

afterEach(async () => {
	vi.restoreAllMocks();
	for (const manager of nativeManagers.splice(0)) await manager.close();
	setProjectDir(originalProjectDir);
	await Promise.all(directories.splice(0).map(directory => fs.rm(directory, { recursive: true, force: true })));
});

async function makeHarness(state: ExecutionSnapshot["grant"]["state"] = "active", repository?: string, bound = true) {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "execution-halt-"));
	directories.push(directory);
	const item: ExecutionGrantItemView = {
		item_id: "item-1",
		workspace_id: "workspace-1",
		grant_id: "grant-1",
		work_id: "work-1",
		position: 0,
		phase: "executing",
		claimed_revision_id: "revision-1",
		initial_git_baseline: "1".repeat(40),
		original_request: "Implement the approved change",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
	};
	let execution: ExecutionSnapshot = {
		grant: {
			grant_id: "grant-1",
			workspace_id: "workspace-1",
			owner_id: "owner-1",
			repository: repository ?? directory,
			remote_ref: "refs/heads/execution/omp-1",
			state,
			mode: "single",
			grant_version: 3,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 1,
			authorization_hash: "3".repeat(64),
			judge_sha256: "4".repeat(64),
			created_at: "2026-09-05T00:00:00Z",
			expires_at: "2026-09-06T00:00:00Z",
		},
		items: [item],
		activeItem: item,
	};
	const stateChanges: StateChange[] = [];
	let nativeSession: SessionManager | undefined;
	const sentMessages: unknown[] = [];
	const workspaceEffects: string[] = [];
	const workspace = { grantId: "grant-1", key: "OMP-1", primaryRoot: directory, path: directory, branch: "execution/omp-1", baseline: item.initial_git_baseline, reused: false };
	const branch: SessionEntry[] = bound ? [{ type: "custom", customType: "work-now", id: "original-binding", parentId: null, timestamp: new Date().toISOString(), data: { backend: "work", executionWorkspace: workspace } }] : [];
	const lookups: Array<string | undefined> = [];
	let lookupGate: Promise<void> | undefined;
	let lookupEntered: (() => void) | undefined;
	let ensureGate: Promise<void> | undefined;
	let ensureEntered: (() => void) | undefined;
	let sessionId = "session-1";
	let hostSessionId = "session-1";
	let cwd = directory;
	const stateCalls: StateChange[] = [];
	let stateResponseHook: (() => void) | undefined;
	const ensures: Array<{ cwd: string; key: string; baseline: string }> = [];
	const backend = {
		name: "work",
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(directory, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		getExecution: async (selector?: string) => {
			lookups.push(selector);
			lookupEntered?.();
			await lookupGate;
			return structuredClone(execution);
		},
		setNowRemote: async () => {},
		currentNow: async () => ({ id: item.work_id, key: "OMP-1", title: "Development change" }),
		pendingDeliveries: async () => [],
		getPendingExecutionClaims: async () => [],
		issueDetail: async () => ({}),
		findIssue: async () => ({ id: item.work_id, key: "OMP-1", title: "Development change" }),
		setExecutionState: async (input: StateChange) => {
			stateCalls.push(input);
			if (input.expectedGrantVersion !== execution.grant.grant_version) throw new Error("Execution version CAS refused");
			stateChanges.push(input);
			execution = {
				...execution,
				grant: {
					...execution.grant,
					state: input.targetState,
					terminal_reason: input.reason,
					grant_version: execution.grant.grant_version + 1,
				},
				activeItem: input.targetState === "stopped" || input.targetState === "canceled" ? null : item,
			};
			stateResponseHook?.();
			return execution;
		},
		workClient: {
			healthReady: async () => ({ ready: true, service_fingerprint: "5".repeat(64) }),
			workItem: async () => ({ work_id: item.work_id, state: "IN_PROGRESS", project_id: null, revision: { revision_id: item.claimed_revision_id } }),
			workflow: async () => ({ relations: [] }),
		},
	} as unknown as WorkflowBackend;
	const inputHandlers: InputHandler[] = [];
	const startHandlers: InputHandler[] = [];
	const switchHandlers: Array<(event: { reason: string }, ctx: ExtensionContext) => Promise<unknown>> = [];
	const beforeStartHandlers: InputHandler[] = [];
	const commands = new Map<string, CommandHandler>();
	let executeTool: ToolHandler | undefined;
	const pi = {
		zod: z,
		registerTool: (definition: { execute: ToolHandler }) => {
			executeTool = definition.execute;
		},
		registerCommand: (name: string, definition: { handler: CommandHandler }) => {
			commands.set(name, definition.handler);
		},
		registerFlag: () => {},
		registerMessageRenderer: () => {},
		on: (name: string, handler: InputHandler) => {
			if (name === "input") inputHandlers.push(handler);
			if (name === "session_start") startHandlers.push(handler);
			if (name === "before_agent_start") beforeStartHandlers.push(handler);
			if (name === "session_switch") switchHandlers.push(handler as unknown as (event: { reason: string }, ctx: ExtensionContext) => Promise<unknown>);
		},
		appendEntry: (customType: string, data: unknown) => {
			if (nativeSession) nativeSession.appendCustomEntry(customType, data);
			else branch.push({ type: "custom", customType, data, id: crypto.randomUUID(), parentId: branch.at(-1)?.id ?? null, timestamp: new Date().toISOString() });
		},
		sendMessage: (message: unknown) => { sentMessages.push(message); },
		getSessionId: () => hostSessionId,
		logger: { warn: () => {} },
	} as unknown as ExtensionAPI;
	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: data => data.backend === "work",
		executionWorkspaceManager: {
			primaryRoot: async cwd => cwd,
			ensure: async (_cwd, key, grantId, baseline) => {
				workspaceEffects.push("ensure");
				ensures.push({ cwd: _cwd, key, baseline });
				ensureEntered?.();
				await ensureGate;
				return {
				primaryRoot: directory,
				path: directory,
				branch: `execution/${key.toLowerCase()}`,
				grantId,
				baseline,
				reused: true,
				};
			},
			cleanup: async () => ({ cleaned: true, detail: "fixture workspace" }),
		},
	})(pi);
	const notifications: string[] = [];
	let aborts = 0;
	let abortHook: (() => void) | undefined;
	let manager = { getCwd: () => cwd, getSessionId: () => sessionId, getBranch: () => branch, moveTo: async (target: string) => { workspaceEffects.push("moveTo"); cwd = target; } };
	const context = {
		get cwd() { return nativeSession?.getCwd() ?? cwd; },
		taskDepth: 0,
		abort: () => {
			aborts += 1;
			abortHook?.();
		},
		get sessionManager() { return manager; },
		newSession: async (options: Parameters<ExtensionCommandContext["newSession"]>[0]) => {
			workspaceEffects.push("newSession");
			if (nativeSession) {
				await nativeSession.newSession({ parentSession: options?.parentSession });
				hostSessionId = nativeSession.getSessionId();
				for (const handler of switchHandlers) await handler({ reason: "new" }, context);
				await options?.setup?.(nativeSession);
			}
			return { cancelled: false };
		},
		ui: {
			notify: (message: string) => {
				notifications.push(message);
			},
			setStatus: () => {},
			theme: { fg: (_color: string, text: string) => text },
		},
	} as unknown as ExtensionCommandContext;
	return {
		backend,
		context,
		sentMessages,
		useNativeSession: (native: SessionManager) => { nativeSession = native; manager = native; hostSessionId = native.getSessionId(); },
		directory,
		workspace,
		stateCalls,
		ensures,
		preparePrompt: async () => { const results: unknown[] = []; for (const handler of beforeStartHandlers) results.push(await handler({ originalText: "", source: "tui" }, context)); return results; },
		onStateResponse: (callback: () => void) => { stateResponseHook = callback; },
		setItem: (patch: Partial<ExecutionGrantItemView>) => { Object.assign(item, patch); },
		appendState: (data: unknown) => { branch.push({ type: "custom", customType: "work-now", data, id: crypto.randomUUID(), parentId: branch.at(-1)?.id ?? null, timestamp: new Date().toISOString() }); },
		setCwd: (value: string) => { cwd = value; },
		setSession: (value: string) => { sessionId = value; hostSessionId = value; },
		setHostSession: (value: string) => { hostSessionId = value; },
		replaceManager: () => { manager = { ...manager }; },
		setGrant: (patch: Partial<ExecutionSnapshot["grant"]>) => { Object.assign(execution.grant, patch); },
		onAbort: (callback: () => void) => { abortHook = callback; },
		holdLookup: () => { const entered = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>(); lookupEntered = entered.resolve; lookupGate = release.promise; return { entered: entered.promise, release: release.resolve }; },
		holdEnsure: () => { const entered = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>(); ensureEntered = entered.resolve; ensureGate = release.promise; return { entered: entered.promise, release: release.resolve }; },
		poisonCache: () => Bun.write(path.join(directory, "cache.json"), JSON.stringify({ executionWorkspace: workspace })),
		now: async () => { await commands.get("now")!("OMP-1", context); },
		switchSession: async () => { for (const handler of switchHandlers) await handler({ reason: "new" }, context); },
		branch,
		lookups,
		getSnapshot: () => structuredClone(execution),
		workspaceEffects,
		setPhase: (phase: ExecutionGrantItemView["phase"]) => { item.phase = phase; },
		start: async () => { for (const handler of startHandlers) await handler({ originalText: "", source: "startup" }, context); },
		stateChanges,
		notifications,
		getAborts: () => aborts,
		input: async (text: string) => {
			for (const handler of inputHandlers) await handler({ originalText: text, source: "tui" }, context);
		},
		command: async (args: string) => {
			const handler = commands.get("execute");
			if (!handler) throw new Error("execute command missing");
			await handler(args, context);
		},
		stop: async () => {
			if (!executeTool) throw new Error("work tool missing");
			return executeTool(
				"stop-1",
				{ action: "stop_execution", work: "OMP-1", body: "owner requested a halt" },
				new AbortController().signal,
				undefined,
				context,
			);
		},
		review: async () => {
			if (!executeTool) throw new Error("work tool missing");
			return executeTool("review-1", { action: "begin_execution_review", work: "OMP-1", body: "Focused verification passed" }, new AbortController().signal, undefined, context);
		},
	};
}

async function nativeResumeHarness(differentCwd = false) {
	const h = await makeHarness("paused", undefined, false);
	const origin = differentCwd ? path.join(h.directory, "origin") : h.directory;
	await fs.mkdir(origin, { recursive: true });
	const sessionDir = path.join(h.directory, "native-sessions");
	const native = SessionManager.create(origin, sessionDir);
	nativeManagers.push(native);
	const move = native.moveTo.bind(native);
	vi.spyOn(native, "moveTo").mockImplementation(target => move(target, sessionDir));
	h.useNativeSession(native);
	vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({ agents: [{ name: "auditor", description: "Fixture auditor", systemPrompt: "", model: ["@audit"], output: { properties: { report: { type: "string" } } }, source: "bundled" }], projectAgentsDir: null });
	vi.spyOn(gitModule, "inProgressGitOp").mockReturnValue(false);
	vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
	vi.spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
	const tcb = await computeAuditTcb(h.context, h.backend.workClient!);
	h.setGrant({ judge_sha256: tcb.judgeSha256, expires_at: new Date(Date.now() + 86400000).toISOString() });
	return { h, native };
}

describe("execution session journal materialization", () => {
	test("native flush alone stays lazy; ensureOnDisk publishes custom state before closure", async () => {
		const { h, native } = await nativeResumeHarness();
		const id = native.appendCustomEntry("work-now", { backend: "work", executionWorkspace: h.workspace });
		await native.flush();
		expect(native.getEntry(id)?.type).toBe("custom");
		expect(await Bun.file(native.getSessionFile()!).exists()).toBe(false);
		await native.ensureOnDisk();
		await native.flush();
		const saved = Bun.JSONL.parse(await Bun.file(native.getSessionFile()!).text()) as SessionEntry[];
		expect(saved.find(entry => entry.id === id)?.type).toBe("custom");
	});

	for (const kind of ["same-cwd-unbound", "different-cwd", "foreign", "superseded"] as const) {
		test(`${kind} explicit resume materializes its new owning frame before dispatch`, async () => {
			const { h, native } = await nativeResumeHarness(kind === "different-cwd");
			if (kind === "foreign" || kind === "superseded") {
				native.appendCustomEntry("work-now", { backend: "work", executionWorkspace: { ...h.workspace, grantId: kind === "foreign" ? "another-grant" : h.workspace.grantId } });
				if (kind === "superseded") native.appendCustomEntry("work-now", { backend: "work" });
			}
			const previousId = native.getSessionId();
			await h.command("resume OMP-1");
			expect(h.getSnapshot().grant.state).toBe("active");
			expect(h.sentMessages).toHaveLength(1);
			expect(native.getSessionId()).not.toBe(previousId);
			expect(native.getHeader().parentSession).toBe(previousId);
			expect(native.getEntries().some(entry => entry.type === "message" && entry.message.role === "assistant")).toBe(false);
			const saved = Bun.JSONL.parse(await Bun.file(native.getSessionFile()!).text()) as SessionEntry[];
			const binding = saved.findLast(entry => entry.type === "custom" && entry.customType === "work-now");
			expect(binding?.type === "custom" ? binding.data : undefined).toMatchObject({ executionWorkspace: { grantId: h.getSnapshot().grant.grant_id, path: h.directory } });
		});
	}

	test("owned in-place resume retains session and progress despite newer recovery baseline", async () => {
		const { h, native } = await nativeResumeHarness();
		native.appendCustomEntry("work-now", { backend: "work", executionWorkspace: h.workspace });
		const progressId = native.appendCustomEntry("progress", { completed: "first step" });
		await native.ensureOnDisk();
		const previousId = native.getSessionId();
		h.setItem({ current_git_baseline: "2".repeat(40) });
		vi.spyOn(gitModule, "headCommit").mockReturnValue("2".repeat(40));
		await h.command("resume OMP-1");
		expect(h.getSnapshot().grant.state).toBe("active");
		expect(h.sentMessages).toHaveLength(1);
		expect(h.workspaceEffects).not.toContain("newSession");
		expect(native.getSessionId()).toBe(previousId);
		expect(native.getEntry(progressId)?.type).toBe("custom");
	});

	for (const failure of ["materialization", "drain"] as const) {
		test(`setup ${failure} rejection blocks binding publication and dispatch`, async () => {
			const { h, native } = await nativeResumeHarness();
			vi.spyOn(native, failure === "materialization" ? "ensureOnDisk" : "flush").mockRejectedValue(new Error(`setup ${failure} refused`));
			await h.command("resume OMP-1");
			expect(h.notifications.some(message => message.includes(`setup ${failure} refused`))).toBe(true);
			expect(h.getSnapshot().grant.state).toBe("paused");
			expect(h.stateCalls).toEqual([]);
			expect(h.sentMessages).toEqual([]);
			expect(native.getEntries().filter(entry => entry.type === "custom" && entry.customType === "work-now")).toHaveLength(0);
		});
	}
});

describe("execution halt remains available when development breaks the auditor", () => {
	test("unrelated owner input cannot abort its turn or pause another session's grant", async () => {
		const harness = await makeHarness("active", undefined, false);
		const before = harness.getSnapshot();
		await harness.input("Inspect this unrelated repository");
		expect(harness.getAborts()).toBe(0);
		expect(harness.stateChanges).toEqual([]);
		expect(harness.getSnapshot()).toEqual(before);
	});

	test("fresh unrelated startup cannot manufacture an execution binding before owner input", async () => {
		const harness = await makeHarness("active", undefined, false);
		const before = harness.getSnapshot();
		await harness.start();
		await harness.input("Continue unrelated work");
		expect(harness.workspaceEffects).toEqual([]);
		expect(harness.branch.filter(entry => entry.type === "custom" && entry.customType === "work-now" && (entry.data as { executionWorkspace?: unknown }).executionWorkspace)).toHaveLength(0);
		expect(harness.getAborts()).toBe(0);
		expect(harness.stateChanges).toEqual([]);
		expect(harness.getSnapshot()).toEqual(before);
	});

	test("owner interjection pauses the current grant even when auditor discovery fails", async () => {
		const harness = await makeHarness();
		vi.spyOn(taskModule, "discoverAgents").mockRejectedValue(new Error("auditor source unavailable"));
		await harness.input("Please stop and let me inspect this failure");
		expect(harness.getAborts()).toBe(1);
		expect(harness.stateChanges).toEqual([
			{
				grantId: "grant-1",
				expectedGrantVersion: 3,
				targetState: "paused",
				reason: "owner_interjection",
				judgeSha256: "4".repeat(64),
			},
		]);
	});

	test("owner cancel terminates the grant when no auditor is installed", async () => {
		const harness = await makeHarness("active", "legacy-repo");
		vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({ agents: [], projectAgentsDir: null });
		await harness.command("cancel OMP-1");
		expect(harness.stateChanges).toEqual([
			{
				grantId: "grant-1",
				expectedGrantVersion: 3,
				targetState: "canceled",
				reason: "owner_cancel",
				judgeSha256: "4".repeat(64),
			},
		]);
		expect(harness.notifications.some(message => message.includes("Execution grant canceled"))).toBe(true);
		expect(harness.getSnapshot().grant.repository).toBe("legacy-repo");
	});

	test("stop_execution records its terminal reason despite unavailable auditor source", async () => {
		const harness = await makeHarness("active", "legacy-repo");
		vi.spyOn(taskModule, "discoverAgents").mockRejectedValue(new Error("auditor source unavailable"));
		const result = await harness.stop();
		expect(result.details.success).toBe(true);
		expect(harness.stateChanges).toEqual([
			{
				grantId: "grant-1",
				expectedGrantVersion: 3,
				targetState: "stopped",
				reason: "owner requested a halt",
				judgeSha256: "4".repeat(64),
			},
		]);
		expect(result.content[0]?.text).toContain("terminal; resume is impossible");
		expect(harness.getSnapshot().grant.repository).toBe("legacy-repo");
	});

	test("resume still refuses an unavailable auditor without reactivating the grant", async () => {
		const harness = await makeHarness("paused");
		vi.spyOn(gitModule, "inProgressGitOp").mockReturnValue(false);
		vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		vi.spyOn(taskModule, "discoverAgents").mockRejectedValue(new Error("auditor source unavailable"));
		await harness.command("resume OMP-1");
		expect(harness.stateChanges).toEqual([]);
		expect(
			harness.notifications.some(
				message =>
					message.includes("judge TCB computation failed") && message.includes("auditor source unavailable"),
			),
		).toBe(true);
	});
});

describe("own-session execution witness", () => {
	for (const change of ["clear", "replace"] as const) {
		test(`ordinary publication respects ${change} during held startup lookup`, async () => {
			const h = await makeHarness();
			const before = h.getSnapshot();
			const gate = h.holdLookup();
			const starting = h.start();
			await gate.entered;
			h.appendState({ backend: "work", ...(change === "replace" ? { executionWorkspace: { ...h.workspace, grantId: "replacement-grant", key: "OMP-2", branch: "execution/omp-2" } } : {}) });
			gate.release();
			await starting;
			await h.now();
			await h.input("Continue this session after the ownership change");
			expect(h.getAborts()).toBe(0);
			expect(h.stateCalls).toEqual([]);
			expect(h.getSnapshot()).toEqual(before);
			expect(h.lookups).toEqual(change === "replace" ? ["grant-1", "replacement-grant"] : ["grant-1"]);
		});
	}

	test("complete legacy binding remains haltable without auditor discovery", async () => {
		const h = await makeHarness("active", "legacy-repo");
		const discovery = vi.spyOn(taskModule, "discoverAgents").mockRejectedValue(new Error("Auditor unavailable"));
		await h.input("Pause my execution");
		expect(h.lookups).toEqual(["grant-1"]);
		expect(h.getAborts()).toBe(1);
		expect(h.getSnapshot().grant.state).toBe("paused");
		expect(h.stateCalls[0].judgeSha256).toBe("4".repeat(64));
		expect(discovery).not.toHaveBeenCalled();
	});

	for (const change of ["sibling-cwd", "host-session", "foreign-grant", "foreign-repository", "cleared-binding", "malformed-binding"] as const) {
		test(`${change} does not authorize automatic pause`, async () => {
			const h = await makeHarness();
			if (change === "sibling-cwd") h.setCwd(path.join(h.directory, "sibling"));
			if (change === "host-session") h.setHostSession("another-session");
			if (change === "foreign-grant") h.setGrant({ grant_id: "another-grant" });
			if (change === "foreign-repository") h.setGrant({ repository: path.join(h.directory, "another-primary") });
			if (change === "cleared-binding") h.appendState({ backend: "work" });
			if (change === "malformed-binding") h.appendState({ backend: "work", executionWorkspace: { ...h.workspace, path: "relative-workspace" } });
			const before = h.getSnapshot();
			await h.input("Work on this session");
			expect(h.getAborts()).toBe(0);
			expect(h.stateCalls).toEqual([]);
			expect(h.getSnapshot()).toEqual(before);
		});
	}

	test("ignored foreign-backend entry cannot supersede own binding", async () => {
		const h = await makeHarness();
		h.appendState({ backend: "other" });
		await h.input("Pause own work");
		expect(h.getSnapshot().grant.state).toBe("paused");
	});

	for (const change of ["session", "manager", "cwd", "witness", "terminal"] as const) {
		test(`held grant lookup refuses ${change} drift before abort or pause`, async () => {
			const h = await makeHarness();
			const gate = h.holdLookup();
			const input = h.input("Pause own work");
			await gate.entered;
			if (change === "session") h.setSession("replacement");
			if (change === "manager") h.replaceManager();
			if (change === "cwd") h.setCwd(path.join(h.directory, "sibling"));
			if (change === "witness") h.appendState({ backend: "work", executionWorkspace: { ...h.workspace } });
			if (change === "terminal") h.setGrant({ state: "canceled", grant_version: 4 });
			gate.release();
			await input;
			expect(h.getAborts()).toBe(0);
			expect(h.stateCalls).toEqual([]);
		});
	}

	test("ordinary conversation advancement does not revoke unchanged ownership", async () => {
		const h = await makeHarness();
		const gate = h.holdLookup();
		const input = h.input("Pause own work");
		await gate.entered;
		h.branch.push({ type: "message", id: "new-conversation-entry", parentId: h.branch.at(-1)!.id, timestamp: new Date().toISOString(), message: { role: "user", content: "ordinary message", timestamp: Date.now() } });
		gate.release();
		await input;
		expect(h.getSnapshot().grant.state).toBe("paused");
	});

	test("pause CAS cannot undo a racing terminal transition", async () => {
		const h = await makeHarness();
		h.onAbort(() => h.setGrant({ state: "canceled", grant_version: 4 }));
		await expect(h.input("Pause own work")).rejects.toThrow("CAS refused");
		expect(h.getSnapshot().grant.state).toBe("canceled");
		expect(h.getSnapshot().grant.grant_version).toBe(4);
		expect(h.stateChanges).toEqual([]);
	});

	test("pause response cannot publish notice into replacement session", async () => {
		const h = await makeHarness();
		h.onStateResponse(() => h.setSession("replacement"));
		await h.input("Pause own work");
		expect(h.getSnapshot().grant.state).toBe("paused");
		expect((await h.preparePrompt()).filter(result => result !== undefined)).toEqual([]);
	});

	test("shared cache cannot become ownership through startup and ordinary persistence", async () => {
		const h = await makeHarness("active", undefined, false);
		await h.poisonCache();
		await h.start();
		await h.now();
		await h.input("Unrelated follow-up");
		expect(h.getAborts()).toBe(0);
		expect(h.stateCalls).toEqual([]);
		expect(h.branch.filter(entry => entry.type === "custom" && entry.customType === "work-now" && (entry.data as { executionWorkspace?: unknown }).executionWorkspace)).toHaveLength(0);
	});

	test("new session cannot republish previous transcript binding through ordinary persistence", async () => {
		const h = await makeHarness();
		await h.start();
		h.branch.splice(0);
		h.setSession("new-owner-session");
		await h.switchSession();
		await h.now();
		await h.input("New session work");
		expect(h.getAborts()).toBe(0);
		expect(h.stateCalls).toEqual([]);
		expect(h.branch.filter(entry => entry.type === "custom" && entry.customType === "work-now" && (entry.data as { executionWorkspace?: unknown }).executionWorkspace)).toHaveLength(0);
	});

	test("startup retains recorded anchor but uses current recovery baseline", async () => {
		const h = await makeHarness();
		h.setItem({ current_git_baseline: "a".repeat(40), work_id: "next-queue-item" });
		h.setCwd(path.join(h.directory, "launch-location"));
		await h.start();
		expect(h.ensures).toEqual([{ cwd: h.directory, key: "OMP-1", baseline: "a".repeat(40) }]);
	});

	test("startup refuses changed context during ensure before relocation or new binding", async () => {
		const h = await makeHarness();
		const gate = h.holdEnsure();
		const starting = h.start();
		await gate.entered;
		const noticesBeforeDrift = h.notifications.length;
		h.setCwd(path.join(h.directory, "replacement"));
		gate.release();
		await starting;
		expect(h.workspaceEffects).toEqual(["ensure"]);
		expect(h.notifications.slice(noticesBeforeDrift)).toEqual([]);
		expect(h.branch.filter(entry => entry.type === "custom" && entry.customType === "work-now")).toHaveLength(1);
		expect(h.stateCalls).toEqual([]);
	});
});

describe("legacy relative execution repository refusal", () => {
	for (const phase of ["executing", "reviewing"] as const) {
		test(`${phase} grant refuses review before audit, freeze, push or new authority`, async () => {
			const harness = await makeHarness("active", "legacy-repo");
			harness.setPhase(phase);
			const original = harness.getSnapshot();
			const discover = vi.spyOn(taskModule, "discoverAgents");
			const freeze = vi.spyOn(gitModule, "freezeCandidateCommit");
			const push = vi.spyOn(gitModule, "pushCandidate");
			const result = await harness.review();
			expect(result.details.success).toBe(false);
			expect(result.content[0].text).toContain("repository must be an absolute path");
			expect(discover).not.toHaveBeenCalled();
			expect(freeze).not.toHaveBeenCalled();
			expect(push).not.toHaveBeenCalled();
			expect(harness.getSnapshot()).toEqual(original);
			expect(harness.stateChanges).toEqual([]);
		});
	}

	test("paused legacy grant refuses explicit resume before workspace or SDK relocation", async () => {
		const harness = await makeHarness("paused", "legacy-repo");
		const original = harness.getSnapshot();
		await harness.command("resume OMP-1");
		expect(harness.notifications.some(message => message.includes("repository must be an absolute path"))).toBe(true);
		expect(harness.workspaceEffects).toEqual([]);
		expect(harness.stateChanges).toEqual([]);
		expect(harness.getSnapshot()).toEqual(original);
	});

	test("active legacy startup refuses before managed workspace recovery", async () => {
		const harness = await makeHarness("active", "legacy-repo");
		const original = harness.getSnapshot();
		await harness.start();
		expect(harness.notifications.some(message => message.includes("Execution recovery skipped") && message.includes("repository must be an absolute path"))).toBe(true);
		expect(harness.workspaceEffects).toEqual([]);
		expect(harness.stateChanges).toEqual([]);
		expect(harness.getSnapshot()).toEqual(original);
	});

	for (const state of ["stopped", "canceled", "completed"] as const) {
		test(`${state} legacy grant keeps terminal refusal ahead of repository validation`, async () => {
			const harness = await makeHarness(state, "legacy-repo");
			const original = harness.getSnapshot();
			await harness.command("resume OMP-1");
			expect(harness.notifications.some(message => message.includes(`grant state is ${state}`))).toBe(true);
			expect(harness.notifications.some(message => message.includes("repository must"))).toBe(false);
			const review = await harness.review();
			expect(review.content[0].text).toContain("no active execution grant");
			expect(harness.workspaceEffects).toEqual([]);
			expect(harness.getSnapshot()).toEqual(original);
		});
	}
});
