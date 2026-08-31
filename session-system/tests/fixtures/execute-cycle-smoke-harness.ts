// OMP-180 execution cycle smoke harness: drives the REAL work-now extension
// and workflow host against the live loopback WorkService.
import { vi } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { checkProspectiveContract } from "../../extensions/workflow/config";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import * as executorModule from "@oh-my-pi/pi-coding-agent/task/executor";
import { createWorkBackend } from "../../extensions/workflow/work";
import { loadBearer, loadWorkConfig } from "../../extensions/workflow/config";
import { freezeCandidateCommit } from "../../extensions/workflow/git";
import { WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";

const scenario = process.argv[3] as "single" | "dirty" | "queue" | "contract-pause" | "start-only" | "recovery" | "tamper-a" | "tamper-b" | "tamper-c" | "tamper-d" | "blocked" | "freeze-probes" | "judge-freeze" | "judge-resume" | "already-delivered" | "already-unmet";
const probe = process.argv[2];
const workKeyArg = process.argv[4];

if (!probe || !scenario) {
	throw new Error("usage: harness <probe-repo> <single|dirty|queue|tamper-*> [work-key]");
}

const repoRoot = path.resolve(import.meta.dir, "../../..");
const extDir = process.env.OMP_WORK_SMOKE_EXT_DIR ?? path.join(repoRoot, "session-system/extensions");
let subprocessCount = 0;
const NEEDS_FIX_REPORT = "VERDICT: NEEDS_FIX\n\nFINDINGS\n- [major] AC-1 src/smoke_feat.ts:1 evidence: feat is false; impact: broken; minimal fix: set to true\n\nACCEPTANCE COVERAGE\nAC-1 deliver smoke feature\n\nOUT OF SCOPE\nnone\n\nCHECKS RUN\nbun test\n\nREMAINING QUESTIONS\nnone";
const PASS_REPORT = "VERDICT: PASS\n\nFINDINGS\n(none)\n\nACCEPTANCE COVERAGE\nAC-1 deliver smoke feature\n\nOUT OF SCOPE\nnone\n\nCHECKS RUN\nbun test\n\nREMAINING QUESTIONS\nnone";
const BLOCKED_REPORT = "VERDICT: BLOCKED\n\nFINDINGS\n- [blocker] AC-1 blocked on external dependency\n\nACCEPTANCE COVERAGE\nAC-1 deliver smoke feature\n\nOUT OF SCOPE\nnone\n\nCHECKS RUN\nbun test\n\nREMAINING QUESTIONS\nnone";

vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: any) => {
	subprocessCount++;
	const report = (scenario === "judge-freeze" || scenario === "judge-resume") ? PASS_REPORT : subprocessCount === 1 ? (scenario === "blocked" ? BLOCKED_REPORT : NEEDS_FIX_REPORT) : PASS_REPORT;
	const wrapped = JSON.stringify({ report });
	return {
		index: options.index,
		id: options.id,
		agent: options.agent.name,
		agentSource: options.agent.source,
		task: options.task,
		exitCode: 0,
		output: wrapped,
		stderr: "",
		truncated: false,
		durationMs: 100,
		tokens: 300,
		requests: 1,
	} as any;
});

