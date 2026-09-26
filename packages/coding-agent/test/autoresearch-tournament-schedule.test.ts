import { describe, expect, it } from "bun:test";
import {
	chooseScheduleRule,
	maxJudgeCalls,
	pairKey,
	roundRobinPairs,
	swissRoundPairs,
} from "../src/autoresearch/tournament/schedule";
import { createSeededRng } from "../src/autoresearch/tournament/types";

describe("tournament schedule rule selection", () => {
	it("selects round-robin for candidate pools of size 8 or fewer", () => {
		expect(chooseScheduleRule(2, 5)).toEqual({ kind: "round-robin" });
		expect(chooseScheduleRule(5, 3)).toEqual({ kind: "round-robin" });
		expect(chooseScheduleRule(8, 4)).toEqual({ kind: "round-robin" });
	});

	it("selects swiss schedule with configured round cap for candidate pools larger than 8", () => {
		expect(chooseScheduleRule(9, 4)).toEqual({ kind: "swiss", roundCap: 4 });
		expect(chooseScheduleRule(20, 5)).toEqual({ kind: "swiss", roundCap: 5 });
	});

	it("rejects candidate count below 2", () => {
		expect(() => chooseScheduleRule(1, 5)).toThrow(RangeError);
		expect(() => chooseScheduleRule(0, 5)).toThrow(RangeError);
		expect(() => chooseScheduleRule(-3, 5)).toThrow(RangeError);
	});

	it("rejects swiss round cap below 1", () => {
		expect(() => chooseScheduleRule(5, 0)).toThrow(RangeError);
		expect(() => chooseScheduleRule(10, 0)).toThrow(RangeError);
		expect(() => chooseScheduleRule(10, -1)).toThrow(RangeError);
	});
});

describe("pairKey helper", () => {
	it("produces invariant keys regardless of operand order using sorted null-separated IDs", () => {
		expect(pairKey("alpha", "beta")).toBe("alpha\u0000beta");
		expect(pairKey("beta", "alpha")).toBe("alpha\u0000beta");
	});
});

describe("round-robin pairings", () => {
	it("generates exactly 10 distinct id-sorted pairs for 5 candidates", () => {
		const ids = ["h1", "h2", "h3", "h4", "h5"];
		const pairs = roundRobinPairs(ids);

		expect(pairs).toHaveLength(10);

		// Every tuple must be id-sorted
		for (const [a, b] of pairs) {
			expect(a < b).toBe(true);
		}

		// All pairs must be unique
		const keys = new Set(pairs.map(([a, b]) => pairKey(a, b)));
		expect(keys.size).toBe(10);
	});

	it("normalizes unsorted input order into canonically sorted pairs", () => {
		const scrambled = ["h4", "h1", "h3", "h2"];
		const pairs = roundRobinPairs(scrambled);

		expect(pairs).toEqual([
			["h1", "h2"],
			["h1", "h3"],
			["h1", "h4"],
			["h2", "h3"],
			["h2", "h4"],
			["h3", "h4"],
		]);
	});

	it("returns empty array for fewer than 2 candidates", () => {
		expect(roundRobinPairs([])).toEqual([]);
		expect(roundRobinPairs(["only-one"])).toEqual([]);
	});
});

describe("maxJudgeCalls calculation", () => {
	it("calculates round-robin upper bound as n * (n - 1) * judgeCount", () => {
		const rule = { kind: "round-robin" as const };
		// 5 candidates, 1 judge: 5 * 4 * 1 = 20 calls (evaluating both presentation orders)
		expect(maxJudgeCalls(rule, 5, 1)).toBe(20);
		// 5 candidates, 2 judges: 5 * 4 * 2 = 40 calls
		expect(maxJudgeCalls(rule, 5, 2)).toBe(40);
	});

	it("calculates swiss upper bound as roundCap * floor(n / 2) * 2 * judgeCount", () => {
		const rule = { kind: "swiss" as const, roundCap: 5 };
		// 20 candidates, 5 rounds, 1 judge: 5 * 10 * 2 * 1 = 100 calls
		expect(maxJudgeCalls(rule, 20, 1)).toBe(100);
		// 20 candidates, 5 rounds, 2 judges: 5 * 10 * 2 * 2 = 200 calls
		expect(maxJudgeCalls(rule, 20, 2)).toBe(200);
	});

	it("handles boundary candidate and judge counts safely", () => {
		const rule = { kind: "round-robin" as const };
		expect(maxJudgeCalls(rule, 1, 1)).toBe(0);
		expect(maxJudgeCalls(rule, 5, 0)).toBe(0);
	});
});

