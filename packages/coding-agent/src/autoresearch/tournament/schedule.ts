import type { ScheduleRule } from "./types";

/**
 * Derives a canonical key for a pair of hypothesis IDs to track played matchups.
 * The key joins sorted IDs with a null byte so comparison order is invariant.
 */
export function pairKey(a: string, b: string): string {
	return a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`;
}

/**
 * Selects the tournament schedule rule based on candidate pool size.
 * For pools of 8 or fewer, a full round-robin tournament is scheduled.
 * For pools larger than 8, a Swiss-style tournament capped at `swissRoundCap` rounds is selected.
 *
 * @throws {RangeError} If candidate count n < 2 or swissRoundCap < 1.
 */
export function chooseScheduleRule(n: number, swissRoundCap: number): ScheduleRule {
	if (!Number.isFinite(n) || n < 2) {
		throw new RangeError(`Tournament candidate count must be at least 2, got ${n}`);
	}
	if (!Number.isFinite(swissRoundCap) || swissRoundCap < 1) {
		throw new RangeError(`Swiss round cap must be at least 1, got ${swissRoundCap}`);
	}
	if (n <= 8) {
		return { kind: "round-robin" };
	}
	return { kind: "swiss", roundCap: swissRoundCap };
}

/**
 * Computes all unique pairings for a round-robin tournament.
 * Every unordered pair of candidates is returned exactly once, with each tuple id-sorted.
 */
export function roundRobinPairs(ids: string[]): Array<[string, string]> {
	const sorted = [...new Set(ids)].sort();
	const pairs: Array<[string, string]> = [];
	for (let i = 0; i < sorted.length; i++) {
		for (let j = i + 1; j < sorted.length; j++) {
			pairs.push([sorted[i], sorted[j]]);
		}
	}
	return pairs;
}

export interface CandidateStanding {
	id: string;
	score: number;
}

/**
 * Pairs candidates for a Swiss tournament round.
 * Candidates are sorted descending by score, with ties broken by a seeded Fisher-Yates shuffle.
 * Unpaired entries are greedily paired with the next available opponent they have not yet played.
 * Candidates with no legal remaining opponent receive a bye for the round.
 *
 * Returns at most floor(n / 2) pairings without rematches.
 */
export function swissRoundPairs(
	standings: CandidateStanding[],
	played: ReadonlySet<string>,
	rng: () => number,
): Array<[string, string]> {
	const pool = [...standings].sort((a, b) => b.score - a.score);

	// Break ties among equal-scoring candidates using a seeded Fisher-Yates shuffle
	let start = 0;
	while (start < pool.length) {
		let end = start + 1;
		while (end < pool.length && pool[end].score === pool[start].score) {
			end++;
		}
		const count = end - start;
		if (count > 1) {
			for (let i = count - 1; i > 0; i--) {
				const j = Math.floor(rng() * (i + 1));
				const temp = pool[start + i];
				pool[start + i] = pool[start + j];
				pool[start + j] = temp;
			}
		}
		start = end;
	}

	const pairs: Array<[string, string]> = [];
	const paired = new Set<string>();

	for (let i = 0; i < pool.length; i++) {
		const entry = pool[i];
		if (paired.has(entry.id)) {
			continue;
		}

		let opponent: CandidateStanding | undefined;
		for (let j = i + 1; j < pool.length; j++) {
			const candidate = pool[j];
			if (candidate.id === entry.id || paired.has(candidate.id)) {
				continue;
			}
			const key = pairKey(entry.id, candidate.id);
			if (!played.has(key)) {
				opponent = candidate;
				break;
			}
		}

		if (opponent) {
			paired.add(entry.id);
			paired.add(opponent.id);
			pairs.push([entry.id, opponent.id]);
		}
	}

	return pairs;
}

/**
 * Calculates the theoretical upper bound of judge calls for a given schedule rule,
 * candidate count, and number of participating judges.
 *
 * For round-robin: n * (n - 1) * judgeCount (every pair evaluated in both presentation orders).
 * For swiss: roundCap * floor(n / 2) * 2 * judgeCount.
 */
export function maxJudgeCalls(rule: ScheduleRule, n: number, judgeCount: number): number {
	if (n < 2 || judgeCount < 1) {
		return 0;
	}
	if (rule.kind === "round-robin") {
		return n * (n - 1) * judgeCount;
	}
	return rule.roundCap * Math.floor(n / 2) * 2 * judgeCount;
}
