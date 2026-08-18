// HOME-122 harness: drive the real linear-now extension through its public
// events, command, and tool with a deterministic in-memory Linear API.
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
const mode = process.argv[3];
const MODES = ["intake", "plan", "summary", "summary-subagent", "done", "footer", "audit", "restore"];
if (!probe || !mode || !MODES.includes(mode)) throw new Error(`usage: harness <probe-repo> ${MODES.join("|")}`);

interface Comment {
	body: string;
	createdAt: string;
}

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const issue = { id: "id-1", identifier: "HOME-1", title: "First", project: undefined as { name: string } | undefined };
const comments: Comment[] = [];
const writes = { created: 0, addNow: 0, removeNow: 0, closed: 0 };
let nowSelected = mode === "restore";
let nowId: string | null = mode === "restore" ? "id-1" : null;
let slotVersion = 1;
const receipts: Array<Record<string, unknown>> = [];
const closeoutIntents: Array<{ state: string; candidate_id: string }> = [];

interface MockWorkItem {
	work_id: string;
	workspace_id: string;
	alias: { key: string };
	revision: { revision_id: string; title: string; description: string; scope: string; acceptance_criteria: string[] };
	state: string;
	project_id: string | null;
	candidate: { candidate_id: string; candidate_sha256: string } | null;
}

