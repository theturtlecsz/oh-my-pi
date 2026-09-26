import { describe, expect, it } from "bun:test";
import { aggregateComparisons, replayTournament } from "../src/autoresearch/tournament/aggregate";
import {
	type ComparisonRecord,
	type Hypothesis,
	type JudgeOutcome,
	TOURNAMENT_LABEL,
	type Tournament,
} from "../src/autoresearch/tournament/types";

function hypothesis(id: string): Hypothesis {
	return {
		id,
		title: `Title ${id}`,
		writeUp: `Write-up ${id}`,
		judgeText: `Judge text ${id}`,
		summarized: true,
	};
}

interface ComparisonOptions {
	pair: [string, string];
	presented?: [string, string];
	outcome: JudgeOutcome;
	judgeId?: string;
	judgeFamily?: string;
	round?: number;
	timestamp?: string;
}

function comparison(options: ComparisonOptions): ComparisonRecord {
	return {
		pair: options.pair,
		presented: options.presented ?? options.pair,
		judgeId: options.judgeId ?? "judge-1",
		judgeFamily: options.judgeFamily ?? "family-1",
		outcome: options.outcome,
		round: options.round ?? 1,
		timestamp: options.timestamp ?? "2026-01-01T00:00:00.000Z",
	};
}

function stubTournament(hypotheses: Hypothesis[], comparisons: ComparisonRecord[]): Tournament {
	return {
		version: 1,
		id: "tournament-1",
		question: "Which hypothesis is best?",
		seed: 7,
		schedule: { kind: "round-robin" },
		hypotheses,
		comparisons,
		result: aggregateComparisons(hypotheses, comparisons),
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
	};
}

