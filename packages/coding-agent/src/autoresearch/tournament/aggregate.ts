import {
	type ComparisonRecord,
	type FamilyAgreement,
	type Hypothesis,
	type RankedHypothesis,
	TOURNAMENT_LABEL,
	type Tournament,
	type TournamentResult,
} from "./types";

const BT_ITERATIONS = 500;
const ROUND_DECIMALS = 6;
const PRIOR_STRENGTH = 1;
const PRIOR_TIE_WINS = 0.5;

/**
 * Reduce a tournament's raw comparisons into a Bradley-Terry ranking.
 *
 * Comparisons are canonically ordered first, so the input array order never
 * influences the result. Each comparison contributes one game between the two
 * `presented` hypotheses (`A` wins `presented[0]`, `B` wins `presented[1]`, a
 * tie splits the credit). Every hypothesis also plays one virtual tie against a
 * fixed strength-1 pseudo-opponent, which keeps an undefeated hypothesis finite.
 */
export function aggregateComparisons(hypotheses: Hypothesis[], comparisons: ComparisonRecord[]): TournamentResult {
	const count = hypotheses.length;
	const index = new Map<string, number>();
	for (let i = 0; i < count; i++) index.set(hypotheses[i].id, i);

	const ordered = [...comparisons].sort(compareComparisons);
	const games: Float64Array[] = Array.from({ length: count }, () => new Float64Array(count));
	const wins = new Float64Array(count);
	const appearances = new Uint32Array(count);

	for (const record of ordered) {
		const involved = new Set<string>();
		for (const id of record.pair) involved.add(id);
		for (const id of record.presented) involved.add(id);
		for (const id of involved) {
			const i = index.get(id);
			if (i !== undefined) appearances[i] += 1;
		}

		const a = index.get(record.presented[0]);
		const b = index.get(record.presented[1]);
		if (a === undefined || b === undefined || a === b) continue;
		const weight = record.outcome === "A" ? 1 : record.outcome === "B" ? 0 : 0.5;
		wins[a] += weight;
		wins[b] += 1 - weight;
		games[a][b] += 1;
		games[b][a] += 1;
	}

	const strengths = fitStrengths(wins, games, count);
	let logMean = 0;
	for (let i = 0; i < count; i++) logMean += Math.log(strengths[i]);
	const geometricMean = count > 0 ? Math.exp(logMean / count) : 1;
	for (let i = 0; i < count; i++) strengths[i] /= geometricMean;

	const ranking: RankedHypothesis[] = hypotheses.map((hypothesis, i) => ({
		id: hypothesis.id,
		title: hypothesis.title,
		rank: 0,
		score: round(strengths[i] > 0 ? Math.log(strengths[i]) : 0),
		spread: round(fisherSpread(strengths, games, i)),
		comparisons: appearances[i],
	}));
	ranking.sort((a, b) => b.score - a.score || compareStrings(a.id, b.id));
	let rank = 0;
	let previous: number | null = null;
	for (const entry of ranking) {
		if (previous === null || entry.score !== previous) {
			rank += 1;
			previous = entry.score;
		}
		entry.rank = rank;
	}

	const result: TournamentResult = { label: TOURNAMENT_LABEL, ranking };
	const familyAgreement = computeFamilyAgreement(ordered);
	if (familyAgreement) result.familyAgreement = familyAgreement;
	return result;
}

export function replayTournament(tournament: Tournament): TournamentResult {
	return aggregateComparisons(tournament.hypotheses, tournament.comparisons);
}

function fitStrengths(wins: Float64Array, games: Float64Array[], count: number): Float64Array {
	let strengths = new Float64Array(count).fill(1);
	let next = new Float64Array(count);
	for (let iteration = 0; iteration < BT_ITERATIONS; iteration++) {
		for (let i = 0; i < count; i++) {
			const row = games[i];
			let denominator = 1 / (strengths[i] + PRIOR_STRENGTH);
			for (let j = 0; j < count; j++) {
				const played = row[j];
				if (played > 0) denominator += played / (strengths[i] + strengths[j]);
			}
			const numerator = wins[i] + PRIOR_TIE_WINS;
			next[i] = denominator > 0 ? numerator / denominator : strengths[i];
		}
		const swap = strengths;
		strengths = next;
		next = swap;
	}
	return strengths;
}

