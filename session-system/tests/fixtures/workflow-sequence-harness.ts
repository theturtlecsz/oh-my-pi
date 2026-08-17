// HOME-122 harness: drive the real linear-now extension through its public
// events, command, and tool with a deterministic in-memory Linear API.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
const mode = process.argv[3];
const MODES = ["intake", "plan", "summary", "summary-subagent", "done", "footer", "audit"];
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
// audit mode loads model-bookends too: the receipt must cross from one
// top-level extension's module graph into the other's (HOME-147 bridge).
const EXTENSION_FILES =
	mode === "audit"
		? ["session-system/extensions/linear-now.ts", "session-system/extensions/model-bookends.ts"]
		: ["session-system/extensions/linear-now.ts"];
const loaded = await loadExtensions(EXTENSION_FILES.map(file => path.join(repoRoot, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("linear-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

const uiCalls: string[] = [];
const statuses: string[] = [];
const statusCalls: { key: string; text: string | null; placement: string }[] = [];
const depth = mode === "summary-subagent" ? 1 : 0;
// A subagent inherits NOW from the session branch; it never performs the write.
const inheritedNow =
	mode === "summary-subagent"
		? [{ type: "custom", customType: "linear-now", data: { team: "HOME", issueId: "id-1", identifier: "HOME-1", title: "First", setAt: Date.now() } }]
		: [];
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => inheritedNow } as never,
	{ getAvailable: () => [fableModel, gptModel], hasProvider: () => true } as never,
	undefined,
	{ getModelRole: (role: string) => (role === "audit" ? "openai/gpt-5.2" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	undefined,
	undefined,
	depth,
);
runner.initialize(
	{
		appendEntry: () => {},
		setModel: async () => true,
		getThinkingLevel: () => "high",
		setThinkingLevel: () => {},
	} as never,
	{
		getModel: () => fableModel,
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
	const { confirmed } = await confirmRoundTrip(execute, { action: "set_now", work: "HOME-1" });
	if (!confirmed.includes("NOW → HOME-1")) throw new Error(`set_now failed: ${confirmed}`);
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
	const first = await confirmRoundTrip(execute, { action: "create_work", title: "First", description: "one", project: "The Bookends" });
	out.preview = first.preview;
	out.confirmed = first.confirmed;
	const second = await confirmRoundTrip(execute, { action: "create_work", title: "Second", description: "two", project: "The Bookends" });
	out.second = second.confirmed;
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
	// HOME-147: ambient untyped notes no longer exist — a kindless call is refused with the kind menu.
	out.evidence = await execute({ action: "append_evidence", work: "HOME-1", body: "tests pass" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterEvidence = statuses.at(-1);
	out.handoff = await execute({ action: "append_evidence", work: "HOME-1", kind: "handoff", body: "done / none / resume" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterHandoff = statuses.at(-1);
	out.stopAfterHandoff = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
} else if (mode === "summary" || mode === "summary-subagent") {
	if (mode === "summary") await setNow(); // subagent mode inherits NOW from the branch
	if (mode === "summary") {
		await runner.emit(summaryMessage as never);
		out.noPlanNotice = uiCalls.at(-1);
		out.noPlanReview = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "premature" });
	}
	await approve(planA);
	out.beforeInvocation = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "before" });
	await runner.emit(pastedSummary as never);
	out.afterPaste = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "pasted" });
	await runner.emit(summaryMessage as never);
	out.afterStructured = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "review body" });
	out.reviewBodies = comments.filter(comment => comment.body.startsWith("**Session review**")).map(comment => comment.body);
	out.uiCalls = uiCalls;
} else if (mode === "footer") {
	out.initialCalls = [...statusCalls];
	await setNow();
	out.callsAfterSetNow = [...statusCalls];
} else if (mode === "audit") {
	// HOME-147 bridge contract end-to-end: the auditor result arrives through
	// model-bookends (separate top-level extension, separate module graph), the
	// receipt crosses the process-global bridge, and the host's
	// work/append_evidence/audit consumes exactly one receipt bound to the
	// verbatim bytes.
	await setNow();
	await approve(planA);
	const AUDIT_TASK = [
		"Approved plan: change the shared path.",
		"Acceptance criteria: AC-1 the focused check passes.",
		"Starting state: commit abc123; pre-existing dirty files: none.",
		"Final diff:",
		"```diff",
		"--- a/x",
		"+++ b/x",
		"```",
		"Verification: bun test → pass.",
	].join("\n");
	const REPORT = [
		"VERDICT: PASS",
		"",
		"FINDINGS",
		"(none)",
		"",
		"ACCEPTANCE COVERAGE",
		"| AC-1 | met | tests |",
		"",
		"OUT OF SCOPE",
		"none",
		"",
		"CHECKS RUN",
		"bun test → pass",
		"",
		"REMAINING QUESTIONS",
		"none",
	].join("\n");
	out.unauthorized = await execute({ action: "append_evidence", work: "HOME-1", kind: "audit", body: REPORT });
	await runner.emitInput("/summary", undefined, "interactive");
	await runner.emit(summaryMessage as never);
	const spawn = await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-1",
		input: { context: "audit the completed work", tasks: [{ agent: "auditor", task: AUDIT_TASK }] },
	} as never);
	out.spawnBlocked = spawn && typeof spawn === "object" && "block" in spawn ? Boolean(spawn.block) : false;
	await runner.emitToolResult({
		type: "tool_result",
		toolName: "task",
		toolCallId: "aud-1",
		input: {},
		content: [{ type: "text", text: `<task-result id="Aud" agent="auditor" status="completed">\n<output>\n${REPORT}\n</output>\n</task-result>` }],
		details: { results: [{ output: REPORT }] },
		isError: false,
	} as never);
	out.edited = await execute({ action: "append_evidence", work: "HOME-1", kind: "audit", body: `${REPORT} ` });
	out.exact = await execute({ action: "append_evidence", work: "HOME-1", kind: "audit", body: REPORT });
	out.replay = await execute({ action: "append_evidence", work: "HOME-1", kind: "audit", body: REPORT });
	out.auditBodies = comments.filter(comment => comment.body.includes("VERDICT: PASS")).length;
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
	out.review = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "complete" });
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