describe("autoresearch tournament aggregation", () => {
	it("flattens every hypothesis to the same score and rank when the judge always ties", () => {
		const hypotheses = [hypothesis("h1"), hypothesis("h2"), hypothesis("h3")];
		const comparisons = [
			comparison({ pair: ["h1", "h2"], outcome: "tie" }),
			comparison({ pair: ["h1", "h3"], outcome: "tie" }),
			comparison({ pair: ["h2", "h3"], outcome: "tie" }),
		];

		const result = aggregateComparisons(hypotheses, comparisons);

		expect(result.label).toBe(TOURNAMENT_LABEL);
		expect(result.ranking.map(entry => entry.score)).toEqual([0, 0, 0]);
		expect(result.ranking.map(entry => entry.rank)).toEqual([1, 1, 1]);
		expect(result.familyAgreement).toBeUndefined();
	});

	it("ranks a transitive preference h1 > h2 > ... > h5 when both presentation orders agree", () => {
		const ids = ["h1", "h2", "h3", "h4", "h5"];
		const hypotheses = ids.map(hypothesis);
		const comparisons: ComparisonRecord[] = [];
		for (let i = 0; i < ids.length; i++) {
			for (let j = i + 1; j < ids.length; j++) {
				const strong = ids[i];
				const weak = ids[j];
				comparisons.push(
					comparison({ pair: [strong, weak], presented: [strong, weak], outcome: "A" }),
					comparison({ pair: [strong, weak], presented: [weak, strong], outcome: "B" }),
				);
			}
		}

		const result = aggregateComparisons(hypotheses, comparisons);

		expect(result.ranking.map(entry => entry.id)).toEqual(ids);
		expect(result.ranking.map(entry => entry.rank)).toEqual([1, 2, 3, 4, 5]);
		expect(result.ranking[0].score).toBeGreaterThan(result.ranking[1].score);
		expect(result.ranking[0].comparisons).toBe(8);
		expect(result.ranking.every(entry => entry.spread > 0)).toBe(true);
	});

	it("produces identical output independent of comparison order and survives a JSON round-trip", () => {
		const hypotheses = [hypothesis("h1"), hypothesis("h2"), hypothesis("h3")];
		const comparisons = [
			comparison({ pair: ["h1", "h2"], presented: ["h1", "h2"], outcome: "A", round: 2 }),
			comparison({ pair: ["h1", "h3"], presented: ["h3", "h1"], outcome: "B", round: 1 }),
			comparison({ pair: ["h2", "h3"], presented: ["h2", "h3"], outcome: "A", round: 1 }),
			comparison({ pair: ["h1", "h2"], presented: ["h2", "h1"], outcome: "B", judgeId: "judge-2", round: 3 }),
		];
		const tournament = stubTournament(hypotheses, comparisons);

		const reversed = aggregateComparisons(hypotheses, [...comparisons].reverse());
		const rotated = aggregateComparisons(hypotheses, [...comparisons.slice(2), ...comparisons.slice(0, 2)]);

		expect(JSON.stringify(reversed)).toBe(JSON.stringify(tournament.result));
		expect(JSON.stringify(rotated)).toBe(JSON.stringify(tournament.result));

		const revived = JSON.parse(JSON.stringify(tournament)) as Tournament;
		expect(JSON.stringify(replayTournament(revived))).toBe(JSON.stringify(tournament.result));
	});

	it("agrees fully when two judge families rank every shared pair the same way", () => {
		const hypotheses = ["a", "b", "c", "d"].map(hypothesis);
		const pairs: Array<[string, string]> = [
			["a", "b"],
			["a", "c"],
			["a", "d"],
			["b", "c"],
		];
		const comparisons: ComparisonRecord[] = [];
		for (const pair of pairs) {
			comparisons.push(
				comparison({ pair, judgeId: "j1", judgeFamily: "family-1", outcome: "A" }),
				comparison({ pair, judgeId: "j2", judgeFamily: "family-2", outcome: "A" }),
			);
		}

		const result = aggregateComparisons(hypotheses, comparisons);

		expect(result.familyAgreement).toEqual({
			families: ["family-1", "family-2"],
			agreement: 1,
			pairs: 4,
		});
	});

	it("scores one disagreeing pair out of four as 0.75", () => {
		const hypotheses = ["a", "b", "c", "d"].map(hypothesis);
		const pairs: Array<[string, string]> = [
			["a", "b"],
			["a", "c"],
			["a", "d"],
			["b", "c"],
		];
		const comparisons: ComparisonRecord[] = [];
		for (const pair of pairs) {
			comparisons.push(comparison({ pair, judgeId: "j1", judgeFamily: "family-1", outcome: "A" }));
			if (pair[0] === "b" && pair[1] === "c") {
				comparisons.push(
					comparison({ pair, presented: ["c", "b"], judgeId: "j2", judgeFamily: "family-2", outcome: "A" }),
				);
			} else {
				comparisons.push(comparison({ pair, judgeId: "j2", judgeFamily: "family-2", outcome: "A" }));
			}
		}

		const result = aggregateComparisons(hypotheses, comparisons);

		expect(result.familyAgreement).toEqual({
			families: ["family-1", "family-2"],
			agreement: 0.75,
			pairs: 4,
		});
	});

	it("omits family agreement unless exactly two judge families are present", () => {
		const hypotheses = [hypothesis("a"), hypothesis("b")];

		const singleFamily = aggregateComparisons(hypotheses, [
			comparison({ pair: ["a", "b"], judgeFamily: "family-1", outcome: "A" }),
		]);
		const threeFamilies = aggregateComparisons(hypotheses, [
			comparison({ pair: ["a", "b"], judgeFamily: "family-1", outcome: "A" }),
			comparison({ pair: ["a", "b"], judgeFamily: "family-2", outcome: "A" }),
			comparison({ pair: ["a", "b"], judgeFamily: "family-3", outcome: "A" }),
		]);

		expect(singleFamily.familyAgreement).toBeUndefined();
		expect(threeFamilies.familyAgreement).toBeUndefined();
	});
});
