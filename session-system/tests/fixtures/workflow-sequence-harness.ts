// HOME-122 harness: drive the real linear-now extension through its public
// events, command, and tool with a deterministic in-memory Linear API.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const probe = process.argv[2];
const mode = process.argv[3];
const MODES = ["intake", "plan", "summary", "summary-subagent", "done", "footer"];
if (!probe || !mode || !MODES.includes(mode)) throw new Error(`usage: harness <probe-repo> ${MODES.join("|")}`);

interface Comment {
	body: string;
	createdAt: string;
}

const issue = { id: "id-1", identifier: "HOME-1", title: "First", project: undefined as { name: string } | undefined };
const comments: Comment[] = [];
const writes = { created: 0, addNow: 0, removeNow: 0, closed: 0 };
let nowSelected = false;
let clock = 0;

function response(data: unknown): Response {
	return new Response(JSON.stringify({ data }), { status: 200 });
}

globalThis.fetch = (async (_url: unknown, init: { body?: string }) => {
	const parsed = JSON.parse(init.body ?? "{}") as { query?: string; variables?: Record<string, unknown> };
	const query = parsed.query ?? "";
	const variables = parsed.variables ?? {};
	if (query.includes("issueLabelCreate")) return response({ issueLabelCreate: { issueLabel: { id: "label-now" } } });
	if (query.includes("issueCreate")) {
		writes.created++;
		const input = variables.input as { title?: string } | undefined;
		const created = {
			id: `id-${writes.created}`,
			identifier: `HOME-${writes.created}`,
			title: input?.title ?? "created",
		};
		if (writes.created === 1) Object.assign(issue, created);
		return response({ issueCreate: { success: true, issue: created } });
	}
	if (query.includes("commentCreate")) {
		const input = variables.input as { body?: string } | undefined;
		clock++;
		comments.push({ body: input?.body ?? "", createdAt: new Date(Date.UTC(2026, 7, 14, 0, 0, clock)).toISOString() });
		return response({ commentCreate: { success: true } });
	}
	if (query.includes("issueUpdate")) {
		writes.closed++;
		return response({ issueUpdate: { success: true } });
	}
	if (query.includes("issueAddLabel")) {
		writes.addNow++;
		nowSelected = true;
		return response({ issueAddLabel: { success: true } });
	}
	if (query.includes("issueRemoveLabel")) {
		writes.removeNow++;
		nowSelected = false;
		return response({ issueRemoveLabel: { success: true } });
	}
	if (query.includes("states(first:20)")) {
		return response({ teams: { nodes: [{ states: { nodes: [{ id: "done", name: "Done", type: "completed" }, { id: "canceled", name: "Canceled", type: "canceled" }] } }] } });
	}
	if (query.includes("teams(filter:")) return response({ teams: { nodes: [{ id: "team-1", key: "HOME" }] } });
	if (query.includes("issueLabels(")) {
		const name = variables.name;
		return response({ issueLabels: { nodes: name === "now" ? [{ id: "label-now", name: "now" }] : [] } });
	}
	if (query.includes("issues(first:2") && query.includes("labels:")) {
		return response({ issues: { nodes: nowSelected ? [{ ...issue, project: issue.project }] : [] } });
	}
	if (query.includes("projects(filter:")) return response({ projects: { nodes: [{ id: "project-1" }] } });
	if (query.includes("comments(last:50)")) return response({ issue: { ...issue, comments: { nodes: comments } } });
	if (query.includes("issue(id:")) return response({ issue });
	throw new Error(`unhandled GraphQL: ${query}`);
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const loaded = await loadExtensions([path.join(repoRoot, "session-system/extensions/linear-now.ts")], probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("linear-now extension did not load");
const tool = extension.tools.get("linear");
if (!tool) throw new Error("linear tool missing");

const uiCalls: string[] = [];
const statuses: string[] = [];
const statusCalls: { key: string; text: string | null; placement: string }[] = [];
const depth = mode === "summary-subagent" ? 1 : 0;
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => [] } as never,
	{} as never,
	undefined,
	undefined,
	undefined,
	undefined,
	depth,
);
runner.initialize(
	{ appendEntry: () => {} } as never,
	{
		getModel: () => undefined,
		isIdle: () => true,
		abort: () => {},
		hasPendingMessages: () => false,
		shutdown: () => {},
		getSystemPrompt: () => [],
	} as never,
	undefined,
	{
		theme: { fg: (_color: string, text: string) => text },
		setStatus: (key: string, text: string | undefined, options?: { placement?: string }) => {
			statuses.push(text ?? "");
			statusCalls.push({ key, text: text ?? null, placement: options?.placement ?? "footer" });
		},
		notify: (text: string) => uiCalls.push(`notify:${text}`),
		select: async (title: string) => {
			uiCalls.push(`select:${title}`);
			return undefined;
		},
		confirm: async (title: string) => {
			uiCalls.push(`confirm:${title}`);
			return title.startsWith("This is your verdict");
		},
	} as never,
);
await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}

