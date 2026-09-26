import type { AdvisorCategory } from "../../advisor/advise-tool";

/** Schema discriminator for CPK-6 supervision contracts. */
export const CPK6_SCHEMA = "cpk6/v1";

/** Canonical supervision rule classes across Advisor and Task Observer. */
export const CPK_SUPERVISION_RULE_CLASSES = [
	"gate-defect",
	"model-procedure-miss",
	"semantic-concern",
	"policy-ambiguity",
	"possible-false-positive",
] as const;

export type CpkSupervisionRuleClass = (typeof CPK_SUPERVISION_RULE_CLASSES)[number];

// Compile-time check that CpkSupervisionRuleClass is exactly AdvisorCategory.
// The conditional form `[A] extends [B] ? ... : never` collapses to `never` on a
// mismatch, and `never` satisfies `T extends true`; the identity form yields
// `false`, which the compiler rejects. The unused alias is still evaluated.
type CpkSupervisionTypeEquals<A, B> =
	(<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type CpkSupervisionAssertTrue<T extends true = never> = T;
type _CpkSupervisionRuleClassEqualsAdvisorCategory = CpkSupervisionAssertTrue<
	CpkSupervisionTypeEquals<CpkSupervisionRuleClass, AdvisorCategory>
>;

/** Type guard for canonical supervision rule classes. */
export function isCpkSupervisionRuleClass(value: unknown): value is CpkSupervisionRuleClass {
	return typeof value === "string" && (CPK_SUPERVISION_RULE_CLASSES as readonly string[]).includes(value);
}

/** Allowed supervision proposal severity levels. */
export const CPK_SUPERVISION_SEVERITIES = ["nit", "concern", "blocker"] as const;
export type CpkSupervisionSeverity = (typeof CPK_SUPERVISION_SEVERITIES)[number];

/** Type guard for supervision severity levels. */
export function isCpkSupervisionSeverity(value: unknown): value is CpkSupervisionSeverity {
	return typeof value === "string" && (CPK_SUPERVISION_SEVERITIES as readonly string[]).includes(value);
}

export interface CpkSupervisionProposal {
	schema: typeof CPK6_SCHEMA;
	ruleClass: CpkSupervisionRuleClass;
	severity: CpkSupervisionSeverity;
	transcriptIndex: number;
	claim: string;
}

export type CpkSupervisionErrorCode =
	| "not_object"
	| "invalid_schema"
	| "invalid_class"
	| "invalid_index"
	| "invalid_severity"
	| "invalid_claim"
	| "invalid_sentinel";

export class CpkSupervisionError extends Error {
	readonly code: CpkSupervisionErrorCode;

	constructor(code: CpkSupervisionErrorCode, message: string) {
		super(message);
		this.name = "CpkSupervisionError";
		this.code = code;
	}
}

/**
 * Validate and canonicalize an untrusted supervision proposal.
 * Returns a fresh proposal or throws CpkSupervisionError.
 */
export function parseCpkSupervisionProposal(value: unknown): CpkSupervisionProposal {
	if (typeof value !== "object" || value === null || Array.isArray(value)) {
		throw new CpkSupervisionError("not_object", "proposal must be a non-null object");
	}

	const record = value as Record<string, unknown>;

	if (record.schema !== CPK6_SCHEMA) {
		throw new CpkSupervisionError("invalid_schema", `schema must be "${CPK6_SCHEMA}"`);
	}

	if (!isCpkSupervisionRuleClass(record.ruleClass)) {
		throw new CpkSupervisionError(
			"invalid_class",
			`ruleClass must be one of [${CPK_SUPERVISION_RULE_CLASSES.join(", ")}], got ${String(record.ruleClass)}`,
		);
	}

	const index = record.transcriptIndex;
	if (typeof index !== "number" || !Number.isSafeInteger(index) || index < 0) {
		throw new CpkSupervisionError(
			"invalid_index",
			`transcriptIndex must be a non-negative safe integer, got ${String(index)}`,
		);
	}

	let severity: CpkSupervisionSeverity = "nit";
	if (record.severity !== undefined) {
		if (!isCpkSupervisionSeverity(record.severity)) {
			throw new CpkSupervisionError(
				"invalid_severity",
				`severity must be one of [${CPK_SUPERVISION_SEVERITIES.join(", ")}], got ${String(record.severity)}`,
			);
		}
		severity = record.severity;
	}

	if (typeof record.claim !== "string" || record.claim.trim().length === 0) {
		throw new CpkSupervisionError("invalid_claim", "claim must be a non-empty string after trim");
	}

	return {
		schema: CPK6_SCHEMA,
		ruleClass: record.ruleClass,
		severity,
		transcriptIndex: index,
		claim: record.claim,
	};
}

/**
 * Construct a CpkSupervisionProposal from advisor note parameters by building
 * through parseCpkSupervisionProposal.
 * Missing/unknown category throws invalid_class.
 */
export function proposalFromAdvisorNote(
	note: string,
	severity: CpkSupervisionSeverity | undefined,
	category: string | undefined,
	transcriptIndex: number,
): CpkSupervisionProposal {
	return parseCpkSupervisionProposal({
		schema: CPK6_SCHEMA,
		ruleClass: category,
		severity,
		transcriptIndex,
		claim: note,
	});
}

export interface CpkSentinelLabel {
	ruleClass: CpkSupervisionRuleClass;
	transcriptIndex: number;
}

/**
 * Validates an array of sentinel labels against an event count.
 * Returns a fresh array of validated labels or throws invalid_sentinel.
 */
export function assertCpkSentinelLabels(labels: unknown, eventCount: number): CpkSentinelLabel[] {
	if (!Array.isArray(labels)) {
		throw new CpkSupervisionError("invalid_sentinel", "sentinel labels must be an array");
	}

	if (typeof eventCount !== "number" || !Number.isSafeInteger(eventCount) || eventCount < 0) {
		throw new CpkSupervisionError(
			"invalid_sentinel",
			`eventCount must be a non-negative safe integer, got ${String(eventCount)}`,
		);
	}

	const seen = new Set<string>();
	const result: CpkSentinelLabel[] = [];

	for (const item of labels) {
		if (typeof item !== "object" || item === null || Array.isArray(item)) {
			throw new CpkSupervisionError("invalid_sentinel", "sentinel label must be a non-null object");
		}

		const record = item as Record<string, unknown>;

		if (!isCpkSupervisionRuleClass(record.ruleClass)) {
			throw new CpkSupervisionError(
				"invalid_sentinel",
				`sentinel label ruleClass must be canonical, got ${String(record.ruleClass)}`,
			);
		}

		const index = record.transcriptIndex;
		if (typeof index !== "number" || !Number.isSafeInteger(index) || index < 0 || index >= eventCount) {
			throw new CpkSupervisionError(
				"invalid_sentinel",
				`sentinel label transcriptIndex must be a safe integer in [0, ${eventCount}), got ${String(index)}`,
			);
		}

		const key = `${record.ruleClass}:${index}`;
		if (seen.has(key)) {
			throw new CpkSupervisionError(
				"invalid_sentinel",
				`duplicate sentinel label for (${record.ruleClass}, ${index})`,
			);
		}
		seen.add(key);

		result.push({
			ruleClass: record.ruleClass,
			transcriptIndex: index,
		});
	}

	return result;
}
