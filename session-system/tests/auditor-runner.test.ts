import * as fs from "node:fs";
import * as os from "node:os";
import { spawnSync } from "node:child_process";
import { WORK_CONTRACT_SHA256, type WorkClient } from "@oh-my-pi/pi-work-client";
import { afterEach, describe, expect, test, vi } from "bun:test";
import * as path from "node:path";
import { z } from "zod";
import { Agent } from "@oh-my-pi/pi-agent-core";
import { type Model, AssistantMessageEventStream } from "@oh-my-pi/pi-ai";
import { AgentSession, SessionManager, Settings, type ExtensionAPI, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import * as executorModule from "@oh-my-pi/pi-coding-agent/task/executor";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { prepareNativeAuditRunner } from "../extensions/workflow/auditor-runner";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";
import type { CloseAttemptSnapshot, ExecutionItemPhase, ExecutionSnapshot } from "../extensions/workflow/backend";
import {
	confirmWrite,
	RECEIPT_TTL_MS,
	resetConfirmations,
} from "../extensions/workflow/confirm";
import * as gitModule from "../extensions/workflow/git";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";
import { headCommit } from "../extensions/workflow/git";
import {
	computeExecutionNoticeDetails,
	renderExecutionTerminalBanner,
	renderNextActionBanner,
	renderSummaryResumeDigest,
	resolveAnchorKey,
} from "../extensions/workflow/host";

describe("native auditor runner (OMP-168)", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	afterEach(() => {
		vi.restoreAllMocks();
	});

	test("prepareNativeAuditRunner fails if @audit role cannot be resolved", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: () => undefined },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("@audit");
	});

	test("prepareNativeAuditRunner returns a runner when preconditions exist", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: (role: string) => (role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);
		expect(typeof runner).toBe("function");
	});

	test("runner returns started:false when cancelled before start", async () => {
		mockDiscovery();
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: (role: string) => (role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);

		const abortController = new AbortController();
		abortController.abort(); // already aborted

		const result = await runner("test task", "attempt-1", abortController.signal);
		expect(result.started).toBe(false);
		expect(result.payload).toBeUndefined();
	});
	test("forwards effective settings to the native auditor subprocess", async () => {
		const sentinelSettings = Settings.isolated({ modelRoles: { audit: "test/auditor" } });
		const settingsSpy = vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(sentinelSettings);

		const sentinelOutputSchema = { properties: { report: { type: "string" } } };
		const fakeAgent: AgentDefinition = {
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			output: sentinelOutputSchema,
			source: "bundled",
		};
		const discoverSpy = mockDiscovery(fakeAgent);
		let capturedOptions: executorModule.ExecutorOptions | undefined;
		const wrappedPayload = JSON.stringify({
			report: "VERDICT: PASS\nAll acceptance criteria verified.",
		});
		const runSubprocessSpy = vi
			.spyOn(executorModule, "runSubprocess")
			.mockImplementation(async (options) => {
				capturedOptions = options;
				return {
					index: options.index,
					id: options.id,
					agent: options.agent.name,
					agentSource: options.agent.source,
					task: options.task,
					exitCode: 0,
					output: wrappedPayload,
					stderr: "",
					truncated: false,
					durationMs: 120,
					tokens: 450,
					requests: 1,
				} as executorModule.SingleResult;
			});

		const sentinelRegistry = { getApiKey: () => Promise.resolve("key") };
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: {
				resolve: (role: string) =>
					role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined,
			},
			modelRegistry: sentinelRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(fakeCtx);
		const result = await runner("Run audit on OMP-173", "attempt-123");

		expect(settingsSpy).toHaveBeenCalledWith({
			cwd: repoRoot,
			agentDir: expect.any(String),
		});
		expect(discoverSpy).toHaveBeenCalledWith(repoRoot);
		expect(runSubprocessSpy).toHaveBeenCalledTimes(1);
		expect(capturedOptions).toBeDefined();
		expect(capturedOptions?.settings).toBe(sentinelSettings);
		expect(capturedOptions?.modelOverride).toEqual(["@audit"]);
		expect(capturedOptions?.modelRole).toBe("audit");
		expect(capturedOptions?.modelRegistry).toBe(sentinelRegistry);
		expect(capturedOptions?.outputSchema).toBe(sentinelOutputSchema);
		expect(capturedOptions?.outputSchemaSource).toBe("agent");
		expect(capturedOptions?.outputSchemaMode).toBe("strict");
		expect(result.started).toBe(true);
		expect(result.payload).toBe(wrappedPayload);
	});

	test("forwards authStorage and getApiKey resolver for OAuth-backed @audit models (OMP-176)", async () => {
		const sentinelSettings = Settings.isolated({ modelRoles: { audit: "kimi-code/k3:high" } });
		vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(sentinelSettings);
		mockDiscovery();

		let capturedOptions: executorModule.ExecutorOptions | undefined;
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async (options) => {
			capturedOptions = options;
			return {
				index: options.index,
				id: options.id,
				agent: options.agent.name,
				agentSource: options.agent.source,
				task: options.task,
				exitCode: 0,
				output: JSON.stringify({ report: "VERDICT: PASS\nAll ACs verified." }),
				stderr: "",
				truncated: false,
				durationMs: 100,
				tokens: 200,
				requests: 1,
			} as executorModule.SingleResult;
		});

		const fakeAuthStorage = { hasOAuth: () => true };
		const fakeResolver = vi.fn().mockReturnValue(async () => "oauth-bearer-token");
		const sentinelRegistry = {
			getApiKey: () => Promise.resolve("key"),
			authStorage: fakeAuthStorage,
			resolver: fakeResolver,
		};
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: {
				resolve: (role: string) =>
					role === "@audit" ? { id: "k3", provider: "kimi-code" } : undefined,
			},
			modelRegistry: sentinelRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(fakeCtx);
		const result = await runner("Run audit on OMP-176", "attempt-oauth-1");

		expect(result.started).toBe(true);
		expect(capturedOptions).toBeDefined();
		expect(capturedOptions?.modelRegistry).toBe(sentinelRegistry as unknown as executorModule.ExecutorOptions["modelRegistry"]);
		expect(capturedOptions?.authStorage).toBe(fakeAuthStorage as unknown as executorModule.ExecutorOptions["authStorage"]);
		expect(typeof capturedOptions?.getApiKey).toBe("function");

		const testModel = { id: "k3", provider: "kimi-code" } as unknown as Parameters<NonNullable<executorModule.ExecutorOptions["getApiKey"]>>[0];
		const resolvedKey = await capturedOptions?.getApiKey?.(testModel);
		expect(fakeResolver).toHaveBeenCalledWith(testModel, "attempt-oauth-1");
		expect(typeof resolvedKey).toBe("function");
	});
	test("behavioral: child AgentSession prompt resolves OAuth token when static registry key is unavailable (OMP-176)", async () => {
		const testModel = {
			id: "k3",
			provider: "kimi-code",
			api: "openai-completions",
			baseUrl: "https://api.kimi.com/coding/v1",
			name: "Kimi k3",
		} as unknown as Model;

		const fakeResolver = vi.fn().mockReturnValue(async () => "oauth-valid-bearer-token");
		const mockRegistry = {
			getApiKey: vi.fn().mockResolvedValue(undefined),
			authStorage: { hasOAuth: () => true },
			resolver: fakeResolver,
		} as unknown as ModelRegistry;

		const agent = new Agent({
			initialState: { model: testModel, systemPrompt: ["test"], tools: [] },
			getApiKey: requestModel => mockRegistry.resolver(requestModel, "attempt-oauth-behavioral"),
			streamFn: async () => {
				const stream = new AssistantMessageEventStream();
				queueMicrotask(() => {
					stream.push({ type: "text_delta", delta: "OK" });
					stream.end({ role: "assistant", content: [{ type: "text", text: "OK" }], stopReason: "stop" });
				});
				return stream;
			},
		});

		const session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings: Settings.isolated(),
			modelRegistry: mockRegistry,
		});

		// Calling session.prompt executes real AgentSession.prototype.prompt and validates API key using getApiKey
		await session.prompt("Run audit check");
		await session.waitForIdle();

		expect(fakeResolver).toHaveBeenCalledWith(testModel, "attempt-oauth-behavioral");
		await session.dispose();
	});

	test("fails if the auditor output schema is missing", async () => {
		const fakeAgent: AgentDefinition = {
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			source: "bundled",
		};
		mockDiscovery(fakeAgent);
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: {
				resolve: (role: string) =>
					role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined,
			},
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;

		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("output schema");
	});

	test("grant state guard denies remediation on stopped or canceled grants (OMP-186)", async () => {
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;
		let grantState = "stopped";
		let terminalReason: string | null = "budget_exhausted";
		let getExecutionCallCount = 0;
		let mockJudge = "judge-sha";
		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "work-1", key: "OMP-186", title: "Test", project: "Bookends" }),
			issueDetail: async () => ({
				key: "OMP-186",
				attemptSnapshot: { attemptId: "att-1", state: "audit_ready", candidateCommit: "commit-1", hasManifest: true },
			}),
			getExecution: async () => {
				getExecutionCallCount++;
				const effectiveState = getExecutionCallCount % 2 === 1 ? "active" : grantState;
				return {
					grant: { grant_id: "grant-1", grant_version: 1, state: effectiveState, terminal_reason: terminalReason, judge_sha256: mockJudge },
					items: [{ position: 0, work_id: "work-1", phase: "executing", plan_stamp: { paths: [] } }],
					activeItem: { position: 0, work_id: "work-1", phase: "executing", plan_stamp: { paths: [] }, close_attempts_started: 0 },
				};
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => ({ status: "reserved", launchId: "launch-1" }),
			settleAuditorLaunch: async () => ({ verdict: "NEEDS_FIX", event: { renderedText: "AC-1 failed" } }),
			workClient: {
				healthReady: async () => ({ contract_sha256: WORK_CONTRACT_SHA256, service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
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

		expect(registeredExecute).toBeDefined();

		mockDiscovery();
		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-1",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 0,
			output: JSON.stringify({ report: "VERDICT: NEEDS_FIX\nAC-1 failed" }),
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 10,
			requests: 1,
		} as executorModule.SingleResult);

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			models: { resolve: () => ({ id: "gpt-5.2", provider: "openai" }) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
			ui: { notify: () => {} },
		} as unknown as ExtensionContext;

		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
			mockJudge = tcb.judgeSha256;

			// 1. Stopped grant returns denial with terminal reason
			grantState = "stopped";
			terminalReason = "budget_exhausted";
			const stoppedResult = await registeredExecute!("call-1", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(stoppedResult.content[0].text).toContain("Execution grant stopped (budget_exhausted)");
			expect(stoppedResult.content[0].text).not.toContain("Update plan, stamp plan");

			// 2. Canceled grant returns denial with terminal reason
			grantState = "canceled";
			terminalReason = "owner_canceled";
			const canceledResult = await registeredExecute!("call-2", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(canceledResult.content[0].text).toContain("Execution grant canceled (owner_canceled)");
			expect(canceledResult.content[0].text).not.toContain("Update plan, stamp plan");

			// 3. Active grant returns remediation instruction
			grantState = "active";
			terminalReason = null;
			const activeResult = await registeredExecute!("call-3", { action: "begin_execution_review", body: "verification", work: "OMP-186" }, new AbortController().signal, undefined, fakeCtx);
			expect(activeResult.content[0].text).toContain("Update plan, stamp plan, fix findings, and rerun review.");
		} finally {
			dirtySpy.mockRestore();
		}
	});
	test("pause notice argument round-trips through execute resume lookup", async () => {
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "pause-notice-cache-"));
		const cacheFile = path.relative(
			path.join(os.homedir(), ".omp", "agent"),
			path.join(cacheDir, "work-cache.json"),
		);
		const handlers = new Map<string, Array<(event: any, ctx: ExtensionContext) => Promise<any>>>();
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const notifications: string[] = [];
		const sentMessages: Array<{ customType?: string; content?: string }> = [];
		const lookupArgs: Array<string | undefined> = [];
		const fakePi = {
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: (event: string, handler: (event: any, ctx: ExtensionContext) => Promise<any>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (message: { customType?: string; content?: string }) => {
				sentMessages.push(message);
			},
			appendEntry: () => {},
			getSessionId: () => "pause-notice-session",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = path.resolve(import.meta.dir, "../..");
		const head = headCommit(cwd) ?? "0".repeat(40);
		const workId = "c7a904ca-c979-4cc9-aa87-6063da842aec";
		const revisionId = "f7eed1b9-7f74-4998-85d6-2bce9862067c";
		const issue = { id: workId, key: "OMP-190", title: "Pause notice", project: "Bookends" };
		const exec = {
			grant: {
				grant_id: "28a3950b-0abe-4a0c-a3ec-18438b8b3267",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: cwd,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 1,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [],
			activeItem: {
				item_id: "item-1",
				workspace_id: "ws-1",
				grant_id: "28a3950b-0abe-4a0c-a3ec-18438b8b3267",
				work_id: workId,
				position: 0,
				phase: "executing",
				claimed_revision_id: revisionId,
				project_id: null,
				original_request: "Pause notice",
				original_request_sha256: "0".repeat(64),
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: head,
				current_git_baseline: head,
			},
		} as unknown as ExecutionSnapshot;
		exec.items = [exec.activeItem!];

		let failNextIssueLookup = false;
		const mockBackend = {
			cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			getExecution: async (selector?: string) => {
				lookupArgs.push(selector);
				return selector === undefined || selector === issue.key || selector === workId ? exec : null;
			},
			findIssue: async () => {
				if (failNextIssueLookup) {
					failNextIssueLookup = false;
					return null;
				}
				return issue;
			},
			setExecutionState: async (input: { targetState: "active" | "paused" }) => {
				exec.grant.state = input.targetState;
				exec.grant.grant_version++;
				return exec;
			},
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: WORK_CONTRACT_SHA256,
					service_fingerprint: "service-fp",
					judge_manifest: { judge_sha256: "judge-sha" },
				}),
				workItem: async () => ({
					work_id: workId,
					state: "IN_PROGRESS",
					project_id: null,
					revision: { revision_id: revisionId },
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);
		mockDiscovery();

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			abort: () => {},
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_color: string, text: string) => text },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;
		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);

		const pauseAndResume = async (fallback: boolean): Promise<string> => {
			failNextIssueLookup = fallback;
			const inputHandler = handlers.get("input")?.[0];
			expect(inputHandler).toBeDefined();
			await inputHandler!({ source: "user", originalText: "owner interjection" }, fakeCtx);
			expect(exec.grant.state).toBe("paused");

			const beforeStarts = handlers.get("before_agent_start") ?? [];
			expect(beforeStarts.length).toBeGreaterThan(0);
			let notice = "";
			for (const beforeStart of beforeStarts) {
				const injected = await beforeStart({}, fakeCtx);
				const content = String(injected?.message?.content ?? "");
				if (content.includes("Execution grant paused")) notice = content;
			}
			const match = notice.match(/\/execute resume ([^']+)'/);
			expect(match).not.toBeNull();
			const suggested = match![1]!;

			const resume = commands.get("execute");
			expect(resume).toBeDefined();
			await resume!(`resume ${suggested}`, fakeCtx);
			expect(exec.grant.state).toBe("active");
			expect(notifications.at(-1)).toContain("Execution grant resumed");
			return suggested;
		};

		try {
			expect(await pauseAndResume(false)).toBe(issue.key);
			expect(lookupArgs).toContain(issue.key);

			expect(await pauseAndResume(true)).toBe(workId);
			expect(lookupArgs).toContain(workId);
			expect(sentMessages.some(message => message.customType === "work-execute")).toBe(true);
		} finally {
			dirtySpy.mockRestore();
			fs.rmSync(cacheDir, { recursive: true, force: true });
		}
	});

});

