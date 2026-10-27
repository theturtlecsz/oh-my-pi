/**
 * Subprocess-per-case execution remote-ref planning regressions (OMP-233 AC6).
 *
 * `createWorkflowHost` can leave an unfinished continuation reading a fixture
 * while test teardown deletes it (see docs/omp-640-process-lifecycle.md). Each
 * case therefore runs in its own child process of THIS file: the parent owns the
 * fixture root and every child handle/pipe, and removes fixtures only after
 * `OwnedChildren.reap()` has settled both. Children build and remove nothing.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test, vi } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import { executionRemoteRef } from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";
import { OwnedChildren } from "./fixtures/owned-children";

const CASE_ENV = "OMP_EXEC_REMOTE_REF_CASE";
const ROOT_ENV = "OMP_EXEC_REMOTE_REF_ROOT";
const STALL_ENV = "OMP_EXEC_REMOTE_REF_STALL";
// Child test deadline exceeds the parent kill so the parent's bound is the one
// that ends a stalled child, letting the parent own cleanup deterministically.
const CHILD_TEST_TIMEOUT_MS = 25_000;
const PARENT_KILL_MS = 20_000;
const PARENT_CASE_MS = 28_000;
const READY_WAIT_MS = 8_000;
// The delayed probe fails on purpose. Its own child deadline sits far below the
// parent's bound, so the child (not the parent kill) ends the case and its
// timed-out body resumes to touch the fixture — the continuation the parent
// must outlive before it may remove that fixture.
const DELAYED_CASE = "delayed-fixture-touch";
const DELAYED_CHILD_TIMEOUT_MS = 1_500;
// Generous headroom over the child deadline so parallel gate load cannot make
// the parent kill the child before its own deadline resumes the continuation.
const DELAYED_PARENT_KILL_MS = 12_000;

const GRANT_ID = "00000000-0000-7000-8000-000000000237";
const ANCHOR_KEY = "OMP-237";
const CANONICAL_REF = executionRemoteRef(ANCHOR_KEY, GRANT_ID);
const ALIAS_REF = "refs/heads/execution/omp-238";
const WORK_UUID = "00000000-0000-7000-8000-000000000010";

type ToolResult = { content: Array<{ type: "text"; text: string }>; details: { success: boolean } };
type ToolHandler = (
	id: string,
	params: { action: string; work?: string; criteria?: string[]; plan_file?: string; paths?: string[] },
	signal: AbortSignal,
	onUpdate: undefined,
	ctx: ExtensionContext,
) => Promise<ToolResult>;
type CommandHandler = (args: string, ctx: ExtensionContext) => Promise<void>;

/** Park this case's process until the parent reaps it; publishes a ready marker. */
async function stall(dir: string, phase: string): Promise<never> {
	await fs.promises.writeFile(path.join(dir, "ready"), phase);
	// Readable on the owned pipe after reap — proves pipes settled before removal.
	console.log(`stall:${phase}`);
	await Bun.sleep(60_000);
	throw new Error("stall did not end");
}

/**
 * Fails on purpose (DELAYED_CASE). A bounded synchronous wait outlasts the case
 * deadline, so Bun kills that dangling child, marks the case timed out, and the
 * timed-out body resumes to touch `plan.md`. The parent must surface the timeout
 * and this child's own stderr, reap the process and settle its pipes, and only
 * then remove the fixture whose late touch proves the continuation ran.
 */
async function delayedFixtureTouch(directory: string): Promise<void> {
	const planPath = path.join(directory, "plan.md");
	await fs.promises.mkdir(path.join(directory, "src"), { recursive: true });
	await fs.promises.writeFile(
		planPath,
		"## Approach\n1. Write the change\n\n## Verification\n1. Run the focused test\n",
	);
	await fs.promises.writeFile(path.join(directory, "ready"), DELAYED_CASE);
	console.error("delayed:fixture-deferred");
	Bun.spawnSync([process.execPath, "-e", "Bun.sleepSync(3_000)"], { timeout: DELAYED_CHILD_TIMEOUT_MS * 2 });
	// Synchronous so the resumed continuation lands the touch before any process
	// exit can race it: an async write loses to Bun tearing the runner down.
	fs.appendFileSync(planPath, "late-touch\n");
	console.error("delayed:plan-touched");
}

