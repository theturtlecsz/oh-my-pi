import { describe, expect, test } from "bun:test";
import { Effort, type Api, type Model } from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";
import {
	resolveModelRoleValue,
	resolveAgentModelPatterns,
	rolePriorityDefaults,
} from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as fs from "node:fs";
import * as path from "node:path";
import { resolveAuditModel } from "../../../session-system/extensions/workflow/auditor-runner";

function m(provider: string, id: string, kind?: string): Model<Api> {
	return buildModel({
		id,
		name: `${provider}/${id}`,
		api: "openai-completions",
		provider,
		baseUrl: `https://${provider}.example.com`,
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 128000,
		maxTokens: 8192,
		...(kind ? { kind: kind as never } : {}),
	} as never);
}

describe("probe", () => {
	test("slow prio", () => {
		console.log("PRIO slow", rolePriorityDefaults("slow"));
		console.log("PRIO judge", rolePriorityDefaults("judge"));
		const pool = [m("openai-codex", "gpt-5.6-sol"), m("anthropic", "claude-opus-4-8")];
		const r = resolveModelRoleValue("@slow", pool, { settings: Settings.isolated() });
		console.log("slow none:", r.model?.provider, r.model?.id, r.matchedPatternIndex);
		const r2 = resolveModelRoleValue("@slow", pool, {
			settings: Settings.isolated({ modelRoles: { default: "anthropic/claude-opus-4-8" } }),
		});
		console.log("slow default:", r2.model?.provider, r2.model?.id, r2.matchedPatternIndex);
		const r3 = resolveModelRoleValue("@slow", pool, {
			settings: Settings.isolated({ modelRoles: { slow: "anthropic/claude-opus-4-8" } }),
		});
		console.log("slow configured:", r3.model?.provider, r3.model?.id, r3.matchedPatternIndex);
	});

	test("judge", () => {
		const jev = m("typesafe", "jev-latest", "judge");
		const r = resolveModelRoleValue("@judge", [jev], { settings: Settings.isolated() });
		console.log("judge present:", r.model?.provider, r.model?.id);
		const smol = m("openai-codex", "gpt-5.3-codex-spark");
		const r2 = resolveModelRoleValue("@judge", [smol], { settings: Settings.isolated() });
		console.log("judge absent:", r2.model?.provider, r2.model?.id, r2.matchedPatternIndex);
	});

	test("bundled models exist", () => {
		console.log("sol", getBundledModel("openai-codex", "gpt-5.6-sol")?.id);
		console.log("spark", getBundledModel("openai-codex", "gpt-5.3-codex-spark")?.id);
		console.log("jev", getBundledModel("typesafe", "jev-latest")?.id, getBundledModel("typesafe", "jev-latest")?.kind);
	});

	test("agent frontmatter", () => {
		for (const p of [
			"src/prompts/agents/scout.md",
			"src/prompts/agents/reviewer.md",
		]) {
			const content = fs.readFileSync(path.join(import.meta.dir, "..", p), "utf8");
			const { frontmatter } = parseFrontmatter(content);
			console.log(p, frontmatter.model, frontmatter.name);
		}
		const auditor = fs.readFileSync(
			path.resolve(import.meta.dir, "../../../session-system/agents/auditor.md"),
			"utf8",
		);
		const { frontmatter } = parseFrontmatter(auditor);
		console.log("auditor", frontmatter.model);
		const patterns = resolveAgentModelPatterns({ agentModel: frontmatter.model as string, settings: Settings.isolated() });
		console.log("auditor patterns", patterns.slice(0, 3));
	});

	test("resolveAuditModel with real resolver", () => {
		const k3 = m("kimi-code", "k3");
		const settings = Settings.isolated({ modelRoles: { audit: "kimi-code/k3" } });
		const found = resolveAuditModel({
			models: { resolve: (spec) => resolveModelRoleValue(spec, [k3], { settings }).model },
		});
		console.log("audit found", found.provider, found.id);
		const settings2 = Settings.isolated({ modelRoles: { audit: "kimi-code/missing" } });
		expect(() =>
			resolveAuditModel({
				models: { resolve: (spec) => resolveModelRoleValue(spec, [k3], { settings: settings2 }).model },
			}),
		).toThrow("@audit");
	});
});
