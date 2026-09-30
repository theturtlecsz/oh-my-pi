/**
 * Tournament judge measurement harness (R1-J / OMP-301).
 *
 * Measures the R1 Jev tournament judge against the chat judge second family:
 *   1. Agreement on a labeled pair set, in both presentation orders.
 *   2. Cost and wall time for a 20-hypothesis Swiss tournament.
 *
 * The judges are injected, so the harness runs against the committed stub Jev
 * server in tests and against the real typed Jev endpoint (plus the real smol
 * model) in the owner slice. The harness itself fetches nothing and holds no
 * credential; the caller builds the judges and their transport.
 *
 * Nothing here is measured against a real system unless the caller supplies a
 * real judge. Every number is computed from the judge calls the caller drives.
 */

import type { Usage } from "@oh-my-pi/pi-ai";
import { runTournament } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/runner";
import type {
	Hypothesis,
	JudgeOutcome,
	JudgeVerdict,
	ScheduleRule,
	Tournament,
	TournamentJudge,
} from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/types";
import { createUsageTotals } from "@oh-my-pi/pi-coding-agent/utils/usage-totals";
import { percentile } from "./harness";

/** One labeled hypothesis pair: which write-up the reference judge prefers. */
export interface LabeledPair {
	id: string;
	textA: string;
	textB: string;
	/** Reference label in terms of `textA` / `textB`; `"tie"` for no preference. */
	label: JudgeOutcome;
}

/** Per-judge aggregate over one judge's calls. */
export interface JudgeCallStats {
	/** Calls attempted, including failures. */
	calls: number;
	/** Calls that threw (transport failure or off-list answer). */
	failures: number;
	totalTokens: number;
	costUsd: number;
	p50LatencyMs: number;
	p95LatencyMs: number;
}

export interface PairJudgeStats {
	/** Share of pairs whose canonical preference matches the reference label. */
	agreementWithReference: number;
	/** Share of pairs whose canonical preference is a tie. */
	tieRate: number;
	p50LatencyMs: number;
	p95LatencyMs: number;
}

export interface PairMeasurement {
	/** Pairs with a reference label and two successful calls per judge. */
	pairs: number;
	jev: PairJudgeStats;
	chat: PairJudgeStats;
	/** Share of pairs where the two judges reach the same canonical preference. */
	interJudgeAgreement: number;
	/** Share of pairs each judge classifies identically in both presentation orders. */
	orderInvariance: { jev: number; chat: number };
	/** Share of calls each judge answered off-list or failed. */
	offListRate: { jev: number; chat: number };
}

export interface TournamentBenchmarkStats {
	/** Comparisons recorded by the schedule (both orders per pair). */
	comparisons: number;
	/** Judge calls attempted. */
	calls: number;
	/** Calls that threw; each is recorded as a tie so the tournament completes. */
	failures: number;
	costUsd: number;
	totalTokens: number;
	wallTimeMs: number;
	p50LatencyMs: number;
	p95LatencyMs: number;
}

export interface TournamentMeasurementResults {
	labeledPairSet: PairMeasurement;
	tournament20: {
		question: string;
		poolSize: number;
		schedule: ScheduleRule;
		chat: TournamentBenchmarkStats;
		jev: TournamentBenchmarkStats;
	};
}

/** Running totals for one judge, kept while the caller drives it. */
class JudgeRecorder {
	calls = 0;
	failures = 0;
	totalTokens = 0;
	costUsd = 0;
	readonly latencies: number[] = [];

	constructor(private readonly now: () => number) {}

	/**
	 * Wraps a judge so every call's latency, usage, and failure land here.
	 * `onFailure` decides what a thrown call returns: the tournament path maps
	 * it to a tie so the schedule completes; the labeled-pair path rethrows so
	 * the pair is dropped rather than scored off a fabricated tie.
	 */
	instrument(judge: TournamentJudge, onFailure: () => JudgeVerdict): TournamentJudge {
		return {
			id: judge.id,
			family: judge.family,
			judge: async (aText, bText, options) => {
				const started = this.now();
				this.calls += 1;
				try {
					const verdict = await judge.judge(aText, bText, options);
					this.latencies.push(this.now() - started);
					if (verdict.usage) {
						this.totalTokens += verdict.usage.totalTokens;
						this.costUsd += verdict.usage.cost.total;
					}
					return verdict;
				} catch {
					this.failures += 1;
					this.latencies.push(this.now() - started);
					return onFailure();
				}
			},
		};
	}