describe("swiss round pairings", () => {
	it("produces identical round 1 pairings for identical seeds", () => {
		const ids = Array.from({ length: 12 }, (_, i) => `h${i + 1}`);
		const standings = ids.map(id => ({ id, score: 0 }));
		const played = new Set<string>();

		const pairs1 = swissRoundPairs(standings, played, createSeededRng(42));
		const pairs2 = swissRoundPairs(standings, played, createSeededRng(42));

		expect(pairs1).toEqual(pairs2);
	});

	it("produces diverging round 1 pairings for different seeds", () => {
		const ids = Array.from({ length: 12 }, (_, i) => `h${i + 1}`);
		const standings = ids.map(id => ({ id, score: 0 }));
		const played = new Set<string>();

		const pairsA = swissRoundPairs(standings, played, createSeededRng(42));
		const pairsB = swissRoundPairs(standings, played, createSeededRng(99));

		expect(pairsA).not.toEqual(pairsB);
	});

	it("simulates 5 rounds for 20 candidates without repeated pairs or exceeding round cap bounds", () => {
		const n = 20;
		const roundCap = 5;
		const judgeCount = 1;
		const rule = chooseScheduleRule(n, roundCap);
		expect(rule).toEqual({ kind: "swiss", roundCap: 5 });

		const maxCalls = maxJudgeCalls(rule, n, judgeCount);
		expect(maxCalls).toBe(100);

		const rng = createSeededRng(2026);
		const ids = Array.from({ length: n }, (_, i) => `hypo-${i + 1}`);
		const standings = ids.map(id => ({ id, score: 0 }));
		const played = new Set<string>();
		let totalJudgeCalls = 0;

		for (let round = 1; round <= roundCap; round++) {
			const roundPairs = swissRoundPairs(standings, played, rng);

			// Must not exceed floor(n / 2) pairs per round
			expect(roundPairs.length).toBeLessThanOrEqual(Math.floor(n / 2));
			expect(roundPairs.length).toBe(10);

			for (const [a, b] of roundPairs) {
				const key = pairKey(a, b);
				// Must not replay an already-played matchup
				expect(played.has(key)).toBe(false);
				played.add(key);

				// Simulate round outcome: award winner 1 point
				const entryA = standings.find(s => s.id === a);
				if (entryA) entryA.score += 1;
			}

			// Each pair is evaluated in both presentation orders by each judge
			totalJudgeCalls += roundPairs.length * 2 * judgeCount;
		}

		// Total judge calls must not exceed theoretical upper bound
		expect(totalJudgeCalls).toBeLessThanOrEqual(maxCalls);
		expect(totalJudgeCalls).toBe(100);
		// All 50 pairs across 5 rounds must be unique
		expect(played.size).toBe(50);
	});

	it("pairs highest-scoring candidates together when eligible", () => {
		const standings = [
			{ id: "h1", score: 10 },
			{ id: "h2", score: 10 },
			{ id: "h3", score: 2 },
			{ id: "h4", score: 1 },
		];
		const played = new Set<string>();
		const rng = createSeededRng(1);

		const pairs = swissRoundPairs(standings, played, rng);
		expect(pairs).toHaveLength(2);

		// h1 and h2 (both score 10) must be paired against each other
		const topPairKeys = new Set([pairKey(pairs[0][0], pairs[0][1])]);
		expect(topPairKeys.has(pairKey("h1", "h2"))).toBe(true);
	});

	it("assigns byes to candidates with no legal remaining opponents", () => {
		const standings = [
			{ id: "h1", score: 5 },
			{ id: "h2", score: 4 },
			{ id: "h3", score: 3 },
		];
		// Odd number of candidates: floor(3 / 2) = 1 pair; 1 candidate receives a bye
		const played = new Set<string>();
		const rng = createSeededRng(123);

		const pairs = swissRoundPairs(standings, played, rng);
		expect(pairs).toHaveLength(1);

		// If h1 and h2 have already played each other, h1 pairs with h3 and h2 gets a bye
		played.add(pairKey("h1", "h2"));
		const nextPairs = swissRoundPairs(standings, played, rng);
		expect(nextPairs).toHaveLength(1);
		expect(pairKey(nextPairs[0][0], nextPairs[0][1])).toBe(pairKey("h1", "h3"));
	});
});
