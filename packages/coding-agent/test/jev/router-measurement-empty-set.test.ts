import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import type { RouterMeasurementResults } from "../../../../docs/reports/jev-measurement/router-harness";
import { runRouter } from "../../../../docs/reports/jev-measurement/run-router";
import { FakeOpenRouterTransport } from "./fake-openrouter";

describe("Jev Router empty decision set measurement", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-jev-router-empty-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	it("renders 'not measured' in every rate, latency and cost row for empty robomp issues while non-empty routes render numeric cells", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: {
				text: "low",
				routedModel: "openai/gpt-5-mini",
				promptTokens: 100,
				completionTokens: 2,
				id: "gen-empty-test",
			},
			generations: {
				"gen-empty-test": {
					total_cost: 0.0002,
					model: "openai/gpt-5-mini",
					reasoning: "low",
				},
			},
		});

		const outPath = path.join(tempDir.path(), "report.md");
		const jsonPath = path.join(tempDir.path(), "results.json");

		const results = await runRouter({
			prompts: [{ prompt: "rename function old to new", effort: "low" }],
			turnEnds: [{ text: "Done with refactor.", label: "stop" }],
			issues: [],
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			generationRecords: true,
			outPath,
			jsonPath,
		});

		// 1. Verify JSON output
		const json = (await Bun.file(jsonPath).json()) as RouterMeasurementResults;
		expect(results).toEqual(json);
		expect(json.sampleSizes.robomp).toBe(0);
		expect(json.features.robomp_route.sampleSize).toBe(0);
		expect(json.features.robomp_route.accuracy).toBeNull();
		expect(json.features.robomp_route.answerRate).toBeNull();
		expect(json.features.robomp_route.p50LatencyMs).toBeNull();
		expect(json.features.robomp_route.p95LatencyMs).toBeNull();
		expect(json.features.robomp_route.costPer1000Usd).toBeNull();
		expect(json.features.robomp_route.costSource).toBe("unavailable");
		expect(json.features.robomp_route.unparseableRate).toBeNull();
		expect(json.features.robomp_route.truncatedRate).toBeNull();
		expect(json.features.robomp_route.transportFailureRate).toBeNull();
		expect(json.features.robomp_route.skipSessionShare).toBeNull();

		// Non-empty routes in JSON hold real numeric values
		expect(json.sampleSizes.auto_thinking).toBe(1);
		expect(json.features.auto_thinking_route.sampleSize).toBe(1);
		expect(json.features.auto_thinking_route.accuracy).toBe(1);
		expect(json.features.auto_thinking_route.answerRate).toBe(1);
		expect(typeof json.features.auto_thinking_route.p50LatencyMs).toBe("number");
		expect(typeof json.features.auto_thinking_route.costPer1000Usd).toBe("number");

		expect(json.sampleSizes.unexpected_stop).toBe(1);
		expect(json.features.unexpected_stop_route.sampleSize).toBe(1);
		expect(typeof json.features.unexpected_stop_route.accuracy).toBe("number");
		expect(typeof json.features.unexpected_stop_route.precision).toBe("number");
		expect(typeof json.features.unexpected_stop_route.recall).toBe("number");

		// 2. Verify Markdown report output
		const markdown = await Bun.file(outPath).text();
		expect(markdown).not.toContain("{{");

		const robompSectionIndex = markdown.indexOf("### 3. Robomp issue pre-gate");
		expect(robompSectionIndex).toBeGreaterThan(-1);
		const robompSection = markdown.slice(
			robompSectionIndex,
			markdown.indexOf("## What is not measured", robompSectionIndex),
		);

		// Every rate, latency and cost row in robomp shows "not measured"
		expect(robompSection).toContain("| Primary-label accuracy | not measured |");
		expect(robompSection).toContain("| Answered (named a label) | not measured |");
		expect(robompSection).toContain("| Skip-session share (invalid / question) | not measured |");
		expect(robompSection).toContain("| p50 Latency (ms) | not measured |");
		expect(robompSection).toContain("| p95 Latency (ms) | not measured |");
		expect(robompSection).toContain("| Cost per 1000 calls ($) | not measured |");
		expect(robompSection).toContain("| Unparseable / off-list | not measured |");
		expect(robompSection).toContain("| Truncated (no answer at token cap) | not measured |");
		expect(robompSection).toContain("| Transport-failure rate | not measured |");

		// Cost source and histogram cells keep their descriptive text
		expect(robompSection).toContain("| Cost source | unavailable |");
		expect(robompSection).toContain("Routed models picked: unavailable");
		expect(robompSection).toContain("Routed reasoning effort reported: unavailable");

		// Auto-thinking and unexpected-stop sections render numeric cells
		const autoThinkingSection = markdown.slice(
			markdown.indexOf("### 1. Auto-thinking effort"),
			markdown.indexOf("### 2. Unexpected-stop"),
		);
		expect(autoThinkingSection).toContain("| Accuracy | not measured | 100.0% |");
		expect(autoThinkingSection).toContain("| Answered (named a label) | — | 100.0% |");
		expect(autoThinkingSection).toContain("| Unparseable / off-list | — | 0.0% |");
		expect(autoThinkingSection).toContain("| Truncated (no answer at token cap) | — | 0.0% |");
		expect(autoThinkingSection).toContain("| Transport-failure rate | — | 0.0% |");
		expect(autoThinkingSection).toContain("$0.2000");

		const unexpectedStopSection = markdown.slice(markdown.indexOf("### 2. Unexpected-stop"), robompSectionIndex);
		expect(unexpectedStopSection).toContain("| Accuracy | not measured | 0.0% |");
		expect(unexpectedStopSection).toContain("| Precision (at the router's own decision) | not measured | 100.0% |");
		expect(unexpectedStopSection).toContain("| Recall (at the router's own decision) | not measured | 100.0% |");
		expect(unexpectedStopSection).toContain("| Answered (named a label) | — | 0.0% |");
		expect(unexpectedStopSection).toContain("| Unparseable / off-list | — | 100.0% |");
		expect(unexpectedStopSection).toContain("| Truncated (no answer at token cap) | — | 0.0% |");
		expect(unexpectedStopSection).toContain("| Transport-failure rate | — | 0.0% |");
	});

	it("renders 'not measured' when all auto-thinking prompt labels are excluded and router makes 0 calls", async () => {
		const transport = new FakeOpenRouterTransport();
		const outPath = path.join(tempDir.path(), "report-excluded.md");
		const jsonPath = path.join(tempDir.path(), "results-excluded.json");

		const results = await runRouter({
			prompts: [{ prompt: "test prompt", effort: "out-of-vocabulary-tier" }],
			turnEnds: [],
			issues: [],
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			outPath,
			jsonPath,
		});

		// 0 calls made to the router
		expect(transport.requests).toHaveLength(0);

		// JSON fields for auto_thinking_route are null
		expect(results.features.auto_thinking_route.sampleSize).toBe(0);
		expect(results.features.auto_thinking_route.accuracy).toBeNull();
		expect(results.features.auto_thinking_route.answerRate).toBeNull();
		expect(results.features.auto_thinking_route.p50LatencyMs).toBeNull();
		expect(results.features.auto_thinking_route.p95LatencyMs).toBeNull();
		expect(results.features.auto_thinking_route.costPer1000Usd).toBeNull();
		expect(results.features.auto_thinking_route.costSource).toBe("unavailable");
		expect(results.features.auto_thinking_route.unparseableRate).toBeNull();
		expect(results.features.auto_thinking_route.truncatedRate).toBeNull();
		expect(results.features.auto_thinking_route.transportFailureRate).toBeNull();
		expect(results.features.auto_thinking_route.excludedLabels).toEqual({ "out-of-vocabulary-tier": 1 });

		const markdown = await Bun.file(outPath).text();
		const autoThinkingSection = markdown.slice(
			markdown.indexOf("### 1. Auto-thinking effort"),
			markdown.indexOf("### 2. Unexpected-stop"),
		);

		expect(autoThinkingSection).toContain("| Accuracy | not measured | not measured |");
		expect(autoThinkingSection).toContain("| Answered (named a label) | — | not measured |");
		expect(autoThinkingSection).toContain("| p50 Latency (ms) | not measured | not measured |");
		expect(autoThinkingSection).toContain("| p95 Latency (ms) | not measured | not measured |");
		expect(autoThinkingSection).toContain("| Cost per 1000 calls ($) | not measured | not measured |");
		expect(autoThinkingSection).toContain("| Cost source | not measured | unavailable |");
		expect(autoThinkingSection).toContain("| Unparseable / off-list | — | not measured |");
		expect(autoThinkingSection).toContain("| Truncated (no answer at token cap) | — | not measured |");
		expect(autoThinkingSection).toContain("| Transport-failure rate | — | not measured |");
		expect(autoThinkingSection).toContain("out-of-vocabulary-tier x1");
	});
});
