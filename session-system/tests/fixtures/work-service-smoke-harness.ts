// HOME-147 candidate smoke harness: drives the REAL work-now backend against
// the live loopback WorkService the parent test orchestrates. Runs as a child
// process with isolated HOME/XDG env (repo full-suite-safety rule).
//
// Sequence: intake create → plan stamp → dirty file → /summary (freeze +
// finalize) → bookends auditor → verification → audit → closeout receipts →
// request_closeout → /done (push to the bare remote + complete_work).
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
if (!probe) throw new Error("usage: harness <probe-repo>");
const repoRoot = path.resolve(import.meta.dir, "../../..");

// Network guard: record every URL the backend touches; the parent asserts all
// are loopback (the WorkService backend must never cross the network boundary).
const fetchUrls: string[] = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
	fetchUrls.push(String(input instanceof Request ? input.url : input));
	return realFetch(input, init);
}) as typeof fetch;

const loaded = await loadExtensions(
	["session-system/extensions/work-now.ts", "session-system/extensions/model-bookends.ts"].map(file => path.join(repoRoot, file)),
	probe,
);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

const uiCalls: string[] = [];
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => [] } as never,
	{ getAvailable: () => [fableModel, gptModel], hasProvider: () => true } as never,
	undefined,
	{ getModelRole: (role: string) => (role === "audit" ? "openai/gpt-5.2" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	undefined,
	undefined,
	0,
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
		theme: { fg: (_c: string, text: string) => text },
		setStatus: () => {},
		notify: (text: string) => uiCalls.push(`notify:${text}`),
		select: async () => undefined,
		confirm: async (title: string) => {
			uiCalls.push(`confirm:${title}`);
			return true; // smoke confirms every gate: freeze, verdict, capture
		},
	} as never,
);
await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}

const out: Record<string, unknown> = {};
const summaryMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "summary", path: "/x/SKILL.md" }, content: "summary", timestamp: Date.now() },
};

// 0. First-screen sanity: what the operator sees on first open (HOME-148).
out.firstScreen = await execute({ action: "status" });

// 1. /capture files the item (owner-confirmed), then /now selects it.
const cmdCtx = runner.createCommandContext();
const capture = extension.commands.get("capture");
if (!capture) throw new Error("capture command missing");
await capture.handler("Smoke item — candidate smoke", cmdCtx);
const key = /Captured → (\S+)/.exec(uiCalls.join("\n"))?.[1];
if (!key) throw new Error(`no key in: ${uiCalls.join("\n")}`);
out.key = key;
out.captured = uiCalls.find(call => call.includes("Captured →"));
const nowCommand = extension.commands.get("now");
if (!nowCommand) throw new Error("now command missing");
await nowCommand.handler(key, cmdCtx);
out.nowAfterSelect = await execute({ action: "my_now" });

// 2. plan stamp (approved plan event).
const plan = "# Smoke\n\n## Approach\n1. Freeze and push the candidate\n\n## Verification\n1. The smoke asserts the closed state\n";
const planResult = await runner.emit({ type: "plan_approved", planFilePath: "local://work-plan.md", planContent: plan, title: "Smoke" } as never);
// host returns undefined on success, { cancel, reason } on refusal
out.plan = planResult === undefined ? "stamped" : planResult;

// 3. work happens: a dirty file the freeze must pick up.
fs.writeFileSync(path.join(probe, "smoke.txt"), "candidate payload\n");

// 4. owner-entered /summary: gate → freeze → finalize_candidate.
await runner.emitInput("/summary", undefined, "interactive");
await runner.emit(summaryMessage as never);

// 5. fresh auditor through model-bookends → bridge receipt.
const AUDIT_TASK = [
	"Approved plan: freeze and push the candidate.",
	"Acceptance criteria: AC-1 the item closes done with a pushed candidate.",
	"Starting state: clean probe repo; dirty file smoke.txt.",
	"Final diff:",
	"```diff",
	"--- a/smoke.txt",
	"+++ b/smoke.txt",
	"```",
	"Verification: this smoke's assertions.",
].join("\n");
const REPORT = [
	"VERDICT: PASS",
	"",
	"FINDINGS",
	"(none)",
	"",
	"ACCEPTANCE COVERAGE",
	"| AC-1 | met | smoke |",
	"",
	"OUT OF SCOPE",
	"none",
	"",
	"CHECKS RUN",
	"candidate smoke",
	"",
	"REMAINING QUESTIONS",
	"none",
].join("\n");
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

// 6. typed receipts in close-ritual order.
out.verification = await execute({ action: "append_evidence", work: key, kind: "verification", body: "the smoke asserts the end state" });
out.audit = await execute({ action: "append_evidence", work: key, kind: "audit", body: REPORT });
out.closeout = await execute({ action: "append_evidence", work: key, kind: "closeout", body: "session review: candidate smoke completed" });

// 7. owner close request, then /done (preflight → verdict → push → complete).
const close = await confirmRoundTrip(execute, { action: "request_closeout", work: key, body: "smoke complete" });
out.requestCloseout = close.confirmed;
const done = extension.commands.get("done");
if (!done) throw new Error("done command missing");
await done.handler("", runner.createCommandContext());
out.doneUi = uiCalls;
out.now = await execute({ action: "my_now" });
out.fetchUrls = fetchUrls;

process.stdout.write(JSON.stringify(out));