function installDiscoverAgentsMock(): void {
	vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
		agents: [
			{
				name: "auditor",
				description: "Auditor agent",
				systemPrompt: "Audit prompt",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "bundled",
			},
		],
		projectAgentsDir: null,
	});
}

async function makeHarness(
	directory: string,
	options: {
		remoteRef: string | undefined;
		state?: ExecutionSnapshot["grant"]["state"];
		workId?: string;
		positionZeroWorkId?: string;
		phase?: ExecutionGrantItemView["phase"];
		findIssue?: WorkflowBackend["findIssue"];
		workspaceBranch?: (key: string, grantId: string) => string;
	},
) {
	await fs.promises.mkdir(path.join(directory, "src"), { recursive: true });
	const planPath = path.join(directory, "plan.md");
	await fs.promises.writeFile(
		planPath,
		"## Approach\n1. Write the change\n\n## Verification\n1. Run the focused test\n",
	);
	// A "setup" stall stops here, after the plan fixture exists: the parent must
	// see that fixture survive the child and remove it only after reaping.
	if (process.env[STALL_ENV] === "setup") await stall(directory, "setup");
	const workId = options.workId ?? ANCHOR_KEY;
	const makeItem = (
		position: number,
		itemWorkId: string,
		phase: ExecutionGrantItemView["phase"],
	): ExecutionGrantItemView => ({
		item_id: `item-${position}`,
		workspace_id: "00000000-0000-7000-8000-000000000001",
		grant_id: GRANT_ID,
		work_id: itemWorkId,
		position,
		phase,
		claimed_revision_id: "00000000-0000-7000-8000-000000000020",
		initial_git_baseline: "1".repeat(40),
		original_request: "Ship the change",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
	});
	const items: ExecutionGrantItemView[] = options.positionZeroWorkId
		? [makeItem(0, options.positionZeroWorkId, "completed"), makeItem(1, workId, options.phase ?? "criteria_pending")]
		: [makeItem(0, workId, options.phase ?? "criteria_pending")];
	const item = items.at(-1)!;
	const execution: ExecutionSnapshot = {
		grant: {
			grant_id: GRANT_ID,
			workspace_id: "00000000-0000-7000-8000-000000000001",
			owner_id: "00000000-0000-7000-8000-000000000002",
			repository: directory,
			remote_ref: options.remoteRef,
			state: options.state ?? "active",
			mode: options.positionZeroWorkId ? "queue" : "single",
			grant_version: 1,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 0,
			authorization_hash: "3".repeat(64),
			judge_sha256: "4".repeat(64),
			created_at: "2026-09-26T00:00:00Z",
			expires_at: "2026-09-27T00:00:00Z",
		},
		items,
		activeItem: item,
	};
	const sealExecutionCriteria = vi.fn(async () => ({
		sealedCriteria: ["AC-1"],
		grant: structuredClone(execution.grant),
		items: structuredClone(execution.items),
		activeItem: structuredClone(execution.activeItem),
	}));
	const stampExecutionPlan = vi.fn(async () => structuredClone(execution));
	const healthReady = vi.fn(async () => ({ ready: true, service_fingerprint: "5".repeat(64) }));
	const setExecutionState = vi.fn(
		async (input: { targetState: "active" | "paused" | "stopped" | "canceled" | "completed" }) => {
			execution.grant.state = input.targetState;
			execution.grant.grant_version += 1;
			return structuredClone(execution);
		},
	);
	const backend = {
		name: "work",
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(directory, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		getExecution: async () => structuredClone(execution),
		findIssue:
			options.findIssue ??
			(async () => {
				throw new Error("findIssue should not run for a key-shaped position-0 work id");
			}),
		executionChildren: async () => ({ umbrella: false, children: [] }),
		pendingDeliveries: async () => [],
		sealExecutionCriteria,
		stampExecutionPlan,
		setExecutionState,
		workClient: { healthReady },
	} as unknown as WorkflowBackend;
	const ensureCalls: Array<{ key: string; grantId: string }> = [];
	const newSessionCalls: string[] = [];
	const notifications: string[] = [];
	let executeTool: ToolHandler | undefined;
	const commands = new Map<string, CommandHandler>();
	const pi = {
		zod: z,
		registerTool: (definition: { execute: ToolHandler }) => {
			executeTool = definition.execute;
		},
		registerCommand: (name: string, definition: { handler: CommandHandler }) => {
			commands.set(name, definition.handler);
		},
		registerFlag: () => {},
		registerMessageRenderer: () => {},
		on: () => {},
		appendEntry: () => {},
		sendMessage: () => {},
		getSessionId: () => "session-1",
		logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
	} as unknown as ExtensionAPI;
	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: () => true,
		executionWorkspaceManager: {
			primaryRoot: async cwd => cwd,
			ensure: async (_cwd, key, grantId, baseline) => {
				ensureCalls.push({ key, grantId });
				const branch = options.workspaceBranch
					? options.workspaceBranch(key, grantId)
					: executionRemoteRef(key, grantId).slice("refs/heads/".length);
				return { primaryRoot: directory, path: directory, branch, grantId, baseline, reused: true };
			},
			cleanup: async () => ({ cleaned: true, detail: "fixture workspace" }),
		},
	})(pi);
	const context = {
		cwd: directory,
		taskDepth: 0,
		sessionManager: { getCwd: () => directory, getSessionId: () => "session-1", getBranch: () => [] },
		newSession: async () => {
			newSessionCalls.push("newSession");
			return { cancelled: false };
		},
		models: {
			resolve: (role: string) =>
				role === "@audit" ? ({ id: "audit-model", provider: "mock" } as never) : undefined,
		},
		ui: {
			notify: (text: string) => {
				notifications.push(text);
			},
			setStatus: () => {},
			theme: { fg: (_color: string, text: string) => text },
		},
	} as unknown as ExtensionContext;
	const stallAt = async (phase: string): Promise<void> => {
		if (process.env[STALL_ENV] === phase) await stall(directory, phase);
	};
	const call = async (action: "seal_execution_criteria" | "stamp_execution_plan", work = ANCHOR_KEY) => {
		await stallAt("call");
		if (!executeTool) throw new Error("work tool missing");
		return executeTool(
			"planning-1",
			{ action, work, criteria: ["AC-1"], plan_file: planPath, paths: ["src/feat.ts"] },
			new AbortController().signal,
			undefined,
			context,
		);
	};
	const resume = async (key?: string) => {
		await stallAt("resume");
		const handler = commands.get("execute");
		if (!handler) throw new Error("execute command missing");
		await handler(key ? `resume ${key}` : "resume", context);
	};
	return {
		execution,
		item,
		planPath,
		directory,
		sealExecutionCriteria,
		stampExecutionPlan,
		healthReady,
		setExecutionState,
		ensureCalls,
		newSessionCalls,
		notifications,
		call,
		resume,
	};
}