	stats(): JudgeCallStats {
		return {
			calls: this.calls,
			failures: this.failures,
			totalTokens: this.totalTokens,
			costUsd: this.costUsd,
			p50LatencyMs: percentile(this.latencies, 0.5),
			p95LatencyMs: percentile(this.latencies, 0.95),
		};
	}
}

/**
 * Canonical preference for a pair, independent of presentation order.
 * `aPresentedFirst` is true when `textA` occupied slot A.
 */
function canonicalPreference(aPresentedFirst: boolean, outcome: JudgeOutcome): JudgeOutcome {
	if (outcome === "tie") return "tie";
	const winnerIsA = aPresentedFirst ? outcome === "A" : outcome === "B";
	return winnerIsA ? "A" : "B";
}

/**
 * Judge one labeled pair in both presentation orders with both judges.
 * Returns the two successful calls per judge, or `undefined` per call on throw.
 */
async function judgePair(
	pair: LabeledPair,
	question: string,
	jev: TournamentJudge,
	chat: TournamentJudge,
): Promise<{
	jevAb?: JudgeOutcome;
	jevBa?: JudgeOutcome;
	chatAb?: JudgeOutcome;
	chatBa?: JudgeOutcome;
}> {
	const call = async (judge: TournamentJudge, aText: string, bText: string): Promise<JudgeOutcome | undefined> => {
		try {
			const verdict = await judge.judge(aText, bText, { question });
			return verdict.outcome;
		} catch {
			return undefined;
		}
	};
	const [jevAb, jevBa, chatAb, chatBa] = await Promise.all([
		call(jev, pair.textA, pair.textB),
		call(jev, pair.textB, pair.textA),
		call(chat, pair.textA, pair.textB),
		call(chat, pair.textB, pair.textA),
	]);
	return { jevAb, jevBa, chatAb, chatBa };
}

function share(matching: number, total: number): number {
	return total === 0 ? 0 : matching / total;
}

/**
 * Measure agreement over a labeled pair set. Both judges see each pair in both
 * presentation orders; the canonical preference collapses the order, so a
 * position-biased judge shows up as low order invariance.
 */
export async function measureLabeledPairs(
	pairs: LabeledPair[],
	question: string,
	jev: TournamentJudge,
	chat: TournamentJudge,
	now: () => number = Date.now,
): Promise<PairMeasurement> {
	const jevRecorder = new JudgeRecorder(now);
	const chatRecorder = new JudgeRecorder(now);
	// A failed judge call is recorded by the recorder but rethrown here, so
	// `judgePair` drops the pair instead of scoring a fabricated tie.
	const jevInstrumented = jevRecorder.instrument(jev, () => {
		throw new Error("Judge call failed");
	});
	const chatInstrumented = chatRecorder.instrument(chat, () => {
		throw new Error("Judge call failed");
	});

	let considered = 0;
	let jevCorrect = 0;
	let chatCorrect = 0;
	let jevTies = 0;
	let chatTies = 0;
	let agree = 0;
	let jevInvariant = 0;
	let chatInvariant = 0;

	for (const pair of pairs) {
		const calls = await judgePair(pair, question, jevInstrumented, chatInstrumented);
		if (
			calls.jevAb === undefined ||
			calls.jevBa === undefined ||
			calls.chatAb === undefined ||
			calls.chatBa === undefined
		) {
			continue;
		}
		considered += 1;
		// Collapse presentations to a canonical preference for the pair.
		const jevAb = canonicalPreference(true, calls.jevAb);
		const jevBa = canonicalPreference(false, calls.jevBa);
		const chatAb = canonicalPreference(true, calls.chatAb);
		const chatBa = canonicalPreference(false, calls.chatBa);

		if (jevAb === pair.label) jevCorrect += 1;
		if (chatAb === pair.label) chatCorrect += 1;
		if (jevAb === "tie") jevTies += 1;
		if (chatAb === "tie") chatTies += 1;
		if (jevAb === chatAb) agree += 1;
		if (jevAb === jevBa) jevInvariant += 1;
		if (chatAb === chatBa) chatInvariant += 1;
	}

	const jevLat = jevRecorder.latencies;
	const chatLat = chatRecorder.latencies;
	return {
		pairs: considered,
		jev: {
			agreementWithReference: share(jevCorrect, considered),
			tieRate: share(jevTies, considered),
			p50LatencyMs: percentile(jevLat, 0.5),
			p95LatencyMs: percentile(jevLat, 0.95),
		},
		chat: {
			agreementWithReference: share(chatCorrect, considered),
			tieRate: share(chatTies, considered),
			p50LatencyMs: percentile(chatLat, 0.5),
			p95LatencyMs: percentile(chatLat, 0.95),
		},
		interJudgeAgreement: share(agree, considered),
		orderInvariance: {
			jev: share(jevInvariant, considered),
			chat: share(chatInvariant, considered),
		},
		offListRate: {
			jev: share(jevRecorder.failures, jevRecorder.calls),
			chat: share(chatRecorder.failures, chatRecorder.calls),
		},
	};
}

