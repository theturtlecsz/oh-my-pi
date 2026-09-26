import { Database } from "bun:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { unregisterCustomApis } from "@oh-my-pi/pi-ai/api-registry";
import { createMockModel, registerMockApi } from "@oh-my-pi/pi-ai/providers/mock";
import { TempDir } from "@oh-my-pi/pi-utils";
import {
	buildIssuesSet,
	extractPromptsFromSession,
	extractTurnEndsFromSession,
} from "../../../../docs/reports/jev-measurement/build-sets";
import {
	type CurrentSmolHarness,
	type FakeSmolHandler,
	type MeasurementResults,
	verdict,
	WP5_AMENDMENT,
	WP5_VERDICT_WITHHELD,
} from "../../../../docs/reports/jev-measurement/harness";
import { renderReport, run } from "../../../../docs/reports/jev-measurement/run";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

const ROBOMP_NOT_MEASURED = "not measured: no robomp history on the measuring machine";
const REPO_ROOT = path.resolve(import.meta.dir, "../../../..");

function wp5VerdictLines(markdown: string): string[] {
	return markdown
		.split("\n")
		.map(line => line.trim())
		.filter(line => line === WP5_AMENDMENT || line.startsWith("WP5 unchanged:"));
}

function robompValueCells(markdown: string): string[] {
	const start = markdown.indexOf("### 3. Robomp Issue Triage Prefilter");
	const end = markdown.indexOf("## Verdict");
	const section = markdown.slice(start, end);
	const cells: string[] = [];
	for (const line of section.split("\n")) {
		if (!line.startsWith("|") || line.includes("---") || line.includes("Metric")) continue;
		const parts = line
			.split("|")
			.slice(1, -1)
			.map(cell => cell.trim());
		cells.push(...parts.slice(1));
	}
	return cells;
}

