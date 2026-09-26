import { describe, expect, it } from "bun:test";
import type { Usage } from "@oh-my-pi/pi-ai";
import { type HypothesisInput, prepareHypotheses } from "../src/autoresearch/tournament/prepare";
import { runTournament } from "../src/autoresearch/tournament/runner";
import { maxJudgeCalls, pairKey } from "../src/autoresearch/tournament/schedule";
import type { ComparisonRecord, Hypothesis, TournamentJudge } from "../src/autoresearch/tournament/types";

function makeUsage(overrides: Partial<Usage> = {}): Usage {
	return {
		input: 1,
		output: 2,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 3,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		...overrides,
	};
}

/**
 * Build hypotheses whose write-ups embed both id and title, then run them through
 * the normal blinding pass so `judgeText` carries a redacted identity plus the
 * surviving `strength <n>` marker the stub judges rank on.
 */
async function buildHypotheses(count: number): Promise<Hypothesis[]> {
	const inputs: HypothesisInput[] = Array.from({ length: count }, (_, i) => {
		const id = `h${i + 1}`;
		const title = `Title ${id}`;
		return { id, title, writeUp: `Hypothesis ${id} titled ${title} has strength ${count - i}.` };
	});
	const { hypotheses } = await prepareHypotheses(inputs, { maxJudgeChars: 10_000 });
	return hypotheses;
}

function strengthOf(text: string): number {
	const match = /strength (\d+)/.exec(text);
	return match ? Number(match[1]) : 0;
}

function preferHigherStrengthJudge(id: string, family: string, usage?: Usage): TournamentJudge {
	return {
		id,
		family,
		async judge(aText, bText) {
			const verdict = { outcome: (strengthOf(aText) >= strengthOf(bText) ? "A" : "B") as "A" | "B" };
			return usage ? { ...verdict, usage } : verdict;
		},
	};
}

describe("runTournament blinding and ordering", () => {
	it("never exposes hypothesis ids or titles to a judge and evaluates both presentation orders per pair", async () => {
		const hypotheses = await buildHypotheses(4);
		const seen: Array<[string, string]> = [];
		const judge: TournamentJudge = {
			id: "recorder",
			family: "family-1",
			async judge(aText, bText) {
				seen.push([aText, bText]);
				return { outcome: "A" };
			},
		};

		await runTournament({
			id: "t-blinding",
			question: "Pick the strongest hypothesis.",
			hypotheses,
			judges: [judge],
			swissRoundCap: 3,
			seed: 1,
		});

		// Every input the judge saw must be identity-free: write-ups embed id and title,
		// so any leak would surface here.
		for (const [aText, bText] of seen) {
			for (const hypothesis of hypotheses) {
				for (const text of [aText, bText]) {
					expect(text).not.toContain(hypothesis.id);
					expect(text).not.toContain(hypothesis.title);
				}
			}
		}

		// 4 candidates -> 6 pairs, each evaluated in both orders: 12 judge calls.
		expect(seen).toHaveLength(12);

		// Map each judge text back to its hypothesis, then confirm every unordered
		// pair was shown in both presentation orders.
		const idByText = new Map(hypotheses.map(hypothesis => [hypothesis.judgeText, hypothesis.id]));
		const ordersByPair = new Map<string, Set<string>>();
		for (const [aText, bText] of seen) {
			const a = idByText.get(aText);
			const b = idByText.get(bText);
			expect(a).toBeDefined();
			expect(b).toBeDefined();
			const key = pairKey(a as string, b as string);
			const orders = ordersByPair.get(key) ?? new Set<string>();
			orders.add(`${a}>${b}`);
			ordersByPair.set(key, orders);
		}
		expect(ordersByPair.size).toBe(6);
		for (const orders of ordersByPair.values()) {
			expect(orders.size).toBe(2);
		}
	});
});