const loaded = await loadExtensions(["work-now.ts", "model-bookends.ts"].map(file => path.join(extDir, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));

let extensions = loaded.extensions;
if (process.env.OMP_WORK_SMOKE_MISSING_YIELD === "1") {
	const { createWorkflowHost } = await import("../../extensions/workflow/host");
	const { createWorkBackend } = await import("../../extensions/workflow/work");
	const { loadBearer, loadWorkConfig } = await import("../../extensions/workflow/config");
	extensions = loaded.extensions.map((ext, idx) => {
		if (idx === 0) {
			return {
				...ext,
				factory: (pi: unknown) => {
					const config = loadWorkConfig();
					if (!config) return;
					createWorkflowHost({
						backend: createWorkBackend(config, () => loadBearer(config)),
						teamNoun: "the ledger",
						entryType: "work-now",
						acceptEntry: data => data.backend === "work",
						sourceResolver: (specifier: string) => specifier === "@oh-my-pi/pi-coding-agent/task/yield-assembly" ? undefined : import.meta.resolve(specifier),
					})(pi as never);
				},
				tools: new Map(ext.tools),
				commands: new Map(ext.commands),
				handlers: new Map(),
			};
		}
		return ext;
	});
}
const extension = extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const uiCalls: string[] = [];
const sentMessages: unknown[] = [];
let modelTurnCount = 0;
const sessionId = `smoke-exec-${scenario}`;
const sessionBranchFile = path.join(path.dirname(probe), ".smoke-session-branch.json");
const getBranch = () => {
	try {
		return JSON.parse(fs.readFileSync(sessionBranchFile, "utf8"));
	} catch {
		return [];
	}
};
const appendEntry = (customType: string, data: unknown) => {
	const list = getBranch();
	list.push({ type: "custom", customType, data });
	fs.writeFileSync(sessionBranchFile, JSON.stringify(list));
};
const runner = new ExtensionRunner(
	extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch, getSessionId: () => sessionId, taskDepth: 0 } as never,
	{ getAvailable: () => [fableModel], hasProvider: () => true } as never,
	undefined,
	{ getModelRole: (role: string) => (role === "audit" ? "anthropic/claude-fable-5" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	undefined,
	undefined,
	0,
);
runner.initialize(
	{
		appendEntry,
		getSessionId: () => sessionId,
		// Faithful to AgentSession.queueExtensionDelivery (OMP-97): the promise
		// settles only after the current turn yields - i.e. after the tool
		// handler returns. Awaiting it INSIDE a tool handler deadlocks; execute()
		// flushes the queue once the handler resolves.
		deliverMessage: () => new Promise<void>(resolve => pendingTurnDeliveries.push(resolve)),
		setModel: async () => true,
		getThinkingLevel: () => "high",
		setThinkingLevel: () => {},
		sendMessage: (message: unknown) => {
			sentMessages.push(message);
		},
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
			return true;
		},
	} as never,
);
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();
const cmdCtx = runner.createCommandContext();

const pendingTurnDeliveries: Array<() => void> = [];

async function execute(params: Record<string, unknown>): Promise<string> {
	modelTurnCount++;
	const toolDone = tool.definition.execute("t", params, undefined, undefined, ctx);
	const timeout = setTimeout(() => {
		throw new Error(`tool call deadlocked awaiting turn-yield delivery: ${JSON.stringify(params)}`);
	}, 120_000);
	try {
		const result = await toolDone;
		return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
	} finally {
		clearTimeout(timeout);
		// Turn yields now: settle queued extension deliveries.
		while (pendingTurnDeliveries.length) pendingTurnDeliveries.shift()!();
	}
}

/** Drive the cross-turn review protocol: each "END YOUR TURN NOW" response
 * queued a checkpoint delivery that settles when execute() flushes the turn;
 * re-invoke until the review reaches a verdict, completion, or refusal. */
async function reviewUntilSettled(body?: string): Promise<string> {
	let last = "";
	for (let turn = 0; turn < 8; turn++) {
		last = await execute({ action: "begin_execution_review", ...(body ? { body } : {}) });
		if (!last.includes("END YOUR TURN NOW")) return last;
		const { promise, resolve } = Promise.withResolvers<void>();
		setTimeout(resolve, 50);
		await promise;
	}
	throw new Error(`review never settled after 8 turns: ${last}`);
}

const out: Record<string, unknown> = {};

if (scenario === "dirty") {
	fs.writeFileSync(path.join(probe, "dirty.txt"), "dirty\n");
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");
	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	out.uiCalls = uiCalls;
	out.notices = uiCalls.filter(c => c.includes("execution_worktree_not_clean"));
	out.modelTurnCount = modelTurnCount;
	fs.rmSync(path.join(probe, "dirty.txt"), { force: true });
} else if (scenario === "single") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	out.executeNotices = uiCalls.filter(c => c.includes("Execution grant started"));

	// 1. get_execution
	const execState1 = JSON.parse(await execute({ action: "get_execution" }));
	out.initialPhase = execState1.activeItem?.phase;

	// 2. seal_execution_criteria
	const sealResult = await execute({
		action: "seal_execution_criteria",
		criteria: ["AC-1 guessed paraphrase"],
	});
	out.sealResult = sealResult;

	// 3. stamp_execution_plan
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write feature\n\n## Verification\n1. Check feature\n");
	const stampResult = await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["src/smoke_feat.ts"],
	});
	out.stampResult = stampResult;

	// 4. Implement on disk (initial attempt with failure)
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	fs.writeFileSync(path.join(probe, "src/smoke_feat.ts"), "export const feat = false;\n");

	// Missing body is refused before any mutation
	const noBodyReview = await execute({ action: "begin_execution_review" });
	out.noBodyRefused = noBodyReview.includes("Verification evidence body is required");

	// 5. Review with verification evidence (yields NEEDS_FIX)
	const review1 = await reviewUntilSettled("Ran test: expected true but got false (NEEDS_FIX)");
	out.review1 = review1;

	// 6. Remediate: fix code, restamp plan, re-review (yields PASS)
	fs.writeFileSync(path.join(probe, "src/smoke_feat.ts"), "export const feat = true;\n");
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write feature\n2. Fix feat to true\n\n## Verification\n1. Check feature\n");
	out.stampResult2 = await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["src/smoke_feat.ts"],
	});

	const review2 = await reviewUntilSettled("Ran test: feat is true, all checks passed");
	out.review2 = review2;

	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
	out.modelTurnCount = modelTurnCount;
	out.sentMessages = sentMessages;
} else if (scenario === "queue") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	await executeCmd.handler(`${workKeyArg || "OMP-2"} --queue`, cmdCtx);
	const execState1 = JSON.parse(await execute({ action: "get_execution" }));
	out.queueLength = execState1.items.length;
	out.item0WorkId = execState1.items[0]?.work_id;

	// Create an eligible item AFTER snapshot creation
	const config = loadWorkConfig();
	if (config) {
		const bearer = loadBearer(config);
		const postRes = await (await fetch(`${config.baseUrl}/v1/commands`, {
			method: "POST",
			headers: { authorization: `Bearer ${bearer}`, "X-OMP-Workspace-ID": config.workspaceId, "X-OMP-Contract-SHA256": WORK_CONTRACT_SHA256, "Content-Type": "application/json" },
			body: JSON.stringify({
				api_version: "work.omp.dev/v1",
				workspace_id: config.workspaceId,
				request_id: crypto.randomUUID(),
				correlation_id: crypto.randomUUID(),
				operation_id: crypto.randomUUID(),
				command: {
					type: "create_work_batch",
					payload: {
						items: [{
							client_ref: "smoke-item-post-snapshot",
							title: "Smoke Delivery Feature Post Snapshot",
							description: "Eligible item created after snapshot",
							scope: "smoke",
							acceptance_criteria: [],
							state: "BACKLOG",
							project_id: execState1.items[0]?.project_id,
						}],
					},
				},
			}),
		})).json();
		out.postSnapshotWorkId = postRes.result?.items?.[0]?.work_id;
		out.postSnapshotKey = postRes.result?.items?.[0]?.key;
	}
	// Process item 1
	await execute({ action: "seal_execution_criteria", criteria: ["AC-1: queue item 1"] });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write item 1\n\n## Verification\n1. Prove item 1\n");
	await execute({ action: "stamp_execution_plan", plan_file: planFile, paths: ["src/q1.ts"] });
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	fs.writeFileSync(path.join(probe, "src/q1.ts"), "export const q1 = true;\n");
	// Set subprocess count to 1 so review immediately PASSes
	subprocessCount = 1;
	const reviewQ1 = await reviewUntilSettled("test q1 passed");
	out.reviewQ1 = reviewQ1;

	// Check that focus advanced to item 2
	const execState2 = JSON.parse(await execute({ action: "get_execution" }));
	out.advancedItemPosition = execState2.activeItem?.position;
	out.completedItem0Phase = execState2.items[0]?.phase;

	// Process item 2
	await execute({ action: "seal_execution_criteria", criteria: ["AC-2: queue item 2"] });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write item 2\n\n## Verification\n1. Prove item 2\n");
	await execute({ action: "stamp_execution_plan", plan_file: planFile, paths: ["src/q2.ts"] });
	fs.writeFileSync(path.join(probe, "src/q2.ts"), "export const q2 = true;\n");
	subprocessCount = 1;
	const reviewQ2 = await reviewUntilSettled("test q2 passed");
	out.reviewQ2 = reviewQ2;

	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
} else if (scenario === "queue-dirt") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	await executeCmd.handler(`${workKeyArg || "OMP-2"} --queue`, cmdCtx);
	const execState1 = JSON.parse(await execute({ action: "get_execution" }));
	out.queueLength = execState1.items.length;
	// Process item 1
	await execute({ action: "seal_execution_criteria", criteria: ["AC-1: queue item 1"] });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write item 1\n\n## Verification\n1. Prove item 1\n");
	await execute({ action: "stamp_execution_plan", plan_file: planFile, paths: ["src/qdirt1.ts"] });
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	fs.writeFileSync(path.join(probe, "src/qdirt1.ts"), "export const qdirt1 = true;\n");
	subprocessCount = 1;
	// Turn 1: freeze & push
	const reviewT1 = await execute({ action: "begin_execution_review", body: "test qdirt1 passed" });
	out.reviewT1 = reviewT1;
	// Write residual untracked dirt after freeze before settlement completion
	fs.writeFileSync(path.join(probe, "residual-dirt.txt"), "residual dirt\n");
	// Turn 2 & 3: audit and completion with dirt
	const reviewQ1 = await reviewUntilSettled("test qdirt1 passed");
	out.reviewQ1 = reviewQ1;
	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
	out.sentMessages = sentMessages;
} else if (scenario === "contract-pause") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	// Clean worktree before starting
	fs.rmSync(path.join(probe, "src"), { recursive: true, force: true });
	Bun.spawnSync(["git", "clean", "-fdx"], { cwd: probe });
	Bun.spawnSync(["git", "reset", "--hard", "HEAD"], { cwd: probe });

	// Copy real contract directory to probe so manifest and hashing match real contracts
	const realContractDir = path.join(repoRoot, "python/omp-work/src/omp_work/contracts/v1");
	const probeContractDir = path.join(probe, "python/omp-work/src/omp_work/contracts/v1");
	fs.cpSync(realContractDir, probeContractDir, { recursive: true });
	// Commit the copied contract directory so preflight is clean
	Bun.spawnSync(["git", "add", "python"], { cwd: probe });
	Bun.spawnSync(["git", "commit", "-m", "add contract dir"], { cwd: probe });

	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);

	// 1. Seal criteria
	await execute({ action: "seal_execution_criteria", criteria: ["AC-1: change contract"] });

	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Modify contract\n\n## Verification\n1. Prove contract\n");
	fs.writeFileSync(path.join(probeContractDir, "contract.json"), JSON.stringify({ contract_version: "work.omp.dev/v1", modified: true }));
	await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["python/omp-work/src/omp_work/contracts/v1/contract.json"],
	});
	// 3. Begin execution review -> must be denied and grant paused
	const reviewDenied = await execute({
		action: "begin_execution_review",
		body: "testing contract change",
	});
	out.reviewDenied = reviewDenied;
	out.pausedExecution = JSON.parse(await execute({ action: "get_execution" }));

	// 4. Try to resume without approval -> must fail
	await executeCmd.handler("resume", cmdCtx);
	out.resumeDeniedNotices = uiCalls.filter(c => c.includes("Cannot resume:") && (c.includes("prospective digest") || c.includes("contract approval required")));

	// 5. Simulate owner approval (write approval.json with prospective digest)
	const contractCheck = checkProspectiveContract(probe);
	const approvalPath = path.join(probe, "python/omp-work/src/omp_work/contracts/v1/approval.json");
	fs.writeFileSync(approvalPath, JSON.stringify({
		contract_version: "work.omp.dev/v1",
		contract_sha256: contractCheck.prospectiveDigest,
		approved_by: "owner",
		approved_at: new Date().toISOString(),
		issue: workKeyArg || "OMP-1",
	}));

	// 6. Resume with approval -> succeeds
	await executeCmd.handler("resume", cmdCtx);
	out.resumedExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
} else if (scenario === "start-only") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");
	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	out.exec = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
} else if (scenario === "recovery") {
	out.sentMessages = sentMessages;
	out.modelTurnCount = modelTurnCount;
	out.uiCalls = uiCalls;
	try {
		out.exec = JSON.parse(await execute({ action: "get_execution" }));
	} catch {}
} else if (scenario === "blocked") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	await execute({
		action: "seal_execution_criteria",
		criteria: ["AC-1: deliver smoke feature"],
	});
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write feature\n\n## Verification\n1. Check feature\n");
	await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["src/blocked_feat.ts"],
	});
	fs.writeFileSync(path.join(probe, "src/blocked_feat.ts"), "export const blocked = true;\n");
	const reviewBlocked = await reviewUntilSettled("Ran test: blocked check");
	out.reviewBlocked = reviewBlocked;

	// Remediate after BLOCKED: fix code, restamp plan, re-review (yields PASS)
	fs.writeFileSync(path.join(probe, "src/blocked_feat.ts"), "export const blocked = false;\n");
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write feature\n2. Unblock feature\n\n## Verification\n1. Check feature\n");
	out.stampResult2 = await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["src/blocked_feat.ts"],
	});

	const reviewRemediated = await reviewUntilSettled("Ran test: blocker resolved, all checks passed");
	out.reviewRemediated = reviewRemediated;

	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
	out.sentMessages = sentMessages;
} else if (scenario === "judge-freeze") {
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");

	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	await execute({
		action: "seal_execution_criteria",
		criteria: ["AC-1: judge isolation check"],
	});
	fs.mkdirSync(path.join(probe, "session-system/agents"), { recursive: true });
	fs.mkdirSync(path.join(probe, "session-system/extensions/workflow"), { recursive: true });
	fs.mkdirSync(path.join(probe, "packages/coding-agent/src/task"), { recursive: true });
	fs.writeFileSync(path.join(probe, "session-system/agents/auditor.md"), "TAMPERED CANDIDATE AUDITOR\n");
	fs.writeFileSync(path.join(probe, "session-system/extensions/workflow/audit-tcb.ts"), "TAMPERED CANDIDATE TCB\n");
	fs.writeFileSync(path.join(probe, "packages/coding-agent/src/task/executor.ts"), "TAMPERED CANDIDATE EXECUTOR\n");

	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write judge isolation files\n\n## Verification\n1. Verify judge isolation\n");
	await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: [
			"session-system/agents/auditor.md",
			"session-system/extensions/workflow/audit-tcb.ts",
			"packages/coding-agent/src/task/executor.ts",
		],
	});
	const reviewTurn1 = await execute({
		action: "begin_execution_review",
		body: "Ran test: judge isolation verified",
	});
	out.reviewTurn1 = reviewTurn1;
	out.execAfterFreeze = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
	out.sentMessages = sentMessages;
} else if (scenario === "judge-resume") {
	const reviewResumed = await reviewUntilSettled("Ran test: judge isolation verified");
	out.reviewResumed = reviewResumed;
	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
	out.sentMessages = sentMessages;
} else if (scenario === "already-delivered") {
	// OMP-188/OMP-189: the sealed path is already committed and pushed at the
	// grant baseline; with the host passing expectedBaseline, the cycle binds
	// the baseline commit and completes autonomously.
	for (const args of [["fetch", "-q", "origin"], ["reset", "-q", "--hard", "origin/main"], ["clean", "-qfd", "--", "src/"]]) {
		const gitRun = Bun.spawnSync(["git", ...args], { cwd: probe });
		if (gitRun.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${gitRun.stderr.toString()}`);
	}
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	fs.writeFileSync(path.join(probe, "src/already_delivered.ts"), "export const delivered = true;\n");
	for (const args of [["add", "--", "src/already_delivered.ts"], ["commit", "-q", "-m", "deliver ahead of grant"], ["push", "-q", "origin", "main"]]) {
		const gitRun = Bun.spawnSync(["git", ...args], { cwd: probe });
		if (gitRun.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${gitRun.stderr.toString()}`);
	}
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");
	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	await execute({ action: "seal_execution_criteria", criteria: ["AC-1: feature already delivered at baseline"] });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Verify the delivered feature\n\n## Verification\n1. Prove the delivered feature\n");
	await execute({ action: "stamp_execution_plan", plan_file: planFile, paths: ["src/already_delivered.ts"] });
	subprocessCount = 1; // next auditor report: PASS
	out.review = await reviewUntilSettled("verified at baseline: src/already_delivered.ts committed and correct; no changes required");
	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
} else if (scenario === "already-unmet") {
	// OMP-189 AC-2: an empty sealed diff with UNMET criteria binds the
	// baseline, reaches the audit, and ends NEEDS_FIX with no completion —
	// the audit gate is the arbiter, never bypassed.
	for (const args of [["fetch", "-q", "origin"], ["reset", "-q", "--hard", "origin/main"], ["clean", "-qfd", "--", "src/"]]) {
		const gitRun = Bun.spawnSync(["git", ...args], { cwd: probe });
		if (gitRun.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${gitRun.stderr.toString()}`);
	}
	const executeCmd = extension.commands.get("execute");
	if (!executeCmd) throw new Error("execute command missing");
	await executeCmd.handler(workKeyArg || "OMP-1", cmdCtx);
	await execute({ action: "seal_execution_criteria", criteria: ["AC-1: feature that was never built"] });
	const planFile = "local://execute-plan.md";
	const planDiskPath = path.join(path.dirname(probe), "execute-plan.md");
	fs.mkdirSync(path.dirname(planDiskPath), { recursive: true });
	fs.writeFileSync(planDiskPath, "## Approach\n1. Claim the feature exists\n\n## Verification\n1. The audit catches the gap\n");
	await execute({ action: "stamp_execution_plan", plan_file: planFile, paths: ["src/never_written.ts"] });
	// subprocessCount stays 0: the first auditor report is NEEDS_FIX.
	out.review = await reviewUntilSettled("claims unverified at baseline");
	out.execAfterReview = JSON.parse(await execute({ action: "get_execution" }));
	out.stopResult = await execute({ action: "stop_execution", body: "already-unmet scenario complete" });
	out.uiCalls = uiCalls;
} else if (scenario === "freeze-probes") {
	const mockUi = {
		confirm: async () => true,
		notify: (msg: string) => uiCalls.push(`freeze-notify:${msg}`),
	};
	const preHead = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();

	// 1. Unsealed pre-session / dirty files present in working tree
	fs.mkdirSync(path.join(probe, "src"), { recursive: true });
	fs.writeFileSync(path.join(probe, "src/unsealed.txt"), "unsealed\n");
	const res1 = await freezeCandidateCommit(mockUi, probe, "OMP-1", "cand-1", [], {
		mode: "execution",
		sealedPaths: ["src/sealed.txt"],
	});
	const headAfter1 = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();
	fs.rmSync(path.join(probe, "src/unsealed.txt"), { force: true });

	// 2. Oversized sealed file (>= 50MB)
	fs.writeFileSync(path.join(probe, "src/big.bin"), "");
	const fd = fs.openSync(path.join(probe, "src/big.bin"), "w");
	fs.ftruncateSync(fd, 50_000_000);
	fs.closeSync(fd);
	const res2 = await freezeCandidateCommit(mockUi, probe, "OMP-1", "cand-1", [], {
		mode: "execution",
		sealedPaths: ["src/big.bin"],
	});
	const headAfter2 = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();
	fs.rmSync(path.join(probe, "src/big.bin"), { force: true });

	// 3. Possible secret in staged diff
	fs.writeFileSync(path.join(probe, "src/secret.txt"), ["const token = '", "sk-", "1234567890abcdef123456';\n"].join(""));
	const res3 = await freezeCandidateCommit(mockUi, probe, "OMP-1", "cand-1", [], {
		mode: "execution",
		sealedPaths: ["src/secret.txt"],
	});
	const headAfter3 = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();
	fs.rmSync(path.join(probe, "src/secret.txt"), { force: true });

	out.res1 = res1;
	out.res2 = res2;
	out.res3 = res3;
	out.headUnchanged = preHead === headAfter1 && preHead === headAfter2 && preHead === headAfter3;
	out.uiCalls = uiCalls;
}
console.log(JSON.stringify(out, null, 2));
