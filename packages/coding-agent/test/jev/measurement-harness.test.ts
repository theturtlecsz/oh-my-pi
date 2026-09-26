import { Database } from "bun:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import {
	buildIssuesSet,
	extractPromptsFromSession,
	extractTurnEndsFromSession,
} from "../../../../docs/reports/jev-measurement/build-sets";
import {
	type FakeSmolHandler,
	type MeasurementResults,
	verdict,
	WP5_AMENDMENT,
} from "../../../../docs/reports/jev-measurement/harness";
import { run } from "../../../../docs/reports/jev-measurement/run";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

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
	});
});