describe("renderNextActionBanner table-driven coverage (OMP-168)", () => {
	const snapshot = (state: string): CloseAttemptSnapshot => ({
		attemptId: "att-1",
		state,
		remainingLaunches: 3,
		remainingReports: 2,
		hasManifest: true,
		isLaunchable: state === "audit_ready",
		nextAction: "",
	});

	test("active state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("active"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT active",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"verification"',
			'BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT active");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audit_ready state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audit_ready"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audit_ready",
			'NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"',
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audit_ready");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("auditor_in_flight state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("auditor_in_flight"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT auditor_in_flight",
			"NEXT REQUIRED ACTION: wait for the current native run to settle and use get_work only for recovery",
			"BLOCKED ACTIONS: run_audit, append_evidence, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT auditor_in_flight");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (authorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"',
			"BLOCKED ACTIONS: run_audit, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (unauthorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), false);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			"NEXT REQUIRED ACTION: owner /summary must be entered in this session to authorize closeout review",
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("closeout_requested state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("closeout_requested"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT closeout_requested",
			"NEXT REQUIRED ACTION: owner /done closes this work",
			"BLOCKED ACTIONS: run_audit, append_evidence",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT closeout_requested");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("terminal or missing snapshot returns empty array", () => {
		expect(renderNextActionBanner("HOME-1", undefined, true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("completed"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("superseded"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("budget_exhausted"), true)).toEqual([]);
	});

	test("renderSummaryResumeDigest contains banner and 5 compact review sections", () => {
		const digest = renderSummaryResumeDigest("HOME-1", snapshot("audited"));
		expect(digest).toContain("STATUS: CLOSE ATTEMPT audited");
		expect(digest).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"');
		expect(digest).toContain("Satisfied steps must NOT be repeated");
		expect(digest).toContain('Call `work action:"get_work", work:"HOME-1"`');
		expect(digest).toContain('1. Verbatim `work action:"my_now"` completion tree');
		expect(digest).toContain("2. MOVED");
		expect(digest).toContain("3. PROOF");
		expect(digest).toContain("4. UNVERIFIED / BLOCKED");
		expect(digest).toContain("5. NEXT SESSION");
	});
});

describe("confirmation lifecycle (OMP-168)", () => {
	test("same-transcript identical payload approves once; consumed receipt refuses retry", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create an issue";
		const detail = "title: test";
		const params = { title: "test" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		expect(match).not.toBeNull();
		const confirmationId = match![1];

		// Second call with confirm:true and confirmation_id approves
		const second = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(second.approved).toBe(true);

		// Third call with already consumed id is refused
		const third = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(third.approved).toBe(false);
		if (!third.approved) {
			expect(third.preview).toContain("already consumed");
		}
	});

	test("59-minute receipt remains usable", () => {
		resetConfirmations({ resetShared: true });
		const action = "revise_work";
		const question = "Model wants to revise";
		const detail = "new title";
		const params = { title: "revised" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		const confirmationId = match![1];

		// Advance time by 59 minutes
		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 59 * 60_000;
			const outcome = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: confirmationId,
			});
			expect(outcome.approved).toBe(true);
		} finally {
			Date.now = originalNow;
		}
	});

	test(">60-minute expired receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "set_now";
		const question = "Model wants to set now";
		const detail = "HOME-1";
		const params = { work: "HOME-1" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 61 * 60_000;
			const retry = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: oldId,
			});
			expect(retry.approved).toBe(false);
			if (!retry.approved) {
				expect(retry.preview).toContain("CONFIRM REQUIRED");
				const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
				expect(newId).not.toBe(oldId);
			}
		} finally {
			Date.now = originalNow;
		}
	});

	test("foreign transcript receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "queue_work";
		const question = "Model wants to queue";
		const detail = "HOME-2";
		const params = { work: "HOME-2", question: "Is this done?" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Switch session / rotate transcript without clearing unconsumed receipts
		resetConfirmations({ resetShared: true });

		const retry = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: oldId,
		});
		expect(retry.approved).toBe(false);
		if (!retry.approved) {
			expect(retry.preview).toContain("CONFIRM REQUIRED");
			const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
			expect(newId).not.toBe(oldId);
		}
	});

	test("changed payload and unknown receipt stay refused", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create";
		const detail = "title A";
		const params = { title: "title A" };

		const first = confirmWrite(action, question, detail, params);
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Changed payload
		const changed = confirmWrite(action, question, "title B", {
			title: "title B",
			confirm: true,
			confirmation_id: id,
		});
		expect(changed.approved).toBe(false);
		if (!changed.approved) {
			expect(changed.preview).toContain("payload changed");
		}

		// Unknown ID
		const unknown = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: "cf-unknown0000",
		});
		expect(unknown.approved).toBe(false);
		if (!unknown.approved) {
			expect(unknown.preview).toContain("unknown or already-used");
		}
	});

	test("subagent reset (resetShared:false) never invalidates owner receipts", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Owner create";
		const detail = "owner details";
		const params = { title: "owner task" };

		const first = confirmWrite(action, question, detail, params, { isSubagent: false });
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Subagent session resets local confirmations
		resetConfirmations({ resetShared: false });

		// Owner confirmation call still approves
		const confirmCall = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: id,
		});
		expect(confirmCall.approved).toBe(true);
	});
});

