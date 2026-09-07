// HOME-131/OMP-168 model bookends: /intake auto-routing to the intake role.
// OMP-168 removed model-transported audit gates: audits run natively through
// work run_audit, so /summary, before_agent_start, auditor task calls, and
// task results receive zero interception from this extension.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";
import * as auditorRunnerModule from "../extensions/workflow/auditor-runner";
import * as gitModule from "../extensions/workflow/git";
import { describe, expect, spyOn, test } from "bun:test";
import { type ExtensionAPI, type ExtensionContext, ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { z } from "zod";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost, syncExecutionPhaseModel } from "../extensions/workflow/host";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/model-bookends.ts");

interface Harness {
	runner: ExtensionRunner;
	setModelCalls: Array<{ provider: string; id: string }>;
	thinkingLevels: string[];
	notifies: string[];
}

async function makeHarness(depth = 0, opts: { intakeConfigured?: boolean; hasCredential?: boolean } = {}): Promise<Harness> {
	const { intakeConfigured = true, hasCredential = true } = opts;
	const result = await loadExtensions([extPath], repoRoot);
	if (result.errors.length > 0) throw new Error(result.errors.map(e => e.error).join("; "));
	const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
	const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
	const fakeRegistry = {
		getAvailable: () => (intakeConfigured ? [fableModel, gptModel] : [gptModel]),
		find: (p: string, id: string) => (p === "anthropic" && id === "claude-fable-5" ? fableModel : id === "gpt-5.2" ? gptModel : undefined),
		hasProvider: () => true,
		resolver: () => () => undefined,
	};
	const fakeSettings = {
		getModelRole: (role: string) => (role === "intake" && intakeConfigured ? "anthropic/claude-fable-5" : undefined),
		getModelRoles: () => (intakeConfigured ? { intake: "anthropic/claude-fable-5" } : {}),
		get: (key: string) => (key === "thinkingLevel" ? "medium" : undefined),
		getStorage: () => undefined,
	};
	const runner = new ExtensionRunner(
		result.extensions,
		result.runtime,
		repoRoot,
		{ getCwd: () => repoRoot, getBranch: () => [], getSessionId: () => "session-test" } as never,
		fakeRegistry as never,
		undefined,
		fakeSettings as never,
		undefined,
		undefined,
		depth,
	);
	const setModelCalls: Array<{ provider: string; id: string }> = [];
	const thinkingLevels: string[] = [];
	const notifies: string[] = [];
	runner.initialize(
		{
			setModel: async (model: { provider: string; id: string }) => {
				if (!hasCredential) return false;
				setModelCalls.push({ provider: model.provider, id: model.id });
				return true;
			},
			setThinkingLevel: (level: string) => {
				thinkingLevels.push(level);
			},
		} as never,
		{
			getModel: () => fableModel,
			isIdle: () => true,
			abort: () => {},
		} as never,
		undefined,
		{
			notify: (msg: string) => {
				notifies.push(msg);
			},
		} as never,
	);
	await runner.emit({ type: "session_start" } as never);
	return { runner, setModelCalls, thinkingLevels, notifies };
}

const taskCall = (id: string, input: Record<string, unknown>) =>
	({ type: "tool_call", toolName: "task", toolCallId: id, input }) as never;
const auditorCall = (id: string, task: unknown = "sealed body") =>
	taskCall(id, { context: "audit the completed work", tasks: [{ agent: "auditor", task }] });

describe("/intake routing (HOME-131)", () => {
	test("owner /intake switches to the intake role at :high and forwards", async () => {
		const h = await makeHarness();
		const result = (await h.runner.emitInput("/intake plan the thing", undefined, "interactive")) as { text?: string } | undefined;
		expect(h.setModelCalls).toEqual([{ provider: "anthropic", id: "claude-fable-5" }]);
		expect(h.thinkingLevels).toEqual(["high"]);
		expect(result?.text).toBe("/skill:intake plan the thing");
	});
	test("unresolvable intake role fails closed", async () => {
		const h = await makeHarness(0, { intakeConfigured: false });
		const result = (await h.runner.emitInput("/intake x", undefined, "interactive")) as { handled?: boolean } | undefined;
		expect(result?.handled).toBe(true);
		expect(h.notifies.some(n => n.includes("could not resolve @intake"))).toBe(true);
	});
	test("missing credential fails closed without forwarding", async () => {
		const h = await makeHarness(0, { hasCredential: false });
		const result = (await h.runner.emitInput("/intake x", undefined, "interactive")) as { handled?: boolean } | undefined;
		expect(result?.handled).toBe(true);
		expect(h.notifies.some(n => n.includes("no credential"))).toBe(true);
	});
	test("subagent /intake is ignored", async () => {
		const h = await makeHarness(1);
		const result = await h.runner.emitInput("/intake x", undefined, "interactive");
		expect(result?.text).toBeUndefined();
		expect(result?.handled).toBeUndefined();
		expect(h.setModelCalls).toHaveLength(0);
	});
});

