import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { authPolicyFor } from "@oh-my-pi/pi-catalog/compat/auth";
import { providerEntry, seedModels } from "@oh-my-pi/pi-catalog/compat/providers";
import { resolveProviderModels } from "@oh-my-pi/pi-catalog/model-manager";
import { calculateUsageCost, getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { PROVIDER_DESCRIPTORS } from "@oh-my-pi/pi-catalog/provider-models/descriptors";
import { claudeCliModelManagerOptions } from "@oh-my-pi/pi-catalog/provider-models/special";
import type { Usage } from "@oh-my-pi/pi-catalog/types";

const COMPAT_DIR = path.join(import.meta.dir, "../src/compat");

/** The authored Opus 5.5 row: published Anthropic list prices and limits. */
const EXPECTED_COST = { input: 4, output: 20, cacheRead: 0.2, cacheWrite: 5 };

function mixedUsage(): Usage {
	const input = 1_000;
	const output = 500;
	const cacheRead = 2_000;
	const cacheWrite = 300;
	return {
		input,
		output,
		cacheRead,
		cacheWrite,
		totalTokens: input + output + cacheRead + cacheWrite,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

describe("Claude CLI provider support", () => {
	test("bundles the authored Opus 5.5 row with the published prices and limits", () => {
		const model = getBundledModel("claude-cli", "claude-opus-5-5");
		expect(model).toBeDefined();
		expect(model.api).toBe("claude-cli");
		expect(model.provider).toBe("claude-cli");
		expect(model.cost).toEqual(EXPECTED_COST);
		expect(model.contextWindow).toBe(1_000_000);
		expect(model.maxTokens).toBe(128_000);
		// The CLI transport runs no tool loop, so the row must not advertise tools.
		expect(model.supportsTools).toBe(false);
	});

	test("prices a turn with mixed input, output, and cache traffic as a finite positive cost", () => {
		const usage = mixedUsage();
		const cost = calculateUsageCost(getBundledModel("claude-cli", "claude-opus-5-5").cost, usage);

		expect(Number.isFinite(cost.total)).toBe(true);
		expect(cost.total).toBeGreaterThan(0);
		// Every bucket is billable on this row, so a zero rate would silently drop
		// cache-read or cache-write charges from the receipt.
		expect(cost.input).toBeGreaterThan(0);
		expect(cost.output).toBeGreaterThan(0);
		expect(cost.cacheRead).toBeGreaterThan(0);
		expect(cost.cacheWrite).toBeGreaterThan(0);
	});

	test("registers a descriptor whose manager serves the seed with no dynamic fetch", async () => {
		const descriptor = PROVIDER_DESCRIPTORS.find(item => item.providerId === "claude-cli");
		expect(descriptor).toBeDefined();
		expect(descriptor?.defaultModel).toBe("claude-opus-5-5");
		expect(descriptor?.allowUnauthenticated).toBe(true);
		expect(descriptor?.skipCrossProviderReferenceFills).toBe(true);

		const options = claudeCliModelManagerOptions();
		expect(options.providerId).toBe("claude-cli");
		expect(options.cacheProviderId).toBe("claude-cli");
		expect(options.fetchDynamicModels).toBeUndefined();
		expect(options.staticModels).toEqual(seedModels("claude-cli"));

		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-catalog-claude-cli-"));
		try {
			const result = await resolveProviderModels(
				{ ...options, cacheDbPath: path.join(tempDir, "models.db") },
				"offline",
			);
			expect(result.models.map(model => model.id)).toEqual(["claude-opus-5-5"]);
			expect(result.models[0]?.api).toBe("claude-cli");
			expect(result.models[0]?.cost).toEqual(EXPECTED_COST);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	test("catalog entry pins Opus 5.5 as the default and opts out of reference fills", () => {
		const entry = providerEntry("claude-cli");
		expect(entry?.defaultModel).toBe("claude-opus-5-5");
		expect(entry?.skipCrossProviderReferenceFills).toBe(true);
		expect(entry?.allowUnauthenticated).toBe(true);

		// The CLI carries its own login, so the provider must resolve with no key
		// and offer no interactive login flow.
		const policy = authPolicyFor("claude-cli");
		expect(policy).toBeDefined();
		expect(policy?.allowsMissingApiKey).toBe(true);
		expect(policy?.login).toBeUndefined();
		expect(policy?.env).toBeUndefined();
	});

	test("generated id unions declare the provider", async () => {
		// The generated unions are the compile-time membership contract: a provider
		// missing from `provider-ids.ts` (no consumer regenerates it in tests) or
		// `auth-ids.ts` silently drops out of typed registries.
		const providerIds = await Bun.file(path.join(COMPAT_DIR, "provider-ids.ts")).text();
		const authIds = await Bun.file(path.join(COMPAT_DIR, "auth-ids.ts")).text();
		expect(providerIds).toContain('"claude-cli"');
		expect(authIds).toContain('"claude-cli"');
	});
});
