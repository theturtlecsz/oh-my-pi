import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { createHypothesisTournamentTool } from "../../src/autoresearch/tools/hypothesis-tournament";
import { replayTournament } from "../../src/autoresearch/tournament/aggregate";
import { createJevJudge, parseJevJudgeOutcome } from "../../src/autoresearch/tournament/jev-judge";
import { type HypothesisInput, prepareHypotheses, type Summarizer } from "../../src/autoresearch/tournament/prepare";
import { runTournament } from "../../src/autoresearch/tournament/runner";
import { maxJudgeCalls } from "../../src/autoresearch/tournament/schedule";
import { TOURNAMENT_LABEL, type Tournament, type TournamentJudge } from "../../src/autoresearch/tournament/types";
import type { AutoresearchToolFactoryOptions } from "../../src/autoresearch/types";
import { resetSettingsForTest, Settings } from "../../src/config/settings";
import type { ExtensionAPI, ExtensionContext } from "../../src/extensibility/extensions";
import type { JevUsageEntry } from "../../src/tiny/jev-client";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

/** Minimal ExtensionAPI stub: the Jev judge path only calls `appendEntry`. */
function makePiStub(): { pi: ExtensionAPI; entries: Array<{ customType: string; data: unknown }> } {
	const entries: Array<{ customType: string; data: unknown }> = [];
	const pi = {
		appendEntry: (customType: string, data?: unknown) => entries.push({ customType, data }),
	} as unknown as ExtensionAPI;
	return { pi, entries };
}

describe("parseJevJudgeOutcome", () => {
	it("maps highest probability to outcome A", () => {
		const result = parseJevJudgeOutcome({ A: 0.7, B: 0.2, tie: 0.1 });
		expect(result.outcome).toBe("A");
		expect(result.probabilities).toEqual({ A: 0.7, B: 0.2, tie: 0.1 });
	});

	it("maps highest probability to outcome B", () => {
		const result = parseJevJudgeOutcome({ A: 0.15, B: 0.75, tie: 0.1 });
		expect(result.outcome).toBe("B");
		expect(result.probabilities).toEqual({ A: 0.15, B: 0.75, tie: 0.1 });
	});

	it("declares a tie when tie probability is highest or equal to winner", () => {
		const result1 = parseJevJudgeOutcome({ A: 0.3, B: 0.2, tie: 0.5 });
		expect(result1.outcome).toBe("tie");

		const result2 = parseJevJudgeOutcome({ A: 0.4, B: 0.2, tie: 0.4 });
		expect(result2.outcome).toBe("tie");
	});

	it("declares a tie when A and B have equal probability exceeding tie", () => {
		const result = parseJevJudgeOutcome({ A: 0.45, B: 0.45, tie: 0.1 });
		expect(result.outcome).toBe("tie");
	});

	it("handles distributions without an explicit tie key", () => {
		const resultA = parseJevJudgeOutcome({ A: 0.8, B: 0.2 });
		expect(resultA.outcome).toBe("A");
		expect(resultA.probabilities.tie).toBeUndefined();

		const resultB = parseJevJudgeOutcome({ A: 0.3, B: 0.7 });
		expect(resultB.outcome).toBe("B");
	});
});

