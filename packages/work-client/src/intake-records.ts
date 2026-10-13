/** NSI-2 intake write wire types. Schemas in contracts/v1 own runtime
 * validation. Mirrors omp_work.v1.models intake payloads/commands and
 * omp_work.v1.api_models intake results and IntakeRecordView one-for-one.
 *
 * No implicit defaults (E1078): every Python field that is required without a
 * default is required here too. `expected_revision` is required on every
 * follow-on payload; `start_intake` carries the literal `0`. The two nullable
 * decision fields are required and explicitly `| null`. AssessIntakeResult has
 * no `expected_revision` — assessment binds `revision == assessed_revision`.
 */
import type {
	AcceptanceCriterion,
	Conflict,
	EntityRef,
	EvidenceArtifact,
	EvidenceSpan,
	IntakeCategory,
	IntakeClaim,
	IntakeOntology,
	IntakeSnapshot,
	RequirementClause,
	TypedValue,
	OwnerRef,
} from "./intake-nsi";
import type { IntakeSource, OwnerInstruction, UUID } from "./index";

/** omp_work.contracts.nsi.v1.models.ClarificationQuestion. */
export type ClarificationQuestion = {
	id: string;
	question: string;
	gap_ids: string[];
	conflict_ids: string[];
	category: IntakeCategory;
};

/** omp_work.contracts.nsi.v1.models.OwnerDecision. */
export type OwnerDecision = {
	id: string;
	run_id: string;
	revision: number;
	actor: OwnerRef;
	question_id: string;
	answer: TypedValue;
	reason: string;
	created_at: string;
};

// ---- command payloads (models.py) ----

/** Shared shape for every follow-on intake write. */
export type IntakeRevisionPayload = {
	run_id: string;
	expected_revision: number;
};

export type StartIntakePayload = {
	run_id: string;
	expected_revision: 0;
	mission_id: UUID;
	ontology: IntakeOntology;
	entities: EntityRef[];
};

export type RegisterIntakeEvidencePayload = {
	run_id: string;
	expected_revision: number;
	artifact: EvidenceArtifact;
	source: IntakeSource;
};

export type IntakeClaimEvidenceLink = {
	span: EvidenceSpan;
	relation: "supports" | "contradicts" | "neither";
};

export type RecordIntakeClaimPayload = {
	run_id: string;
	expected_revision: number;
	claim: IntakeClaim;
	evidence_links: IntakeClaimEvidenceLink[];
};

export type RecordIntakeDecisionPayload = {
	run_id: string;
	expected_revision: number;
	decision: OwnerDecision;
	claim_ids: string[];
	instruction: OwnerInstruction;
	superseded_decision_id: string | null;
	verified_relay_operation_id: UUID | null;
};

export type SupersedeIntakeClaimPayload = {
	run_id: string;
	expected_revision: number;
	old_claim_id: string;
	replacement_claim_id: string;
	decision_id: string;
};

export type RecordIntakeIssuePayload = {
	run_id: string;
	expected_revision: number;
	issue: Conflict | ClarificationQuestion;
	gaps: string[];
};

export type LinkIntakeCriterionPayload = {
	run_id: string;
	expected_revision: number;
	criterion: AcceptanceCriterion;
	requirements: RequirementClause[];
};

export type AssessIntakePayload = {
	run_id: string;
	expected_revision: number;
};

// ---- commands (models.py) ----

export type StartIntakeCommand = { type: "start_intake"; payload: StartIntakePayload };
export type RegisterIntakeEvidenceCommand = {
	type: "register_intake_evidence";
	payload: RegisterIntakeEvidencePayload;
};
export type RecordIntakeClaimCommand = { type: "record_intake_claim"; payload: RecordIntakeClaimPayload };
export type RecordIntakeDecisionCommand = { type: "record_intake_decision"; payload: RecordIntakeDecisionPayload };
export type SupersedeIntakeClaimCommand = { type: "supersede_intake_claim"; payload: SupersedeIntakeClaimPayload };
export type RecordIntakeIssueCommand = { type: "record_intake_issue"; payload: RecordIntakeIssuePayload };
export type LinkIntakeCriterionCommand = { type: "link_intake_criterion"; payload: LinkIntakeCriterionPayload };
export type AssessIntakeCommand = { type: "assess_intake"; payload: AssessIntakePayload };

