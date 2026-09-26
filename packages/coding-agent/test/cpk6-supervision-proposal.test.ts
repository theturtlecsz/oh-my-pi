import { describe, expect, it } from "bun:test";
import {
	assertCpkSentinelLabels,
	CPK_SUPERVISION_RULE_CLASSES,
	CPK_SUPERVISION_SEVERITIES,
	CPK6_SCHEMA,
	CpkSupervisionError,
	type CpkSupervisionErrorCode,
	type CpkSupervisionProposal,
	type CpkSupervisionRuleClass,
	isCpkSupervisionRuleClass,
	isCpkSupervisionSeverity,
	parseCpkSupervisionProposal,
	proposalFromAdvisorNote,
} from "../src/extensibility/cpk/supervision-proposal";

function captureSupervisionError(fn: () => unknown): CpkSupervisionError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkSupervisionError) return err;
		throw err;
	}
	throw new Error("expected CpkSupervisionError to be thrown");
}

function expectSupervisionErrorCode(fn: () => unknown, code: CpkSupervisionErrorCode): void {
	const err = captureSupervisionError(fn);
	expect(err.code).toBe(code);
}

describe("CPK-6 supervision proposals (OMP-208-s01)", () => {
	describe("isCpkSupervisionRuleClass and canonical constants", () => {
		it("recognizes all canonical rule classes and rejects arbitrary strings", () => {
			for (const cls of CPK_SUPERVISION_RULE_CLASSES) {
				expect(isCpkSupervisionRuleClass(cls)).toBe(true);
			}
			expect(isCpkSupervisionRuleClass("not-a-canonical-class")).toBe(false);
			expect(isCpkSupervisionRuleClass("")).toBe(false);
			expect(isCpkSupervisionRuleClass(null)).toBe(false);
			expect(isCpkSupervisionRuleClass(42)).toBe(false);
		});

		it("recognizes all canonical severities and rejects unknown levels", () => {
			for (const sev of CPK_SUPERVISION_SEVERITIES) {
				expect(isCpkSupervisionSeverity(sev)).toBe(true);
			}
			expect(isCpkSupervisionSeverity("fatal")).toBe(false);
			expect(isCpkSupervisionSeverity("")).toBe(false);
			expect(isCpkSupervisionSeverity(undefined)).toBe(false);
		});
	});

	describe("error codes defense on distinct bad inputs", () => {
		it("fails with not_object when proposal input is null, primitive, or an array", () => {
			expectSupervisionErrorCode(() => parseCpkSupervisionProposal(null), "not_object");
			expectSupervisionErrorCode(() => parseCpkSupervisionProposal("primitive string"), "not_object");
			expectSupervisionErrorCode(() => parseCpkSupervisionProposal([1, 2, 3]), "not_object");
		});

		it("fails with invalid_schema when proposal schema does not match cpk6/v1", () => {
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: "cpk5/v1",
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						claim: "deterministic gate check",
					}),
				"invalid_schema",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: undefined,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						claim: "deterministic gate check",
					}),
				"invalid_schema",
			);
		});

		it("fails with invalid_class when ruleClass is not-a-canonical-class or omitted", () => {
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "not-a-canonical-class",
						transcriptIndex: 0,
						claim: "unrecognized rule class",
					}),
				"invalid_class",
			);
			expectSupervisionErrorCode(
				() => proposalFromAdvisorNote("some advice", undefined, "not-a-canonical-class", 0),
				"invalid_class",
			);
			expectSupervisionErrorCode(
				() => proposalFromAdvisorNote("some advice", undefined, undefined, 0),
				"invalid_class",
			);
		});

		it("fails with invalid_index when transcriptIndex is NaN, negative, non-integer, or infinite", () => {
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: Number.NaN,
						claim: "check index boundary",
					}),
				"invalid_index",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: -1,
						claim: "negative index",
					}),
				"invalid_index",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 1.5,
						claim: "fractional index",
					}),
				"invalid_index",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: Number.POSITIVE_INFINITY,
						claim: "infinite index",
					}),
				"invalid_index",
			);
		});

		it("fails with invalid_severity when severity is present but not nit, concern, or blocker", () => {
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						severity: "urgent" as unknown,
						claim: "unsupported severity level",
					}),
				"invalid_severity",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						severity: null as unknown,
						claim: "null severity",
					}),
				"invalid_severity",
			);
		});

		it("fails with invalid_claim when claim is whitespace, empty, or non-string", () => {
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						claim: "   ",
					}),
				"invalid_claim",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						claim: "",
					}),
				"invalid_claim",
			);
			expectSupervisionErrorCode(
				() =>
					parseCpkSupervisionProposal({
						schema: CPK6_SCHEMA,
						ruleClass: "gate-defect",
						transcriptIndex: 0,
						claim: 12345 as unknown,
					}),
				"invalid_claim",
			);
		});

		it("fails with invalid_sentinel on malformed array, unknown class, boundary index, or duplicates", () => {
			expectSupervisionErrorCode(() => assertCpkSentinelLabels("not-an-array", 5), "invalid_sentinel");
			expectSupervisionErrorCode(() => assertCpkSentinelLabels([null], 5), "invalid_sentinel");
			expectSupervisionErrorCode(
				() =>
					assertCpkSentinelLabels(
						[{ ruleClass: "not-a-canonical-class" as unknown as CpkSupervisionRuleClass, transcriptIndex: 0 }],
						5,
					),
				"invalid_sentinel",
			);
			expectSupervisionErrorCode(
				() => assertCpkSentinelLabels([{ ruleClass: "gate-defect", transcriptIndex: -1 }], 5),
				"invalid_sentinel",
			);
			expectSupervisionErrorCode(
				() => assertCpkSentinelLabels([{ ruleClass: "gate-defect", transcriptIndex: 1.5 }], 5),
				"invalid_sentinel",
			);
			expectSupervisionErrorCode(
				() => assertCpkSentinelLabels([{ ruleClass: "gate-defect", transcriptIndex: Number.NaN }], 5),
				"invalid_sentinel",
			);
			// Boundary: label index == eventCount must be rejected because indices are [0, eventCount)
			expectSupervisionErrorCode(
				() => assertCpkSentinelLabels([{ ruleClass: "gate-defect", transcriptIndex: 5 }], 5),
				"invalid_sentinel",
			);
			// Duplicate (ruleClass, transcriptIndex) label must be rejected
			expectSupervisionErrorCode(
				() =>
					assertCpkSentinelLabels(
						[
							{ ruleClass: "gate-defect", transcriptIndex: 2 },
							{ ruleClass: "gate-defect", transcriptIndex: 2 },
						],
						5,
					),
				"invalid_sentinel",
			);
		});
	});

	describe("valid proposal contracts and round-trip", () => {
		it("round-trips a valid advisor note with default severity 'nit' applied", () => {
			const note = "Primary tool called without required verification step.";
			const category = "model-procedure-miss";
			const transcriptIndex = 4;

			const proposal = proposalFromAdvisorNote(note, undefined, category, transcriptIndex);

			const expected: CpkSupervisionProposal = {
				schema: "cpk6/v1",
				ruleClass: "model-procedure-miss",
				severity: "nit",
				transcriptIndex: 4,
				claim: note,
			};
			expect(proposal).toEqual(expected);

			const roundTripped = parseCpkSupervisionProposal(proposal);
			expect(roundTripped).toEqual(expected);
			// Fresh instance guarantee
			expect(roundTripped).not.toBe(proposal);
		});

		it("preserves explicit severity and excludes extraneous input properties", () => {
			const rawInput = {
				schema: CPK6_SCHEMA,
				ruleClass: "semantic-concern",
				severity: "blocker" as const,
				transcriptIndex: 0,
				claim: "Proposed mutation widens effect boundary beyond declared scope.",
				untrackedExtraProperty: "should-be-dropped",
			};

			const parsed = parseCpkSupervisionProposal(rawInput);

			expect(parsed).toEqual({
				schema: CPK6_SCHEMA,
				ruleClass: "semantic-concern",
				severity: "blocker",
				transcriptIndex: 0,
				claim: "Proposed mutation widens effect boundary beyond declared scope.",
			});
			expect("untrackedExtraProperty" in parsed).toBe(false);
		});

		it("assertCpkSentinelLabels returns fresh array and permits distinct classes at the same index", () => {
			const input = [
				{ ruleClass: "gate-defect" as const, transcriptIndex: 1 },
				{ ruleClass: "policy-ambiguity" as const, transcriptIndex: 1 },
				{ ruleClass: "possible-false-positive" as const, transcriptIndex: 3 },
			];

			const validated = assertCpkSentinelLabels(input, 4);

			expect(validated).toEqual(input);
			expect(validated).not.toBe(input);
			expect(validated[0]).not.toBe(input[0]);
		});

		it("assertCpkSentinelLabels accepts empty label lists for non-empty or empty event sequences", () => {
			expect(assertCpkSentinelLabels([], 0)).toEqual([]);
			expect(assertCpkSentinelLabels([], 10)).toEqual([]);
		});
	});
});