describe("audit cutover (OMP-168: no model-transport interception)", () => {
	test("/summary and before_agent_start inject no audit contract", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const startResult = await h.runner.emitBeforeAgentStart("x", undefined, []);
		expect(startResult).toBeUndefined();
	});

	test("task calls with agent:auditor pass through untouched", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const callResult = await h.runner.emitToolCall(auditorCall("t-1"));
		expect(callResult).toBeUndefined();
	});

	test("tool results for auditor tasks receive no ledger settlement interception", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const resResult = await h.runner.emitToolResult({
			type: "tool_result",
			toolName: "task",
			toolCallId: "t-1",
			input: {},
			content: [{ type: "text", text: "VERDICT: PASS" }],
			details: { results: [{ output: "VERDICT: PASS" }] },
			isError: false,
		} as never);
		expect(resResult).toBeUndefined();
	});
});

describe("intake doctrine contract (OMP-247)", () => {
	test("requires a native delivery->activation blocks edge for post-merge-only live probes", async () => {
		const skillPath = path.join(repoRoot, "session-system/skills/intake/SKILL.md");
		const content = await Bun.file(skillPath).text();
		expect(content).toContain("Post-merge live activation: any criterion that can only be observed after candidate merge plus install/restart/reload must be published as a blocked activation child");
		expect(content).toContain("delivery item owns repository changes, pre-merge tests/audit, and merge, while the activation child owns deploy/restart and live probes and is blocked by delivery through a native blocks edge");
		expect(content).toContain("/execute delivery criteria must never require the currently running process to expose candidate code");
		expect(content).toContain("Post-merge live probes: a blueprint with criteria that can only be observed after candidate merge plus install/restart/reload is invalid as one delivery item unless it publishes the linked delivery→activation batch");
	});
});

