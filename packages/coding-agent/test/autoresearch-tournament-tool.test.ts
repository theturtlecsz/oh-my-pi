import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { Usage } from "@oh-my-pi/pi-ai";
import { createHypothesisTournamentTool } from "../src/autoresearch/tools/hypothesis-tournament";
import { replayTournament } from "../src/autoresearch/tournament/aggregate";
import type { Summarizer } from "../src/autoresearch/tournament/prepare";
import { TOURNAMENT_LABEL, type Tournament, type TournamentJudge } from "../src/autoresearch/tournament/types";
import type { AutoresearchToolFactoryOptions } from "../src/autoresearch/types";
import type { ExtensionContext } from "../src/extensibility/extensions";
import { SessionManager } from "../src/session/session-manager";
import { SessionStatsTracker, type SessionStatsTrackerHost } from "../src/session/session-stats";

function makeUsage(overrides: Partial<Usage> = {}): Usage {
	return {
		input: 10,
		output: 5,
		cacheRead: 2,
		cacheWrite: 1,
		totalTokens: 18,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0.05 },
		...overrides,
	};
}

describe("autoresearch hypothesis_tournament tool", () => {
	let tempDir: string;

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "ht-tool-test-"));
	});

	afterEach(async () => {
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	it("runs tournament on five-hypotheses fixture with stub judges and summarizer", async () => {
		const fixturePath = path.resolve(import.meta.dir, "fixtures/tournament/five-hypotheses.json");
		const inputCopyPath = path.join(tempDir, "input.json");
		await fs.copyFile(fixturePath, inputCopyPath);

		const stubSummarizer: Summarizer = {
			async summarize(writeUp, maxChars) {
				return {
					text: writeUp.slice(0, Math.min(writeUp.length, maxChars - 100)),
					usage: makeUsage({ input: 100, output: 50 }),
				};
			},
		};

		const stubJudge: TournamentJudge = {
			id: "stub-judge-1",
			family: "family-stub",
			async judge(aText, bText) {
				const outcome = aText.length >= bText.length ? "A" : "B";
				return {
					outcome,
					usage: makeUsage({ input: 20, output: 10 }),
				};
			},
		};

		const options: AutoresearchToolFactoryOptions = {
			dashboard: {} as any,
			getRuntime: () => ({}) as any,
			pi: {} as any,
		};

		const tool = createHypothesisTournamentTool(options, {
			createSummarizer: async () => stubSummarizer,
			createJudges: async () => [stubJudge],
		});

		expect(tool.name).toBe("hypothesis_tournament");
		expect(tool.defaultInactive).toBe(true);

		const mockCtx: Partial<ExtensionContext> = {
			cwd: tempDir,
			sessionManager: { getSessionId: () => "sess-1" } as any,
			modelRegistry: { getApiKey: async () => "key" } as any,
			models: {
				resolve: () => undefined,
				family: () => "test-family",
				list: () => [],
				current: () => undefined,
			},
		};

		const result = await tool.execute(
			"call_1",
			{ input_path: inputCopyPath, seed: 42 },
			undefined,
			undefined,
			mockCtx as ExtensionContext,
		);

		expect(result.details).toBeDefined();
		const details = result.details!;

		// Contract: first line is exactly TOURNAMENT_LABEL
		const textContent = result.content[0]?.type === "text" ? result.content[0].text : "";
		const lines = textContent.split("\n");
		expect(lines[0]).toBe(TOURNAMENT_LABEL);

		// Contract: output file exists (<input_path minus .json>.tournament.json)
		const expectedTournamentPath = path.join(tempDir, "input.tournament.json");
		expect(details.tournamentPath).toBe(expectedTournamentPath);
		const fileExists = await Bun.file(expectedTournamentPath).exists();
		expect(fileExists).toBe(true);

		// Contract: replayTournament(file) JSON equals stored result
		const tournamentJson: Tournament = await Bun.file(expectedTournamentPath).json();
		const replayed = replayTournament(tournamentJson);
		expect(JSON.stringify(replayed)).toBe(JSON.stringify(details.result));
		expect(JSON.stringify(replayed)).toBe(JSON.stringify(tournamentJson.result));

		// Contract: long write-up has summarized: true, others have summarized: false
		const longHypothesis = tournamentJson.hypotheses.find(h => h.id === "h3");
		expect(longHypothesis).toBeDefined();
		expect(longHypothesis?.summarized).toBe(true);

		const shortHypotheses = tournamentJson.hypotheses.filter(h => h.id !== "h3");
		expect(shortHypotheses.length).toBe(4);
		for (const h of shortHypotheses) {
			expect(h.summarized).toBe(false);
		}

		// Contract: seed is recorded
		expect(tournamentJson.seed).toBe(42);

		// Contract: usage accumulates both summarizer and judge usage
		expect(details.usage.input).toBeGreaterThan(100);
		expect(details.usage.output).toBeGreaterThan(50);

		// Contract: renderers produce Text components
		const theme = {
			fg: (_color: string, text: string) => text,
			bold: (text: string) => text,
		} as any;
		const callComponent = tool.renderCall?.({ input_path: inputCopyPath }, {} as any, theme);
		expect(callComponent).toBeDefined();

		const resultComponent = tool.renderResult?.(result, {} as any, theme);
		expect(resultComponent).toBeDefined();
	});

	it("defaults seed to a recorded random integer when omitted", async () => {
		const fixturePath = path.resolve(import.meta.dir, "fixtures/tournament/five-hypotheses.json");
		const inputCopyPath = path.join(tempDir, "input-random-seed.json");
		await fs.copyFile(fixturePath, inputCopyPath);

		const stubJudge: TournamentJudge = {
			id: "stub-judge-1",
			family: "family-stub",
			async judge() {
				return { outcome: "A" };
			},
		};

		const tool = createHypothesisTournamentTool({} as any, {
			createSummarizer: async () => ({
				async summarize(writeUp) {
					return { text: writeUp.slice(0, 100) };
				},
			}),
			createJudges: async () => [stubJudge],
		});

		const mockCtx: Partial<ExtensionContext> = {
			cwd: tempDir,
			sessionManager: { getSessionId: () => "sess-2" } as any,
			modelRegistry: { getApiKey: async () => "key" } as any,
			models: {
				resolve: () => undefined,
				family: () => "test-family",
				list: () => [],
				current: () => undefined,
			},
		};

		const result = await tool.execute(
			"call_2",
			{ input_path: inputCopyPath },
			undefined,
			undefined,
			mockCtx as ExtensionContext,
		);

		expect(result.details).toBeDefined();
		const details = result.details!;
		const tournamentJson: Tournament = await Bun.file(details.tournamentPath).json();
		expect(typeof tournamentJson.seed).toBe("number");
		expect(Number.isInteger(tournamentJson.seed)).toBe(true);
		expect(tournamentJson.seed).toBeGreaterThanOrEqual(0);
	});
});

