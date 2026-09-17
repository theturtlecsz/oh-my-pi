import { describe, expect, test } from "bun:test";
import type { ExtensionModelQuery } from "@oh-my-pi/pi-coding-agent";
import type { Model } from "@oh-my-pi/pi-ai";
import { resolveNativeStageRoute } from "../extensions/workflow/native-stage-profile";

function model(overrides: Partial<Model>): Model {
	return {
		id: "unknown",
		name: "test",
		api: "openai-completions",
		provider: "kimi-code",
		baseUrl: "https://example.invalid",
		reasoning: true,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 1000,
		maxTokens: 100,
		compat: {} as Model["compat"],
		thinking: { mode: "effort", efforts: ["high"] },
		...overrides,
	} as Model;
}

function query(models: Record<string, Model>): ExtensionModelQuery {
	return {
		list: () => Object.values(models),
		current: () => undefined,
		family: selected => `${selected.provider}/${selected.id}`,
		resolve: selector => models[selector.split(":", 1)[0]],
	};
}

const gemini38 = model({
	id: "gemini-3.8-flash",
	provider: "google-antigravity",
	api: "google-gemini-cli",
	thinking: { mode: "google-level", efforts: ["low", "medium", "high"], effortRouting: { high: "gemini-3.8-flash-high" } },
});
const luna = model({ id: "gpt-5.6-luna", provider: "openai-codex", api: "openai-codex-responses" });
const kimi = model({ id: "k3", provider: "kimi-code", api: "openai-completions" });

describe("native stage route profile", () => {
	test("selects exact Gemini 3.8 implementer tuple", () => {
		const route = resolveNativeStageRoute(query({ "google-antigravity/gemini-3.8-flash": gemini38, "openai-codex/gpt-5.6-luna": luna }), "implement");
		expect(route.requestedSelector).toBe("google-antigravity/gemini-3.8-flash:high");
		expect(route.isFallback).toBe(false);
	});

	test("selects Luna when exact Gemini 3.8 is unavailable", () => {
		const route = resolveNativeStageRoute(query({ "openai-codex/gpt-5.6-luna": luna }), "implement");
		expect(route.requestedSelector).toBe("openai-codex/gpt-5.6-luna:high");
		expect(route.isFallback).toBe(true);
	});

	test("rejects Gemini 3.7 as primary and requires exact Fable route", () => {
		const gemini37 = model({ id: "gemini-3.7-flash", provider: "google-antigravity", api: "google-gemini-cli", thinking: { mode: "google-level", efforts: ["high"] } });
		const route = resolveNativeStageRoute(query({ "google-antigravity/gemini-3.7-flash": gemini37, "openai-codex/gpt-5.6-luna": luna }), "implement");
		expect(route.model).toBe(luna);
		expect(route.isFallback).toBe(true);
		expect(() => resolveNativeStageRoute(query({ "kimi-code/k3": kimi }), "plan")).toThrow("No qualified native plan route");
	});

	test("selects exact Kimi audit tuple", () => {
		const route = resolveNativeStageRoute(query({ "kimi-code/k3": kimi }), "audit");
		expect(route.requestedSelector).toBe("kimi-code/k3:high");
		expect(route.isFallback).toBe(false);
	});
});
