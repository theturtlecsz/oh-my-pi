/**
 * Tests for AgentSession concurrent prompt guard.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { scheduler } from "node:timers/promises";
import { type } from "@oh-my-pi/omptype";
import { Agent, type AgentEvent, type AgentMessage, type AgentTool } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage, ToolCall } from "@oh-my-pi/pi-ai";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { AsyncJobManager } from "@oh-my-pi/pi-coding-agent/async";
import type { Rule } from "@oh-my-pi/pi-coding-agent/capability/rule";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { TtsrManager } from "@oh-my-pi/pi-coding-agent/export/ttsr";
import { ExtensionRuntime, loadExtensionFromFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type { ExtensionError } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { GoalRuntime } from "@oh-my-pi/pi-coding-agent/goals/runtime";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import {
	convertToLlm,
	shouldRenderAbortReason,
	USER_INTERRUPT_LABEL,
} from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TtsrCoordinator, type TtsrCoordinatorHost } from "@oh-my-pi/pi-coding-agent/session/ttsr-coordinator";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { logger, removeSyncWithRetries, Snowflake, untilAborted } from "@oh-my-pi/pi-utils";
import { YieldGate } from "../../agent/src/utils/yield";

// Mock stream that mimics AssistantMessageEventStream

// AgentSession schedules its TTSR retry and context-promotion continuations
// through `scheduler.wait(delayMs, { signal })` (node:timers/promises), with
// blind 50ms/100ms "settle" delays. Tests that drive a continuation to
// completion would otherwise pay that wall-clock time on every run. This spy
// collapses the blind delay to a single macrotask hop (`scheduler.wait(0)`)
// while preserving the real abort-signal semantics, so the continuation still
// fires only after the aborted/overflowed turn has been recorded. Each test
// that opts in must run inside a block whose afterEach restores mocks.
const originalSchedulerWait = scheduler.wait.bind(scheduler);
function collapseSchedulerSettleDelays(): void {
	vi.spyOn(scheduler, "wait").mockImplementation((_delayMs, options) => originalSchedulerWait(0, options));
}
let sharedDir: string;
let sharedAuthStorage: AuthStorage;
let sharedModelRegistry: ModelRegistry;

beforeAll(async () => {
	sharedDir = path.join(os.tmpdir(), `pi-concurrent-shared-${Snowflake.next()}`);
	fs.mkdirSync(sharedDir, { recursive: true });
	sharedAuthStorage = await AuthStorage.create(path.join(sharedDir, "auth.db"));
	sharedAuthStorage.setRuntimeApiKey("anthropic", "test-key");
	sharedAuthStorage.setRuntimeApiKey("openai-codex", "test-key");
	sharedModelRegistry = new ModelRegistry(sharedAuthStorage, path.join(sharedDir, "models.yml"));
});

afterAll(() => {
	sharedAuthStorage.close();
	removeSyncWithRetries(sharedDir);
});

describe("AgentSession concurrent prompt guard", () => {
	let session: AgentSession;
	let tempDir: string;

	beforeEach(() => {
		// Collapse scheduler settle delays so the post-abort auto-continue and
		// dispose teardown are deterministic instead of racing the wall clock.
		collapseSchedulerSettleDelays();
		tempDir = path.join(os.tmpdir(), `pi-concurrent-test-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
	});

	afterEach(async () => {
		if (session) {
			await session.dispose();
		}
		if (tempDir && fs.existsSync(tempDir)) {
			removeSyncWithRetries(tempDir);
		}
		vi.restoreAllMocks();
		AsyncJobManager.resetForTests();
	});

	it("continues a main session from session_stop feedback before settling", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const stopEvents: Array<{
			messages: AgentMessage[];
			stop_hook_active: boolean;
			session_id: string;
			turn_id: number;
			last_assistant_message?: AgentMessage;
		}> = [];
		const eventOrder: string[] = [];
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn(event => {
				eventOrder.push(event.type);
				return Promise.resolve(undefined);
			}),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn(event => {
				eventOrder.push("session_stop");
				stopEvents.push(event);
				if (stopEvents.length === 1) {
					return Promise.resolve({ continue: true, additionalContext: "Mission incomplete; continue." });
				}
				return Promise.resolve(undefined);
			}),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		await session.prompt("First message");
		await session.waitForIdle();

		const callMessages = mock.calls.map(call => call.context.messages);
		expect(callMessages).toHaveLength(2);
		expect(
			callMessages[1]?.some(message =>
				typeof message.content === "string"
					? message.content.includes("Mission incomplete; continue.")
					: message.content.some(
							content => content.type === "text" && content.text.includes("Mission incomplete; continue."),
						),
			),
		).toBe(true);
		expect(eventOrder.filter(type => type === "session_stop" || type === "agent_end")).toEqual([
			"session_stop",
			"agent_end",
			"session_stop",
			"agent_end",
		]);
		expect(stopEvents.map(event => event.stop_hook_active)).toEqual([false, true]);
		expect(stopEvents.map(event => event.turn_id)).toEqual([0, 0]);
		expect(stopEvents[0]?.session_id).toBe(session.sessionId);
		expect(stopEvents[0]?.last_assistant_message?.role).toBe("assistant");
		expect(
			stopEvents[1]?.messages.some(
				message =>
					message.role === "user" &&
					Array.isArray(message.content) &&
					message.content.some(block => block.type === "text" && block.text === "First message"),
			),
		).toBe(true);
	});

	it("uses non-empty session_stop reason when additional context is empty", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		let stopCount = 0;
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn(() => {
				stopCount++;
				if (stopCount === 1) {
					return Promise.resolve({
						continue: true,
						additionalContext: "",
						reason: "Continue from fallback reason.",
					});
				}
				return Promise.resolve(undefined);
			}),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		await session.prompt("First message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(2);
		expect(
			mock.calls[1]?.context.messages.some(message =>
				typeof message.content === "string"
					? message.content.includes("Continue from fallback reason.")
					: message.content.some(
							content => content.type === "text" && content.text.includes("Continue from fallback reason."),
						),
			),
		).toBe(true);
	});

	it("does not emit session_stop when abort starts before the settle pass", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const settleGate = Promise.withResolvers<void>();
		const settleReached = Promise.withResolvers<void>();
		const emitSessionStop = vi.fn().mockResolvedValue(undefined);
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop,
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });
		vi.spyOn(session.goalRuntime, "onAgentEnd").mockImplementation(() => {
			settleReached.resolve();
			return settleGate.promise;
		});

		const promptPromise = session.prompt("First message");
		await settleReached.promise;
		const abortPromise = session.abort();
		settleGate.resolve();

		await abortPromise;
		await promptPromise;
		await session.waitForIdle();

		expect(emitSessionStop).not.toHaveBeenCalled();
	});

	it("cancels an active session_stop pass without applying stale continuation feedback", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const stopStarted = Promise.withResolvers<void>();
		const stopHook = Promise.withResolvers<{ continue: true; additionalContext: string }>();
		let firstStopSignal: AbortSignal | undefined;
		let stopCount = 0;
		const extensionRuntime = new ExtensionRuntime();
		const extension = await loadExtensionFromFactory(
			pi => {
				pi.on("session_stop", event => {
					stopCount++;
					if (stopCount !== 1) return;
					firstStopSignal = event.signal;
					stopStarted.resolve();
					return stopHook.promise;
				});
			},
			tempDir,
			new EventBus(),
			extensionRuntime,
			"slow-session-stop",
		);
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		const extensionRunner = new ExtensionRunner(
			[extension],
			extensionRuntime,
			tempDir,
			sessionManager,
			modelRegistry,
		);
		const extensionErrors: string[] = [];
		extensionRunner.onError(error => extensionErrors.push(error.error));

		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		const promptPromise = session.prompt("First message");
		await stopStarted.promise;
		let abortSettled = false;
		const abortPromise = session.abort().then(() => {
			abortSettled = true;
		});
		await scheduler.yield();
		const abortSettledBeforeHandler = abortSettled;
		const signalWasCancelled = firstStopSignal?.aborted;
		stopHook.resolve({ continue: true, additionalContext: "Should not run after abort." });

		await abortPromise;
		await promptPromise;
		await session.waitForIdle();

		expect(abortSettledBeforeHandler).toBe(true);
		expect(signalWasCancelled).toBe(true);
		expect(extensionErrors).toEqual([]);
		expect(mock.calls).toHaveLength(1);
		expect(session.queuedMessageCount).toBe(0);

		await session.prompt("Second message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(2);
		expect(
			mock.calls[1]?.context.messages.some(message =>
				typeof message.content === "string"
					? message.content.includes("Should not run after abort.")
					: message.content.some(
							content => content.type === "text" && content.text.includes("Should not run after abort."),
						),
			),
		).toBe(false);
	});

	it("caps consecutive session_stop continuations at eight", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Pass"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn(() => Promise.resolve({ decision: "block" as const, reason: "Run another pass." })),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		await session.prompt("First message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(9);
		expect(extensionRunner.emitSessionStop).toHaveBeenCalledTimes(9);
	});

	it("emits session_stop only after empty-stop recovery reaches a final stop", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			responses: [{ content: [""] }, { content: ["Recovered"] }],
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn().mockResolvedValue(undefined),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		await session.prompt("First message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(2);
		expect(extensionRunner.emitSessionStop).toHaveBeenCalledTimes(1);
	});

	it("emits session_stop after empty-stop retry cap settles", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			responses: [{ content: [""] }, { content: [""] }, { content: [""] }, { content: [""] }],
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn().mockResolvedValue(undefined),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		await session.prompt("First message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(4);
		expect(extensionRunner.emitSessionStop).toHaveBeenCalledTimes(1);
	});

	it("continues session_stop feedback in ACP sessions with deferred client turns", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		let stopCount = 0;
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn(() => {
				stopCount++;
				if (stopCount === 1) {
					return Promise.resolve({ continue: true, additionalContext: "ACP stop continuation." });
				}
				return Promise.resolve(undefined);
			}),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });
		session.setClientBridge({
			capabilities: {},
			deferAgentInitiatedTurns: true,
		});

		await session.prompt("First message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(2);
		expect(
			mock.calls[1]?.context.messages.some(message =>
				typeof message.content === "string"
					? message.content.includes("ACP stop continuation.")
					: message.content.some(
							content => content.type === "text" && content.text.includes("ACP stop continuation."),
						),
			),
		).toBe(true);
	});

	it("does not emit session_stop for subagent sessions", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({
			handler: () => ({ content: ["Subagent done"] }),
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
			convertToLlm,
		});
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn().mockResolvedValue(undefined),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			extensionRunner,
			agentKind: "sub",
		});

		await session.prompt("Subagent message");
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(1);
		expect(extensionRunner.emit).toHaveBeenCalledWith({ type: "agent_end", messages: expect.any(Array) });
		expect(extensionRunner.emitSessionStop).not.toHaveBeenCalled();
	});

	it("should allow prompt() after previous completes", async () => {
		// Create session with a stream that completes immediately
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({ handler: () => ({ content: ["Done"] }) });
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: {
				model,
				systemPrompt: ["Test"],
				tools: [],
			},
			streamFn: mock.stream,
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
		});

		// First prompt completes
		await session.prompt("First message");

		// Should not be streaming anymore
		expect(session.isStreaming).toBe(false);

		// Second prompt should work
		await expect(session.prompt("Second message")).resolves.toBe(true);
	});
	it("queues extension follow-up user messages on an idle session without starting a turn", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({ handler: () => ({ content: ["Done"] }) });
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: {
				model,
				systemPrompt: ["Test"],
				tools: [],
			},
			streamFn: mock.stream,
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
		});

		await session.sendUserMessage("hello from session_start", { deliverAs: "followUp" });

		expect(mock.calls).toHaveLength(0);
		expect(session.queuedMessageCount).toBe(1);
	});

	// Regression: a subscriber that fires the next prompt synchronously from the
	// agent_end listener (the shape every wire transport ends up in — rpc-mode
	// stdout subscriber, ACP bridge, Cursor exec) must not collide with the
	// outgoing turn's still-unwinding in-flight bookkeeping. Before the wire-level
	// agent_end was deferred until #promptInFlightCount drops to 0, the
	// subscriber observed agent_end while Session.isStreaming was still true (the
	// agent's own `isStreaming` had flipped, but #promptWithMessage's finally had
	// not yet decremented the prompt-in-flight counter), and the next prompt
	// threw AgentBusyError. Surfaced as `RpcCommandError: prompt: Agent is
	// already processing` from omp-rpc clients (robomp triage reminder path).

	it("does not let extension notifications block public agent_end", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({ handler: () => ({ content: ["Done"] }) });
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: mock.stream,
		});
		const { promise: extensionGate, resolve: releaseExtension } = Promise.withResolvers<void>();
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn((event: { type: string }) =>
				event.type === "agent_end" ? extensionGate : Promise.resolve(undefined),
			),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn().mockReturnValue(false),
		} as unknown as ExtensionRunner;
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, extensionRunner });

		const { promise: publicAgentEnd, resolve: onPublicAgentEnd } = Promise.withResolvers<void>();
		session.subscribe(event => {
			if (event.type === "agent_end") onPublicAgentEnd();
		});

		await session.prompt("First message");
		await publicAgentEnd;
		expect(extensionRunner.emit).toHaveBeenCalledWith({ type: "agent_end", messages: expect.any(Array) });

		releaseExtension();
		await session.waitForIdle();
	});

	it("queues idle ACP client-triggered custom messages instead of starting an ownerless turn", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const mock = createMockModel({ handler: () => ({ content: ["Done"] }) });
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: {
				model,
				systemPrompt: ["Test"],
				tools: [],
			},
			convertToLlm,
			streamFn: mock.stream,
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
		});
		session.setClientBridge({
			capabilities: {},
			deferAgentInitiatedTurns: true,
		});

		await session.prompt("First message");
		expect(session.isStreaming).toBe(false);
		const callsAfterFirstPrompt = mock.calls.length;

		await session.sendCustomMessage(
			{
				customType: "async-result",
				content: "Background result",
				display: true,
				attribution: "agent",
			},
			{ deliverAs: "followUp", triggerTurn: true },
		);

		expect(mock.calls).toHaveLength(callsAfterFirstPrompt);
		expect(session.isStreaming).toBe(false);

		await session.prompt("Next user prompt");
		await session.dispose();
		session = undefined as unknown as AgentSession;
		expect(mock.calls).toHaveLength(callsAfterFirstPrompt + 1);
		expect(
			mock.calls.at(-1)?.context.messages.some(message => {
				if (typeof message.content === "string") {
					return message.content.includes("Background result");
				}

				return message.content.some(
					content => content.type === "text" && content.text.includes("Background result"),
				);
			}),
		).toBe(true);
	});
});

describe("AgentSession TTSR resume gate", () => {
	let session: AgentSession;
	let tempDir: string;

	beforeEach(() => {
		tempDir = path.join(os.tmpdir(), `pi-ttsr-gate-test-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
	});

	afterEach(async () => {
		if (session) {
			await session.dispose();
		}
		if (tempDir && fs.existsSync(tempDir)) {
			removeSyncWithRetries(tempDir);
		}
		vi.restoreAllMocks();
	});

	const testRule: Rule = {
		name: "no-unwrap",
		path: "/tmp/no-unwrap.md",
		content: "Do not use .unwrap()",
		condition: ["\\.unwrap\\("],
		_source: { provider: "test", providerName: "test", path: "/tmp/no-unwrap.md", level: "project" },
	};

	function makeMsg(text: string, stopReason: "stop" | "aborted" = "stop"): AssistantMessage {
		return {
			role: "assistant",
			content: [{ type: "text", text }],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason,
			timestamp: Date.now(),
		};
	}

	function pushContinuationStream(stream: AssistantMessageEventStream, onComplete: () => void): void {
		queueMicrotask(() => {
			const partial = makeMsg("");
			stream.push({ type: "start", partial });
			onComplete();
			stream.push({
				type: "done",
				reason: "stop",
				message: makeMsg('Fixed: let val = result.expect("msg")'),
			});
		});
	}

	function pushAbortableTtsrStream(stream: AssistantMessageEventStream, signal: AbortSignal | undefined): void {
		queueMicrotask(() => {
			const partial = makeMsg("");
			stream.push({ type: "start", partial });
			stream.push({
				type: "text_delta",
				contentIndex: 0,
				delta: "let val = result.unwrap(",
				partial: { ...makeMsg("let val = result.unwrap("), timestamp: partial.timestamp },
			});
			if (signal) {
				signal.addEventListener(
					"abort",
					() => {
						stream.push({
							type: "error",
							reason: "aborted",
							error: { ...makeMsg("let val = result.unwrap(", "aborted"), timestamp: partial.timestamp },
						});
					},
					{ once: true },
				);
			}
		});
	}

	function recordedLifecycleFixture() {
		const manager = new TtsrManager({
			enabled: true,
			contextMode: "keep",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		const agent = new Agent({ initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")! } });
		const journal = SessionManager.inMemory();
		const core = Promise.withResolvers<void>();
		const continuation = Promise.withResolvers<void>();
		const continued = Promise.withResolvers<void>();
		vi.spyOn(agent, "waitForIdle").mockReturnValue(core.promise);
		vi.spyOn(agent, "continue").mockImplementation(() => {
			continued.resolve();
			return continuation.promise;
		});
		const tasks: Array<{ run: (signal: AbortSignal) => Promise<void>; skip?: () => void }> = [];
		const deferred: Array<Parameters<TtsrCoordinatorHost["scheduleAgentContinue"]>[0]> = [];
		const notices: string[] = [];
		const emitSessionEvent = vi.fn(async () => {});
		let generation = 1;
		const coordinator = new TtsrCoordinator(
			{
				agent,
				sessionManager: journal,
				settings: Settings.isolated(),
				emitSessionEvent,
				emitNotice: (_level, message) => {
					notices.push(message);
				},
				promptGeneration: () => generation,
				schedulePostPromptTask: (run, options) => {
					tasks.push({ run, skip: options?.onSkip });
				},
				scheduleAgentContinue: options => {
					deferred.push(options);
				},
			},
			manager,
		);
		const trigger = async (timestamp: number, interruptMode: "always" | "never" = "always") => {
			manager.addRule({ ...testRule, name: `no-unwrap-${timestamp}`, interruptMode });
			coordinator.onTurnStart();
			const message = { ...makeMsg("result.unwrap("), timestamp };
			await coordinator.checkMessageUpdate({
				type: "message_update",
				message,
				assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "result.unwrap(", partial: message },
			});
			return { ...message, stopReason: "aborted" as const };
		};
		const captureTextMatch = (timestamp: number, name: string, token: string): AgentEvent => {
			manager.addRule({ ...testRule, name, condition: [token] });
			const message = { ...makeMsg(token), timestamp };
			const event: AgentEvent = {
				type: "message_update",
				message,
				assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: token, partial: message },
			};
			coordinator.onEventEntry(event);
			return event;
		};
		const record = (message: AssistantMessage) => {
			agent.appendMessage(message);
			journal.appendMessage(message);
			coordinator.onAssistantRecorded(message);
		};
		return {
			coordinator,
			manager,
			agent,
			journal,
			core,
			continuation,
			continued,
			tasks,
			deferred,
			notices,
			emitSessionEvent,
			trigger,
			captureTextMatch,
			record,
			advanceGeneration: () => {
				generation++;
			},
		};
	}

	it.each([
		"absent-target",
		"absent-record",
		"rejected-after-record",
		"unrelated-same-timestamp",
		"missing-index",
		"core-idle-rejection",
	] as const)("recorded TTSR lifecycle surfaces %s without injection or a stranded gate", async mode => {
		const f = recordedLifecycleFixture();
		const abort = vi.spyOn(f.agent, "abort");
		const message = await f.trigger(101);
		const late = f.captureTextMatch(101, "late-failed-rule", "lateFailedRule");
		const processing = Promise.withResolvers<void>();
		if (mode !== "absent-target")
			f.coordinator.observeProcessing({ type: "message_end", message }, processing.promise);
		if (["rejected-after-record", "unrelated-same-timestamp", "missing-index", "core-idle-rejection"].includes(mode))
			f.record(message);
		if (mode === "unrelated-same-timestamp") f.agent.replaceMessages([{ ...message }]);
		if (mode === "missing-index") f.agent.replaceMessages([]);
		// Attach the real task's rejection handling before rejecting either fixture promise.
		const running = f.tasks[0]!.run(new AbortController().signal);
		if (mode === "core-idle-rejection") f.core.reject(new Error("original core failed"));
		else f.core.resolve();
		if (mode === "rejected-after-record") processing.reject(new Error("post-append processing failed"));
		else processing.resolve();
		await running;
		expect(f.notices).toHaveLength(1);
		expect(f.agent.continue).not.toHaveBeenCalled();
		expect(f.journal.getEntries().some(entry => entry.type === "custom_message")).toBe(false);
		expect(f.coordinator.resumeGate).toBeUndefined();
		expect(f.coordinator.abortPending).toBe(false);
		expect(await f.coordinator.checkMessageUpdate(late)).toBe(false);
		expect(f.tasks).toHaveLength(1);
		expect(abort).toHaveBeenCalledTimes(1);
		expect(f.emitSessionEvent).toHaveBeenCalledTimes(1);
		f.coordinator.onTurnStart();
		const fresh = f.captureTextMatch(103, "fresh-after-failure", "freshAfterFailure");
		const checkDelta = vi.spyOn(f.manager, "checkDelta");
		expect(await f.coordinator.checkMessageUpdate(fresh)).toBe(true);
		expect(checkDelta.mock.results[0]?.value).toEqual([expect.objectContaining({ name: "fresh-after-failure" })]);
		expect(f.tasks).toHaveLength(2);
		expect(abort).toHaveBeenCalledTimes(2);
		f.coordinator.resolveResume();
	});

	it.each(["before-wait", "during-core", "during-handler", "generation", "dispose", "skip"] as const)(
		"recorded TTSR lifecycle cancels %s without waiting for a delayed handler",
		async mode => {
			const f = recordedLifecycleFixture();
			const message = await f.trigger(102);
			const processing = Promise.withResolvers<void>();
			f.coordinator.observeProcessing({ type: "message_end", message }, processing.promise);
			const controller = new AbortController();
			if (mode === "before-wait") controller.abort();
			if (mode === "during-handler") f.core.resolve();
			const running = mode === "skip" ? Promise.resolve() : f.tasks[0]!.run(controller.signal);
			await Promise.resolve();
			if (mode === "skip") f.tasks[0]!.skip!();
			else if (mode === "dispose" || mode === "generation") {
				if (mode === "generation") f.advanceGeneration();
				f.coordinator.resolveResume();
			} else controller.abort();
			await running;
			expect(f.coordinator.resumeGate).toBeUndefined();
			expect(f.coordinator.abortPending).toBe(false);
			expect(f.notices).toEqual([]);
			expect(f.agent.continue).not.toHaveBeenCalled();
			// Late success cannot inject or revive cancelled ownership.
			f.record(message);
			processing.resolve();
			f.core.resolve();
			await Promise.resolve();
			expect(f.agent.continue).not.toHaveBeenCalled();
			f.coordinator.onTurnStart();
			const fresh = f.captureTextMatch(104, "fresh-after-cancel", "freshAfterCancel");
			expect(await f.coordinator.checkMessageUpdate(fresh)).toBe(true);
			expect(f.tasks).toHaveLength(2);
			f.coordinator.resolveResume();
		},
	);

	it("recorded TTSR lifecycle keeps a newer interruption alive after stale continuation completion", async () => {
		const f = recordedLifecycleFixture();
		const first = await f.trigger(201);
		f.record(first);
		f.coordinator.observeProcessing({ type: "message_end", message: first }, Promise.resolve());
		f.core.resolve();
		const old = f.tasks[0]!.run(new AbortController().signal);
		await f.continued.promise;
		const capturedC = f.captureTextMatch(202, "new-c-matching", "newCMatching");
		const second = await f.trigger(202);
		const newer = f.coordinator.resumeGate;
		f.continuation.resolve();
		await old;
		expect(await f.coordinator.checkMessageUpdate(capturedC)).toBe(true);
		expect(f.tasks).toHaveLength(2);
		expect(f.coordinator.resumeGate).toBe(newer);
		expect(f.coordinator.ownsInterruptedMessage(second)).toBe(true);
		f.coordinator.resolveResume();
		expect(f.coordinator.resumeGate).toBeUndefined();
	});

	it("recorded TTSR lifecycle successful settlement preserves admitted continuation matching", async () => {
		const f = recordedLifecycleFixture();
		const first = await f.trigger(211);
		f.record(first);
		f.coordinator.observeProcessing({ type: "message_end", message: first }, Promise.resolve());
		f.core.resolve();
		const running = f.tasks[0]!.run(new AbortController().signal);
		await f.continued.promise;
		const capturedContinuation = f.captureTextMatch(212, "continuation-rule", "continuationRule");
		f.continuation.resolve();
		await running;
		expect(f.coordinator.resumeGate).toBeUndefined();
		f.coordinator.onTurnStart();
		const checkDelta = vi.spyOn(f.manager, "checkDelta");
		expect(await f.coordinator.checkMessageUpdate(capturedContinuation)).toBe(true);
		expect(checkDelta.mock.results[0]?.value).toEqual([expect.objectContaining({ name: "continuation-rule" })]);
		expect(f.tasks).toHaveLength(2);
		expect(f.agent.continue).toHaveBeenCalledTimes(1);
		expect(f.notices).toEqual([]);
		f.coordinator.resolveResume();
	});

	it("recorded TTSR lifecycle rejects a fresh same-timestamp text match after admission", async () => {
		const f = recordedLifecycleFixture();
		const first = await f.trigger(203);
		f.record(first);
		f.coordinator.observeProcessing({ type: "message_end", message: first }, Promise.resolve());
		f.core.resolve();
		const running = f.tasks[0]!.run(new AbortController().signal);
		await f.continued.promise;
		expect(f.coordinator.abortPending).toBe(false);
		f.manager.addRule({ ...testRule, name: "different-late-rule", condition: ["dangerB"] });
		const late = { ...makeMsg("dangerB"), timestamp: first.timestamp };
		const event: AgentEvent = {
			type: "message_update",
			message: late,
			assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "dangerB", partial: late },
		};
		// Fresh entry uses the new matching signal: this reaches the defensive phase guard.
		f.coordinator.onEventEntry(event);
		const notifications = f.emitSessionEvent.mock.calls.length;
		const checkDelta = vi.spyOn(f.manager, "checkDelta");
		try {
			expect(await f.coordinator.checkMessageUpdate(event)).toBe(false);
			expect(checkDelta.mock.results[0]?.value).toEqual([expect.objectContaining({ name: "different-late-rule" })]);
			expect(f.tasks).toHaveLength(1);
			expect(f.agent.continue).toHaveBeenCalledTimes(1);
			expect(f.emitSessionEvent).toHaveBeenCalledTimes(notifications);
		} finally {
			f.continuation.resolve();
			await running;
			f.coordinator.resolveResume();
		}
	});

	it("recorded TTSR lifecycle ignores stale deferred callbacks after a newer interruption", async () => {
		const f = recordedLifecycleFixture();
		const deferredMessage = await f.trigger(301, "never");
		f.coordinator.onAssistantMessageEnd({ ...deferredMessage, stopReason: "stop" });
		expect(f.deferred).toHaveLength(1);
		const target = await f.trigger(302);
		const newer = f.coordinator.resumeGate;
		f.deferred[0]?.onSkip?.();
		f.deferred[0]?.onError?.();
		f.agent.clearAllQueues();
		f.deferred[0]?.shouldContinue?.();
		expect(f.coordinator.resumeGate).toBe(newer);
		expect(f.coordinator.ownsInterruptedMessage(target)).toBe(true);
		f.coordinator.resolveResume();
	});

	it("recorded TTSR lifecycle retains entry observation across delayed matching and waits for processing success", async () => {
		const f = recordedLifecycleFixture();
		f.manager.addRule(testRule);
		const target = { ...makeMsg("result.unwrap(", "aborted"), timestamp: 401 };
		const processing = Promise.withResolvers<void>();
		f.coordinator.observeProcessing({ type: "message_end", message: target }, processing.promise);
		await f.trigger(401);
		f.record(target);
		f.core.resolve();
		const running = f.tasks[0]!.run(new AbortController().signal);
		await originalSchedulerWait(0);
		expect(f.agent.continue).not.toHaveBeenCalled();
		processing.resolve();
		await f.continued.promise;
		expect(f.coordinator.abortPending).toBe(false);
		expect(f.coordinator.resumeGate).toBeDefined();
		f.continuation.resolve();
		await running;
		expect(f.agent.continue).toHaveBeenCalledTimes(1);
		expect(f.coordinator.resumeGate).toBeUndefined();
	});

	it.each(["cancel", "generation"] as const)(
		"recorded TTSR lifecycle rejects old entry and late AST matching after %s",
		async mode => {
			const f = recordedLifecycleFixture();
			f.manager.addRule(testRule);
			const tool: AgentTool = {
				name: "edit",
				label: "edit",
				description: "fixture",
				parameters: type({}),
				matcherDigest: () => "safe",
				execute: async () => ({ content: [] }),
			};
			f.agent.setTools([tool]);
			const call: ToolCall = { type: "toolCall", id: "old-edit", name: "edit", arguments: {} };
			const partial = { ...makeMsg(""), content: [call] };
			const event: AgentEvent = {
				type: "message_update",
				message: partial,
				assistantMessageEvent: { type: "toolcall_delta", contentIndex: 0, delta: "safe", partial },
			};
			const late: AgentEvent = { ...event };
			f.coordinator.onEventEntry(event);
			f.coordinator.onEventEntry(late);
			const matching = Promise.withResolvers<Rule[]>();
			vi.spyOn(f.manager, "hasAstRules").mockReturnValue(true);
			vi.spyOn(f.manager, "checkAstSnapshot").mockReturnValue(matching.promise);
			const checking = f.coordinator.checkMessageUpdate(event);
			if (mode === "cancel") f.coordinator.resolveResume();
			else f.advanceGeneration();
			matching.resolve([testRule]);
			expect(await checking).toBe(false);
			expect(await f.coordinator.checkMessageUpdate(late)).toBe(false);
			expect(f.tasks).toHaveLength(0);
			expect(f.coordinator.resumeGate).toBeUndefined();
			expect(f.coordinator.abortPending).toBe(false);
		},
	);

	it.each(["await", "return"] as const)(
		"recorded TTSR lifecycle lets a delayed message_end hook await ctx.abort without a cycle (%s)",
		async form => {
			const stages: string[] = [];
			const errors: string[] = [];
			let abortTask: Promise<void> | undefined;
			let abortReturned: unknown = "not called";
			const abortRequests: unknown[] = [];
			let hookHandled = false;
			const hookEntered = Promise.withResolvers<void>();
			const releaseHook = Promise.withResolvers<void>();
			const hookFinished = Promise.withResolvers<void>();
			const retryElapsed = Promise.withResolvers<void>();
			vi.spyOn(scheduler, "wait").mockImplementation(async (delay, options) => {
				await originalSchedulerWait(delay, options);
				if (delay === 50) retryElapsed.resolve();
			});
			const ttsrManager = new TtsrManager({
				enabled: true,
				contextMode: "discard",
				interruptMode: "always",
				repeatMode: "once",
				repeatGap: 10,
			});
			ttsrManager.addRule(testRule);
			let streams = 0;
			const agent = new Agent({
				initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")!, tools: [] },
				getApiKey: () => "test-key",
				streamFn: (_model, _context, options) => {
					streams++;
					const stream = new AssistantMessageEventStream();
					pushAbortableTtsrStream(stream, options?.signal);
					return stream;
				},
			});
			const sessionManager = SessionManager.inMemory();
			const runtime = new ExtensionRuntime();
			const extension = await loadExtensionFromFactory(
				pi => {
					pi.on("message_end", async (event, ctx) => {
						if (event.message.role !== "assistant" || event.message.stopReason !== "aborted" || hookHandled)
							return;
						hookHandled = true;
						hookEntered.resolve();
						await releaseHook.promise;
						stages.push("awaiting-ctx-abort");
						abortReturned = ctx.abort();
						expect(abortReturned).toBeUndefined();
						expect(abortRequests).toEqual([{ reason: USER_INTERRUPT_LABEL }]);
						if (form === "return") {
							hookFinished.resolve();
							return abortReturned as void;
						}
						await abortReturned;
						stages.push("ctx-abort-returned");
						hookFinished.resolve();
					});
				},
				tempDir,
				new EventBus(),
				runtime,
				"abort-delayed-ttsr-handler",
			);
			const extensionRunner = new ExtensionRunner(
				[extension],
				runtime,
				tempDir,
				sessionManager,
				sharedModelRegistry,
			);
			session = new AgentSession({
				agent,
				sessionManager,
				settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
				modelRegistry: sharedModelRegistry,
				ttsrManager,
				extensionRunner,
			});
			const abort = session.abort.bind(session);
			vi.spyOn(session, "abort").mockImplementation(options => {
				abortRequests.push(options);
				abortTask = abort(options);
				return abortTask;
			});
			await initializeExtensions(session, {
				reportSendError: (_action, error) => {
					errors.push(error.message);
				},
				reportRuntimeError: error => {
					errors.push(error.error);
				},
			});
			const prompted = session.prompt("Write Rust code");
			try {
				await hookEntered.promise;
				await retryElapsed.promise;
				await originalSchedulerWait(0);
				stages.push(
					JSON.stringify({ stage: "before-ctx-abort", abortPending: session.isTtsrAbortPending, streams }),
				);
			} finally {
				releaseHook.resolve();
			}
			await untilAborted(AbortSignal.timeout(5000), hookFinished.promise).catch(error => {
				throw new Error(
					JSON.stringify({ stages, abortPending: session.isTtsrAbortPending, streaming: agent.state.isStreaming }),
					{ cause: error },
				);
			});
			await prompted;
			await session.waitForIdle();
			await abortTask;
			expect(errors).toEqual([]);
			expect(streams).toBe(1);
			expect(session.isTtsrAbortPending).toBe(false);
			expect(
				sessionManager
					.getEntries()
					.some(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection"),
			).toBe(false);
			expect(
				sessionManager
					.getEntries()
					.some(
						entry =>
							entry.type === "message" &&
							entry.message.role === "assistant" &&
							entry.message.stopReason === "aborted",
					),
			).toBe(true);
		},
	);

	it.each([
		{
			thrown: new Error("shared abort rejected"),
			reporterThrows: false,
			reporterError: undefined,
			valueKind: "Error",
		},
		{
			thrown: "shared abort rejected",
			reporterThrows: true,
			reporterError: new Error("runtime reporter rejected"),
			valueKind: "string",
		},
		{
			thrown: Object.assign(Object.create(null) as object, { message: "shared abort rejected" }),
			reporterThrows: false,
			reporterError: undefined,
			valueKind: "null-prototype-rejection",
		},
		{
			thrown: "shared abort rejected",
			reporterThrows: true,
			reporterError: Object.assign(Object.create(null) as object, { message: "runtime reporter rejected" }),
			valueKind: "null-prototype-reporter",
		},
	])(
		"shared host reports abort rejection with throwing reporter=$reporterThrows ($valueKind)",
		async ({ thrown, reporterThrows, reporterError }) => {
			const originalStack = thrown instanceof Error ? thrown.stack : undefined;
			const reports: ExtensionError[] = [];
			const logs: Array<{ message: string; context?: Record<string, unknown> }> = [];
			const removeSink = logger.registerLogSink(event => logs.push(event));
			const runtime = new ExtensionRuntime();
			const manager = SessionManager.inMemory();
			let returned: unknown = "not called";
			const extension = await loadExtensionFromFactory(
				pi => {
					pi.on("message_end", (_event, ctx) => {
						returned = ctx.abort();
						return returned as void;
					});
				},
				tempDir,
				new EventBus(),
				runtime,
				"shared-abort-rejection",
			);
			const runner = new ExtensionRunner([extension], runtime, tempDir, manager, sharedModelRegistry);
			session = new AgentSession({
				agent: new Agent({ initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")! } }),
				sessionManager: manager,
				settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
				modelRegistry: sharedModelRegistry,
				extensionRunner: runner,
			});
			const abort = vi.spyOn(session, "abort").mockRejectedValue(thrown);
			try {
				await initializeExtensions(session, {
					reportSendError: (_action, error) => {
						throw error;
					},
					reportRuntimeError: error => {
						reports.push(error);
						if (reporterThrows) throw reporterError;
					},
				});
				await runner.emit({ type: "message_end", message: makeMsg("finished") });
				await originalSchedulerWait(0);
				expect(returned).toBeUndefined();
				expect(abort).toHaveBeenCalledWith({ reason: USER_INTERRUPT_LABEL });
				expect(reports).toHaveLength(1);
				expect(reports[0]).toMatchObject({
					extensionPath: "<runtime-init>",
					event: "abort",
					error: "shared abort rejected",
				});
				if (thrown instanceof Error) expect(reports[0].stack).toBe(originalStack);
				expect(logs).toEqual(
					reporterThrows
						? [
								{
									...logs[0],
									message: "Extension abort error reporting failed",
									context: {
										path: "<runtime-init>",
										error: "shared abort rejected",
										reportError: "runtime reporter rejected",
									},
								},
							]
						: [],
				);
			} finally {
				abort.mockRestore();
				removeSink();
			}
		},
	);

	it("TTSR deferred continuation can be interrupted and still settle after the final stream", async () => {
		collapseSchedulerSettleDelays();
		const manager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		manager.addRule(testRule);
		manager.addRule({ ...testRule, name: "defer-first", condition: ["defer\\("], interruptMode: "never" });
		let streams = 0;
		let completed = false;
		const agent = new Agent({
			initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")!, tools: [] },
			getApiKey: () => "test-key",
			streamFn: (_model, _context, options) => {
				const stream = new AssistantMessageEventStream();
				streams++;
				if (streams === 1)
					queueMicrotask(() => {
						const partial = makeMsg("defer(");
						stream.push({ type: "start", partial });
						stream.push({ type: "text_delta", contentIndex: 0, delta: "defer(", partial });
						stream.push({ type: "done", reason: "stop", message: partial });
					});
				else if (streams === 2) pushAbortableTtsrStream(stream, options?.signal);
				else
					pushContinuationStream(stream, () => {
						completed = true;
					});
				return stream;
			},
		});
		const sessionManager = SessionManager.inMemory();
		session = new AgentSession({
			agent,
			sessionManager,
			modelRegistry: sharedModelRegistry,
			settings: Settings.isolated(),
			ttsrManager: manager,
		});
		await session.prompt("Exercise deferred and interrupted rules");
		expect(streams).toBe(3);
		expect(completed).toBe(true);
		expect(session.isStreaming).toBe(false);
		expect(
			sessionManager
				.getEntries()
				.filter(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection"),
		).toHaveLength(2);
	});

	it("prompt() blocks until TTSR interrupt continuation completes", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;
		let continuationCompleted = false;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;

				if (streamCallCount === 1) {
					// First stream: emit text that triggers TTSR, then respond to abort
					pushAbortableTtsrStream(stream, signal);
				} else {
					// Continuation stream: complete normally after a delay
					pushContinuationStream(stream, () => {
						continuationCompleted = true;
					});
				}

				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
		});

		// prompt() must block until the TTSR continuation completes
		await session.prompt("Write some Rust code");

		// By the time prompt() returns, the continuation must have finished
		expect(continuationCompleted).toBe(true);
		expect(streamCallCount).toBeGreaterThanOrEqual(2);
		expect(session.isStreaming).toBe(false);
	});

	it("marks extension agent_end willContinue for TTSR abort and not ordinary abort", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5");
		if (!model) {
			throw new Error("Expected bundled Anthropic test model to exist");
		}

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const extensionEmits: Array<{ type: string; willContinue?: boolean }> = [];
		const continuationStarted = Promise.withResolvers<void>();

		let streamCallCount = 0;
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;
				if (streamCallCount === 1) {
					pushAbortableTtsrStream(stream, signal);
				} else {
					pushContinuationStream(stream, () => continuationStarted.resolve());
				}
				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated({
			"compaction.enabled": false,
			"retry.enabled": false,
			"todo.enabled": false,
			"todo.reminders": false,
		});
		const modelRegistry = sharedModelRegistry;
		const extensionRuntime = new ExtensionRuntime();
		const extension = await loadExtensionFromFactory(
			pi => {
				pi.on("agent_end", event => {
					extensionEmits.push({ type: event.type, willContinue: event.willContinue });
				});
			},
			tempDir,
			new EventBus(),
			extensionRuntime,
			"capture-agent-end",
		);
		const extensionRunner = new ExtensionRunner(
			[extension],
			extensionRuntime,
			tempDir,
			sessionManager,
			modelRegistry,
		);

		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
			extensionRunner,
		});

		const firstGoalEndStarted = Promise.withResolvers<void>();
		const releaseFirstGoalEnd = Promise.withResolvers<void>();
		let goalEndCalls = 0;
		vi.spyOn(GoalRuntime.prototype, "onAgentEnd").mockImplementation(async () => {
			goalEndCalls++;
			if (goalEndCalls !== 1) return;
			firstGoalEndStarted.resolve();
			await releaseFirstGoalEnd.promise;
		});

		const ttsrPrompt = session.prompt("Write some Rust code");
		await firstGoalEndStarted.promise;
		await continuationStarted.promise;
		const pendingClearedWhileMaintenanceBlocked = !session.isTtsrAbortPending;
		releaseFirstGoalEnd.resolve();
		await ttsrPrompt;
		await session.waitForIdle();
		expect(pendingClearedWhileMaintenanceBlocked).toBe(true);

		const ttsrEnds = extensionEmits.filter(event => event.type === "agent_end");
		expect(streamCallCount).toBeGreaterThanOrEqual(2);
		// Intermediate TTSR-abort settle continues; terminal settle after retry does not.
		expect(ttsrEnds.length).toBeGreaterThanOrEqual(2);
		expect(ttsrEnds[0]?.willContinue).toBe(true);
		expect(ttsrEnds.slice(1, -1).every(event => !event.willContinue)).toBe(true);
		expect(ttsrEnds.at(-1)?.willContinue).toBeFalsy();

		extensionEmits.length = 0;
		const ordinaryAgent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;
				queueMicrotask(() => {
					const partial = makeMsg("partial");
					stream.push({ type: "start", partial });
					stream.push({
						type: "text_delta",
						contentIndex: 0,
						delta: "partial",
						partial: makeMsg("partial"),
					});
					queueMicrotask(() => {
						session?.agent.abort("user cancelled");
					});
					if (signal) {
						signal.addEventListener(
							"abort",
							() => {
								stream.push({
									type: "error",
									reason: "aborted",
									error: makeMsg("partial", "aborted"),
								});
							},
							{ once: true },
						);
					}
				});
				return stream;
			},
		});
		await session.dispose();
		session = new AgentSession({
			agent: ordinaryAgent,
			sessionManager: SessionManager.inMemory(),
			settings,
			modelRegistry,
			extensionRunner,
		});

		const promptPromise = session.prompt("user will cancel");
		await session.waitForIdle();
		await promptPromise.catch(() => undefined);

		const ordinaryEnds = extensionEmits.filter(event => event.type === "agent_end");
		expect(ordinaryEnds.length).toBeGreaterThanOrEqual(1);
		for (const event of ordinaryEnds) {
			expect(event.willContinue).toBeFalsy();
		}
	});

	it("labels aborted tool placeholders with the TTSR rule reason", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const toolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_ttsr_abort_reason",
			name: "mock_edit",
			arguments: { snippet: "let val = result.unwrap(" },
		};

		const makeToolCallMsg = (stopReason: "toolUse" | "aborted" = "toolUse"): AssistantMessage => ({
			role: "assistant",
			content: [toolCallContent],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason,
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;
				if (streamCallCount === 1) {
					queueMicrotask(() => {
						const partial = makeToolCallMsg();
						if (signal) {
							signal.addEventListener(
								"abort",
								() => {
									stream.push({
										type: "error",
										reason: "aborted",
										error: { ...makeToolCallMsg("aborted"), timestamp: partial.timestamp },
									});
								},
								{ once: true },
							);
						}
						stream.push({ type: "start", partial });
						stream.push({ type: "toolcall_start", contentIndex: 0, partial });
						stream.push({
							type: "toolcall_delta",
							contentIndex: 0,
							delta: 'let val = result.unwrap("oops")',
							partial,
						});
						// The TTSR abort placeholder is only minted for tool calls that reached
						// `toolcall_end`: the agent loop drops incomplete tool calls from an
						// aborted turn (partial args are unsafe to replay). Complete the call
						// before the rule-driven abort fires so the labeled placeholder survives.
						stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: toolCallContent, partial });
					});
				} else {
					pushContinuationStream(stream, () => {});
				}
				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, ttsrManager });

		await session.prompt("Write some Rust code");

		const toolResult = sessionManager
			.getEntries()
			.find(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === toolCallContent.id,
			);
		expect(toolResult?.type).toBe("message");
		const text =
			toolResult?.type === "message" && toolResult.message.role === "toolResult"
				? (toolResult.message.content.find((part): part is { type: "text"; text: string } => part.type === "text")
						?.text ?? "")
				: "";
		expect(text).toContain("Tool execution was aborted: TTSR matched rule: no-unwrap");
		expect(text).not.toContain("Request was aborted");

		// The persisted aborted assistant turn must not render as an error on
		// resume/`/tree`/rebuild: TTSR interruption is control flow, so AgentSession
		// stamps the SilentAbort flag and `shouldRenderAbortReason` returns false.
		const abortedAssistant = sessionManager
			.getEntries()
			.find(
				entry =>
					entry.type === "message" && entry.message.role === "assistant" && entry.message.stopReason === "aborted",
			);
		expect(abortedAssistant?.type).toBe("message");
		if (abortedAssistant?.type === "message" && abortedAssistant.message.role === "assistant") {
			expect(shouldRenderAbortReason(abortedAssistant.message)).toBe(false);
		}
	});

	it("real 50ms retry waits for the original aborted record and completes its next stream", async () => {
		const retryElapsed = Promise.withResolvers<void>();
		const releaseYield = Promise.withResolvers<void>();
		vi.spyOn(scheduler, "wait").mockImplementation(async (delayMs, options) => {
			await originalSchedulerWait(delayMs, options);
			if (delayMs === 50) retryElapsed.resolve();
		});
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const toolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_ttsr_abort_reason",
			name: "mock_edit",
			arguments: { snippet: "let val = result.unwrap(" },
		};

		const makeToolCallMsg = (stopReason: "toolUse" | "aborted" = "toolUse"): AssistantMessage => ({
			role: "assistant",
			content: [toolCallContent],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason,
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;
				if (streamCallCount === 1) {
					queueMicrotask(() => {
						const partial = makeToolCallMsg();
						if (signal) {
							signal.addEventListener(
								"abort",
								() => {
									stream.push({
										type: "error",
										reason: "aborted",
										error: { ...makeToolCallMsg("aborted"), timestamp: partial.timestamp },
									});
								},
								{ once: true },
							);
						}
						stream.push({ type: "start", partial });
						stream.push({ type: "toolcall_start", contentIndex: 0, partial });
						stream.push({
							type: "toolcall_delta",
							contentIndex: 0,
							delta: 'let val = result.unwrap("oops")',
							partial,
						});
						// The TTSR abort placeholder is only minted for tool calls that reached
						// `toolcall_end`: the agent loop drops incomplete tool calls from an
						// aborted turn (partial args are unsafe to replay). Complete the call
						// before the rule-driven abort fires so the labeled placeholder survives.
						stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: toolCallContent, partial });
					});
				} else {
					pushContinuationStream(stream, () => {});
				}
				return stream;
			},
		});

		let deltaSeen = false;
		let yieldHeld = false;
		const originalSubscribe = agent.subscribe.bind(agent);
		vi.spyOn(agent, "subscribe").mockImplementation(listener =>
			originalSubscribe(event => {
				if (event.type === "message_update" && event.assistantMessageEvent.type === "toolcall_delta")
					deltaSeen = true;
				return listener(event);
			}),
		);
		const originalYield = YieldGate.prototype.yieldIfDue;
		vi.spyOn(YieldGate.prototype, "yieldIfDue").mockImplementation(function (this: YieldGate) {
			const yielded = originalYield.call(this);
			if (!deltaSeen || yieldHeld) return yielded;
			yieldHeld = true;
			return Promise.all([yielded, releaseYield.promise]).then(() => {});
		});
		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, ttsrManager });

		const prompted = session.prompt("Write some Rust code");
		try {
			await retryElapsed.promise;
			// Let the timer continuation run while the original stream remains held.
			await originalSchedulerWait(0);
			expect(yieldHeld).toBe(true);
			expect(streamCallCount).toBe(1);
			expect(
				sessionManager
					.getEntries()
					.some(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection"),
			).toBe(false);
		} finally {
			releaseYield.resolve();
		}
		await prompted;
		expect(streamCallCount).toBe(2);
		expect(session.isStreaming).toBe(false);
		const ordered = sessionManager
			.getEntries()
			.flatMap(entry =>
				entry.type === "message" && entry.message.role === "assistant"
					? [entry.message.stopReason]
					: entry.type === "custom_message" && entry.customType === "ttsr-injection"
						? ["injection"]
						: [],
			);
		expect(ordered).toEqual(["aborted", "injection", "stop"]);

		const toolResult = sessionManager
			.getEntries()
			.find(
				entry =>
					entry.type === "message" &&
					entry.message.role === "toolResult" &&
					entry.message.toolCallId === toolCallContent.id,
			);
		expect(toolResult?.type).toBe("message");
		const text =
			toolResult?.type === "message" && toolResult.message.role === "toolResult"
				? (toolResult.message.content.find((part): part is { type: "text"; text: string } => part.type === "text")
						?.text ?? "")
				: "";
		expect(text).toContain("Tool execution was aborted: TTSR matched rule: no-unwrap");
		expect(text).not.toContain("Request was aborted");

		// The persisted aborted assistant turn must not render as an error on
		// resume/`/tree`/rebuild: TTSR interruption is control flow, so AgentSession
		// stamps the SilentAbort flag and `shouldRenderAbortReason` returns false.
		const abortedAssistant = sessionManager
			.getEntries()
			.find(
				entry =>
					entry.type === "message" && entry.message.role === "assistant" && entry.message.stopReason === "aborted",
			);
		expect(abortedAssistant?.type).toBe("message");
		if (abortedAssistant?.type === "message" && abortedAssistant.message.role === "assistant") {
			expect(shouldRenderAbortReason(abortedAssistant.message)).toBe(false);
		}
	});

	it("labels only the matching aborted tool placeholder with the TTSR rule reason", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const readToolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_innocent_read",
			name: "read",
			arguments: { path: "history://Eval1WithSkill" },
		};
		const matchedToolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_ttsr_abort_reason",
			name: "mock_edit",
			arguments: { snippet: "let val = result.unwrap(" },
		};

		const makeToolCallMsg = (stopReason: "toolUse" | "aborted" = "toolUse"): AssistantMessage => ({
			role: "assistant",
			content: [readToolCallContent, matchedToolCallContent],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason,
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;
				if (streamCallCount === 1) {
					queueMicrotask(() => {
						const partial = makeToolCallMsg();
						if (signal) {
							signal.addEventListener(
								"abort",
								() => {
									stream.push({
										type: "error",
										reason: "aborted",
										error: { ...makeToolCallMsg("aborted"), timestamp: partial.timestamp },
									});
								},
								{ once: true },
							);
						}
						stream.push({ type: "start", partial });
						stream.push({ type: "toolcall_start", contentIndex: 1, partial });
						stream.push({
							type: "toolcall_delta",
							contentIndex: 1,
							delta: 'let val = result.unwrap("oops")',
							partial,
						});
						// The abort placeholder is only minted for tool calls that reached
						// `toolcall_end`: the agent loop drops incomplete tool calls from an
						// aborted turn (partial args are unsafe to replay). Complete the
						// innocent read before the rule-driven abort fires so its placeholder
						// survives and can carry the neutral sibling label.
						stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: readToolCallContent, partial });
					});
				} else {
					pushContinuationStream(stream, () => {});
				}
				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, ttsrManager });

		await session.prompt("Write some Rust code");

		const toolResults = sessionManager
			.getEntries()
			.filter(entry => entry.type === "message" && entry.message.role === "toolResult")
			.map(entry => (entry.type === "message" && entry.message.role === "toolResult" ? entry.message : undefined))
			.filter(message => message !== undefined);
		const toolResultText = (toolCallId: string): string =>
			toolResults
				.find(message => message.toolCallId === toolCallId)
				?.content.find((part): part is { type: "text"; text: string } => part.type === "text")?.text ?? "";

		const readText = toolResultText(readToolCallContent.id);
		expect(readText).toContain("Tool execution was aborted: TTSR interrupt on another tool call");
		expect(readText).not.toContain("TTSR matched rule: no-unwrap");
		// The matching call never reached `toolcall_end`, so the loop drops it from
		// the aborted turn (partial args are unsafe to replay) and no placeholder is
		// minted. The rule label for a completed matching call is covered by the
		// single-call test above.
		expect(toolResultText(matchedToolCallContent.id)).toBe("");
	});

	it("relativizes the rule file path in the TTSR interrupt injection (no absolute leak)", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;

		const sessionManager = SessionManager.inMemory();
		const cwd = sessionManager.getCwd();
		const ruleAbsPath = path.join(cwd, ".omp", "rules", "no-unwrap.md");
		const expectedRel = path.relative(cwd, ruleAbsPath);
		const rule: Rule = {
			name: "no-unwrap",
			path: ruleAbsPath,
			content: "Do not use .unwrap()",
			condition: ["\\.unwrap\\("],
			_source: { provider: "test", providerName: "test", path: ruleAbsPath, level: "project" },
		};

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(rule);

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				if (streamCallCount === 1) {
					pushAbortableTtsrStream(stream, options?.signal);
				} else {
					pushContinuationStream(stream, () => {});
				}
				return stream;
			},
		});

		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({ agent, sessionManager, settings, modelRegistry, ttsrManager });

		await session.prompt("Write some Rust code");

		const injection = sessionManager
			.getEntries()
			.find(e => e.type === "custom_message" && e.customType === "ttsr-injection");
		expect(injection?.type).toBe("custom_message");
		const content = injection?.type === "custom_message" ? injection.content : undefined;
		expect(typeof content).toBe("string");
		const text = content as string;
		// The rendered interrupt the model receives references the rule by a
		// project-relative path, never the absolute home path.
		expect(text).toContain('reason="rule_violation"');
		expect(text).toContain(`path="${expectedRel}"`);
		expect(text).not.toContain(ruleAbsPath);
	});

	it("prompt() blocks until TTSR deferred continuation completes", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;
		let continuationCompleted = false;

		// interruptMode: "never" -> TTSR match queues deferred injection instead of aborting
		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "never",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [] },
			streamFn: (_model, _context, _options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();

				if (streamCallCount === 1) {
					// First stream: emit matching text and complete normally
					queueMicrotask(() => {
						const partial = makeMsg("");
						stream.push({ type: "start", partial });
						stream.push({
							type: "text_delta",
							contentIndex: 0,
							delta: "let val = result.unwrap(",
							partial: { ...makeMsg("let val = result.unwrap("), timestamp: partial.timestamp },
						});
						// Complete normally (no abort) -- deferred path
						stream.push({
							type: "done",
							reason: "stop",
							message: makeMsg("let val = result.unwrap()"),
						});
					});
				} else {
					// Continuation stream after deferred TTSR injection
					pushContinuationStream(stream, () => {
						continuationCompleted = true;
					});
				}

				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
		});

		// prompt() must block until the deferred TTSR continuation completes
		await session.prompt("Write some Rust code");

		// By the time prompt() returns, the deferred continuation must have finished
		expect(continuationCompleted).toBe(true);
		expect(streamCallCount).toBeGreaterThanOrEqual(2);
		expect(session.isStreaming).toBe(false);
	});

	it("prompt() waits for TTSR continuation with tool calls to finish", async () => {
		collapseSchedulerSettleDelays();
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;
		let toolExecutionFinished = false;
		let allTurnsCompleted = false;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const mockTool: AgentTool = {
			name: "mock_edit",
			label: "Mock Edit",
			description: "A mock edit tool",
			parameters: type({}),
			execute: async () => {
				toolExecutionFinished = true;
				return { content: [{ type: "text" as const, text: "edit applied" }] };
			},
		};

		const toolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_test_001",
			name: "mock_edit",
			arguments: {},
		};

		function makeToolCallMsg(): AssistantMessage {
			return {
				role: "assistant",
				content: [toolCallContent],
				api: "anthropic-messages",
				provider: "anthropic",
				model: "mock",
				usage: {
					input: 0,
					output: 0,
					cacheRead: 0,
					cacheWrite: 0,
					totalTokens: 0,
					cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
				},
				stopReason: "toolUse",
				timestamp: Date.now(),
			};
		}

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [mockTool] },
			streamFn: (_model, _context, options) => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				const signal = options?.signal;

				if (streamCallCount === 1) {
					// First stream: emit text that triggers TTSR, then respond to abort
					pushAbortableTtsrStream(stream, signal);
				} else if (streamCallCount === 2) {
					// Continuation: return assistant message with a tool call
					queueMicrotask(() => {
						const msg = makeToolCallMsg();
						stream.push({ type: "start", partial: msg });
						stream.push({ type: "done", reason: "toolUse", message: msg });
					});
				} else {
					// After tool execution: return final response
					queueMicrotask(() => {
						allTurnsCompleted = true;
						const msg = makeMsg('Fixed: let val = result.expect("msg")');
						stream.push({ type: "start", partial: msg });
						stream.push({ type: "done", reason: "stop", message: msg });
					});
				}

				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
		});

		// prompt() must block until the TTSR continuation (including tool execution) completes.
		// Before the fix, prompt() returned after the continuation's first assistant message_end,
		// while the agent was still executing tool calls in the background.
		await session.prompt("Write some Rust code");

		// By the time prompt() returns, ALL turns must have completed
		expect(toolExecutionFinished).toBe(true);
		expect(allTurnsCompleted).toBe(true);
		expect(streamCallCount).toBeGreaterThanOrEqual(3);
		expect(session.isStreaming).toBe(false);
	});
	it("interruptMode never folds tool-match reminder into the toolResult instead of driving an extra turn", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;
		let toolExecuted = false;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "never",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const mockTool: AgentTool = {
			name: "mock_edit",
			label: "Mock Edit",
			description: "A mock edit tool",
			parameters: type({ snippet: "string?" }),
			execute: async () => {
				toolExecuted = true;
				return { content: [{ type: "text" as const, text: "edit applied" }] };
			},
		};

		const toolCallContent: ToolCall = {
			type: "toolCall",
			id: "call_never_001",
			name: "mock_edit",
			arguments: { snippet: "let val = result.unwrap()" },
		};

		const makeToolCallMsg = (): AssistantMessage => ({
			role: "assistant",
			content: [toolCallContent],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "toolUse",
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [mockTool] },
			streamFn: () => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				if (streamCallCount === 1) {
					// Emit a tool call whose argument delta matches the TTSR rule.
					queueMicrotask(() => {
						const partial = makeToolCallMsg();
						stream.push({ type: "start", partial });
						stream.push({ type: "toolcall_start", contentIndex: 0, partial });
						stream.push({
							type: "toolcall_delta",
							contentIndex: 0,
							delta: 'let val = result.unwrap("oops")',
							partial,
						});
						stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: toolCallContent, partial });
						stream.push({ type: "done", reason: "toolUse", message: partial });
					});
				} else {
					// Continuation after tool result; finish cleanly.
					queueMicrotask(() => {
						const done = makeMsg("ok");
						stream.push({ type: "start", partial: done });
						stream.push({ type: "done", reason: "stop", message: done });
					});
				}
				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
		});

		await session.prompt("Write some Rust code");

		// Tool ran (no interrupt) and the loop didn't spawn an extra follow-up turn for injection.
		expect(toolExecuted).toBe(true);
		expect(streamCallCount).toBe(2);

		// The matched tool's result must carry the in-band reminder.
		const toolResult = agent.state.messages.find(
			(m): m is Extract<typeof m, { role: "toolResult" }> =>
				m.role === "toolResult" && m.toolCallId === toolCallContent.id,
		);
		expect(toolResult).toBeDefined();
		const text = Array.isArray(toolResult?.content)
			? toolResult.content
					.filter((c): c is { type: "text"; text: string } => c.type === "text")
					.map(c => c.text)
					.join("\n")
			: "";
		expect(text).toContain("<system-reminder");
		expect(text).toContain('rule="no-unwrap"');
		expect(text).toContain("Do not use .unwrap()");
		expect(text.indexOf("<system-reminder")).toBeLessThan(text.indexOf("edit applied"));
	});

	it("interruptMode never deduplicates the reminder across sibling tool calls in one batch", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let streamCallCount = 0;
		let executedCount = 0;

		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "never",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule(testRule);

		const mockTool: AgentTool = {
			name: "mock_edit",
			label: "Mock Edit",
			description: "A mock edit tool",
			parameters: type({ snippet: "string?" }),
			execute: async () => {
				executedCount++;
				return { content: [{ type: "text" as const, text: "edit applied" }] };
			},
		};

		const toolCallA: ToolCall = {
			type: "toolCall",
			id: "call_dup_A",
			name: "mock_edit",
			arguments: { snippet: "a.unwrap()" },
		};
		const toolCallB: ToolCall = {
			type: "toolCall",
			id: "call_dup_B",
			name: "mock_edit",
			arguments: { snippet: "b.unwrap()" },
		};
		const toolCallC: ToolCall = {
			type: "toolCall",
			id: "call_dup_C",
			name: "mock_edit",
			arguments: { snippet: "c.unwrap()" },
		};

		const makeBatchMsg = (): AssistantMessage => ({
			role: "assistant",
			content: [toolCallA, toolCallB, toolCallC],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "toolUse",
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [mockTool] },
			streamFn: () => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				if (streamCallCount === 1) {
					queueMicrotask(() => {
						const partial = makeBatchMsg();
						stream.push({ type: "start", partial });
						const calls: ToolCall[] = [toolCallA, toolCallB, toolCallC];
						for (let i = 0; i < calls.length; i++) {
							const call = calls[i]!;
							stream.push({ type: "toolcall_start", contentIndex: i, partial });
							stream.push({
								type: "toolcall_delta",
								contentIndex: i,
								delta: `let val = result.unwrap("oops-${call.id}")`,
								partial,
							});
							stream.push({ type: "toolcall_end", contentIndex: i, toolCall: call, partial });
						}
						stream.push({ type: "done", reason: "toolUse", message: partial });
					});
				} else {
					queueMicrotask(() => {
						const done = makeMsg("ok");
						stream.push({ type: "start", partial: done });
						stream.push({ type: "done", reason: "stop", message: done });
					});
				}
				return stream;
			},
		});

		const sessionManager = SessionManager.inMemory();
		const settings = Settings.isolated();
		const modelRegistry = sharedModelRegistry;
		session = new AgentSession({
			agent,
			sessionManager,
			settings,
			modelRegistry,
			ttsrManager,
		});

		await session.prompt("Write some Rust code");

		expect(executedCount).toBe(3);
		const toolResults = agent.state.messages.filter(
			(m): m is Extract<typeof m, { role: "toolResult" }> => m.role === "toolResult",
		);
		expect(toolResults).toHaveLength(3);
		const withReminder = toolResults.filter(r =>
			Array.isArray(r.content)
				? r.content.some(c => c.type === "text" && c.text.includes("<system-reminder"))
				: false,
		);
		expect(withReminder).toHaveLength(1);
	});

	it("prompt() waits for context-promotion continuation to finish", async () => {
		collapseSchedulerSettleDelays();
		const authStorage = sharedAuthStorage;
		// The bundled catalog has no codex model whose promotion target carries a
		// strictly larger window (gpt-5.5's bundled target gpt-5.4 is same-window),
		// so pin gpt-5.5 (272k) -> gpt-5.6-sol (372k) via modelOverrides.
		const modelsConfigPath = path.join(tempDir, "models-promo.json");
		await Bun.write(
			modelsConfigPath,
			JSON.stringify({
				providers: {
					"openai-codex": {
						modelOverrides: {
							"gpt-5.5": { contextPromotionTarget: "openai-codex/gpt-5.6-sol" },
						},
					},
				},
			}),
		);
		const modelRegistry = new ModelRegistry(authStorage, modelsConfigPath);

		const smallModel = modelRegistry.find("openai-codex", "gpt-5.5");
		const largeModel = modelRegistry.find("openai-codex", "gpt-5.6-sol");
		if (!smallModel || !largeModel) {
			throw new Error("Expected small and large codex models to exist");
		}

		let streamCallCount = 0;
		let continuationCompleted = false;

		const makeOverflowMessage = (): AssistantMessage => ({
			role: "assistant",
			content: [{ type: "text", text: "" }],
			api: smallModel.api,
			provider: smallModel.provider,
			model: smallModel.id,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "error",
			errorMessage: "context_length_exceeded: Your input exceeds the context window of this model.",
			timestamp: Date.now(),
		});

		const makeSuccessMessage = (): AssistantMessage => ({
			role: "assistant",
			content: [{ type: "text", text: "Recovered after promotion" }],
			api: largeModel.api,
			provider: largeModel.provider,
			model: largeModel.id,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "stop",
			timestamp: Date.now(),
		});

		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model: smallModel, systemPrompt: ["Test"], tools: [] },
			streamFn: () => {
				streamCallCount++;
				const stream = new AssistantMessageEventStream();
				if (streamCallCount === 1) {
					queueMicrotask(() => {
						const message = makeOverflowMessage();
						stream.push({ type: "start", partial: message });
						stream.push({ type: "error", reason: "error", error: message });
					});
				} else {
					queueMicrotask(() => {
						continuationCompleted = true;
						const message = makeSuccessMessage();
						stream.push({ type: "start", partial: message });
						stream.push({ type: "done", reason: "stop", message });
					});
				}
				return stream;
			},
		});

		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			emit: vi.fn().mockResolvedValue(undefined),
			emitBeforeAgentStart: vi.fn().mockResolvedValue(undefined),
			hasHandlers: vi.fn((eventType: string) => eventType === "session_stop"),
			emitSessionStop: vi.fn().mockResolvedValue(undefined),
		} as unknown as ExtensionRunner;

		session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings: Settings.isolated({ "compaction.enabled": false, "contextPromotion.enabled": true }),
			modelRegistry,
			extensionRunner,
		});

		await session.prompt("Handle overflow");

		expect(continuationCompleted).toBe(true);
		expect(streamCallCount).toBeGreaterThanOrEqual(2);
		expect(session.model?.id).toBe(largeModel.id);
		expect(session.isStreaming).toBe(false);
		expect(extensionRunner.emitSessionStop).toHaveBeenCalledTimes(1);
	});
});

// Concurrent native results must retain their original response's reminder and retry ownership.
describe("same-message native AST", () => {
	it.each(["union", "target-held", "late-during", "late-after", "duplicate"] as const)(
		"preserves one completed retry and the admitted rule set (%s)",
		async mode => {
			const trace: object[] = [];
			const started = performance.now();
			const record = (stage: string, details: object = {}) =>
				trace.push({ stage, ms: performance.now() - started, ...details });
			const ready = [Promise.withResolvers<void>(), Promise.withResolvers<void>()];
			const release = [Promise.withResolvers<void>(), Promise.withResolvers<void>()];
			const returned = [Promise.withResolvers<void>(), Promise.withResolvers<void>()];
			const triggeredA = Promise.withResolvers<void>();
			const providerTerminal = Promise.withResolvers<void>();
			const hookEntered = Promise.withResolvers<void>();
			const hookRelease = Promise.withResolvers<void>();
			const firstTimer = Promise.withResolvers<void>();
			const continuationStarted = Promise.withResolvers<void>();
			const continuationRelease = Promise.withResolvers<void>();
			const late = mode === "late-during" || mode === "late-after";
			const names =
				mode === "duplicate" ? ["same-message-a", "same-message-a"] : ["same-message-a", "same-message-b"];
			const expected = late || mode === "duplicate" ? [names[0]!] : names;
			let retryTimers = 0;
			let streams = 0;
			let originalAborts = 0;
			let continuationAborts = 0;
			let continuationFinished = false;
			let retryInput = "";
			const timestamps: number[] = [];
			const notifications: string[][] = [];
			const notices: string[] = [];
			const schedulerSpy = vi.spyOn(scheduler, "wait").mockImplementation(async (delay, options) => {
				if (delay === 50) retryTimers++;
				record("scheduler-enter", { delay });
				await originalSchedulerWait(delay, options);
				record("scheduler-complete", { delay });
				if (delay === 50) firstTimer.resolve();
			});
			const manager = new TtsrManager({
				enabled: true,
				contextMode: "discard",
				interruptMode: "always",
				repeatMode: "once",
				repeatGap: 10,
			});
			for (const [index, name] of names.entries()) {
				if (mode === "duplicate" && index === 1) continue;
				manager.addRule({
					name,
					path: `/rehearsal/${name}.md`,
					content: `Use safe${index} instead of danger${index}.`,
					astCondition: [mode === "duplicate" ? "danger($$$ARGS)" : `danger${index}($$$ARGS)`],
					scope: [mode === "duplicate" ? "tool:write(file*.ts)" : `tool:write(file${index}.ts)`],
					_source: { provider: "test", providerName: "test", path: `/rehearsal/${name}.md`, level: "project" },
				});
			}
			const actualCheck = manager.checkAstSnapshot.bind(manager);
			const checkSpy = vi.spyOn(manager, "checkAstSnapshot").mockImplementation(async (snapshot, context) => {
				const index = context.streamKey?.includes("same-message-0") ? 0 : 1;
				record("ast-enter", { index, snapshot, context });
				const matches = await actualCheck(snapshot, context);
				record("native-result", { index, names: matches.map(rule => rule.name) });
				expect(matches.map(rule => rule.name)).toEqual([names[index]]);
				ready[index]!.resolve();
				await release[index]!.promise;
				record("ast-return", { index });
				returned[index]!.resolve();
				return matches;
			});
			const message = (
				content: AssistantMessage["content"],
				stopReason: AssistantMessage["stopReason"],
				timestamp: number,
			): AssistantMessage => ({
				role: "assistant",
				content,
				api: "anthropic-messages",
				provider: "anthropic",
				model: "mock",
				stopReason,
				timestamp,
				usage: {
					input: 0,
					output: 0,
					cacheRead: 0,
					cacheWrite: 0,
					totalTokens: 0,
					cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
				},
			});
			const calls: ToolCall[] = names.map((_name, index) => ({
				type: "toolCall",
				id: `same-message-${index}`,
				name: "write",
				arguments: {
					path: `file${index}.ts`,
					content: mode === "duplicate" ? `danger(${index});` : `danger${index}();`,
				},
			}));
			const tool: AgentTool = {
				name: "write",
				label: "Write",
				description: "Rehearsal write",
				parameters: type({ path: "string", content: "string" }),
				matcherEntries: args => {
					const value = args as { path: string; content: string };
					return [{ path: value.path, digest: value.content }];
				},
				execute: async () => {
					throw new Error("Interrupted tools must not execute");
				},
			};
			const agent = new Agent({
				getApiKey: () => "test-key",
				convertToLlm,
				initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")!, tools: [tool] },
				streamFn: (_model, context, options) => {
					streams++;
					record("stream-start", { streams });
					const stream = new AssistantMessageEventStream();
					if (streams === 1)
						queueMicrotask(() => {
							const partial = message(calls, "toolUse", 1720000000000);
							options?.signal?.addEventListener(
								"abort",
								() => {
									originalAborts++;
									record("original-provider-abort");
									// Agent may emit its own aborted message_end before this provider terminal event.
									void providerTerminal.promise.then(() => {
										record("provider-terminal-release");
										stream.push({
											type: "error",
											reason: "aborted",
											error: { ...partial, stopReason: "aborted" },
										});
									});
								},
								{ once: true },
							);
							stream.push({ type: "start", partial });
							for (const [contentIndex, call] of calls.entries()) {
								stream.push({ type: "toolcall_start", contentIndex, partial });
								stream.push({
									type: "toolcall_delta",
									contentIndex,
									delta: JSON.stringify(call.arguments),
									partial,
								});
								stream.push({ type: "toolcall_end", contentIndex, toolCall: call, partial });
							}
						});
					else {
						retryInput = JSON.stringify(context.messages);
						options?.signal?.addEventListener(
							"abort",
							() => {
								continuationAborts++;
							},
							{ once: true },
						);
						continuationStarted.resolve();
						void continuationRelease.promise.then(() => {
							const done = message(
								[{ type: "text", text: "Both corrected writes completed." }],
								"stop",
								1720000000001,
							);
							stream.push({ type: "start", partial: done });
							stream.push({ type: "done", reason: "stop", message: done });
							continuationFinished = true;
						});
					}
					return stream;
				},
			});
			const abortSpy = vi.spyOn(agent, "abort");
			agent.subscribe(event => {
				if (event.type === "message_update" && event.assistantMessageEvent.type === "toolcall_delta") {
					timestamps.push(event.message.timestamp);
					record("delta-enter", { timestamp: event.message.timestamp, originalAborts });
				}
				if (event.type === "message_end") record("core-message-end", { message: event.message });
				if (event.type === "agent_end") record("core-agent-end");
			});
			const sessionManager = SessionManager.inMemory();
			const runtime = new ExtensionRuntime();
			const extension = await loadExtensionFromFactory(
				pi => {
					pi.on("message_end", async event => {
						if (
							mode !== "target-held" ||
							event.message.role !== "assistant" ||
							event.message.stopReason !== "aborted"
						)
							return;
						record("target-hook-enter");
						hookEntered.resolve();
						await hookRelease.promise;
						record("target-hook-exit");
					});
				},
				os.tmpdir(),
				new EventBus(),
				runtime,
				"same-message-target",
			);
			const extensionRunner = new ExtensionRunner(
				[extension],
				runtime,
				os.tmpdir(),
				sessionManager,
				sharedModelRegistry,
			);
			const session = new AgentSession({
				agent,
				sessionManager,
				extensionRunner,
				settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
				modelRegistry: sharedModelRegistry,
				ttsrManager: manager,
			});
			session.subscribe(event => {
				if (event.type === "notice") notices.push(event.message);
				if (event.type === "ttsr_triggered") {
					const matched = event.rules.map(rule => rule.name);
					notifications.push(matched);
					record("triggered", { names: matched });
					if (matched.includes(names[0]!)) triggeredA.resolve();
				}
			});
			const injections = () =>
				sessionManager
					.getEntries()
					.filter(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection");
			const prompted = session.prompt("Write both files safely.");
			try {
				await untilAborted(AbortSignal.timeout(3000), Promise.all(ready.map(gate => gate.promise)));
				expect(originalAborts).toBe(0);
				expect(timestamps).toEqual([1720000000000, 1720000000000]);
				release[0]!.resolve();
				await untilAborted(AbortSignal.timeout(3000), triggeredA.promise);
				if (mode === "target-held") await untilAborted(AbortSignal.timeout(3000), hookEntered.promise);
				if (!late) {
					release[1]!.resolve();
					await returned[1]!.promise;
				}
				providerTerminal.resolve();
				if (mode === "target-held") {
					await untilAborted(AbortSignal.timeout(3000), firstTimer.promise);
					await originalSchedulerWait(0);
					record("target-held-after-first-real-timer", { streams, abortPending: session.isTtsrAbortPending });
					expect(streams).toBe(1);
					expect(session.isTtsrAbortPending).toBe(true);
					expect(injections()).toEqual([]);
					expect(
						sessionManager
							.getEntries()
							.some(
								entry =>
									entry.type === "message" &&
									entry.message.role === "assistant" &&
									entry.message.stopReason === "aborted",
							),
					).toBe(false);
					expect(notices).toEqual([]);
					hookRelease.resolve();
				}
				await untilAborted(AbortSignal.timeout(3000), continuationStarted.promise);
				if (mode === "late-during") {
					record("release-b-during-continuation");
					release[1]!.resolve();
					await returned[1]!.promise;
					await originalSchedulerWait(0);
					expect(continuationAborts).toBe(0);
					expect(agent.state.isStreaming).toBe(true);
				}
				continuationRelease.resolve();
				await untilAborted(AbortSignal.timeout(3000), prompted);
				await session.waitForIdle();
				if (mode === "late-after") {
					record("release-b-after-session-idle");
					release[1]!.resolve();
					await returned[1]!.promise;
					await originalSchedulerWait(0);
					await session.waitForIdle();
				}
				record("consumer-result", {
					streams,
					originalAborts,
					continuationAborts,
					retryTimers,
					continuationFinished,
					notifications,
					notices,
					persistedRules: sessionManager.getInjectedTtsrRules(),
					injections: injections(),
					retryInput,
				});
				expect(abortSpy).toHaveBeenCalledTimes(1);
				expect(originalAborts).toBe(1);
				expect(continuationAborts).toBe(0);
				expect(streams).toBe(2);
				expect(retryTimers).toBe(1);
				expect(continuationFinished).toBe(true);
				expect(injections()).toHaveLength(1);
				expect(sessionManager.getInjectedTtsrRules().sort()).toEqual(expected);
				expect(notifications).toEqual(late ? [[names[0]!]] : [[names[0]!], [names[1]!]]);
				expect(notices).toEqual([]);
				for (const name of expected)
					expect(retryInput.match(new RegExp(`rule=\\\\?"${name}`, "g"))).toHaveLength(1);
				if (late) expect(retryInput).not.toContain(names[1]!);
				if (mode === "target-held")
					expect(
						sessionManager
							.getEntries()
							.some(
								entry =>
									entry.type === "message" &&
									entry.message.role === "assistant" &&
									entry.message.stopReason === "aborted",
							),
					).toBe(true);
				expect(
					sessionManager
						.getEntries()
						.some(
							entry =>
								entry.type === "message" &&
								entry.message.role === "assistant" &&
								entry.message.stopReason === "stop",
						),
				).toBe(true);
			} finally {
				release.forEach(gate => {
					gate.resolve();
				});
				providerTerminal.resolve();
				hookRelease.resolve();
				continuationRelease.resolve();
				process.stdout.write(`SAME_MESSAGE_TRACE ${JSON.stringify({ mode, trace })}\n`);
				await session.dispose();
				checkSpy.mockRestore();
				abortSpy.mockRestore();
				schedulerSpy.mockRestore();
			}
		},
		10000,
	);
});

// Fault injection: omit one supported processing observation, while real persistence continues.
describe("failed attempt retirement", () => {
	it("failed attempt cannot interrupt a fresh user prompt when its old native AST result returns", async () => {
		const trace: object[] = [];
		const started = performance.now();
		const record = (stage: string, detail: object = {}) =>
			trace.push({ stage, ms: performance.now() - started, ...detail });
		const ready = [Promise.withResolvers<void>(), Promise.withResolvers<void>()];
		const release = [Promise.withResolvers<void>(), Promise.withResolvers<void>()];
		const returnedB = Promise.withResolvers<void>();
		const triggeredA = Promise.withResolvers<void>();
		const freshStarted = Promise.withResolvers<void>();
		const freshRelease = Promise.withResolvers<void>();
		const manager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		const names = ["failed-attempt-a", "failed-attempt-b"];
		for (const [index, name] of names.entries())
			manager.addRule({
				name,
				path: `/rehearsal/${name}.md`,
				content: `Replace danger${index} with safe${index}.`,
				astCondition: [`danger${index}($$$ARGS)`],
				scope: [`tool:write(file${index}.ts)`],
				_source: { provider: "test", providerName: "test", path: `/rehearsal/${name}.md`, level: "project" },
			});
		const actualCheck = manager.checkAstSnapshot.bind(manager);
		const checkSpy = vi.spyOn(manager, "checkAstSnapshot").mockImplementation(async (snapshot, context) => {
			const index = context.streamKey?.includes("failed-call-0") ? 0 : 1;
			record("ast-enter", { index, snapshot, context });
			const matches = await actualCheck(snapshot, context);
			record("native-result", { index, names: matches.map(rule => rule.name) });
			expect(matches.map(rule => rule.name)).toEqual([names[index]]);
			ready[index]!.resolve();
			await release[index]!.promise;
			record("ast-return", { index });
			if (index === 1) returnedB.resolve();
			return matches;
		});
		let omitted = false;
		let observationRestored = false;
		const actualObserve = TtsrCoordinator.prototype.observeProcessing;
		const observationSpy = vi.spyOn(TtsrCoordinator.prototype, "observeProcessing").mockImplementation(function (
			this: TtsrCoordinator,
			event,
			processing,
		) {
			if (
				!omitted &&
				event.type === "message_end" &&
				event.message.role === "assistant" &&
				event.message.stopReason === "aborted" &&
				event.message.timestamp === 1720000000100
			) {
				omitted = true;
				record("fault-omit-first-aborted-observation", { timestamp: event.message.timestamp });
				return;
			}
			return actualObserve.call(this, event, processing);
		});
		let timers = 0;
		const schedulerSpy = vi.spyOn(scheduler, "wait").mockImplementation(async (delay, options) => {
			if (delay === 50) timers++;
			record("scheduler-enter", { delay });
			await originalSchedulerWait(delay, options);
			record("scheduler-complete", { delay });
		});
		const message = (
			content: AssistantMessage["content"],
			stopReason: AssistantMessage["stopReason"],
			timestamp: number,
		): AssistantMessage => ({
			role: "assistant",
			content,
			stopReason,
			timestamp,
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
		});
		const calls: ToolCall[] = names.map((_name, index) => ({
			type: "toolCall",
			id: `failed-call-${index}`,
			name: "write",
			arguments: { path: `file${index}.ts`, content: `danger${index}();` },
		}));
		const tool: AgentTool = {
			name: "write",
			label: "Write",
			description: "Rehearsal write",
			parameters: type({ path: "string", content: "string" }),
			matcherEntries: args => {
				const value = args as { path: string; content: string };
				return [{ path: value.path, digest: value.content }];
			},
			execute: async () => {
				throw new Error("Interrupted tool must not execute");
			},
		};
		let streams = 0;
		let initialAborts = 0;
		let freshAborts = 0;
		const timestamps: number[] = [];
		const notifications: string[][] = [];
		const notices: string[] = [];
		const agent = new Agent({
			getApiKey: () => "test-key",
			convertToLlm,
			initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")!, tools: [tool] },
			streamFn: (_model, _context, options) => {
				streams++;
				record("stream-start", { streams });
				const stream = new AssistantMessageEventStream();
				if (streams === 1)
					queueMicrotask(() => {
						const partial = message(calls, "toolUse", 1720000000100);
						options?.signal?.addEventListener(
							"abort",
							() => {
								initialAborts++;
								record("initial-abort");
								stream.push({ type: "error", reason: "aborted", error: { ...partial, stopReason: "aborted" } });
							},
							{ once: true },
						);
						stream.push({ type: "start", partial });
						for (const [contentIndex, call] of calls.entries()) {
							stream.push({ type: "toolcall_start", contentIndex, partial });
							stream.push({
								type: "toolcall_delta",
								contentIndex,
								delta: JSON.stringify(call.arguments),
								partial,
							});
							stream.push({ type: "toolcall_end", contentIndex, toolCall: call, partial });
						}
					});
				else
					queueMicrotask(() => {
						const partial = message([{ type: "text", text: "Independent answer." }], "stop", 1720000000200);
						options?.signal?.addEventListener(
							"abort",
							() => {
								freshAborts++;
								record("fresh-user-stream-aborted");
								stream.push({ type: "error", reason: "aborted", error: { ...partial, stopReason: "aborted" } });
							},
							{ once: true },
						);
						stream.push({ type: "start", partial });
						void freshRelease.promise.then(() => stream.push({ type: "done", reason: "stop", message: partial }));
					});
				return stream;
			},
		});
		const abortSpy = vi.spyOn(agent, "abort");
		agent.subscribe(event => {
			if (event.type === "message_update" && event.assistantMessageEvent.type === "toolcall_delta")
				timestamps.push(event.message.timestamp);
			if (
				event.type === "message_start" &&
				event.message.role === "assistant" &&
				event.message.timestamp === 1720000000200
			) {
				record("fresh-assistant-start");
				freshStarted.resolve();
			}
			if (event.type === "message_end") record("core-message-end", { message: event.message });
		});
		const journal = SessionManager.inMemory();
		const session = new AgentSession({
			agent,
			sessionManager: journal,
			settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
			modelRegistry: sharedModelRegistry,
			ttsrManager: manager,
		});
		session.subscribe(event => {
			if (event.type === "notice") {
				notices.push(event.message);
				record("notice", { message: event.message });
			}
			if (event.type === "ttsr_triggered") {
				const matched = event.rules.map(rule => rule.name);
				notifications.push(matched);
				record("triggered", { names: matched });
				if (matched.includes(names[0]!)) triggeredA.resolve();
			}
		});
		let freshPrompt: Promise<boolean> | undefined;
		const firstPrompt = session.prompt("First request.");
		try {
			await untilAborted(AbortSignal.timeout(3000), Promise.all(ready.map(gate => gate.promise)));
			expect(initialAborts).toBe(0);
			expect(timestamps).toEqual([1720000000100, 1720000000100]);
			release[0]!.resolve();
			await untilAborted(AbortSignal.timeout(3000), triggeredA.promise);
			await untilAborted(AbortSignal.timeout(3000), firstPrompt);
			await session.waitForIdle();
			expect(omitted).toBe(true);
			expect(notices).toHaveLength(1);
			expect(session.isTtsrAbortPending).toBe(false);
			expect(
				journal
					.getEntries()
					.some(
						entry =>
							entry.type === "message" &&
							entry.message.role === "assistant" &&
							entry.message.timestamp === 1720000000100,
					),
			).toBe(true);
			observationSpy.mockRestore();
			observationRestored = true;
			record("first-prompt-settled", { streams, notices, timers });
			freshPrompt = session.prompt("Independent new user request.");
			await untilAborted(AbortSignal.timeout(3000), freshStarted.promise);
			record("release-old-b-into-fresh-prompt");
			release[1]!.resolve();
			await returnedB.promise;
			await originalSchedulerWait(0);
			record("fresh-prompt-after-old-b", { freshAborts, streams, timers, notices, notifications });
			expect(freshAborts).toBe(0);
			freshRelease.resolve();
			await untilAborted(AbortSignal.timeout(3000), freshPrompt);
			await session.waitForIdle();
			expect(abortSpy).toHaveBeenCalledTimes(1);
			expect(streams).toBe(2);
			expect(timers).toBe(1);
			expect(notifications).toEqual([[names[0]!]]);
			expect(notices).toHaveLength(1);
			expect(journal.getInjectedTtsrRules()).toEqual([]);
			expect(
				journal
					.getEntries()
					.some(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection"),
			).toBe(false);
			expect(
				journal
					.getEntries()
					.some(
						entry =>
							entry.type === "message" &&
							entry.message.role === "assistant" &&
							entry.message.timestamp === 1720000000200 &&
							entry.message.stopReason === "stop",
					),
			).toBe(true);
		} finally {
			release.forEach(gate => {
				gate.resolve();
			});
			freshRelease.resolve();
			process.stdout.write(`FAILED_ATTEMPT_TRACE ${JSON.stringify(trace)}\n`);
			await session.dispose();
			if (!observationRestored) observationSpy.mockRestore();
			checkSpy.mockRestore();
			abortSpy.mockRestore();
			schedulerSpy.mockRestore();
		}
	}, 10000);
});
