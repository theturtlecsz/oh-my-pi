import type { Usage } from "@oh-my-pi/pi-ai";

export const TOURNAMENT_LABEL = "search aid, not a validity certificate";

export interface Hypothesis {
	id: string;
	title: string;
	writeUp: string;
	judgeText: string;
	summarized: boolean;
}

export type JudgeOutcome = "A" | "B" | "tie";

export interface JudgeProbabilities {
	A: number;
	B: number;
	tie?: number;
}

export interface JudgeVerdict {
	outcome: JudgeOutcome;
	probabilities?: JudgeProbabilities;
	usage?: Usage;
}

export interface TournamentJudgeOptions {
	question: string;
	signal?: AbortSignal;
}

export interface TournamentJudge {
	id: string;
	family: string;
	judge(aText: string, bText: string, options: TournamentJudgeOptions): Promise<JudgeVerdict>;
}

export interface ComparisonRecord {
	pair: [string, string];
	presented: [string, string];
	judgeId: string;
	judgeFamily: string;
	outcome: JudgeOutcome;
	probabilities?: JudgeProbabilities;
	round: number;
	timestamp: string;
}

export type ScheduleRule = { kind: "round-robin" } | { kind: "swiss"; roundCap: number };

export interface RankedHypothesis {
	id: string;
	title: string;
	rank: number;
	score: number;
	spread: number;
	comparisons: number;
}

export interface FamilyAgreement {
	families: [string, string];
	agreement: number;
	pairs: number;
}

export interface TournamentResult {
	label: string;
	ranking: RankedHypothesis[];
	familyAgreement?: FamilyAgreement;
}

export interface Tournament {
	version: 1;
	id: string;
	question: string;
	seed: number;
	schedule: ScheduleRule;
	hypotheses: Hypothesis[];
	comparisons: ComparisonRecord[];
	result: TournamentResult;
	usage: Usage;
}

/**
 * Deterministic PRNG using the Mulberry32 algorithm.
 * Generates uniformly distributed pseudo-random numbers in [0, 1).
 */
export function createSeededRng(seed: number): () => number {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) | 0;
		let t = Math.imul(state ^ (state >>> 15), 1 | state);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}