describe("audit judge TCB sealing (OMP-180)", () => {
	test("getExecutorSha fails closed when required yield-assembly source is unresolvable", async () => {
		const { getExecutorSha } = await import("../extensions/workflow/audit-tcb");
		expect(() => {
			getExecutorSha((specifier: string) => {
				if (specifier === "@oh-my-pi/pi-coding-agent/task/yield-assembly") return undefined;
				return import.meta.resolve(specifier);
			});
		}).toThrow("Failed to resolve required audit transport source: @oh-my-pi/pi-coding-agent/task/yield-assembly");
	});

	test("getExecutorSha returns stable 64-hex digest under standard resolver", async () => {
		const { getExecutorSha } = await import("../extensions/workflow/audit-tcb");
		const sha = getExecutorSha();
		expect(typeof sha).toBe("string");
		expect(sha).toMatch(/^[0-9a-f]{64}$/);
	});
});

describe("terminal execution grant closing notices and banners (OMP-196)", () => {
	const makeSnapshot = (
		state: "stopped" | "canceled" | "active" | "paused" | "completed",
		mode: "single" | "queue" = "queue",
		items: Array<{ position: number; work_id: string; phase: ExecutionItemPhase; completed_at?: string }> = [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
			{ position: 2, work_id: "OMP-181", phase: "pending" },
		],
		terminal_reason: string | null = null,
	): ExecutionSnapshot => {
		const mappedItems = items.map(it => ({
			item_id: `item-${it.position}`,
			workspace_id: "ws-1",
			grant_id: "ad5c45a7-1234-5678-9abc-def012345678",
			work_id: it.work_id,
			position: it.position,
			phase: it.phase,
			claimed_revision_id: "rev-1",
			initial_git_baseline: "commit-0",
			original_request: "req",
			original_request_sha256: "req-sha",
			close_attempts_started: 0,
			consecutive_no_progress: 0,
			completed_at: it.completed_at,
		}));
		return {
			grant: {
				grant_id: "ad5c45a7-1234-5678-9abc-def012345678",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: "oh-my-pi",
				remote_ref: "refs/heads/main",
				state,
				mode,
				grant_version: 1,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				terminal_reason,
				authorization_hash: "auth-hash",
				judge_sha256: "judge-sha",
				created_at: new Date().toISOString(),
				expires_at: new Date().toISOString(),
			},
			items: mappedItems,
			activeItem: mappedItems.find(it => it.phase === "executing") ?? null,
		};
	};

	test("closing notice on defect stop with blocking fix key (queue mode)", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
			{ position: 2, work_id: "OMP-181", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const notice = computeExecutionNoticeDetails(exec, "candidate_drift gate defect filed as OMP-195", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 0 completed, 3 skipped (of 3 items).");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-195 then /execute OMP-176 --queue");
		expect(notice.fullNotice).toBe([
			"Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.",
			"Items: 0 completed, 3 skipped (of 3 items).",
			"Next: /execute OMP-195 then /execute OMP-176 --queue",
		].join("\n"));
	});

	test("closing notice on defect stop with blocking fix key (single mode)", () => {
		const exec = makeSnapshot("stopped", "single", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
		], "blocked by OMP-195");

		const notice = computeExecutionNoticeDetails(exec, "blocked by OMP-195", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (blocked by OMP-195). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 0 completed, 1 skipped (of 1 item).");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-195 then /execute OMP-176");
	});

	test("closing notice on partial queue completion and stop", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-170", phase: "completed", completed_at: new Date().toISOString() },
			{ position: 1, work_id: "OMP-176", phase: "executing" },
			{ position: 2, work_id: "OMP-180", phase: "pending" },
		], "budget_exhausted");

		const notice = computeExecutionNoticeDetails(exec, "budget_exhausted", "OMP-176");
		expect(notice.causeLine).toBe("Execution grant stopped (budget_exhausted). Grant is terminal; resume is impossible.");
		expect(notice.tallyLine).toBe("Items: 1 completed, 2 skipped (of 3 items).");
		expect(notice.nextCommandLine).toBe("Next: /now OMP-176 then /summary");
	});

	test("closing notice on contract approval requirement (paused vs terminal)", () => {
		const pausedExec = makeSnapshot("paused", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "contract_approval_required:1a5441d9");
		const pausedNotice = computeExecutionNoticeDetails(pausedExec, "contract_approval_required:1a5441d9", "OMP-180");
		expect(pausedNotice.nextCommandLine).toBe("Next: omp-work approve --issue OMP-180 then /execute resume");

		const stoppedExec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "contract_approval_required:1a5441d9");
		const stoppedNotice = computeExecutionNoticeDetails(stoppedExec, "contract_approval_required:1a5441d9", "OMP-180");
		expect(stoppedNotice.nextCommandLine).toBe("Next: omp-work approve --issue OMP-180 then /execute OMP-180 --queue");
	});

	test("closing notice on owner cancellation", () => {
		const exec = makeSnapshot("canceled", "queue", [
			{ position: 0, work_id: "OMP-180", phase: "executing" },
		], "owner_cancel");

		const notice = computeExecutionNoticeDetails(exec, "owner_cancel", "OMP-180");
		expect(notice.causeLine).toBe("Execution grant canceled (owner_cancel). Grant is terminal; resume is impossible.");
		expect(notice.nextCommandLine).toBe("Next: /execute OMP-180 --queue");
	});

	test("renderExecutionTerminalBanner returns 4-line banner for stopped/canceled grants", () => {
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const banner = renderExecutionTerminalBanner(exec as any, "OMP-176");
		expect(banner).toEqual([
			"STATUS: EXECUTION GRANT stopped (terminal — resume impossible)",
			"CAUSE: candidate_drift gate defect filed as OMP-195",
			"ITEMS: 0 completed, 2 skipped (of 2 items).",
			"NEXT REQUIRED ACTION: /execute OMP-195 then /execute OMP-176 --queue",
		]);

		const activeExec = makeSnapshot("active", "queue");
		expect(renderExecutionTerminalBanner(activeExec, "OMP-176")).toEqual([]);
	});

	test("resolveAnchorKey resolves UUID to issue key via backend", async () => {
		const mockBackend = {
			findIssue: async (keyOrId: string) => {
				if (keyOrId === "uuid-176") return { key: "OMP-176" };
				if (keyOrId === "uuid-180") return { key: "OMP-180" };
				return null;
			},
		};
		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "uuid-176", phase: "executing" },
			{ position: 1, work_id: "uuid-180", phase: "pending" },
		]);
		const key = await resolveAnchorKey(mockBackend, exec);
		expect(key).toBe("OMP-176");

		// Foreign targetKey that does not belong to the grant is ignored
		const foreignKey = await resolveAnchorKey(mockBackend, exec, "OMP-999");
		expect(foreignKey).toBe("OMP-176");

		// Member targetKey is accepted
		const memberKey = await resolveAnchorKey(mockBackend, exec, "uuid-180");
		expect(memberKey).toBe("OMP-180");
	});

	test("stop_execution tool action returns full closing notice with cause, tally, and next command", async () => {
		let registeredExecute: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const fakePi = {
			registerTool: (def: { execute: (id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }> }) => {
				registeredExecute = def.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = makeSnapshot("active", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		]);

		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			getExecution: async () => exec,
			setExecutionState: async (input: { targetState: ExecutionSnapshot["grant"]["state"]; reason?: string }) => ({
				grant: { ...exec.grant, state: input.targetState, terminal_reason: input.reason },
			}),
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

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			ui: { notify: () => {}, theme: { fg: (_c: string, t: string) => t }, setStatus: () => {} },
		} as unknown as ExtensionContext;

		const result = await registeredExecute!(
			"call-stop",
			{ action: "stop_execution", body: "candidate_drift gate defect filed as OMP-195", work: "OMP-176" },
			new AbortController().signal,
			undefined,
			fakeCtx,
		);

		expect(result.content[0].text).toContain("Execution grant stopped (candidate_drift gate defect filed as OMP-195). Grant is terminal; resume is impossible.");
		expect(result.content[0].text).toContain("Items: 0 completed, 2 skipped (of 2 items).");
		expect(result.content[0].text).toContain("Next: /execute OMP-195 then /execute OMP-176 --queue");
	});

	test("session_start populates footer status with terminal execution grant banner", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const statuses: Record<string, string> = {};
		const notifications: string[] = [];
		const fakePi = {
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const exec = makeSnapshot("stopped", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], "candidate_drift gate defect filed as OMP-195");

		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => {
				if (key === "OMP-404") return null;
				return { id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" };
			},
			getExecution: async () => exec,
			currentNow: async () => ({ id: "uuid-999", key: "OMP-999", title: "Unrelated NOW", project: "Bookends" }),
			setNowRemote: async () => {},
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

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: (t: string) => { notifications.push(t); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const startHandlers = handlers.get("session_start") ?? [];
		expect(startHandlers.length).toBeGreaterThan(0);
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
		expect(statuses["work-now"]).toContain("0 completed, 2 skipped (of 2 items).");
		expect(statuses["work-now"]).toContain("Next: /execute OMP-195 then /execute OMP-176 --queue");

		// Verify persistence across /now focus change to an unrelated issue
		const nowCmd = commands.get("now");
		if (nowCmd) {
			await nowCmd("OMP-999", fakeCtx);
			expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
			expect(statuses["work-now"]).toContain("Next: /execute OMP-195 then /execute OMP-176 --queue");
		}

		// Verify persistence across rejected /execute start (issue not found)
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const execCmd = commands.get("execute");
			if (execCmd) {
				await execCmd("OMP-404", fakeCtx);
				expect(notifications.some(n => n.includes("Issue OMP-404 not found"))).toBe(true);
				expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (candidate_drift gate defect filed as OMP-195)");
			}
		} finally {
			dirtySpy.mockRestore();
		}
	});

	test("session_start with active or null grant clears cached terminal execution state", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const statuses: Record<string, string> = {};
		const fakePi = {
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" }),
			getExecution: async () => null,
			currentNow: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test OMP-176", project: "Bookends" }),
			setNowRemote: async () => {},
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

		const fakeCtx = {
			cwd: path.resolve(import.meta.dir, "../.."),
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const startHandlers = handlers.get("session_start") ?? [];
		for (const h of startHandlers) {
			await h({}, fakeCtx);
		}

		expect(statuses["work-now"]).not.toContain("✕ Grant");
		expect(statuses["work-now"]).toContain("NOW · Bookends");
	});

	test("/execute resume transitioning to stopped emits full notice and status message", async () => {
		const commands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const messages: Array<{ customType?: string; content?: string }> = [];
		const notifications: string[] = [];
		const fakePi = {
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				commands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			sendMessage: (msg: { customType?: string; content?: string }) => {
				messages.push(msg);
			},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = path.resolve(import.meta.dir, "../..");
		const head = headCommit(cwd) ?? "0".repeat(40);
		const exec = makeSnapshot("paused", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], null);
		exec.items[0]!.initial_git_baseline = head;
		exec.items[0]!.current_git_baseline = head;
		exec.activeItem!.initial_git_baseline = head;
		exec.activeItem!.current_git_baseline = head;

		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" }),
			getExecution: async () => exec,
			currentNow: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			setExecutionState: async () => ({
				grant: { ...exec.grant, state: "stopped" as const, terminal_reason: "max_continuations_exceeded" },
			}),
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({
					work_id: "uuid-176",
					state: "IN_PROGRESS",
					project_id: null,
					revision: { revision_id: "rev-1" },
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const resumeCmd = commands.get("execute");
			expect(resumeCmd).toBeDefined();
			await resumeCmd!("resume OMP-176", fakeCtx);

			expect(notifications.some(n => n.includes("Execution grant stopped: max_continuations_exceeded"))).toBe(true);
			expect(notifications.some(n => n.includes("Items: 0 completed, 2 skipped (of 2 items)."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Grant is terminal; resume is impossible."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Next: /execute OMP-176 --queue"))).toBe(true);
		} finally {
			dirtySpy.mockRestore();
		}
	});

	test("session_start recovery transitioning to stopped emits full notice and updates status", async () => {
		const handlers = new Map<string, Array<(event: unknown, ctx: ExtensionContext) => Promise<void>>>();
		const messages: Array<{ customType?: string; content?: string }> = [];
		const notifications: string[] = [];
		const statuses: Record<string, string> = {};
		const fakePi = {
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: (e: unknown, ctx: ExtensionContext) => Promise<void>) => {
				const list = handlers.get(event) ?? [];
				list.push(handler);
				handlers.set(event, list);
			},
			sendMessage: (msg: { customType?: string; content?: string }) => {
				messages.push(msg);
			},
			appendEntry: () => {},
			getSessionId: () => "sess-1",
			zod: z,
		} as unknown as ExtensionAPI;

		const cwd = path.resolve(import.meta.dir, "../..");
		const head = headCommit(cwd) ?? "0".repeat(40);
		const exec = makeSnapshot("active", "queue", [
			{ position: 0, work_id: "OMP-176", phase: "executing" },
			{ position: 1, work_id: "OMP-180", phase: "pending" },
		], null);
		exec.items[0]!.initial_git_baseline = head;
		exec.items[0]!.current_git_baseline = head;
		exec.activeItem!.initial_git_baseline = head;
		exec.activeItem!.current_git_baseline = head;

		const mockBackend = {
			cacheFile: "work-cache.json",
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (key: string) => ({ id: `uuid-${key}`, key, title: `Test ${key}`, project: "Bookends" }),
			getExecution: async () => exec,
			currentNow: async () => ({ id: "uuid-176", key: "OMP-176", title: "Test", project: "Bookends" }),
			setExecutionState: async () => ({
				grant: { ...exec.grant, state: "stopped" as const, terminal_reason: "max_continuations_exceeded" },
			}),
			getPendingExecutionClaims: async () => [],
			workClient: {
				healthReady: async () => ({ contract_sha256: "contract-sha", service_fingerprint: "service-fp", judge_manifest: { judge_sha256: "judge-sha" } }),
				workItem: async () => ({
					work_id: "uuid-176",
					state: "IN_PROGRESS",
					project_id: null,
					revision: { revision_id: "rev-1" },
				}),
				workflow: async () => ({ relations: [] }),
			},
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = {
			cwd,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			ui: {
				notify: (text: string) => { notifications.push(text); },
				theme: { fg: (_c: string, t: string) => t },
				setStatus: (key: string, text: string | undefined) => {
					if (text !== undefined) statuses[key] = text;
					else delete statuses[key];
				},
			},
		} as unknown as ExtensionContext;

		const tcb = await computeAuditTcb(fakeCtx, mockBackend.workClient!);
		exec.grant.judge_sha256 = tcb.judgeSha256;
		const dirtySpy = vi.spyOn(gitModule, "dirtyPaths").mockReturnValue([]);
		try {
			const startHandlers = handlers.get("session_start") ?? [];
			for (const h of startHandlers) {
				await h({}, fakeCtx);
			}

			expect(notifications.some(n => n.includes("Execution grant stopped: max_continuations_exceeded"))).toBe(true);
			expect(statuses["work-now"]).toContain("✕ Grant ad5c45a7 stopped (terminal — resume impossible) (max_continuations_exceeded)");
			expect(statuses["work-now"]).toContain("0 completed, 2 skipped (of 2 items).");
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Grant is terminal; resume is impossible."))).toBe(true);
			expect(messages.some(m => m.customType === "work-execution-status" && m.content?.includes("Next: /execute OMP-176 --queue"))).toBe(true);
		} finally {
			dirtySpy.mockRestore();
		}
	});
});

