import { afterEach, describe, expect, it, vi } from "bun:test";
import { Agent } from "@oh-my-pi/pi-agent-core";
import type { Model } from "@oh-my-pi/pi-ai";
import { createMockModel, type MockModel, type MockResponseSource } from "@oh-my-pi/pi-ai/providers/mock";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import { ExtensionRuntime } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner as RealExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type {
	BeforeAgentStartEvent,
	BeforeAgentStartEventResult,
	Extension,
} from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { AgentSession, PromptBlockedError } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";

// Contract: a `before_agent_start` handler returning `{ block: true }` refuses
// the turn. The prompt rejects with `PromptBlockedError` carrying the reason,
// no provider request is made, and no user message is persisted. This is the
// gate print mode and CLI initial messages need, because `input` never runs on
// those paths and cannot refuse them (OMP-536).

const BLOCK_REASON = "blocked by policy handler";

function createModel(): Model<"openai-responses"> {
	return buildModel({
		id: "mock",
		name: "mock",
		api: "openai-responses",
		provider: "openai",
		baseUrl: "https://example.invalid",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 8192,
		maxTokens: 2048,
	});
}

function extension(
	name: string,
	handler: (event: BeforeAgentStartEvent) => Promise<BeforeAgentStartEventResult | undefined>,
	onRun?: () => void,
): Extension {
	return {
		path: name,
		resolvedPath: name,
		handlers: new Map([
			[
				"before_agent_start",
				[
					async (...args: unknown[]) => {
						onRun?.();
						return handler(args[0] as BeforeAgentStartEvent);
					},
				],
			],
		]),
		tools: new Map(),
		assistantThinkingRenderers: [],
		fileWriteFallbackHandlers: [],
		fileDeleteFallbackHandlers: [],
		messageRenderers: new Map(),
		composerShapes: new Map(),
		commands: new Map(),
		flags: new Map(),
		shortcuts: new Map(),
	};
}

describe("AgentSession before_agent_start block", () => {
	let session: AgentSession | undefined;

	afterEach(async () => {
		if (session) {
			await session.dispose();
			session = undefined;
		}
		vi.restoreAllMocks();
	});

	function createSession(
		responses: MockResponseSource,
		emitBeforeAgentStart: ExtensionRunner["emitBeforeAgentStart"],
	): { session: AgentSession; mock: MockModel; manager: SessionManager } {
		const mock = createMockModel({ responses });
		const manager = SessionManager.inMemory();
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: {
				model: createModel(),
				systemPrompt: ["initial-base"],
				tools: [],
				messages: [],
			},
			convertToLlm,
			streamFn: mock.stream,
		});

		session = new AgentSession({
			agent,
			sessionManager: manager,
			settings: Settings.isolated({ "compaction.enabled": false, "todo.enabled": false }),
			modelRegistry: { getApiKey: async () => "test-key" } as never,
			extensionRunner: {
				setTaskResultProcessingGate: () => {},
				emitBeforeAgentStart,
				emit: async () => undefined,
			} as unknown as ExtensionRunner,
		});

		return { session, mock, manager };
	}

	it("rejects with the handler's reason and dispatches nothing", async () => {
		const { session, mock, manager } = createSession([{ content: ["Done"] }], async () => ({
			block: { reason: BLOCK_REASON },
		}));

		let error: unknown;
		try {
			await session.prompt("hi");
		} catch (caught) {
			error = caught;
		}

		expect(error).toBeInstanceOf(PromptBlockedError);
		expect((error as Error).name).toBe("PromptBlockedError");
		expect((error as Error).message).toBe(BLOCK_REASON);
		// The refusal reached the session before any provider request, and the
		// prompt never entered the transcript.
		expect(mock.calls).toHaveLength(0);
		const persistedUsers = manager
			.getEntries()
			.filter(entry => entry.type === "message" && entry.message.role === "user");
		expect(persistedUsers).toHaveLength(0);
	});

	it("dispatches one request when no handler blocks", async () => {
		const { session, mock } = createSession([{ content: ["Done"] }], async () => undefined);

		await expect(session.prompt("hi")).resolves.toBe(true);
		await session.waitForIdle();

		expect(mock.calls).toHaveLength(1);
	});
});

describe("ExtensionRunner before_agent_start block", () => {
	const manager = SessionManager.inMemory();
	const registry = { getApiKey: async () => "test-key" } as never;

	function runner(extensions: Extension[]): RealExtensionRunner {
		return new RealExtensionRunner(extensions, new ExtensionRuntime(), manager.getCwd(), manager, registry);
	}

	it("stops the chain and defaults the reason to the extension path", async () => {
		let laterRan = false;
		const blocking = extension("blocking-ext", async () => ({ block: true }));
		const later = extension(
			"later-ext",
			async () => undefined,
			() => {
				laterRan = true;
			},
		);

		const result = await runner([blocking, later]).emitBeforeAgentStart("hi", undefined, ["base"]);

		expect(result?.block?.reason).toBe("Prompt blocked by extension blocking-ext");
		expect(laterRan).toBe(false);
	});

	it("preserves an explicit reason", async () => {
		const result = await runner([
			extension("blocking-ext", async () => ({ block: true, reason: BLOCK_REASON })),
		]).emitBeforeAgentStart("hi", undefined, ["base"]);

		expect(result?.block?.reason).toBe(BLOCK_REASON);
	});
});