describe("/execute phase/model routing (OMP-241)", () => {
	const solModel = { id: "gpt-5.6-sol", provider: "openai-codex", name: "GPT 5.6 SOL", api: "openai-responses" };
	const geminiModel = { id: "gemini-3.7-flash", provider: "google-antigravity", name: "Gemini 3.7 Flash", api: "google-gemini" };

	function makeModelContext(roles: Record<string, typeof solModel | typeof geminiModel>) {
		return {
			models: {
				resolve: (role: string) => roles[role],
				list: () => Object.values(roles),
				current: () => undefined,
				family: () => "test-family",
			},
		} as never;
	}

	test("planning, criteria, and remediation phases route to SOL (@plan / @slow)", async () => {
		const setCalls: Array<{ provider: string; id: string }> = [];
		const fakePi = {
			setModel: async (m: { provider: string; id: string }) => {
				setCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
		} as never;
		const ctx = makeModelContext({ "@plan": solModel, "@task": geminiModel });

		for (const phase of ["criteria_pending", "planning", "remediating"]) {
			setCalls.length = 0;
			const res = await syncExecutionPhaseModel(phase, ctx, fakePi);
			expect(res.ok).toBe(true);
			expect(res.role).toBe("@plan");
			expect(setCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);
		}
	});

	test("executing and reviewing phases route to Gemini 3.7 Flash (@task / @smol)", async () => {
		const setCalls: Array<{ provider: string; id: string }> = [];
		const fakePi = {
			setModel: async (m: { provider: string; id: string }) => {
				setCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
		} as never;
		const ctx = makeModelContext({ "@plan": solModel, "@task": geminiModel });

		for (const phase of ["executing", "reviewing"]) {
			setCalls.length = 0;
			const res = await syncExecutionPhaseModel(phase, ctx, fakePi);
			expect(res.ok).toBe(true);
			expect(res.role).toBe("@task");
			expect(setCalls).toEqual([{ provider: "google-antigravity", id: "gemini-3.7-flash" }]);
		}
	});

	test("genuinely complex work during executing phase retains SOL (@plan / @slow)", async () => {
		const setCalls: Array<{ provider: string; id: string }> = [];
		const fakePi = {
			setModel: async (m: { provider: string; id: string }) => {
				setCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
		} as never;
		const ctx = makeModelContext({ "@plan": solModel, "@task": geminiModel });

		const res = await syncExecutionPhaseModel("executing", ctx, fakePi, { complex: true });
		expect(res.ok).toBe(true);
		expect(res.role).toBe("@plan");
		expect(setCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);
	});

	test("falls back to @slow for planning and @smol/@default for executing when primary role is absent", async () => {
		const setCalls: Array<{ provider: string; id: string }> = [];
		const fakePi = {
			setModel: async (m: { provider: string; id: string }) => {
				setCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
		} as never;
		const ctx = makeModelContext({ "@slow": solModel, "@smol": geminiModel });

		const planRes = await syncExecutionPhaseModel("planning", ctx, fakePi);
		expect(planRes.role).toBe("@slow");
		expect(setCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);

		setCalls.length = 0;
		const execRes = await syncExecutionPhaseModel("executing", ctx, fakePi);
		expect(execRes.role).toBe("@smol");
		expect(setCalls).toEqual([{ provider: "google-antigravity", id: "gemini-3.7-flash" }]);
	});
	test("unresolved model role fails closed with no_model error", async () => {
		const fakePi = {
			setModel: async () => true,
		} as never;
		const emptyCtx = makeModelContext({});

		const planRes = await syncExecutionPhaseModel("planning", emptyCtx, fakePi);
		expect(planRes.ok).toBe(false);
		expect(planRes.error).toBe("no_model");
		expect(planRes.reason).toContain("could not resolve model for @plan");

		const execRes = await syncExecutionPhaseModel("executing", emptyCtx, fakePi);
		expect(execRes.ok).toBe(false);
		expect(execRes.error).toBe("no_model");
		expect(execRes.reason).toContain("could not resolve model for @task");
	});

	test("setModel failure (missing credentials) fails closed with no_credential error", async () => {
		const fakePi = {
			setModel: async () => false,
		} as never;
		const ctx = makeModelContext({ "@plan": solModel, "@task": geminiModel });

		const planRes = await syncExecutionPhaseModel("planning", ctx, fakePi);
		expect(planRes.ok).toBe(false);
		expect(planRes.error).toBe("no_credential");
		expect(planRes.reason).toContain("no credential for openai-codex/gpt-5.6-sol");

		const execRes = await syncExecutionPhaseModel("executing", ctx, fakePi);
		expect(execRes.ok).toBe(false);
		expect(execRes.error).toBe("no_credential");
		expect(execRes.reason).toContain("no credential for google-antigravity/gemini-3.7-flash");
	});

	test("stamp_execution_plan refuses fail-closed without advancing when model switch fails", async () => {
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		let stampCalled = false;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			setModel: async () => false, // credentials fail
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = {
			grant: { grant_id: "grant-1", state: "active", grant_version: 1 },
			items: [{ position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 }],
			activeItem: { position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 },
		};

		const mockBackend = {
			cacheFile: "cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-241", key: "OMP-241", title: "Test", project: "OMP" }),
			getExecution: async () => exec,
			stampExecutionPlan: async () => {
				stampCalled = true;
				return exec;
			},
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "model-routing-test-"));
		fs.mkdirSync(path.join(testDir, "src"), { recursive: true });
		const planPath = path.join(testDir, "plan.md");
		fs.writeFileSync(planPath, "## Approach\n1. Do thing\n\n## Verification\n1. Test thing\n");
		const fakeCtx = {
			cwd: testDir,
			taskDepth: 0,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			models: {
				resolve: (role: string) => (role === "@task" ? geminiModel : undefined),
				list: () => [geminiModel],
				current: () => undefined,
				family: () => "test-family",
			},
		} as unknown as ExtensionContext;

		const result = await registeredExecute!(
			"call-1",
			{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/index.ts"] },
			new AbortController().signal,
			undefined,
			fakeCtx,
		);

		expect(result.content[0].text).toContain("stamp_execution_plan refused: no credential for google-antigravity/gemini-3.7-flash");
		expect(stampCalled).toBe(false);
		fs.rmSync(testDir, { recursive: true, force: true });
	});
	test("begin_execution_review denies fail-closed when remediation model switch fails", async () => {
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			setModel: async () => false, // model switch fails
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = {
			grant: { grant_id: "grant-1", state: "active", grant_version: 1 },
			items: [{ position: 0, work_id: "OMP-241", phase: "executing", close_attempts_started: 0, plan_stamp: { candidate_id: "cand-1", paths: ["src/index.ts"] } }],
			activeItem: { position: 0, work_id: "OMP-241", phase: "executing", close_attempts_started: 0, plan_stamp: { candidate_id: "cand-1", paths: ["src/index.ts"] } },
		};

		const mockBackend = {
			cacheFile: "cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			issueDetail: async () => ({
				key: "OMP-241",
				attemptSnapshot: { attemptId: "att-1", state: "audit_ready", candidateCommit: "commit-1", hasManifest: true },
			}),
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "audit this" }),
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-241", key: "OMP-241", title: "Test", project: "OMP" }),
			finalizeExecutionCandidate: async () => ({}),
			getExecution: async () => exec,
			reserveAuditorLaunch: async () => ({ status: "reserved", launchId: "launch-1" }),
			settleAuditorLaunch: async () => ({ verdict: "NEEDS_FIX", event: { renderedText: "AC-1 failed" } }),
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({
					close_attempts: [{ attempt_id: "att-1", revision_id: "rev-1", candidate_id: "cand-1", candidate_sha256: "sha-1", candidate_commit: "commit-1" }],
					item: { current_revision_id: "rev-1", candidate: { candidate_id: "cand-1", candidate_sha256: "sha-1", commit_sha: "commit-1" } },
				}),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "model-routing-review-test-"));
		const fakeCtx = {
			cwd: testDir,
			taskDepth: 0,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			models: {
				resolve: (role: string) => (role === "@plan" ? solModel : undefined),
				list: () => [solModel],
				current: () => undefined,
				family: () => "test-family",
			},
		} as unknown as ExtensionContext;
		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;

		const auditRunnerSpy = spyOn(auditorRunnerModule, "prepareNativeAuditRunner").mockResolvedValue(async () => ({
			started: true,
			payload: "VERDICT: NEEDS_FIX",
		}));
		const dirtySpy = spyOn(gitModule, "dirtyPaths").mockReturnValue([]);

		try {
			const res = await registeredExecute!(
				"call-1",
				{ action: "begin_execution_review", body: "verification", work: "OMP-241" },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(res.content[0].text).toContain("remediation model switch refused: no credential for openai-codex/gpt-5.6-sol");
		} finally {
			auditRunnerSpy.mockRestore();
			dirtySpy.mockRestore();
			fs.rmSync(testDir, { recursive: true, force: true });
		}
	});

	test("/execute start notifies and aborts fail-closed when planning model cannot be resolved", async () => {
		const notifications: Array<{ msg: string; type?: string }> = [];
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		let beginCalled = false;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			setModel: async () => true,
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const mockBackend = {
			cacheFile: "cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-241", key: "OMP-241", title: "Test", project: "OMP" }),
			beginExecution: async () => {
				beginCalled = true;
				return { grant: { grant_id: "grant-1", grant_version: 1 }, items: [] };
			},
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({ work_id: "uuid-241", revision: { revision_id: "rev-1", description: "test request" } }),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "model-routing-start-test-"));
		Bun.spawnSync(["git", "init", "-q", "-b", "main"], { cwd: testDir });
		const headSpy = spyOn(gitModule, "headCommit").mockReturnValue("1".repeat(40));
		const refSpy = spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/main");
		const upToDateSpy = spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		const checksSpy = spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 12, detail: "12 required checks" });
		const gitOpSpy = spyOn(gitModule, "inProgressGitOp").mockReturnValue(false);

		try {
			const fakeCtx = {
				cwd: testDir,
				taskDepth: 0,
				ui: { notify: (msg: string, type?: string) => { notifications.push({ msg, type }); }, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
				models: {
					resolve: () => undefined, // no model resolves
					list: () => [],
					current: () => undefined,
					family: () => "test-family",
				},
			} as unknown as ExtensionContext;

			const execCmd = registeredCommands.get("execute");
			expect(execCmd).toBeDefined();
			await execCmd!("OMP-241", fakeCtx);

			expect(notifications.some(n => n.msg.includes("could not resolve model for @plan"))).toBe(true);
			expect(beginCalled).toBe(false);
		} finally {
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
			gitOpSpy.mockRestore();
			fs.rmSync(testDir, { recursive: true, force: true });
		}
	});
	test("/execute --complex flag retains SOL (@plan) into execution phase", async () => {
		const setModelCalls: Array<{ provider: string; id: string }> = [];
		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
			setModel: async (m: { provider: string; id: string }) => {
				setModelCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
			getSessionId: () => "sess-complex-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = {
			grant: { grant_id: "grant-complex-1", state: "active", grant_version: 1 },
			items: [{ position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 }],
			activeItem: { position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 },
		};

		const mockBackend = {
			cacheFile: "cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-241", key: "OMP-241", title: "Test", project: "OMP" }),
			beginExecution: async () => exec,
			getExecution: async () => exec,
			getFocusVersion: async () => 1,
			stampExecutionPlan: async () => exec,
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({ work_id: "uuid-241", revision: { revision_id: "rev-1", description: "test request" } }),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "model-routing-complex-test-"));
		Bun.spawnSync(["git", "init", "-q", "-b", "main"], { cwd: testDir });
		Bun.spawnSync(["git", "config", "user.name", "Test"], { cwd: testDir });
		Bun.spawnSync(["git", "config", "user.email", "test@example.com"], { cwd: testDir });
		Bun.spawnSync(["git", "commit", "-q", "--allow-empty", "-m", "init"], { cwd: testDir });
		const realHead = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: testDir }).stdout.toString().trim();
		fs.mkdirSync(path.join(testDir, "src"), { recursive: true });
		const planPath = path.join(testDir, "plan.md");
		fs.writeFileSync(planPath, "## Approach\n1. Do complex thing\n\n## Verification\n1. Test complex thing\n");

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager: {
				ensure: async (_c, _k, grantId) => ({ path: testDir, grantId, isWorktree: false, primaryRoot: testDir }),
				primaryRoot: async () => testDir,
				clean: async () => ({ cleaned: true, detail: "cleaned" }),
			},
		})(fakePi);

		const headSpy = spyOn(gitModule, "headCommit").mockReturnValue(realHead);
		const refSpy = spyOn(gitModule, "currentSymbolicRef").mockReturnValue("refs/heads/main");
		const upToDateSpy = spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		const checksSpy = spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({ ok: true, count: 12, detail: "12 required checks" });
		const gitOpSpy = spyOn(gitModule, "inProgressGitOp").mockReturnValue(false);
		const dirtySpy = spyOn(gitModule, "dirtyPaths").mockReturnValue([]);

		try {
			const fakeCtx = {
				cwd: testDir,
				taskDepth: 0,
				ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
				models: {
					resolve: (role: string) => (role === "@plan" ? solModel : role === "@task" ? geminiModel : undefined),
					list: () => [solModel, geminiModel],
					current: () => undefined,
					family: () => "test-family",
				},
			} as unknown as ExtensionContext;

			const execCmd = registeredCommands.get("execute");
			expect(execCmd).toBeDefined();
			await execCmd!("OMP-241 --complex", fakeCtx);
			// Initial start selected SOL (@plan)
			expect(setModelCalls.length).toBeGreaterThan(0);
			expect(setModelCalls.every(c => c.id === "gpt-5.6-sol")).toBe(true);

			// Stamping plan with --complex retains SOL (@plan) into executing phase
			setModelCalls.length = 0;
			await registeredExecute!(
				"call-stamp-complex",
				{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/index.ts"] },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(setModelCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);
		} finally {
			headSpy.mockRestore();
			refSpy.mockRestore();
			upToDateSpy.mockRestore();
			checksSpy.mockRestore();
			gitOpSpy.mockRestore();
			dirtySpy.mockRestore();
			fs.rmSync(testDir, { recursive: true, force: true });
		}
	});


	test("host tool actions trigger model synchronization at phase boundaries", async () => {
		const setModelCalls: Array<{ provider: string; id: string }> = [];
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			setModel: async (m: { provider: string; id: string }) => {
				setModelCalls.push({ provider: m.provider, id: m.id });
				return true;
			},
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = {
			grant: { grant_id: "grant-1", state: "active", grant_version: 1 },
			items: [{ position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 }],
			activeItem: { position: 0, work_id: "OMP-241", phase: "planning", close_attempts_started: 0 },
		};

		const workClientMock = {
			healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
			workflow: async () => ({
				close_attempts: [{ attempt_id: "att-1", revision_id: "rev-1", candidate_id: "cand-1", candidate_sha256: "sha-1", candidate_commit: "commit-1" }],
				item: { current_revision_id: "rev-1", candidate: { candidate_id: "cand-1", candidate_sha256: "sha-1", commit_sha: "commit-1" } },
			}),
		};
		const mockBackend = {
			cacheFile: "cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			issueDetail: async () => ({
				key: "OMP-241",
				attemptSnapshot: { attemptId: "att-1", state: "audit_ready", candidateCommit: "commit-1", hasManifest: true },
			}),
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "audit this" }),
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-241", key: "OMP-241", title: "Test", project: "OMP" }),
			finalizeExecutionCandidate: async () => ({}),
			getExecution: async () => exec,
			stampExecutionPlan: async () => exec,
			sealExecutionCriteria: async () => ({
				sealedCriteria: ["AC-1"],
				grant: exec.grant,
				item: exec.activeItem,
			}),
			settleAuditorLaunch: async () => ({
				verdict: "NEEDS_FIX",
				event: { renderedText: "Fix something" },
			}),
			beginCloseAttempt: async () => ({
				status: "active",
				attempt: { attempt_id: "att-1" },
				candidate: { candidate_id: "cand-1" },
			}),
			sealAuditManifest: async () => ({
				status: "active",
				attempt: { attempt_id: "att-1" },
			}),
			reserveAuditorLaunch: async () => ({
				status: "reserved",
				launchId: "launch-1",
			}),
			workClient: workClientMock,
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "model-routing-test-"));
		fs.mkdirSync(path.join(testDir, "src"), { recursive: true });
		const planPath = path.join(testDir, "plan.md");
		fs.writeFileSync(planPath, "## Approach\n1. Do thing\n\n## Verification\n1. Test thing\n");
		const fakeCtx = {
			cwd: testDir,
			taskDepth: 0,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
			models: {
				resolve: (role: string) => (role === "@plan" ? solModel : role === "@task" ? geminiModel : undefined),
				list: () => [solModel, geminiModel],
				current: () => undefined,
				family: () => "test-family",
			},
		} as unknown as ExtensionContext;
		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;

		await registeredExecute!(
			"call-1",
			{ action: "stamp_execution_plan", plan_file: planPath, paths: ["src/index.ts"] },
			new AbortController().signal,
			undefined,
			fakeCtx,
		);

		expect(setModelCalls).toEqual([{ provider: "google-antigravity", id: "gemini-3.7-flash" }]);
		fs.rmSync(testDir, { recursive: true, force: true });

		// seal_execution_criteria switches to @plan
		setModelCalls.length = 0;
		exec.activeItem.phase = "criteria_pending";
		await registeredExecute!(
			"call-2",
			{ action: "seal_execution_criteria", criteria: ["AC-1"] },
			new AbortController().signal,
			undefined,
			fakeCtx,
		);
		expect(setModelCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);

		// begin_execution_review finding (NEEDS_FIX) switches to @plan (remediating)
		setModelCalls.length = 0;
		exec.activeItem.phase = "executing";
		const auditRunnerSpy = spyOn(auditorRunnerModule, "prepareNativeAuditRunner").mockResolvedValue(async () => ({
			started: true,
			payload: "VERDICT: NEEDS_FIX",
		}));
		const dirtySpy = spyOn(gitModule, "dirtyPaths").mockReturnValue([]);

		try {
			await registeredExecute!(
				"call-3",
				{ action: "begin_execution_review", body: "verification", work: "OMP-241" },
				new AbortController().signal,
				undefined,
				fakeCtx,
			);
			expect(setModelCalls).toEqual([{ provider: "openai-codex", id: "gpt-5.6-sol" }]);
		} finally {
			auditRunnerSpy.mockRestore();
			dirtySpy.mockRestore();
		}

	});
});
