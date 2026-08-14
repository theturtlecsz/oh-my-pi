// Obligation-loop harness: loads the real extension and drives the HOME-45
// digest obligation machine through real handlers with a stubbed Linear API.
// Proves the 2026-08-14 loop fix: a plan whose digest was posted stays settled
// (same session dir + name, unchanged mtime); a rewrite or a same-named plan in
// a DIFFERENT session's local dir re-arms.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const probe = process.argv[2];
if (!probe) throw new Error("usage: harness <probe-repo>");

// Stub Linear before any driving — gql resolves global fetch at call time.
globalThis.fetch = (async (_url: unknown, init: { body?: string }) => {
	const parsed: unknown = JSON.parse(init?.body ?? "{}");
	const q = parsed && typeof parsed === "object" && "query" in parsed && typeof parsed.query === "string" ? parsed.query : "";
	const data = q.includes("commentCreate")
		? { commentCreate: { success: true } }
		: { issue: { id: "id-1", identifier: "HOME-1", title: "t" } };
	return new Response(JSON.stringify({ data }), { status: 200 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const result = await loadExtensions([path.join(repoRoot, "session-system/extensions/linear-now.ts")], probe);
if (result.errors.length > 0) throw new Error(result.errors.map(error => error.error).join("; "));
const ext = result.extensions[0];
if (!ext) throw new Error("linear-now extension did not load");
const tool = ext.tools.get("linear");
if (!tool) throw new Error("linear tool missing");
const stopHandler = ext.handlers.get("session_stop")?.[0];
if (!stopHandler) throw new Error("session_stop handler missing");

const runner = new ExtensionRunner(
	result.extensions,
	result.runtime,
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
	{} as never,
	{
		getModel: () => undefined,
		isIdle: () => true,
		abort: () => {},
		hasPendingMessages: () => false,
		shutdown: () => {},
		getSystemPrompt: () => [],
	} as never,
	undefined,
	{ theme: { fg: (_c: string, t: string) => t }, setStatus: () => {}, notify: () => {} } as never,
);
await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

// Two fake session transcripts, each with a same-named plan in its local/ dir.
const sessions = path.join(probe, "sessions");
const mkSession = (name: string): { file: string; plan: string } => {
	const local = path.join(sessions, name, "local");
	fs.mkdirSync(local, { recursive: true });
	const plan = path.join(local, "test-plan.md");
	fs.writeFileSync(plan, "# plan\n");
	return { file: path.join(sessions, `${name}.jsonl`), plan };
};
const a = mkSession("sessA");
const b = mkSession("sessB");

const comment = async (): Promise<void> => {
	const res = await tool.definition.execute("t", { action: "comment", issue: "HOME-1", body: "digest" }, undefined, undefined, ctx);
	const text = res.content.map((p: { type: string; text?: string }) => (p.type === "text" ? (p.text ?? "") : "")).join("\n");
	if (!text.includes("comment posted")) throw new Error(`comment failed: ${text}`);
};
const stop = async (file: string): Promise<string> => {
	const r: unknown = await stopHandler({ type: "session_stop", stop_hook_active: false, session_file: file }, ctx);
	if (r && typeof r === "object" && "additionalContext" in r && typeof r.additionalContext === "string") return r.additionalContext;
	return "none";
};

// Arm executingIssue (comment on the issue this session executes against);
// discharges the just-armed handoff so later stops isolate the digest path.
await comment();

const out: Record<string, string> = {};
out.armed = await stop(a.file); // plan A discovered → digest owed
await comment(); // digest posted → plan A settled
out.settled = await stop(a.file); // same plan, unchanged → must stay quiet
Bun.sleepSync(10); // ensure the rewrite mtime lands strictly after the first discharge
const now = new Date();
fs.utimesSync(a.plan, now, now); // plan rewritten after its digest (realistic past mtime)
out.rewritten = await stop(a.file);
await comment(); // digest for the rewrite
out.resettled = await stop(a.file); // rewrite digested → quiet again
out.otherSession = await stop(b.file); // same name, different session dir → owed
process.stdout.write(JSON.stringify(out));
