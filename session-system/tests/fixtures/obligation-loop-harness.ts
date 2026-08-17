// HOME-122 obligation harness: plan files are inert; only an approved-plan
// event arms execution, and only a typed handoff settles it.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
if (!probe) throw new Error("usage: harness <probe-repo>");

const comments: Array<{ body: string; createdAt: string }> = [];
let nowSelected = false;
let clock = 0;
globalThis.fetch = (async (_url: unknown, init: { body?: string }) => {
	const parsed = JSON.parse(init.body ?? "{}") as { query?: string; variables?: Record<string, unknown> };
	const query = parsed.query ?? "";
	const variables = parsed.variables ?? {};
	let data: unknown;
	if (query.includes("commentCreate")) {
		const input = variables.input as { body?: string } | undefined;
		clock++;
		comments.push({ body: input?.body ?? "", createdAt: new Date(Date.UTC(2026, 7, 14, 0, 0, clock)).toISOString() });
		data = { commentCreate: { success: true } };
	} else if (query.includes("issueAddLabel")) {
		nowSelected = true;
		data = { issueAddLabel: { success: true } };
	} else if (query.includes("issueLabels(")) {
		data = { issueLabels: { nodes: [{ id: "label-now", name: "now" }] } };
	} else if (query.includes("issues(first:2")) {
		data = { issues: { nodes: nowSelected ? [{ id: "id-1", identifier: "HOME-1", title: "t" }] : [] } };
	} else if (query.includes("comments(last:50)")) {
		data = { issue: { id: "id-1", identifier: "HOME-1", title: "t", comments: { nodes: comments } } };
	} else if (query.includes("issue(id:")) {
		data = { issue: { id: "id-1", identifier: "HOME-1", title: "t" } };
	} else {
		throw new Error(`unhandled GraphQL: ${query}`);
	}
	return new Response(JSON.stringify({ data }), { status: 200 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const loaded = await loadExtensions([path.join(repoRoot, "session-system/extensions/linear-now.ts")], probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("linear-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");
const stopHandler = extension.handlers.get("session_stop")?.[0];
if (!stopHandler) throw new Error("session_stop handler missing");

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
	0,
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
	{ theme: { fg: (_color: string, text: string) => text }, setStatus: () => {}, notify: () => {} } as never,
);
await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

const toolText = async (params: Record<string, unknown>): Promise<string> => {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
};
const stop = async (file: string): Promise<string> => {
	const result = await stopHandler({ type: "session_stop", stop_hook_active: false, session_file: file }, ctx);
	if (result && "additionalContext" in result && typeof result.additionalContext === "string") return result.additionalContext;
	return "none";
};

await confirmRoundTrip(toolText, { action: "set_now", work: "HOME-1" });
const local = path.join(probe, "sessions", "one", "local");
fs.mkdirSync(local, { recursive: true });
const planFile = path.join(local, "work-plan.md");
fs.writeFileSync(planFile, "# inert file\n");
const sessionFile = path.join(probe, "sessions", "one.jsonl");

const out: Record<string, string | number> = {};
out.fileOnly = await stop(sessionFile);
const planContent = "# Work\n\n## Approach\n1. Change it\n\n## Verification\n1. Check it\n";
await runner.emit({ type: "plan_approved", planFilePath: "local://work-plan.md", planContent, title: "Work" } as never);
out.approved = await stop(sessionFile);
out.handoff = await toolText({ action: "append_evidence", work: "HOME-1", kind: "handoff", body: "done / none / resume" });
out.settled = await stop(sessionFile);
fs.writeFileSync(planFile, "# still inert after rewrite\n");
out.rewrittenFile = await stop(sessionFile);
out.planComments = comments.filter(comment => comment.body.startsWith("**Plan approved**")).length;
process.stdout.write(JSON.stringify(out));
