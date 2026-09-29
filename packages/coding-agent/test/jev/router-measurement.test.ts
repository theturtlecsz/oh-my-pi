import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import {
	measureRouterEffort,
	measureRouterRobomp,
	measureRouterStop,
	ROUTER_EFFORT_LABELS,
	RouterRouteError,
	runRouterMeasurement,
	WP5_ROUTER_DOES_NOT_APPLY,
} from "../../../../docs/reports/jev-measurement/router-harness";
import {
	fetchGenerationRecord,
	jevEnvPath,
	makeOpenRouterFetch,
	OPENROUTER_JEV_ROUTER_MODEL,
	parseFirstLabel,
	parseYesNo,
	ROUTER_ENV_DIR,
	ROUTER_ENV_FILE,
	RouterTransportError,
	type RouterUsageEntry,
	readJevEnvKey,
	routeCompletion,
} from "../../../../docs/reports/jev-measurement/router-transport";
import { keySourceLabel, runRouter } from "../../../../docs/reports/jev-measurement/run-router";
import { FakeOpenRouterTransport } from "./fake-openrouter";

const REPO_ROOT = path.resolve(import.meta.dir, "../../../..");

function deps(transport: FakeOpenRouterTransport, overrides: Partial<Parameters<typeof routeCompletion>[1]> = {}) {
	const entries: RouterUsageEntry[] = [];
	return {
		entries,
		deps: {
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			recordUsage: (entry: RouterUsageEntry) => entries.push(entry),
			...overrides,
		},
	};
}

