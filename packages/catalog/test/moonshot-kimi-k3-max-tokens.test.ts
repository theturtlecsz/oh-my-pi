/**
 * Moonshot-native Kimi K3 advertises a 1,048,576-token context window but its
 * `/v1/models` envelope omits `max_completion_tokens`, so discovery (and the
 * previous snapshot carried forward by the generator) assigned the
 * context-sized budget as the output ceiling. Every request sends
 * `max_tokens`, so the bundled catalog must pin K3's documented 131,072 output
 * cap while leaving its context window alone.
 */
import { describe, expect, it } from "bun:test";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { moonshotKimiK3MaxTokens } from "@oh-my-pi/pi-catalog/provider-models/openai-compat";

describe("Moonshot Kimi K3 maxTokens cap", () => {
	it("clamps every Kimi K3 id form and leaves other ids untouched", () => {
		for (const id of ["kimi-k3", "moonshotai/kimi-k3", "kimi-k3.1", "kimi-k3-turbo"]) {
			expect(moonshotKimiK3MaxTokens(id, 1_048_576)).toBe(131_072);
		}
		// Already-low candidate stays low — the helper never raises a budget.
		expect(moonshotKimiK3MaxTokens("kimi-k3", 4_096)).toBe(4_096);
		// Non-K3 ids pass through verbatim.
		expect(moonshotKimiK3MaxTokens("kimi-k2.6", 1_048_576)).toBe(1_048_576);
		expect(moonshotKimiK3MaxTokens("moonshot-v1-128k", 131_072)).toBe(131_072);
	});

	it("ships the capped output budget with the full context window", () => {
		const model = getBundledModel("moonshot", "kimi-k3");
		expect(model).toBeDefined();
		expect(model.maxTokens).toBe(131_072);
		expect(model.contextWindow).toBe(1_048_576);
	});
});
