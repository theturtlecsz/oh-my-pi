// HOME-122 obligation harness: plan files are inert; only an approved-plan
// event arms execution, and only a typed handoff settles it.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
if (!probe) throw new Error("usage: harness <probe-repo>");

const WORK_ID = "00000000-0000-7000-8000-000000000003";
const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
let slotVersion = 1;
let nowWorkId: string | null = null;
const receipts: Array<Record<string, unknown>> = [];
let planComments = 0;
globalThis.fetch = (async (url: unknown, init?: { body?: string; method?: string }) => {
	const u = String(url);
	const method = init?.method ?? "GET";

	if (u.includes("/v1/health/live") || u.includes("/v1/health/ready")) {
		return new Response(JSON.stringify({ live: true, ready: true, alerts: [] }), { status: 200 });
	}
	if (u.includes("/authority")) {
		return new Response(JSON.stringify({ authority: "work", epoch_id: null, epoch_state: "sealed" }), { status: 200 });
	}
	if (u.includes("/tree")) {
		return new Response(JSON.stringify({ projects: [], items: [], relations: [] }), { status: 200 });
	}
	if (u.includes(`/focus/${OWNER_ID}`)) {
		return new Response(
			JSON.stringify({
				workspace_id: WORKSPACE_ID,
				owner_id: OWNER_ID,
				work_id: nowWorkId,
				version: slotVersion,
			}),
			{ status: 200 },
		);
	}
	if (u.endsWith("/workflow")) {
		const planRec = receipts.filter(r => r.kind === "plan").at(-1);
		const handoffRec = receipts.filter(r => r.kind === "handoff").at(-1);
		const candId = planRec ? (planRec.candidate_id as string) : undefined;
		return new Response(
			JSON.stringify({
				item: {
					work_id: WORK_ID,
					workspace_id: WORKSPACE_ID,
					alias: { key: "HOME-1" },
					revision: { revision_id: "rev-1", title: "Work", description: "", scope: "", acceptance_criteria: [] },
					state: "IN_PROGRESS",
					candidate: candId ? { candidate_id: candId, candidate_sha256: "0".repeat(64) } : null,
				},
				plan: planRec ? { plan_name: "work-plan.md", plan_sha256: planRec.payload.plan_sha256, at: planRec.issued_at } : null,
				handoff: handoffRec ? { at: handoffRec.issued_at } : null,
				review: null,
				closeout: [],
				relations: [],
				receipts,
				current_candidate: candId ? { candidate_id: candId, candidate_sha256: "0".repeat(64) } : null,
			}),
			{ status: 200 },
		);
	}
	if (u.includes("/v1/work-items/")) {
		const planRec = receipts.filter(r => r.kind === "plan").at(-1);
		const candId = planRec ? (planRec.candidate_id as string) : undefined;
		return new Response(
			JSON.stringify({
				work_id: WORK_ID,
				workspace_id: WORKSPACE_ID,
				alias: { key: "HOME-1" },
				revision: { revision_id: "rev-1", title: "Work", description: "", scope: "", acceptance_criteria: [] },
				state: "IN_PROGRESS",
				project_id: null,
				candidate: candId ? { candidate_id: candId, candidate_sha256: "0".repeat(64) } : null,
			}),
			{ status: 200 },
		);
	}
	if (method === "POST" && u.endsWith("/v1/commands")) {
		const env = JSON.parse(init?.body ?? "{}") as { command: { type: string; payload: Record<string, unknown> } };
		const cmdType = env.command?.type;
		const payload = (env.command?.payload ?? {}) as Record<string, unknown>;
		if (cmdType === "set_focus") {
			slotVersion++;
			nowWorkId = payload.slot?.work_id ?? WORK_ID;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000010" },
					result: { type: "set_focus", workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: nowWorkId, version: slotVersion },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "append_evidence") {
			const rec = payload.receipt;
			if (rec.kind === "plan") planComments++;
			receipts.push(rec);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000012" },
					result: { type: "append_evidence", receipt: rec },
				}),
				{ status: 200 },
			);
		}
		return new Response(
			JSON.stringify({
				receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000013" },
				result: { type: cmdType },
			}),
			{ status: 200 },
		);
	}
	return new Response(JSON.stringify({ error: { code: "not_found", diagnostics: [u] } }), { status: 404 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const loaded = await loadExtensions([path.join(repoRoot, "session-system/extensions/work-now.ts")], probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
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
out.planComments = planComments;
process.stdout.write(JSON.stringify(out));