describe("Jev Router transport", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-jev-router-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	it("reads the OpenRouter key from a config home and never from the repo", async () => {
		await Bun.write(jevEnvPath(tempDir.path()), `OPENROUTER_API_KEY=sk-or-from-file\n`);

		expect(jevEnvPath(tempDir.path())).toBe(path.join(tempDir.path(), ROUTER_ENV_DIR, ROUTER_ENV_FILE));
		expect(readJevEnvKey(tempDir.path())).toBe("sk-or-from-file");
		// An unconfigured home yields undefined rather than throwing or searching.
		expect(readJevEnvKey(path.join(tempDir.path(), "absent"))).toBeUndefined();
		expect(keySourceLabel(tempDir.path())).toContain(path.join(ROUTER_ENV_DIR, ROUTER_ENV_FILE));
	});

	it("refuses a request to any origin other than openrouter.ai", async () => {
		const guarded = makeOpenRouterFetch();
		await expect(guarded("https://api.typesafe.ai/v1/systemone", { method: "POST" })).rejects.toThrow(
			RouterTransportError,
		);
	});

	it("refuses to send a model other than the pinned router", async () => {
		const transport = new FakeOpenRouterTransport();
		const { deps: routeDeps } = deps(transport);
		await expect(routeCompletion("hi", { ...routeDeps, model: "openai/gpt-5" })).rejects.toThrow(
			RouterTransportError,
		);
		expect(transport.requests).toHaveLength(0);
	});

	it("requires a key at call time instead of falling back to any stored credential", async () => {
		const transport = new FakeOpenRouterTransport();
		const { deps: routeDeps } = deps(transport);
		await expect(routeCompletion("hi", { ...routeDeps, apiKey: undefined })).rejects.toThrow(RouterTransportError);
	});

	it("sends the pinned router to openrouter.ai and reads the routed model, tokens and text", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: {
				text: "high",
				routedModel: "google/gemini-3-pro",
				promptTokens: 200,
				completionTokens: 3,
				id: "gen-a",
			},
		});
		const { deps: routeDeps, entries } = deps(transport);
		const decision = await routeCompletion("classify this", routeDeps);

		expect(decision.text).toBe("high");
		expect(decision.routedModel).toBe("google/gemini-3-pro");
		expect(entries).toHaveLength(1);
		expect(entries[0].outcome).toBe("ok");
		expect(entries[0].promptTokens).toBe(200);
		expect(entries[0].routedModel).toBe("google/gemini-3-pro");

		expect(transport.requests).toHaveLength(1);
		const request = transport.requests[0];
		expect(new URL(request.url).origin).toBe("https://openrouter.ai");
		expect(request.authorization).toBe("Bearer sk-or-test-key");
		expect(request.body).toMatchObject({ model: OPENROUTER_JEV_ROUTER_MODEL });
	});

	it("reads the actual cost and routed reasoning from the OpenRouter generation record", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "medium", id: "gen-cost" },
			generations: { "gen-cost": { total_cost: 0.00042, model: "x-ai/grok-4", reasoning: "high" } },
		});
		const { deps: routeDeps, entries } = deps(transport, { generationRecords: true });
		await routeCompletion("classify", routeDeps);

		expect(transport.generationRequests).toEqual(["gen-cost"]);
		expect(entries[0].costUsd).toBeCloseTo(0.00042, 8);
		expect(entries[0].routedReasoningEffort).toBe("high");
		expect(entries[0].routedModel).toBe("x-ai/grok-4");
	});

	it("surfaces a 401 as an http_error entry with the status, and does not retry", async () => {
		const transport = new FakeOpenRouterTransport({ mode: "401" });
		const { deps: routeDeps, entries } = deps(transport);
		const decision = await routeCompletion("classify", routeDeps);

		expect(decision.text).toBeUndefined();
		expect(entries).toHaveLength(1);
		expect(entries[0].outcome).toBe("http_error");
		expect(entries[0].status).toBe(401);
	});

	it("retries a 500 inside the budget and reports the failure when it persists", async () => {
		const transport = new FakeOpenRouterTransport({ mode: "500" });
		const { deps: routeDeps, entries } = deps(transport);
		const decision = await routeCompletion("classify", routeDeps);

		expect(decision.text).toBeUndefined();
		expect(entries).toHaveLength(2);
		expect(entries.every(entry => entry.outcome === "http_error")).toBe(true);
	});

	it("parses only allowed labels and maps yes/no exactly", () => {
		expect(parseFirstLabel("The answer is HIGH.", ROUTER_EFFORT_LABELS)).toBe("high");
		expect(parseFirstLabel("xhigh", ROUTER_EFFORT_LABELS)).toBe("xhigh");
		expect(parseFirstLabel("I cannot decide", ROUTER_EFFORT_LABELS)).toBeUndefined();
		expect(parseFirstLabel("moderate", ROUTER_EFFORT_LABELS)).toBeUndefined();
		expect(parseYesNo("Yes")).toBe(true);
		expect(parseYesNo("no.")).toBe(false);
		expect(parseYesNo("maybe")).toBeUndefined();
	});

	it("returns undefined when a generation record is missing", async () => {
		const transport = new FakeOpenRouterTransport();
		const record = await fetchGenerationRecord("absent", { apiKey: "sk-or-test-key", fetch: transport.fetch });
		expect(record).toBeUndefined();
	});

	it("names the key source as a config path outside the repo", () => {
		expect(keySourceLabel("/tmp/home/.config")).toBe("/tmp/home/.config/omp/jev.env (omp/jev.env)");
		expect(keySourceLabel("/tmp/home/.config")).not.toContain(REPO_ROOT);
	});
});

