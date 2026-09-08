import { afterEach, describe, expect, it, vi } from "bun:test";
import { AsyncLocalStorage } from "node:async_hooks";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { Agent, type AgentEvent } from "@oh-my-pi/pi-agent-core";
import { completeSimple } from "@oh-my-pi/pi-ai";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as customTools from "@oh-my-pi/pi-coding-agent/extensibility/custom-tools";
import * as extensions from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import type { ExtensionAPI, ExtensionFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { AgentLifecycleManager } from "@oh-my-pi/pi-coding-agent/registry/agent-lifecycle";
import { AgentRegistry, getAgentTombstonePath } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import { ensurePersistedRoster } from "@oh-my-pi/pi-coding-agent/registry/persisted-agents";
import * as sdk from "@oh-my-pi/pi-coding-agent/sdk";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import * as artifacts from "@oh-my-pi/pi-coding-agent/session/artifacts";
import { TOOL_EXECUTION_START_CUSTOM_TYPE } from "@oh-my-pi/pi-coding-agent/session/exit-diagnostics";
import { loadSessionFile } from "@oh-my-pi/pi-coding-agent/session/session-loader";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { FileSessionStorage } from "@oh-my-pi/pi-coding-agent/session/session-storage";
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
	TASK_READ_CONTINUATION_READY,
	TASK_READ_CONTINUATION_STARTED,
	TASK_RESULT_PROCESSING_STARTED,
	type TaskReadContinuationReadyV1,
	taskRecoveryHash,
	taskResultRecoveryState,
} from "@oh-my-pi/pi-coding-agent/task/recovery";
import { TASK_SUBAGENT_LIFECYCLE_CHANNEL, type TaskToolDetails } from "@oh-my-pi/pi-coding-agent/task/types";
import * as outputMeta from "@oh-my-pi/pi-coding-agent/tools/output-meta";
import { nativePlainReadProvenance, ReadTool } from "@oh-my-pi/pi-coding-agent/tools/read";
import { TempDir, untilAborted } from "@oh-my-pi/pi-utils";
import assignment from "../../../python/omp-work/tests/fixtures/task-active-assignment.md" with { type: "text" };
import ownerResume from "./fixtures/task-recovery-owner-resume.md" with { type: "text" };
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
		testOptions: {
			maxRuntimeMs?: number;
			outputSchema?: unknown;
			extensions?: ExtensionFactory[];
			holdReadResponse?: boolean;
			readHttpError?: "terminal" | "strict-once";
			onReadHttpError?: () => void;
		} = {},
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
		const readRequestReached = Promise.withResolvers<void>();
		const releaseReadResponse = Promise.withResolvers<void>();
		const calls: WireRequest[] = [];
		let readHttpErrorSent = false;
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
				if (input.model === "child" && input.tools?.some(tool => tool.function.name === "yield")) {
					if (results.length === 0) {
						childReached.resolve();
						await releaseChild.promise;
						delta = { tool_calls: [call("child-read", "read", { path: "result.txt" })] };
					} else {
						readRequestReached.resolve();
						if (testOptions.readHttpError && !readHttpErrorSent) {
							readHttpErrorSent = true;
							testOptions.onReadHttpError?.();
							return Response.json(
								{
									error: {
										message:
											testOptions.readHttpError === "strict-once"
												? "strict tools unsupported"
												: "Rejected completion fixture",
										type: "invalid_request_error",
									},
								},
								{ status: 400 },
							);
						}
						if (testOptions.holdReadResponse) await releaseReadResponse.promise;
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
				...(testOptions.readHttpError === "strict-once" ? { compat: { supportsStrictMode: true } } : {}),
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
			releaseReadResponse.resolve();
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
					...(testOptions.extensions ?? []),
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
			readRequestReached,
			releaseReadResponse,
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

	function installChildExtensions(factories: ExtensionFactory[]): void {
		const create = sdk.createAgentSession;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
			create({ ...options, extensions: [...(options?.extensions ?? []), ...(options?.agentId ? factories : [])] }),
		);
	}

	async function readRecoveryFixture() {
		const f = await fixture(false, true, false, false, {
			holdReadResponse: true,
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.readRequestReached.promise);
		const snapshot = await f.takeSnapshot();
		const originalChild = AgentRegistry.global().get(snapshot.binding.child.registryId)!.session!;
		const beforeChild = originalChild.sessionManager.getEntries();
		f.releaseReadResponse.resolve();
		f.releaseParent.resolve();
		await f.run;
		await f.session.dispose();
		await originalChild.dispose();
		await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
		for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
		await Bun.write(snapshot.file, snapshot.journal);
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		const journal = await SessionManager.open(snapshot.file);
		const created = await f.createParent(journal);
		f.setCurrentSession(created.session);
		AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
			createPersistedSubagentReviverFactory({
				session: created.session,
				authStorage: f.auth,
				modelRegistry: f.models,
				settings: f.settings,
				enableLsp: false,
				eventBus: created.eventBus,
			}),
			0,
		);
		await initializeExtensions(created.session, { reportSendError: () => {}, reportRuntimeError: () => {} });
		return { f, snapshot, beforeChild, journal, created };
	}

	it("native read checkpoint precedes held next HTTP headers and response claim precedes response hooks", async () => {
		const originalCreate = sdk.createAgentSession;
		let responseHooks = 0;
		let responseMessages = 0;
		let yieldHooks = 0;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
			originalCreate({
				...options,
				extensions: [
					...(options?.extensions ?? []),
					...(options?.agentId
						? [
								(pi: ExtensionAPI) => {
									const assertClaim = async (ctx: extensions.ExtensionContext) => {
										const disk = await loadSessionFile(ctx.sessionManager.getSessionFile()!);
										expect(
											disk.entries.filter(
												entry =>
													entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
											),
										).toHaveLength(1);
									};
									pi.on("after_provider_response", async (_event, ctx) => {
										if (
											ctx.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
												)
										) {
											await assertClaim(ctx);
											responseHooks++;
										}
									});
									pi.on("message_start", async (event, ctx) => {
										if (
											event.message.role === "assistant" &&
											ctx.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
												)
										) {
											await assertClaim(ctx);
											responseMessages++;
										}
									});
									pi.on("tool_call", async (event, ctx) => {
										if (event.toolName === "yield") {
											await assertClaim(ctx);
											yieldHooks++;
										}
									});
								},
							]
						: []),
				],
			}),
		);
		const f = await fixture(false, true, false, false, {
			holdReadResponse: true,
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		try {
			await untilAborted(AbortSignal.timeout(10000), f.readRequestReached.promise);
		} catch (error) {
			throw new Error(`${String(error)}; parent=${JSON.stringify(f.session.messages)}`);
		}
		const binding = f.manager
			.getEntries()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const child = AgentRegistry.global().get(binding.child.registryId)!.session!;
		const disk = await loadSessionFile(binding.child.sessionFile);
		const ready = disk.entries.filter(
			entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
		);
		expect(ready).toHaveLength(1);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED),
		).toHaveLength(0);
		expect(responseHooks).toBe(0);
		expect(responseMessages).toBe(0);
		expect(yieldHooks).toBe(0);
		const wire = f.calls.filter(request => request.model === "child").at(-1)!;
		expect(wire.messages.filter(message => message.tool_call_id === "child-read")).toHaveLength(1);
		expect(child.sessionManager.getLeafId()).toBe(ready[0].id);
		f.releaseReadResponse.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		expect(responseHooks).toBe(1);
		expect(responseMessages).toBe(1);
		expect(yieldHooks).toBe(1);
		expect(
			f.calls.filter(
				request =>
					request.model === "parent" && request.messages.some(message => message.tool_call_id === "original-task"),
			),
		).toHaveLength(1);
	}, 30000);

	it("nested read result-hook mutation preserves native proof and binds transformed result on wire", async () => {
		let nativeHash = "";
		let nativeProof: unknown;
		const observe = AgentSession.prototype.observeNativeTaskRead;
		vi.spyOn(AgentSession.prototype, "observeNativeTaskRead").mockImplementation(function (
			this: AgentSession,
			id,
			args,
			result,
		) {
			if (id === "child-read") {
				nativeHash = taskRecoveryHash(result);
				nativeProof = structuredClone(nativePlainReadProvenance(result));
			}
			observe.call(this, id, args, result);
		});
		let changed = false;
		installChildExtensions([
			pi => {
				pi.on("tool_result", event => {
					if (event.toolName !== "read") return;
					const text = event.content.find(part => part.type === "text");
					if (!text) throw new Error("Native text result missing");
					text.text = `hook-transformed:${text.text}`;
					const details = event.details as { meta?: { source?: string } };
					if (!details.meta) throw new Error("Native result metadata missing");
					details.meta.source = "hook-source";
					changed = true;
				});
			},
		]);
		const f = await fixture(false, true, false, false, {
			holdReadResponse: true,
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.readRequestReached.promise);
		const snapshot = await f.takeSnapshot();
		const disk = await loadSessionFile(snapshot.binding.child.sessionFile);
		const readyEntry = disk.entries.find(
			entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
		);
		if (readyEntry?.type !== "custom") throw new Error("Native ready missing");
		const ready = readyEntry.data as TaskReadContinuationReadyV1;
		const result = disk.entries.find(entry => entry.id === ready.read.resultEntryId);
		if (result?.type !== "message" || result.message.role !== "toolResult") throw new Error("Durable read missing");
		expect(changed).toBe(true);
		expect(ready.read.nativeResultSha256).toBe(nativeHash);
		expect(taskRecoveryHash(ready.read.native)).toBe(taskRecoveryHash(nativeProof));
		expect(ready.read.nativeResultSha256).not.toBe(
			taskRecoveryHash({ content: result.message.content, details: result.message.details }),
		);
		expect(ready.read.finalResultSha256).toBe(taskRecoveryHash(result.message));
		const wire = f.calls
			.filter(request => request.model === "child" && request.tools?.some(tool => tool.function.name === "yield"))
			.at(-1)!;
		const text = result.message.content
			.filter(part => part.type === "text")
			.map(part => part.text)
			.join("\n");
		expect(text.startsWith("hook-transformed:")).toBe(true);
		expect(wire.messages.find(message => message.tool_call_id === "child-read")!.content).toBe(text);
		f.releaseReadResponse.resolve();
		f.releaseParent.resolve();
		await f.run;
	}, 15000);

	it("read continuation waits for queued read-assistant message-update work before certifying", async () => {
		const held = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		const preparing = Promise.withResolvers<void>();
		const originalCreate = sdk.createAgentSession;
		let blocked = false;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
			originalCreate({
				...options,
				extensions: [
					...(options?.extensions ?? []),
					...(options?.agentId
						? [
								(pi: ExtensionAPI) => {
									pi.on("message_update", async event => {
										if (
											!blocked &&
											event.message.role === "assistant" &&
											event.message.content.some(part => part.type === "toolCall" && part.name === "read")
										) {
											blocked = true;
											held.resolve();
											await release.promise;
										}
									});
									pi.on("before_provider_request", event => {
										if (JSON.stringify(event.payload).includes("child-read")) preparing.resolve();
									});
								},
							]
						: []),
				],
			}),
		);
		const f = await fixture(false, true, false, false, {
			holdReadResponse: true,
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		cleanups.push(async () => release.resolve());
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), Promise.all([held.promise, preparing.promise]));
		await Promise.race([f.readRequestReached.promise, Bun.sleep(50)]);
		const binding = f.manager
			.getEntries()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const child = AgentRegistry.global().get(binding.child.registryId)!.session!;
		const ready = child.sessionManager
			.getEntries()
			.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY);
		expect(blocked).toBe(true);
		expect(ready).toHaveLength(0);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(1);
		release.resolve();
		f.releaseReadResponse.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		expect(
			child.sessionManager
				.getEntries()
				.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY),
		).toHaveLength(1);
	}, 15000);

	it("read checkpoint cannot adopt foreign metadata during a held request hook", async () => {
		const held = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		const originalCreate = sdk.createAgentSession;
		let blocked = false;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
			originalCreate({
				...options,
				extensions: [
					...(options?.extensions ?? []),
					...(options?.agentId
						? [
								(pi: ExtensionAPI) => {
									pi.on("before_provider_request", async event => {
										if (!blocked && JSON.stringify(event.payload).includes("child-read")) {
											blocked = true;
											held.resolve();
											await release.promise;
										}
									});
								},
							]
						: []),
				],
			}),
		);
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		cleanups.push(async () => release.resolve());
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), held.promise);
		const binding = f.manager
			.getEntries()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const child = AgentRegistry.global().get(binding.child.registryId)!.session!;
		const foreign = child.sessionManager.appendServiceTierChange(null);
		await child.sessionManager.flush();
		release.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		const persisted = (await loadSessionFile(binding.child.sessionFile)).entries;
		expect(persisted.find(entry => entry.id === foreign)?.type).toBe("service_tier_change");
		expect(
			persisted.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY),
		).toHaveLength(0);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(1);
	}, 15000);

	it("certified read cold continuation claims the same manager before SDK and resumes without another read or input", async () => {
		const { f, snapshot, beforeChild, journal, created } = await readRecoveryFixture();
		const nativeRead = vi.spyOn(ReadTool.prototype, "execute");
		const originalCreate = sdk.createAgentSession;
		let claimManager: SessionManager | undefined;
		let sdkObserved = false;
		let startupObserved = false;
		const append = SessionManager.prototype.appendCustomEntry;
		vi.spyOn(SessionManager.prototype, "appendCustomEntry").mockImplementation(function (
			this: SessionManager,
			type,
			data,
		) {
			if (type === TASK_READ_CONTINUATION_STARTED) claimManager = this;
			return append.call(this, type, data);
		});
		const sdkConstruction = vi.spyOn(sdk, "createAgentSession").mockImplementation(async options => {
			if (options?.agentId) {
				expect(options.sessionManager).toBe(claimManager);
				const disk = await loadSessionFile(snapshot.binding.child.sessionFile);
				expect(
					disk.entries.filter(
						entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
					),
				).toHaveLength(1);
				sdkObserved = true;
				return originalCreate({
					...options,
					extensions: [
						...(options.extensions ?? []),
						(pi: ExtensionAPI) => {
							pi.on("session_start", async (_event, ctx) => {
								const saved = await loadSessionFile(ctx.sessionManager.getSessionFile()!);
								expect(
									saved.entries.filter(
										entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
									),
								).toHaveLength(1);
								startupObserved = true;
							});
						},
					],
				});
			}
			return originalCreate(options);
		});
		const before = f.calls.length;
		const refusals: unknown[] = [];
		const outcome = created.session.requestPersistedTurnContinuation({
			sessionId: created.session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		expect(outcome.status).toBe("scheduled");
		await untilAborted(AbortSignal.timeout(10000), created.session.waitForIdle());
		expect(refusals).toEqual([]);
		expect(sdkObserved).toBe(true);
		expect(startupObserved).toBe(true);
		expect(nativeRead).not.toHaveBeenCalled();
		const childRequests = f.calls.slice(before).filter(request => request.model === "child");
		expect(childRequests).toHaveLength(1);
		expect(childRequests[0].messages.filter(message => message.tool_call_id === "child-read")).toHaveLength(1);
		const wire = childRequests[0].messages;
		const savedAssistant = beforeChild.find(
			entry =>
				entry.type === "message" &&
				entry.message.role === "assistant" &&
				entry.message.content.some(part => part.type === "toolCall" && part.id === "child-read"),
		);
		if (savedAssistant?.type !== "message" || savedAssistant.message.role !== "assistant")
			throw new Error("Saved read assistant missing");
		const savedCall = savedAssistant.message.content.find(
			part => part.type === "toolCall" && part.id === "child-read",
		);
		if (savedCall?.type !== "toolCall") throw new Error("Saved read call missing");
		const calls = wire
			.filter(message => message.role === "assistant")
			.flatMap(message => message.tool_calls ?? [])
			.filter(call => call.id === "child-read");
		expect(calls).toHaveLength(1);
		expect(calls[0].function.name).toBe("read");
		expect(JSON.parse(calls[0].function.arguments)).toEqual(savedCall.arguments);
		const callIndex = wire.findIndex(
			message => message.role === "assistant" && message.tool_calls?.some(call => call.id === "child-read"),
		);
		const resultIndex = wire.findIndex(message => message.role === "tool" && message.tool_call_id === "child-read");
		expect(resultIndex).toBeGreaterThan(callIndex);
		const savedResult = beforeChild.find(
			entry =>
				entry.type === "message" &&
				entry.message.role === "toolResult" &&
				entry.message.toolCallId === "child-read",
		);
		if (savedResult?.type !== "message" || savedResult.message.role !== "toolResult")
			throw new Error("Saved read result missing");
		expect(wire[resultIndex].content).toBe(
			savedResult.message.content
				.filter(part => part.type === "text")
				.map(part => part.text)
				.join("\n"),
		);
		const resumedChild = AgentRegistry.global().get(snapshot.binding.child.registryId)!.session!;
		const afterChild = resumedChild.sessionManager.getEntries();
		const prepBefore = beforeChild.filter(
			entry => entry.type === "custom" && entry.customType === "prompt-preparation",
		);
		expect(afterChild.filter(entry => entry.type === "custom" && entry.customType === "prompt-preparation")).toEqual(
			prepBefore,
		);
		expect(afterChild.filter(entry => entry.type === "message" && entry.message.role === "user")).toEqual(
			beforeChild.filter(entry => entry.type === "message" && entry.message.role === "user"),
		);
		expect(
			f.calls
				.slice(before)
				.filter(
					request =>
						request.model === "parent" &&
						request.messages.some(message => message.tool_call_id === "original-task"),
				),
		).toHaveLength(1);
		const parentWire = f.calls
			.slice(before)
			.find(
				request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"),
			)!.messages;
		const parentCalls = parentWire
			.filter(message => message.role === "assistant")
			.flatMap(message => message.tool_calls ?? [])
			.filter(call => call.id === "original-task");
		expect(parentCalls).toHaveLength(1);
		expect(parentCalls[0].function.name).toBe("task");
		expect(JSON.parse(parentCalls[0].function.arguments)).toEqual({ name: "Child", agent: "task", task: assignment });
		expect(parentWire.findIndex(message => message.tool_call_id === "original-task")).toBeGreaterThan(
			parentWire.findIndex(message => message.tool_calls?.some(call => call.id === "original-task")),
		);
		sdkConstruction.mockRestore();
		await AgentLifecycleManager.global().park(snapshot.binding.child.registryId);
		const reopenedChild = await AgentLifecycleManager.global().ensureLive(snapshot.binding.child.registryId);
		expect(reopenedChild.sessionId).toBe(snapshot.binding.child.sessionId);
		await reopenedChild.dispose();
	}, 30000);

	it("read response error cannot expose terminal hooks while its claim authority is pending", async () => {
		const held = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		const exposed = Promise.withResolvers<void>();
		let terminalEffects = 0;
		let requestRejected = false;
		const originalCreate = sdk.createAgentSession;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options =>
			originalCreate({
				...options,
				extensions: [
					...(options?.extensions ?? []),
					...(options?.agentId
						? [
								(pi: ExtensionAPI) => {
									pi.on("turn_end", (_event, ctx) => {
										if (
											ctx.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
												)
										) {
											terminalEffects++;
											exposed.resolve();
										}
									});
									pi.on("agent_end", (_event, ctx) => {
										if (
											ctx.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
												)
										) {
											terminalEffects++;
											exposed.resolve();
										}
									});
								},
							]
						: []),
				],
			}),
		);
		const f = await fixture(false, true, false, false, {
			readHttpError: "terminal",
			onReadHttpError: () => {
				requestRejected = true;
			},
			extensions: [
				pi => {
					pi.on("tool_call", (event, ctx) =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										const binding = ctx.sessionManager
											.getEntries()
											.map(readTaskBinding)
											.find(value => value !== undefined);
										const child = binding
											? AgentRegistry.global().get(binding.child.registryId)?.session
											: undefined;
										if (
											requestRejected &&
											child?.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY,
												) &&
											!child.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" &&
														entry.customType === TASK_READ_CONTINUATION_STARTED,
												)
										) {
											held.resolve();
											await release.promise;
										}
										return { ok: true };
									},
								}
							: undefined,
					);
				},
			],
		});
		cleanups.push(async () => release.resolve());
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), held.promise);
		await Promise.race([exposed.promise, Bun.sleep(50)]);
		expect(requestRejected).toBe(true);
		expect(terminalEffects).toBe(0);
		release.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(terminalEffects).toBeGreaterThan(0);
		const binding = f.manager
			.getEntries()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const disk = await loadSessionFile(binding.child.sessionFile);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED),
		).toHaveLength(1);
	}, 15000);

	for (const failedHook of [
		"tool_result",
		"message_end",
		"turn_end",
		"before_provider_request",
		"queued-emission",
	] as const) {
		it(`read certificate refuses ${failedHook} failure without parent fallback hooks`, async () => {
			let failed = false;
			let parentHooks = 0;
			const fail = () => {
				failed = true;
				throw new Error("Returned read-step hook failure");
			};
			installChildExtensions([
				pi => {
					if (failedHook === "tool_result")
						pi.on("tool_result", event => {
							if (event.toolName === "read") fail();
						});
					if (failedHook === "message_end")
						pi.on("message_end", event => {
							if (event.message.role === "toolResult" && event.message.toolName === "read") fail();
						});
					if (failedHook === "turn_end")
						pi.on("turn_end", event => {
							if (
								event.message.role === "assistant" &&
								event.message.content.some(part => part.type === "toolCall" && part.name === "read")
							)
								fail();
						});
					if (failedHook === "before_provider_request")
						pi.on("before_provider_request", event => {
							if (JSON.stringify(event.payload).includes("child-read")) fail();
						});
					if (failedHook === "queued-emission") pi.on("message_update", () => {});
				},
			]);
			if (failedHook === "queued-emission") {
				const emit = extensions.ExtensionRunner.prototype.emit;
				vi.spyOn(extensions.ExtensionRunner.prototype, "emit").mockImplementation(function (
					this: extensions.ExtensionRunner,
					event: Parameters<typeof emit>[0],
				) {
					if (
						event.type === "message_update" &&
						event.message.role === "assistant" &&
						event.message.content.some(part => part.type === "toolCall" && part.name === "read")
					) {
						failed = true;
						return Promise.reject(new Error("Queued extension emit rejected"));
					}
					return emit.call(this, event);
				} as typeof emit);
			}
			const f = await fixture(false, true, false, false, {
				extensions: [
					pi => {
						pi.on("tool_call", event =>
							event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
						);
						pi.on("tool_result", event => {
							if (event.toolName === "task") parentHooks++;
						});
					},
				],
			});
			f.releaseChild.resolve();
			f.releaseParent.resolve();
			await untilAborted(
				AbortSignal.timeout(10000),
				f.run!.catch(() => {}),
			);
			expect(failed).toBe(true);
			expect(parentHooks).toBe(0);
			expect(
				f.calls.filter(
					request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"),
				),
			).toHaveLength(1);
			const binding = f.manager
				.getEntries()
				.map(readTaskBinding)
				.find(value => value !== undefined)!;
			const disk = await loadSessionFile(binding.child.sessionFile);
			expect(
				disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY),
			).toHaveLength(0);
			expect(
				f.calls.filter(
					request => request.model === "child" && request.tools?.some(tool => tool.function.name === "yield"),
				),
			).toHaveLength(1);
		}, 15000);
	}

	it("read step drains a nested same-model side request without fencing it as the next main request", async () => {
		let sideRequests = 0;
		installChildExtensions([
			pi => {
				pi.on("turn_end", async (event, ctx) => {
					if (
						event.message.role !== "assistant" ||
						!event.message.content.some(part => part.type === "toolCall" && part.name === "read")
					)
						return;
					const child = AgentRegistry.global().get("Child")!.session!;
					const model = child.model!;
					await completeSimple(
						model,
						{ messages: [{ role: "user", content: ownerResume, timestamp: Date.now() }] },
						child.prepareSimpleStreamOptions({ apiKey: "local-test", maxTokens: 32 }, model.provider),
					);
					expect(
						ctx.sessionManager
							.getEntries()
							.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY),
					).toHaveLength(0);
					sideRequests++;
				});
			},
		]);
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		expect(sideRequests).toBe(1);
		expect(f.calls.filter(request => request.model === "child" && !request.tools?.length)).toHaveLength(1);
		expect(
			f.calls.filter(
				request => request.model === "child" && request.tools?.some(tool => tool.function.name === "yield"),
			),
		).toHaveLength(2);
	}, 15000);

	it("read retry claims before reentering request hooks after an actual strict-schema HTTP rejection", async () => {
		let requestEffects = 0;
		installChildExtensions([
			pi => {
				pi.on("before_provider_request", async (event, ctx) => {
					if (!JSON.stringify(event.payload).includes("child-read")) return;
					requestEffects++;
					const disk = await loadSessionFile(ctx.sessionManager.getSessionFile()!);
					const starts = disk.entries.filter(
						entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
					);
					expect(starts).toHaveLength(requestEffects === 1 ? 0 : 1);
				});
			},
		]);
		const f = await fixture(false, true, false, false, {
			readHttpError: "strict-once",
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		expect(requestEffects).toBe(2);
		expect(
			f.calls.filter(
				request =>
					request.model === "child" && request.messages.some(message => message.tool_call_id === "child-read"),
			),
		).toHaveLength(2);
		expect(
			f.calls.filter(
				request =>
					request.model === "parent" && request.messages.some(message => message.tool_call_id === "original-task"),
			),
		).toHaveLength(1);
	}, 15000);

	it("read main transport receives the validated private payload when a hook mutates its retained object during authority", async () => {
		let retained: WireRequest | undefined;
		let mutated = false;
		installChildExtensions([
			pi => {
				pi.on("before_provider_request", event => {
					const payload = event.payload as WireRequest;
					if (payload.messages.some(message => message.tool_call_id === "child-read")) retained = payload;
				});
			},
		]);
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										if (retained && !mutated) {
											retained.messages.find(message => message.tool_call_id === "child-read")!.content =
												"Hook mutated retained payload";
											mutated = true;
										}
										return { ok: true };
									},
								}
							: undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		expect(mutated).toBe(true);
		const wire = f.calls.find(
			request =>
				request.model === "child" && request.messages.some(message => message.tool_call_id === "child-read"),
		)!;
		expect(wire.messages.find(message => message.tool_call_id === "child-read")!.content).toContain("before");
		expect(wire.messages.find(message => message.tool_call_id === "child-read")!.content).not.toContain(
			"Hook mutated",
		);
	}, 15000);

	for (const mutation of ["missing-call", "wrong-role", "reversed-pair"] as const) {
		it(`read final main payload rejects ${mutation} before HTTP`, async () => {
			let changed = false;
			installChildExtensions([
				pi => {
					pi.on("before_provider_request", event => {
						const payload = event.payload as WireRequest;
						if (!payload.messages.some(message => message.tool_call_id === "child-read")) return;
						const index = payload.messages.findIndex(message =>
							message.tool_calls?.some(call => call.id === "child-read"),
						);
						if (mutation === "missing-call") payload.messages[index].tool_calls = [];
						else if (mutation === "wrong-role") payload.messages[index].role = "user";
						else {
							const [call] = payload.messages.splice(index, 1);
							payload.messages.push(call);
						}
						changed = true;
						return payload;
					});
				},
			]);
			const f = await fixture(false, true, false, false, {
				extensions: [
					pi => {
						pi.on("tool_call", event =>
							event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
						);
					},
				],
			});
			f.releaseChild.resolve();
			f.releaseParent.resolve();
			await untilAborted(
				AbortSignal.timeout(10000),
				f.run!.catch(() => {}),
			);
			expect(changed).toBe(true);
			expect(
				f.calls.filter(
					request => request.model === "child" && request.tools?.some(tool => tool.function.name === "yield"),
				),
			).toHaveLength(1);
		}, 15000);
	}

	it("cold read claim coalesces two revivals and rejects a second preparation manager before SDK", async () => {
		const { snapshot, journal, created } = await readRecoveryFixture();
		const held = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		cleanups.push(async () => release.resolve());
		let manager: SessionManager | undefined;
		let sdkStarts = 0;
		const flush = SessionManager.prototype.flush;
		vi.spyOn(SessionManager.prototype, "flush").mockImplementation(async function (this: SessionManager) {
			if (
				!manager &&
				this.getSessionFile() === snapshot.binding.child.sessionFile &&
				this.getEntries().some(
					entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
				)
			) {
				manager = this;
				held.resolve();
				await release.promise;
			}
			return flush.call(this);
		});
		const create = sdk.createAgentSession;
		vi.spyOn(sdk, "createAgentSession").mockImplementation(options => {
			if (options?.agentId) sdkStarts++;
			return create(options);
		});
		const refusals: unknown[] = [];
		created.session.requestPersistedTurnContinuation({
			sessionId: created.session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), held.promise);
		expect(sdkStarts).toBe(0);
		const ref = AgentRegistry.global().get(snapshot.binding.child.registryId)!;
		const second = AgentLifecycleManager.global().ensureLive(ref.id);
		const other = await SessionManager.open(snapshot.binding.child.sessionFile, undefined, undefined, {
			suppressBreadcrumb: true,
		});
		try {
			await expect(created.session.prepareTaskRecoveryRevival(ref, other)).rejects.toThrow("Another manager");
		} finally {
			await other.close();
		}
		expect(sdkStarts).toBe(0);
		release.resolve();
		const coalesced = await second;
		await untilAborted(AbortSignal.timeout(10000), created.session.waitForIdle());
		expect(refusals).toEqual([]);
		expect(sdkStarts).toBe(1);
		expect(coalesced.sessionManager).toBe(manager!);
		const disk = await loadSessionFile(snapshot.binding.child.sessionFile);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED),
		).toHaveLength(1);
	}, 15000);

	for (const change of ["registry", "tombstone", "branch", "file", "policy", "owner", "authority"] as const) {
		it(`cold read ${change} during claim flush blocks SDK startup and preserves claim`, async () => {
			const { f, snapshot, journal, created } = await readRecoveryFixture();
			const held = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			cleanups.push(async () => release.resolve());
			let stopped = false;
			let manager: SessionManager | undefined;
			let deny = false;
			let sdkStarts = 0;
			const flush = SessionManager.prototype.flush;
			vi.spyOn(SessionManager.prototype, "flush").mockImplementation(async function (this: SessionManager) {
				if (
					!stopped &&
					this.getSessionFile() === snapshot.binding.child.sessionFile &&
					this.getEntries().some(
						entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
					)
				) {
					stopped = true;
					manager = this;
					held.resolve();
					await release.promise;
				}
				return flush.call(this);
			});
			const create = sdk.createAgentSession;
			vi.spyOn(sdk, "createAgentSession").mockImplementation(options => {
				if (options?.agentId) sdkStarts++;
				return create(options);
			});
			const refusals: unknown[] = [];
			created.session.requestPersistedTurnContinuation({
				sessionId: created.session.sessionId,
				entryId: snapshot.binding.call.promptEntryId,
				expectedLeafId: journal.getLeafId()!,
				recoverSynchronousTask: true,
				validateDispatch: async () =>
					deny ? { ok: false, reason: "Authority revoked during claim" } : { ok: true },
				onRefused: reason => refusals.push(reason),
			});
			await untilAborted(AbortSignal.timeout(10000), held.promise);
			const registry = AgentRegistry.global();
			const ref = registry.get(snapshot.binding.child.registryId)!;
			if (change === "registry") registry.register({ ...ref, session: null, status: "parked" });
			else if (change === "tombstone")
				await Bun.write(getAgentTombstonePath(snapshot.binding.child.sessionFile), "");
			else if (change === "branch") {
				manager!.branch(snapshot.binding.child.initEntryId);
				manager!.appendCustomEntry("foreign-owner", {});
			} else if (change === "file") {
				const other = path.join(f.root.path(), "other-child.jsonl");
				await Bun.write(other, Bun.file(snapshot.binding.child.sessionFile));
				await manager!.setSessionFile(other);
			} else if (change === "policy") created.session.settings.set("tools.approval", { task: "deny" });
			else if (change === "owner") await created.session.sendUserMessage(ownerResume, { deliverAs: "followUp" });
			else deny = true;
			release.resolve();
			await untilAborted(AbortSignal.timeout(10000), created.session.waitForIdle());
			expect(refusals).toHaveLength(1);
			expect(sdkStarts).toBe(0);
			if (change === "owner") expect(created.session.getQueuedMessages().followUp).toEqual([ownerResume]);
			expect(registry.get(ref.id)?.session).toBeNull();
			const disk = await loadSessionFile(snapshot.binding.child.sessionFile);
			expect(
				disk.entries.filter(
					entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED,
				),
			).toHaveLength(1);
		}, 15000);
	}

	it("unbound managed revival cannot run startup from an undelivered read certificate", async () => {
		const { f, snapshot } = await readRecoveryFixture();
		await ensurePersistedRoster(AgentRegistry.global(), snapshot.file);
		const create = vi.spyOn(sdk, "createAgentSession");
		const before = f.calls.length;
		await expect(AgentLifecycleManager.global().ensureLive(snapshot.binding.child.registryId)).rejects.toThrow(
			"Undelivered read continuation",
		);
		expect(create).not.toHaveBeenCalled();
		expect(f.calls).toHaveLength(before);
		const disk = await loadSessionFile(snapshot.binding.child.sessionFile);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED),
		).toHaveLength(0);
	}, 15000);

	it("in-memory parent result after actual storage failure cannot authorize marked child revival", async () => {
		let parentFile: string | undefined;
		let failed = false;
		let block = true;
		const rejectResult = (file: string, text: string) => {
			if (
				block &&
				file === parentFile &&
				text.includes('"role":"toolResult"') &&
				text.includes('"toolCallId":"original-task"')
			) {
				failed = true;
				throw new Error("Original result disk write refused");
			}
		};
		const open = FileSessionStorage.prototype.openWriter;
		vi.spyOn(FileSessionStorage.prototype, "openWriter").mockImplementation(function (
			this: FileSessionStorage,
			file,
			options,
		) {
			const writer = open.call(this, file, options);
			const append = writer.appendSync!.bind(writer);
			vi.spyOn(writer, "appendSync").mockImplementation(text => {
				rejectResult(file, text);
				append(text);
			});
			return writer;
		});
		const sync = FileSessionStorage.prototype.writeTextSync;
		vi.spyOn(FileSessionStorage.prototype, "writeTextSync").mockImplementation(function (
			this: FileSessionStorage,
			file,
			text,
		) {
			rejectResult(file, text);
			sync.call(this, file, text);
		});
		const atomic = FileSessionStorage.prototype.writeTextAtomic;
		vi.spyOn(FileSessionStorage.prototype, "writeTextAtomic").mockImplementation(function (
			this: FileSessionStorage,
			file,
			text,
			options,
		) {
			rejectResult(file, text);
			return atomic.call(this, file, text, options);
		});
		const f = await fixture(false, false, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		cleanups.push(async () => {
			block = false;
			await f.manager.recoverPersistenceFromCurrentState();
		});
		parentFile = f.manager.getSessionFile();
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.session
				.sendCustomMessage(
					{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
					{ triggerTurn: true },
				)
				.catch(() => {}),
		);
		expect(failed).toBe(true);
		const binding = f.manager
			.getEntries()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const memoryResults = f.manager
			.getEntries()
			.filter(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			);
		expect(memoryResults).toHaveLength(1);
		const disk = await loadSessionFile(parentFile!);
		expect(
			disk.entries.filter(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === "original-task",
			),
		).toHaveLength(0);
		await AgentLifecycleManager.global().park(binding.child.registryId);
		const create = vi.spyOn(sdk, "createAgentSession");
		// Rebuild only the managed roster, retaining the actual failed parent manager in memory.
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
			createPersistedSubagentReviverFactory({
				session: f.session,
				authStorage: f.auth,
				modelRegistry: f.models,
				settings: f.settings,
				enableLsp: false,
			}),
			0,
		);
		await ensurePersistedRoster(AgentRegistry.global(), parentFile!);
		await expect(AgentLifecycleManager.global().ensureLive(binding.child.registryId)).rejects.toThrow(
			"Undelivered read continuation",
		);
		expect(create).not.toHaveBeenCalled();
		expect(
			f.calls.filter(
				request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"),
			),
		).toHaveLength(1);
	}, 15000);

	it("cold startup effect leaves a durable claim that refuses a fresh replay", async () => {
		const { f, snapshot, journal, created } = await readRecoveryFixture();
		let effects = 0;
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		cleanups.push(async () => release.resolve());
		installChildExtensions([
			pi => {
				pi.on("session_start", async () => {
					effects++;
					await Bun.write(path.join(f.root.path(), "read-startup-effect.txt"), String(effects));
					reached.resolve();
					await release.promise;
				});
			},
		]);
		const refused: unknown[] = [];
		created.session.requestPersistedTurnContinuation({
			sessionId: created.session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refused.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), reached.promise);
		const checkpoint = await f.takeSnapshot(created.session);
		const child = AgentRegistry.global().get(snapshot.binding.child.registryId)!.session!;
		expect(
			child.sessionManager
				.getEntries()
				.filter(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_STARTED),
		).toHaveLength(1);
		release.resolve();
		await untilAborted(AbortSignal.timeout(10000), created.session.waitForIdle());
		expect(effects).toBe(1);
		expect(refused).toEqual([]);
		await created.session.dispose();
		await child.dispose();
		// Restore only exact runtime bytes captured while the first startup effect was held.
		await fs.rm(checkpoint.artifactsDir, { recursive: true, force: true });
		for (const [name, bytes] of checkpoint.artifacts)
			await Bun.write(path.join(checkpoint.artifactsDir, name), bytes);
		await Bun.write(checkpoint.file, checkpoint.journal);
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		const reopened = await SessionManager.open(snapshot.file);
		const next = await f.createParent(reopened);
		f.setCurrentSession(next.session);
		AgentLifecycleManager.global().setPersistedSubagentReviverFactory(
			createPersistedSubagentReviverFactory({
				session: next.session,
				authStorage: f.auth,
				modelRegistry: f.models,
				settings: f.settings,
				enableLsp: false,
				eventBus: next.eventBus,
			}),
			0,
		);
		await initializeExtensions(next.session, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const before = f.calls.length;
		const again: unknown[] = [];
		const outcome = next.session.requestPersistedTurnContinuation({
			sessionId: next.session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: reopened.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => again.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), next.session.waitForIdle());
		expect(JSON.stringify([outcome, ...again])).toContain("task-read-continuation-started");
		expect(effects).toBe(1);
		expect(f.calls).toHaveLength(before);
		expect(await Bun.file(path.join(f.root.path(), "read-startup-effect.txt")).text()).toBe("1");
	}, 15000);

	for (const fault of ["ready-append", "ready-flush", "claim-append", "claim-flush"] as const) {
		it(`read ${fault} failure cannot enter response or parent result hooks`, async () => {
			let failed = false;
			let responseEffects = 0;
			let parentEffects = 0;
			installChildExtensions([
				pi => {
					pi.on("after_provider_response", (_event, ctx) => {
						if (
							ctx.sessionManager
								.getEntries()
								.some(entry => entry.type === "custom" && entry.customType === TASK_READ_CONTINUATION_READY)
						)
							responseEffects++;
					});
				},
			]);
			const marker = fault.startsWith("ready") ? TASK_READ_CONTINUATION_READY : TASK_READ_CONTINUATION_STARTED;
			if (fault.endsWith("append")) {
				const append = SessionManager.prototype.appendCustomEntry;
				vi.spyOn(SessionManager.prototype, "appendCustomEntry").mockImplementation(function (
					this: SessionManager,
					type,
					data,
				) {
					if (type === marker) {
						failed = true;
						throw new Error("Read append failed");
					}
					return append.call(this, type, data);
				});
			} else {
				const flush = SessionManager.prototype.flush;
				vi.spyOn(SessionManager.prototype, "flush").mockImplementation(async function (this: SessionManager) {
					if (
						!failed &&
						this.getBranch().some(
							entry => entry.id === this.getLeafId() && entry.type === "custom" && entry.customType === marker,
						)
					) {
						failed = true;
						throw new Error("Read flush failed");
					}
					return flush.call(this);
				});
			}
			const f = await fixture(false, true, false, false, {
				extensions: [
					pi => {
						pi.on("tool_call", event =>
							event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
						);
						pi.on("tool_result", event => {
							if (event.toolName === "task") parentEffects++;
						});
					},
				],
			});
			f.releaseChild.resolve();
			f.releaseParent.resolve();
			await untilAborted(
				AbortSignal.timeout(10000),
				f.run!.catch(() => {}),
			);
			expect(failed).toBe(true);
			expect(responseEffects).toBe(0);
			expect(parentEffects).toBe(0);
		}, 15000);
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

	async function originalReadyFixture() {
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		let held = false;
		cleanups.push(async () => release.resolve());
		const f = await fixture(false, true, true, false, {
			extensions: [
				pi => {
					pi.on("tool_call", (event, ctx) =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										if (
											!held &&
											ctx.sessionManager
												.getEntries()
												.some(
													entry =>
														entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY,
												)
										) {
											held = true;
											reached.resolve();
											await release.promise;
										}
										return { ok: true };
									},
								}
							: undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), reached.promise);
		const snapshot = await f.takeSnapshot();
		const state = taskResultRecoveryState(f.manager.getEntries(), f.manager.getBranch(), snapshot.binding);
		if (!state.ready || state.processing) throw new Error("Original native checkpoint was not unprocessed");
		expect(state.ready.record.producer).toBe("original-sync-task-v1");
		const child = AgentRegistry.global().get(snapshot.binding.child.registryId)?.session;
		release.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		await f.session.dispose();
		await child?.dispose();
		await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
		for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
		await Bun.write(snapshot.file, snapshot.journal);
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		return { f, snapshot, ready: state.ready };
	}

	it("qualified original ready checkpoint restores actual parent wire result without reopening the completed child", async () => {
		const { f, snapshot, ready } = await originalReadyFixture();
		const journal = await SessionManager.open(snapshot.file);
		let hooks = 0;
		const { session } = await f.createParent(journal, [
			pi => {
				pi.on("tool_result", async event => {
					if (event.toolName !== "task") return;
					const disk = await loadSessionFile(snapshot.file);
					expect(
						disk.entries.filter(
							entry => entry.type === "custom" && entry.customType === TASK_RESULT_PROCESSING_STARTED,
						),
					).toHaveLength(1);
					hooks++;
				});
			},
		]);
		f.setCurrentSession(session);
		await initializeExtensions(session, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const revive = vi.spyOn(AgentLifecycleManager.global(), "ensureLive");
		const requests = f.calls.length;
		const refusals: unknown[] = [];
		session.requestPersistedTurnContinuation({
			sessionId: session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), session.waitForIdle());
		expect(refusals).toEqual([]);
		expect(revive).not.toHaveBeenCalled();
		expect(hooks).toBe(1);
		const resumed = f.calls.slice(requests);
		expect(resumed.filter(request => request.model === "child")).toHaveLength(0);
		const wire = resumed.find(
			request =>
				request.model === "parent" && request.messages.some(message => message.tool_call_id === "original-task"),
		)?.messages;
		expect(wire).toBeDefined();
		const taskCall = wire!.flatMap(message => message.tool_calls ?? []).filter(call => call.id === "original-task");
		expect(taskCall).toHaveLength(1);
		expect(JSON.parse(taskCall[0].function.arguments).task).toBe(assignment);
		expect(wire!.filter(message => message.tool_call_id === "original-task")).toHaveLength(1);
		expect(wire!.find(message => message.tool_call_id === "original-task")?.content).toContain("before");
		const state = taskResultRecoveryState(journal.getEntries(), journal.getBranch(), snapshot.binding);
		expect(state.ready?.record.payloadJson).toBe(ready.record.payloadJson);
		expect(state.processing?.record.readyEntryId).toBe(ready.entryId);
	}, 30000);

	it("qualified original completion persists its claim and result before the parent provider sees the pair", async () => {
		let resultHooks = 0;
		let authorityCalls = 0;
		const f = await fixture(false, true, true, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event => {
						return event.toolName === "task" && event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										authorityCalls++;
										return { ok: true };
									},
								}
							: undefined;
					});
					pi.on("tool_result", event => {
						if (event.toolName === "task") {
							resultHooks++;
							(event.details as TaskToolDetails).results[0].output = "Hook processed actual native result";
						}
					});
				},
			],
		});
		try {
			await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		} catch (error) {
			throw new Error(`${error}: ${JSON.stringify(f.session.messages)}`);
		}
		f.releaseChild.resolve();
		try {
			await untilAborted(AbortSignal.timeout(10000), f.parentReached.promise);
		} catch (error) {
			throw new Error(`${error}: ${JSON.stringify(f.session.messages)}`);
		}
		const binding = f.manager
			.getBranch()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const state = taskResultRecoveryState(f.manager.getEntries(), f.manager.getBranch(), binding);
		if (!state.ready) throw new Error(JSON.stringify({ authorityCalls, entries: f.manager.getEntries() }));
		expect(state.ready?.record.producer).toBe("original-sync-task-v1");
		expect(state.processing?.record.readyEntryId).toBe(state.ready?.entryId);
		expect(resultHooks).toBe(1);
		expect(authorityCalls).toBeGreaterThan(1);
		const disk = await loadSessionFile(f.manager.getSessionFile()!);
		const result = disk.entries.find(
			entry =>
				entry.type === "message" &&
				entry.message.role === "toolResult" &&
				entry.message.toolCallId === "original-task",
		);
		expect(result?.type === "message" && result.taskResult?.completion?.processingEntryId).toBe(
			state.processing?.entryId,
		);
		const wire = f.calls.find(
			request =>
				request.model === "parent" && request.messages.some(message => message.tool_call_id === "original-task"),
		)?.messages;
		expect(wire?.filter(message => message.tool_call_id === "original-task")).toHaveLength(1);
		const calls = wire?.flatMap(message => message.tool_calls ?? []).filter(call => call.id === "original-task");
		expect(calls).toHaveLength(1);
		expect(JSON.parse(calls![0].function.arguments).task).toBe(assignment);
		const raw = JSON.parse(state.ready.record.payloadJson) as { details: TaskToolDetails };
		expect(raw.details.results[0].output).toContain("before");
		expect(raw.details.results[0].output).not.toContain("Hook processed");
		expect(wire!.find(message => message.tool_call_id === "original-task")?.content).toContain("before");
		f.releaseParent.resolve();
		await f.run;
	}, 30000);

	it("qualified original authority failure cannot downgrade into task result hooks or child execution", async () => {
		let hooks = 0;
		let validations = 0;
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										validations++;
										return { ok: false, reason: "Execution authority was revoked" };
									},
								}
							: undefined,
					);
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			],
		});
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(validations).toBe(1);
		expect(hooks).toBe(0);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(0);
		expect(
			f.calls.filter(
				request => request.model === "parent" && request.messages.some(message => message.role === "tool"),
			),
		).toHaveLength(0);
		expect(
			f.manager.getEntries().filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
		).toHaveLength(0);
	}, 15000);

	it("qualified original first-use assistant copy is refused before authority activation or child dispatch", async () => {
		let validations = 0;
		let hooks = 0;
		const capture = AgentSession.prototype.captureTaskCall;
		vi.spyOn(AgentSession.prototype, "captureTaskCall").mockImplementation(function (
			this: AgentSession,
			id,
			params,
			signal,
		) {
			const original = this.messages.findLast(message => message.role === "assistant");
			if (original)
				this.agent.replaceMessages(
					this.messages.map(message => (message === original ? { ...original } : message)),
				);
			return capture.call(this, id, params, signal);
		});
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										validations++;
										return { ok: true };
									},
								}
							: undefined,
					);
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			],
		});
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(validations).toBe(0);
		expect(hooks).toBe(0);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(0);
		expect(
			f.manager.getEntries().filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
		).toHaveLength(0);
		const requestsBeforeOwner = f.calls.length;
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.session.prompt(ownerResume));
		const ownerRequests = f.calls
			.slice(requestsBeforeOwner)
			.filter(request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"));
		expect(ownerRequests).toHaveLength(1);
		expect(
			ownerRequests[0].messages.some(
				message => message.role === "user" && JSON.stringify(message.content).includes(ownerResume.trim()),
			),
		).toBe(true);
		expect(validations).toBe(0);
		expect(hooks).toBe(0);
	}, 15000);

	it("owner-authored task remains ordinary when a matching execution authority callback is available", async () => {
		let validations = 0;
		let hooks = 0;
		const f = await fixture(false, false, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										validations++;
										return { ok: true };
									},
								}
							: undefined,
					);
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.session.prompt(assignment));
		expect(validations).toBe(0);
		expect(hooks).toBe(1);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(2);
		expect(
			f.manager
				.getEntries()
				.filter(
					entry =>
						entry.type === "custom" &&
						(entry.customType === TASK_NATIVE_RESULT_READY ||
							entry.customType === TASK_RESULT_PROCESSING_STARTED),
				),
		).toHaveLength(0);
	}, 15000);

	for (const failedMarker of [TASK_NATIVE_RESULT_READY, TASK_RESULT_PROCESSING_STARTED]) {
		it(`qualified original ${failedMarker} append failure cannot reach fallback result hooks or parent provider`, async () => {
			const append = SessionManager.prototype.appendCustomEntry;
			let failed = false;
			let hooks = 0;
			vi.spyOn(SessionManager.prototype, "appendCustomEntry").mockImplementation(function (
				this: SessionManager,
				type,
				data,
			) {
				if (type === failedMarker) {
					failed = true;
					throw new Error("Injected journal write failure");
				}
				return append.call(this, type, data);
			});
			const f = await fixture(false, true, false, false, {
				extensions: [
					pi => {
						pi.on("tool_call", event =>
							event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
						);
						pi.on("tool_result", event => {
							if (event.toolName === "task") hooks++;
						});
					},
				],
			});
			await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
			f.releaseChild.resolve();
			await untilAborted(
				AbortSignal.timeout(10000),
				f.run!.catch(() => {}),
			);
			expect(failed).toBe(true);
			expect(hooks).toBe(0);
			expect(
				f.calls.filter(
					request => request.model === "parent" && request.messages.some(message => message.role === "tool"),
				),
			).toHaveLength(0);
			const disk = await loadSessionFile(f.manager.getSessionFile()!);
			expect(
				disk.entries.filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
			).toHaveLength(0);
			expect(
				disk.entries.filter(entry => entry.type === "custom" && entry.customType === failedMarker),
			).toHaveLength(0);
		}, 15000);
	}

	it("qualified original hook effect with no result stays refused after restart without repeating processing", async () => {
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		let hooks = 0;
		cleanups.push(async () => release.resolve());
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
					pi.on("tool_result", async (event, ctx) => {
						if (event.toolName !== "task") return;
						hooks++;
						await Bun.write(path.join(ctx.cwd, "original-hook-effect.txt"), String(hooks));
						reached.resolve();
						await release.promise;
					});
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), reached.promise);
		const snapshot = await f.takeSnapshot();
		const state = taskResultRecoveryState(f.manager.getEntries(), f.manager.getBranch(), snapshot.binding);
		expect(state.processing?.record.readyEntryId).toBe(state.ready?.entryId);
		f.settings.set("tools.approval", { read: "deny" });
		release.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(
			f.calls.filter(
				request => request.model === "parent" && request.messages.some(message => message.role === "tool"),
			),
		).toHaveLength(0);
		expect(
			f.session.messages.filter(message => message.role === "toolResult" && message.toolCallId === "original-task"),
		).toHaveLength(0);
		const after = await loadSessionFile(snapshot.file);
		expect(
			after.entries.filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
		).toHaveLength(0);
		const child = AgentRegistry.global().get(snapshot.binding.child.registryId)?.session;
		await f.session.dispose();
		await child?.dispose();
		await fs.rm(snapshot.artifactsDir, { recursive: true, force: true });
		for (const [name, bytes] of snapshot.artifacts) await Bun.write(path.join(snapshot.artifactsDir, name), bytes);
		await Bun.write(snapshot.file, snapshot.journal);
		f.settings.set("tools.approval", {});
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
		const journal = await SessionManager.open(snapshot.file);
		const { session } = await f.createParent(journal);
		f.setCurrentSession(session);
		await initializeExtensions(session, { reportSendError: () => {}, reportRuntimeError: () => {} });
		const requests = f.calls.length;
		const refusals: unknown[] = [];
		const outcome = session.requestPersistedTurnContinuation({
			sessionId: session.sessionId,
			entryId: snapshot.binding.call.promptEntryId,
			expectedLeafId: journal.getLeafId()!,
			recoverSynchronousTask: true,
			validateDispatch: async () => ({ ok: true }),
			onRefused: reason => refusals.push(reason),
		});
		await untilAborted(AbortSignal.timeout(10000), session.waitForIdle());
		expect(outcome.status).toBe("refused");
		if (outcome.status !== "refused") throw new Error("Incomplete original processing was admitted");
		expect(outcome.code).toBe("task-result-processing-incomplete");
		expect(refusals).toEqual([]);
		expect(f.calls.length).toBe(requests);
		expect(hooks).toBe(1);
		expect(await Bun.file(path.join(f.root.path(), "original-hook-effect.txt")).text()).toBe("1");
	}, 30000);

	it("qualified original final result append failure retains native evidence without exposing success or parent dispatch", async () => {
		const append = SessionManager.prototype.appendMessage;
		let failed = false;
		let hooks = 0;
		vi.spyOn(SessionManager.prototype, "appendMessage").mockImplementation(function (
			this: SessionManager,
			message,
			metadata,
		) {
			if (message.role === "toolResult" && message.toolName === "task") {
				failed = true;
				throw new Error("Original result journal append failed");
			}
			return append.call(this, message, metadata);
		});
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(failed).toBe(true);
		expect(hooks).toBe(1);
		expect(
			f.calls.filter(
				request => request.model === "parent" && request.messages.some(message => message.role === "tool"),
			),
		).toHaveLength(0);
		expect(
			f.session.messages.filter(message => message.role === "toolResult" && message.toolCallId === "original-task"),
		).toHaveLength(0);
		const disk = await loadSessionFile(f.manager.getSessionFile()!);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY),
		).toHaveLength(1);
		expect(
			disk.entries.filter(entry => entry.type === "custom" && entry.customType === TASK_RESULT_PROCESSING_STARTED),
		).toHaveLength(1);
		expect(
			disk.entries.filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
		).toHaveLength(0);
	}, 15000);

	it("qualified original delivered child remains available for later ordinary lifecycle revival", async () => {
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.run!);
		const binding = f.manager
			.getBranch()
			.map(readTaskBinding)
			.find(value => value !== undefined)!;
		const before = AgentRegistry.global().get(binding.child.registryId)?.session;
		const lifecycle = AgentLifecycleManager.global();
		await lifecycle.park(binding.child.registryId);
		expect(AgentRegistry.global().get(binding.child.registryId)?.session).toBeNull();
		const revived = await lifecycle.ensureLive(binding.child.registryId);
		expect(revived).not.toBe(before);
		expect(revived.sessionId).toBe(binding.child.sessionId);
		expect(revived.sessionFile).toBe(binding.child.sessionFile);
		await untilAborted(AbortSignal.timeout(10000), revived.prompt(assignment, { attribution: "agent" }));
		expect(revived.messages.at(-1)?.role).toBe("toolResult");
		await revived.dispose();
	}, 30000);

	it("qualified original result message-end delivery failure cannot commit or advance parent", async () => {
		const f = await fixture(false, false, true, false, {
			extensions: [
				pi => {
					pi.on("message_end", () => {});
					pi.on("tool_call", event =>
						event.toolName === "task" && event.taskResultOrigin
							? { taskResultAuthority: async () => ({ ok: true }) }
							: undefined,
					);
				},
			],
		});
		const runner = f.session.extensionRunner!;
		const emit = runner.emit.bind(runner);
		let failed = false;
		vi.spyOn(runner, "emit").mockImplementation(event => {
			if (
				event.type === "message_end" &&
				event.message.role === "toolResult" &&
				event.message.toolCallId === "original-task"
			) {
				failed = true;
				return Promise.reject(new Error("Probe original message-end delivery failed"));
			}
			return emit(event);
		});
		f.releaseParent.resolve();
		const run = f.session.sendCustomMessage(
			{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
			{ triggerTurn: true },
		);
		cleanups.push(async () => {
			f.releaseChild.resolve();
			await run.catch(() => {});
		});
		await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), run);
		await f.session.waitForIdle();
		await f.manager.flush();
		const disk = await loadSessionFile(f.manager.getSessionFile()!);
		const results = disk.entries.filter(
			e => e.type === "message" && e.message.role === "toolResult" && e.message.toolCallId === "original-task",
		);
		const requests = f.calls.filter(
			c => c.model === "parent" && c.messages.some(m => m.tool_call_id === "original-task"),
		);
		expect(failed).toBe(true);
		expect(results).toHaveLength(0);
		expect(requests).toHaveLength(0);
	}, 30000);
	it("qualified original owner follow-up during result delivery preserves input and suppresses uncommitted success", async () => {
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		cleanups.push(async () => release.resolve());
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin ? { taskResultAuthority: async () => ({ ok: true }) } : undefined,
					);
					pi.on("message_end", async event => {
						if (event.message.role === "toolResult" && event.message.toolName === "task") {
							reached.resolve();
							await release.promise;
						}
					});
				},
			],
		});
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), reached.promise);
		await f.session.sendUserMessage(assignment, { deliverAs: "followUp" });
		expect(f.session.getQueuedMessages().followUp).toEqual([assignment]);
		release.resolve();
		await untilAborted(
			AbortSignal.timeout(10000),
			f.run!.catch(() => {}),
		);
		expect(f.session.getQueuedMessages().followUp).toEqual([assignment]);
		expect(
			f.session.messages.filter(message => message.role === "toolResult" && message.toolCallId === "original-task"),
		).toHaveLength(0);
		const disk = await loadSessionFile(f.manager.getSessionFile()!);
		expect(
			disk.entries.filter(entry => entry.type === "message" && entry.message.role === "toolResult"),
		).toHaveLength(0);
		const requestsBeforeOwner = f.calls.length;
		f.releaseParent.resolve();
		await untilAborted(AbortSignal.timeout(10000), f.session.waitForIdle());
		await untilAborted(AbortSignal.timeout(10000), f.session.prompt(ownerResume));
		expect(f.session.getQueuedMessages().followUp).toEqual([]);
		const ownerRequests = f.calls
			.slice(requestsBeforeOwner)
			.filter(request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"));
		const explicitOwnerRequests = ownerRequests.filter(request =>
			request.messages.some(
				message => message.role === "user" && JSON.stringify(message.content).includes(ownerResume.trim()),
			),
		);
		expect(explicitOwnerRequests).toHaveLength(1);
		const ownerText = JSON.stringify(explicitOwnerRequests[0].messages.filter(message => message.role === "user"));
		expect(ownerText).toContain(ownerResume.trim());
		const initialRequest = f.calls
			.slice(0, requestsBeforeOwner)
			.find(request => request.model === "parent" && request.tools?.some(tool => tool.function.name === "task"))!;
		const initialText = JSON.stringify(initialRequest.messages.filter(message => message.role === "user"));
		expect(ownerText.split(assignment.trim()).length - 1).toBe(initialText.split(assignment.trim()).length);
	}, 15000);

	it("qualified original message-start authority cannot be overtaken by result commit/provider dispatch", async () => {
		const eventScope = new AsyncLocalStorage<AgentEvent>();
		const subscribe = Agent.prototype.subscribe;
		vi.spyOn(Agent.prototype, "subscribe").mockImplementation(function (this: Agent, fn) {
			return subscribe.call(this, event => eventScope.run(event, () => fn(event)));
		});
		const parentBoundary = Promise.withResolvers<void>();
		const addBeforeModel = Agent.prototype.addBeforeModelCall;
		vi.spyOn(Agent.prototype, "addBeforeModelCall").mockImplementation(function (this: Agent, fn) {
			return addBeforeModel.call(this, (context, signal) => {
				if (context.messages.some(m => m.role === "toolResult" && m.toolCallId === "original-task"))
					parentBoundary.resolve();
				return fn(context, signal);
			});
		});
		const held = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		cleanups.push(async () => release.resolve());
		let stopped = false;
		const f = await fixture(false, true, true, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.toolName === "task" && event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										const phase = eventScope.getStore();
										if (
											!stopped &&
											phase?.type === "message_start" &&
											phase.message.role === "toolResult" &&
											phase.message.toolCallId === "original-task"
										) {
											stopped = true;
											held.resolve();
											await release.promise;
										}
										return { ok: true };
									},
								}
							: undefined,
					);
				},
			],
		});
		await untilAborted(AbortSignal.timeout(10000), f.childReached.promise);
		f.releaseChild.resolve();
		await untilAborted(AbortSignal.timeout(10000), held.promise);
		await untilAborted(AbortSignal.timeout(10000), parentBoundary.promise);
		const before = await loadSessionFile(f.manager.getSessionFile()!);
		const resultsBefore = before.entries.filter(
			e => e.type === "message" && e.message.role === "toolResult" && e.message.toolCallId === "original-task",
		);
		const providerBefore = f.calls.filter(
			c => c.model === "parent" && c.messages.some(m => m.tool_call_id === "original-task"),
		);
		expect(resultsBefore).toHaveLength(0);
		expect(providerBefore).toHaveLength(0);
		release.resolve();
		f.releaseParent.resolve();
		await f.run;
		await f.session.waitForIdle();
		await f.manager.flush();
		const after = await loadSessionFile(f.manager.getSessionFile()!);
		const resultsAfter = after.entries.filter(
			e => e.type === "message" && e.message.role === "toolResult" && e.message.toolCallId === "original-task",
		);
		const live = f.session.messages.filter(m => m.role === "toolResult" && m.toolCallId === "original-task");
		expect(resultsAfter).toHaveLength(1);
		expect(live).toHaveLength(1);
		expect(
			f.calls.filter(c => c.model === "parent" && c.messages.some(m => m.tool_call_id === "original-task")),
		).toHaveLength(1);
	}, 30000);

	it("qualified original capture cannot adopt a foreign leaf during initial persistence", async () => {
		const f = await fixture(false, false, true, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.toolName === "task" && event.taskResultOrigin
							? { taskResultAuthority: async () => ({ ok: true }) }
							: undefined,
					);
				},
			],
		});
		let capturing = false;
		let foreign: string | undefined;
		const capture = f.session.captureTaskCall.bind(f.session);
		vi.spyOn(f.session, "captureTaskCall").mockImplementation(async (...args) => {
			capturing = true;
			try {
				return await capture(...args);
			} finally {
				capturing = false;
			}
		});
		const flush = f.manager.flush.bind(f.manager);
		vi.spyOn(f.manager, "flush").mockImplementation(async () => {
			await flush();
			if (capturing && !foreign) {
				foreign = f.manager.appendCustomEntry("foreign-owner-during-initial-persistence", { owner: "new" });
				await flush();
			}
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await f.session.sendCustomMessage(
			{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
			{ triggerTurn: true },
		);
		await f.session.waitForIdle();
		await f.manager.flush();
		const children = f.calls.filter(c => c.model === "child");
		const ready = f.manager
			.getEntries()
			.filter(e => e.type === "custom" && e.customType === TASK_NATIVE_RESULT_READY);
		expect(foreign).toBeDefined();
		expect(children).toHaveLength(0);
		expect(ready).toHaveLength(0);
	}, 30000);
	it("qualified original post-flush ownership loss preserves already-durable original result", async () => {
		const f = await fixture(false, false, true, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.toolName === "task" && event.taskResultOrigin
							? { taskResultAuthority: async () => ({ ok: true }) }
							: undefined,
					);
				},
			],
		});
		let foreign: string | undefined;
		const flush = f.manager.flush.bind(f.manager);
		vi.spyOn(f.manager, "flush").mockImplementation(async () => {
			await flush();
			const leaf = f.manager.getEntry(f.manager.getLeafId()!);
			if (
				!foreign &&
				leaf?.type === "message" &&
				leaf.message.role === "toolResult" &&
				leaf.message.toolCallId === "original-task"
			) {
				foreign = f.manager.appendCustomEntry("foreign-owner-after-durable-result", { owner: "new" });
				await flush();
			}
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await f.session.sendCustomMessage(
			{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
			{ triggerTurn: true },
		);
		await f.session.waitForIdle();
		await f.manager.flush();
		const disk = await loadSessionFile(f.manager.getSessionFile()!);
		const results = disk.entries.filter(
			e => e.type === "message" && e.message.role === "toolResult" && e.message.toolCallId === "original-task",
		);
		const live = f.session.messages.filter(m => m.role === "toolResult" && m.toolCallId === "original-task");
		const requests = f.calls.filter(
			c => c.model === "parent" && c.messages.some(m => m.tool_call_id === "original-task"),
		);
		expect(foreign).toBeDefined();
		expect(results).toHaveLength(1);
		expect(requests).toHaveLength(0);
		expect(live).toHaveLength(1);
	}, 30000);

	it("qualified original retained authority cannot downgrade when the session changes before capture", async () => {
		let validations = 0;
		let hooks = 0;
		const f = await fixture(false, false, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", event =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										validations++;
										return { ok: true };
									},
								}
							: undefined,
					);
					pi.on("tool_result", event => {
						if (event.toolName === "task") hooks++;
					});
				},
			],
		});
		const replacement = SessionManager.create(f.root.path(), path.join(f.root.path(), "other-sessions"));
		await replacement.ensureOnDisk();
		const replacementFile = replacement.getSessionFile()!;
		await replacement.close();
		const capture = f.session.captureTaskCall.bind(f.session);
		vi.spyOn(f.session, "captureTaskCall").mockImplementation(async (...args) => {
			await f.manager.setSessionFile(replacementFile);
			return capture(...args);
		});
		await untilAborted(
			AbortSignal.timeout(10000),
			f.session
				.sendCustomMessage(
					{ customType: "work-execute", content: assignment, display: false, attribution: "agent" },
					{ triggerTurn: true },
				)
				.catch(() => {}),
		);
		expect(validations).toBe(0);
		expect(hooks).toBe(0);
		expect(f.calls.filter(request => request.model === "child")).toHaveLength(0);
		expect(
			f.manager
				.getEntries()
				.filter(entry => entry.type === "custom" && entry.customType === TASK_NATIVE_RESULT_READY),
		).toHaveLength(0);
	}, 15000);

	it("qualified original child bootstrap rejects foreign journal append during initial authority", async () => {
		let changed = false;
		const f = await fixture(false, true, false, false, {
			extensions: [
				pi => {
					pi.on("tool_call", (event, ctx) =>
						event.taskResultOrigin
							? {
									taskResultAuthority: async () => {
										const binding = ctx.sessionManager
											.getBranch()
											.map(readTaskBinding)
											.find(value => value !== undefined);
										const child = binding
											? AgentRegistry.global().get(binding.child.registryId)?.session
											: undefined;
										if (
											child &&
											!changed &&
											!child.sessionManager.getBranch().some(entry => readPreparationRecord(entry))
										) {
											changed = true;
											child.sessionManager.appendCustomEntry(
												"foreign-owner-during-initial-child-authority",
												{ owner: "changed" },
											);
											await child.sessionManager.flush();
										}
										return { ok: true };
									},
								}
							: undefined,
					);
				},
			],
		});
		f.releaseChild.resolve();
		f.releaseParent.resolve();
		await f.run;
		await f.session.waitForIdle();
		const children = f.calls.filter(c => c.model === "child");
		const ready = f.manager
			.getEntries()
			.filter(e => e.type === "custom" && e.customType === TASK_NATIVE_RESULT_READY);
		expect(changed).toBe(true);
		expect(children).toHaveLength(0);
		expect(ready).toHaveLength(0);
	}, 30000);

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
