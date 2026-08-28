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
const scenario = process.argv[3] as "single" | "dirty" | "queue" | "contract-pause" | "tamper-a" | "tamper-b" | "tamper-c" | "tamper-d";
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

vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options: any) => {
	subprocessCount++;
	const report = subprocessCount === 1 ? NEEDS_FIX_REPORT : PASS_REPORT;
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
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };

const uiCalls: string[] = [];
const sentMessages: unknown[] = [];
let modelTurnCount = 0;
const sessionId = `smoke-exec-${scenario}`;
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => [], getSessionId: () => sessionId } as never,
	{ getAvailable: () => [fableModel], hasProvider: () => true } as never,
	undefined,
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

await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();
const cmdCtx = runner.createCommandContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	modelTurnCount++;
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
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
		criteria: ["AC-1 deliver smoke feature"],
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
	const review1 = await execute({
		action: "begin_execution_review",
		body: "Ran test: expected true but got false (NEEDS_FIX)",
	});
	out.review1 = review1;

	// 6. Remediate: fix code, restamp plan, re-review (yields PASS)
	fs.writeFileSync(path.join(probe, "src/smoke_feat.ts"), "export const feat = true;\n");
	fs.writeFileSync(planDiskPath, "## Approach\n1. Write feature\n2. Fix feat to true\n\n## Verification\n1. Check feature\n");
	out.stampResult2 = await execute({
		action: "stamp_execution_plan",
		plan_file: planFile,
		paths: ["src/smoke_feat.ts"],
	});

	const review2 = await execute({
		action: "begin_execution_review",
		body: "Ran test: feat is true, all checks passed",
	});
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
	const reviewQ1 = await execute({ action: "begin_execution_review", body: "test q1 passed" });
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
	const reviewQ2 = await execute({ action: "begin_execution_review", body: "test q2 passed" });
	out.reviewQ2 = reviewQ2;

	out.finalExecution = JSON.parse(await execute({ action: "get_execution" }));
	out.uiCalls = uiCalls;
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
	out.resumeDeniedNotices = uiCalls.filter(c => c.includes("Cannot resume: prospective contract digest"));

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
}

console.log(JSON.stringify(out, null, 2));