/** Run one 20-hypothesis Swiss tournament with a single judge, measuring cost and wall time. */
async function measureTournamentWith(
	judge: TournamentJudge,
	options: {
		id: string;
		question: string;
		hypotheses: Hypothesis[];
		swissRoundCap: number;
		seed: number;
		now: () => number;
	},
): Promise<{ tournament: Tournament; stats: TournamentBenchmarkStats }> {
	const recorder = new JudgeRecorder(options.now);
	const started = options.now();
	// A failed judge call is a measurement result, not a tournament abort:
	// record it as a tie so the schedule completes and the failure rate is
	// visible.
	const tournament = await runTournament({
		id: options.id,
		question: options.question,
		hypotheses: options.hypotheses,
		judges: [recorder.instrument(judge, () => ({ outcome: "tie" }))],
		swissRoundCap: options.swissRoundCap,
		seed: options.seed,
	});
	const wallTimeMs = options.now() - started;
	const calls = recorder.stats();
	return {
		tournament,
		stats: {
			comparisons: tournament.comparisons.length,
			calls: calls.calls,
			failures: calls.failures,
			// Prefer the tournament's own usage total, which the runner builds
			// from the verdicts the instrumented judge passed through.
			costUsd: tournament.usage.cost.total,
			totalTokens: tournament.usage.totalTokens,
			wallTimeMs,
			p50LatencyMs: calls.p50LatencyMs,
			p95LatencyMs: calls.p95LatencyMs,
		},
	};
}

export interface TournamentMeasurementOptions {
	pairs: LabeledPair[];
	/** Prepared hypotheses for the 20-hypothesis benchmark. */
	hypotheses: Hypothesis[];
	question: string;
	jev: TournamentJudge;
	chat: TournamentJudge;
	swissRoundCap: number;
	seed: number;
	now?: () => number;
}

/**
 * Measure both surfaces: labeled-pair agreement and the 20-hypothesis cost /
 * wall-time benchmark, with each judge in its own tournament.
 */
export async function runTournamentMeasurement(
	options: TournamentMeasurementOptions,
): Promise<TournamentMeasurementResults> {
	const now = options.now ?? Date.now;
	const labeledPairSet = await measureLabeledPairs(
		options.pairs,
		options.question,
		options.jev,
		options.chat,
		now,
	);
	const chat = await measureTournamentWith(options.chat, {
		id: "tournament-20-chat",
		question: options.question,
		hypotheses: options.hypotheses,
		swissRoundCap: options.swissRoundCap,
		seed: options.seed,
		now,
	});
	const jev = await measureTournamentWith(options.jev, {
		id: "tournament-20-jev",
		question: options.question,
		hypotheses: options.hypotheses,
		swissRoundCap: options.swissRoundCap,
		seed: options.seed,
		now,
	});
	return {
		labeledPairSet,
		tournament20: {
			question: options.question,
			poolSize: options.hypotheses.length,
			schedule: chat.tournament.schedule,
			chat: chat.stats,
			jev: jev.stats,
		},
	};
}

/** Empty usage totals, re-exported for the CLI's own accounting. */
export function emptyUsage(): Usage {
	return createUsageTotals();
}