export type IntakeCommand =
	| StartIntakeCommand
	| RegisterIntakeEvidenceCommand
	| RecordIntakeClaimCommand
	| RecordIntakeDecisionCommand
	| SupersedeIntakeClaimCommand
	| RecordIntakeIssueCommand
	| LinkIntakeCriterionCommand
	| AssessIntakeCommand;

// ---- results (api_models.py) ----

export type IntakeWriteResult = { run_id: string; revision: number; expected_revision: number };
export type StartIntakeResult = { type: "start_intake"; run_id: string; revision: number; expected_revision: 0 };

export type IntakeFollowOnResult = { run_id: string; revision: number; expected_revision: number };
export type RegisterIntakeEvidenceResult = { type: "register_intake_evidence" } & IntakeFollowOnResult;
export type RecordIntakeClaimResult = { type: "record_intake_claim" } & IntakeFollowOnResult;
export type RecordIntakeDecisionResult = { type: "record_intake_decision" } & IntakeFollowOnResult;
export type SupersedeIntakeClaimResult = { type: "supersede_intake_claim" } & IntakeFollowOnResult;
export type RecordIntakeIssueResult = { type: "record_intake_issue" } & IntakeFollowOnResult;
export type LinkIntakeCriterionResult = { type: "link_intake_criterion" } & IntakeFollowOnResult;

export type AssessIntakeResult = {
	type: "assess_intake";
	run_id: string;
	revision: number;
	assessed_revision: number;
};

export type IntakeCommandResult =
	| StartIntakeResult
	| RegisterIntakeEvidenceResult
	| RecordIntakeClaimResult
	| RecordIntakeDecisionResult
	| SupersedeIntakeClaimResult
	| RecordIntakeIssueResult
	| LinkIntakeCriterionResult
	| AssessIntakeResult;

// ---- read view (api_models.py) ----

export type IntakeProvenance = { run_id: string; revision: number; source_sha256: string };
export type IntakeDecisionRecord = {
	decision: OwnerDecision;
	claim_ids: string[];
	instruction: OwnerInstruction;
	superseded_decision_id: string | null;
	verified_relay_operation_id: UUID | null;
};
export type IntakeIssueRecord = { issue: Conflict | ClarificationQuestion; gaps: string[] };
export type IntakeCriterionLink = { criterion: AcceptanceCriterion; requirements: RequirementClause[] };
export type IntakeTrace = { criterion_id: string; requirement_ids: string[]; claim_ids: string[] };

export type IntakeSupportVerdict = "supports" | "contradicts" | "neither";
export type IntakeSupportJudgment = {
	claim_id: string;
	artifact_id: string;
	model_id: string;
	start_byte: number;
	end_byte: number;
	caller_relation: IntakeSupportVerdict;
	verdict: IntakeSupportVerdict;
	passage_sha256: string;
	claim_text_sha256: string;
	disagreement: boolean;
};

export type IntakeRecordView = {
	workspace_id: UUID;
	mission_id: UUID;
	run_id: string;
	revision: number;
	snapshot: IntakeSnapshot;
	evidence_links: IntakeClaimEvidenceLink[];
	support_judgments?: IntakeSupportJudgment[];
	criterion_links: IntakeCriterionLink[];
	decisions: IntakeDecisionRecord[];
	issues: IntakeIssueRecord[];
	traces: IntakeTrace[];
	assessments: AssessIntakeResult[];
	provenance: IntakeProvenance;
};