async function setNow(): Promise<void> {
	const text = await execute({ action: "set_now", issue: "HOME-1", confirm: true });
	if (!text.includes("NOW → HOME-1")) throw new Error(`set_now failed: ${text}`);
}

const planA = "# Work\n\n## Approach\n1. Change the shared path\n\n## Verification\n1. Run the focused check\n";
const planB = `${planA}2. Run the smoke path\n`;
async function approve(content: string): Promise<{ cancel: boolean; reason?: string }> {
	const result = await runner.emit({
		type: "plan_approved",
		planFilePath: "local://work-plan.md",
		planContent: content,
		title: "Work",
	} as never);
	return (result ?? { cancel: false }) as { cancel: boolean; reason?: string };
}

const intakeMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "intake", path: "/x/SKILL.md" }, content: "intake", timestamp: Date.now() },
};
const summaryMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "summary", path: "/x/SKILL.md" }, content: "summary", timestamp: Date.now() },
};
const pastedSummary = {
	type: "message_start",
	message: { role: "user", content: "[IMPORTANT: User invoked the summary skill]", timestamp: Date.now() },
};

const out: Record<string, unknown> = {};
if (mode === "intake") {
	await runner.emit(intakeMessage as never);
	out.preview = await execute({ action: "create_issue", title: "First", description: "one", project: "The Bookends" });
	out.confirmed = await execute({ action: "create_issue", title: "First", description: "one", project: "The Bookends", confirm: true });
	out.second = await execute({ action: "create_issue", title: "Second", description: "two", project: "The Bookends", confirm: true });
	const stop = await extension.handlers.get("session_stop")?.[0]?.({ type: "session_stop", stop_hook_active: false }, ctx);
	out.stop = stop ?? null;
	out.writes = writes;
	out.nowSelected = nowSelected;
} else if (mode === "plan") {
	out.noNow = await runner.emitInput("/plan", undefined, "interactive");
	await setNow();
	out.first = await approve(planA);
	out.commentsAfterFirst = comments.length;
	out.invalid = await approve("# Missing required sections\n");
	out.commentsAfterInvalid = comments.length;
	out.firstBody = comments[0]?.body;
	out.same = await approve(planA);
	out.commentsAfterSame = comments.length;
	out.changed = await approve(planB);
	out.commentsAfterChanged = comments.length;
	out.hashA = Bun.SHA256.hash(planA, "hex");
	out.hashB = Bun.SHA256.hash(planB, "hex");
	const stopHandler = extension.handlers.get("session_stop")?.[0];
	out.stopFirst = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
	out.stopSecond = (await stopHandler?.({ type: "session_stop", stop_hook_active: true }, ctx)) ?? null;
	out.evidence = await execute({ action: "comment", issue: "HOME-1", kind: "evidence", body: "tests pass" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterEvidence = statuses.at(-1);
	out.handoff = await execute({ action: "comment", issue: "HOME-1", kind: "handoff", body: "done / none / resume" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterHandoff = statuses.at(-1);
	out.stopAfterHandoff = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
} else if (mode === "summary" || mode === "summary-subagent") {
	await setNow();
	if (mode === "summary") {
		await runner.emit(summaryMessage as never);
		out.noPlanNotice = uiCalls.at(-1);
		out.noPlanReview = await execute({ action: "comment", issue: "HOME-1", kind: "review", body: "premature" });
	}
	await approve(planA);
	out.beforeInvocation = await execute({ action: "comment", issue: "HOME-1", kind: "review", body: "before" });
	await runner.emit(pastedSummary as never);
	out.afterPaste = await execute({ action: "comment", issue: "HOME-1", kind: "review", body: "pasted" });
	await runner.emit(summaryMessage as never);
	out.afterStructured = await execute({ action: "comment", issue: "HOME-1", kind: "review", body: "review body" });
	out.reviewBodies = comments.filter(comment => comment.body.startsWith("**Session review**")).map(comment => comment.body);
	out.uiCalls = uiCalls;
} else if (mode === "footer") {
	out.initialCalls = [...statusCalls];
	await setNow();
	out.callsAfterSetNow = [...statusCalls];
} else {
	await setNow();
	fs.writeFileSync(path.join(probe, "dirty.txt"), "dirty\n");
	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	await done.handler("", runner.createCommandContext());
	out.beforePlanUi = [...uiCalls];
	out.beforePlanWrites = { ...writes, comments: comments.length };
	await approve(planA);
	uiCalls.length = 0;
	await done.handler("", runner.createCommandContext());
	out.beforeReviewUi = [...uiCalls];
	out.beforeReviewWrites = { ...writes, comments: comments.length };
	await runner.emit(summaryMessage as never);
	out.review = await execute({ action: "comment", issue: "HOME-1", kind: "review", body: "complete" });
	const commentsBeforeDone = comments.length;
	uiCalls.length = 0;
	await done.handler("", runner.createCommandContext());
	out.doneUi = [...uiCalls];
	out.doneWrites = { ...writes, verdictComments: comments.length - commentsBeforeDone };
	out.now = await execute({ action: "my_now" });
	await done.handler("", runner.createCommandContext());
	out.afterSecondDone = { ...writes };
}

process.stdout.write(JSON.stringify(out));
