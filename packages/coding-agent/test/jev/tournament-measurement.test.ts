import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import type { Usage } from "@oh-my-pi/pi-ai";
import { renderTournamentReport } from "../../../../docs/reports/jev-measurement/run-tournament";
import {
	type LabeledPair,
	measureLabeledPairs,
	runTournamentMeasurement,
} from "../../../../docs/reports/jev-measurement/tournament-harness";
import { createJevJudge } from "../../src/autoresearch/tournament/jev-judge";
import { type HypothesisInput, prepareHypotheses } from "../../src/autoresearch/tournament/prepare";
import { maxJudgeCalls } from "../../src/autoresearch/tournament/schedule";
import type { JudgeOutcome, TournamentJudge } from "../../src/autoresearch/tournament/types";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

function makeUsage(totalCost: number): Usage {
	return {
		input: 40,
		output: 10,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 50,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: totalCost },
	};
}

/** Test chat judge: always picks the longer write-up, with distinct family. */
function makeChatJudge(family = "chat-test"): TournamentJudge {
	return {
		id: "chat-test",
		family,
		async judge(aText, bText): Promise<{ outcome: JudgeOutcome; usage: Usage }> {
			return { outcome: aText.length >= bText.length ? "A" : "B", usage: makeUsage(0.001) };
		},
	};
}

/** Test Jev-side judge that answers a fixed outcome, with a Jev usage total. */
function makeFixedJudge(outcome: JudgeOutcome, family = "jev-fixed"): TournamentJudge {
	return {
		id: family,
		family,
		async judge(): Promise<{ outcome: JudgeOutcome; usage: Usage }> {
			return { outcome, usage: makeUsage(0.0005) };
		},
	};
}

/** Test Jev-side judge whose every call throws (off-list / transport failure). */
function makeFailingJudge(family = "jev-fail"): TournamentJudge {
	return {
		id: family,
		family,
		async judge(): Promise<{ outcome: JudgeOutcome }> {
			throw new Error("Jev judge call failed or returned no answer");
		},
	};
}

describe("tournament measurement harness — labeled pairs", () => {
	it("scores order-collapsed agreement, order invariance, and inter-judge agreement", async () => {
		const pairs: LabeledPair[] = [
			{ id: "p1", textA: "longer write-up text", textB: "short", label: "A" },
			{ id: "p2", textA: "tiny", textB: "much longer write-up text", label: "B" },
		];

		const measured = await measureLabeledPairs(
			pairs,
			"Which is better?",
			makeChatJudge("jev-fixed"),
			makeChatJudge("chat-test"),
		);

		expect(measured.pairs).toBe(2);
		expect(measured.jev.agreementWithReference).toBe(1);
		expect(measured.chat.agreementWithReference).toBe(1);
		expect(measured.interJudgeAgreement).toBe(1);
		expect(measured.orderInvariance.jev).toBe(1);
		expect(measured.offListRate.jev).toBe(0);
	});

	it("drops a pair when a judge call throws and reports the failure rate", async () => {
		const pairs: LabeledPair[] = [{ id: "p1", textA: "a", textB: "b", label: "A" }];

		const measured = await measureLabeledPairs(pairs, "Q", makeFailingJudge("jev-fail"), makeChatJudge("chat-test"));

		// Both calls from the failing judge threw, so the pair is not scored,
		// and the failure is visible as the off-list / failed rate (2/2 = 1).
		expect(measured.pairs).toBe(0);
		expect(measured.offListRate.jev).toBe(1);
		expect(measured.offListRate.chat).toBe(0);
	});
});

