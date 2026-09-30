import type { Usage } from "@oh-my-pi/pi-ai";
import { prompt } from "@oh-my-pi/pi-utils";
import preferenceInstructions from "../../prompts/jev/tournament-preference.md" with { type: "text" };
import { createJevBreaker, decide, type JevDeps, type JevQuestions } from "../../tiny/jev-client";
import jevJudgeStateTemplate from "./jev-judge-state.md" with { type: "text" };
import type { JudgeOutcome, JudgeProbabilities, JudgeVerdict, TournamentJudge, TournamentJudgeOptions } from "./types";

export interface CreateJevJudgeOptions {
	id?: string;
	family?: string;
	deps?: Partial<JevDeps>;
}

/**
 * Maps probability distribution over choice options {"A", "B", "tie"} to a
 * deterministic {@link JudgeOutcome} and typed {@link JudgeProbabilities}.
 *
 * Strict invariants:
 * - If tie probability is >= both A and B, or if A and B are equal, outcome is "tie".
 * - If A is strictly greater than B, outcome is "A".
 * - If B is strictly greater than A, outcome is "B".
 * - Off-list options are impossible: only "A", "B", and "tie" are admitted.
 */
export function parseJevJudgeOutcome(probabilities: Record<string, number>): {
	outcome: JudgeOutcome;
	probabilities: JudgeProbabilities;
} {
	const pA = probabilities.A ?? 0;
	const pB = probabilities.B ?? 0;
	const pTie = probabilities.tie ?? 0;

	let outcome: JudgeOutcome;
	if (pTie >= pA && pTie >= pB) {
		outcome = "tie";
	} else if (pA > pB) {
		outcome = "A";
	} else if (pB > pA) {
		outcome = "B";
	} else {
		outcome = "tie";
	}

	const resultProbabilities: JudgeProbabilities = {
		A: pA,
		B: pB,
		...(probabilities.tie !== undefined ? { tie: pTie } : {}),
	};

	return { outcome, probabilities: resultProbabilities };
}

/**
 * Creates a TournamentJudge backed by the Jev typed decision client.
 *
 * Evaluates pairs of candidate hypotheses using Jev choice {A, B, tie} on
 * summary-form write-ups with identities blinded.
 */
export function createJevJudge(options?: CreateJevJudgeOptions): TournamentJudge {
	const id = options?.id ?? "jev";
	const family = options?.family ?? "jev";
	const baseDeps = options?.deps;
	const breaker = baseDeps?.breaker ?? createJevBreaker();

	return {
		id,
		family,
		async judge(aText: string, bText: string, judgeOptions: TournamentJudgeOptions): Promise<JudgeVerdict> {
			judgeOptions.signal?.throwIfAborted();

			const state = prompt.render(jevJudgeStateTemplate, {
				question: judgeOptions.question,
				textA: aText,
				textB: bText,
			});

			const effectiveDeps: JevDeps = {
				recordUsage: () => {},
				feature: "tournament_judge",
				...baseDeps,
				breaker,
				signal: judgeOptions.signal,
			};

			const questions: JevQuestions = {
				preference: {
					type: "choice",
					instructions: preferenceInstructions.trim(),
					options: ["A", "B", "tie"],
				},
			};

			const answers = await decide(state, questions, effectiveDeps);
			if (!answers) {
				judgeOptions.signal?.throwIfAborted();
				throw new Error("Jev judge call failed or returned no answer");
			}

			const answer = answers.preference ?? Object.values(answers)[0];
			if (!answer || !("probabilities" in answer)) {
				throw new Error("Jev judge response missing choice probabilities");
			}

			const { outcome, probabilities } = parseJevJudgeOutcome(answer.probabilities);

			// Input tokens approximated at ~4 characters per token; Jev pricing is $0.042 / MTok.
			const inputTokens = Math.ceil(state.length / 4);
			const totalCost = (inputTokens / 1_000_000) * 0.042;
			const usage: Usage = {
				input: inputTokens,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: inputTokens,
				cost: { input: totalCost, output: 0, cacheRead: 0, cacheWrite: 0, total: totalCost },
			};

			return {
				outcome,
				probabilities,
				usage,
			};
		},
	};
}
