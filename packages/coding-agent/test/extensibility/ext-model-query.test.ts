import { describe, expect, test } from "bun:test";
import type { Api, Model } from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { Effort } from "@oh-my-pi/pi-catalog/effort";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { createExtensionModelQuery } from "../../src/extensibility/extensions/model-api";

function model(id: string, name: string, provider: string, thinking?: Model["thinking"]): Model<"anthropic-messages"> {
	return buildModel({
		id,
		name,
		api: "anthropic-messages",
		provider,
		baseUrl: "https://example.test",
		reasoning: thinking !== undefined,
		thinking,
		input: ["text"],
		cost: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 200000,
		maxTokens: 8192,
	});
}

const claudeThinking: Model["thinking"] = {
	mode: "anthropic-adaptive",
	efforts: [Effort.Minimal, Effort.Low, Effort.Medium, Effort.High],
};

const claude = model("claude-opus-4-8", "Claude Opus 4.8", "anthropic", claudeThinking);
const claudePrev = model("claude-opus-4-7", "Claude Opus 4.7", "anthropic", claudeThinking);
const gpt = model("gpt-5.4", "GPT-5.4", "openai");

const available = [claude, gpt] as Model<Api>[];

/** Minimal registry stub: only the methods the facade and core resolver touch. */
function registry(): ModelRegistry {
	return {
		getAvailable: () => available,
	} as unknown as ModelRegistry;
}

describe("createExtensionModelQuery", () => {
	test("list() and current() pass through to the registry and session model", () => {
		const q = createExtensionModelQuery(registry(), undefined, () => gpt);
		expect(q.list()).toEqual(available);
		expect(q.current()).toBe(gpt);
	});

	test("current() reflects the live session model, read lazily", () => {
		let active: Model<Api> | undefined = claude;
		const q = createExtensionModelQuery(registry(), undefined, () => active);
		expect(q.current()).toBe(claude);
		active = gpt;
		expect(q.current()).toBe(gpt);
	});

	test("resolve() matches model strings through the core resolver", () => {
		const q = createExtensionModelQuery(registry(), undefined, () => undefined);
		expect(q.resolve("anthropic/claude-opus-4-8")).toBe(claude);
		expect(q.resolve("gpt-5.4")?.provider).toBe("openai");
		expect(q.resolve("definitely-not-a-model")).toBeUndefined();
	});

	test("resolve() honors configured role aliases via the same settings-backed path as core", () => {
		const settings = Settings.isolated({
			modelRoles: {
				slow: "anthropic/claude-opus-4-8",
			},
		});
		const q = createExtensionModelQuery(registry(), settings, () => undefined);
		expect(q.resolve("@slow")).toBe(claude);
	});

	test("resolveSelection() preserves configured explicit medium thinking level and pattern metadata", () => {
		const settings = Settings.isolated({
			modelRoles: {
				slow: "anthropic/claude-opus-4-8:medium",
			},
		});
		const q = createExtensionModelQuery(registry(), settings, () => undefined);
		const selection = q.resolveSelection("@slow");
		expect(selection.model).toBe(claude);
		expect(selection.thinkingLevel).toBe(Effort.Medium);
		expect(selection.requestedThinkingLevel).toBe(Effort.Medium);
		expect(selection.matchedPattern).toBe("anthropic/claude-opus-4-8:medium");
		expect(selection.explicitThinkingLevel).toBe(true);
		expect(selection.matchedPatternIndex).toBe(0);
		expect(selection.warning).toBeUndefined();
	});

	test("resolveSelection() distinguishes fallback matched index when first choice is missing", () => {
		const settings = Settings.isolated({
			modelRoles: {
				fallbackRole: ["missing-first-choice", "openai/gpt-5.4"],
			},
		});
		const q = createExtensionModelQuery(registry(), settings, () => undefined);
		const selection = q.resolveSelection("@fallbackRole");
		expect(selection.model).toBe(gpt);
		expect(selection.matchedPatternIndex).toBe(1);
		expect(selection.explicitThinkingLevel).toBe(false);
		expect(selection.thinkingLevel).toBeUndefined();
	});

	test("resolveSelection() returns no model or effort for unknown role", () => {
		const settings = Settings.isolated({});
		const q = createExtensionModelQuery(registry(), settings, () => undefined);
		const selection = q.resolveSelection("@unknownRole");
		expect(selection.model).toBeUndefined();
		expect(selection.thinkingLevel).toBeUndefined();
		expect(selection.explicitThinkingLevel).toBe(false);
		expect(selection.matchedPatternIndex).toBeUndefined();
	});

	test("resolve() projects base model for specs with explicit thinking suffix", () => {
		const q = createExtensionModelQuery(registry(), undefined, () => undefined);
		expect(q.resolve("anthropic/claude-opus-4-8:medium")).toBe(claude);
	});

	test("family() groups a vendor's point releases and separates vendors", () => {
		const q = createExtensionModelQuery(registry(), undefined, () => undefined);
		expect(q.family(claude)).toBe(q.family(claudePrev));
		expect(q.family(claude)).not.toBe(q.family(gpt));
	});
});
