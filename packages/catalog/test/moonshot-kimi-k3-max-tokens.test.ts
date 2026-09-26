import { describe, expect, it } from "bun:test";
import { moonshotKimiK3MaxTokens } from "@oh-my-pi/pi-catalog/provider-models/openai-compat";

describe("moonshotKimiK3MaxTokens", () => {
	it("clamps kimi-k3 candidate output tokens and preserves non-K3 or lower budgets", () => {
		// Simulates a stencil.so metadata shift
		expect(moonshotKimiK3MaxTokens("kimi-k3", 1048576)).toBe(131072);
		// Lower value kept
		expect(moonshotKimiK3MaxTokens("kimi-k3", 65536)).toBe(65536);
		// Non-K3 id passes through
		expect(moonshotKimiK3MaxTokens("kimi-k2.6", 1048576)).toBe(1048576);
	});
});