describe("Jev measurement harness", () => {
	let tempDir: TempDir;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-measurement-harness-");
	});

	afterEach(() => {
		tempDir.removeSync();
	});

	describe("dataset extraction (build-sets)", () => {
		it("extracts prompts and overrides with manual thinking level change", async () => {
			const entries = [
				// Prompt 1: auto effort medium
				{
					type: "message",
					message: {
						role: "user",
						content: [{ type: "text", text: "Fix bug in parser" }],
					},
				},
				{
					type: "thinking_level_change",
					thinkingLevel: "medium",
					configured: "auto",
				},
				{
					type: "message",
					message: {
						role: "assistant",
						content: [{ type: "text", text: "I will fix it." }],
						stopReason: "stop",
					},
				},
				// Prompt 2: auto effort low, then manual change to high before prompt 3
				{
					type: "message",
					message: {
						role: "user",
						content: [{ type: "text", text: "Refactor database queries" }],
					},
				},
				{
					type: "thinking_level_change",
					thinkingLevel: "low",
					configured: "auto",
				},
				{
					type: "thinking_level_change",
					thinkingLevel: "high",
					configured: "high",
				},
				// Prompt 3: no auto thinking (manual high from before), should not be emitted
				{
					type: "message",
					message: {
						role: "user",
						content: "Just a comment",
					},
				},
			];

			const prompts = await extractPromptsFromSession(entries);
			expect(prompts).toHaveLength(2);
			expect(prompts[0].prompt).toBe("Fix bug in parser");
			expect(prompts[0].effort).toBe("medium");
			expect(prompts[1].prompt).toBe("Refactor database queries");
			expect(prompts[1].effort).toBe("high");
		});

		it("extracts turn ends and labels continue vs stop based on follow-up", async () => {
			const entries = [
				// Candidate 1: followed by retry reminder -> continue
				{
					type: "message",
					message: {
						role: "assistant",
						content: [{ type: "text", text: "I will execute the tool now." }],
						stopReason: "stop",
					},
				},
				{
					type: "message",
					message: {
						role: "developer",
						content: [
							{
								type: "text",
								text: "<system-injection>\nYou said you would continue with a tool call or action but stopped. Continue now.\nAttempt #1/3\n</system-injection>",
							},
						],
					},
				},
				// Candidate 2: followed by user "proceed" -> continue
				{
					type: "message",
					message: {
						role: "assistant",
						content: [{ type: "text", text: "Working on the next part..." }],
						stopReason: "stop",
					},
				},
				{
					type: "message",
					message: {
						role: "user",
						content: "proceed please",
					},
				},
				// Candidate 3: followed by normal user prompt -> stop
				{
					type: "message",
					message: {
						role: "assistant",
						content: [{ type: "text", text: "All tests are passing." }],
						stopReason: "stop",
					},
				},
				{
					type: "message",
					message: {
						role: "user",
						content: "Now let's work on feature B",
					},
				},
			];

			const turnEnds = await extractTurnEndsFromSession(entries);
			expect(turnEnds).toHaveLength(3);
			expect(turnEnds[0].label).toBe("continue");
			expect(turnEnds[1].label).toBe("continue");
			expect(turnEnds[2].label).toBe("stop");
		});

		it("extracts issues with exactly one primary label from robomp sqlite db", async () => {
			const dbFile = path.join(tempDir.path(), "robomp.sqlite");
			const db = new Database(dbFile);
			db.run(`
				CREATE TABLE issue_index (
					repo TEXT NOT NULL,
					number INTEGER NOT NULL,
					title TEXT NOT NULL,
					body TEXT NOT NULL,
					labels_json TEXT NOT NULL,
					PRIMARY KEY (repo, number)
				);
			`);

			// Row 1: single primary label "bug" -> included
			db.run("INSERT INTO issue_index VALUES (?, ?, ?, ?, ?)", [
				"owner/repo",
				1,
				"App crash",
				"Crash trace",
				JSON.stringify(["bug", "prio:p1"]),
			]);
			// Row 2: single primary label "question" -> included
			db.run("INSERT INTO issue_index VALUES (?, ?, ?, ?, ?)", [
				"owner/repo",
				2,
				"How to configure?",
				"Need help",
				JSON.stringify(["question"]),
			]);
			// Row 3: multiple primary labels -> excluded
			db.run("INSERT INTO issue_index VALUES (?, ?, ?, ?, ?)", [
				"owner/repo",
				3,
				"Confused issue",
				"Details",
				JSON.stringify(["bug", "question"]),
			]);
			// Row 4: no primary labels -> excluded
			db.run("INSERT INTO issue_index VALUES (?, ?, ?, ?, ?)", [
				"owner/repo",
				4,
				"Only prio",
				"Details",
				JSON.stringify(["prio:p2"]),
			]);
			db.close();

			const issues = await buildIssuesSet(dbFile);
			expect(issues).toHaveLength(2);
			expect(issues[0].key).toBe("owner/repo#1");
			expect(issues[0].label).toBe("bug");
			expect(issues[1].key).toBe("owner/repo#2");
			expect(issues[1].label).toBe("question");
		});

		it("build-sets --no-robomp writes prompts, turn ends, and an empty issues set", async () => {
			const sessionsDir = path.join(tempDir.path(), "sessions");
			const outDir = path.join(tempDir.path(), "sets");
			await fs.mkdir(sessionsDir, { recursive: true });
			const session = [
				{
					type: "message",
					message: { role: "user", content: [{ type: "text", text: "Fix bug in parser" }] },
				},
				{ type: "thinking_level_change", thinkingLevel: "medium", configured: "auto" },
				{
					type: "message",
					message: {
						role: "assistant",
						content: [{ type: "text", text: "I will fix it." }],
						stopReason: "stop",
					},
				},
			];
			await Bun.write(
				path.join(sessionsDir, "session.jsonl"),
				`${session.map(entry => JSON.stringify(entry)).join("\n")}\n`,
			);

			const script = path.join(REPO_ROOT, "docs/reports/jev-measurement/build-sets.ts");
			const proc = Bun.spawn(["bun", script, "--sessions", sessionsDir, "--no-robomp", "--out", outDir], {
				cwd: REPO_ROOT,
				stdout: "pipe",
				stderr: "pipe",
			});
			const [stdout, stderr, code] = await Promise.all([
				new Response(proc.stdout).text(),
				new Response(proc.stderr).text(),
				proc.exited,
			]);
			expect({ code, stdout, stderr }).toMatchObject({ code: 0 });

			const prompts = (await Bun.file(path.join(outDir, "prompts.jsonl")).text()).trim().split("\n");
			const turnEnds = (await Bun.file(path.join(outDir, "turn-ends.jsonl")).text()).trim().split("\n");
			const issuesText = await Bun.file(path.join(outDir, "issues.jsonl")).text();
			expect(prompts).toHaveLength(1);
			expect(JSON.parse(prompts[0]).effort).toBe("medium");
			expect(turnEnds).toHaveLength(1);
			expect(JSON.parse(turnEnds[0]).label).toBe("stop");
			expect(issuesText).toBe("");
		}, 30_000);
	});

	describe("verdict logic branches", () => {
		const basePassingResults: MeasurementResults = {
			features: {
				auto_thinking: {
					current: {
						accuracy: 0.85,
						p50LatencyMs: 1500,
						p95LatencyMs: 3000,
						costPer1000: 0.5,
						unparseableRate: 0.01,
						offListRate: 0.0,
					},
					jev: {
						accuracy: 0.9, // >= current
						p50LatencyMs: 100, // <= 1/10
						p95LatencyMs: 250, // <= 1/10
						costPer1000: 0.008, // <= 1/10
						unparseableRate: 0.0,
						offListRate: 0.0,
					},
				},
				unexpected_stop: {
					current: {
						accuracy: 0.8,
						precision: 0.75,
						recall: 0.85,
						p50LatencyMs: 1200,
						p95LatencyMs: 2500,
						costPer1000: 0.3,
						unparseableRate: 0.0,
						offListRate: 0.0,
					},
					jev: {
						accuracy: 0.85, // >= current
						precision: 0.88,
						recall: 0.9,
						p50LatencyMs: 80, // <= 1/10
						p95LatencyMs: 180, // <= 1/10
						costPer1000: 0.005, // <= 1/10
						unparseableRate: 0.0,
						offListRate: 0.0,
					},
				},
				robomp: {
					current: {
						accuracy: 1.0,
						p50LatencyMs: 35000,
						p95LatencyMs: 65000,
						costPer1000: 500.0,
						unparseableRate: 0.0,
						offListRate: 0.0,
					},
					jev: {
						accuracy: 0.98,
						confidentBucketAccuracy: 0.96, // >= 95%
						skipSessionShare: 0.3,
						p50LatencyMs: 120, // <= 1/10
						p95LatencyMs: 280, // <= 1/10
						costPer1000: 0.008, // <= 1/10
						unparseableRate: 0.0,
						offListRate: 0.0,
					},
				},
			},
			robompSessionFlagsMissing: false,
		};

		it("branch: amend when all criteria are satisfied", () => {
			const res = verdict(basePassingResults);
			expect(res).toBe(WP5_AMENDMENT);
		});

		it("branch: accuracy short when Jev accuracy drops below current", () => {
			const results: MeasurementResults = structuredClone(basePassingResults);
			results.features.auto_thinking.jev.accuracy = 0.7; // below 0.85
			const res = verdict(results);
			expect(res).toContain("WP5 unchanged:");
			expect(res).toContain("accuracy short");
		});

		it("branch: accuracy short when robomp confident bucket is below 95%", () => {
			const results: MeasurementResults = structuredClone(basePassingResults);
			results.features.robomp.jev.confidentBucketAccuracy = 0.92; // below 0.95
			const res = verdict(results);
			expect(res).toContain("WP5 unchanged:");
			expect(res).toContain("accuracy short");
		});

		it("branch: cost <10x when Jev cost is not at least 10x cheaper", () => {
			const results: MeasurementResults = structuredClone(basePassingResults);
			// Current: 0.50, 1/10 is 0.05. Set Jev to 0.10 (only 5x cheaper)
			results.features.auto_thinking.jev.costPer1000 = 0.1;
			const res = verdict(results);
			expect(res).toContain("WP5 unchanged:");
			expect(res).toContain("cost <10x");
		});

		it("branch: unknown robomp cost when robomp session flags are missing", () => {
			const results: MeasurementResults = structuredClone(basePassingResults);
			results.robompSessionFlagsMissing = true;
			const res = verdict(results);
			expect(res).toContain("WP5 unchanged:");
			expect(res).toContain("unknown robomp cost");
		});

		it("branch: null robomp fields stay out of the verdict", async () => {
			const results: MeasurementResults = structuredClone(basePassingResults);
			results.features.robomp = {
				current: {
					accuracy: null,
					p50LatencyMs: null,
					p95LatencyMs: null,
					costPer1000: null,
					unparseableRate: null,
					offListRate: null,
				},
				jev: {
					accuracy: null,
					p50LatencyMs: null,
					p95LatencyMs: null,
					costPer1000: null,
					unparseableRate: null,
					offListRate: null,
					confidentBucketAccuracy: null,
					skipSessionShare: null,
				},
			};
			results.robompSessionFlagsMissing = null;
			expect(verdict(results)).toBe(WP5_AMENDMENT);

			results.features.auto_thinking.jev.accuracy = 0.1;
			expect(verdict(results)).toBe("WP5 unchanged: accuracy short");

			const template = await Bun.file(
				path.join(REPO_ROOT, "docs/reports/jev-measurement/report-template.md"),
			).text();
			const measured = renderReport({ ...basePassingResults, verdict: WP5_AMENDMENT }, template);
			expect(measured).toContain("| Confident-Bucket Accuracy | - | 96.0% |");
			expect(measured).toContain("| Skip-Session Share | 0.0% | 30.0% |");
			expect(measured).toContain("| Overall Accuracy | 100.0% | 98.0% |");
			expect(measured).not.toContain(ROBOMP_NOT_MEASURED);
			expect(measured).not.toContain("{{");
			expect(wp5VerdictLines(measured)).toEqual([WP5_AMENDMENT]);
		});
	});

	describe("end-to-end execution: run.ts vs stub-jev-server and injected fake smol", () => {
		let stub: StubJevServer;

		beforeEach(() => {
			stub = startStubJevServer({
				answers: {
					difficulty: {
						probabilities: {
							medium: 0.9,
							low: 0.05,
							high: 0.03,
							xhigh: 0.02,
						},
					},
					no_repro: { probability: 0.1 },
					irreversible: { probability: 0.1 },
					live_cutover: { probability: 0.1 },
					statement: {
						probability: 0.85,
					},
					unexpected_stop: {
						probability: 0.85,
					},
					primary_type: {
						probabilities: {
							invalid: 0.96,
							bug: 0.04,
						},
					},
				},
			});
		});

		afterEach(() => {
			stub.stop();
		});

		it("runs measurement and writes results.json and jev-measurement-report.md with verdict", async () => {
			const setsDir = path.join(tempDir.path(), "sets");
			const outDir = path.join(tempDir.path(), "out");
			await fs.mkdir(setsDir, { recursive: true });

			// Write sample dataset files
			await Bun.write(
				path.join(setsDir, "prompts.jsonl"),
				`${JSON.stringify({ prompt: "Refactor user authentication service", effort: "medium" })}\n`,
			);
			await Bun.write(
				path.join(setsDir, "turn-ends.jsonl"),
				`${JSON.stringify({ text: "I will now edit the file.", label: "continue" })}\n`,
			);
			await Bun.write(
				path.join(setsDir, "issues.jsonl"),
				`${JSON.stringify({
					key: "test/repo#1",
					repo: "test/repo",
					number: 1,
					title: "Spam issue",
					body: "Spam content",
					label: "invalid",
				})}\n`,
			);

			const fakeSmol: FakeSmolHandler = {
				classifyDifficulty: async () => ({
					effort: "medium",
					cost: 0.0005,
					latencyMs: 1200,
				}),
				classifyUnexpectedStop: async () => ({
					unexpectedStop: true,
					cost: 0.0003,
					latencyMs: 900,
				}),
			};

			const mockRobompRunner = async () => ({
				accuracy: 1.0,
				confidentBucketAccuracy: 0.98,
				skipSessionShare: 0.25,
				p50LatencyMs: 150,
				p95LatencyMs: 300,
				costPer1000: 0.005,
				unparseableRate: 0.0,
				offListRate: 0.0,
			});

			const results = await run({
				setsDir,
				outDir,
				robompSessionCostUsd: 0.5,
				robompSessionP50Ms: 30000,
				robompSessionP95Ms: 60000,
				jevBaseUrl: stub.baseUrl,
				fakeSmol,
				robompRunner: mockRobompRunner,
			});

			expect(results.verdict).toBe(WP5_AMENDMENT);

			const resultsFile = path.join(outDir, "results.json");
			const reportFile = path.join(outDir, "jev-measurement-report.md");

			expect(await Bun.file(resultsFile).exists()).toBe(true);
			expect(await Bun.file(reportFile).exists()).toBe(true);

			const resultsJson = await Bun.file(resultsFile).json();
			expect(resultsJson.verdict).toBe(WP5_AMENDMENT);
			expect(resultsJson.features.auto_thinking.jev.accuracy).toBe(1.0);
			expect(resultsJson.features.unexpected_stop.jev.accuracy).toBe(1.0);

			const reportMarkdown = await Bun.file(reportFile).text();
			expect(reportMarkdown).toContain("Auto-thinking Difficulty Classification");
			expect(reportMarkdown).toContain("Unexpected-Stop Detection");
			expect(reportMarkdown).toContain("Robomp Issue Triage Prefilter");
			expect(reportMarkdown).toContain("Verdict");
			expect(reportMarkdown).toContain(WP5_AMENDMENT);
		});

		it("empty issues.jsonl leaves robomp unmeasured and keeps one WP5 verdict", async () => {
			const setsDir = path.join(tempDir.path(), "sets-empty-issues");
			const outDir = path.join(tempDir.path(), "out-empty-issues");
			await fs.mkdir(setsDir, { recursive: true });

			await Bun.write(
				path.join(setsDir, "prompts.jsonl"),
				`${JSON.stringify({ prompt: "Refactor user authentication service", effort: "medium" })}\n`,
			);
			await Bun.write(
				path.join(setsDir, "turn-ends.jsonl"),
				`${JSON.stringify({ text: "I will now edit the file.", label: "continue" })}\n`,
			);
			await Bun.write(path.join(setsDir, "issues.jsonl"), "");

			const fakeSmol: FakeSmolHandler = {
				classifyDifficulty: async () => ({
					effort: "medium",
					cost: 0.0005,
					latencyMs: 1200,
				}),
				classifyUnexpectedStop: async () => ({
					unexpectedStop: true,
					cost: 0.0003,
					latencyMs: 900,
				}),
			};

			let robompCalls = 0;
			const results = await run({
				setsDir,
				outDir,
				jevBaseUrl: stub.baseUrl,
				fakeSmol,
				robompRunner: async () => {
					robompCalls += 1;
					return { accuracy: 1 };
				},
			});

			expect(robompCalls).toBe(0);
			expect(results.verdict).toBe(WP5_AMENDMENT);
			expect(results.features.robomp.current.accuracy).toBeNull();
			expect(results.features.robomp.current.p50LatencyMs).toBeNull();
			expect(results.features.robomp.current.p95LatencyMs).toBeNull();
			expect(results.features.robomp.current.costPer1000).toBeNull();
			expect(results.features.robomp.current.unparseableRate).toBeNull();
			expect(results.features.robomp.current.offListRate).toBeNull();
			expect(results.features.robomp.jev.accuracy).toBeNull();
			expect(results.features.robomp.jev.p50LatencyMs).toBeNull();
			expect(results.features.robomp.jev.p95LatencyMs).toBeNull();
			expect(results.features.robomp.jev.costPer1000).toBeNull();
			expect(results.features.robomp.jev.unparseableRate).toBeNull();
			expect(results.features.robomp.jev.offListRate).toBeNull();
			expect(results.features.robomp.jev.confidentBucketAccuracy).toBeNull();
			expect(results.features.robomp.jev.skipSessionShare).toBeNull();
			expect(results.robompSessionFlagsMissing).toBeNull();
			expect(results.features.auto_thinking.jev.accuracy).toBe(1);
			expect(results.features.unexpected_stop.jev.accuracy).toBe(1);

			const resultsJson = await Bun.file(path.join(outDir, "results.json")).json();
			expect(resultsJson.features.robomp.current.accuracy).toBeNull();
			expect(resultsJson.features.robomp.jev.confidentBucketAccuracy).toBeNull();
			expect(resultsJson.features.robomp.jev.skipSessionShare).toBeNull();
			expect(resultsJson.robompSessionFlagsMissing).toBeNull();
			expect(resultsJson.verdict).toBe(WP5_AMENDMENT);

			const reportMarkdown = await Bun.file(path.join(outDir, "jev-measurement-report.md")).text();
			const cells = robompValueCells(reportMarkdown);
			expect(cells.length).toBe(16);
			for (const cell of cells) {
				expect(cell).toBe(ROBOMP_NOT_MEASURED);
			}
			expect(reportMarkdown.split(ROBOMP_NOT_MEASURED).length - 1).toBe(16);
			expect(wp5VerdictLines(reportMarkdown)).toEqual([WP5_AMENDMENT]);
		});

		it("non-empty issues.jsonl still measures robomp and keeps one WP5 verdict", async () => {
			const setsDir = path.join(tempDir.path(), "sets-with-issues");
			const outDir = path.join(tempDir.path(), "out-with-issues");
			await fs.mkdir(setsDir, { recursive: true });
			await Bun.write(path.join(setsDir, "prompts.jsonl"), "");
			await Bun.write(path.join(setsDir, "turn-ends.jsonl"), "");
			await Bun.write(
				path.join(setsDir, "issues.jsonl"),
				`${JSON.stringify({
					key: "test/repo#1",
					repo: "test/repo",
					number: 1,
					title: "Spam issue",
					body: "Spam content",
					label: "invalid",
				})}\n`,
			);

			let robompCalls = 0;
			const results = await run({
				setsDir,
				outDir,
				robompSessionCostUsd: 0.5,
				robompSessionP50Ms: 30000,
				robompSessionP95Ms: 60000,
				jevBaseUrl: stub.baseUrl,
				robompRunner: async () => {
					robompCalls += 1;
					return {
						accuracy: 1,
						confidentBucketAccuracy: 0.98,
						skipSessionShare: 0.25,
						p50LatencyMs: 150,
						p95LatencyMs: 300,
						costPer1000: 0.005,
						unparseableRate: 0,
						offListRate: 0,
					};
				},
			});

			expect(robompCalls).toBe(1);
			expect(results.features.robomp.current.accuracy).toBe(1);
			expect(results.features.robomp.jev.confidentBucketAccuracy).toBe(0.98);
			expect(results.robompSessionFlagsMissing).toBe(false);
			expect(results.verdict).toBe(WP5_AMENDMENT);

			const reportMarkdown = await Bun.file(path.join(outDir, "jev-measurement-report.md")).text();
			expect(reportMarkdown).not.toContain(ROBOMP_NOT_MEASURED);
			expect(reportMarkdown).toContain("| Confident-Bucket Accuracy | - | 98.0% |");
			expect(reportMarkdown).toContain("| Skip-Session Share | 0.0% | 25.0% |");
			expect(wp5VerdictLines(reportMarkdown)).toEqual([WP5_AMENDMENT]);
		});

		it("fake-smol baseline is named and still permits the WP5 verdict", async () => {
			const setsDir = path.join(tempDir.path(), "sets-fake-baseline");
			const outDir = path.join(tempDir.path(), "out-fake-baseline");
			await fs.mkdir(setsDir, { recursive: true });

			await Bun.write(
				path.join(setsDir, "prompts.jsonl"),
				`${JSON.stringify({ prompt: "Refactor user authentication service", effort: "medium" })}\n`,
			);
			await Bun.write(
				path.join(setsDir, "turn-ends.jsonl"),
				`${JSON.stringify({ text: "I will now edit the file.", label: "continue" })}\n`,
			);

			const fakeSmol: FakeSmolHandler = {
				classifyDifficulty: async () => ({ effort: "medium", cost: 0.0005, latencyMs: 1200 }),
				classifyUnexpectedStop: async () => ({ unexpectedStop: true, cost: 0.0003, latencyMs: 900 }),
			};

			const results = await run({
				setsDir,
				outDir,
				jevBaseUrl: stub.baseUrl,
				fakeSmol,
				robompRunner: async () => ({ accuracy: 1 }),
			});

			expect(results.currentBaseline).toBe("fake");
			expect(results.features.auto_thinking.current.costPer1000).toBeCloseTo(0.5, 6);
			expect(results.verdict).toBe(WP5_AMENDMENT);

			const reportMarkdown = await Bun.file(path.join(outDir, "jev-measurement-report.md")).text();
			expect(reportMarkdown).toContain("--fake-smol test handler (verdict allowed)");
			expect(reportMarkdown).toContain("Sample size: 1 prompts");
			expect(reportMarkdown).toContain("Sample size: 1 turn ends");
			expect(reportMarkdown).toContain("Sample size: 0 issues");
			expect(wp5VerdictLines(reportMarkdown)).toEqual([WP5_AMENDMENT]);
		});

		it("mocked baseline withholds the WP5 verdict line and names the mock", async () => {
			const setsDir = path.join(tempDir.path(), "sets-mocked-baseline");
			const outDir = path.join(tempDir.path(), "out-mocked-baseline");
			await fs.mkdir(setsDir, { recursive: true });

			const results = await run({ setsDir, outDir, jevBaseUrl: stub.baseUrl, mockedBaseline: true });

			expect(results.currentBaseline).toBe("mocked");
			expect(results.verdict).toBe(WP5_VERDICT_WITHHELD);
			// The withheld sentence is what marks the fabrication; flipping the
			// marker back to a real baseline restores a WP5 decision.
			expect(verdict({ ...results, currentBaseline: "real" })).not.toBe(WP5_VERDICT_WITHHELD);

			const reportMarkdown = await Bun.file(path.join(outDir, "jev-measurement-report.md")).text();
			expect(reportMarkdown).toContain("mocked baseline (WP5 verdict withheld)");
			expect(reportMarkdown).toContain(WP5_VERDICT_WITHHELD);
			expect(wp5VerdictLines(reportMarkdown)).toEqual([]);
		});

		it("real current side measures per-call latency and provider-reported usage via an injected registry", async () => {
			const setsDir = path.join(tempDir.path(), "sets-real-baseline");
			const outDir = path.join(tempDir.path(), "out-real-baseline");
			await fs.mkdir(setsDir, { recursive: true });

			// The mock answers a FIXED value unrelated to the prompt's recorded
			// label, so accuracy can only be 0 when the answer comes from the mock
			// rather than from the label (the fabricating path would score 100%).
			await Bun.write(
				path.join(setsDir, "prompts.jsonl"),
				`${JSON.stringify({ prompt: "Refactor user authentication service", effort: "medium" })}\n`,
			);
			await Bun.write(
				path.join(setsDir, "turn-ends.jsonl"),
				`${JSON.stringify({ text: "I will now edit the file.", label: "stop" })}\n`,
			);

			const mock = createMockModel({
				id: "mock-model",
				provider: "mock",
				handler: (() => {
					let call = 0;
					return () => {
						call += 1;
						return {
							content: [call === 1 ? "high" : "yes"],
							stopReason: "stop" as const,
							delayMs: 5,
							usage: { cost: { total: 0.0042 } },
						};
					};
				})(),
			});
			registerMockApi("measurement-harness-test");
			try {
				const settings = {
					get: (key: string) => {
						if (key === "providers.autoThinkingModel") return "online";
						if (key === "providers.unexpectedStopModel") return "online";
						return undefined;
					},
					getModelRole: (role: string) => (role === "tiny" || role === "smol" ? "mock/mock-model" : undefined),
					getStorage: () => undefined,
				} as unknown as CurrentSmolHarness["settings"];
				const registry = {
					getAvailable: () => [mock],
					getApiKey: async () => "test-key",
					getApiKeyForProvider: async () => "test-key",
					resolver: () => async () => "test-key",
				} as unknown as CurrentSmolHarness["registry"];

				const results = await run({
					setsDir,
					outDir,
					jevBaseUrl: stub.baseUrl,
					smol: { settings, registry },
					robompRunner: async () => ({ accuracy: 1 }),
				});

				expect(results.currentBaseline).toBe("real");
				// 0.0042 per call * 1000, replacing the fabricated 0.0005 constant.
				expect(results.features.auto_thinking.current.costPer1000).toBeCloseTo(4.2, 6);
				expect(results.features.unexpected_stop.current.costPer1000).toBeCloseTo(4.2, 6);
				// Latency is measured around the call (the mock delays 5ms), never
				// the constant 1200/900 the faked handler reported.
				expect(results.features.auto_thinking.current.p50LatencyMs).toBeGreaterThanOrEqual(5);
				expect(results.features.auto_thinking.current.p50LatencyMs).toBeLessThan(1200);
				expect(results.features.unexpected_stop.current.p50LatencyMs).toBeGreaterThanOrEqual(5);
				expect(results.features.unexpected_stop.current.p50LatencyMs).toBeLessThan(900);
				// The answer came from the model, not the item's label.
				expect(results.features.auto_thinking.current.accuracy).toBe(0);
				expect(results.features.unexpected_stop.current.accuracy).toBe(0);

				const reportMarkdown = await Bun.file(path.join(outDir, "jev-measurement-report.md")).text();
				expect(reportMarkdown).toContain(
					"real configured smol model (measured latency and provider-reported usage)",
				);
			} finally {
				unregisterCustomApis("measurement-harness-test");
			}
		});
	});
});
