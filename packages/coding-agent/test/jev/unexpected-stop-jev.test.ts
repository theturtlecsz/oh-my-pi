import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import { type } from "@oh-my-pi/omptype";
import { Agent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import * as ai from "@oh-my-pi/pi-ai";
import { createMockModel, type MockModel, type MockResponse } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { type SettingPath, Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import type { CustomEntry } from "@oh-my-pi/pi-coding-agent/session/session-entries";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import {
	type ClassifyUnexpectedStopDeps,
	classifyUnexpectedStop,
} from "@oh-my-pi/pi-coding-agent/session/unexpected-stop-classifier";
import { type JevUsageEntry, resetDefaultJevBreaker } from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import { logger, TempDir } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "../helpers/agent-session-setup";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

const recordToolSchema = type({ value: type("string") });

type Harness = {
	session: AgentSession;
	tempDir: TempDir;
	mock: MockModel;
};

const activeHarnesses: Harness[] = [];
const sharedAuthStorage = createInMemoryAuthStorage();
sharedAuthStorage.setRuntimeApiKey("mock", "test-key");
sharedAuthStorage.setRuntimeApiKey("anthropic", "test-key");
sharedAuthStorage.setRuntimeApiKey("typesafe", "test-key");
const sharedModelRegistry = new ModelRegistry(sharedAuthStorage);

afterAll(() => {
	sharedAuthStorage.close();
});

const recordTool: AgentTool<typeof recordToolSchema, { value: string }> = {
	name: "record",
	label: "Record",
	description: "Record a value",
	parameters: recordToolSchema,
	async execute(_toolCallId, params) {
		return {
			content: [{ type: "text", text: `recorded:${params.value}` }],
			details: { value: params.value },
		};
	},
};

function unexpectedStopResponse(text: string): MockResponse {
	return {
		content: [{ type: "text", text }],
		stopReason: "stop",
	};
}

async function createHarness(
	responses: MockResponse[],
	settingsOverrides: Partial<Record<SettingPath, unknown>> = {},
): Promise<Harness> {
	const tempDir = TempDir.createSync("@pi-unexpected-stop-jev-");
	const mock = createMockModel({ responses });
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		"todo.enabled": false,
		"todo.eager": "default",
		"todo.reminders": false,
		...settingsOverrides,
	});
	settings.setModelRole("default", `${mock.provider}/${mock.id}`);

	const model = getBundledModel("anthropic", "claude-sonnet-4-5") ?? mock;
	const sessionManager = SessionManager.inMemory(tempDir.path());
	const tools = [recordTool as AgentTool];
	let session: AgentSession | undefined;
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: {
			model,
			systemPrompt: ["Test"],
			tools,
			messages: [],
		},
		convertToLlm,
		getToolChoice: () => session?.nextToolChoiceDirective(),
		streamFn: mock.stream,
	});

	const agentSession = new AgentSession({
		agent,
		sessionManager,
		settings,
		modelRegistry: sharedModelRegistry,
		toolRegistry: new Map(tools.map(tool => [tool.name, tool])),
	});
	session = agentSession;
	const harness: Harness = { session: agentSession, tempDir, mock };
	activeHarnesses.push(harness);
	return harness;
}

function makeClassifierDeps(options: {
	stub: StubJevServer;
	enabled?: boolean;
	unexpectedStop?: boolean;
	threshold?: number;
	budgetMs?: number;
}): { deps: ClassifyUnexpectedStopDeps; entries: JevUsageEntry[] } {
	const entries: JevUsageEntry[] = [];
	const baseModel = getBundledModel("anthropic", "claude-sonnet-4-5");
	if (!baseModel) throw new Error("Expected bundled Claude Sonnet 4.5 model");
	const model = { ...baseModel, reasoning: false };

	const settings = {
		get(path: string) {
			if (path === "providers.unexpectedStopModel") return "online";
			if (path === "jev.enabled") return options.enabled ?? true;
			if (path === "jev.unexpectedStop") return options.unexpectedStop ?? true;
			if (path === "jev.baseUrl") return options.stub.baseUrl;
			if (path === "jev.unexpectedStopThreshold") return options.threshold;
			return undefined;
		},
		getModelRole(role: string) {
			return role === "smol" ? `${model.provider}/${model.id}` : undefined;
		},
		getStorage() {
			return undefined;
		},
	} as never;

	const registry = {
		getAvailable: () => [model],
		getApiKey: async () => "test-key",
		getApiKeyForProvider: async () => "test-key",
		resolver: () => async () => "test-key",
	} as never;

	const deps: ClassifyUnexpectedStopDeps = {
		settings,
		registry,
		sessionId: "test-session-1",
		budgetMs: options.budgetMs,
		recordJevUsage: (entry: JevUsageEntry) => {
			entries.push(entry);
		},
	};

	return { deps, entries };
}