describe("hypothesis_tournament session usage accounting", () => {
	it("accumulates usage from hypothesis_tournament tool results in SessionManager totals", () => {
		const session = SessionManager.inMemory();

		session.appendMessage({ role: "user", content: "run tournament", timestamp: 1 });
		session.appendMessage({
			role: "assistant",
			content: [{ type: "text", text: "running tournament..." }],
			api: "openai-completions",
			provider: "openai",
			model: "gpt-4o",
			usage: {
				input: 10,
				output: 5,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 15,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0.01 },
			},
			stopReason: "stop",
			timestamp: 2,
		});

		session.appendMessage({
			role: "toolResult",
			toolCallId: "call_ht",
			toolName: "hypothesis_tournament",
			content: [{ type: "text", text: "tournament result" }],
			details: {
				tournamentPath: "/path/to/tournament.json",
				result: { label: TOURNAMENT_LABEL, ranking: [] },
				usage: {
					input: 200,
					output: 100,
					cacheRead: 50,
					cacheWrite: 25,
					totalTokens: 375,
					cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0.45 },
				},
			},
			isError: false,
			timestamp: 3,
		});

		const stats = session.getUsageStatistics();
		expect(stats.input).toBe(210);
		expect(stats.output).toBe(105);
		expect(stats.cacheRead).toBe(50);
		expect(stats.cacheWrite).toBe(25);
		expect(stats.totalTokens).toBe(390);
		expect(stats.cost).toBeCloseTo(0.46, 5);
	});

	it("accumulates usage from hypothesis_tournament tool results in SessionStatsTracker", () => {
		const messages: any[] = [
			{
				role: "user",
				content: "evaluate",
				timestamp: 1,
			},
			{
				role: "assistant",
				content: [{ type: "text", text: "started" }],
				usage: {
					input: 10,
					output: 20,
					cacheRead: 0,
					cacheWrite: 0,
					totalTokens: 30,
					cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0.02 },
				},
				timestamp: 2,
			},
			{
				role: "toolResult",
				toolCallId: "ht_call_1",
				toolName: "hypothesis_tournament",
				content: [{ type: "text", text: "result text" }],
				details: {
					usage: {
						input: 500,
						output: 250,
						cacheRead: 100,
						cacheWrite: 50,
						totalTokens: 900,
						cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 1.25 },
					},
				},
				isError: false,
				timestamp: 3,
			},
		];

		const host: SessionStatsTrackerHost = {
			session: {
				getSkills: () => [],
				getTools: () => [],
				getSystemContext: () => [],
				getSystemPrompt: () => [],
			} as any,
			agent: {
				state: { messages },
				tokenizer: {
					countTokens: () => 0,
					countMessages: () => 0,
				},
			} as any,
			sessionManager: {
				getSessionFile: () => "/path/to/session.jsonl",
				getBranch: () => [],
			} as any,
			modelRegistry: {} as any,
			model: () => undefined,
			sessionId: () => "test-session-id",
		};

		const tracker = new SessionStatsTracker(host);
		const stats = tracker.getSessionStats();

		expect(stats.tokens.input).toBe(510);
		expect(stats.tokens.output).toBe(270);
		expect(stats.tokens.cacheRead).toBe(100);
		expect(stats.tokens.cacheWrite).toBe(50);
		expect(stats.tokens.total).toBe(930);
		expect(stats.cost).toBeCloseTo(1.27, 5);
	});
});
