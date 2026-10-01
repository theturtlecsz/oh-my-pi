import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as ai from "@oh-my-pi/pi-ai";
import { Effort } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { classifyDifficulty } from "@oh-my-pi/pi-coding-agent/auto-thinking/classifier";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { classifyUnexpectedStop } from "@oh-my-pi/pi-coding-agent/session/unexpected-stop-classifier";
import { type JevUsageEntry, resetDefaultJevBreaker } from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import type { FetchImpl } from "@oh-my-pi/pi-utils";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";
import { cfgJevAutoThinking, cfgJevEnabled, cfgJevUnexpectedStop } from "@oh-my-pi/pi-coding-agent/tiny/jev-settings";

type SpyOnGetter = (
	target: unknown,
	prop: string,
	accessType: "get",
) => { mockRestore(): void; mockReturnValue(value: string): void };

describe("Jev default off", () => {
	let stub: StubJevServer;
	let entries: JevUsageEntry[];
	let savedApiKey: string | undefined;

	beforeEach(() => {
		stub = startStubJevServer();
		resetDefaultJevBreaker();
		entries = [];
		savedApiKey = process.env.TYPESAFE_API_KEY;
		const spyOnGetter = vi.spyOn as unknown as SpyOnGetter;
		spyOnGetter(process.env, "TYPESAFE_API_KEY", "get").mockReturnValue("test-typesafe-key");
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

	it("keeps Jev disabled by default, sends zero stub requests, records no jev_usage, and returns faked smol answers", async () => {
		const model = getBundledModel("anthropic", "claude-sonnet-4-6");
		if (!model) throw new Error("Expected bundled claude-sonnet-4-6 model");

		// Default Settings with no jev.* overrides
		const settings = Settings.isolated();
		settings.setModelRole("smol", `${model.provider}/${model.id}`);

		expect(cfgJevEnabled.get(settings)).toBe(false);
		expect(cfgJevAutoThinking.get(settings)).toBe(false);
		expect(cfgJevUnexpectedStop.get(settings)).toBe(false);

		const registry = {
			getAvailable: () => [model],
			getApiKey: async () => "test-key",
			getApiKeyForProvider: async () => "test-key",
			resolver: () => async () => "test-key",
		} as never;

		const completeSimpleSpy = vi.spyOn(ai, "completeSimple").mockImplementation(async (_model, params) => {
			const promptText = Array.isArray(params.systemPrompt)
				? params.systemPrompt.join("\n")
				: (params.systemPrompt ?? "");
			if (promptText.includes("unexpected")) {
				return {
					stopReason: "stop",
					content: [{ type: "text", text: "YES" }],
				} as never;
			}
			return {
				stopReason: "stop",
				content: [{ type: "text", text: "high" }],
			} as never;
		});

		// Forward any fetch calls to the stub server so if Jev were attempted, the stub would record it
		const testFetch: FetchImpl = async (input, init) => {
			const urlStr = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
			const url = new URL(urlStr);
			return fetch(`${stub.baseUrl}${url.pathname}${url.search}`, init);
		};

		const difficultyResult = await classifyDifficulty(
			{ request: "Write a parser for arithmetic expressions" },
			{
				settings,
				registry,
				model,
				recordJevUsage: entry => entries.push(entry),
				fetch: testFetch,
			},
		);

		const unexpectedStopResult = await classifyUnexpectedStop("I will run the command now.", {
			settings,
			registry,
			sessionId: "test-session-default-off",
			recordJevUsage: entry => entries.push(entry),
			fetch: testFetch,
		});

		// Assert faked smol answers
		expect(difficultyResult).toBe(Effort.High);
		expect(unexpectedStopResult).toBe(true);

		// Assert completeSimple was called twice (once per classifier)
		expect(completeSimpleSpy).toHaveBeenCalledTimes(2);

		// Assert stub received zero requests
		expect(stub.requests).toHaveLength(0);

		// Assert no jev_usage entry was recorded
		expect(entries).toHaveLength(0);
	});
});
