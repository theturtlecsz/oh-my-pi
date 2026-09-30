import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as path from "node:path";
import { type FetchImpl, TempDir } from "@oh-my-pi/pi-utils";
import {
	measureRouterEffort,
	partitionEffortPrompts,
	runRouterMeasurement,
} from "../../../../docs/reports/jev-measurement/router-harness";
import { ROUTER_MAX_TOKENS, routeCompletion } from "../../../../docs/reports/jev-measurement/router-transport";
import { runRouter } from "../../../../docs/reports/jev-measurement/run-router";
import { FakeOpenRouterTransport } from "./fake-openrouter";

const API_KEY = "sk-or-test-key";

describe("Jev Router completion budget", () => {
	it("caps routed calls at ROUTER_MAX_TOKENS and reports a content-less length finish as truncated, not ok", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { truncate: true, id: "gen-trunc" } });
		const entries: unknown[] = [];
		const decision = await routeCompletion("classify", {
			apiKey: API_KEY,
			fetch: transport.fetch,
			recordUsage: entry => entries.push(entry),
		});

		expect(ROUTER_MAX_TOKENS).toBeGreaterThanOrEqual(1024);
		expect(transport.requests[0].body).toMatchObject({ max_tokens: ROUTER_MAX_TOKENS });
		expect(decision.text).toBeUndefined();
		const entry = entries[0] as { outcome: string; truncated: boolean };
		expect(entry.outcome).toBe("truncated");
		expect(entry.truncated).toBe(true);

		const ok = new FakeOpenRouterTransport({ reply: { text: "high" } });
		const okEntries: unknown[] = [];
		const okDecision = await routeCompletion("classify", {
			apiKey: API_KEY,
			fetch: ok.fetch,
			recordUsage: entry => okEntries.push(entry),
		});
		expect(okDecision.text).toBe("high");
		expect((okEntries[0] as { outcome: string }).outcome).toBe("ok");
		expect((okEntries[0] as { truncated: boolean }).truncated).toBe(false);
	});

	it("counts a truncated routed call separately from an unparseable one", async () => {
		let call = 0;
		const fetch: FetchImpl = async () => {
			call += 1;
			const truncate = call === 1;
			return Response.json({
				id: `gen-${call}`,
				model: "openai/gpt-5-mini",
				choices: [
					{
						message: { role: "assistant", content: truncate ? null : "low" },
						finish_reason: truncate ? "length" : "stop",
					},
				],
				usage: { prompt_tokens: 10, completion_tokens: truncate ? 32 : 1 },
			});
		};

		const metrics = await measureRouterEffort(
			[
				{ prompt: "a", effort: "low" },
				{ prompt: "b", effort: "low" },
			],
			{ apiKey: API_KEY, fetch, recordUsage: () => {} },
		);

		// One hit the token cap, the other answered. The cap is its own count,
		// so unparseable stays empty and the answered share is the real one.
		expect(metrics.sampleSize).toBe(2);
		expect(metrics.truncatedRate).toBe(0.5);
		expect(metrics.unparseableRate).toBe(0);
		expect(metrics.answerRate).toBe(0.5);
	});
});

describe("Jev Router auto-thinking label vocabulary", () => {
	it("aliases max/minimal onto scored labels and excludes an unknown label", () => {
		const { included, excluded } = partitionEffortPrompts([
			{ prompt: "a", effort: "max" },
			{ prompt: "b", effort: "minimal" },
			{ prompt: "c", effort: "HIGH" },
			{ prompt: "d", effort: "bogus" },
		]);

		expect(included.map(item => item.expected)).toEqual(["xhigh", "low", "high"]);
		expect(excluded).toEqual({ bogus: 1 });
	});

	it("scores an aliased max label on both sides and excludes the unwinnable item from both", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "xhigh" } });
		const results = await runRouterMeasurement({
			prompts: [
				{ prompt: "needs max", effort: "max" },
				{ prompt: "unknown tier", effort: "unheardof" },
			],
			apiKey: API_KEY,
			fetch: transport.fetch,
			// The current classifier names the tier `max`, which the alias
			// maps onto the scored `xhigh` the dataset item now expects.
			fakeCurrent: { classifyDifficulty: async () => ({ effort: "max" }) },
		});

		const route = results.features.auto_thinking_route;
		expect(route.sampleSize).toBe(1);
		expect(route.excludedLabels).toEqual({ unheardof: 1 });
		expect(route.accuracy).toBe(1);
		// The current side is scored over the same partition, so both columns
		// share a denominator.
		expect(results.current.auto_thinking?.sampleSize).toBe(1);
		expect(results.current.auto_thinking?.accuracy).toBe(1);
		// The excluded item was never sent to the router.
		expect(transport.requests).toHaveLength(1);
	});
});

describe("Jev Router report budget rows", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-jev-router-budget-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	it("renders the truncated and excluded rows without leaving placeholders", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "low" } });
		const outPath = path.join(tempDir.path(), "report.md");
		await runRouter({
			prompts: [
				{ prompt: "a", effort: "low" },
				{ prompt: "b", effort: "weird" },
			],
			apiKey: API_KEY,
			fetch: transport.fetch,
			outPath,
		});

		const report = await Bun.file(outPath).text();
		expect(report).not.toContain("{{");
		expect(report).toContain("Truncated (no answer at token cap)");
		expect(report).toContain("Excluded (label outside scored set)");
		expect(report).toContain("weird x1");
	});
});
