// HOME-147 candidate smoke harness: drives the REAL work-now backend against
// the live loopback WorkService the parent test orchestrates. Runs as a child
// process with isolated HOME/XDG env (repo full-suite-safety rule).
//
// Phased execution across process restarts:
//   harness <probe-repo> <audit|closeout|done> [work-key]
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { discoverAgents, getAgent } from "@oh-my-pi/pi-coding-agent/task";
import { finalizeSubprocessOutput } from "@oh-my-pi/pi-coding-agent/task/executor";
import { createWorkBackend } from "../../extensions/workflow/work";
import { loadBearer, loadWorkConfig } from "../../extensions/workflow/config";
import { confirmRoundTrip } from "./two-phase";

const probe = process.argv[2];
const phase = process.argv[3] as "audit" | "closeout" | "done";
const workKeyArg = process.argv[4];

if (!probe || !phase || !["audit", "closeout", "done"].includes(phase)) {
	throw new Error("usage: harness <probe-repo> <audit|closeout|done> [work-key]");
}
if ((phase === "closeout" || phase === "done") && !workKeyArg) {
	throw new Error(`phase "${phase}" requires work-key argument`);
}

const repoRoot = path.resolve(import.meta.dir, "../../..");

// Network guard: record every URL the backend touches; the parent asserts all
// are loopback (the WorkService backend must never cross the network boundary).
const fetchUrls: string[] = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
	fetchUrls.push(String(input instanceof Request ? input.url : input));
	return realFetch(input, init);
}) as typeof fetch;

// OMP-38 step-6 smoke: OMP_WORK_SMOKE_EXT_DIR points at the INSTALLED
// extension set (~/.omp/agent/extensions) to prove the linked/installed
// artifacts load and gate identically; default is the repo source.
const extDir = process.env.OMP_WORK_SMOKE_EXT_DIR ?? path.join(repoRoot, "session-system/extensions");
const loaded = await loadExtensions(["work-now.ts", "model-bookends.ts"].map(file => path.join(extDir, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

const uiCalls: string[] = [];
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const sessionId = `smoke-${phase}`;
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => [], getSessionId: () => sessionId } as never,
	{ getAvailable: () => [fableModel], hasProvider: () => true } as never,
	undefined,
	// AC-4 (OMP-38): @audit resolves to the SESSION'S OWN family — the gate must
	// accept a same-family auditor; independence is fresh context, not family.
	{ getModelRole: (role: string) => (role === "audit" ? "anthropic/claude-fable-5" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	undefined,
	undefined,
	0,
);
runner.initialize(
	{
		appendEntry: () => {},
		getSessionId: () => sessionId,
		deliverMessage: async () => {},
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
			// Decline ONLY the pre-session opt-in (owner.txt must stay outside the
			// candidate — asserted by the smoke); confirm every other gate:
			// freeze, verdict, capture.
			return !title.includes("pre-session file(s)");
		},
	} as never,
);

if (phase === "audit") {
	// Owner work present before this transcript must survive candidate freeze.
	fs.writeFileSync(path.join(probe, "owner.txt"), "owner setting\n");
}

await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}

const cfg = loadWorkConfig()!;
const harnessBackend = createWorkBackend(cfg, () => loadBearer(cfg));

async function waitForNoPendingDeliveries(key: string) {
	for (let i = 0; i < 40; i++) {
		const pending = await harnessBackend.pendingDeliveries(key);
		if (pending.length === 0) return;
		await Bun.sleep(50);
	}
	const finalPending = await harnessBackend.pendingDeliveries(key);
	if (finalPending.length > 0) {
		throw new Error(`delivery debt remains after 40 checks: ${JSON.stringify(finalPending)}`);
	}
}

const out: Record<string, unknown> = {};
const summaryMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "summary", path: "/x/SKILL.md" }, content: "summary", timestamp: Date.now() },
};