describe("tournament measurement harness — 20-hypothesis benchmark", () => {
	it("runs both families over the same Swiss pool and reports cost, wall time, and schedule", async () => {
		const inputs: HypothesisInput[] = Array.from({ length: 20 }, (_, i) => ({
			id: `cand-${i + 1}`,
			title: `Candidate ${i + 1}`,
			writeUp: `Detailed hypothesis text for candidate ${i + 1}.`,
		}));
		const { hypotheses } = await prepareHypotheses(inputs, { maxJudgeChars: 2000 });

		const results = await runTournamentMeasurement({
			pairs: [{ id: "p1", textA: "a", textB: "b", label: "A" }],
			hypotheses,
			question: "Which mechanism explains the effect?",
			jev: makeFixedJudge("A", "jev-fixed"),
			chat: makeChatJudge("chat-test"),
			swissRoundCap: 5,
			seed: 2026,
		});

		expect(results.tournament20.poolSize).toBe(20);
		expect(results.tournament20.schedule).toEqual({ kind: "swiss", roundCap: 5 });

		const bound = maxJudgeCalls(results.tournament20.schedule, 20, 1);
		for (const stats of [results.tournament20.jev, results.tournament20.chat]) {
			expect(stats.comparisons).toBeGreaterThan(0);
			expect(stats.comparisons).toBeLessThanOrEqual(bound);
			expect(stats.failures).toBe(0);
			expect(stats.totalTokens).toBeGreaterThan(0);
			expect(stats.costUsd).toBeGreaterThan(0);
		}
	});

	it("records a failed judge call as a tie so the tournament completes", async () => {
		const inputs: HypothesisInput[] = Array.from({ length: 4 }, (_, i) => ({
			id: `c-${i + 1}`,
			title: `C ${i + 1}`,
			writeUp: `Hypothesis ${i + 1} write-up.`,
		}));
		const { hypotheses } = await prepareHypotheses(inputs, { maxJudgeChars: 2000 });

		const results = await runTournamentMeasurement({
			pairs: [],
			hypotheses,
			question: "Q",
			jev: makeFailingJudge("jev-fail"),
			chat: makeChatJudge("chat-test"),
			swissRoundCap: 5,
			seed: 1,
		});

		expect(results.tournament20.schedule).toEqual({ kind: "round-robin" });
		expect(results.tournament20.jev.failures).toBeGreaterThan(0);
		// Every failed call became a tie, so no hypothesis is dropped.
		expect(results.tournament20.jev.comparisons).toBe(12);
	});
});

describe("tournament measurement harness — stub Jev endpoint", () => {
	let stub: StubJevServer;

	beforeEach(() => {
		stub = startStubJevServer();
	});

	afterEach(() => {
		stub.stop();
	});

	function makeJevJudge() {
		return createJevJudge({
			deps: {
				getSetting: (p: string) => {
					if (p === "jev.enabled") return true;
					if (p === "jev.baseUrl") return stub.baseUrl;
					return undefined;
				},
				getApiKey: () => "test-key",
				recordUsage: () => {},
			},
		});
	}

	it("counts an off-list answer as a failed call, never a measured label", async () => {
		stub.setMode("off-list");
		const pairs: LabeledPair[] = [{ id: "p1", textA: "a", textB: "b", label: "A" }];

		const measured = await measureLabeledPairs(pairs, "Q", makeJevJudge(), makeChatJudge("chat-test"));

		expect(measured.pairs).toBe(0);
		expect(measured.offListRate.jev).toBe(1);
	});

	it("drives the real Jev judge through a stub endpoint over the labeled pairs", async () => {
		stub.setAnswers({ preference: { probabilities: { A: 0.8, B: 0.15, tie: 0.05 } } });
		const pairs: LabeledPair[] = [{ id: "p1", textA: "aa", textB: "bb", label: "A" }];

		const measured = await measureLabeledPairs(pairs, "Q", makeJevJudge(), makeFixedJudge("A", "chat-test"));

		expect(measured.pairs).toBe(1);
		expect(measured.jev.agreementWithReference).toBe(1);
		// The stub always answers slot A (option order), so when the write-ups
		// swap slots the canonical preference flips: the harness reports the
		// position bias as zero order invariance rather than hiding it.
		expect(measured.orderInvariance.jev).toBe(0);
		// Two calls per presentation order for the Jev judge.
		expect(stub.requests.length).toBe(2);
	});

	it("renders the report template with the measured results", async () => {
		stub.setAnswers({ preference: { probabilities: { A: 0.8, B: 0.15, tie: 0.05 } } });
		const inputs: HypothesisInput[] = Array.from({ length: 3 }, (_, i) => ({
			id: `c-${i + 1}`,
			title: `C ${i + 1}`,
			writeUp: `Write-up ${i + 1}.`,
		}));
		const { hypotheses } = await prepareHypotheses(inputs, { maxJudgeChars: 2000 });

		const results = await runTournamentMeasurement({
			pairs: [{ id: "p1", textA: "a", textB: "b", label: "A" }],
			hypotheses,
			question: "Q",
			jev: makeJevJudge(),
			chat: makeFixedJudge("B", "chat-test"),
			swissRoundCap: 5,
			seed: 3,
		});

		const rendered = renderTournamentReport(
			results,
			"pairs={{pairs_count}} jev={{jev_pair_agreement}} inter={{inter_judge_agreement}}",
		);
		expect(rendered).toBe("pairs=1 jev=100.0% inter=0.0%");
	});
});
