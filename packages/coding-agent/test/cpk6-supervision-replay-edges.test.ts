import { describe, expect, it } from "bun:test";
import {
	CPK6_SCHEMA,
	type CpkSupervisionProposal,
	type CpkSupervisionRuleClass,
} from "../src/extensibility/cpk/supervision-proposal";
import {
	type CpkSupervisionArm,
	type CpkSupervisionReplaySession,
	replayCpkSupervision,
} from "../src/extensibility/cpk/supervision-replay";

function proposal(
	ruleClass: CpkSupervisionRuleClass,
	transcriptIndex: number,
	claim = "caught",
): CpkSupervisionProposal {
	return { schema: CPK6_SCHEMA, ruleClass, transcriptIndex, severity: "nit", claim };
}

function armFrom(name: string, onEvent: (event: unknown, index: number) => unknown[]): CpkSupervisionArm {
	return {
		name,
		start: () => ({ onEvent }),
	};
}

describe("CPK-6 supervision replay regression edges (OMP-208-s02-s02)", () => {
	it("keys invocations by role, not arm.name: arms literally named legacy/candidate on 3 events", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "role-named-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: armFrom("legacy", () => []),
			candidate: armFrom("candidate", () => []),
		});

		expect(report.invocations).toEqual({ legacy: 3, candidate: 3 });
	});

	it("keys invocations by role only when both arms share the name 'same' on 2 events", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "same-named-session",
			events: [{ type: "e0" }, { type: "e1" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [session],
			legacy: armFrom("same", () => []),
			candidate: armFrom("same", () => []),
		});

		expect(report.invocations).toEqual({ legacy: 2, candidate: 2 });
		expect(Object.keys(report.invocations).sort()).toEqual(["candidate", "legacy"]);
		expect("same" in report.invocations).toBe(false);
	});

	it("charges every tracked class once for a non-canonical invalid output on an unlabelled event", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "invalid-unlabelled-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }],
			expected: [
				{ ruleClass: "gate-defect", transcriptIndex: 0 },
				{ ruleClass: "semantic-concern", transcriptIndex: 1 },
			],
		};

		const catchingLegacy = armFrom("legacy", (_event, index) => {
			if (index === 0) return [proposal("gate-defect", 0)];
			if (index === 1) return [proposal("semantic-concern", 1)];
			return [];
		});

		const invalidOutputs: unknown[] = [{ ruleClass: "not-a-real-class" }, "garbage"];
		for (const invalidOutput of invalidOutputs) {
			const catchingCandidate = armFrom("candidate", (_event, index) => {
				if (index === 0) return [proposal("gate-defect", 0)];
				if (index === 1) return [proposal("semantic-concern", 1)];
				if (index === 2) return [invalidOutput];
				return [];
			});

			const report = await replayCpkSupervision({
				classes: ["gate-defect", "semantic-concern"],
				sessions: [session],
				legacy: catchingLegacy,
				candidate: catchingCandidate,
			});

			// Invalid output attributed to every tracked class once, catches unchanged.
			expect(report.classes["gate-defect"].candidate.invalid).toBe(1);
			expect(report.classes["semantic-concern"].candidate.invalid).toBe(1);
			expect(report.classes["gate-defect"].candidate.caught).toBe(1);
			expect(report.classes["semantic-concern"].candidate.caught).toBe(1);
			expect(report.classes["gate-defect"].legacy.invalid).toBe(0);
			expect(report.classes["semantic-concern"].legacy.invalid).toBe(0);
		}
	});

	it("charges only the named class for a schema-less invalid output naming a tracked class", async () => {
		const session: CpkSupervisionReplaySession = {
			id: "invalid-named-class-session",
			events: [{ type: "e0" }, { type: "e1" }, { type: "e2" }],
			expected: [
				{ ruleClass: "gate-defect", transcriptIndex: 0 },
				{ ruleClass: "semantic-concern", transcriptIndex: 1 },
			],
		};

		const catchingLegacy = armFrom("legacy", (_event, index) => {
			if (index === 0) return [proposal("gate-defect", 0)];
			if (index === 1) return [proposal("semantic-concern", 1)];
			return [];
		});

		const catchingCandidate = armFrom("candidate", (_event, index) => {
			if (index === 0) return [proposal("gate-defect", 0)];
			if (index === 1) return [proposal("semantic-concern", 1)];
			// Missing schema and claim: parse fails; ruleClass names gate-defect only.
			if (index === 2) return [{ ruleClass: "gate-defect" }];
			return [];
		});

		const report = await replayCpkSupervision({
			classes: ["gate-defect", "semantic-concern"],
			sessions: [session],
			legacy: catchingLegacy,
			candidate: catchingCandidate,
		});

		expect(report.classes["gate-defect"].candidate.invalid).toBe(1);
		expect(report.classes["semantic-concern"].candidate.invalid).toBe(0);
		expect(report.classes["gate-defect"].candidate.caught).toBe(1);
		expect(report.classes["semantic-concern"].candidate.caught).toBe(1);
	});

	it("keeps catch state per session position, not per session id, for duplicate ids", async () => {
		const makeSession = (): CpkSupervisionReplaySession => ({
			id: "dup",
			events: [{ type: "e0" }],
			expected: [{ ruleClass: "gate-defect", transcriptIndex: 0 }],
		});

		let starts = 0;
		const firstOnlyLegacy: CpkSupervisionArm = {
			name: "first-session-only",
			start: () => {
				const isFirst = starts++ === 0;
				return {
					onEvent: () => (isFirst ? [proposal("gate-defect", 0, "first session only")] : []),
				};
			},
		};

		const report = await replayCpkSupervision({
			classes: ["gate-defect"],
			sessions: [makeSession(), makeSession()],
			legacy: firstOnlyLegacy,
			candidate: armFrom("empty-candidate", () => []),
		});

		expect(report.regressions).toEqual([
			{ sessionId: "dup", ruleClass: "gate-defect", transcriptIndex: 0 },
		]);
		expect(report.uncaught).toEqual([{ sessionId: "dup", ruleClass: "gate-defect", transcriptIndex: 0 }]);
		expect(report.classes["gate-defect"].legacy.caught).toBe(1);
		expect(report.classes["gate-defect"].legacy.missed).toBe(1);
	});
});