function fisherSpread(strengths: Float64Array, games: Float64Array[], i: number): number {
	const wi = strengths[i];
	let information = (wi * PRIOR_STRENGTH) / (wi + PRIOR_STRENGTH) ** 2;
	const row = games[i];
	for (let j = 0; j < strengths.length; j++) {
		const played = row[j];
		if (played > 0) information += (played * wi * strengths[j]) / (wi + strengths[j]) ** 2;
	}
	return information > 0 ? 1 / Math.sqrt(information) : 0;
}

function computeFamilyAgreement(ordered: ComparisonRecord[]): FamilyAgreement | undefined {
	const familySet = new Set<string>();
	for (const record of ordered) familySet.add(record.judgeFamily);
	if (familySet.size !== 2) return undefined;
	const families = [...familySet].sort(compareStrings);

	const byPair = new Map<string, Map<string, { sum: number; count: number }>>();
	for (const record of ordered) {
		const [first, second] = canonicalPair(record.pair);
		const key = `${first}\u0000${second}`;
		let byFamily = byPair.get(key);
		if (!byFamily) {
			byFamily = new Map();
			byPair.set(key, byFamily);
		}
		const score = preferenceFor(first, record);
		const entry = byFamily.get(record.judgeFamily);
		if (entry) {
			entry.sum += score;
			entry.count += 1;
		} else {
			byFamily.set(record.judgeFamily, { sum: score, count: 1 });
		}
	}

	let pairs = 0;
	let matching = 0;
	for (const byFamily of byPair.values()) {
		const left = byFamily.get(families[0]);
		const right = byFamily.get(families[1]);
		if (!left || !right) continue;
		pairs += 1;
		if (classifyPreference(left.sum / left.count) === classifyPreference(right.sum / right.count)) matching += 1;
	}

	return {
		families: [families[0], families[1]],
		agreement: pairs === 0 ? 0 : matching / pairs,
		pairs,
	};
}

function preferenceFor(first: string, record: ComparisonRecord): number {
	if (record.outcome === "tie") return 0.5;
	const winner = record.outcome === "A" ? record.presented[0] : record.presented[1];
	return winner === first ? 1 : 0;
}

function classifyPreference(mean: number): number {
	if (mean > 0.5) return 1;
	if (mean < 0.5) return -1;
	return 0;
}

function compareComparisons(a: ComparisonRecord, b: ComparisonRecord): number {
	const left = comparisonKey(a);
	const right = comparisonKey(b);
	const order =
		compareStrings(left[0], right[0]) ||
		compareStrings(left[1], right[1]) ||
		compareStrings(left[2], right[2]) ||
		compareStrings(left[3], right[3]) ||
		compareStrings(left[4], right[4]);
	if (order !== 0) return order;
	if (left[5] !== right[5]) return left[5] - right[5];
	const timestampOrder = compareStrings(left[6], right[6]);
	if (timestampOrder !== 0) return timestampOrder;
	const outcomeOrder = compareStrings(a.outcome, b.outcome);
	if (outcomeOrder !== 0) return outcomeOrder;
	return compareStrings(JSON.stringify(a.probabilities ?? null), JSON.stringify(b.probabilities ?? null));
}

function comparisonKey(record: ComparisonRecord): [string, string, string, string, string, number, string] {
	const [first, second] = canonicalPair(record.pair);
	return [first, second, record.presented[0], record.presented[1], record.judgeId, record.round, record.timestamp];
}

function canonicalPair(pair: [string, string]): [string, string] {
	return pair[0] <= pair[1] ? [pair[0], pair[1]] : [pair[1], pair[0]];
}

function compareStrings(a: string, b: string): number {
	return a < b ? -1 : a > b ? 1 : 0;
}

function round(value: number): number {
	const rounded = Math.round(value * 10 ** ROUND_DECIMALS) / 10 ** ROUND_DECIMALS;
	return rounded === 0 ? 0 : rounded;
}
