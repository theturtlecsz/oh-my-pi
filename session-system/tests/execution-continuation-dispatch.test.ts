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

type Notice = { level?: string; text: string };
type DispatchResult = { ok: true } | { ok: false; reason: string };

/** In-memory grant plus a fake session, same shape as the guarded continuation harness. */
async function deliverContinuation(mode: "fresh" | "replay") {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omp-246-dispatch-"));
	const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-246-dispatch-cache-"));
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
	const notices: Notice[] = [];
	let cwd = directory;
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
	let readGrant: () => Promise<ExecutionSnapshot | null> = async () => exec;
	const issue = { id: workId, key, title: "Dispatch authority", project: "Bookends" };
	const workItem = { work_id: workId, state: "IN_PROGRESS", project_id: null, revision: { revision_id: revisionId } };
	const ownership = {
		type: "custom" as const,
		customType: "work-now",
		id: "ownership-entry",
		parentId: null,
		timestamp: new Date().toISOString(),
		data: {
			backend: "work",
			executionWorkspace: { grantId, key, primaryRoot: directory, path: directory, branch, baseline, reused: false },
		},
	};
	const entries: unknown[] = [ownership];
	if (mode === "replay") {
		entries.push({
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
				messageId: "replay-message",
				status: "pending",
				at: new Date().toISOString(),
			},
		});
	}
	const sent: Array<{ message: { customType?: string; details?: { executionContinuation?: { messageId?: string } } }; options?: { deliverAs?: string; triggerTurn?: boolean; validateDispatch?: () => Promise<DispatchResult> } }> = [];
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
		sendMessage: (message: (typeof sent)[number]["message"], options?: (typeof sent)[number]["options"]) => {
			sent.push({ message, options });
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
		getExecution: async () => { markCall("getExecution"); return readGrant(); },
		findIssue: async () => { markCall("findIssue"); return issue; },
		setExecutionState: async (input: { targetState: ExecutionSnapshot["grant"]["state"] }) => {
			markCall("setExecutionState", true);
			exec.grant.state = input.targetState;
			exec.grant.grant_version += 1;
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
			getCwd: () => cwd,
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
	if (delivery.length !== 1) throw new Error(`expected one execution delivery for ${mode}, got ${delivery.length}; notices=${JSON.stringify(notices)}`);
	const validateDispatch = delivery[0]?.options?.validateDispatch;
	if (!validateDispatch) throw new Error(`${mode} delivery did not capture validateDispatch`);
	const outbox = appended.filter(entry => entry.customType === "work-now-execute-outbox").map(entry => entry.status);
	return {
		exec,
		calls,
		writes,
		notices,
		outbox,
		appended,
		messageId: delivery[0]?.message.details?.executionContinuation?.messageId,
		options: delivery[0]?.options,
		validateDispatch,
		owned: () => { cwd = directory; },
		detachWorkspace: () => { cwd = path.join(directory, "detached"); },
		failRead: () => { readGrant = async () => { throw new Error("getExecution down"); }; },
		restoreRead: () => { readGrant = async () => exec; },
	};
}

test.each(["fresh", "replay"] as const)("%s execution delivery checks dispatch authority without a backend write", async (mode) => {
	const fixture = await deliverContinuation(mode);
	expect(fixture.options).toMatchObject({ deliverAs: "nextTurn", triggerTurn: true });
	expect(fixture.outbox).toEqual(mode === "fresh" ? ["pending", "queued"] : ["queued"]);
	if (mode === "replay") expect(fixture.messageId).toBe("replay-message");

	const acceptedAt = fixture.calls.length;
	const writesAt = fixture.writes.length;
	const noticesAt = fixture.notices.length;
	const appendedAt = fixture.appended.length;
	expect(await fixture.validateDispatch()).toEqual({ ok: true });
	expect(fixture.calls.slice(acceptedAt)).toEqual(["getExecution"]);
	expect(fixture.writes.slice(writesAt)).toEqual([]);
	expect(fixture.appended.slice(appendedAt)).toEqual([]);
	expect(fixture.notices.slice(noticesAt)).toEqual([]);

	const saved = {
		state: fixture.exec.grant.state,
		expiresAt: fixture.exec.grant.expires_at,
		version: fixture.exec.grant.grant_version,
		activeItem: fixture.exec.activeItem,
	};
	const refusals: Array<{ name: string; apply: () => void; calls: string[]; reason: string; notify: boolean }> = [
		{ name: "canceled", apply: () => { fixture.exec.grant.state = "canceled"; }, calls: ["getExecution"], reason: "Execution authority is no longer active", notify: true },
		{ name: "stopped", apply: () => { fixture.exec.grant.state = "stopped"; }, calls: ["getExecution"], reason: "Execution authority is no longer active", notify: true },
		{ name: "expired", apply: () => { fixture.exec.grant.expires_at = new Date(Date.now() - 60_000).toISOString(); }, calls: ["getExecution"], reason: "Execution authority is no longer active", notify: true },
		{ name: "superseded", apply: () => { fixture.exec.grant.grant_version += 1; }, calls: ["getExecution"], reason: "continuation version differs from the authoritative grant", notify: true },
		{ name: "no active item", apply: () => { fixture.exec.activeItem = null; }, calls: ["getExecution"], reason: "Execution authority is no longer active", notify: true },
		{ name: "unreadable", apply: () => fixture.failRead(), calls: ["getExecution"], reason: "Execution authority is unreadable: Error: getExecution down", notify: true },
		{ name: "unowned workspace", apply: () => fixture.detachWorkspace(), calls: [], reason: "Execution session ownership is unavailable", notify: false },
	];
	try {
		for (const refusal of refusals) {
			refusal.apply();
			const callAt = fixture.calls.length;
			const writeAt = fixture.writes.length;
			const noticeAt = fixture.notices.length;
			const appendAt = fixture.appended.length;
			const decision = await fixture.validateDispatch();
			expect({ name: refusal.name, decision, calls: fixture.calls.slice(callAt), writes: fixture.writes.slice(writeAt), appends: fixture.appended.slice(appendAt) }).toEqual({
				name: refusal.name,
				decision: { ok: false, reason: refusal.reason },
				calls: refusal.calls,
				writes: [],
				appends: [],
			});
			expect(fixture.notices.slice(noticeAt)).toEqual(refusal.notify ? [{ level: "warning", text: refusal.reason }] : []);
			fixture.exec.grant.state = saved.state;
			fixture.exec.grant.expires_at = saved.expiresAt;
			fixture.exec.grant.grant_version = saved.version;
			fixture.exec.activeItem = saved.activeItem;
			fixture.restoreRead();
			fixture.owned();
		}
	} finally {
		fixture.restoreRead();
		fixture.owned();
	}
});
