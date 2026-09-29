import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as path from "node:path";
import { type FetchImpl, TempDir } from "@oh-my-pi/pi-utils";
import {
	GENERATION_RECORD_CONCURRENCY,
	measureRouterEffort,
	type RouterPromptItem,
} from "../../../../docs/reports/jev-measurement/router-harness";
import { type RouterUsageEntry, routeCompletion } from "../../../../docs/reports/jev-measurement/router-transport";
import { runRouter } from "../../../../docs/reports/jev-measurement/run-router";

const API_KEY = "sk-or-test-key";

function requestUrl(input: Parameters<FetchImpl>[0]): string {
	return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

function prompts(count: number): RouterPromptItem[] {
	return Array.from({ length: count }, (_, index) => ({ prompt: `request ${index}`, effort: "low" }));
}

interface RecordProbeState {
	attempts: Map<string, number>;
	waitsMs: number[];
	maxCompletions: number;
	reportTouchedDuringRead: boolean;
}

/**
 * Completion answers at once. The generation record 404s twice, then waits
 * `delayMs` and answers 200. Each id's cost is `callIndex * 0.0001`.
 */
function delayedRecordFetch(options: { delayMs: number; outPath?: string; jsonPath?: string }): {
	fetch: FetchImpl;
	state: RecordProbeState;
} {
	const state: RecordProbeState = {
		attempts: new Map(),
		waitsMs: [],
		maxCompletions: 0,
		reportTouchedDuringRead: false,
	};
	let seq = 0;
	let completionsActive = 0;
	const readStartedMs = new Map<string, number>();
	const fetch: FetchImpl = async input => {
		const url = requestUrl(input);
		if (url.includes("/api/v1/generation")) {
			if (options.outPath && (await Bun.file(options.outPath).exists())) state.reportTouchedDuringRead = true;
			if (options.jsonPath && (await Bun.file(options.jsonPath).exists())) state.reportTouchedDuringRead = true;
			const id = new URL(url).searchParams.get("id") ?? "";
			const n = (state.attempts.get(id) ?? 0) + 1;
			state.attempts.set(id, n);
			if (!readStartedMs.has(id)) readStartedMs.set(id, performance.now());
			if (n <= 2) return new Response("not found", { status: 404 });
			await Bun.sleep(options.delayMs);
			state.waitsMs.push(performance.now() - (readStartedMs.get(id) ?? performance.now()));
			const call = Number(id.slice("gen-".length));
			return Response.json({
				data: { total_cost: call * 0.0001, model: "x-ai/grok-4", reasoning: "high" },
			});
		}
		completionsActive += 1;
		state.maxCompletions = Math.max(state.maxCompletions, completionsActive);
		await Bun.sleep(0);
		completionsActive -= 1;
		seq += 1;
		return Response.json({
			id: `gen-${seq}`,
			model: "openai/gpt-5-mini",
			choices: [{ message: { role: "assistant", content: "low" } }],
			usage: { prompt_tokens: 8, completion_tokens: 1, total_tokens: 9 },
		});
	};
	return { fetch, state };
}

describe("Jev Router generation records off the serial path", () => {
	const delayMs = 40;
	const retryDelayMs = 20;
	const callCount = 4;

	it("matches the inline record read, with wall time under the sum of the record waits", async () => {
		const inline = delayedRecordFetch({ delayMs });
		const inlineEntries: RouterUsageEntry[] = [];
		for (const item of prompts(callCount)) {
			await routeCompletion(item.prompt, {
				apiKey: API_KEY,
				fetch: inline.fetch,
				generationRecords: true,
				generationRecordRetryDelayMs: retryDelayMs,
				recordUsage: entry => inlineEntries.push(entry),
			});
		}

		const background = delayedRecordFetch({ delayMs });
		const harnessEntries: RouterUsageEntry[] = [];
		const started = performance.now();
		const metrics = await measureRouterEffort(prompts(callCount), {
			apiKey: API_KEY,
			fetch: background.fetch,
			generationRecords: true,
			generationRecordRetryDelayMs: retryDelayMs,
			feature: "router_auto_thinking",
			recordUsage: entry => harnessEntries.push(entry),
		});
		const wallMs = performance.now() - started;

		expect(harnessEntries).toHaveLength(inlineEntries.length);
		for (let i = 0; i < inlineEntries.length; i++) {
			expect(harnessEntries[i].costUsd).toBeCloseTo(inlineEntries[i].costUsd ?? Number.NaN, 8);
			expect(harnessEntries[i].costSource).toBe(inlineEntries[i].costSource);
			expect(harnessEntries[i].routedReasoningEffort).toBe(inlineEntries[i].routedReasoningEffort);
			expect(harnessEntries[i].routedModel).toBe(inlineEntries[i].routedModel);
		}
		for (const count of inline.state.attempts.values()) expect(count).toBe(3);
		for (const count of background.state.attempts.values()) expect(count).toBe(3);

		const measured = inlineEntries.filter(entry => entry.costUsd !== undefined);
		const mean = measured.reduce((sum, entry) => sum + (entry.costUsd ?? 0), 0) / measured.length;
		expect(metrics.costSource).toBe("generation-record");
		expect(metrics.costPer1000Usd).toBeCloseTo(mean * 1000, 6);
		expect(metrics.routedEffort).toEqual({ high: callCount });

		const waitSum = background.state.waitsMs.reduce((sum, wait) => sum + wait, 0);
		expect(background.state.waitsMs).toHaveLength(callCount);
		expect(wallMs).toBeLessThan(waitSum);
		expect(background.state.maxCompletions).toBe(1);

		const minWait = Math.min(...background.state.waitsMs);
		expect(metrics.p50LatencyMs).toBeLessThan(minWait);
		for (const entry of harnessEntries) expect(entry.latencyMs).toBeLessThan(minWait);
	});

	it("keeps at most the configured number of generation-record reads in flight", async () => {
		expect(GENERATION_RECORD_CONCURRENCY).toBe(8);
		const concurrency = 2;
		const calls = 6;
		const holdMs = 40;
		let completionsActive = 0;
		let maxCompletions = 0;
		let inFlight = 0;
		let maxInFlight = 0;
		let generationReads = 0;
		const holdsMs: number[] = [];
		let seq = 0;
		const fetch: FetchImpl = async input => {
			const url = requestUrl(input);
			if (url.includes("/api/v1/generation")) {
				generationReads += 1;
				inFlight += 1;
				maxInFlight = Math.max(maxInFlight, inFlight);
				const started = performance.now();
				await Bun.sleep(holdMs);
				holdsMs.push(performance.now() - started);
				inFlight -= 1;
				return Response.json({ data: { total_cost: 0.00042, model: "x-ai/grok-4", reasoning: "high" } });
			}
			completionsActive += 1;
			maxCompletions = Math.max(maxCompletions, completionsActive);
			await Bun.sleep(0);
			completionsActive -= 1;
			seq += 1;
			return Response.json({
				id: `gen-${seq}`,
				model: "openai/gpt-5-mini",
				choices: [{ message: { role: "assistant", content: "low" } }],
				usage: { prompt_tokens: 4, completion_tokens: 1, total_tokens: 5 },
			});
		};

		const started = performance.now();
		const metrics = await measureRouterEffort(prompts(calls), {
			apiKey: API_KEY,
			fetch,
			generationRecords: true,
			generationRecordConcurrency: concurrency,
			feature: "router_auto_thinking",
		});
		const wallMs = performance.now() - started;

		expect(calls).toBeGreaterThan(concurrency);
		expect(maxCompletions).toBe(1);
		expect(generationReads).toBe(calls);
		expect(maxInFlight).toBeLessThanOrEqual(concurrency);
		expect(maxInFlight).toBe(concurrency);
		const holdSum = holdsMs.reduce((sum, hold) => sum + hold, 0);
		expect(wallMs).toBeLessThan(holdSum);
		expect(metrics.costSource).toBe("generation-record");
		expect(metrics.costPer1000Usd).toBeCloseTo(0.42, 6);
		expect(metrics.routedEffort).toEqual({ high: calls });
	});

	describe("report written after the reads", () => {
		let tempDir: TempDir;

		beforeEach(() => {
			tempDir = TempDir.createSync("@pi-jev-router-records-");
		});

		afterEach(() => {
			tempDir.removeSync();
		});

		it("finishes every generation-record read before the report and results JSON exist", async () => {
			const outPath = path.join(tempDir.path(), "report.md");
			const jsonPath = path.join(tempDir.path(), "results.json");
			const probe = delayedRecordFetch({ delayMs, outPath, jsonPath });
			const results = await runRouter({
				prompts: prompts(callCount),
				apiKey: API_KEY,
				fetch: probe.fetch,
				generationRecords: true,
				generationRecordRetryDelayMs: retryDelayMs,
				outPath,
				jsonPath,
			});

			expect(probe.state.reportTouchedDuringRead).toBe(false);
			for (const count of probe.state.attempts.values()) expect(count).toBe(3);
			expect(probe.state.waitsMs).toHaveLength(callCount);
			expect(results.features.auto_thinking_route.costSource).toBe("generation-record");
			expect(results.features.auto_thinking_route.routedEffort).toEqual({ high: callCount });

			const report = await Bun.file(outPath).text();
			expect(report).toContain("generation-record");
			const json = (await Bun.file(jsonPath).json()) as {
				features: { auto_thinking_route: { costSource: string; routedEffort: Record<string, number> } };
			};
			expect(json.features.auto_thinking_route.costSource).toBe("generation-record");
			expect(json.features.auto_thinking_route.routedEffort).toEqual({ high: callCount });
		});
	});
});
