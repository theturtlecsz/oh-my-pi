/** NSI-1 wire types. Schemas in contracts/nsi/v1 own runtime validation.
 * mission.intake continues to accept BoundedIntakeDraft through the client contract.
 * Numeric bounds, hashes, unique arrays, and evidence semantics require validation.
 */
export type IntakeCategory =
	| "functional_scope_and_behavior"
	| "domain_and_data_model"
	| "interaction_and_ux_flow"
	| "non_functional_qualities"
	| "integrations_and_external_dependencies"
	| "edge_cases_and_failure_handling"
	| "constraints_and_tradeoffs"
	| "terminology"
	| "completion_signals"
	| "placeholders_and_ambiguities"
	| "scope_boundaries";

export type IntakeRunState =
	| "collecting"
	| "reasoning"
	| "needs_spec"
	| "conflicted"
	| "ready_for_ratification"
	| "ratified"
	| "published"
	| "stale"
	| "invalid"
	| "superseded";
export type ClaimOrigin =
	| "owner_statement"
	| "policy_rule"
	| "repository_observation"
	| "tool_receipt"
	| "model_proposal"
	| "rule_derivation";
export type ClaimState = "proposed" | "admitted" | "rejected" | "superseded";
export type ClaimModality = "observed" | "desired" | "required" | "prohibited" | "possible" | "unknown";
export type ActorKind = "owner" | "service" | "model" | "tool" | "rule_engine";
export type ActorRef = { id: string; kind: ActorKind };
export type OwnerRef = { id: string; kind: "owner" };
export type RuleEngineRef = { id: string; kind: "rule_engine" };
export type EntityRef = { id: string; type: string };
export type TypedValue =
	| { type: "string"; value: string }
	| { type: "integer" | "number"; value: number }
	| { type: "boolean"; value: boolean }
	| { type: "null"; value: null }
	| { type: "entity"; value: EntityRef };
export type EvidenceSpan = { artifact_id: string; start_byte: number; end_byte: number; quote?: string };
export type EvidenceArtifact = {
	id: string;
	uri: string;
	content_hash: string;
	revision: string;
	actor: ActorRef;
	media_type: string;
};

/** Origin identifies who asserted a claim; admission requires an authorized actor. */
export type IntakeClaim = {
	id: string;
	category: IntakeCategory;
	subject: EntityRef;
	predicate_id: string;
	value: TypedValue;
	modality: ClaimModality;
	polarity: "positive" | "negative";
	truth: "true" | "false" | "unknown";
	evidence: EvidenceSpan[];
	premise_ids: string[];
} & (
	| { origin: "owner_statement"; asserted_by: OwnerRef }
	| { origin: "rule_derivation"; asserted_by: RuleEngineRef }
	| { origin: "model_proposal"; asserted_by: { id: string; kind: "model" } }
	| { origin: "policy_rule" | "repository_observation" | "tool_receipt"; asserted_by: ActorRef }
) &
	(
		| { state: "admitted"; admitted_by: OwnerRef | RuleEngineRef }
		| { state: Exclude<ClaimState, "admitted">; admitted_by?: OwnerRef | RuleEngineRef }
	);

export type WaiverPolicy = "never" | "owner_with_reason";
type MaterialityPolicy =
	| { materiality: "critical"; waiver_policy: "never" }
	| { materiality: "material" | "minor"; waiver_policy: WaiverPolicy };
export type PredicateDefinition = {
	id: string;
	title: string;
	subject_types: string[];
	object_types: string[];
	cardinality: "one" | "many";
	exclusivity: boolean;
	transitivity: boolean;
	authority: ActorKind[];
};
export type RequirementDimensionDefinition = {
	id: string;
	title: string;
	category: IntakeCategory;
	applicability: { entity_types: string[]; predicate_ids: string[] };
	required_premises: string[];
} & MaterialityPolicy;
export type IntakeOntology = {
	predicates: PredicateDefinition[];
	dimensions: RequirementDimensionDefinition[];
};
/** Runtime aggregate from NSI-1 models; no standalone snapshot JSON Schema. */
export type IntakeSnapshot = {
	run_id: string;
	revision: number;
	state: IntakeRunState;
	artifacts: EvidenceArtifact[];
	entities: EntityRef[];
	claims: IntakeClaim[];
	ontology: IntakeOntology;
};

export type RequirementClause = {
	id: string;
	category: IntakeCategory;
	text: string;
	claim_ids: string[];
	dimension_ids: string[];
};
export type AcceptanceCriterion = {
	id: string;
	requirement_ids: string[];
	description: string;
	verification_method: string;
	expected_result: TypedValue;
};
export type Formalization = {
	id: string;
	requirement_ids: string[];
	language: string;
	expression: string;
	scope: string;
	assumptions: string[];
	content_hash: string;
};
export type WaiverDefinition = {
	id: string;
	run_id: string;
	revision: number;
	premise_id: string;
	dimension_id: string;
	materiality: "material" | "minor";
	waiver_policy: WaiverPolicy;
	granted_by: OwnerRef;
	reason: string;
	decision_id: string;
	created_at: string;
};
export type IntakeContract = {
	requirements: RequirementClause[];
	acceptance_criteria: AcceptanceCriterion[];
	formalizations: Formalization[];
	waivers: WaiverDefinition[];
};
export type Binding = {
	run_id: string;
	revision: number;
	contract_hash: string;
	claim_set_hash: string;
	rule_bundle_hash: string;
	schema_hash: string;
	source_hash: string;
};
export type Gap = {
	id: string;
	dimension_id: string;
	premise_id: string;
	reason: string;
} & MaterialityPolicy;
export type Conflict = { id: string; claim_ids: string[]; predicate_id: string; reason: string };
export type IntakeReadinessAssessment = Binding & {
	status: "READY_FOR_RATIFICATION" | "NEEDS_SPEC" | "CONFLICTED";
	gaps: Gap[];
	conflicts: Conflict[];
	derivation_receipt_ids: string[];
	assessed_by: RuleEngineRef;
	created_at: string;
};
export type RatificationReceipt = Binding & {
	assessment_id: string;
	formal_binding_ids: string[];
	ratified_by: OwnerRef;
	created_at: string;
};
export type IntakeReadinessReceipt = Binding & {
	status: "ELIGIBLE_FOR_PUBLICATION";
	ratification_receipt_id: string;
	formal_binding_ids: string[];
	derivation_receipt_ids: string[];
	issued_by: RuleEngineRef;
	created_at: string;
};
export type IntakePublicationReceipt = Binding & {
	state: "published";
	ratification_receipt_id: string;
	readiness_receipt_id: string;
	publishedBy: OwnerRef;
	created_at: string;
};
