import { describe, expect, test, vi } from "bun:test";
import type { Api, Model } from "@oh-my-pi/pi-ai";
import * as ai from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { Effort } from "@oh-my-pi/pi-catalog/effort";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import { createExtensionModelQuery } from "../../packages/coding-agent/src/extensibility/extensions/model-api";
import { resolveAuditPolicy } from "../extensions/workflow/audit-policy";
import { prepareNativeStageRunner } from "../extensions/workflow/auditor-runner";

function makeModel(id: string, provider: string, api: string, efforts?: Effort[]): Model {
	return buildModel({
		id,
		name: id,
		api: api as any,
		provider,
		baseUrl: "https://example.test",
		reasoning: efforts !== undefined,
		thinking: efforts ? { mode: "effort", efforts } : undefined,
		input: ["text"],
		cost: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 200000,
		maxTokens: 8192,
	});
}

function makeRegistry(models: Model[]): ModelRegistry {
	return {
		getAvailable: () => models as Model<Api>[],
	} as unknown as ModelRegistry;
}

describe("audit-policy deterministic contract verification", () => {
	const solModel = makeModel("gpt-5.6-sol", "openai-codex", "openai-codex-responses", [
		Effort.Low,
		Effort.Medium,
		Effort.High,
	]);
	const lunaModel = makeModel("gpt-5.6-luna", "openai-codex", "openai-codex-responses", [
		Effort.Low,
		Effort.Medium,
	]);
	const availableModels = [solModel, lunaModel];

	test("resolves and freezes openai-codex/gpt-5.6-sol:medium with boundPolicySha256", () => {
		const settings = Settings.isolated({
			modelRoles: {
				audit: "openai-codex/gpt-5.6-sol:medium",
			},
		});
		const q = createExtensionModelQuery(makeRegistry(availableModels), settings, () => undefined);
		const res = resolveAuditPolicy(q);
		expect(res.route.requestedSelector).toBe("openai-codex/gpt-5.6-sol:medium");
		expect(res.route.effort).toBe(Effort.Medium);
		expect(res.route.boundPolicySha256).toBe(res.policySha256);
		expect(Object.isFrozen(res.route)).toBe(true);
		expect(Object.isFrozen(res.policy)).toBe(true);
		expect(Object.isFrozen(res.route.model)).toBe(true);
	});

	test("refuses clamped effort when requested effort exceeds model support", () => {
		const settings = Settings.isolated({
			modelRoles: {
				audit: "openai-codex/gpt-5.6-luna:high",
			},
		});
		const q = createExtensionModelQuery(makeRegistry(availableModels), settings, () => undefined);
		expect(() => resolveAuditPolicy(q)).toThrow(/native auditor obligation requires openai-codex\/gpt-5.6-sol:medium/);
	});

	test("refuses fallback pattern substitution when first pattern missing", () => {
		const settings = Settings.isolated({
			modelRoles: {
				audit: ["missing-primary-model", "openai-codex/gpt-5.6-sol:medium"],
			},
		});
		const q = createExtensionModelQuery(makeRegistry(availableModels), settings, () => undefined);
		expect(() => resolveAuditPolicy(q)).toThrow(/substitution disallowed/);
	});

	test("refuses non-sol auditor models even with medium effort", () => {
		const settings = Settings.isolated({
			modelRoles: {
				audit: "openai-codex/gpt-5.6-luna:medium",
			},
		});
		const q = createExtensionModelQuery(makeRegistry(availableModels), settings, () => undefined);
		expect(() => resolveAuditPolicy(q)).toThrow(/native auditor obligation requires openai-codex\/gpt-5.6-sol:medium/);
	});

	test("throws before provider probe when audit policy drifts during awaited credentials preflight", async () => {
		const completeSpy = vi.spyOn(ai, "completeSimple");
		completeSpy.mockClear();

		vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [{
				name: "auditor",
				description: "Auditor",
				systemPrompt: "Audit",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "bundled",
			}],
			projectAgentsDir: null,
		});

		const settings = Settings.isolated({
			modelRoles: {
				audit: "openai-codex/gpt-5.6-sol:medium",
			},
		});
		const q = createExtensionModelQuery(makeRegistry(availableModels), settings, () => solModel);
		const { route: boundRoute } = resolveAuditPolicy(q);

		const registry: ModelRegistry = {
			getAvailable: () => availableModels as Model<Api>[],
			getApiKey: vi.fn(async () => {
				settings.override("modelRoles", { audit: "openai-codex/gpt-5.6-sol:high" });
				return "api-key";
			}),
		} as unknown as ModelRegistry;

		const fakeCtx = {
			cwd: "/tmp",
			taskDepth: 0,
			models: q,
			modelRegistry: registry,
		} as unknown as ExtensionContext;

		await expect(
			prepareNativeStageRunner(fakeCtx, {
				role: "audit",
				boundRoute,
			}),
		).rejects.toThrow(/native auditor obligation requires openai-codex\/gpt-5.6-sol:medium/);

		expect(completeSpy).not.toHaveBeenCalled();
	});
});