describe("createJevJudge", () => {
	let stub: StubJevServer;
	let recordedUsage: JevUsageEntry[];

	beforeEach(() => {
		stub = startStubJevServer();
		recordedUsage = [];
	});

	afterEach(() => {
		stub.stop();
	});

	function makeDeps(overrides: Record<string, unknown> = {}) {
		const settingsStore: Record<string, unknown> = {
			"jev.enabled": true,
			"jev.baseUrl": stub.baseUrl,
			...overrides,
		};
		return {
			getSetting: (p: string) => settingsStore[p],
			getApiKey: () => "test-api-key",
			recordUsage: (entry: JevUsageEntry) => recordedUsage.push(entry),
		};
	}

	it("exposes judge id and family contracts", () => {
		const judgeDefault = createJevJudge();
		expect(judgeDefault.id).toBe("jev");
		expect(judgeDefault.family).toBe("jev");

		const judgeCustom = createJevJudge({ id: "custom-jev", family: "custom-family" });
		expect(judgeCustom.id).toBe("custom-jev");
		expect(judgeCustom.family).toBe("custom-family");
	});

	it("sends blinded candidate texts and receives valid choice verdict", async () => {
		stub.setAnswers({
			preference: {
				probabilities: { A: 0.82, B: 0.12, tie: 0.06 },
			},
		});

		const judge = createJevJudge({ deps: makeDeps() });
		const verdict = await judge.judge("Write-up for candidate 1", "Write-up for candidate 2", {
			question: "Which approach is superior?",
		});

		expect(verdict.outcome).toBe("A");
		expect(verdict.probabilities).toEqual({ A: 0.82, B: 0.12, tie: 0.06 });
		expect(verdict.usage).toBeDefined();
		expect(verdict.usage?.totalTokens).toBeGreaterThan(0);
		expect(verdict.usage?.cost.total).toBeGreaterThan(0);

		expect(stub.requests.length).toBe(1);
		const req = stub.requests[0];
		expect(req.body).toMatchObject({
			model: "jev-latest",
			questions: {
				preference: {
					type: "choice",
					options: ["A", "B", "tie"],
				},
			},
		});
		const rawState = (req.body as { state: string }).state;
		expect(rawState).toContain("Research Question:\nWhich approach is superior?");
		expect(rawState).toContain("Candidate A:\nWrite-up for candidate 1");
		expect(rawState).toContain("Candidate B:\nWrite-up for candidate 2");

		expect(recordedUsage.length).toBe(1);
		expect(recordedUsage[0].feature).toBe("tournament_judge");
		expect(recordedUsage[0].outcome).toBe("ok");
	});

	it("ensures off-list answers are structurally impossible and rejects corrupt backend options", async () => {
		stub.setAnswers({
			preference: {
				probabilities: { C: 0.99, D: 0.01 },
			},
		});

		const judge = createJevJudge({ deps: makeDeps() });
		await expect(judge.judge("Text A", "Text B", { question: "Q" })).rejects.toThrow(/Jev judge call failed/);

		expect(recordedUsage.length).toBe(1);
		expect(recordedUsage[0].outcome).toBe("off_list");
	});

	it("rejects when Jev service encounters an HTTP 500 or timeout", async () => {
		stub.setMode("500");
		const judge = createJevJudge({ deps: makeDeps() });
		await expect(judge.judge("Text A", "Text B", { question: "Q" })).rejects.toThrow(/Jev judge call failed/);
	});

	it("stops and rejects immediately when signal is aborted", async () => {
		const controller = new AbortController();
		controller.abort();

		const judge = createJevJudge({ deps: makeDeps() });
		await expect(judge.judge("Text A", "Text B", { question: "Q", signal: controller.signal })).rejects.toThrow();
	});
});

