import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

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

afterEach(async () => {
	vi.restoreAllMocks();
	await Promise.all(directories.splice(0).map(directory => fs.rm(directory, { recursive: true, force: true })));
});

async function makeHarness(state: "active" | "paused" = "active") {
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
			repository: directory,
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
	const backend = {
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(directory, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		getExecution: async () => execution,
		findIssue: async () => ({ id: item.work_id, key: "OMP-1", title: "Development change" }),
		setExecutionState: async (input: StateChange) => {
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
			return execution;
		},
		workClient: {
			healthReady: async () => ({ ready: true, service_fingerprint: "5".repeat(64) }),
		},
	} as unknown as WorkflowBackend;
	const inputHandlers: InputHandler[] = [];
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
		},
		appendEntry: () => {},
		sendMessage: () => {},
		getSessionId: () => "session-1",
		logger: { warn: () => {} },
	} as unknown as ExtensionAPI;
	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: () => true,
		executionWorkspaceManager: {
			primaryRoot: async cwd => cwd,
			ensure: async (cwd, _key, grantId, baseline) => ({
				primaryRoot: cwd,
				path: cwd,
				branch: "execution/omp-1",
				grantId,
				baseline,
				reused: true,
			}),
			cleanup: async () => ({ cleaned: true, detail: "fixture workspace" }),
		},
	})(pi);
	const notifications: string[] = [];
	let aborts = 0;
	const context = {
		cwd: directory,
		taskDepth: 0,
		abort: () => {
			aborts += 1;
		},
		sessionManager: { getCwd: () => directory },
		ui: {
			notify: (message: string) => {
				notifications.push(message);
			},
			setStatus: () => {},
			theme: { fg: (_color: string, text: string) => text },
		},
	} as unknown as ExtensionCommandContext;
	return {
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
	};
}

describe("execution halt remains available when development breaks the auditor", () => {
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
		const harness = await makeHarness();
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
	});

	test("stop_execution records its terminal reason despite unavailable auditor source", async () => {
		const harness = await makeHarness();
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
