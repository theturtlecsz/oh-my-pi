import { afterEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as customTools from "@oh-my-pi/pi-coding-agent/extensibility/custom-tools";
import * as extensions from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import type { ExtensionAPI, ExtensionFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { AgentLifecycleManager } from "@oh-my-pi/pi-coding-agent/registry/agent-lifecycle";
import { AgentRegistry, getAgentTombstonePath } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import * as sdk from "@oh-my-pi/pi-coding-agent/sdk";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import * as artifacts from "@oh-my-pi/pi-coding-agent/session/artifacts";
import { TOOL_EXECUTION_START_CUSTOM_TYPE } from "@oh-my-pi/pi-coding-agent/session/exit-diagnostics";
import { loadSessionFile } from "@oh-my-pi/pi-coding-agent/session/session-loader";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TaskTool } from "@oh-my-pi/pi-coding-agent/task";
import { loadBundledAgents } from "@oh-my-pi/pi-coding-agent/task/agents";
import * as discovery from "@oh-my-pi/pi-coding-agent/task/discovery";
import * as executor from "@oh-my-pi/pi-coding-agent/task/executor";
import { createPersistedSubagentReviverFactory } from "@oh-my-pi/pi-coding-agent/task/persisted-revive";
import {
	preparedEntryIds,
	readPreparationRecord,
	readTaskBinding,
	TASK_NATIVE_RESULT_READY,
	TASK_RESULT_PROCESSING_STARTED,
	taskRecoveryHash,
	taskResultRecoveryState,
} from "@oh-my-pi/pi-coding-agent/task/recovery";
import { TASK_SUBAGENT_LIFECYCLE_CHANNEL, type TaskToolDetails } from "@oh-my-pi/pi-coding-agent/task/types";
import * as outputMeta from "@oh-my-pi/pi-coding-agent/tools/output-meta";
import { ReadTool } from "@oh-my-pi/pi-coding-agent/tools/read";
import { TempDir, untilAborted } from "@oh-my-pi/pi-utils";
import assignment from "../../../python/omp-work/tests/fixtures/task-active-assignment.md" with { type: "text" };
import { createAssistantMessage, createInMemoryAuthStorage } from "./helpers/agent-session-setup";

interface WireRequest {
	model: string;
	messages: Array<{
		role: string;
		content: string;
		tool_call_id?: string;
		tool_calls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>;
	}>;
	tools?: Array<{ function: { name: string } }>;
}

describe("native task recovery session integration", () => {
	const cleanups: Array<() => Promise<void>> = [];
	afterEach(async () => {
		for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
		vi.restoreAllMocks();
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
	});

	async function fixture(
		multipleCalls = false,
		startRun = true,
		preparedContext = false,
		assistantNarration = false,
		testOptions: { maxRuntimeMs?: number; outputSchema?: unknown } = {},
	) {
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		// Only discovery is confined: executable tools, sessions and task driver are real.
		vi.spyOn(discovery, "discoverAgents").mockResolvedValue({ agents: loadBundledAgents(), projectAgentsDir: null });
		vi.spyOn(extensions, "discoverExtensionPaths").mockResolvedValue([]);
		vi.spyOn(customTools, "discoverCustomToolPaths").mockResolvedValue([]);
		const root = TempDir.createSync(path.join(os.tmpdir(), "omp-native-task-recovery-"));
		await Bun.write(path.join(root.path(), "result.txt"), "before\n");
		const auth = createInMemoryAuthStorage();
		const models = new ModelRegistry(auth, path.join(root.path(), "models.yml"));
		const childReached = Promise.withResolvers<void>();
		const parentReached = Promise.withResolvers<void>();
		const releaseChild = Promise.withResolvers<void>();
		const releaseParent = Promise.withResolvers<void>();
		const calls: WireRequest[] = [];
		const call = (id: string, name: string, args: unknown) => ({
			index: 0,
			id,
			type: "function",
			function: { name, arguments: JSON.stringify(args) },
		});
		const server = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			async fetch(request) {
				const input = (await request.json()) as WireRequest;
				calls.push(input);
				const results = input.messages.filter(message => message.role === "tool");
				let delta: {
					content?: string;
					tool_calls?: Array<{
						index: number;
						id: string;
						type: string;
						function: { name: string; arguments: string };
					}>;
				};
				if (input.model === "child") {
					if (results.length === 0) {
						childReached.resolve();
						await releaseChild.promise;
						delta = { tool_calls: [call("child-read", "read", { path: "result.txt" })] };
					} else {
						delta = {
							tool_calls: [
								call("child-yield", "yield", {
									result: { data: { path: "result.txt", observed: results.at(-1)!.content } },
								}),
							],
						};
					}
				} else if (input.tools?.some(tool => tool.function.name === "task")) {
					if (results.length === 0) {
						const task = call("original-task", "task", {
							name: "Child",
							agent: "task",
							task: assignment,
							...(testOptions.outputSchema !== undefined
								? { outputSchema: testOptions.outputSchema, schemaMode: "strict" }
								: {}),
						});
						delta = {
							...(assistantNarration ? { content: assignment } : {}),
							tool_calls: multipleCalls
								? [call("parent-read", "read", { path: "result.txt" }), { ...task, index: 1 }]
								: [task],
						};
					} else {
						parentReached.resolve();
						await releaseParent.promise;
						delta = { content: "Parent received the actual task result." };
					}
				} else delta = { content: "Task fixture" };
				const chunk = {
					id: "task-fixture",
					object: "chat.completion.chunk",
					created: 1,
					model: input.model,
					choices: [{ index: 0, delta, finish_reason: null }],
				};
				const finish = {
					...chunk,
					choices: [{ index: 0, delta: {}, finish_reason: delta.tool_calls ? "tool_calls" : "stop" }],
				};
				return new Response(
					`data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify(finish)}\n\ndata: [DONE]\n\n`,
					{ headers: { "Content-Type": "text/event-stream" } },
				);
			},
		});
		const provider = `task-recovery-${crypto.randomUUID()}`;
		auth.setRuntimeApiKey(provider, "local-test");
		models.registerProvider(provider, {
			baseUrl: `${server.url}v1`,
			api: "openai-completions",
			apiKey: "local-test",
			models: ["parent", "child"].map(id => ({
				id,
				name: id,
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 200000,
				maxTokens: 2048,
			})),
		});
		const settings = Settings.isolated({
			...(testOptions.maxRuntimeMs !== undefined ? { "task.maxRuntimeMs": testOptions.maxRuntimeMs } : {}),
			"tools.xdev": false,
			"async.enabled": false,
			"task.batch": false,
			"task.isolation.mode": "none",
			"task.prewalk": false,
			"task.maxRecursionDepth": 1,
			"advisor.enabled": false,
			"compaction.enabled": false,
			"retry.enabled": false,
			modelRoles: { default: `${provider}/parent`, smol: `${provider}/parent`, task: `${provider}/child` },
		});
		const manager = SessionManager.create(root.path(), path.join(root.path(), "sessions"));
		let session: AgentSession | undefined;
		let run: Promise<unknown> | undefined;
		cleanups.push(async () => {
			releaseChild.resolve();
			releaseParent.resolve();
			await run?.catch(() => {});
			await session?.dispose();
			server.stop(true);
			models.unregisterProvider(provider);
			auth.close();
			root.removeSync();
		});
		const createParent = (journal: SessionManager, extraExtensions: ExtensionFactory[] = []) =>
			createAgentSession({
				cwd: root.path(),
				agentDir: root.path(),
				sessionManager: journal,
				authStorage: auth,
				modelRegistry: models,
				settings,
				disableExtensionDiscovery: true,
				extensions: [
					...extraExtensions,
					...(preparedContext
						? [
								(pi: ExtensionAPI) => {
									pi.on("before_agent_start", () => ({
										message: { customType: "work-digest", content: assignment, display: false },
									}));
								},
							]
						: []),
				],
				preloadedCustomToolPaths: [],
				skills: [],
				contextFiles: [],
				promptTemplates: [],
				slashCommands: [],
				rules: [],
				enableMCP: false,
				enableLsp: false,
				skipPythonPreflight: true,
				toolNames: ["task", "read"],
			});
		({ session } = await createParent(manager));
		await initializeExtensions(session, {
			reportSendError: (_action, error) => {
				throw error;
			},
			reportRuntimeError: error => {
				throw new Error(error.error);
			},
		});
		if (startRun)
			run = session.sendCustomMessage(
				{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
				{ triggerTurn: true },
			);
		run?.catch(() => {});
		const takeSnapshot = async (owner: AgentSession = session!) => {
			const journalManager = owner.sessionManager;
			const binding = journalManager
				.getBranch()
				.map(readTaskBinding)
				.find(value => value !== undefined);
			if (!binding) throw new Error("Runtime did not write an original task binding");
			const child = AgentRegistry.global().get(binding.child.registryId)?.session;
			await child?.sessionManager.flush();
			await journalManager.flush();
			const file = journalManager.getSessionFile()!;
			const artifactsDir = journalManager.getArtifactsDir()!;
			const artifacts = new Map<string, Uint8Array>();
			for await (const name of new Bun.Glob("**/*").scan({ cwd: artifactsDir, onlyFiles: true, dot: true }))
				artifacts.set(name, new Uint8Array(await Bun.file(path.join(artifactsDir, name)).arrayBuffer()));
			return { binding, file, artifactsDir, journal: new Uint8Array(await Bun.file(file).arrayBuffer()), artifacts };
		};
		const replay = async (extraExtensions: ExtensionFactory[] = []) => {
			await untilAborted(AbortSignal.timeout(10000), childReached.promise);
			const snapshot = await takeSnapshot();
			releaseChild.resolve();
			releaseParent.resolve();
			await run;
			await session!.dispose();
			await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
			for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
			await Bun.write(snapshot.file, snapshot.journal);
			AgentLifecycleManager.resetGlobalForTests();
			AgentRegistry.resetGlobalForTests();
			const journal = await SessionManager.open(snapshot.file);
			const created = await createParent(journal, extraExtensions);
			session = created.session;
			AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
				createPersistedSubagentReviverFactory({
					session,
					authStorage: auth,
					modelRegistry: models,
					settings,
					enableLsp: false,
					eventBus: created.eventBus,
				}),
				0,
			);
			await initializeExtensions(session, { reportSendError: () => {}, reportRuntimeError: () => {} });
			return { snapshot, journal, restored: session };
		};
		return {
			replay,
			root,
			session,
			manager,
			calls,
			childReached,
			parentReached,
			releaseChild,
			releaseParent,
			run,
			takeSnapshot,
			createParent,
			auth,
			models,
			settings,
			setCurrentSession: (next: AgentSession) => {
				session = next;
			},
		};
	}

	async function completedReadyFixture() {
		const f = await fixture(false, true, true);
		const { snapshot: initial, journal, restored } = await f.replay();
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		cleanups.push(async () => {
			release.resolve();
		});
		let held = false;
		const refusals: unknown[] = [];
		restored.requestPersistedTurnContinuation({
			sessionId: restored.sessionId,
			entryId: initial.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			onRefused: reason => refusals.push(reason),
			validateDispatch: async () => {
				const state = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), initial.binding);
				if (!held && state.ready && !state.processing) {
					held = true;
					reached.resolve();
					await release.promise;
				}
				return { ok: true };
			},
		});
		await untilAborted(AbortSignal.timeout(10000), reached.promise);
		const state = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), initial.binding);
		if (!state.ready || state.processing) throw new Error("Native ready boundary is not unprocessed");
		const snapshot = await f.takeSnapshot(restored);
		const child = AgentRegistry.global().get(initial.binding.child.registryId)?.session;
		release.resolve();
		await untilAborted(AbortSignal.timeout(10000), restored.waitForIdle());
		expect(refusals).toEqual([]);
		await restored.dispose();
		await child?.dispose();
		// Replay an exact, runtime-written checkpoint captured before processing.
		await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
		for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
		await Bun.write(snapshot.file, snapshot.journal);
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		return { f, snapshot, ready: state.ready };
	}

	it("writes original parent/child binding before first child response and attaches the real yield result", async () => {
		const f = await fixture();
		await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		await f.manager.flush();
		const bindings = f.manager
			.getBranch()
			.map(readTaskBinding)
			.filter(binding => binding !== undefined);
		expect(bindings).toHaveLength(1);
		const binding = bindings[0];
		expect(binding.call.toolCallId).toBe("original-task");
		expect(binding.call.sessionId).toBe(f.manager.getSessionId());
		const child = AgentRegistry.global().get(binding.child.registryId)?.session;
		if (!child) throw new Error("Actual task child did not register its live session");
		await child.sessionManager.flush();
		const init = child.sessionManager.getEntry(binding.child.initEntryId);
		expect(init?.type === "session_init" && init.taskCall).toEqual(binding.call);
		expect(binding.contract.args.task).toBe(assignment.trim());
		const childRecords = child.sessionManager
			.getBranch()
			.map(readPreparationRecord)
			.filter(record => record?.taskBindingId === binding.call.bindingId);
		expect(childRecords).toHaveLength(1);
		preparedEntryIds(
			child.sessionManager.getBranch(),
			child.sessionId,
			childRecords[0]!.anchorEntryId,
			binding.call.bindingId,
		);
		const anchor = f.manager.getEntry(binding.call.promptEntryId);
		expect(anchor?.type).toBe("custom_message");
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.parentReached.promise);
		await f.manager.flush();
		const result = f.manager
			.getBranch()
			.find(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			);
		if (result?.type !== "message" || result.message.role !== "toolResult")
			throw new Error("Actual task did not produce its original parent result");
		expect(result.taskResult).toEqual({
			bindingId: binding.call.bindingId,
			contractSha256: binding.contractSha256,
			toolCallId: "original-task",
			childSessionId: binding.child.sessionId,
		});
		expect(result.message.isError).toBe(false);
		expect(JSON.stringify(result.message.content)).toContain("before");
		expect(binding.contractSha256).toBe(taskRecoveryHash(binding.contract));
		f.releaseParent.resolve();
		await f.run;
	}, 30000);
	it("normal read-plus-task turn still executes without automatic recovery eligibility", async () => {
		const f = await fixture(true);
		await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		expect(f.manager.getBranch().map(readTaskBinding).filter(Boolean)).toHaveLength(0);
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.parentReached.promise);
		await f.manager.flush();
		const result = f.manager
			.getBranch()
			.find(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			);
		if (result?.type !== "message" || result.message.role !== "toolResult")
			throw new Error("Normal task result is missing");
		expect(result.taskResult).toBeUndefined();
		expect(JSON.stringify(result.message.content)).toContain("before");
		f.releaseParent.resolve();
		await f.run;
	}, 30000);
	it.each(["call-only", "text-and-call"] as const)(
		"cold replay restores %s assistant and dispatches parent after real read/yield",
		async turnShape => {
			const assistantNarration = turnShape === "text-and-call";
			const f = await fixture(false, true, true, assistantNarration);
			await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
			const snapshot = await f.takeSnapshot();
			// Component replay of exact runtime-written snapshots. Installed tests own
			// process death; this test neither invents nor edits binding/join fields.
			f.releaseChild.resolve();
			f.releaseParent.resolve();
			await f.run;
			await f.session.dispose();
			await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
			for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
			await Bun.write(snapshot.file, snapshot.journal);
			AgentLifecycleManager.resetGlobalForTests();
			AgentRegistry.resetGlobalForTests();
			const journal = await SessionManager.open(snapshot.file);
			const { session: restored, eventBus } = await f.createParent(journal);
			f.setCurrentSession(restored);
			const projected = restored.messages.filter(message => message.role === "assistant");
			expect(projected).toHaveLength(assistantNarration ? 1 : 0);
			if (assistantNarration) expect(projected[0].content).toEqual([{ type: "text", text: assignment }]);
			AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
				createPersistedSubagentReviverFactory({
					session: restored,
					authStorage: f.auth,
					modelRegistry: f.models,
					settings: f.settings,
					enableLsp: false,
					eventBus,
				}),
				0,
			);
			await initializeExtensions(restored, { reportSendError: () => {}, reportRuntimeError: () => {} });
			const before = f.calls.length;
			const refusals: unknown[] = [];
			expect(
				restored.requestPersistedTurnContinuation({
					sessionId: restored.sessionId,
					entryId: snapshot.binding.call.promptEntryId,
					expectedLeafId: journal.getLeafId()!,
					recoverSynchronousTask: true,
					validateDispatch: async () => ({ ok: true }),
					onRefused: reason => refusals.push(reason),
				}),
			).toEqual({ status: "scheduled" });
			await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
			await journal.flush();
			expect(refusals).toEqual([]);
			expect(f.calls.slice(before).filter(call => call.model === "child")).toHaveLength(2);
			const results = journal
				.getBranch()
				.filter(
					entry =>
						entry.type === "message" &&
						entry.message.role === "toolResult" &&
						entry.message.toolCallId === "original-task",
				);
			expect(results).toHaveLength(1);
			const result = results[0];
			if (result.type !== "message" || result.message.role !== "toolResult")
				throw new Error("Original task result missing");
			expect(result.taskResult?.childSessionId).toBe(snapshot.binding.child.sessionId);
			expect(JSON.stringify(result.message.content)).toContain("before");
			expect(AgentRegistry.global().get("Child-2")).toBeUndefined();

			const parentRequests = f.calls
				.slice(before)
				.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task"));
			expect(parentRequests).toHaveLength(1);
			const wire = parentRequests[0].messages;
			const originalCalls = wire
				.flatMap(message => message.tool_calls ?? [])
				.filter(call => call.id === "original-task");
			expect(originalCalls).toHaveLength(1);
			expect(JSON.parse(originalCalls[0].function.arguments)).toEqual({
				name: "Child",
				agent: "task",
				task: assignment,
			});
			const callIndex = wire.findIndex(message => message.tool_calls?.some(call => call.id === "original-task"));
			const resultIndex = wire.findIndex(message => message.tool_call_id === "original-task");
			expect(resultIndex).toBeGreaterThan(callIndex);
			expect(wire.filter(message => message.tool_call_id === "original-task")).toHaveLength(1);
			expect(
				journal
					.getBranch()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "assistant" &&
							entry.message.stopReason === "error",
					),
			).toHaveLength(0);
		},
		30000,
	);

	it("durable original task result continues parent without native recovery or child reopen", async () => {
		const f = await fixture();
		await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.parentReached.promise);
		const snapshot = await f.takeSnapshot();
		f.releaseParent.resolve();
		await f.run;
		await f.session.dispose();
		await Bun.write(snapshot.file, snapshot.journal);
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		const journal = await SessionManager.open(snapshot.file);
		const { session: restored } = await f.createParent(journal);
		f.setCurrentSession(restored);
		await initializeExtensions(restored, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const native = vi.spyOn(TaskTool.prototype, "recoverPersistedCall");
		const revival = vi.spyOn(AgentLifecycleManager.global(), "ensureLive");
		const opens = vi.spyOn(SessionManager, "open");
		const before = f.calls.length;
		const refusals: unknown[] = [];
		restored.requestPersistedTurnContinuation({
			sessionId: restored.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), restored.waitForIdle());
		expect(refusals).toEqual([]);
		expect(native).not.toHaveBeenCalled();
		expect(revival).not.toHaveBeenCalled();
		expect(opens.mock.calls.filter(args => args[0] === snapshot.binding.child.sessionFile)).toHaveLength(0);
		expect(f.calls.slice(before).filter(call => call.model === "child")).toHaveLength(0);
		expect(
			f.calls.slice(before).filter(call => call.tools?.some(tool => tool.function.name === "task")),
		).toHaveLength(1);
	}, 30000);
	it("legacy unbound task history refuses before any native recovery or child reopen", async () => {
		const f = await fixture(false, false);
		const anchor = f.manager.appendCustomMessageEntry("work-execute", assignment, false, {}, "agent");
		const args = { name: "RecoveryTask", agent: "task", task: assignment };
		const assistant = createAssistantMessage("");
		assistant.content = [{ type: "toolCall", id: "task-active-call", name: "task", arguments: args }];
		assistant.stopReason = "toolUse";
		f.manager.appendMessage(assistant);
		f.manager.appendCustomEntry(TOOL_EXECUTION_START_CUSTOM_TYPE, {
			toolCallId: "task-active-call",
			toolName: "task",
			args,
			startedAt: Date.now(),
		});
		// Legacy component shape from R3: no forward binding, reverse taskCall or core preparation record.
		const child = SessionManager.create(f.root.path(), path.join(f.root.path(), "legacy-child"));
		child.appendSessionInit({ systemPrompt: assignment, task: assignment, tools: ["read", "yield"], agent: "task" });
		child.appendMessage({
			role: "user",
			content: [{ type: "text", text: assignment }],
			attribution: "agent",
			timestamp: Date.now(),
		});
		child.appendCustomMessageEntry("work-digest", assignment, false, {}, "agent");
		await child.ensureOnDisk();
		await child.close();
		const childFile = child.getSessionFile()!;
		const childBytes = await Bun.file(childFile).bytes();
		await f.manager.ensureOnDisk();
		const file = f.manager.getSessionFile()!;
		await f.session.dispose();
		const journal = await SessionManager.open(file);
		const { session: restored } = await f.createParent(journal);
		f.setCurrentSession(restored);
		await initializeExtensions(restored, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const native = vi.spyOn(TaskTool.prototype, "recoverPersistedCall");
		const revival = vi.spyOn(AgentLifecycleManager.global(), "ensureLive");
		const before = f.calls.length;
		const outcome = restored.requestPersistedTurnContinuation({
			sessionId: restored.sessionId,
			entryId: anchor,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
		});
		expect(outcome).toMatchObject({ status: "refused", code: "unsafe-suffix" });
		if (outcome.status === "refused") expect(outcome.reason).toContain("no unique durable parent/child binding");
		expect(native).not.toHaveBeenCalled();
		expect(revival).not.toHaveBeenCalled();
		expect(f.calls).toHaveLength(before);
		expect(await Bun.file(childFile).bytes()).toEqual(childBytes);
	});

	it.each(["startup-abort", "move-during-authority", "registry-replacement"] as const)(
		"cold child ownership refusal prevents original result: %s",
		async mode => {
			const f = await fixture();
			const { snapshot, journal, restored } = await f.replay();
			const before = f.calls.length;
			const reads = vi.spyOn(ReadTool.prototype, "execute");
			const reached = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			cleanups.push(async () => {
				release.resolve();
			});
			let startupAborted = false;
			if (mode === "startup-abort") {
				const actualCreate = sdk.createAgentSession;
				vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
					actualCreate({
						...options,
						extensions: [
							...(options?.extensions ?? []),
							pi => {
								pi.on("session_start", async (_event, ctx) => {
									await ctx.abort();
									startupAborted = true;
								});
							},
						],
					}),
				);
			}
			let held = false;
			const refusals: unknown[] = [];
			restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				onRefused: reason => refusals.push(reason),
				validateDispatch: async () => {
					const child = AgentRegistry.global().get(snapshot.binding.child.registryId)?.session;
					const atBoundary =
						mode === "move-during-authority"
							? Boolean(child)
							: mode === "registry-replacement" && child?.agent.state.pendingToolCalls.has("child-read");
					if (!held && atBoundary) {
						held = true;
						reached.resolve();
						await release.promise;
					}
					return { ok: true };
				},
			});
			let displaced: AgentSession | undefined;
			if (mode !== "startup-abort") {
				await untilAborted(AbortSignal.timeout(10000), reached.promise);
				const registry = AgentRegistry.global();
				const ref = registry.get(snapshot.binding.child.registryId)!;
				const child = ref.session!;
				if (mode === "move-during-authority") {
					await expect(child.moveSession(path.join(f.root.path(), "foreign-workspace"))).rejects.toThrow();
					expect(child.sessionManager.getCwd()).toBe(snapshot.binding.child.cwd);
				} else {
					displaced = child;
					registry.register({ ...ref, session: null, status: "parked" });
				}
				release.resolve();
			}
			await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
			await journal.flush();
			expect(refusals).toHaveLength(1);
			if (mode === "startup-abort") expect(startupAborted).toBe(true);
			expect(reads).not.toHaveBeenCalled();
			expect(
				journal
					.getBranch()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-task",
					),
			).toHaveLength(0);
			expect(f.calls.slice(before).filter(call => call.model === "child")).toHaveLength(
				mode === "registry-replacement" ? 1 : 0,
			);
			if (displaced) {
				expect(AgentRegistry.global().get(snapshot.binding.child.registryId)?.session).toBeNull();
				await displaced.dispose();
			}
		},
		30000,
	);
	it("cached native-ready result runs parent processing once with durable claim and immutable raw payload", async () => {
		const { f, snapshot, ready } = await completedReadyFixture();
		const journal = await SessionManager.open(snapshot.file);
		const effectFile = path.join(f.root.path(), "result-hook-effect.txt");
		const rawPayload = ready.record.payloadJson;
		let hookCalls = 0;
		let claimAtHook: string | undefined;
		const { session: cached, eventBus } = await f.createParent(journal, [
			pi => {
				pi.on("tool_result", async event => {
					if (event.toolName !== "task") return;
					const persisted = await loadSessionFile(snapshot.file);
					const claims = persisted.entries.filter(
						entry => entry.type === "custom" && entry.customType === TASK_RESULT_PROCESSING_STARTED,
					);
					expect(claims).toHaveLength(1);
					claimAtHook = claims[0].type === "custom" ? claims[0].id : undefined;
					hookCalls++;
					await Bun.write(effectFile, String(hookCalls));
					const details = event.details as TaskToolDetails;
					details.results[0].output = "Result hook mutated its own fresh processor input";
					return { details: { hookReplaced: true } };
				});
			},
		]);
		f.setCurrentSession(cached);
		await initializeExtensions(cached, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const nativeRun = vi.spyOn(executor, "runPersistedTask");
		const revival = vi.spyOn(AgentLifecycleManager.global(), "ensureLive");
		let lifecycleEvents = 0;
		const unsubscribe = eventBus.on(TASK_SUBAGENT_LIFECYCLE_CHANNEL, () => {
			lifecycleEvents++;
		});
		cleanups.push(async () => {
			unsubscribe();
		});
		const before = f.calls.length;
		const childBefore = await Bun.file(snapshot.binding.child.sessionFile).bytes();
		const refusals: unknown[] = [];
		cached.requestPersistedTurnContinuation({
			sessionId: cached.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(15000), cached.waitForIdle());
		await journal.flush();
		expect(refusals).toEqual([]);
		expect(nativeRun).not.toHaveBeenCalled();
		expect(revival).not.toHaveBeenCalled();
		expect(lifecycleEvents).toBe(0);
		expect(f.calls.slice(before).filter(call => call.model === "child")).toHaveLength(0);
		const parentRequests = f.calls
			.slice(before)
			.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task"));
		expect(parentRequests).toHaveLength(1);
		const wire = parentRequests[0].messages;
		const originalCalls = wire
			.flatMap(message => message.tool_calls ?? [])
			.filter(call => call.id === "original-task");
		expect(originalCalls).toHaveLength(1);
		expect(JSON.parse(originalCalls[0].function.arguments)).toEqual({
			name: "Child",
			agent: "task",
			task: assignment,
		});
		const callIndex = wire.findIndex(message => message.tool_calls?.some(call => call.id === "original-task"));
		const resultIndex = wire.findIndex(message => message.tool_call_id === "original-task");
		expect(resultIndex).toBeGreaterThan(callIndex);
		expect(wire.filter(message => message.tool_call_id === "original-task")).toHaveLength(1);
		expect(wire[resultIndex].content).toContain("before");
		expect(hookCalls).toBe(1);
		expect(await Bun.file(effectFile).text()).toBe("1");
		expect(await Bun.file(snapshot.binding.child.sessionFile).bytes()).toEqual(childBefore);
		const checkpoint = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), snapshot.binding);
		expect(checkpoint.ready?.record.payloadJson).toBe(rawPayload);
		expect(checkpoint.ready?.sha256).toBe(ready.sha256);
		const result = journal
			.getBranch()
			.find(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			);
		if (result?.type !== "message" || result.message.role !== "toolResult")
			throw new Error("Cached original task result missing");
		expect(result.message.details).toEqual({ hookReplaced: true });
		if (!claimAtHook || !checkpoint.processing) throw new Error("Actual durable processing identity missing");
		expect(result.taskResult?.completion).toEqual({
			readyEntryId: ready.entryId,
			readySha256: ready.sha256,
			processingEntryId: claimAtHook,
			processingSha256: checkpoint.processing.sha256,
		});
		expect(
			journal.getEntries().filter(entry => entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY),
		).toHaveLength(1);
	}, 30000);
	it.each(["active-start", "branch-before-start", "discard-start"] as const)(
		"uncertain result hook effect is never replayed after %s",
		async mode => {
			const { f, snapshot, ready } = await completedReadyFixture();
			const journal = await SessionManager.open(snapshot.file);
			const reached = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			cleanups.push(async () => {
				release.resolve();
			});
			const effectFile = path.join(f.root.path(), "uncertain-hook-effect.txt");
			let effects = 0;
			const hook: ExtensionFactory = pi => {
				pi.on("tool_result", async event => {
					if (event.toolName !== "task") return;
					effects++;
					await Bun.write(effectFile, String(effects));
					reached.resolve();
					await release.promise;
				});
			};
			const { session: processing } = await f.createParent(journal, [hook]);
			f.setCurrentSession(processing);
			await initializeExtensions(processing, { reportSendError: () => {}, reportRuntimeError: () => {} });
			processing.requestPersistedTurnContinuation({
				sessionId: processing.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
			});
			await untilAborted(AbortSignal.timeout(10000), reached.promise);
			const state = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), snapshot.binding);
			expect(state.processing?.record.readyEntryId).toBe(ready.entryId);
			const started = await f.takeSnapshot(processing);
			const aborting = processing.abort();
			release.resolve();
			await processing.waitForIdle();
			await aborting;
			await processing.dispose();
			await Bun.write(started.file, started.journal);
			AgentLifecycleManager.resetGlobalForTests();
			AgentRegistry.resetGlobalForTests();
			const retained = await SessionManager.open(started.file);
			if (mode === "discard-start") await retained.discardEntryDurably(state.processing!.entryId);
			if (mode !== "active-start") {
				retained.branch(ready.entryId);
				retained.appendCustomEntry("selected-before-result-processing", {});
			}
			await retained.close();
			const reopened = await SessionManager.open(started.file);
			const { session: restored } = await f.createParent(reopened, [hook]);
			f.setCurrentSession(restored);
			await initializeExtensions(restored, { reportSendError: () => {}, reportRuntimeError: () => {} });
			const native = vi.spyOn(TaskTool.prototype, "recoverPersistedCall");
			const before = f.calls.length;
			const outcome = restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: reopened.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
			});
			expect(outcome.status).toBe("refused");
			if (outcome.status !== "refused") throw new Error("Uncertain processing was admitted again");
			if (mode === "discard-start") expect(outcome.reason).toContain("discarded");
			else expect(outcome.code).toBe("task-result-processing-incomplete");
			expect(native).not.toHaveBeenCalled();
			expect(f.calls).toHaveLength(before);
			expect(effects).toBe(1);
			expect(await Bun.file(effectFile).text()).toBe("1");
			expect(
				reopened
					.getBranch()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-task",
					),
			).toHaveLength(0);
		},
		30000,
	);
	it.each(["owner-input", "policy", "registry", "child-file", "output-file", "tombstone", "revival"] as const)(
		"cached processing refuses %s arriving during final authority await",
		async change => {
			const { f, snapshot, ready } = await completedReadyFixture();
			const journal = await SessionManager.open(snapshot.file);
			let hooks = 0;
			const { session: cached, eventBus } = await f.createParent(journal, [
				pi => {
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			]);
			f.setCurrentSession(cached);
			if (change === "revival")
				AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
					createPersistedSubagentReviverFactory({
						session: cached,
						authStorage: f.auth,
						modelRegistry: f.models,
						settings: f.settings,
						enableLsp: false,
						eventBus,
					}),
					0,
				);

			await initializeExtensions(cached, { reportSendError: () => {}, reportRuntimeError: () => {} });
			const reached = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			cleanups.push(async () => {
				release.resolve();
			});
			let held = false;
			const refusals: unknown[] = [];
			const before = f.calls.length;
			cached.requestPersistedTurnContinuation({
				sessionId: cached.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				onRefused: reason => refusals.push(reason),
				validateDispatch: async () => {
					const state = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), snapshot.binding);
					if (!held && state.processing) {
						held = true;
						reached.resolve();
						await release.promise;
					}
					return { ok: true };
				},
			});
			await untilAborted(AbortSignal.timeout(10000), reached.promise);
			const registry = AgentRegistry.global();
			const originalRef = registry.get(snapshot.binding.child.registryId)!;
			if (change === "owner-input") await cached.sendUserMessage("Owner correction", { deliverAs: "followUp" });
			if (change === "policy") cached.settings.set("tools.approval", { task: "deny" });
			if (change === "registry") registry.register({ ...originalRef, session: null, status: "parked" });
			if (change === "revival") {
				const attaches = vi.spyOn(registry, "attachSession");
				await expect(AgentLifecycleManager.global().ensureLive(originalRef.id)).rejects.toThrow(
					"Completed task cannot be revived",
				);
				expect(attaches.mock.calls.filter(call => call[0] === originalRef.id)).toHaveLength(0);
				expect(registry.get(originalRef.id)).toBe(originalRef);
				expect(originalRef.session).toBeNull();
			}

			if (change === "child-file") {
				const replacement = `${snapshot.binding.child.sessionFile}.replacement`;
				await Bun.write(replacement, await Bun.file(snapshot.binding.child.sessionFile).bytes());
				await fs.rename(replacement, snapshot.binding.child.sessionFile);
			}
			if (change === "output-file")
				await Bun.write(
					ready.record.output.path,
					(await Bun.file(ready.record.output.path).text()).replace("before", "tamper"),
				);
			if (change === "tombstone") await Bun.write(getAgentTombstonePath(snapshot.binding.child.sessionFile), "");
			release.resolve();
			await untilAborted(AbortSignal.timeout(15000), cached.waitForIdle());
			expect(refusals).toHaveLength(1);
			expect(hooks).toBe(0);
			expect(f.calls).toHaveLength(before);
			expect(
				journal
					.getBranch()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-task",
					),
			).toHaveLength(0);
			expect(
				taskResultRecoveryState(journal.getEntries(), journal.getBranch(), snapshot.binding).processing,
			).toBeDefined();
			if (change === "owner-input") expect(cached.getQueuedMessages().followUp).toEqual(["Owner correction"]);
			if (change === "registry") expect(registry.get(originalRef.id)).not.toBe(originalRef);
			if (change === "tombstone")
				expect(await Bun.file(getAgentTombstonePath(snapshot.binding.child.sessionFile)).exists()).toBe(true);
		},
		30000,
	);
	it.each(["ready", "processing", "result"] as const)(
		"native result journal lease does not adopt a foreign leaf during %s flush",
		async stage => {
			const f = await fixture();
			let hooks = 0;
			const { snapshot, journal, restored } = await f.replay([
				pi => {
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			]);
			const reached = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			cleanups.push(async () => {
				release.resolve();
			});
			const flush = journal.flush.bind(journal);
			let held = false;
			vi.spyOn(journal, "flush").mockImplementation(async () => {
				await flush();
				const leaf = journal.getEntry(journal.getLeafId()!);
				const selected =
					stage === "result"
						? leaf?.type === "message" &&
							leaf.message.role === "toolResult" &&
							leaf.message.toolCallId === "original-task"
						: leaf?.type === "custom" &&
							leaf.customType ===
								(stage === "ready" ? TASK_NATIVE_RESULT_READY : TASK_RESULT_PROCESSING_STARTED);
				if (!held && selected) {
					held = true;
					reached.resolve();
					await release.promise;
				}
			});
			const before = f.calls.length;
			const refusals: unknown[] = [];
			restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
				onRefused: reason => refusals.push(reason),
			});
			await untilAborted(AbortSignal.timeout(10000), reached.promise);
			const foreign = journal.appendCustomEntry("foreign-owner-leaf", { owner: "new" });
			release.resolve();
			await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
			expect(refusals).toHaveLength(1);
			expect(journal.getLeafId()).toBe(foreign);
			expect(hooks).toBe(stage === "result" ? 1 : 0);
			expect(
				f.calls
					.slice(before)
					.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task")),
			).toHaveLength(0);
			expect(
				journal
					.getBranch()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-task",
					),
			).toHaveLength(stage === "result" ? 1 : 0);
		},
		30000,
	);

	it.each([false, true])(
		"uncertified completed child remains refused after rewind: %s",
		async rewind => {
			const f = await fixture();
			await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
			const unanswered = await f.takeSnapshot();
			const originalChild = AgentRegistry.global().get(unanswered.binding.child.registryId)!.session!;
			f.releaseChild.resolve();
			f.releaseParent.resolve();
			await f.run;
			expect(
				f.manager
					.getEntries()
					.filter(entry => entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY),
			).toHaveLength(0);
			await f.session.dispose();
			await originalChild.dispose();
			// The parent prefix was genuinely captured before its result. Keep the actual
			// ordinary first-run child's completed journal; never manufacture a certificate.
			await Bun.write(unanswered.file, unanswered.journal);
			AgentLifecycleManager.resetGlobalForTests();
			AgentRegistry.resetGlobalForTests();
			const child = await SessionManager.open(unanswered.binding.child.sessionFile);
			if (rewind) {
				const preparation = child
					.getBranch()
					.find(entry => entry.type === "custom" && entry.customType === "prompt-preparation");
				if (!preparation) throw new Error("Original child preparation missing");
				child.branch(preparation.id);
				child.appendCustomEntry("selected-unanswered-child-branch", {});
				expect(
					child.getBranch().some(entry => entry.type === "message" && entry.message.role === "toolResult"),
				).toBe(false);
			}
			expect(child.getEntries().some(entry => entry.type === "message" && entry.message.role === "toolResult")).toBe(
				true,
			);
			await child.close();
			const journal = await SessionManager.open(unanswered.file);
			const { session: restored } = await f.createParent(journal);
			f.setCurrentSession(restored);
			await initializeExtensions(restored, { reportSendError: () => {}, reportRuntimeError: () => {} });
			const revive = vi.spyOn(AgentLifecycleManager.global(), "ensureLive");
			const before = f.calls.length;
			const refusals: Array<{ reason: string }> = [];
			restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: unanswered.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
				onRefused: reason => refusals.push(reason),
			});
			await untilAborted(AbortSignal.timeout(10000), restored.waitForIdle());
			expect(refusals).toHaveLength(1);
			expect(refusals[0].reason).toContain("Child already has assistant/tool/effect history");
			expect(revive).not.toHaveBeenCalled();
			expect(f.calls).toHaveLength(before);
		},
		30000,
	);
	it("native ready append refuses a child changed after its original settled lease", async () => {
		const f = await fixture();
		const { snapshot, journal, restored } = await f.replay();
		const realRun = executor.runPersistedTask;
		let laterEntry: string | undefined;
		vi.spyOn(executor, "runPersistedTask").mockImplementation(options =>
			realRun({
				...options,
				onNativeResult: async (result, output) => {
					laterEntry = options.session.sessionManager.appendMessage({
						role: "user",
						content: [{ type: "text", text: assignment }],
						attribution: "user",
						timestamp: Date.now(),
					});
					await options.session.sessionManager.flush();
					await options.onNativeResult(result, output);
				},
			}),
		);
		const refusals: Array<{ reason: string }> = [];
		const before = f.calls.length;
		restored.requestPersistedTurnContinuation({
			sessionId: restored.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
		await journal.flush();
		expect(refusals).toHaveLength(1);
		expect(refusals[0].reason).toContain("Settled child changed");
		const durable = await loadSessionFile(snapshot.file);
		expect(
			durable.entries.filter(
				entry =>
					entry.type === "custom" &&
					[TASK_NATIVE_RESULT_READY, TASK_RESULT_PROCESSING_STARTED].includes(entry.customType),
			),
		).toHaveLength(0);
		expect(
			durable.entries.filter(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			),
		).toHaveLength(0);
		expect(
			f.calls
				.slice(before)
				.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task")),
		).toHaveLength(0);
		const child = await loadSessionFile(snapshot.binding.child.sessionFile);
		expect(
			child.entries.some(
				entry =>
					entry.type === "message" &&
					entry.id === laterEntry &&
					entry.message.role === "user" &&
					entry.message.attribution === "user",
			),
		).toBe(true);
	}, 30000);
	it.each(["output-write", "ready-append", "ready-flush", "processing-flush", "result-append"] as const)(
		"native result persistence failure at %s prevents the next processing effect",
		async failure => {
			const f = await fixture();
			let hooks = 0;
			const effectFile = path.join(f.root.path(), "failed-result-hook-effect.txt");
			const { snapshot, journal, restored } = await f.replay([
				pi => {
					pi.on("tool_result", async event => {
						if (event.toolName === "task") {
							hooks++;
							await Bun.write(effectFile, String(hooks));
						}
					});
				},
			]);
			let failed = false;
			if (failure === "output-write") {
				const write = artifacts.writeArtifact;
				vi.spyOn(artifacts, "writeArtifact").mockImplementation(async (file, content) => {
					if (file.endsWith("/Child.md")) {
						failed = true;
						throw new Error("Fixture native output write failed");
					}
					return write(file, content);
				});
			}
			if (failure === "ready-append") {
				const append = journal.appendCustomEntry.bind(journal);
				vi.spyOn(journal, "appendCustomEntry").mockImplementation((type, data) => {
					if (type === TASK_NATIVE_RESULT_READY) {
						failed = true;
						throw new Error("Fixture ready append failed");
					}
					return append(type, data);
				});
			}
			if (failure === "ready-flush" || failure === "processing-flush") {
				const flush = journal.flush.bind(journal);
				vi.spyOn(journal, "flush").mockImplementation(async () => {
					const leaf = journal.getEntry(journal.getLeafId()!);
					if (
						!failed &&
						leaf?.type === "custom" &&
						leaf.customType ===
							(failure === "ready-flush" ? TASK_NATIVE_RESULT_READY : TASK_RESULT_PROCESSING_STARTED)
					) {
						failed = true;
						throw new Error(`Fixture ${failure} failed`);
					}
					await flush();
				});
			}
			if (failure === "result-append") {
				const append = journal.appendMessage.bind(journal);
				vi.spyOn(journal, "appendMessage").mockImplementation((message, ref) => {
					if (message.role === "toolResult" && message.toolCallId === "original-task") {
						failed = true;
						throw new Error("Fixture original result append failed");
					}
					return append(message, ref);
				});
			}
			const processing = vi.spyOn(outputMeta, "processToolResultOutput");
			const refusals: unknown[] = [];
			const before = f.calls.length;
			restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
				onRefused: reason => refusals.push(reason),
			});
			await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
			await journal.flush();
			expect(failed).toBe(true);
			expect(refusals).toHaveLength(1);
			expect(hooks).toBe(failure === "result-append" ? 1 : 0);
			expect(processing.mock.calls.filter(call => call[1] === "task")).toHaveLength(
				failure === "result-append" ? 1 : 0,
			);
			expect(
				f.calls
					.slice(before)
					.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task")),
			).toHaveLength(0);
			const persisted = await loadSessionFile(snapshot.file);
			expect(
				persisted.entries.filter(
					entry =>
						entry.type === "message" &&
						entry.message.role === "toolResult" &&
						entry.message.toolCallId === "original-task",
				),
			).toHaveLength(0);
			expect(
				restored.messages.filter(
					message => message.role === "toolResult" && message.toolCallId === "original-task",
				),
			).toHaveLength(0);
			expect(
				persisted.entries.filter(
					entry => entry.type === "custom" && entry.customType === TASK_RESULT_PROCESSING_STARTED,
				),
			).toHaveLength(failure === "processing-flush" || failure === "result-append" ? 1 : 0);
			if (failure === "output-write" || failure === "ready-append")
				expect(
					persisted.entries.filter(
						entry => entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY,
					),
				).toHaveLength(0);
			const child = await loadSessionFile(snapshot.binding.child.sessionFile);
			expect(
				child.entries.filter(
					entry =>
						entry.type === "message" &&
						entry.message.role === "toolResult" &&
						entry.message.toolName === "yield" &&
						!entry.message.isError,
				),
			).toHaveLength(1);
		},
		30000,
	);

	it.each(["timeout", "schema"] as const)(
		"successful durable yield does not certify final %s failure",
		async failure => {
			const f = await fixture(
				false,
				true,
				false,
				false,
				failure === "timeout"
					? { maxRuntimeMs: 3000 }
					: {
							outputSchema: {
								type: "object",
								properties: { path: { type: "string" }, observed: { type: "string" } },
								required: ["path", "observed"],
								additionalProperties: false,
							},
						},
			);
			const { snapshot, journal, restored } = await f.replay();
			const create = sdk.createAgentSession;
			let interceptedYield = false;
			vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
				create({
					...options,
					extensions: [
						...(options?.extensions ?? []),
						pi => {
							if (failure === "timeout")
								pi.on("tool_result", async event => {
									if (event.toolName === "yield") {
										interceptedYield = true;
										await Bun.sleep(3500);
									}
								});
							else
								pi.on("tool_result", event => {
									if (event.toolName !== "yield") return;
									interceptedYield = true;
									const details = event.details as { data: Record<string, unknown>; status: string };
									return { details: { ...details, data: { ...details.data, observed: 42 } } };
								});
						},
					],
				}),
			);
			const before = f.calls.length;
			const refusals: unknown[] = [];
			restored.requestPersistedTurnContinuation({
				sessionId: restored.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () => ({ ok: true }),
				onRefused: reason => refusals.push(reason),
			});
			await untilAborted(AbortSignal.timeout(15000), restored.waitForIdle());
			await journal.flush();
			expect(interceptedYield).toBe(true);
			expect(refusals).toHaveLength(1);
			expect(
				journal
					.getEntries()
					.filter(
						entry =>
							entry.type === "custom" &&
							[TASK_NATIVE_RESULT_READY, TASK_RESULT_PROCESSING_STARTED].includes(entry.customType),
					),
			).toHaveLength(0);
			expect(
				journal
					.getEntries()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-task",
					),
			).toHaveLength(0);
			expect(
				f.calls
					.slice(before)
					.filter(call => call.model === "parent" && call.tools?.some(tool => tool.function.name === "task")),
			).toHaveLength(0);
			const child = await loadSessionFile(snapshot.binding.child.sessionFile);
			const yielded = child.entries.filter(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolName === "yield" &&
					!entry.message.isError,
			);
			expect(yielded).toHaveLength(1);
		},
		30000,
	);
});