describe("Jev Router harness per decision", () => {
	it("auto-thinking: scores routed effort labels and records the routed model and effort", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "medium", routedModel: "openai/gpt-5-mini", id: "gen-1" },
			generations: { "gen-1": { total_cost: 0.0001, reasoning: "medium" } },
		});
		const { deps: routeDeps } = deps(transport, { generationRecords: true });
		const metrics = await measureRouterEffort(
			[
				{ prompt: "rename a variable", effort: "low" },
				{ prompt: "fix a null deref", effort: "medium" },
			],
			{ ...routeDeps, feature: "router_auto_thinking" },
		);

		expect(metrics.sampleSize).toBe(2);
		expect(metrics.accuracy).toBe(0.5);
		expect(metrics.routedModels).toEqual({ "openai/gpt-5-mini": 2 });
		expect(metrics.routedEffort).toEqual({ medium: 2 });
		expect(metrics.costSource).toBe("generation-record");
		expect(metrics.costPer1000Usd).toBeCloseTo(0.1, 6);
	});

	it("auto-thinking: counts an off-list routed word as unparseable, not as a wrong answer", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "moderate" } });
		const { deps: routeDeps } = deps(transport);
		const metrics = await measureRouterEffort([{ prompt: "fix this", effort: "low" }], routeDeps);

		expect(metrics.unparseableRate).toBe(1);
		expect(metrics.answerRate).toBe(0);
		expect(metrics.accuracy).toBe(0);
	});

	it("unexpected-stop: reads a routed yes as the continue label and reports precision/recall at the router's own decision", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "yes" } });
		const { deps: routeDeps } = deps(transport);
		const allYes = await measureRouterStop(
			[
				{ text: "I will now fix it", label: "continue" },
				{ text: "I will now fix it", label: "stop" },
			],
			routeDeps,
		);
		expect(allYes.precision).toBe(0.5);
		expect(allYes.recall).toBe(1);

		transport.setReply({ text: "no" });
		const { deps: noDeps } = deps(transport);
		const allNo = await measureRouterStop(
			[
				{ text: "I will now fix it", label: "continue" },
				{ text: "Done, anything else?", label: "stop" },
			],
			noDeps,
		);
		expect(allNo.precision).toBe(1);
		expect(allNo.recall).toBe(0);
	});

	it("robomp: scores the primary label and derives the skip share from invalid/question", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "bug" } });
		const { deps: routeDeps } = deps(transport);
		const bugMetrics = await measureRouterRobomp(
			[{ key: "o/r#1", repo: "o/r", number: 1, title: "crash", body: "", label: "bug" }],
			routeDeps,
		);
		expect(bugMetrics.accuracy).toBe(1);
		expect(bugMetrics.skipSessionShare).toBe(0);

		transport.setReply({ text: "question" });
		const { deps: questionDeps } = deps(transport);
		const questionMetrics = await measureRouterRobomp(
			[
				{ key: "o/r#2", repo: "o/r", number: 2, title: "how?", body: "", label: "question" },
				{ key: "o/r#3", repo: "o/r", number: 3, title: "crash", body: "", label: "bug" },
			],
			questionDeps,
		);
		expect(questionMetrics.accuracy).toBe(0.5);
		expect(questionMetrics.skipSessionShare).toBe(1);
	});

	it("aborts a route on 401 and on more than 5% transport failures", async () => {
		const transport401 = new FakeOpenRouterTransport({ mode: "401" });
		const { deps: authDeps } = deps(transport401);
		await expect(measureRouterEffort([{ prompt: "x", effort: "low" }], authDeps)).rejects.toThrow(RouterRouteError);

		const transport500 = new FakeOpenRouterTransport({ mode: "500" });
		const { deps: failureDeps } = deps(transport500);
		await expect(
			measureRouterEffort(
				[
					{ prompt: "a", effort: "low" },
					{ prompt: "b", effort: "low" },
				],
				failureDeps,
			),
		).rejects.toThrow(/transport failures/);
	});

	it("keeps the WP5 verdict as does-not-apply for a router measurement", async () => {
		const transport = new FakeOpenRouterTransport();
		const { deps: routeDeps } = deps(transport);
		const results = await runRouterMeasurement({
			prompts: [{ prompt: "rename x", effort: "low" }],
			apiKey: routeDeps.apiKey,
			fetch: transport.fetch,
		});

		expect(results.verdict).toBe(WP5_ROUTER_DOES_NOT_APPLY);
		expect(results.verdict).toContain("WP5 does not apply");
		expect(results.routerModel).toBe(OPENROUTER_JEV_ROUTER_MODEL);
		// The current side was not measured, so its columns are explicitly absent.
		expect(results.current.auto_thinking).toBeNull();
	});

	it("makes no request to any host other than openrouter.ai across the whole run", async () => {
		const transport = new FakeOpenRouterTransport();
		const { deps: routeDeps } = deps(transport);
		await runRouterMeasurement({
			prompts: [{ prompt: "rename x", effort: "low" }],
			turnEnds: [{ text: "I will act now", label: "continue" }],
			issues: [{ key: "o/r#1", repo: "o/r", number: 1, title: "crash", body: "", label: "bug" }],
			apiKey: routeDeps.apiKey,
			fetch: transport.fetch,
		});

		expect(transport.requests.length).toBeGreaterThan(0);
		for (const request of transport.requests) {
			expect(new URL(request.url).origin).toBe("https://openrouter.ai");
		}
	});
});