describe("runTournament verdict recording", () => {
	it("records a tie for every comparison and produces a flat ranking when the judge always ties", async () => {
		const hypotheses = await buildHypotheses(3);
		const judge: TournamentJudge = {
			id: "tie",
			family: "family-1",
			async judge() {
				return { outcome: "tie" };
			},
		};

		const tournament = await runTournament({
			id: "t-ties",
			question: "Which is best?",
			hypotheses,
			judges: [judge],
			swissRoundCap: 3,
			seed: 7,
		});

		expect(tournament.comparisons).toHaveLength(6);
		expect(tournament.comparisons.every(record => record.outcome === "tie")).toBe(true);
		expect(tournament.schedule).toEqual({ kind: "round-robin" });
		expect(tournament.version).toBe(1);
		expect(tournament.result.ranking.map(entry => entry.rank)).toEqual([1, 1, 1]);
		expect(tournament.result.ranking.map(entry => entry.score)).toEqual([0, 0, 0]);
		expect(tournament.result.familyAgreement).toBeUndefined();
	});

	it("ranks a strictly stronger candidate pool and records 20 comparisons for 5 hypotheses", async () => {
		const hypotheses = await buildHypotheses(5);
		const tournament = await runTournament({
			id: "t-ranking",
			question: "Pick the strongest hypothesis.",
			hypotheses,
			judges: [preferHigherStrengthJudge("judge-1", "family-1")],
			swissRoundCap: 3,
			seed: 42,
		});

		expect(tournament.result.ranking.map(entry => entry.id)).toEqual(["h1", "h2", "h3", "h4", "h5"]);
		expect(tournament.comparisons).toHaveLength(20);
		expect(tournament.comparisons.filter(record => record.judgeId === "judge-1")).toHaveLength(20);

		// Each of the 10 unordered pairs is judged exactly twice (both presentation orders).
		const byPair = new Map<string, ComparisonRecord[]>();
		for (const record of tournament.comparisons) {
			const key = pairKey(record.pair[0], record.pair[1]);
			byPair.set(key, [...(byPair.get(key) ?? []), record]);
		}
		expect(byPair.size).toBe(10);
		for (const records of byPair.values()) {
			expect(records).toHaveLength(2);
			const orders = new Set(records.map(record => record.presented.join(">")));
			expect(orders.size).toBe(2);
		}
	});
});

describe("runTournament swiss schedule", () => {
	it("keeps judge calls within the swiss bound and never replays a pair in a later round", async () => {
		const hypotheses = await buildHypotheses(20);
		const judge = preferHigherStrengthJudge("judge-1", "family-1");
		const tournament = await runTournament({
			id: "t-swiss",
			question: "Pick the strongest hypothesis.",
			hypotheses,
			judges: [judge],
			swissRoundCap: 5,
			seed: 2026,
		});

		expect(tournament.schedule).toEqual({ kind: "swiss", roundCap: 5 });
		const bound = maxJudgeCalls(tournament.schedule, 20, 1);
		expect(tournament.comparisons.length).toBeLessThanOrEqual(bound);

		const roundsByPair = new Map<string, Set<number>>();
		for (const record of tournament.comparisons) {
			const key = pairKey(record.pair[0], record.pair[1]);
			const rounds = roundsByPair.get(key) ?? new Set<number>();
			rounds.add(record.round);
			roundsByPair.set(key, rounds);
		}
		expect(roundsByPair.size).toBeGreaterThan(0);
		for (const rounds of roundsByPair.values()) {
			expect(rounds.size).toBe(1);
		}
	});
});

