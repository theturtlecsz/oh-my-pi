import { addUsageTotals, createUsageTotals } from "../../utils/usage-totals";
import { aggregateComparisons } from "./aggregate";
import { type CandidateStanding, chooseScheduleRule, pairKey, roundRobinPairs, swissRoundPairs } from "./schedule";
import {
	type ComparisonRecord,
	createSeededRng,
	type Hypothesis,
	type Tournament,
	type TournamentJudge,
} from "./types";

export interface RunTournamentOptions {
	id: string;
	question: string;
	hypotheses: Hypothesis[];
	judges: TournamentJudge[];
	swissRoundCap: number;
	seed: number;
	now?: () => Date;
	signal?: AbortSignal;
}

interface JudgeCall {
	pair: [string, string];
	judge: TournamentJudge;
	presented: [string, string];
}

/**
 * Runs a blinded tournament over a candidate pool using injected judges.
 *
 * Every unordered pair is evaluated by every judge in both presentation orders
 * (position A and position B), and each verdict becomes one {@link ComparisonRecord}.
 * Judges only ever receive the two hypotheses' `judgeText` in slot order — never
 * their ids, titles, or full write-ups. Pool sizes of 8 or fewer run a single
 * full round-robin; larger pools run a Swiss schedule whose standings are
 * recomputed from the accumulated comparisons after each round.
 *
 * @throws {Error} If the judge list is not 1 or 2 entries, or when two judges share a family.
 * @throws {RangeError} If fewer than 2 hypotheses or an invalid Swiss round cap are supplied.
 */
export async function runTournament(options: RunTournamentOptions): Promise<Tournament> {
	const { hypotheses, judges, question, seed, signal } = options;
	if (judges.length < 1 || judges.length > 2) {
		throw new Error(`Tournament requires 1 or 2 judges, received ${judges.length}`);
	}
	if (judges.length === 2 && judges[0].family === judges[1].family) {
		throw new Error(`Tournament judges must belong to distinct families, received two "${judges[0].family}" judges`);
	}

	const now = options.now ?? (() => new Date());
	const rule = chooseScheduleRule(hypotheses.length, options.swissRoundCap);
	const rng = createSeededRng(seed);
	const byId = new Map(hypotheses.map(hypothesis => [hypothesis.id, hypothesis]));
	const comparisons: ComparisonRecord[] = [];
	const usage = createUsageTotals();

	const runRound = async (pairs: Array<[string, string]>, round: number): Promise<void> => {
		const calls: JudgeCall[] = [];
		for (const [first, second] of pairs) {
			for (const judge of judges) {
				calls.push({ pair: [first, second], judge, presented: [first, second] });
				calls.push({ pair: [first, second], judge, presented: [second, first] });
			}
		}
		shuffle(calls, rng);

		for (const call of calls) {
			signal?.throwIfAborted();
			const first = byId.get(call.presented[0]);
			const second = byId.get(call.presented[1]);
			if (!first || !second) {
				throw new Error(`Unknown hypothesis in presented pair: ${call.presented.join(", ")}`);
			}
			const verdict = await call.judge.judge(first.judgeText, second.judgeText, { question, signal });
			if (verdict.usage) {
				addUsageTotals(usage, verdict.usage);
			}
			const record: ComparisonRecord = {
				pair: canonicalPair(call.pair[0], call.pair[1]),
				presented: [call.presented[0], call.presented[1]],
				judgeId: call.judge.id,
				judgeFamily: call.judge.family,
				outcome: verdict.outcome,
				round,
				timestamp: now().toISOString(),
			};
			if (verdict.probabilities) {
				record.probabilities = verdict.probabilities;
			}
			comparisons.push(record);
		}
	};

	if (rule.kind === "round-robin") {
		await runRound(roundRobinPairs(hypotheses.map(hypothesis => hypothesis.id)), 1);
	} else {
		const played = new Set<string>();
		for (let round = 1; round <= rule.roundCap; round++) {
			const standings: CandidateStanding[] = aggregateComparisons(hypotheses, comparisons).ranking.map(entry => ({
				id: entry.id,
				score: entry.score,
			}));
			const pairs = swissRoundPairs(standings, played, rng);
			if (pairs.length === 0) {
				break;
			}
			for (const [first, second] of pairs) {
				played.add(pairKey(first, second));
			}
			await runRound(pairs, round);
		}
	}

	return {
		version: 1,
		id: options.id,
		question,
		seed,
		schedule: rule,
		hypotheses,
		comparisons,
		result: aggregateComparisons(hypotheses, comparisons),
		usage,
	};
}

function canonicalPair(first: string, second: string): [string, string] {
	return first <= second ? [first, second] : [second, first];
}

function shuffle<T>(items: T[], rng: () => number): void {
	for (let i = items.length - 1; i > 0; i--) {
		const j = Math.floor(rng() * (i + 1));
		const temp = items[i];
		items[i] = items[j];
		items[j] = temp;
	}
}