describe("Jev Router report runner", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-jev-router-run-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	it("renders the router method, real numbers for what is measurable, and the WP5 decision with no placeholders left", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "low", routedModel: "openai/gpt-5-mini", promptTokens: 300, completionTokens: 2, id: "gen-r" },
			generations: { "gen-r": { total_cost: 0.0003, reasoning: "low" } },
		});
		const outPath = path.join(tempDir.path(), "router-measurement-report.md");
		const results = await runRouter({
			prompts: [
				{ prompt: "rename x", effort: "low" },
				{ prompt: "fix y", effort: "low" },
			],
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			generationRecords: true,
			outPath,
		});

		expect(results.features.auto_thinking_route.accuracy).toBe(1);
		expect(results.features.auto_thinking_route.costPer1000Usd).toBeCloseTo(0.3, 6);

		const report = await Bun.file(outPath).text();
		expect(report).not.toContain("{{");
		expect(report).toContain("Jev Router");
		expect(report).toContain(OPENROUTER_JEV_ROUTER_MODEL);
		expect(report).toContain("100.0%");
		expect(report).toContain("$0.3000");
		expect(report).toContain("openai/gpt-5-mini x2");
		expect(report).toContain("low x2");
		expect(report).toContain("generation-record");
		expect(report).toContain(WP5_ROUTER_DOES_NOT_APPLY);
		// The report names what is not measured rather than leaving the reader to infer it.
		expect(report).toContain("What is not measured");
		expect(report).toContain("calibration");
	});

	it("loads the OMP-298 sets from disk and writes results.json", async () => {
		await Bun.write(
			path.join(tempDir.path(), "prompts.jsonl"),
			`${JSON.stringify({ prompt: "rename x", effort: "low" })}\n`,
		);
		await Bun.write(
			path.join(tempDir.path(), "turn-ends.jsonl"),
			`${JSON.stringify({ text: "I will act now", label: "continue" })}\n`,
		);
		await Bun.write(path.join(tempDir.path(), "issues.jsonl"), "");

		const transport = new FakeOpenRouterTransport({ reply: { text: "low" } });
		const outPath = path.join(tempDir.path(), "report.md");
		const jsonPath = path.join(tempDir.path(), "results.json");
		await runRouter({
			setsDir: tempDir.path(),
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			outPath,
			jsonPath,
		});

		const results = (await Bun.file(jsonPath).json()) as { sampleSizes: Record<string, number> };
		expect(results.sampleSizes).toEqual({ auto_thinking: 1, unexpected_stop: 1, robomp: 0 });
		expect(await Bun.file(outPath).text()).toContain(OPENROUTER_JEV_ROUTER_MODEL);
	});

	it("takes the cost from a completion that reports usage.cost, without pricing tokens", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "low", id: "gen-completion-cost", cost: 0.00038 },
			// A record that reports a different cost: the completion's own charged
			// cost wins, and the record only supplies the reasoning metadata.
			generations: { "gen-completion-cost": { total_cost: 0.0009, reasoning: "high" } },
		});
		const { deps: routeDeps, entries } = deps(transport, {
			generationRecords: true,
			generationRecordWaitMs: 15_000,
			generationRecordRetryDelayMs: 10,
			sleep: async () => {},
		});
		await routeCompletion("classify", routeDeps);

		expect(entries[0].costUsd).toBeCloseTo(0.00038, 8);
		expect(entries[0].costSource).toBe("completion-usage");
		expect(entries[0].routedReasoningEffort).toBe("high");

		const harnessTransport = new FakeOpenRouterTransport({ reply: { text: "low", cost: 0.00038 } });
		const { deps: harnessDeps } = deps(harnessTransport);
		const metrics = await measureRouterEffort([{ prompt: "rename x", effort: "low" }], harnessDeps);
		expect(metrics.costSource).toBe("completion-usage");
		expect(metrics.costPer1000Usd).toBeCloseTo(0.38, 6);
	});

	it("falls back to cost_details.upstream_inference_cost when the completion reports no usage.cost", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "low", upstreamInferenceCost: 0.00021 },
		});
		const { deps: routeDeps, entries } = deps(transport);
		await routeCompletion("classify", routeDeps);

		expect(entries[0].costUsd).toBeCloseTo(0.00021, 8);
		expect(entries[0].costSource).toBe("completion-usage");
	});

	it("polls a generation record that 404s twice before it appears, then reads its cost and reasoning", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "medium", id: "gen-late" },
			generations: { "gen-late": { total_cost: 0.0005, model: "x-ai/grok-4", reasoning: "high" } },
			generation404Count: 2,
		});
		const { deps: routeDeps, entries } = deps(transport, {
			generationRecords: true,
			generationRecordWaitMs: 15_000,
			generationRecordRetryDelayMs: 10,
			sleep: async () => {},
		});
		await routeCompletion("classify", routeDeps);

		// Two 404s prove the reader backed off instead of giving up on the first read.
		expect(transport.generationRequests).toEqual(["gen-late", "gen-late", "gen-late"]);
		expect(entries[0].costUsd).toBeCloseTo(0.0005, 8);
		expect(entries[0].costSource).toBe("generation-record");
		expect(entries[0].routedReasoningEffort).toBe("high");
		expect(entries[0].routedModel).toBe("x-ai/grok-4");
	});

	it("gives up on a generation record after the bounded wait and reports no cost", async () => {
		const transport = new FakeOpenRouterTransport({
			reply: { text: "low", id: "gen-never" },
			generation404Count: 1000,
		});
		const { deps: routeDeps, entries } = deps(transport, {
			generationRecords: true,
			generationRecordWaitMs: 5,
			generationRecordRetryDelayMs: 1,
			sleep: async () => {},
		});
		const metrics = await measureRouterEffort([{ prompt: "rename x", effort: "low" }], routeDeps);

		expect(entries[0].costUsd).toBeUndefined();
		expect(entries[0].costSource).toBeUndefined();
		expect(metrics.costPer1000Usd).toBeNull();
		expect(metrics.costSource).toBe("unavailable");
		// A bounded number of reads, not an unbounded poll.
		expect(transport.generationRequests.length).toBeLessThan(20);
	});

	it("never reports a price it did not read: a costless run renders 'not measured', not zero", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "low", routedModel: "openai/gpt-5-mini" } });
		const outPath = path.join(tempDir.path(), "costless-report.md");
		const results = await runRouter({
			prompts: [{ prompt: "rename x", effort: "low" }],
			turnEnds: [{ text: "I will act now", label: "continue" }],
			apiKey: "sk-or-test-key",
			fetch: transport.fetch,
			outPath,
		});

		expect(results.features.auto_thinking_route.costPer1000Usd).toBeNull();
		expect(results.features.auto_thinking_route.costSource).toBe("unavailable");
		const report = await Bun.file(outPath).text();
		expect(report).toContain("| Cost per 1000 calls ($) | not measured | not measured |");
		expect(report).not.toContain("$0.0000");
	});
});