const CASES: Record<string, (directory: string) => Promise<void>> = {
	"canonical-ref-seals-and-stamps": async directory => {
		const harness = await makeHarness(directory, { remoteRef: CANONICAL_REF });
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(true);
		expect(sealed.content[0].text).toContain("criteria sealed successfully");
		expect(harness.sealExecutionCriteria).toHaveBeenCalledTimes(1);

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(true);
		expect(stamped.content[0].text).toContain("plan stamped successfully");
		expect(harness.stampExecutionPlan).toHaveBeenCalledTimes(1);
	},

	"uuid-position-zero-resolves-through-findissue": async directory => {
		const findIssue = vi.fn(async () => ({ id: WORK_UUID, key: ANCHOR_KEY, title: "Anchor" }));
		const harness = await makeHarness(directory, { remoteRef: CANONICAL_REF, workId: WORK_UUID, findIssue });
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(true);
		expect(findIssue).toHaveBeenCalledWith(WORK_UUID);
		expect(harness.sealExecutionCriteria).toHaveBeenCalledTimes(1);
	},

	"alias-ref-on-anchor-grant-refuses-seal-and-stamp": async directory => {
		const harness = await makeHarness(directory, { remoteRef: ALIAS_REF, phase: "criteria_pending" });
		const read = vi.spyOn(fs, "readFileSync");
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(false);
		expect(sealed.content[0].text).toContain(ALIAS_REF);
		expect(sealed.content[0].text).toContain(CANONICAL_REF);
		expect(harness.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(false);
		expect(stamped.content[0].text).toContain(ALIAS_REF);
		expect(stamped.content[0].text).toContain(CANONICAL_REF);
		expect(harness.stampExecutionPlan).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();
		expect(read.mock.calls.some(call => call[0] === harness.planPath)).toBe(false);
	},

	"paused-and-terminal-refusals-precede-mismatched-ref": async directory => {
		const paused = await makeHarness(directory, { remoteRef: ALIAS_REF, state: "paused", phase: "criteria_pending" });
		const pausedResult = await paused.call("seal_execution_criteria");
		expect(pausedResult.details.success).toBe(false);
		expect(pausedResult.content[0].text).toContain("execution grant is paused");
		expect(pausedResult.content[0].text).toContain("/execute resume OMP-237");
		expect(pausedResult.content[0].text).not.toContain("execution remote ref refusal");
		expect(paused.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(paused.stampExecutionPlan).not.toHaveBeenCalled();

		const stopped = await makeHarness(directory, { remoteRef: ALIAS_REF, state: "stopped", phase: "planning" });
		const stoppedResult = await stopped.call("stamp_execution_plan");
		expect(stoppedResult.details.success).toBe(false);
		expect(stoppedResult.content[0].text).toContain("execution grant is stopped");
		expect(stoppedResult.content[0].text).not.toContain("execution remote ref refusal");
		expect(stopped.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(stopped.stampExecutionPlan).not.toHaveBeenCalled();
	},

	"queue-position-zero-canonical-ref-accepted": async directory => {
		const positionZeroRef = executionRemoteRef("OMP-236", GRANT_ID);
		const harness = await makeHarness(directory, {
			remoteRef: positionZeroRef,
			positionZeroWorkId: "OMP-236",
			phase: "criteria_pending",
		});
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(true);
		expect(sealed.content[0].text).toContain("criteria sealed successfully");
		expect(harness.sealExecutionCriteria).toHaveBeenCalledTimes(1);

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(true);
		expect(harness.stampExecutionPlan).toHaveBeenCalledTimes(1);
	},

	"resume-alias-ref-refuses-before-ensure-or-relocation": async directory => {
		const harness = await makeHarness(directory, { remoteRef: ALIAS_REF, state: "paused", phase: "executing" });
		await harness.resume(ANCHOR_KEY);
		const refusal = harness.notifications.at(-1) ?? "";
		expect(refusal).toContain("Cannot resume");
		expect(refusal).toContain(ALIAS_REF);
		expect(refusal).toContain(CANONICAL_REF);
		expect(harness.ensureCalls).toEqual([]);
		expect(harness.newSessionCalls).toEqual([]);
		expect(harness.setExecutionState).not.toHaveBeenCalled();
	},

	"resume-branch-mismatch-refuses-without-relocating": async directory => {
		const harness = await makeHarness(directory, {
			remoteRef: CANONICAL_REF,
			state: "paused",
			phase: "executing",
			workspaceBranch: () => "execution/omp-238",
		});
		await harness.resume(ANCHOR_KEY);
		const refusal = harness.notifications.at(-1) ?? "";
		expect(refusal).toContain("Cannot resume");
		expect(refusal).toContain("refs/heads/execution/omp-238");
		expect(refusal).toContain(CANONICAL_REF);
		expect(harness.ensureCalls).toHaveLength(1);
		expect(harness.newSessionCalls).toEqual([]);
		expect(harness.setExecutionState).not.toHaveBeenCalled();
	},

	"resume-terminal-grant-reports-state-and-execute-key": async directory => {
		const harness = await makeHarness(directory, { remoteRef: CANONICAL_REF, state: "stopped", phase: "executing" });
		await harness.resume(ANCHOR_KEY);
		const refusal = harness.notifications.at(-1) ?? "";
		expect(refusal).toContain("grant state is stopped");
		expect(refusal).toContain("/execute OMP-237");
		expect(refusal).not.toContain("execution remote ref refusal");
		expect(harness.ensureCalls).toEqual([]);
		expect(harness.newSessionCalls).toEqual([]);
	},
};

// The four non-canonical refusals share one assertion body over distinct inputs.
for (const [name, recordedRef, marker] of [
	[
		"prior-grant-qualified-ref",
		executionRemoteRef(ANCHOR_KEY, "00000000-0000-7000-8000-000000000001"),
		executionRemoteRef(ANCHOR_KEY, "00000000-0000-7000-8000-000000000001"),
	],
	["unqualified-legacy-same-key-ref", "refs/heads/execution/omp-237", "refs/heads/execution/omp-237"],
	["refs-heads-main", "refs/heads/main", "refs/heads/main"],
	["missing-ref", undefined, "undefined"],
] as const) {
	CASES[name] = async directory => {
		const harness = await makeHarness(directory, { remoteRef: recordedRef, phase: "criteria_pending" });
		const read = vi.spyOn(fs, "readFileSync");
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(false);
		expect(sealed.content[0].text).toContain(marker);
		expect(sealed.content[0].text).toContain(CANONICAL_REF);
		expect(harness.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();
		expect(read.mock.calls.some(call => call[0] === harness.planPath)).toBe(false);

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(false);
		expect(stamped.content[0].text).toContain(marker);
		expect(stamped.content[0].text).toContain(CANONICAL_REF);
		expect(harness.stampExecutionPlan).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();
	};
}

const caseName = process.env[CASE_ENV];

if (caseName !== undefined) {
	// Child mode: exactly one case, no cleanup hooks. The parent owns the fixture
	// root and every child handle, and removes fixtures after reaping this process.
	const delayed = caseName === DELAYED_CASE;
	test(
		`case ${caseName}`,
		async () => {
			const directory = process.env[ROOT_ENV];
			if (!directory) throw new Error(`${ROOT_ENV} is required in child mode`);
			if (delayed) {
				await delayedFixtureTouch(directory);
				return;
			}
			installDiscoverAgentsMock();
			const run = CASES[caseName];
			if (!run) throw new Error(`unknown case ${caseName}`);
			await run(directory);
		},
		delayed ? DELAYED_CHILD_TIMEOUT_MS : CHILD_TEST_TIMEOUT_MS,
	);
} else {
	const repoRoot = path.resolve(import.meta.dir, "../..");
	const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "execution-remote-ref-planning-"));
	const owned = new OwnedChildren();

	// Parent owns the root: reap every handle and settle its pipes BEFORE the
	// fixture tree is removed, so no unfinished child continuation can race it.
	afterAll(async () => {
		await owned.reap();
		await fs.promises.rm(fixtureRoot, { recursive: true, force: true });
	});

	async function spawnCase(
		name: string,
		stallPhase?: string,
		childTimeoutMs = CHILD_TEST_TIMEOUT_MS,
		killMs = PARENT_KILL_MS,
	) {
		const directory = path.join(fixtureRoot, stallPhase ? `${name}-${stallPhase}` : name);
		await fs.promises.mkdir(directory, { recursive: true });
		const child = owned.spawn([process.execPath, "test", "--timeout", String(childTimeoutMs), import.meta.path], {
			cwd: repoRoot,
			env: {
				...process.env,
				[CASE_ENV]: name,
				[ROOT_ENV]: directory,
				...(stallPhase ? { [STALL_ENV]: stallPhase } : {}),
			},
			stdout: "pipe",
			stderr: "pipe",
			timeout: killMs,
			killSignal: "SIGKILL",
		});
		return { child, directory };
	}

	async function waitForMarker(marker: string): Promise<void> {
		const deadline = Date.now() + READY_WAIT_MS;
		while (true) {
			try {
				await fs.promises.access(marker);
				return;
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
			}
			if (Date.now() >= deadline) throw new Error(`child never wrote ${marker}`);
			await Bun.sleep(10);
		}
	}

	describe("execution remote ref planning (OMP-233 AC6), one subprocess per case", () => {
		for (const name of Object.keys(CASES)) {
			test(
				name,
				async () => {
					const { child } = await spawnCase(name);
					const output = owned.output(child);
					const [exit, pipes] = await Promise.all([child.exited, output]);
					// Surface the child's own stderr/stdout, never just the exit code.
					const report = `${pipes.stdout}\n${pipes.stderr}`;
					expect(exit, report).toBe(0);
					expect(report).toMatch(/\b1 pass\b/);
					expect(report).toMatch(/\b0 fail\b/);
				},
				PARENT_CASE_MS,
			);
		}

		for (const [phase, name] of [
			["setup", "canonical-ref-seals-and-stamps"],
			["call", "canonical-ref-seals-and-stamps"],
			["resume", "resume-alias-ref-refuses-before-ensure-or-relocation"],
		] as const) {
			test(
				`stalled ${phase} child: fixtures outlive it and pipes settle before removal`,
				async () => {
					const { child, directory } = await spawnCase(name, phase);
					const output = owned.output(child);
					await waitForMarker(path.join(directory, "ready"));
					expect(child.exitCode, "child must still be alive at the ready marker").toBeNull();
					expect(fs.existsSync(path.join(directory, "plan.md"))).toBe(true);

					await owned.reap();

					// The fixture survived the child (the parent removes it, not the child)…
					expect(fs.existsSync(directory)).toBe(true);
					// …and its pipe settled into readable output before removal.
					const pipes = await output;
					expect(pipes.stdout).toContain(`stall:${phase}`);
					await fs.promises.rm(directory, { recursive: true, force: true });
				},
				PARENT_CASE_MS,
			);
		}

		test(
			"delayed case reports its timeout, reaps before removal, and leaves a clean runner",
			async () => {
				// The expected probe failure stays inside this test: the child is SUPPOSED
				// to fail, and the parent asserts that failure rather than inheriting it.
				const { child, directory } = await spawnCase(
					DELAYED_CASE,
					undefined,
					DELAYED_CHILD_TIMEOUT_MS,
					DELAYED_PARENT_KILL_MS,
				);
				const output = owned.output(child);
				const planPath = path.join(directory, "plan.md");
				await waitForMarker(path.join(directory, "ready"));

				// Bound the child's own deadline, not the parent kill, so its timed-out
				// body resumes inside the child before that process exits.
				const exit = await child.exited;
				const report = `${(await output).stdout}\n${(await output).stderr}`;
				expect(exit, report).not.toBe(0);
				expect(child.signalCode, "child must end on its own deadline, not the parent kill").toBeNull();
				// Runner reports the timeout and the child's OWN stderr markers.
				expect(report).toMatch(/timed out after \d+ms/);
				expect(report).toContain("delayed:fixture-deferred");
				expect(report).toContain("delayed:plan-touched");
				expect(report).toMatch(/\b0 pass\b/);
				expect(report).toMatch(/\b1 fail\b/);
				// The continuation settled inside the child: its late touch is on disk
				// BEFORE the parent reaps or removes anything.
				expect(await Bun.file(planPath).text()).toContain("late-touch");

				// No live child or unhandled continuation survives the reap.
				expect(child.exitCode).not.toBeNull();
				expect(() => process.kill(child.pid!, 0)).toThrow();
				await owned.reap();

				// The parent owns removal, after reaping: the fixture is still here and
				// its pipe is readable through the owned readers.
				expect(fs.existsSync(directory)).toBe(true);
				expect((await output).stderr).toContain("delayed:plan-touched");
				await fs.promises.rm(directory, { recursive: true, force: true });

				// A subsequent normal case still passes with fresh mocks and fixtures.
				const next = await spawnCase("canonical-ref-seals-and-stamps");
				const [nextExit, nextPipes] = await Promise.all([next.child.exited, owned.output(next.child)]);
				const nextReport = `${nextPipes.stdout}\n${nextPipes.stderr}`;
				expect(nextExit, nextReport).toBe(0);
				expect(nextReport).toMatch(/\b1 pass\b/);
				expect(nextReport).toMatch(/\b0 fail\b/);
			},
			PARENT_CASE_MS * 2,
		);
	});
}
