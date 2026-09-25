import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { ExtensionRuntime, loadExtensionFromFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type {
	ExtensionContextActions,
	LoadExtensionsResult,
} from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import type { CreateAgentSessionResult } from "@oh-my-pi/pi-coding-agent/sdk";
import * as sdkModule from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession, AgentSessionEvent } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { USER_INTERRUPT_LABEL } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { logger } from "@oh-my-pi/pi-utils";

describe("task executor extension abort handler", () => {
	let tempDir: string;
	let unhandledRejections: unknown[] = [];
	const unhandledListener = (reason: unknown) => {
		unhandledRejections.push(reason);
	};

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "executor-abort-test-"));
		unhandledRejections = [];
		process.on("unhandledRejection", unhandledListener);
	});

	afterEach(async () => {
		process.off("unhandledRejection", unhandledListener);
		vi.restoreAllMocks();
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	const baseAgent: AgentDefinition = {
		name: "task",
		description: "test",
		systemPrompt: "test",
		source: "bundled",
	};

	const mockRegistry = {
		refresh: async () => {},
		getAvailable: () => [],
		getApiKey: async () => "test-key",
	} as unknown as ModelRegistry;

	function createYieldingSession(
		abortMock: (options?: unknown) => Promise<void>,
		extensionRunner?: ExtensionRunner,
	): AgentSession {
		const session: Partial<AgentSession> = {
			state: { messages: [] } as never,
			agent: { state: { systemPrompt: ["test"] } } as never,
			extensionRunner: extensionRunner as never,
			sessionManager: { appendSessionInit: () => {} } as never,
			getActiveToolNames: () => ["read", "yield"],
			getEnabledToolNames: () => ["read", "yield"],
			setActiveToolsByName: async () => {},
			subscribe: (listener: (event: AgentSessionEvent) => void) => {
				queueMicrotask(() => {
					listener({
						type: "tool_execution_end",
						toolCallId: "tool-yield",
						toolName: "yield",
						result: {
							content: [{ type: "text", text: "Result submitted." }],
							details: { status: "success", data: { ok: true } },
						},
						isError: false,
					} as AgentSessionEvent);
				});
				return () => {};
			},
			prompt: async () => true,
			waitForIdle: async () => {},
			prepareForHeadlessAdvisorDrain: () => {},
			waitForAdvisorCatchup: async () => true,
			getLastAssistantMessage: () => undefined,
			abort: abortMock,
			dispose: async () => {},
			setIrcWakeTurnObserver: () => {},
			subscribeRunState: () => () => {},
		};
		return session as AgentSession;
	}

	for (const [faultKind, fault] of [
		["Error", new Error("session abort failed")],
		["string", "session abort failed as string"],
		["null-prototype", Object.assign(Object.create(null), { message: "session abort null-prototype" })],
	] as const) {
		for (const [loggerErrorKind, loggerError] of [
			["Error", new Error("logger.error exploded")],
			["string", "logger.error threw string"],
			["null-prototype", Object.assign(Object.create(null), { message: "logger null-prototype" })],
		] as const) {
			it(`survives ${faultKind} abort rejection with throwing logger (${loggerErrorKind}) during runSubprocess extension session_start`, async () => {
				const eventBus = new EventBus();
				const runtime = new ExtensionRuntime();
				const sessionManager = SessionManager.inMemory();
				let abortCalled = false;

				const extension = await loadExtensionFromFactory(
					pi => {
						pi.on("session_start", (_event, ctx) => {
							abortCalled = true;
							ctx.abort();
						});
					},
					tempDir,
					eventBus,
					runtime,
					"test-abort-extension",
				);
				const runner = new ExtensionRunner([extension], runtime, tempDir, sessionManager, mockRegistry);

				const abortMock = vi.fn().mockImplementation(async (options?: unknown) => {
					if ((options as { reason?: string } | undefined)?.reason === USER_INTERRUPT_LABEL) {
						throw fault;
					}
				});

				const fakeSession = createYieldingSession(abortMock, runner);

				vi.spyOn(sdkModule, "createAgentSession").mockResolvedValue({
					session: fakeSession,
					extensionsResult: { extensions: [extension], errors: [], runtime } as unknown as LoadExtensionsResult,
					setToolUIContext: () => {},
					eventBus,
				} satisfies CreateAgentSessionResult);

				const loggerErrorSpy = vi.spyOn(logger, "error").mockImplementation(() => {
					throw loggerError;
				});

				const result = await runSubprocess({
					cwd: tempDir,
					agent: baseAgent,
					task: "do work",
					index: 0,
					id: `subagent-abort-${faultKind}-${loggerErrorKind}`,
					settings: Settings.isolated(),
					modelRegistry: mockRegistry,
					enableLsp: false,
					eventBus,
				});

				// Wait a microtask tick for any unhandled rejections to settle
				await new Promise(resolve => setTimeout(resolve, 50));

				expect(abortCalled).toBe(true);
				expect(abortMock).toHaveBeenCalledWith({ reason: USER_INTERRUPT_LABEL });
				expect(loggerErrorSpy).toHaveBeenCalled();
				expect(unhandledRejections).toEqual([]);
				expect(result.aborted).toBe(false);
			});
		}

		it(`logs extension error when logger does not throw for ${faultKind} abort rejection`, async () => {
			let capturedContextActions: ExtensionContextActions | undefined;
			const fakeRunner = {
				initialize: (_actions: unknown, contextActions: ExtensionContextActions) => {
					capturedContextActions = contextActions;
				},
				onError: () => {},
				emit: async () => {},
			};

			const abortMock = vi.fn().mockImplementation(async () => {
				throw fault;
			});

			const fakeSession = createYieldingSession(abortMock, fakeRunner as unknown as ExtensionRunner);

			vi.spyOn(sdkModule, "createAgentSession").mockResolvedValue({
				session: fakeSession,
				extensionsResult: { extensions: [], errors: [], runtime: {} } as unknown as LoadExtensionsResult,
				setToolUIContext: () => {},
				eventBus: new EventBus(),
			} satisfies CreateAgentSessionResult);

			const loggedEvents: Array<{ message: string; meta?: Record<string, unknown> }> = [];
			const loggerErrorSpy = vi.spyOn(logger, "error").mockImplementation((message, meta) => {
				loggedEvents.push({ message, meta });
			});

			await runSubprocess({
				cwd: tempDir,
				agent: baseAgent,
				task: "do work",
				index: 0,
				id: `subagent-direct-${faultKind}`,
				settings: Settings.isolated(),
				modelRegistry: mockRegistry,
				enableLsp: false,
			});

			expect(capturedContextActions).toBeDefined();

			// Directly invoke the executor's abort action
			capturedContextActions!.abort();

			// Wait for promise chain
			await new Promise(resolve => setTimeout(resolve, 50));

			expect(abortMock).toHaveBeenCalled();
			expect(loggerErrorSpy).toHaveBeenCalledWith("Extension error", {
				path: "<task-executor>",
				event: "abort",
				error:
					faultKind === "null-prototype"
						? "session abort null-prototype"
						: faultKind === "string"
							? "session abort failed as string"
							: "session abort failed",
			});
			expect(unhandledRejections).toEqual([]);
		});

		it(`directly driving executor contextActions.abort with ${faultKind} abort rejection and throwing logger does not throw or unhandle`, async () => {
			let capturedContextActions: ExtensionContextActions | undefined;
			const fakeRunner = {
				initialize: (_actions: unknown, contextActions: ExtensionContextActions) => {
					capturedContextActions = contextActions;
				},
				onError: () => {},
				emit: async () => {},
			};

			const abortMock = vi.fn().mockImplementation(async () => {
				throw fault;
			});

			const fakeSession = createYieldingSession(abortMock, fakeRunner as unknown as ExtensionRunner);

			vi.spyOn(sdkModule, "createAgentSession").mockResolvedValue({
				session: fakeSession,
				extensionsResult: { extensions: [], errors: [], runtime: {} } as unknown as LoadExtensionsResult,
				setToolUIContext: () => {},
				eventBus: new EventBus(),
			} satisfies CreateAgentSessionResult);

			vi.spyOn(logger, "error").mockImplementation(() => {
				throw new Error("logger threw in direct invocation");
			});

			await runSubprocess({
				cwd: tempDir,
				agent: baseAgent,
				task: "do work",
				index: 0,
				id: `subagent-direct-throw-${faultKind}`,
				settings: Settings.isolated(),
				modelRegistry: mockRegistry,
				enableLsp: false,
			});

			expect(capturedContextActions).toBeDefined();

			// Directly invoke the executor's abort action; it must not throw synchronously
			expect(() => capturedContextActions!.abort()).not.toThrow();

			// Wait for promise chain
			await new Promise(resolve => setTimeout(resolve, 50));

			expect(abortMock).toHaveBeenCalled();
			expect(unhandledRejections).toEqual([]);
		});
	}
});