describe("runTournament judges and usage", () => {
	it("reports family agreement and totals the usage of two judge families", async () => {
		const hypotheses = await buildHypotheses(5);
		const judgeUsage = makeUsage({ input: 5, output: 3, totalTokens: 8 });
		const tournament = await runTournament({
			id: "t-families",
			question: "Pick the strongest hypothesis.",
			hypotheses,
			judges: [
				preferHigherStrengthJudge("judge-1", "family-1", judgeUsage),
				preferHigherStrengthJudge("judge-2", "family-2", judgeUsage),
			],
			swissRoundCap: 3,
			seed: 5,
		});

		expect(tournament.result.familyAgreement).toEqual({
			families: ["family-1", "family-2"],
			agreement: 1,
			pairs: 10,
		});
		// 20 calls per judge * 2 judges = 40 calls, each reporting input 5 / output 3.
		expect(tournament.comparisons).toHaveLength(40);
		expect(tournament.usage.input).toBe(40 * 5);
		expect(tournament.usage.output).toBe(40 * 3);
		expect(tournament.usage.totalTokens).toBe(40 * 8);
	});

	it("rejects a judge count outside 1..2 or a duplicated family", async () => {
		const hypotheses = await buildHypotheses(3);
		const base = {
			id: "t-invalid",
			question: "Which is best?",
			hypotheses,
			swissRoundCap: 3,
			seed: 1,
		};

		await expect(runTournament({ ...base, judges: [] })).rejects.toThrow();
		await expect(
			runTournament({
				...base,
				judges: [
					preferHigherStrengthJudge("a", "family-1"),
					preferHigherStrengthJudge("b", "family-2"),
					preferHigherStrengthJudge("c", "family-3"),
				],
			}),
		).rejects.toThrow();
		await expect(
			runTournament({
				...base,
				judges: [preferHigherStrengthJudge("a", "family-1"), preferHigherStrengthJudge("b", "family-1")],
			}),
		).rejects.toThrow(/distinct families/);
	});
});

describe("runTournament execution discipline", () => {
	it("calls judges sequentially and stops on an aborted signal", async () => {
		const hypotheses = await buildHypotheses(3);
		let inFlight = 0;
		let maxInFlight = 0;
		let calls = 0;
		const judge: TournamentJudge = {
			id: "sequential",
			family: "family-1",
			async judge() {
				inFlight += 1;
				maxInFlight = Math.max(maxInFlight, inFlight);
				await Bun.sleep(1);
				calls += 1;
				inFlight -= 1;
				return { outcome: "A" };
			},
		};

		const tournament = await runTournament({
			id: "t-sequential",
			question: "Which is best?",
			hypotheses,
			judges: [judge],
			swissRoundCap: 3,
			seed: 1,
		});
		expect(maxInFlight).toBe(1);
		expect(calls).toBe(6);
		expect(tournament.comparisons).toHaveLength(6);

		const controller = new AbortController();
		controller.abort();
		const abortedJudge: TournamentJudge = {
			id: "aborted",
			family: "family-1",
			async judge() {
				calls += 1;
				return { outcome: "A" };
			},
		};
		const before = calls;
		await expect(
			runTournament({
				id: "t-aborted",
				question: "Which is best?",
				hypotheses,
				judges: [abortedJudge],
				swissRoundCap: 3,
				seed: 1,
				signal: controller.signal,
			}),
		).rejects.toThrow();
		expect(calls).toBe(before);
	});

	it("stamps every comparison with the injected clock and is reproducible for a fixed seed", async () => {
		const hypotheses = await buildHypotheses(4);
		const timestamps: string[] = [];
		let tick = 0;
		const now = () => new Date(Date.UTC(2026, 0, 2, 0, 0, tick++));

		const run = () =>
			runTournament({
				id: "t-clock",
				question: "Which is best?",
				hypotheses,
				judges: [preferHigherStrengthJudge("judge-1", "family-1")],
				swissRoundCap: 3,
				seed: 99,
				now,
			});

		const first = await run();
		for (const record of first.comparisons) {
			timestamps.push(record.timestamp);
		}
		expect(timestamps.every(timestamp => timestamp.startsWith("2026-01-02T00:00:"))).toBe(true);

		const second = await runTournament({
			id: "t-clock",
			question: "Which is best?",
			hypotheses,
			judges: [preferHigherStrengthJudge("judge-1", "family-1")],
			swissRoundCap: 3,
			seed: 99,
			now: () => new Date(Date.UTC(2026, 0, 2, 0, 0, 0)),
		});
		expect(second.comparisons.map(record => record.presented)).toEqual(
			first.comparisons.map(record => record.presented),
		);
	});
});