if (phase === "audit") {
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

	// 1b. real acceptance criteria via the description fallback (OMP-38): the
	// packet parses only the `## Acceptance criteria` section of the revision.
	await confirmRoundTrip(execute, {
		action: "revise_work",
		work: key,
		description: "Smoke item.\n\n## Acceptance criteria\n- AC-1 the item closes done with a pushed candidate",
	});

	// 2. plan stamp (approved plan event).
	const plan = "# Smoke\n\n## Approach\n1. Freeze and push the candidate\n\n## Verification\n1. The smoke asserts the closed state\n";
	const planResult = await runner.emit({ type: "plan_approved", planFilePath: "local://work-plan.md", planContent: plan, title: "Smoke" } as never);
	out.plan = planResult === undefined ? "stamped" : planResult;

	// 3. work happens: a dirty file the freeze must pick up.
	fs.writeFileSync(path.join(probe, "smoke.txt"), "candidate payload\n");

	// 4. owner-entered /skill:summary authorizes on trusted raw input before its
	// structured prompt starts.
	await runner.emitInput("/skill:summary", undefined, "interactive");
	await runner.emit(summaryMessage as never);

	// 5. OMP-38: rebuild the auditor task from the ledger's PLAN PACKET — no
	// transcript reads. get_work supplies the bound Final commit and plan receipt.
	const getWork = await execute({ action: "get_work", work: key });
	out.getWork = getWork;
	const packetCommit = /^final commit: ([0-9a-f]{40})$/m.exec(getWork)?.[1] ?? "";
	const packetReceiptSha = /^plan receipt sha256: ([0-9a-f]{64})$/m.exec(getWork)?.[1] ?? "";
	const packetPlanBody = (getWork.split("plan body (exact stored bytes):\n")[1] ?? "").trim();
	const packetCriteria = (/^acceptance criteria:\n([\s\S]*?)^plan body /m.exec(getWork)?.[1] ?? "")
		.trim()
		.split("\n")
		.map(line => line.replace(/^- /, ""))
		.filter(line => line && line !== "(none recorded)");
	out.packetCommit = packetCommit;
	out.packetReceiptSha = packetReceiptSha;
	out.packetPlanBody = packetPlanBody;
	out.packetCriteria = packetCriteria;

	// 6. OMP-47: verification evidence seals the audit manifest server-side.
	out.verification = await execute({ action: "append_evidence", work: key, kind: "verification", body: "the smoke asserts the end state" });

	// 7. get_work renders next action banner without task bytes
	const nextActionScreen = await execute({ action: "get_work", work: key });
	out.nextActionScreen = nextActionScreen;
	out.hasRunAuditNextAction = nextActionScreen.includes(`NEXT REQUIRED ACTION: work action:"run_audit", work:"${key}"`);
	out.noSealedTaskBytes = !nextActionScreen.includes("----- SEALED AUDITOR TASK BEGIN -----");

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

	// Discover auditor agent, require its output schema, and run yield through finalizeSubprocessOutput
	const discovery = await discoverAgents(probe);
	const auditorAgent = getAgent(discovery.agents, "auditor");
	if (!auditorAgent || !auditorAgent.output) {
		throw new Error("discovered auditor agent is missing required output schema");
	}
	const finalized = finalizeSubprocessOutput({
		rawOutput: "",
		exitCode: 0,
		stderr: "",
		yieldItems: [{ data: { report: REPORT } }],
		outputSchema: auditorAgent.output,
		outputSchemaSource: "agent",
		outputSchemaMode: "strict",
	});
	const transportPayload = finalized.rawOutput;
	out.transportPayload = transportPayload;

	// Reserve and settle launch directly against work service
	const detail = await harnessBackend.issueDetail(key);
	const manifest = (await harnessBackend.sealedAuditTask(key))!;
	const reservation = await harnessBackend.reserveAuditorLaunch(key, manifest.taskSha256, "smoke-call-1");
	const settle = await harnessBackend.settleAuditorLaunch(key, reservation.launchId!, { payload: transportPayload });
	out.audit = settle.event.renderedText;
	if (settle.event.requiresDelivery) {
		await harnessBackend.attestDelivery(settle.event.eventId, sessionId, settle.event.renderedSha256, "delivered");
	}
	await waitForNoPendingDeliveries(key);
} else if (phase === "closeout") {
	const key = workKeyArg!;
	// Resume the close attempt in fresh session by entering /skill:summary
	const resumeInput = await runner.emitInput("/skill:summary", undefined, "interactive");
	out.resumeText = resumeInput?.text;
	out.isResumeDigest = Boolean(
		resumeInput?.text &&
		resumeInput.text.includes(`# SUMMARY RESUME (${key})`) &&
		resumeInput.text.includes("STATUS: CLOSE ATTEMPT audited") &&
		resumeInput.text.includes("Satisfied steps must NOT be repeated")
	);

	await waitForNoPendingDeliveries(key);

	out.closeout = await execute({ action: "append_evidence", work: key, kind: "closeout", body: "session review: candidate smoke completed" });

	await waitForNoPendingDeliveries(key);

	// create a second item to cancel via staged cancel batch
	const targetCreate = await confirmRoundTrip(execute, {
		action: "create_work",
		title: "Item to cancel in batch",
		project: "Smoke Project",
	});
	const targetMatch = /(?:created|becomes NOW)\s+(HOME-\d+|OMP-\d+)/.exec(targetCreate.confirmed) ?? /(HOME-\d+|OMP-\d+)/.exec(targetCreate.confirmed);
	if (!targetMatch?.[1]) throw new Error(`could not parse created target key from: ${targetCreate.confirmed}`);
	const targetKey = targetMatch[1];
	const canonical = fs.realpathSync(probe);
	const cwdHash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
	const homeDir = process.env.HOME || "/tmp";
	const batchDir = path.join(homeDir, ".omp", "agent", "work-cancel-batches");
	fs.mkdirSync(batchDir, { recursive: true, mode: 0o700 });
	const batchFile = path.join(batchDir, `${cwdHash}.json`);
	fs.writeFileSync(batchFile, JSON.stringify([{ key: targetKey, reason: "superseded by candidate smoke" }]), { mode: 0o600 });
	fs.chmodSync(batchFile, 0o600);

	out.targetKey = targetKey;
} else if (phase === "done") {
	const key = workKeyArg!;
	await waitForNoPendingDeliveries(key);

	const canonical = fs.realpathSync(probe);
	const cwdHash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
	const homeDir = process.env.HOME || "/tmp";
	const batchDir = path.join(homeDir, ".omp", "agent", "work-cancel-batches");
	const batchFile = path.join(batchDir, `${cwdHash}.json`);

	// 9. /done (preflight → verdict with cancel batch → push → complete).
	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	await done.handler("", runner.createCommandContext());

	out.doneUi = uiCalls;
	out.batchFileExists = fs.existsSync(batchFile);
	out.consumedBatchFiles = fs.existsSync(batchDir) ? fs.readdirSync(batchDir).filter(f => f.includes(".consumed-")) : [];
	out.now = await execute({ action: "my_now" });
}

out.fetchUrls = fetchUrls;
process.stdout.write(JSON.stringify(out));
