import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, expect, test, vi } from "bun:test";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";
import { executionRemoteRef } from "../extensions/workflow/git";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

const temps: string[] = [];
afterEach(() => {
	vi.restoreAllMocks();
	for (const dir of temps.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

test("queued sendMessage ack with no persisted custom_message is replayed once and reserves nothing", async () => {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omp-246-ack-"));
	const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-246-ack-cache-"));
	temps.push(directory, cacheDir);
	const sessionId = "dispatch-session";
	const grantId = "11111111-2222-4333-8444-555555555555";
	const workId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
	const revisionId = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
	const key = "OMP-246";
	const baseline = "a".repeat(40);
	const branch = executionRemoteRef(key, grantId).slice("refs/heads/".length);
	const calls: string[] = [];
	const writes: string[] = [];
	const appended: Array<{ customType: string; status?: string }> = [];
	const notices: Array<{ level?: string; text: string }> = [];
	const exec = {
		grant: {
			grant_id: grantId,
			workspace_id: "ws-1",
			owner_id: "owner-1",
			repository: directory,
			remote_ref: executionRemoteRef(key, grantId),
			state: "active",
			mode: "single",
			grant_version: 2,
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
			grant_id: grantId,
			work_id: workId,
			position: 0,
			phase: "executing",
			claimed_revision_id: revisionId,
			criteria_revision_id: revisionId,
			project_id: null,
			active_blocker_ids: [],
			original_request: "Continue the active grant",
			original_request_sha256: "0".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
			initial_git_baseline: baseline,
			current_git_baseline: baseline,
		},
	} as unknown as ExecutionSnapshot;
	exec.items = [exec.activeItem!];
	const issue = { id: workId, key, title: "Dispatch authority", project: "Bookends" };
	const workItem = { work_id: workId, state: "IN_PROGRESS", project_id: null, revision: { revision_id: revisionId } };
	const messageId = "queued-ack";
	// Ack written after pi.sendMessage. The custom_message was never persisted.
	const entries: unknown[] = [
		{
			type: "custom",
			customType: "work-now",
			id: "ownership-entry",
			parentId: null,
			timestamp: new Date().toISOString(),
			data: {
				backend: "work",
				executionWorkspace: { grantId, key, primaryRoot: directory, path: directory, branch, baseline, reused: false },
			},
		},
		{
			type: "custom",
			customType: "work-now-execute-outbox",
			id: "outbox-entry",
			parentId: null,
			timestamp: new Date().toISOString(),
			data: {
				grantId,
				sessionId,
				workId,
				revisionId,
				preReservationVersion: 1,
				postVersion: exec.grant.grant_version,
				messageId,
				status: "queued",
				at: new Date().toISOString(),
			},
		},
	];
	expect(entries.some(entry => (entry as { type?: string }).type === "custom_message")).toBe(false);
	const sent: Array<{ message: { customType?: string; details?: { executionContinuation?: { messageId?: string } } } }> = [];
	const handlers: Array<(event: unknown, ctx: ExtensionContext) => Promise<void>> = [];
	const fakePi = {
		logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
		zod: z,
		getSessionId: () => sessionId,
		registerTool: () => {},
		registerMessageRenderer: () => {},
		registerCommand: () => {},
		registerFlag: () => {},
		on: (event: string, handler: (event: unknown, ctx: ExtensionContext) => Promise<void>) => {
			if (event === "session_start") handlers.push(handler);
		},
		sendMessage: (message: (typeof sent)[number]["message"]) => {
			sent.push({ message });
		},
		appendEntry: (customType: string, data?: unknown) => {
			const status = data && typeof data === "object" && "status" in data && typeof (data as { status?: unknown }).status === "string"
				? (data as { status: string }).status
				: undefined;
			appended.push({ customType, ...(status ? { status } : {}) });
		},
	} as unknown as ExtensionAPI;
	const markCall = (name: string, write = false) => {
		calls.push(name);
		if (write) writes.push(name);
	};
	const backend = {
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		serviceLabel: "ledger",
		projectScopeExists: async () => true,
		currentNow: async () => { markCall("currentNow"); return issue; },
		pendingDeliveries: async () => { markCall("pendingDeliveries"); return []; },
		executionChildren: async () => ({ umbrella: false, children: [] }),
		getCommittedSkipClaims: async () => { markCall("getCommittedSkipClaims"); return []; },
		getPendingExecutionClaims: async () => { markCall("getPendingExecutionClaims"); return []; },
		getExecution: async () => { markCall("getExecution"); return exec; },
		findIssue: async () => { markCall("findIssue"); return issue; },
		setExecutionState: async (input: { targetState: ExecutionSnapshot["grant"]["state"] }) => {
			markCall("setExecutionState", true);
			exec.grant.state = input.targetState;
			exec.grant.grant_version += 1;
			exec.grant.continuations_scheduled += 1;
			return exec;
		},
		workClient: {
			healthReady: async () => ({ service_fingerprint: "dispatch-fixture-service" }),
			workItem: async () => { markCall("workItem"); return workItem; },
			workflow: async () => { markCall("workflow"); return { item: workItem, relations: [] }; },
		},
	} as unknown as WorkflowBackend;
	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: () => true,
		executionWorkspaceManager: {
			primaryRoot: async (dir: string) => dir,
			ensure: async (dir: string, itemKey: string, ownedGrantId: string, ownedBaseline: string) => ({
				primaryRoot: dir,
				path: dir,
				branch: executionRemoteRef(itemKey, ownedGrantId).slice("refs/heads/".length),
				grantId: ownedGrantId,
				baseline: ownedBaseline,
				reused: false,
			}),
			cleanup: async () => ({ cleaned: true, detail: "identity cleanup" }),
		},
	})(fakePi);
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
	const ctx = {
		cwd: directory,
		taskDepth: 0,
		sessionManager: {
			getBranch: () => entries,
			getSessionId: () => sessionId,
			getCwd: () => directory,
			getLeafId: () => "leaf",
		},
		ui: {
			notify: (text: string, level?: string) => { notices.push({ text, ...(level ? { level } : {}) }); },
			theme: { fg: (_color: string, text: string) => text },
			setStatus: () => {},
		},
	} as unknown as ExtensionContext;
	exec.grant.judge_sha256 = (await computeAuditTcb(ctx, backend.workClient!)).judgeSha256;
	vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
	vi.spyOn(gitModule, "headCommit").mockReturnValue(baseline);
	if (!handlers.length) throw new Error("workflow host did not register startup recovery");
	for (const handler of handlers) await handler({}, ctx);
	const delivery = sent.filter(entry => entry.message.customType === "work-execute");
	expect(delivery, JSON.stringify(notices)).toHaveLength(1);
	expect(delivery[0]?.message.details?.executionContinuation?.messageId).toBe(messageId);
	expect(writes).not.toContain("setExecutionState");
	expect(exec.grant.continuations_scheduled).toBe(0);
	expect(exec.grant.grant_version).toBe(2);
	expect(appended.filter(entry => entry.customType === "work-now-execute-outbox").map(entry => entry.status)).toEqual(["queued"]);
	expect(calls).toContain("getExecution");
});