const items = new Map<string, MockWorkItem>();
const initialItem: MockWorkItem = {
	work_id: "id-1",
	workspace_id: WORKSPACE_ID,
	alias: { key: "HOME-1" },
	revision: { revision_id: "rev-1", title: "First", description: "", scope: "", acceptance_criteria: [] },
	state: "IN_PROGRESS",
	project_id: "proj-1",
	candidate: null,
};
items.set("HOME-1", initialItem);
items.set("id-1", initialItem);

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
		return new Response(
			JSON.stringify({
				projects: [{ project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" }],
				items: Array.from(new Set(items.values())),
				relations: [],
			}),
			{ status: 200 },
		);
	}
	if (u.includes(`/focus/${OWNER_ID}`)) {
		return new Response(
			JSON.stringify({
				workspace_id: WORKSPACE_ID,
				owner_id: OWNER_ID,
				work_id: nowSelected ? (nowId ?? "id-1") : null,
				version: slotVersion,
			}),
			{ status: 200 },
		);
	}
	if (u.endsWith("/workflow")) {
		const key = u.split("/v1/work-items/")[1]?.split("/")[0] ?? "HOME-1";
		const it = items.get(key) ?? initialItem;
		const plan = receipts.filter(r => r.kind === "plan").at(-1) as Record<string, unknown> | undefined;
		const handoff = receipts.filter(r => r.kind === "handoff").at(-1) as Record<string, unknown> | undefined;
		const audit = receipts.filter(r => r.kind === "audit").at(-1) as Record<string, unknown> | undefined;
		const closeout = receipts.filter(r => r.kind === "closeout").at(-1) as Record<string, unknown> | undefined;
		const review = closeout ?? audit;
		return new Response(
			JSON.stringify({
				item: it,
				project: { project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" },
				plan: plan ? { plan_name: "work-plan.md", plan_sha256: ((plan.payload as Record<string, unknown>)?.plan_sha256 as string) ?? "", at: plan.issued_at } : null,
				handoff: handoff ? { at: handoff.issued_at } : null,
				review: review ? { hash: String((review.payload_sha256 as string) ?? "").slice(0, 12), at: review.issued_at } : null,
				closeout: closeoutIntents,
				relations: [],
				receipts,
				current_candidate: it.candidate,
			}),
			{ status: 200 },
		);
	}
	if (u.includes("/v1/work-items/")) {
		const key = decodeURIComponent(u.split("/v1/work-items/")[1] ?? "HOME-1");
		const it = items.get(key) ?? initialItem;
		return new Response(JSON.stringify(it), { status: 200 });
	}
	if (method === "POST" && u.endsWith("/v1/commands")) {
		const env = JSON.parse(init?.body ?? "{}") as { command: { type: string; payload: Record<string, unknown> } };
		const cmdType = env.command?.type;
		const payload = env.command?.payload ?? {};
		if (cmdType === "create_work_batch") {
			const batchItems = (payload.items as Array<{ title: string; description?: string }>) ?? [];
			const createdList: Array<{ client_ref: string; work_id: string; revision_id: string; key: string; state: string; row_version: number }> = [];
			for (const b of batchItems) {
				writes.created++;
				const created: MockWorkItem = {
					work_id: `id-${writes.created}`,
					workspace_id: WORKSPACE_ID,
					alias: { key: `HOME-${writes.created}` },
					revision: { revision_id: `rev-${writes.created}`, title: b.title, description: b.description ?? "", scope: "", acceptance_criteria: [] },
					state: "IN_PROGRESS",
					project_id: "proj-1",
					candidate: null,
				};
				items.set(created.alias.key, created);
				items.set(created.work_id, created);
				createdList.push({ client_ref: "p", work_id: created.work_id, revision_id: created.revision.revision_id, key: created.alias.key, state: created.state, row_version: 1 });
				if (writes.created === 1) Object.assign(issue, { id: created.work_id, identifier: created.alias.key, title: created.revision.title });
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000010" },
					result: { type: "create_work_batch", items: createdList },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "set_focus") {
			writes.addNow++;
			nowSelected = true;
			nowId = ((payload.slot as Record<string, unknown>)?.work_id as string) ?? "id-1";
			slotVersion++;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000011" },
					result: { type: "set_focus", workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: nowId, version: slotVersion },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "clear_focus") {
			writes.removeNow++;
			nowSelected = false;
			nowId = null;
			slotVersion++;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000012" },
					result: { type: "clear_focus", workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: null, version: slotVersion },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "append_evidence") {
			const rec = payload.receipt as Record<string, unknown>;
			const kind = rec.kind as string;
			if (kind === "plan") {
				const planPayload = (rec.payload as Record<string, unknown>) ?? {};
				const appText = ((planPayload.approach as string[]) ?? []).map((s, i) => `${i + 1}. ${s}`).join("\n");
				const verText = ((planPayload.verification as string[]) ?? []).map((s, i) => `${i + 1}. ${s}`).join("\n");
				comments.push({
					body: `**Plan approved**\n- SHA-256: \`${planPayload.plan_sha256}\`\n\n## Approach\n${appText}\n\n## Verification\n${verText}`,
					createdAt: new Date().toISOString(),
				});
				const it = items.get(rec.work_id as string) ?? initialItem;
				it.candidate = { candidate_id: rec.candidate_id as string, candidate_sha256: "0".repeat(64) };
			} else if (kind === "closeout") {
				comments.push({ body: `**Session review**\n- Plan SHA-256: \`${(rec.payload as Record<string, unknown>)?.plan_sha256 ?? (rec.payload as Record<string, unknown>)?.body ?? ""}\``, createdAt: new Date().toISOString() });
			} else if (kind === "audit") {
				comments.push({ body: String((rec.payload as Record<string, unknown>)?.report ?? "VERDICT: PASS"), createdAt: new Date().toISOString() });
			}
			receipts.push(rec);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000013" },
					result: { type: "append_evidence", receipt: rec },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "finalize_candidate") {
			const it = items.get("HOME-1") ?? initialItem;
			const finalCandId = (payload.candidate_id as string) ?? "cand-1";
			const plannedCandId = (payload.planned_candidate_id as string) ?? it.candidate?.candidate_id;
			const commitSha = (payload.commit_sha as string) ?? "commit-1";
			const finalCand = {
				candidate_id: finalCandId,
				work_id: it.work_id,
				revision_id: it.revision.revision_id,
				candidate_sha256: "0".repeat(64),
				commit_sha: commitSha,
				kind: "final" as const,
				allocated_at: new Date().toISOString(),
			};
			it.candidate = finalCand;
			initialItem.candidate = finalCand;
			for (const v of items.values()) v.candidate = finalCand;
			const planRec = receipts.filter(r => r.kind === "plan" && r.candidate_id === plannedCandId).at(-1);
			if (planRec) {
				receipts.push({
					...planRec,
					receipt_id: `rec-plan-${finalCandId}`,
					candidate_id: finalCandId,
					issued_at: new Date().toISOString(),
					candidate_sha256: finalCand.candidate_sha256,
					candidate_commit: commitSha,
				});
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000014" },
					result: { type: "finalize_candidate", candidate: finalCand },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "request_closeout") {
			const it = items.get("id-1") ?? initialItem;
			const intent = { state: "pending", candidate_id: it.candidate?.candidate_id ?? "cand-1" };
			closeoutIntents.push(intent);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000015" },
					result: { type: "request_closeout", intent },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "complete_work") {
			writes.closed++;
			const inp = payload.input as Record<string, unknown>;
			const it = items.get((inp?.work_id as string) ?? "id-1") ?? initialItem;
			it.state = "DONE";
			comments.push({ body: "**Owner verdict in session: done**", createdAt: new Date().toISOString() });
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000016" },
					result: { type: "complete_work", work_id: it.work_id, state: "DONE", row_version: 2 },
				}),
				{ status: 200 },
			);
		}
		return new Response(
			JSON.stringify({
				receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000017" },
				result: { type: cmdType },
			}),
			{ status: 200 },
		);
	}
	return new Response(JSON.stringify({ error: { code: "not_found", diagnostics: [u] } }), { status: 404 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const EXTENSION_FILES =
	mode === "audit"
		? ["session-system/extensions/work-now.ts", "session-system/extensions/model-bookends.ts"]
		: ["session-system/extensions/work-now.ts"];
const loaded = await loadExtensions(EXTENSION_FILES.map(file => path.join(repoRoot, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

const uiCalls: string[] = [];
const statuses: string[] = [];
const statusCalls: { key: string; text: string | null; placement: string }[] = [];
const depth = mode === "summary-subagent" ? 1 : 0;
const inheritedNow =
	mode === "summary-subagent"
		? [{ type: "custom", customType: "work-now", data: { backend: "work", issueId: "id-1", identifier: "HOME-1", title: "First", setAt: Date.now() } }]
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
			return true;
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
} else if (mode === "restore") {
	out.now = await execute({ action: "my_now" });
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
	comments.length = 0;
	receipts.length = 0;
	for (const v of items.values()) v.candidate = null;
	initialItem.candidate = null;
	await runner.emit(summaryMessage as never);
	out.repeatSummaryNotice = uiCalls.at(-1);
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
	await runner.emitInput("/summary", undefined, "interactive");
	await runner.emit(summaryMessage as never);
	const it = items.get("HOME-1") ?? initialItem;
	const candId = it.candidate?.candidate_id ?? "cand-1";
	receipts.push({
		receipt_id: "rec-v",
		work_id: it.work_id,
		revision_id: "rev-1",
		candidate_id: candId,
		kind: "verification",
		payload: { body: "tests pass" },
		payload_sha256: "0".repeat(64),
		issued_at: new Date().toISOString(),
	});
	receipts.push({
		receipt_id: "rec-a",
		work_id: it.work_id,
		revision_id: "rev-1",
		candidate_id: candId,
		kind: "audit",
		verdict: "PASS",
		independent: true,
		payload: { report: "VERDICT: PASS" },
		payload_sha256: "0".repeat(64),
		issued_at: new Date().toISOString(),
	});
	closeoutIntents.push({ state: "pending", candidate_id: candId });
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