describe("service refresh during autonomous execution review (OMP-199)", () => {
	const defaultAuditor: AgentDefinition = {
		name: "auditor",
		description: "Auditor agent",
		systemPrompt: "Audit prompt",
		model: ["@audit"],
		output: { properties: { report: { type: "string" } } },
		source: "bundled",
	};

	function mockDiscovery(agent: AgentDefinition = defaultAuditor) {
		return vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [agent],
			projectAgentsDir: null,
		});
	}

	function makeTempRepo(): { dir: string; cacheFile: string; headSha: string; cleanup: () => void } {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "service-refresh-test-"));
		const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "service-refresh-cache-"));
		spawnSync("git", ["init", "-b", "main"], { cwd: dir });
		spawnSync("git", ["config", "user.name", "Test"], { cwd: dir });
		spawnSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
		fs.writeFileSync(path.join(dir, ".work-project"), "The Bookends\n");
		fs.mkdirSync(path.join(dir, "python/omp-work/src/omp_work/v1"), { recursive: true });
		fs.writeFileSync(path.join(dir, "python/omp-work/src/omp_work/v1/store.py"), "# initial\n");
		const contractDir = path.join(dir, "python/omp-work/src/omp_work/contracts/v1");
		const realContractDir = path.resolve(import.meta.dir, "../../python/omp-work/src/omp_work/contracts/v1");
		fs.cpSync(realContractDir, contractDir, { recursive: true });
		spawnSync("git", ["add", "."], { cwd: dir });
		spawnSync("git", ["commit", "-m", "initial commit"], { cwd: dir });
		const head = headCommit(dir) ?? "0".repeat(40);
		const cacheFile = path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "work-cache.json"));
		return {
			dir,
			cacheFile,
			headSha: head,
			cleanup: () => {
				fs.rmSync(dir, { recursive: true, force: true });
				fs.rmSync(cacheDir, { recursive: true, force: true });
			},
		};
	}

	afterEach(() => {
		vi.restoreAllMocks();
	});

	test("service refresh happy path in temp git repo executes rebind -> restart -> readiness -> PASS audit -> completion", async () => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		// Modify sealed python file in real git repo
		fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified source\n");

		const callLog: string[] = [];
		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 3,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "old-judge-sha-00000000000000000000000000000000000000000000000000000000",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async (keyOrId: string) => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			getExecution: async () => exec,
			setExecutionState: async (input: { grantId: string; expectedGrantVersion: number; targetState: string; reason?: string | null; judgeSha256: string }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				exec.grant.grant_version++;
				exec.grant.judge_sha256 = input.judgeSha256;
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async (_issue: unknown, kind: string) => {
				callLog.push(`appendEvidence:${kind}`);
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			recordCloseoutReview: async () => {
				callLog.push("recordCloseoutReview");
				return { status: "applied" };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: {
				healthReady: async () => {
					callLog.push("healthReady");
					return { ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } };
				},
				workflow: async () => ({ receipts: [], auditor_launches: [], item: null, close_attempts: [] }),
			},
		} as unknown as WorkflowBackend;

		const restartMock = vi.fn(async () => {
			callLog.push("restartWorkService");
		});

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		expect(registeredExecute).toBeDefined();

		mockDiscovery();
		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-199",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 0,
			output: JSON.stringify({ report: "VERDICT: PASS\n(none)" }),
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 10,
			requests: 1,
		} as executorModule.SingleResult);

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/main", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: { resolve: () => ({ id: "gpt-5.2", provider: "openai" }) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		try {
			const res = await registeredExecute!("call-1", {
				action: "begin_execution_review",
				work: "OMP-199",
				body: "pytest passed: 53 passed",
			}, new AbortController().signal, () => {}, fakeCtx);

			expect(res.content[0]?.text).toContain("Execution grant completed");
			expect(callLog).toContain("setExecutionState:service_refresh");
			expect(callLog).toContain("restartWorkService");
			expect(callLog).toContain("healthReady");
			expect(callLog).toContain("finalizeExecutionCandidate");
			expect(callLog).toContain("beginCloseAttempt");
			expect(callLog).toContain("completeExecutionItem");

			// Verify exact call order: rebind -> restart -> healthReady -> freeze -> beginCloseAttempt -> complete
			const rebindIdx = callLog.indexOf("setExecutionState:service_refresh");
			const restartIdx = callLog.indexOf("restartWorkService");
			const healthIdx = callLog.lastIndexOf("healthReady");
			const freezeIdx = callLog.indexOf("finalizeExecutionCandidate");
			const attemptIdx = callLog.indexOf("beginCloseAttempt");
			const completeIdx = callLog.indexOf("completeExecutionItem");

			expect(rebindIdx).toBeLessThan(restartIdx);
			expect(restartIdx).toBeLessThan(healthIdx);
			expect(healthIdx).toBeLessThan(freezeIdx);
			expect(freezeIdx).toBeLessThan(attemptIdx);
			expect(attemptIdx).toBeLessThan(completeIdx);
		} finally {
			repo.cleanup();
		}
	});

	test("service refresh refusal cases perform zero freeze, push, or audit calls", async () => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		const freezeSpy = vi.spyOn(gitModule, "freezeCandidateCommit");
		const pushSpy = vi.spyOn(gitModule, "pushCandidate");

		const callLog: string[] = [];
		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 3,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				judge_sha256: "old-judge-sha-00000000000000000000000000000000000000000000000000000000",
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		let shouldFailRestart = false;
		let healthFp = "prospective-fp-199";
		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => [],
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			getExecution: async () => exec,
			setExecutionState: async (input: { reason?: string | null; judgeSha256: string }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async () => {
				callLog.push("appendEvidence");
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: healthFp, judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({ receipts: [], auditor_launches: [], item: null, close_attempts: [] }),
			},
		} as unknown as WorkflowBackend;

		const restartMock = vi.fn(async () => {
			if (shouldFailRestart) throw new Error("systemctl restart failed");
			callLog.push("restartWorkService");
		});

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		expect(registeredExecute).toBeDefined();
		mockDiscovery();

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: { resolve: () => ({ id: "gpt-5.2", provider: "openai" }) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		try {
			// 1. Refusal: unsealed dirt in real repo
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified\n");
			fs.writeFileSync(path.join(repo.dir, "unsealed.txt"), "unsealed dirt\n");
			let res = await registeredExecute!("call-1", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("unsealed dirty paths");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 2. Refusal: migration dirt in real repo (only migration file dirty)
			fs.rmSync(path.join(repo.dir, "unsealed.txt"), { force: true });
			spawnSync("git", ["checkout", "--", "python/omp-work/src/omp_work/v1/store.py"], { cwd: repo.dir });
			fs.mkdirSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations"), { recursive: true });
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations/0024_test.sql"), "-- mig\n");
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await registeredExecute!("call-2", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("migrations directory contains changes");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 3. Refusal: non-service TCB drift (clean repo, mismatched judge)
			fs.rmSync(path.join(repo.dir, "python/omp-work/src/omp_work/operations/migrations/0024_test.sql"), { force: true });
			spawnSync("git", ["checkout", "."], { cwd: repo.dir });
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await registeredExecute!("call-3", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("judge TCB drift");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 4. Refusal: restart failure
			fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified\n");
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			shouldFailRestart = true;
			res = await registeredExecute!("call-4", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("WorkService restart failed");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 5. Refusal: post-restart fingerprint mismatch
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			shouldFailRestart = false;
			let healthCallCount = 0;
			mockBackend.workClient!.healthReady = async () => {
				healthCallCount++;
				const fp = healthCallCount === 1 ? "prospective-fp-199" : "mismatched-fp-999";
				return { ready: true, contract_sha256: "contract-sha", service_fingerprint: fp, judge_manifest: { judge_sha256: "judge-sha" } };
			};
			res = await registeredExecute!("call-5", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("service_fingerprint mismatch");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c === "finalizeExecutionCandidate" || c === "beginCloseAttempt" || c === "reserveAuditorLaunch" || c === "completeExecutionItem")).toHaveLength(0);

			// 6. Refusal: preflight unit unloaded refusal
			let preflightRegisteredExecute: typeof registeredExecute;
			const preflightPi = {
				zod: z,
				registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
					if (spec.name === "work") preflightRegisteredExecute = spec.execute;
				},
				registerMessageRenderer: () => {},
				registerCommand: () => {},
				registerFlag: () => {},
				on: () => {},
				sendMessage: () => {},
				appendEntry: () => {},
			} as unknown as ExtensionAPI;
			createWorkflowHost({
				backend: mockBackend,
				teamNoun: "the ledger",
				entryType: "work-now",
				acceptEntry: () => true,
				preflightWorkService: async () => { throw new Error("omp-work-service.service is not loaded"); },
				restartWorkService: restartMock,
			})(preflightPi);
			callLog.length = 0;
			freezeSpy.mockClear();
			pushSpy.mockClear();
			res = await preflightRegisteredExecute!("call-6", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res.content[0]?.text).toContain("omp-work-service.service is not loaded");
			expect(freezeSpy).toHaveBeenCalledTimes(0);
			expect(pushSpy).toHaveBeenCalledTimes(0);
			expect(callLog.filter(c => c.startsWith("setExecutionState"))).toHaveLength(0);
		} finally {
			repo.cleanup();
		}
	});

	test("crash-retry case: idempotent refresh with new judge runs restart once and caches marker", async () => {
		const repo = makeTempRepo();
		let registeredExecute: ((id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: { type: string; text: string }[] }>) | undefined;
		const fakePi = {
			zod: z,
			registerTool: (spec: { name: string; execute: typeof registeredExecute }) => {
				if (spec.name === "work") registeredExecute = spec.execute;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			appendEntry: () => {},
		} as unknown as ExtensionAPI;

		// Modify sealed python file
		fs.writeFileSync(path.join(repo.dir, "python/omp-work/src/omp_work/v1/store.py"), "# modified source\n");

		const fakeCtx = {
			cwd: repo.dir,
			taskDepth: 0,
			sessionManager: { getBranch: () => [] },
			models: { resolve: () => ({ id: "gpt-5.2", provider: "openai" }) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			ui: {
				notify: () => {},
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		mockDiscovery();
		const tcb = await computeAuditTcb(fakeCtx, {
			healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
		} as unknown as WorkClient);

		const exec: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-199",
				workspace_id: "ws-1",
				owner_id: "owner-1",
				repository: repo.dir,
				remote_ref: "refs/heads/main",
				state: "active",
				mode: "single",
				grant_version: 4,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "auth-hash",
				// Grant already carries the new judge SHA (as if previous turn set it before crash)
				judge_sha256: tcb.judgeSha256,
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [
				{
					item_id: "item-199",
					workspace_id: "ws-1",
					grant_id: "grant-199",
					work_id: "uuid-199",
					position: 0,
					phase: "executing",
					claimed_revision_id: "rev-1",
					original_request: "test request",
					original_request_sha256: "0".repeat(64),
					criteria_sha256: "0".repeat(64),
					plan_stamp_sha256: "0".repeat(64),
					plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
					close_attempts_started: 0,
					consecutive_no_progress: 0,
					initial_git_baseline: repo.headSha,
					current_git_baseline: repo.headSha,
				},
			],
			activeItem: {
				item_id: "item-199",
				workspace_id: "ws-1",
				grant_id: "grant-199",
				work_id: "uuid-199",
				position: 0,
				phase: "executing",
				claimed_revision_id: "rev-1",
				original_request: "test request",
				original_request_sha256: "0".repeat(64),
				criteria_sha256: "0".repeat(64),
				plan_stamp_sha256: "0".repeat(64),
				plan_stamp: { paths: ["python/omp-work/src/omp_work/v1/store.py"], candidate_id: "cand-199" },
				close_attempts_started: 0,
				consecutive_no_progress: 0,
				initial_git_baseline: repo.headSha,
				current_git_baseline: repo.headSha,
			},
		};

		const callLog: string[] = [];
		let pendingEvents = [
			{
				event_id: "ev-pending-1",
				sequence: 1,
				work_id: "uuid-199",
				attempt_id: null,
				launch_id: null,
				event_type: "close_attempt_started",
				reason_code: "started",
				reason: "started",
				legal_next_actions: [] as string[],
				remaining_launches: 3,
				remaining_reports: 2,
				requires_fresh_authorization: false,
				rendered_text: "close attempt started",
				rendered_sha256: "0".repeat(64),
				requires_delivery: true,
				created_at: new Date().toISOString(),
			},
		];
		const mockBackend = {
			cacheFile: repo.cacheFile,
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			pendingDeliveries: async () => pendingEvents,
			findIssue: async () => ({ id: "uuid-199", key: "OMP-199", title: "Test 199", project: "The Bookends" }),
			issueDetail: async () => ({ key: "OMP-199", attemptSnapshot: undefined }),
			getExecution: async () => exec,
			setExecutionState: async (input: { reason?: string | null }) => {
				callLog.push(`setExecutionState:${input.reason}`);
				return exec;
			},
			finalizeExecutionCandidate: async () => {
				callLog.push("finalizeExecutionCandidate");
				return { candidate_id: "cand-199", candidate_sha256: "cand-sha", commit_sha: "1".repeat(40) };
			},
			appendEvidence: async (_issue: unknown, kind: string) => {
				callLog.push(`appendEvidence:${kind}`);
				return { receipt_id: "receipt-199" };
			},
			beginCloseAttempt: async () => {
				callLog.push("beginCloseAttempt");
				return { status: "applied", attemptId: "att-199", event: { requiresDelivery: false } };
			},
			sealAuditManifest: async () => {
				callLog.push("sealAuditManifest");
				return { status: "applied" };
			},
			sealedAuditTask: async () => ({ taskSha256: "task-sha", taskBody: "task body" }),
			reserveAuditorLaunch: async () => {
				callLog.push("reserveAuditorLaunch");
				return { status: "reserved", launchId: "launch-199" };
			},
			settleAuditorLaunch: async () => {
				callLog.push("settleAuditorLaunch");
				return { verdict: "PASS", event: { renderedText: "PASS" } };
			},
			recordCloseoutReview: async () => {
				callLog.push("recordCloseoutReview");
				return { status: "applied" };
			},
			completeExecutionItem: async () => {
				callLog.push("completeExecutionItem");
				exec.activeItem!.phase = "completed";
				return exec;
			},
			workClient: {
				healthReady: async () => ({ ready: true, contract_sha256: "contract-sha", service_fingerprint: "prospective-fp-199", judge_manifest: { judge_sha256: "judge-sha" } }),
				workflow: async () => ({ receipts: [], auditor_launches: [], item: null, close_attempts: [] }),
			},
		} as unknown as WorkflowBackend;
		let restartCount = 0;
		const restartMock = vi.fn(async () => {
			restartCount++;
			callLog.push("restartWorkService");
		});

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			restartWorkService: restartMock,
		})(fakePi);

		vi.spyOn(executorModule, "runSubprocess").mockResolvedValue({
			index: 0,
			id: "att-199",
			agent: "auditor",
			agentSource: "bundled",
			task: "task",
			exitCode: 0,
			output: JSON.stringify({ report: "VERDICT: PASS\n(none)" }),
			stderr: "",
			truncated: false,
			durationMs: 10,
			tokens: 10,
			requests: 1,
		} as executorModule.SingleResult);

		vi.spyOn(gitModule, "pushCandidate").mockResolvedValue({ status: "pushed", remoteRef: "refs/heads/main", remoteCommit: "1".repeat(40), priorTip: repo.headSha });
		vi.spyOn(gitModule, "rangeDiffSha256").mockReturnValue("diff-sha-199");

		try {
			// First run: executes idempotent refresh + restart once, caches marker, yields on pending delivery BEFORE candidate freeze
			const res1 = await registeredExecute!("call-1", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res1.content[0]?.text).toContain("queued for delivery");
			expect(restartCount).toBe(1);
			expect(callLog.filter(c => c === "setExecutionState:service_refresh")).toHaveLength(1);
			expect(callLog).not.toContain("finalizeExecutionCandidate");
			expect(gitModule.dirtyPaths(repo.dir)).toContain("python/omp-work/src/omp_work/v1/store.py");

			// Second run: working tree is STILL dirty, but cached marker prevents a second refresh/restart
			pendingEvents = [];
			const res2 = await registeredExecute!("call-2", { action: "begin_execution_review", work: "OMP-199", body: "body" }, new AbortController().signal, () => {}, fakeCtx);
			expect(res2.content[0]?.text).toContain("Execution grant completed");
			expect(restartCount).toBe(1);
			expect(callLog.filter(c => c === "setExecutionState:service_refresh")).toHaveLength(1);
		} finally {
			repo.cleanup();
		}
	});
});
