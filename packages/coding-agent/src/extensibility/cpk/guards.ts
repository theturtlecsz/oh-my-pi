/**
 * CPK-3 monotonic guard pipeline.
 *
 * Every effect that crosses a plugin seam passes through one fixed pipeline:
 * `deny`, then `narrow`, then `redact`, then `validate`. The stages run in that
 * order, exactly once each, and every stage is monotone: it may only remove
 * effects or fields, never add one. A pipeline that widens, or that declares
 * stages out of order, is refused rather than run.
 *
 * The guarantee this module enforces is structural: a request that survived the
 * pipeline is always a subset of the request that entered it, so no stage can
 * escalate authority — only reduce it.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

import { sortUnique } from "./manifest";
import { CpkSeamError } from "./seams";

/** Guard stages, ordered from the earliest (deny) to the last (validate). */
export const CPK_GUARD_STAGES = ["deny", "narrow", "redact", "validate"] as const;

export type CpkGuardStage = (typeof CPK_GUARD_STAGES)[number];

/** Type guard for the closed set of guard stages. */
export function isCpkGuardStage(value: string): value is CpkGuardStage {
	return (CPK_GUARD_STAGES as readonly string[]).includes(value);
}

export interface CpkGuardState {
	/** Effects carried through the pipeline, sorted. */
	effects: string[];
	/** Named fields carried through the pipeline, sorted by key. */
	fields: Record<string, string>;
}

export interface CpkGuardStageSpec {
	stage: CpkGuardStage;
	/** Monotone transform: may remove effects/fields, never add. */
	apply: (state: CpkGuardState) => CpkGuardState;
}

export type CpkGuardViolationCode =
	| "unknown_stage"
	| "duplicate_stage"
	| "missing_stage"
	| "disordered_pipeline"
	| "non_monotonic";

export interface CpkGuardViolation {
	code: CpkGuardViolationCode;
	/** Offending stage, or `<pipeline>` for a structural error. */
	stage: string;
	/** Effects or fields that violate monotonicity, sorted. Empty for structural errors. */
	additions: string[];
	message: string;
}

function normalizeState(state: CpkGuardState): CpkGuardState {
	const keys = Object.keys(state.fields).sort();
	const fields: Record<string, string> = {};
	for (const key of keys) fields[key] = state.fields[key];
	return { effects: sortUnique(state.effects), fields };
}

/**
 * Validate that a pipeline declares each canonical stage exactly once, in
 * canonical order. Returns one violation per offending stage, sorted by stage
 * order; an empty array means the pipeline is well-formed.
 */
export function validateGuardPipeline(stages: readonly string[]): CpkGuardViolation[] {
	const violations: CpkGuardViolation[] = [];
	const seen = new Set<string>();
	let previous = -1;

	stages.forEach(stage => {
		if (!isCpkGuardStage(stage)) {
			violations.push({
				code: "unknown_stage",
				stage,
				additions: [],
				message: `unknown guard stage "${stage}"`,
			});
			return;
		}
		const position = CPK_GUARD_STAGES.indexOf(stage);
		if (seen.has(stage)) {
			violations.push({
				code: "duplicate_stage",
				stage,
				additions: [],
				message: `duplicate guard stage "${stage}"`,
			});
		} else if (position < previous) {
			violations.push({
				code: "disordered_pipeline",
				stage,
				additions: [],
				message: `guard stage "${stage}" runs after a later stage; the order must be ${CPK_GUARD_STAGES.join(" -> ")}`,
			});
		}
		seen.add(stage);
		previous = Math.max(previous, position);
	});

	for (const stage of CPK_GUARD_STAGES) {
		if (!seen.has(stage)) {
			violations.push({
				code: "missing_stage",
				stage,
				additions: [],
				message: `guards must run every stage exactly once; "${stage}" is missing`,
			});
		}
	}

	return violations.sort((a, b) => {
		const ai = CPK_GUARD_STAGES.indexOf(a.stage as CpkGuardStage);
		const bi = CPK_GUARD_STAGES.indexOf(b.stage as CpkGuardStage);
		const av = ai === -1 ? Number.POSITIVE_INFINITY : ai;
		const bv = bi === -1 ? Number.POSITIVE_INFINITY : bi;
		return av === bv ? a.stage.localeCompare(b.stage) : av - bv;
	});
}

function additions(previous: CpkGuardState, next: CpkGuardState): string[] {
	const priorEffects = new Set(previous.effects);
	const priorFields = new Set(Object.keys(previous.fields));
	const added: string[] = [];
	for (const effect of next.effects) {
		if (!priorEffects.has(effect)) added.push(effect);
	}
	for (const field of Object.keys(next.fields)) {
		if (!priorFields.has(field)) added.push(field);
	}
	return sortUnique(added);
}

/**
 * Run a guard pipeline over an initial state.
 *
 * Throws {@link CpkSeamError} when the pipeline is not the canonical ordered
 * stage list (`disordered_pipeline`/`duplicate_stage`/`unknown_stage`), or when
 * any stage adds an effect or field (`non_monotonic`). The returned state is
 * always a subset of the input state.
 */
export function runCpkGuardPipeline(stages: readonly CpkGuardStageSpec[], initial: CpkGuardState): CpkGuardState {
	const structural = validateGuardPipeline(stages.map(spec => spec.stage));
	if (structural.length > 0) {
		throw new CpkSeamError(structural[0].code, structural[0].message);
	}

	let state = normalizeState(initial);
	for (const spec of stages) {
		const next = normalizeState(spec.apply(state));
		const added = additions(state, next);
		if (added.length > 0) {
			throw new CpkSeamError(
				"non_monotonic",
				`guard stage "${spec.stage}" widened authority by adding: ${added.join(", ")}`,
			);
		}
		state = next;
	}
	return state;
}
