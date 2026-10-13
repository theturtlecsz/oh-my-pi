/**
 * intake-evaluation.ts — wire types and commands for bounded intake assessment (OMP-523).
 *
 * Exposes assess_bounded_intake command, payload, and result models mirroring
 * omp_work.v1.models and omp_work.v1.api_models.
 */
import type { BoundedIntakeDraft } from "./index";

export type IntakeBlockingQuestionRuleClass =
	| "contradictory_constraints"
	| "missing_verification_oracle"
	| "missing_consequential_authority_or_dependency";

/** omp_work.v1.models.IntakeBlockingQuestion. */
export type IntakeBlockingQuestion = {
	rule_class: IntakeBlockingQuestionRuleClass;
	deduplication_key: string;
	statement: string;
	priority: number;
	claim_ids?: string[];
};

/** omp_work.v1.models.AssessBoundedIntakePayload. */
export type AssessBoundedIntakePayload = {
	draft: BoundedIntakeDraft;
};

/** omp_work.v1.models.AssessBoundedIntakeCommand. */
export type AssessBoundedIntakeCommand = {
	type: "assess_bounded_intake";
	payload: AssessBoundedIntakePayload;
};

/** omp_work.v1.api_models.AssessBoundedIntakeResult. */
export type AssessBoundedIntakeResult = {
	type: "assess_bounded_intake";
	semantic_sha256: string;
	rule_bundle_sha256: string;
	ready_for_ratification: boolean;
	issue_count: number;
	questions: IntakeBlockingQuestion[];
};
