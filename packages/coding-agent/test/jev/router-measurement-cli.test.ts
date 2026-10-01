import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as path from "node:path";
import { Effort, type Model } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { TempDir } from "@oh-my-pi/pi-utils";
import { Settings } from "../../src/config/settings";
import { cfgDefaultThinkingLevel } from "../../src/session/settings";
import { cfgJevAutoThinking, cfgJevEnabled, cfgJevUnexpectedStop } from "../../src/tiny/jev-settings";
import type { CurrentSmolHarness } from "../../../../docs/reports/jev-measurement/harness";
import { withoutJevSettings } from "../../../../docs/reports/jev-measurement/router-harness";
import { jevEnvPath } from "../../../../docs/reports/jev-measurement/router-transport";
import { main } from "../../../../docs/reports/jev-measurement/run-router";

describe("run-router CLI and current harness construction", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-run-router-cli-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	it("drives run-router CLI with fake transport and fake current, and fills current metrics in report and JSON", async () => {
		const setsDir = path.join(tempDir.path(), "sets");
		const outPath = path.join(tempDir.path(), "report.md");
		const jsonPath = path.join(tempDir.path(), "results.json");

		// 1. Setup config with test key so no ~/.config/omp/jev.env is touched
		await Bun.write(jevEnvPath(tempDir.path()), "OPENROUTER_API_KEY=sk-test-fake-key\n");

		// 2. Setup datasets
		await Bun.write(
			path.join(setsDir, "prompts.jsonl"),
			`${JSON.stringify({ prompt: "refactor parser", effort: "low" })}\n`,
		);
		await Bun.write(
			path.join(setsDir, "turn-ends.jsonl"),
			`${JSON.stringify({ text: "I will execute the tool now.", label: "continue" })}\n`,
		);
		await Bun.write(path.join(setsDir, "issues.jsonl"), "");

		// 3. Write external modules for --fake-transport and --fake-current
		const fakeTransportPath = path.join(tempDir.path(), "fake-transport.ts");
		await Bun.write(
			fakeTransportPath,
			`export const fakeOpenRouter = async (input, init) => {
	const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
	if (url.includes("/chat/completions")) {
		return new Response(JSON.stringify({
			id: "gen-fake-cli",
			model: "typesafe/jev-router",
			choices: [{ message: { content: "low" } }],
			usage: { prompt_tokens: 50, completion_tokens: 2, total_tokens: 52, cost: 0.0001 },
		}), { status: 200, headers: { "Content-Type": "application/json" } });
	}
	return new Response("{}", { status: 200 });
};
`,
		);

		const fakeCurrentPath = path.join(tempDir.path(), "fake-current.ts");
		await Bun.write(
			fakeCurrentPath,
			`export const fakeCurrent = {
	classifyDifficulty: async () => ({
		effort: "low",
		cost: 0.0002,
		latencyMs: 42.5,
	}),
	classifyUnexpectedStop: async () => ({
		unexpectedStop: true,
		cost: 0.0003,
		latencyMs: 55.0,
	}),
};
`,
		);

		// 4. Drive main() through argv CLI flags
		const results = await main([
			"--sets",
			setsDir,
			"--out",
			outPath,
			"--json",
			jsonPath,
			"--config-home",
			tempDir.path(),
			"--fake-transport",
			fakeTransportPath,
			"--fake-current",
			fakeCurrentPath,
		]);

		// Verify results structure
		expect(results.current.auto_thinking).not.toBeNull();
		expect(results.current.unexpected_stop).not.toBeNull();
		expect(results.current.auto_thinking?.accuracy).toBe(1);
		expect(results.current.auto_thinking?.costPer1000Usd).toBeCloseTo(0.2, 4);
		expect(results.current.auto_thinking?.p50LatencyMs).toBeCloseTo(42.5, 1);
		expect(results.current.unexpected_stop?.accuracy).toBe(1);
		expect(results.current.unexpected_stop?.precision).toBe(1);
		expect(results.current.unexpected_stop?.recall).toBe(1);
		expect(results.current.unexpected_stop?.costPer1000Usd).toBeCloseTo(0.3, 4);
		expect(results.current.unexpected_stop?.p50LatencyMs).toBeCloseTo(55.0, 1);

		// Verify rendered report contains numbers, not "not measured"
		const report = await Bun.file(outPath).text();
		expect(report).toContain("100.0%");
		expect(report).toContain("$0.2000");
		expect(report).toContain("$0.3000");
		expect(report).toContain("42.5");
		expect(report).toContain("55.0");

		const autoThinkingSection = report.slice(
			report.indexOf("### 1. Auto-thinking effort"),
			report.indexOf("### 2. Unexpected-stop"),
		);
		expect(autoThinkingSection).not.toContain("not measured");

		const unexpectedStopSection = report.slice(
			report.indexOf("### 2. Unexpected-stop"),
			report.indexOf("### 3. Robomp"),
		);
		expect(unexpectedStopSection).not.toContain("not measured");

		// Verify results.json was written and populated, and the report uses the
		// same current-side cost source the JSON records.
		const json = (await Bun.file(jsonPath).json()) as typeof results;
		expect(json.current.auto_thinking?.accuracy).toBe(1);
		expect(json.current.unexpected_stop?.accuracy).toBe(1);
		expect(json.current.auto_thinking?.costSource).toBe("provider-usage");
		expect(json.current.unexpected_stop?.costSource).toBe("provider-usage");
		expect(autoThinkingSection).toContain("provider-usage");
		expect(unexpectedStopSection).toContain("provider-usage");
		expect(autoThinkingSection).not.toContain("generation-record");
		expect(unexpectedStopSection).not.toContain("generation-record");
	});

	it("without --fake-current, constructs a real current harness via injected factory without reaching openrouter or network", async () => {
		const setsDir = path.join(tempDir.path(), "sets");
		const outPath = path.join(tempDir.path(), "report.md");
		await Bun.write(jevEnvPath(tempDir.path()), "OPENROUTER_API_KEY=sk-test-fake-key\n");

		await Bun.write(
			path.join(setsDir, "prompts.jsonl"),
			`${JSON.stringify({ prompt: "refactor parser", effort: "low" })}\n`,
		);
		await Bun.write(
			path.join(setsDir, "turn-ends.jsonl"),
			`${JSON.stringify({ text: "I will act now", label: "continue" })}\n`,
		);
		await Bun.write(path.join(setsDir, "issues.jsonl"), "");

		const fakeTransportPath = path.join(tempDir.path(), "fake-transport.ts");
		await Bun.write(
			fakeTransportPath,
			`export const fakeOpenRouter = async () => new Response(JSON.stringify({
	id: "gen-no-fake",
	model: "typesafe/jev-router",
	choices: [{ message: { content: "low" } }],
	usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
}), { status: 200, headers: { "Content-Type": "application/json" } });
`,
		);

		let factoryCalled = false;
		const mockModel: Model = getBundledModel("anthropic", "claude-sonnet-4-5")!;

		// jev.enabled is forced off by withoutJevSettings.
		const harnessSettings = Settings.isolated({ "jev.enabled": true });
		for (const role of ["smol", "tiny", "judge"]) harnessSettings.setModelRole(role, "mock/mock-model");
		const mockHarness: CurrentSmolHarness = {
			settings: harnessSettings,
			registry: {
				getAvailable: () => [mockModel],
				getApiKey: async () => "mock-key",
				resolver: () => async () => "mock-key",
			} as unknown as CurrentSmolHarness["registry"],
		};

		const results = await main(
			["--sets", setsDir, "--out", outPath, "--config-home", tempDir.path(), "--fake-transport", fakeTransportPath],
			{
				buildCurrentHarness: async () => {
					factoryCalled = true;
					return {
						settings: withoutJevSettings(mockHarness.settings),
						registry: mockHarness.registry,
					};
				},
			},
		);

		expect(factoryCalled).toBe(true);
		expect(results.current.auto_thinking).not.toBeNull();
		expect(results.current.unexpected_stop).not.toBeNull();
	});

	it("withoutJevSettings forces Jev decision flags off while preserving other settings", () => {
		const rawSettings = Settings.isolated({
			"jev.enabled": true,
			"jev.autoThinking": true,
			"jev.unexpectedStop": true,
			defaultThinkingLevel: "high",
		});

		const wrapped = withoutJevSettings(rawSettings);
		expect(cfgJevEnabled.get(wrapped)).toBe(false);
		expect(cfgJevAutoThinking.get(wrapped)).toBe(false);
		expect(cfgJevUnexpectedStop.get(wrapped)).toBe(false);
		expect(cfgDefaultThinkingLevel.get(wrapped)).toBe(Effort.High);
	});
});