describe("Jev Router scoring form", () => {
	it("scores a router answer and a current-side answer equal to the dataset label as correct", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "high" } });
		const { deps: routeDeps } = deps(transport);
		const results = await runRouterMeasurement({
			prompts: [{ prompt: "rename x", effort: "HIGH" }],
			apiKey: routeDeps.apiKey,
			fetch: transport.fetch,
			// The current side answers the same label in a different case; both
			// sides normalize case before comparing, so neither is miscounted.
			fakeCurrent: { classifyDifficulty: async () => ({ effort: "High" }) },
		});

		expect(results.features.auto_thinking_route.accuracy).toBe(1);
		expect(results.current.auto_thinking?.accuracy).toBe(1);
	});

	it("scores a router answer that names a different label as incorrect, not unparseable", async () => {
		const transport = new FakeOpenRouterTransport({ reply: { text: "low" } });
		const { deps: routeDeps } = deps(transport);
		const results = await runRouterMeasurement({
			prompts: [{ prompt: "rename x", effort: "high" }],
			apiKey: routeDeps.apiKey,
			fetch: transport.fetch,
		});

		expect(results.features.auto_thinking_route.accuracy).toBe(0);
		expect(results.features.auto_thinking_route.answerRate).toBe(1);
		expect(results.features.auto_thinking_route.unparseableRate).toBe(0);
	});
});
