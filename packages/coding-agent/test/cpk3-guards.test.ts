import { describe, expect, it } from "bun:test";
import {
	CPK_GUARD_STAGES,
	type CpkGuardStage,
	type CpkGuardStageSpec,
	type CpkGuardState,
	runCpkGuardPipeline,
	validateGuardPipeline,
} from "../src/extensibility/cpk/guards";
import { CpkSeamError, type CpkSeamErrorCode } from "../src/extensibility/cpk/seams";

function captureSeamError(fn: () => unknown): CpkSeamError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkSeamError) return err;
		throw err;
	}
	throw new Error("expected CpkSeamError");
}

function expectCode(fn: () => unknown, code: CpkSeamErrorCode): void {
	expect(captureSeamError(fn).code).toBe(code);
}

const full: CpkGuardState = {
	effects: ["read", "write", "exec", "network"],
	fields: { token: "secret", path: "/tmp", secret: "hunter2" },
};

function orderedStages(
	overrides: Partial<Record<CpkGuardStage, (state: CpkGuardState) => CpkGuardState>>,
): CpkGuardStageSpec[] {
	return CPK_GUARD_STAGES.map(stage => ({ stage, apply: overrides[stage] ?? (state => state) }));
}

describe("CPK-3 monotonic guard pipeline (OMP-206)", () => {
	it("accepts the canonical stage order and rejects unknown, duplicate, missing, or disordered stages", () => {
		expect(validateGuardPipeline(CPK_GUARD_STAGES)).toEqual([]);

		expect(validateGuardPipeline(["deny", "bogus", "narrow", "redact", "validate"]).map(v => v.code)).toEqual([
			"unknown_stage",
		]);
		expect(validateGuardPipeline(["deny", "narrow", "narrow", "redact", "validate"]).map(v => v.code)).toEqual([
			"duplicate_stage",
		]);
		expect(validateGuardPipeline(["deny", "narrow", "redact"]).map(v => v.code)).toEqual(["missing_stage"]);
		expect(validateGuardPipeline(["redact", "deny", "narrow", "validate"]).map(v => v.code)).toEqual([
			"disordered_pipeline",
			"disordered_pipeline",
		]);
	});

	it("narrows effects and redacts fields monotonically in stage order", () => {
		const result = runCpkGuardPipeline(
			orderedStages({
				deny: state => ({ ...state, effects: state.effects.filter(effect => effect !== "exec") }),
				narrow: state => ({ ...state, effects: state.effects.filter(effect => effect !== "network") }),
				redact: state => {
					const { secret, ...fields } = state.fields;
					return { ...state, fields };
				},
			}),
			full,
		);
		expect(result.effects).toEqual(["read", "write"]);
		expect(result.fields).toEqual({ path: "/tmp", token: "secret" });
	});

	it("refuses a stage that widens effects or adds a field", () => {
		expectCode(
			() =>
				runCpkGuardPipeline(
					orderedStages({
						narrow: state => ({ ...state, effects: [...state.effects, "root"] }),
					}),
					full,
				),
			"non_monotonic",
		);
		expectCode(
			() =>
				runCpkGuardPipeline(
					orderedStages({
						redact: state => ({ ...state, fields: { ...state.fields, injected: "x" } }),
					}),
					full,
				),
			"non_monotonic",
		);
	});

	it("refuses a structurally invalid pipeline before running any stage", () => {
		let ran = false;
		const apply = (state: CpkGuardState): CpkGuardState => {
			ran = true;
			return { ...state, effects: [] };
		};
		expectCode(() => runCpkGuardPipeline([{ stage: "deny", apply }], full), "missing_stage");
		expect(ran).toBe(false);

		expectCode(
			() =>
				runCpkGuardPipeline(
					[
						{ stage: "narrow", apply },
						{ stage: "deny", apply },
						{ stage: "redact", apply },
						{ stage: "validate", apply },
					],
					full,
				),
			"disordered_pipeline",
		);
		expect(ran).toBe(false);
	});

	it("cannot produce a state outside the input state for any monotone stage", () => {
		const result = runCpkGuardPipeline(
			orderedStages({
				deny: state => ({ ...state, effects: state.effects.filter(effect => effect !== "exec") }),
				narrow: state => ({ ...state, fields: {} }),
				redact: state => ({ ...state, effects: [] }),
			}),
			full,
		);
		expect(result.effects).toEqual([]);
		expect(result.fields).toEqual({});
	});
});
