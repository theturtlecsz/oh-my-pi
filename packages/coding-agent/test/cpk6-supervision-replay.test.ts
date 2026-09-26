import { describe, expect, it } from "bun:test";
import {
	CPK6_SCHEMA,
	CpkSupervisionError,
	type CpkSupervisionErrorCode,
	type CpkSupervisionProposal,
} from "../src/extensibility/cpk/supervision-proposal";
import {
	type CpkSupervisionArm,
	type CpkSupervisionReplaySession,
	replayCpkSupervision,
} from "../src/extensibility/cpk/supervision-replay";

async function expectAsyncSupervisionErrorCode(
	promise: Promise<unknown>,
	code: CpkSupervisionErrorCode,
): Promise<void> {
	try {
		await promise;
	} catch (err) {
		if (err instanceof CpkSupervisionError) {
			expect(err.code).toBe(code);
			return;
		}
		throw err;
	}
	throw new Error(`expected CpkSupervisionError with code "${code}" to be thrown`);
}

function createEmptyArm(name = "empty-arm"): CpkSupervisionArm {
	return {
		name,
		start: () => ({
			onEvent: () => [],
		}),
	};
}

describe("CPK-6 supervision historical replay (OMP-208-s02-s01)", () => {
	it("two [] arms on labelled sessions -> zeroRegression false, regressed, uncaught = every label", async () => {
		const session1: CpkSupervisionReplaySession = {
			id: "session-1",
			events: [{ type: "read" }, { type: "write" }],
			expected: [
				{ ruleClass: "gate-defect", transcriptIndex: 0 },
				{ ruleClass: "semantic-concern", transcriptIndex: 1 },
			],
		};

		const session2: CpkSupervisionReplaySession = {
			id: "session-2",
			events: [{ type: "test" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect", "semantic-concern"],
			sessions: [session1, session2],
			legacy: createEmptyArm("legacy-empty"),
			candidate: createEmptyArm("candidate-empty"),
		});

		expect(report.zeroRegression).toBe(false);
		expect(report.status).toBe("regressed");
		expect(report.regressions).toEqual([]);
		expect(report.corrections).toEqual([]);

		// uncaught equals every expected label across all sessions in session order, then label order
		expect(report.uncaught).toEqual([
			{ sessionId: "session-1", ruleClass: "gate-defect", transcriptIndex: 0 },
			{ sessionId: "session-1", ruleClass: "semantic-concern", transcriptIndex: 1 },
			{ sessionId: "session-2", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);

		expect(report.classes["gate-defect"]).toEqual({
			expected: 2,
			legacy: { caught: 0, missed: 2, falsePositives: 0, invalid: 0 },
			candidate: { caught: 0, missed: 2, falsePositives: 0, invalid: 0 },
		});
		expect(report.classes["semantic-concern"]).toEqual({
			expected: 1,
			legacy: { caught: 0, missed: 1, falsePositives: 0, invalid: 0 },
			candidate: { caught: 0, missed: 1, falsePositives: 0, invalid: 0 },
		});
	});

	it("an async arm (await Bun.sleep(0)) catches like its sync twin", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "sync-async-session",
			events: [{ type: "action" }, { type: "reaction" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const syncArm: CpkSupervisionArm = {
			name: "sync-arm",
			start: () => ({
				onEvent: (_event, index) => {
					if (index === 0) {
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 0,
								severity: "nit",
								claim: "synchronous catch",
							},
						];
					}
					return [];
				},
			}),
		};

		const asyncArm: CpkSupervisionArm = {
			name: "async-arm",
			start: async () => {
				await Bun.sleep(0);
				return {
					onEvent: async (_event, index) => {
						await Bun.sleep(0);
						if (index === 0) {
							return [
								{
									schema: CPK6_SCHEMA,
									ruleClass: "gate-defect",
									transcriptIndex: 0,
									severity: "nit",
									claim: "asynchronous catch",
								},
							];
						}
						return [];
					},
				};
			},
		};

		const syncReport = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: syncArm,
			candidate: syncArm,
		});

		const asyncReport = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: syncArm,
			candidate: asyncArm,
		});

		expect(syncReport.classes["gate-defect"].candidate).toEqual({
			caught: 1,
			missed: 0,
			falsePositives: 0,
			invalid: 0,
		});
		expect(asyncReport.classes["gate-defect"].candidate).toEqual({
			caught: 1,
			missed: 0,
			falsePositives: 0,
			invalid: 0,
		});
		expect(asyncReport.status).toBe("qualified");
		expect(asyncReport.zeroRegression).toBe(true);
		expect(asyncReport.regressions).toEqual([]);
		expect(asyncReport.uncaught).toEqual([]);
	});

	it("output at event 3 with transcriptIndex 1 catches a label at 1", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "delayed-catch-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }, { type: "e3" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 1 }],
		};

		const delayedArm: CpkSupervisionArm = {
			name: "delayed-catch-arm",
			start: () => ({
				onEvent: (_event, index) => {
					// Proposal emitted at event 3 targeting transcriptIndex 1
					if (index === 3) {
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 1,
								severity: "concern",
								claim: "retroactive observation at index 1",
							},
						];
					}
					return [];
				},
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: delayedArm,
			candidate: delayedArm,
		});

		expect(report.classes["gate-defect"].candidate.caught).toBe(1);
		expect(report.classes["gate-defect"].candidate.missed).toBe(0);
		expect(report.classes["gate-defect"].candidate.falsePositives).toBe(0);
		expect(report.classes["gate-defect"].candidate.invalid).toBe(0);
		expect(report.uncaught).toEqual([]);
		expect(report.status).toBe("qualified");
		expect(report.zeroRegression).toBe(true);
	});

	it("{ruleClass:'not-a-real-class'} at a label -> invalid 1, caught 0", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "invalid-output-session",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const invalidArm: CpkSupervisionArm = {
			name: "invalid-arm",
			start: () => ({
				onEvent: () => [{ ruleClass: "not-a-real-class" }],
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: invalidArm,
			candidate: createEmptyArm("candidate-empty"),
		});

		expect(report.classes["gate-defect"].legacy.invalid).toBe(1);
		expect(report.classes["gate-defect"].legacy.caught).toBe(0);
		expect(report.classes["gate-defect"].legacy.missed).toBe(1);
		expect(report.classes["gate-defect"].legacy.falsePositives).toBe(0);
	});

	it("no sessions, or a class unlabelled -> insufficient_evidence; label outside classes -> invalid_sentinel", async () => {
		// Subtest A: No sessions
		const noSessionsReport = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [],
			legacy: createEmptyArm(),
			candidate: createEmptyArm(),
		});
		expect(noSessionsReport.status).toBe("insufficient_evidence");
		expect(noSessionsReport.zeroRegression).toBe(false);

		// Subtest B: Class unlabelled
		const unlabelledSession: CpkSupervisionReplaySession = {
			id: "unlabelled-session",
			events: [{ type: "step" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};
		const unlabelledReport = await replayCpkSupervision({
			classes: ["gate-defect", "model-procedure-miss"],
			sessions: [unlabelledSession],
			legacy: createEmptyArm(),
			candidate: createEmptyArm(),
		});
		expect(unlabelledReport.status).toBe("insufficient_evidence");
		expect(unlabelledReport.zeroRegression).toBe(false);
		expect(unlabelledReport.classes["model-procedure-miss"].expected).toBe(0);

		// Subtest C: Label outside classes throws invalid_sentinel before arm is started
		let armStarted = false;
		const probeArm: CpkSupervisionArm = {
			name: "probe",
			start: () => {
				armStarted = true;
				return { onEvent: () => [] };
			},
		};
		const outsideLabelSession: CpkSupervisionReplaySession = {
			id: "outside-class-session",
			events: [{ type: "step" }],
			expected: [{ ruleClass: "semantic-concern", transcriptIndex: 0 }],
		};
		await expectAsyncSupervisionErrorCode(
			replayCpkSupervision({
				classes: ["gate-defect"], // semantic-concern is outside classes
				sessions: [outsideLabelSession],
				legacy: probeArm,
				candidate: probeArm,
			}),
			"invalid_sentinel",
		);
		expect(armStarted).toBe(false);
	});

	it("candidate missing a legacy catch -> that regressions element; extra unmatched output -> noisier", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "divergence-session",
			events: [{ type: "step-0" }, { type: "step-1" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const catchingLegacy: CpkSupervisionArm = {
			name: "catching-legacy",
			start: () => ({
				onEvent: (_event, index) => {
					if (index === 0) {
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 0,
								severity: "nit",
								claim: "legacy caught",
							},
						];
					}
					return [];
				},
			}),
		};

		// Subtest A: Candidate misses legacy catch -> regressions element and regressed status
		const regressedReport = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: catchingLegacy,
			candidate: createEmptyArm("empty-candidate"),
		});

		expect(regressedReport.status).toBe("regressed");
		expect(regressedReport.zeroRegression).toBe(false);
		expect(regressedReport.regressions).toEqual([
			{ sessionId: "divergence-session", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);

		// Subtest B: Candidate catches plus extra unmatched output -> noisier status
		const noisyCandidate: CpkSupervisionArm = {
			name: "noisy-candidate",
			start: () => ({
				onEvent: (_event, index) => {
					if (index === 0) {
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 0,
								severity: "nit",
								claim: "candidate caught",
							},
						];
					}
					if (index === 1) {
						// Extra proposal targeting index 1 which has no label
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 1,
								severity: "nit",
								claim: "unmatched false positive",
							},
						];
					}
					return [];
				},
			}),
		};

		const noisierReport = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: catchingLegacy,
			candidate: noisyCandidate,
		});

		expect(noisierReport.status).toBe("noisier");
		expect(noisierReport.zeroRegression).toBe(true);
		expect(noisierReport.regressions).toEqual([]);
		expect(noisierReport.uncaught).toEqual([]);
		expect(noisierReport.classes["gate-defect"].legacy.falsePositives).toBe(0);
		expect(noisierReport.classes["gate-defect"].candidate.falsePositives).toBe(1);
	});

	it("mutating a report leaves the next one intact; invocations == events.length per session (arm-side counters)", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "immutability-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }, { type: "e3" }, { type: "e4" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 2 }],
		};

		let legacyCalls = 0;
		let candidateCalls = 0;
		const legacyArm: CpkSupervisionArm = {
			name: "legacy-arm",
			start: () => ({
				onEvent: () => {
					legacyCalls++;
					return [];
				},
			}),
		};
		const candidateArm: CpkSupervisionArm = {
			name: "candidate-arm",
			start: () => ({
				onEvent: () => {
					candidateCalls++;
					return [];
				},
			}),
		};

		const options = {
			classes: ["gate-defect" as const],
			sessions: [session],
			legacy: legacyArm,
			candidate: candidateArm,
		};

		const report1 = await replayCpkSupervision(options);

		// Verify invocations == events.length per session (and arm-side counters match)
		expect(report1.invocations.legacy).toBe(session.events.length);
		expect(report1.invocations.candidate).toBe(session.events.length);
		expect(legacyCalls).toBe(session.events.length);
		expect(candidateCalls).toBe(session.events.length);
		// Invocations keyed strictly by role, never by arm.name
		expect(Object.keys(report1.invocations).sort()).toEqual(["candidate", "legacy"]);

		// Mutate report1 deeply
		report1.classes["gate-defect"].expected = 9999;
		report1.classes["gate-defect"].legacy.caught = 9999;
		report1.classes["gate-defect"].candidate.missed = 9999;
		report1.regressions.push({
			sessionId: "injected-session",
			ruleClass: "gate-defect",
			transcriptIndex: 99,
		});
		report1.uncaught.length = 0;
		report1.zeroRegression = true;
		report1.status = "qualified";

		// Second invocation with same inputs
		const report2 = await replayCpkSupervision(options);

		expect(report2.classes["gate-defect"].expected).toBe(1);
		expect(report2.classes["gate-defect"].legacy.caught).toBe(0);
		expect(report2.classes["gate-defect"].candidate.missed).toBe(1);
		expect(report2.regressions).toEqual([]);
		expect(report2.uncaught).toEqual([
			{ sessionId: "immutability-session", ruleClass: "gate-defect", transcriptIndex: 2 },
		]);
		expect(report2.zeroRegression).toBe(false);
		expect(report2.status).toBe("regressed");
	});

	it("tracks corrections when candidate catches a defect that legacy missed", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "correction-session",
			events: [{ type: "test" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const candidateArm: CpkSupervisionArm = {
			name: "fixing-candidate",
			start: () => ({
				onEvent: () => [
					{
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						severity: "blocker",
						claim: "candidate catches correctly",
					},
				],
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: createEmptyArm("miss-legacy"),
			candidate: candidateArm,
		});

		expect(report.corrections).toEqual([
			{ sessionId: "correction-session", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);
		expect(report.regressions).toEqual([]);
		expect(report.uncaught).toEqual([]);
		expect(report.status).toBe("qualified");
		expect(report.zeroRegression).toBe(true);
	});

	it("treats repeat catches within the same session as false positives", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "repeat-session",
			events: [{ type: "ev1" }, { type: "ev2" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const repeatProposal: CpkSupervisionProposal = {
			schema: CPK6_SCHEMA,
			ruleClass: "gate-defect",
			transcriptIndex: 0,
			severity: "nit",
			claim: "same finding twice",
		};

		const repeatingArm: CpkSupervisionArm = {
			name: "repeating-arm",
			start: () => ({
				onEvent: () => [repeatProposal],
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: repeatingArm,
			candidate: repeatingArm,
		});

		// Event 0 catches index 0; Event 1 is a duplicate catch -> false positive
		expect(report.classes["gate-defect"].candidate.caught).toBe(1);
		expect(report.classes["gate-defect"].candidate.falsePositives).toBe(1);
		expect(report.classes["gate-defect"].candidate.missed).toBe(0);
	});

	it("duplicate session ids do not share catch state", async () => {
		const session1: CpkSupervisionReplaySession = {
			id: "dup",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};
		const session2: CpkSupervisionReplaySession = {
			id: "dup",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		let call = 0;
		const arm: CpkSupervisionArm = {
			name: "dup-arm",
			start: () => ({
				onEvent: () => {
					call++;
					if (call === 1) {
						// Only session 1 legacy catches
						return [
							{
								schema: CPK6_SCHEMA,
								ruleClass: "gate-defect",
								transcriptIndex: 0,
								severity: "nit",
								claim: "first session only",
							},
						];
					}
					return [];
				},
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session1, session2],
			legacy: arm,
			candidate: createEmptyArm("empty-candidate"),
		});

		// Legacy caught in session 1, missed in session 2; candidate missed both
		expect(report.classes["gate-defect"].legacy.caught).toBe(1);
		expect(report.classes["gate-defect"].legacy.missed).toBe(1);
		expect(report.regressions).toEqual([
			{ sessionId: "dup", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);
		expect(report.uncaught).toEqual([
			{ sessionId: "dup", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);
	});

	it("keys invocations by role only, never by arm.name, even when both arms share the same name", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "s1",
			events: [{ type: "e0" }, { type: "e1" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: createEmptyArm("same"),
			candidate: createEmptyArm("same"),
		});

		expect(report.invocations).toEqual({ legacy: 2, candidate: 2 });
		expect((report.invocations as Record<string, unknown>).same).toBeUndefined();
	});

	it("handles invalid output attribution across tracked, canonical untracked, and non-canonical classes", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "multi-class-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }, { type: "e3" }],
			expected: [
				{ ruleClass: "gate-defect", transcriptIndex: 0 },
				{ ruleClass: "semantic-concern", transcriptIndex: 1 },
			],
		};

		const invalidArm: CpkSupervisionArm = {
			name: "invalid-test-arm",
			start: () => ({
				onEvent: (_event, index) => {
					if (index === 0) {
						// 1. Named tracked class: invalid charges only gate-defect
						return [{ ruleClass: "gate-defect" }];
					}
					if (index === 1) {
						// 2. Named canonical untracked class: ignored
						return [{ ruleClass: "policy-ambiguity" }];
					}
					if (index === 2) {
						// 3. Named non-canonical class: charges all tracked classes
						return [{ ruleClass: "not-a-real-class" }];
					}
					if (index === 3) {
						// 4. Garbage primitive / no ruleClass: charges all tracked classes
						return ["garbage"];
					}
					return [];
				},
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect", "semantic-concern"],
			sessions: [session],
			legacy: invalidArm,
			candidate: createEmptyArm(),
		});

		// gate-defect: event 0 (+1), event 2 (+1), event 3 (+1) = 3
		expect(report.classes["gate-defect"].legacy.invalid).toBe(3);
		// semantic-concern: event 2 (+1), event 3 (+1) = 2
		expect(report.classes["semantic-concern"].legacy.invalid).toBe(2);
	});

	it("ignores valid outputs whose ruleClass is not in classes", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "untracked-valid-session",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const arm: CpkSupervisionArm = {
			name: "untracked-valid-arm",
			start: () => ({
				onEvent: () => [
					{
						schema: CPK6_SCHEMA,
						ruleClass: "semantic-concern", // valid proposal, but untracked class
						transcriptIndex: 0,
						severity: "nit",
						claim: "untracked category",
					},
				],
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: arm,
			candidate: arm,
		});

		// Ignored: not in classes, not counted as catch, fp, or invalid
		expect(report.classes["gate-defect"].candidate.caught).toBe(0);
		expect(report.classes["gate-defect"].candidate.falsePositives).toBe(0);
		expect(report.classes["gate-defect"].candidate.invalid).toBe(0);
		expect(report.classes["semantic-concern"]).toBeUndefined();
	});

	it("invalid output count does not affect qualification status", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "invalid-status-session",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const arm: CpkSupervisionArm = {
			name: "catching-with-invalid-arm",
			start: () => ({
				onEvent: () => [
					{
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						severity: "nit",
						claim: "caught",
					},
					"malformed-output", // causes invalid++
				],
			}),
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: arm,
			candidate: arm,
		});

		expect(report.classes["gate-defect"].candidate.caught).toBe(1);
		expect(report.classes["gate-defect"].candidate.invalid).toBe(1);
		expect(report.status).toBe("qualified");
		expect(report.zeroRegression).toBe(true);
	});

	it("validates malformed options and bad rule classes with specific error codes", async () => {
		await expectAsyncSupervisionErrorCode(
			replayCpkSupervision(null as unknown as Parameters<typeof replayCpkSupervision>[0]),
			"not_object",
		);

		await expectAsyncSupervisionErrorCode(
			replayCpkSupervision({
				classes: "not-an-array" as unknown as [],
				sessions: [],
				legacy: createEmptyArm(),
				candidate: createEmptyArm(),
			}),
			"not_object",
		);

		await expectAsyncSupervisionErrorCode(
			replayCpkSupervision({
				classes: ["not-a-canonical-class" as unknown as "gate-defect"],
				sessions: [],
				legacy: createEmptyArm(),
				candidate: createEmptyArm(),
			}),
			"invalid_class",
		);
	});
});