describe("hypothesis_tournament tool with Jev judge selected", () => {
	let tempDir: string;
	let stub: StubJevServer;

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "ht-jev-tool-"));
		stub = startStubJevServer();
		// The tool reads the global settings singleton (ExtensionContext exposes
		// no settings handle); initialize it for this file's tool tests.
		resetSettingsForTest();
		await Settings.init({
			inMemory: true,
			overrides: {
				"autoresearch.tournament.judgeModel": "jev",
				"jev.enabled": true,
				"jev.baseUrl": stub.baseUrl,
			},
		});
	});

	afterEach(async () => {
		stub.stop();
		resetSettingsForTest();
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	it("runs tournament on five-hypotheses fixture with Jev judge selected", async () => {
		const fixturePath = path.resolve(import.meta.dir, "../fixtures/tournament/five-hypotheses.json");
		const inputCopyPath = path.join(tempDir, "input.json");
		await fs.copyFile(fixturePath, inputCopyPath);

		stub.setAnswers({
			preference: {
				probabilities: { A: 0.7, B: 0.2, tie: 0.1 },
			},
		});

		const stubSummarizer: Summarizer = {
			async summarize(writeUp, maxChars) {
				return {
					text: writeUp.slice(0, Math.min(writeUp.length, maxChars - 100)),
				};
			},
		};

		const { pi, entries } = makePiStub();
		const options: AutoresearchToolFactoryOptions = {
			dashboard: {} as any,
			getRuntime: () => ({}) as any,
			pi,
		};

		const tool = createHypothesisTournamentTool(options, {
			createSummarizer: async () => stubSummarizer,
		});

		const mockCtx: Partial<ExtensionContext> = {
			cwd: tempDir,
			sessionManager: {
				getSessionId: () => "sess-jev-1",
			} as any,
			modelRegistry: {
				getApiKey: async () => "test-typesafe-key",
				getApiKeyForProvider: async () => "test-typesafe-key",
			} as any,
			models: {
				resolve: (spec: string) => ({ id: spec, api: "mock" }) as any,
				family: () => "mock-family",
				list: () => [],
				current: () => undefined,
			},
		};

		const result = await tool.execute(
			"call_jev_1",
			{ input_path: inputCopyPath, seed: 101 },
			undefined,
			undefined,
			mockCtx as ExtensionContext,
		);

		expect(result.details).toBeDefined();
		const details = result.details!;

		const textContent = result.content[0]?.type === "text" ? result.content[0].text : "";
		expect(textContent).toContain(TOURNAMENT_LABEL);
		expect(textContent).toContain("Output:");

		const tournamentJson: Tournament = await Bun.file(details.tournamentPath).json();
		expect(tournamentJson.comparisons.length).toBe(20);
		expect(tournamentJson.comparisons.every(c => c.judgeFamily === "jev")).toBe(true);

		const replayed = replayTournament(tournamentJson);
		expect(JSON.stringify(replayed)).toBe(JSON.stringify(details.result));

		// The default judges path records each Jev attempt through pi.appendEntry.
		expect(entries.length).toBeGreaterThan(0);
		expect(entries.every(entry => entry.customType === "jev_usage")).toBe(true);
	});

	it("runs dual-judge tournament with Jev and chat judge families for bias checks", async () => {
		const fixturePath = path.resolve(import.meta.dir, "../fixtures/tournament/five-hypotheses.json");
		const inputCopyPath = path.join(tempDir, "input-dual.json");
		await fs.copyFile(fixturePath, inputCopyPath);

		stub.setAnswers({
			preference: {
				probabilities: { A: 0.65, B: 0.25, tie: 0.1 },
			},
		});

		const stubSummarizer: Summarizer = {
			async summarize(writeUp, maxChars) {
				return { text: writeUp.slice(0, Math.min(writeUp.length, maxChars - 100)) };
			},
		};

		const isolatedSettings = Settings.isolated({
			"autoresearch.tournament.judgeModel": "jev",
			"autoresearch.tournament.secondJudgeModel": "@smol",
			"jev.enabled": true,
			"jev.baseUrl": stub.baseUrl,
		});

		const chatModel = {
			id: "smol-model-1",
			api: "openai-completions",
			provider: "mock",
		} as any;

		const mockCtx: Partial<ExtensionContext> = {
			cwd: tempDir,
			sessionManager: {
				getSessionId: () => "sess-dual-1",
			} as any,
			modelRegistry: {
				getApiKey: async () => "key",
			} as any,
			models: {
				resolve: (spec: string) => (spec === "@smol" ? chatModel : undefined),
				family: () => "chat-family",
				list: () => [],
				current: () => undefined,
			},
		};

		// This test injects its own judges, so the pi stub is inert; only the
		// explicit Jev judge deps below drive the stub Jev server.
		const tool = createHypothesisTournamentTool({} as any, {
			createSummarizer: async () => stubSummarizer,
			createJudges: async () => {
				const jevJudge = createJevJudge({
					deps: {
						getSetting: (p: string) => (isolatedSettings as any).get(p),
						getApiKey: () => "test-key",
						recordUsage: () => {},
					},
				});
				const chatJudge: TournamentJudge = {
					id: "chat-smol",
					family: "chat-family",
					async judge() {
						return { outcome: "A", probabilities: { A: 0.8, B: 0.2 } };
					},
				};
				return [jevJudge, chatJudge];
			},
		});

		const result = await tool.execute(
			"call_dual_1",
			{ input_path: inputCopyPath, seed: 77 },
			undefined,
			undefined,
			mockCtx as ExtensionContext,
		);

		const tournamentJson: Tournament = await Bun.file(result.details!.tournamentPath).json();
		// 10 pairs * 2 orders * 2 judges = 40 comparisons
		expect(tournamentJson.comparisons.length).toBe(40);
		expect(tournamentJson.result.familyAgreement).toBeDefined();
		expect(tournamentJson.result.familyAgreement?.families).toEqual(["chat-family", "jev"]);
		expect(tournamentJson.result.familyAgreement?.agreement).toBe(1);
	});
});

describe("runTournament Swiss schedule with Jev judge (20 hypotheses)", () => {
	let stub: StubJevServer;

	beforeEach(() => {
		stub = startStubJevServer();
	});

	afterEach(() => {
		stub.stop();
	});

	it("executes 5 rounds for 20 candidates under Swiss schedule with Jev judge", async () => {
		stub.setAnswers({
			preference: {
				probabilities: { A: 0.6, B: 0.3, tie: 0.1 },
			},
		});

		const inputs: HypothesisInput[] = Array.from({ length: 20 }, (_, i) => ({
			id: `cand-${i + 1}`,
			title: `Candidate ${i + 1}`,
			writeUp: `Detailed scientific hypothesis text for candidate ${i + 1}.`,
		}));

		const { hypotheses } = await prepareHypotheses(inputs, { maxJudgeChars: 2000 });

		const jevJudge = createJevJudge({
			deps: {
				getSetting: (p: string) => {
					if (p === "jev.enabled") return true;
					if (p === "jev.baseUrl") return stub.baseUrl;
					return undefined;
				},
				getApiKey: () => "key",
				recordUsage: () => {},
			},
		});

		const tournament = await runTournament({
			id: "tournament-20-candidates",
			question: "Which mechanism provides superior cache locality?",
			hypotheses,
			judges: [jevJudge],
			swissRoundCap: 5,
			seed: 2026,
		});

		expect(tournament.hypotheses.length).toBe(20);
		expect(tournament.schedule).toEqual({ kind: "swiss", roundCap: 5 });
		const bound = maxJudgeCalls(tournament.schedule, 20, 1);
		expect(tournament.comparisons.length).toBeLessThanOrEqual(bound);
		expect(tournament.comparisons.length).toBeGreaterThan(0);
		expect(tournament.result.ranking.length).toBe(20);
		expect(tournament.usage.totalTokens).toBeGreaterThan(0);
	});
});