describe("Jev unexpected-stop classifier", () => {
	let stub: StubJevServer;
	let savedApiKey: string | undefined;

	beforeEach(() => {
		stub = startStubJevServer();
		resetDefaultJevBreaker();
		savedApiKey = process.env.TYPESAFE_API_KEY;
		process.env.TYPESAFE_API_KEY = "test-typesafe-key";
	});

	afterEach(async () => {
		stub.stop();
		resetDefaultJevBreaker();
		vi.restoreAllMocks();
		if (savedApiKey !== undefined) {
			process.env.TYPESAFE_API_KEY = savedApiKey;
		} else {
			delete process.env.TYPESAFE_API_KEY;
		}
		for (const harness of activeHarnesses) {
			await harness.session.dispose();
			harness.tempDir.removeSync();
		}
		activeHarnesses.length = 0;
	});

	describe("flags off", () => {
		it("makes no stub request and answers via smol path when jev.enabled is false", async () => {
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: false,
				unexpectedStop: true,
			});
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "YES" }],
			} as never);

			const result = await classifyUnexpectedStop("I will run the command now.", deps);

			expect(result).toBe(true);
			expect(stub.requests).toHaveLength(0);
			expect(completeSimpleMock).toHaveBeenCalledTimes(1);
			expect(entries).toHaveLength(0);
		});

		it("makes no stub request and answers via smol path when jev.unexpectedStop is false", async () => {
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: false,
			});
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "NO" }],
			} as never);

			const result = await classifyUnexpectedStop("The task is done.", deps);

			expect(result).toBe(false);
			expect(stub.requests).toHaveLength(0);
			expect(completeSimpleMock).toHaveBeenCalledTimes(1);
			expect(entries).toHaveLength(0);
		});
	});

	describe("flags on with stub p=0.75", () => {
		it("returns true at default threshold (0.70) without calling completeSimple", async () => {
			stub.setAnswers({
				statement: { probability: 0.75 },
			});
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: true,
			});
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "NO" }],
			} as never);

			const result = await classifyUnexpectedStop("I will fix this bug right now.", deps);

			expect(result).toBe(true);
			expect(completeSimpleMock).not.toHaveBeenCalled();
			expect(stub.requests).toHaveLength(1);

			const request = stub.requests[0];
			const payload = request.body as {
				model: string;
				state: string;
				questions: Record<string, { type: string; instructions: string }>;
			};
			expect(payload.model).toBe("jev-latest");
			expect(payload.state).toBe("I will fix this bug right now.");
			expect(payload.questions.statement).toBeDefined();
			expect(payload.questions.statement.type).toBe("noul");
			expect(payload.questions.statement.instructions).toBe(
				"The assistant says it will act, continue working, or call a tool, and then ends without doing so. Messages that report completion or ask if anything else is needed are not unexpected stops.",
			);

			expect(entries).toHaveLength(1);
			expect(entries[0].requestId).toBeDefined();
			expect(typeof entries[0].requestId).toBe("string");
			expect(entries[0].feature).toBe("unexpected_stop");
			expect(entries[0].attempt).toBe(1);
			expect(entries[0].outcome).toBe("ok");
		});

		it("returns false at threshold 0.8 without calling completeSimple", async () => {
			stub.setAnswers({
				statement: { probability: 0.75 },
			});
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: true,
				threshold: 0.8,
			});
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "YES" }],
			} as never);

			const result = await classifyUnexpectedStop("I will fix this bug right now.", deps);

			expect(result).toBe(false);
			expect(completeSimpleMock).not.toHaveBeenCalled();
			expect(stub.requests).toHaveLength(1);
			expect(entries).toHaveLength(1);
			expect(entries[0].feature).toBe("unexpected_stop");
		});
	});

	describe("stub failures fall back to smol answer with exactly one warn line", () => {
		it("falls back to smol answer and logs one warn line on 500", async () => {
			stub.setMode("500");
			const warnSpy = vi.spyOn(logger, "warn");
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "YES" }],
			} as never);
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: true,
			});

			const result = await classifyUnexpectedStop("I will run tests next.", deps);

			expect(result).toBe(true);
			expect(completeSimpleMock).toHaveBeenCalledTimes(1);
			expect(warnSpy).toHaveBeenCalledTimes(1);
			expect(warnSpy.mock.calls[0]?.[0]).toBe("jev: decision call failed");
			expect(warnSpy.mock.calls[0]?.[1]).toMatchObject({
				feature: "unexpected_stop",
				outcome: "http_error",
				attempts: 2,
			});

			expect(entries).toHaveLength(2);
			expect(entries[0].requestId).toBeDefined();
			expect(entries[0].feature).toBe("unexpected_stop");
			expect(entries[0].outcome).toBe("http_error");
			expect(entries[0].attempt).toBe(1);
			expect(entries[1].requestId).toBeDefined();
			expect(entries[1].feature).toBe("unexpected_stop");
			expect(entries[1].outcome).toBe("http_error");
			expect(entries[1].attempt).toBe(2);
			expect(entries[0].requestId).not.toBe(entries[1].requestId);
		});

		it("falls back to smol answer and logs one warn line on timeout", async () => {
			stub.setMode("delay");
			const warnSpy = vi.spyOn(logger, "warn");
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "YES" }],
			} as never);
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: true,
				budgetMs: 50,
			});

			const result = await classifyUnexpectedStop("I will run tests next.", deps);

			expect(result).toBe(true);
			expect(completeSimpleMock).toHaveBeenCalledTimes(1);
			expect(warnSpy).toHaveBeenCalledTimes(1);
			expect(warnSpy.mock.calls[0]?.[0]).toBe("jev: decision call failed");
			expect(warnSpy.mock.calls[0]?.[1]).toMatchObject({
				feature: "unexpected_stop",
				outcome: "timeout",
				attempts: 1,
			});

			expect(entries).toHaveLength(1);
			expect(entries[0].requestId).toBeDefined();
			expect(entries[0].feature).toBe("unexpected_stop");
			expect(entries[0].outcome).toBe("timeout");
		});

		it("falls back to smol answer and logs one warn line on malformed response", async () => {
			stub.setMode("malformed");
			const warnSpy = vi.spyOn(logger, "warn");
			const completeSimpleMock = vi.spyOn(ai, "completeSimple").mockResolvedValue({
				stopReason: "stop",
				content: [{ type: "text", text: "NO" }],
			} as never);
			const { deps, entries } = makeClassifierDeps({
				stub,
				enabled: true,
				unexpectedStop: true,
			});

			const result = await classifyUnexpectedStop("The work is complete.", deps);

			expect(result).toBe(false);
			expect(completeSimpleMock).toHaveBeenCalledTimes(1);
			expect(warnSpy).toHaveBeenCalledTimes(1);
			expect(warnSpy.mock.calls[0]?.[0]).toBe("jev: decision call failed");
			expect(warnSpy.mock.calls[0]?.[1]).toMatchObject({
				feature: "unexpected_stop",
				outcome: "malformed",
				attempts: 1,
			});

			expect(entries).toHaveLength(1);
			expect(entries[0].requestId).toBeDefined();
			expect(entries[0].feature).toBe("unexpected_stop");
			expect(entries[0].outcome).toBe("malformed");
		});
	});

	describe("turn-recovery session integration", () => {
		it("appends jev_usage custom entry to sessionManager on unexpected stop classification", async () => {
			stub.setAnswers({
				statement: { probability: 0.85 },
			});
			const { session } = await createHarness(
				[
					unexpectedStopResponse("I will implement this function now."),
					{ content: [{ type: "text", text: "Continuing with implementation..." }], stopReason: "stop" },
				],
				{
					"features.unexpectedStopDetection": "smart",
					"jev.enabled": true,
					"jev.unexpectedStop": true,
					"jev.baseUrl": stub.baseUrl,
				},
			);

			await session.prompt("Please fix the code");

			const customEntries = session.sessionManager
				.getEntries()
				.filter((entry): entry is CustomEntry => entry.type === "custom" && entry.customType === "jev_usage");

			expect(customEntries.length).toBeGreaterThanOrEqual(1);
			const usage = customEntries[0]?.data as JevUsageEntry | undefined;
			expect(usage).toBeDefined();
			expect(usage?.feature).toBe("unexpected_stop");
			expect(typeof usage?.requestId).toBe("string");
			expect(usage?.requestId.length).toBeGreaterThan(0);
			expect(usage?.outcome).toBe("ok");
		});
	});
});
