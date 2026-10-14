import { describe, expect, test } from "bun:test";
import type { Api, Model } from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import {
	resolveAgentModelSelection,
	resolveModelOverride,
	resolveModelRoleValue,
	resolveRoleChain,
} from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";
import { resolveAuditModel } from "../../../session-system/extensions/workflow/auditor-runner";

// Offline session-selection evidence only: no live execution, concurrency lanes,
// or owner assignments. Existing model-resolver.test.ts covers caller precedence;
// session-system/tests/auditor-runner.test.ts covers subprocess settings/role and
// transport preflight. Here the bundled YAML selectors reach concrete models.
function fixtureModel(provider: string, id: string, api: Api = "openai-completions"): Model<Api> {
	return buildModel({
		provider,
		id,
		name: `${provider}/${id}`,
		api,
		baseUrl: "https://model-policy.invalid",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 128000,
		maxTokens: 8192,
	});
}

const flash = fixtureModel("google-antigravity", "gemini-3.8-flash", "google-gemini-cli");
const spark = fixtureModel("openai-codex", "gpt-5.3-codex-spark", "openai-codex-responses");
const fable = fixtureModel("anthropic", "claude-fable-5-1", "anthropic-messages");
const sol = fixtureModel("openai-codex", "gpt-5.6-sol", "openai-codex-responses");
const jev = fixtureModel("typesafe", "jev-latest");
// Put competing providers and older slow candidates before the desired model:
// selecting the first available model or a bare shared id must not pass.
const available = [
	fixtureModel("openai", "gpt-5.6-sol", "openai-responses"),
	fable,
	fixtureModel("kimi-code", "k3"),
	fixtureModel("zai", "glm-5.3"),
	fixtureModel("anthropic", "claude-opus-5-5", "anthropic-messages"),
	fixtureModel("openai-codex", "gpt-5.5", "openai-codex-responses"),
	spark,
	flash,
	sol,
];

async function agentSelector(relativePath: string): Promise<string> {
	const { frontmatter } = parseFrontmatter(await Bun.file(`${import.meta.dir}/${relativePath}`).text());
	if (typeof frontmatter.model !== "string") throw new Error(`Missing model selector in ${relativePath}`);
	return frontmatter.model;
}

function selectAgent(selector: string, settings: Settings): Model<Api> | undefined {
	const { patterns } = resolveAgentModelSelection({ agentModel: selector, settings });
	return resolveModelOverride(patterns, { getAvailable: () => available }, settings).model;
}

function expectSelection(model: Model<Api> | undefined, provider: string, id: string): void {
	expect({ provider: model?.provider, id: model?.id }).toEqual({ provider, id });
}

describe("offline model-policy caller selection", () => {
	test("bundled scout resolves smol priority ahead of an available slow model and Spark", async () => {
		const selector = await agentSelector("../src/prompts/agents/scout.md");
		expectSelection(selectAgent(selector, Settings.isolated()), "google-antigravity", "gemini-3.8-flash");
	});

	test("bundled reviewer resolves slow to Codex Sol ahead of competing current models without a default", async () => {
		const selector = await agentSelector("../src/prompts/agents/reviewer.md");
		expectSelection(selectAgent(selector, Settings.isolated()), "openai-codex", "gpt-5.6-sol");
	});

	test("bundled scout and reviewer inherit a configured session default before their priority chains", async () => {
		const settings = Settings.isolated({ modelRoles: { default: "kimi-code/k3" } });
		for (const agent of ["scout", "reviewer"]) {
			const selector = await agentSelector(`../src/prompts/agents/${agent}.md`);
			expectSelection(selectAgent(selector, settings), "kimi-code", "k3");
		}
	});

	test("bundled reviewer uses configured slow while scout still uses the session default", async () => {
		const settings = Settings.isolated({
			modelRoles: { default: "kimi-code/k3", slow: "anthropic/claude-fable-5-1" },
		});
		const reviewer = await agentSelector("../src/prompts/agents/reviewer.md");
		const scout = await agentSelector("../src/prompts/agents/scout.md");
		expectSelection(selectAgent(reviewer, settings), "anthropic", "claude-fable-5-1");
		expectSelection(selectAgent(scout, settings), "kimi-code", "k3");
	});

	test("judge selects Typesafe Jev when available and falls through tiny to configured smol when absent", () => {
		const settings = Settings.isolated({
			modelRoles: { default: "kimi-code/k3", smol: "openai-codex/gpt-5.3-codex-spark" },
		});
		// Judgment's caller uses the role chain to resolve @judge and its role fallbacks.
		expectSelection(resolveRoleChain("judge", settings, [...available, jev])[0]?.model, "typesafe", "jev-latest");
		expectSelection(resolveRoleChain("judge", settings, available)[0]?.model, "openai-codex", "gpt-5.3-codex-spark");
	});

	test("auditor YAML and native runner follow changes to the isolated audit binding", async () => {
		const selector = await agentSelector("../../../session-system/agents/auditor.md");
		const settings = Settings.isolated({ modelRoles: { audit: "openai-codex/gpt-5.6-sol" } });
		const ctx = {
			models: { resolve: (value: string) => resolveModelRoleValue(value, available, { settings }).model },
		};
		expectSelection(selectAgent(selector, settings), "openai-codex", "gpt-5.6-sol");
		expectSelection(resolveAuditModel(ctx), "openai-codex", "gpt-5.6-sol");

		settings.setModelRole("audit", "anthropic/claude-fable-5-1");
		expectSelection(selectAgent(selector, settings), "anthropic", "claude-fable-5-1");
		expectSelection(resolveAuditModel(ctx), "anthropic", "claude-fable-5-1");
	});

	test("native audit selection refuses a missing audit binding despite available models and a session default", () => {
		const settings = Settings.isolated({ modelRoles: { default: "openai-codex/gpt-5.6-sol" } });
		const ctx = {
			models: { resolve: (value: string) => resolveModelRoleValue(value, available, { settings }).model },
		};
		expect(() => resolveAuditModel(ctx)).toThrow("Could not resolve @audit role — fix modelRoles.audit and retry");
	});
});
