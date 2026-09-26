import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as ai from "@oh-my-pi/pi-ai";
import { Effort, type Model } from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { classifyDifficulty } from "@oh-my-pi/pi-coding-agent/auto-thinking/classifier";
import { type JevUsageEntry, resetDefaultJevBreaker } from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

describe("auto-thinking Jev classifier", () => {
	let stub: StubJevServer;
	let entries: JevUsageEntry[];
	let savedApiKey: string | undefined;

	const MAX_LADDER = [Effort.Low, Effort.Medium, Effort.High, Effort.XHigh, Effort.Max];
	const XHIGH_LADDER = [Effort.Low, Effort.Medium, Effort.High, Effort.XHigh];

	function buildLadderModel(id: string, efforts: Effort[]): Model {
		return buildModel({
			id,
			name: id,
			api: "openai-completions",
			provider: "mock",
			baseUrl: "https://example.com",
			reasoning: true,
			thinking: { mode: "effort", efforts },
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: 128_000,
			maxTokens: 4096,
		});
	}

	const classifierModel = getBundledModel("anthropic", "claude-sonnet-4-6")!;

	function createFixture(
		overrides: {
			jevEnabled?: boolean;
			jevAutoThinking?: boolean;
			autoThinkingMaxEffort?: "xhigh" | "max";
			autoThinkingConfidence?: number;
			autoThinkingMaxSignal?: number;
			model?: Model;
		} = {},
	) {
		const targetModel = overrides.model ?? buildLadderModel("mock-max", MAX_LADDER);
		const settingsStore: Record<string, unknown> = {
			"jev.enabled": overrides.jevEnabled ?? true,
			"jev.autoThinking": overrides.jevAutoThinking ?? true,
			"jev.baseUrl": stub.baseUrl,
			"jev.autoThinkingConfidence": overrides.autoThinkingConfidence ?? 0.5,
			"jev.autoThinkingMaxSignal": overrides.autoThinkingMaxSignal ?? 0.7,
			"providers.autoThinkingModel": "online",
			"providers.autoThinkingMaxEffort": overrides.autoThinkingMaxEffort ?? "xhigh",
		};
		const settings = {
			get(path: string) {
				return settingsStore[path];
			},
			getModelRole(role: string) {
				return role === "smol" ? `${classifierModel.provider}/${classifierModel.id}` : undefined;
			},
			getStorage() {
				return undefined;
			},
		} as never;

		const registry = {
			getAvailable: () => [classifierModel],
			getApiKey: async () => "test-key",
			resolver: () => async () => "test-key",
			getApiKeyForProvider: async () => "test-key",
		} as never;

		const deps = {
			settings,
			registry,
			model: targetModel,
			recordJevUsage: (entry: JevUsageEntry) => {
				entries.push(entry);
			},
		};

		return { deps };
	}

	beforeEach(() => {
		entries = [];
		stub = startStubJevServer();
		resetDefaultJevBreaker();
		savedApiKey = process.env.TYPESAFE_API_KEY;
		process.env.TYPESAFE_API_KEY = "test-typesafe-key";
	});

	afterEach(() => {
		stub.stop();
		resetDefaultJevBreaker();
		vi.restoreAllMocks();
		if (savedApiKey !== undefined) {
			process.env.TYPESAFE_API_KEY = savedApiKey;
		} else {
			delete process.env.TYPESAFE_API_KEY;
		}
	});

	it("flags off: no stub request, and the smol path runs as before", async () => {
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "high" }],
		} as never);

		// Case 1: jev.enabled is false
		const fixture1 = createFixture({ jevEnabled: false, jevAutoThinking: true });
		const effort1 = await classifyDifficulty("fix parsing bug", fixture1.deps);

		expect(stub.requests).toHaveLength(0);
		expect(completeSimpleSpy).toHaveBeenCalledTimes(1);
		expect(effort1).toBe(Effort.High);

		completeSimpleSpy.mockClear();

		// Case 2: jev.autoThinking is false
		const fixture2 = createFixture({ jevEnabled: true, jevAutoThinking: false });
		const effort2 = await classifyDifficulty("fix parsing bug", fixture2.deps);

		expect(stub.requests).toHaveLength(0);
		expect(completeSimpleSpy).toHaveBeenCalledTimes(1);
		expect(effort2).toBe(Effort.High);
	});

	it("on, stub high 0.8: returns High with no completeSimple call", async () => {
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple");
		stub.setAnswers({
			difficulty: {
				probabilities: {
					low: 0.05,
					medium: 0.1,
					high: 0.8,
					xhigh: 0.05,
				},
			},
			no_repro: { probability: 0.1 },
			irreversible: { probability: 0.1 },
			live_cutover: { probability: 0.1 },
		});

		const fixture = createFixture();
		const effort = await classifyDifficulty("refactor database connection pool", fixture.deps);

		expect(effort).toBe(Effort.High);
		expect(completeSimpleSpy).not.toHaveBeenCalled();
		expect(stub.requests).toHaveLength(1);

		const requestBody = stub.requests[0].body as {
			model: string;
			state: string;
			questions: Record<string, { type: string; options?: string[]; instructions: string }>;
		};
		expect(requestBody.model).toBe("jev-latest");
		expect(requestBody.questions.difficulty.type).toBe("choice");
		expect(requestBody.questions.difficulty.options).toEqual(["low", "medium", "high", "xhigh"]);
		expect(requestBody.questions.no_repro.type).toBe("noul");
		expect(requestBody.questions.irreversible.type).toBe("noul");
		expect(requestBody.questions.live_cutover.type).toBe("noul");
	});

	it("below the threshold: undefined, with no smol call", async () => {
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple");
		stub.setAnswers({
			difficulty: {
				probabilities: {
					low: 0.4,
					medium: 0.3,
					high: 0.2,
					xhigh: 0.1,
				},
			},
			no_repro: { probability: 0.1 },
			irreversible: { probability: 0.1 },
			live_cutover: { probability: 0.1 },
		});

		const fixture = createFixture({ autoThinkingConfidence: 0.5 });
		const effort = await classifyDifficulty("ambiguous request", fixture.deps);

		expect(effort).toBeUndefined();
		expect(completeSimpleSpy).not.toHaveBeenCalled();
		expect(stub.requests).toHaveLength(1);
	});

	it("xhigh + irreversible 0.9: Max when ceiling is max; XHigh when it is xhigh", async () => {
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple");
		stub.setAnswers({
			difficulty: {
				probabilities: {
					low: 0.02,
					medium: 0.03,
					high: 0.1,
					xhigh: 0.85,
				},
			},
			no_repro: { probability: 0.1 },
			irreversible: { probability: 0.9 },
			live_cutover: { probability: 0.1 },
		});

		// Ceiling is max on a model supporting max
		const maxModel = buildLadderModel("mock-max", MAX_LADDER);
		const maxFixture = createFixture({ model: maxModel, autoThinkingMaxEffort: "max" });
		const maxEffort = await classifyDifficulty("drop table and migrate in-place", maxFixture.deps);

		expect(maxEffort).toBe(Effort.Max);
		expect(completeSimpleSpy).not.toHaveBeenCalled();

		// Ceiling is xhigh
		const xhighFixture = createFixture({ model: maxModel, autoThinkingMaxEffort: "xhigh" });
		const xhighEffort = await classifyDifficulty("drop table and migrate in-place", xhighFixture.deps);

		expect(xhighEffort).toBe(Effort.XHigh);
		expect(completeSimpleSpy).not.toHaveBeenCalled();

		// Model ladder tops out at xhigh even with max requested
		const xhighModel = buildLadderModel("mock-xhigh", XHIGH_LADDER);
		const cappedModelFixture = createFixture({ model: xhighModel, autoThinkingMaxEffort: "max" });
		const cappedEffort = await classifyDifficulty("drop table and migrate in-place", cappedModelFixture.deps);

		expect(cappedEffort).toBe(Effort.XHigh);
	});

	it("triggers Max on no_repro or live_cutover signals at or above threshold", async () => {
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple");
		const maxModel = buildLadderModel("mock-max", MAX_LADDER);

		// no_repro >= 0.7
		stub.setAnswers({
			difficulty: {
				probabilities: { low: 0, medium: 0, high: 0.1, xhigh: 0.9 },
			},
			no_repro: { probability: 0.8 },
			irreversible: { probability: 0.1 },
			live_cutover: { probability: 0.1 },
		});
		const noReproFixture = createFixture({ model: maxModel, autoThinkingMaxEffort: "max" });
		expect(await classifyDifficulty("flaky bug with no repro", noReproFixture.deps)).toBe(Effort.Max);

		// live_cutover >= 0.7
		stub.setAnswers({
			difficulty: {
				probabilities: { low: 0, medium: 0, high: 0.1, xhigh: 0.9 },
			},
			no_repro: { probability: 0.1 },
			irreversible: { probability: 0.1 },
			live_cutover: { probability: 0.75 },
		});
		const liveCutoverFixture = createFixture({ model: maxModel, autoThinkingMaxEffort: "max" });
		expect(await classifyDifficulty("zero-downtime cutover", liveCutoverFixture.deps)).toBe(Effort.Max);

		// all signals below 0.7 -> XHigh
		stub.setAnswers({
			difficulty: {
				probabilities: { low: 0, medium: 0, high: 0.1, xhigh: 0.9 },
			},
			no_repro: { probability: 0.69 },
			irreversible: { probability: 0.5 },
			live_cutover: { probability: 0.69 },
		});
		const noSignalFixture = createFixture({ model: maxModel, autoThinkingMaxEffort: "max" });
		expect(await classifyDifficulty("regular hard task", noSignalFixture.deps)).toBe(Effort.XHigh);

		expect(completeSimpleSpy).not.toHaveBeenCalled();
	});

	it("stub 500: the smol path runs", async () => {
		stub.setMode("500");
		const completeSimpleSpy = vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "medium" }],
		} as never);

		const fixture = createFixture();
		const effort = await classifyDifficulty("investigate issue", fixture.deps);

		expect(effort).toBe(Effort.Medium);
		expect(completeSimpleSpy).toHaveBeenCalledTimes(1);
		// Retried once inside decide: total 2 requests
		expect(stub.requests).toHaveLength(2);
	});

	it("stub timeout: the smol path runs", async () => {
		stub.setMode("delay");
		stub.setDelayMs(3200);

		const completeSimpleSpy = vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "low" }],
		} as never);

		const fixture = createFixture();
		const effort = await classifyDifficulty("investigate issue", fixture.deps);

		expect(effort).toBe(Effort.Low);
		expect(completeSimpleSpy).toHaveBeenCalledTimes(1);
		expect(stub.requests).toHaveLength(1);
	});

	it("stub malformed: the smol path runs", async () => {
		stub.setMode("malformed");

		const completeSimpleSpy = vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "high" }],
		} as never);

		const fixture = createFixture();
		const effort = await classifyDifficulty("investigate issue", fixture.deps);

		expect(effort).toBe(Effort.High);
		expect(completeSimpleSpy).toHaveBeenCalledTimes(1);
		expect(stub.requests).toHaveLength(1);
	});

	it("each attempt appends jev_usage with request id, feature:'auto_thinking'", async () => {
		// Single successful attempt
		stub.setAnswers({
			difficulty: {
				probabilities: { low: 0.1, medium: 0.8, high: 0.1, xhigh: 0 },
			},
			no_repro: { probability: 0.1 },
			irreversible: { probability: 0.1 },
			live_cutover: { probability: 0.1 },
		});

		const fixture1 = createFixture();
		await classifyDifficulty("small feature", fixture1.deps);

		expect(entries).toHaveLength(1);
		expect(entries[0].feature).toBe("auto_thinking");
		expect(entries[0].requestId).toBeTruthy();
		expect(typeof entries[0].requestId).toBe("string");
		expect(entries[0].outcome).toBe("ok");
		expect(entries[0].attempt).toBe(1);

		// Two attempts on 500 retry
		entries = [];
		stub.setMode("500");
		vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "medium" }],
		} as never);

		const fixture2 = createFixture();
		await classifyDifficulty("retry feature", fixture2.deps);

		expect(entries).toHaveLength(2);
		expect(entries[0].feature).toBe("auto_thinking");
		expect(entries[0].requestId).toBeTruthy();
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].outcome).toBe("http_error");

		expect(entries[1].feature).toBe("auto_thinking");
		expect(entries[1].requestId).toBeTruthy();
		expect(entries[1].attempt).toBe(2);
		expect(entries[1].outcome).toBe("http_error");

		expect(entries[0].requestId).not.toBe(entries[1].requestId);
	});
});
