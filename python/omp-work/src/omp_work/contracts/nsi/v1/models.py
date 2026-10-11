"""Strict NSI-1 runtime records; JSON Schemas own wire constraints."""
from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from .validate import validate_instance


class NSIModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    schema_name: ClassVar[str | None] = None

    @model_validator(mode="after")
    def validate_wire(self) -> Self:
        if self.schema_name is not None:
            validate_instance(self.schema_name, self.model_dump(exclude_unset=True))
        return self


IntakeRunState = Literal['collecting', 'reasoning', 'needs_spec', 'conflicted', 'ready_for_ratification', 'ratified', 'published', 'stale', 'invalid', 'superseded']

ClaimOrigin = Literal['owner_statement', 'policy_rule', 'repository_observation', 'tool_receipt', 'model_proposal', 'rule_derivation']

ClaimState = Literal['proposed', 'admitted', 'rejected', 'superseded']

ClaimModality = Literal['observed', 'desired', 'required', 'prohibited', 'possible', 'unknown']

Polarity = Literal['positive', 'negative']

IntakeCategory = Literal['functional_scope_and_behavior', 'domain_and_data_model', 'interaction_and_ux_flow', 'non_functional_qualities', 'integrations_and_external_dependencies', 'edge_cases_and_failure_handling', 'constraints_and_tradeoffs', 'terminology', 'completion_signals', 'placeholders_and_ambiguities', 'scope_boundaries']

ActorKind = Literal['owner', 'service', 'model', 'tool', 'rule_engine']

WaiverPolicy = Literal['never', 'owner_with_reason']

TruthValue = Literal["true", "false", "unknown"]


class Applicability(NSIModel):
    entity_types: list[str]
    predicate_ids: list[str]

class TypedStringValue(NSIModel):
    type: Literal['string']
    value: str

class TypedIntegerValue(NSIModel):
    type: Literal['integer']
    value: int

class TypedNumberValue(NSIModel):
    type: Literal['number']
    value: float | int

class TypedBooleanValue(NSIModel):
    type: Literal['boolean']
    value: bool

class TypedNullValue(NSIModel):
    type: Literal['null']
    value: None

class Gap(NSIModel):
    schema_name = "status#/$defs/Gap"
    id: str
    dimension_id: str
    premise_id: str
    reason: str
    materiality: Literal['critical', 'material', 'minor']
    waiver_policy: Literal['never', 'owner_with_reason']

class Conflict(NSIModel):
    schema_name = "status#/$defs/Conflict"
    id: str
    claim_ids: list[str]
    predicate_id: str
    reason: str

class ClarificationQuestion(NSIModel):
    schema_name = "status#/$defs/ClarificationQuestion"
    id: str
    question: str
    gap_ids: list[str]
    conflict_ids: list[str]
    category: IntakeCategory

class EvidenceSpan(NSIModel):
    schema_name = "claim#/$defs/EvidenceSpan"
    artifact_id: str
    start_byte: int
    end_byte: int
    quote: str | None = None

class RequirementClause(NSIModel):
    schema_name = "contract#/$defs/RequirementClause"
    id: str
    category: IntakeCategory
    text: str
    claim_ids: list[str]
    dimension_ids: list[str]

class Formalization(NSIModel):
    schema_name = "contract#/$defs/Formalization"
    id: str
    requirement_ids: list[str]
    language: str
    expression: str
    scope: str
    assumptions: list[str]
    content_hash: str

class Binding(NSIModel):
    schema_name = "contract#/$defs/Binding"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str

class EntityRef(NSIModel):
    schema_name = "taxonomy#/$defs/EntityRef"
    id: str
    type: str

class TypedEntityValue(NSIModel):
    type: Literal['entity']
    value: EntityRef

class PredicateDefinition(NSIModel):
    schema_name = "taxonomy#/$defs/PredicateDefinition"
    id: str
    title: str
    subject_types: list[str]
    object_types: list[str]
    cardinality: Literal['one', 'many']
    exclusivity: bool
    transitivity: bool
    authority: list[ActorKind]

class RequirementDimensionDefinition(NSIModel):
    schema_name = "taxonomy#/$defs/RequirementDimensionDefinition"
    id: str
    title: str
    category: IntakeCategory
    applicability: Applicability
    required_premises: list[str]
    materiality: Literal['critical', 'material', 'minor']
    waiver_policy: WaiverPolicy

class ActorRef(NSIModel):
    schema_name = "authority#/$defs/ActorRef"
    id: str
    kind: ActorKind

class EvidenceArtifact(NSIModel):
    schema_name = "claim#/$defs/EvidenceArtifact"
    id: str
    uri: str
    content_hash: str
    revision: str
    actor: ActorRef
    media_type: str

class FormalCheckResult(NSIModel):
    schema_name = "contract#/$defs/FormalCheckResult"
    id: str
    formalization_id: str
    formalization_hash: str
    status: Literal['true', 'false', 'unknown']
    scope: str
    evidence_ids: list[str]
    checked_by: ActorRef
    created_at: str

class OwnerRef(NSIModel):
    schema_name = "authority#/$defs/OwnerRef"
    id: str
    kind: Literal['owner']

class RatifiedFormalBinding(NSIModel):
    schema_name = "contract#/$defs/RatifiedFormalBinding"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    formalization_id: str
    formalization_hash: str
    check_result_id: str
    ratified_by: OwnerRef
    ratification_receipt_id: str

class RatificationReceipt(NSIModel):
    schema_name = "contract#/$defs/RatificationReceipt"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    assessment_id: str
    formal_binding_ids: list[str]
    ratified_by: OwnerRef
    created_at: str

class IntakePublicationReceipt(NSIModel):
    schema_name = "contract#/$defs/IntakePublicationReceipt"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    state: Literal['published']
    ratification_receipt_id: str
    readiness_receipt_id: str
    publishedBy: OwnerRef
    created_at: str

class RuleEngineRef(NSIModel):
    schema_name = "authority#/$defs/RuleEngineRef"
    id: str
    kind: Literal['rule_engine']

class DerivationReceipt(NSIModel):
    schema_name = "status#/$defs/DerivationReceipt"
    id: str
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    rule_id: str
    premise_ids: list[str]
    conclusion_ids: list[str]
    derived_by: RuleEngineRef
    created_at: str

class IntakeReadinessAssessment(NSIModel):
    schema_name = "contract#/$defs/IntakeReadinessAssessment"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    status: Literal['READY_FOR_RATIFICATION', 'NEEDS_SPEC', 'CONFLICTED']
    gaps: list[Gap]
    conflicts: list[Conflict]
    derivation_receipt_ids: list[str]
    assessed_by: RuleEngineRef
    created_at: str

class IntakeReadinessReceipt(NSIModel):
    schema_name = "contract#/$defs/IntakeReadinessReceipt"
    run_id: str
    revision: int
    contract_hash: str
    claim_set_hash: str
    rule_bundle_hash: str
    schema_hash: str
    source_hash: str
    status: Literal['ELIGIBLE_FOR_PUBLICATION']
    ratification_receipt_id: str
    formal_binding_ids: list[str]
    derivation_receipt_ids: list[str]
    issued_by: RuleEngineRef
    created_at: str

class WaiverDefinition(NSIModel):
    schema_name = "authority#/$defs/WaiverDefinition"
    id: str
    run_id: str
    revision: int
    premise_id: str
    dimension_id: str
    materiality: Literal['material', 'minor']
    waiver_policy: WaiverPolicy
    granted_by: OwnerRef
    reason: str
    decision_id: str
    created_at: str

class AuthorityMatrixRule(NSIModel):
    schema_name = "authority#/$defs/AuthorityMatrixRule"
    actor_kind: ActorKind
    allowed_operations: list[str]

class MutationEnvelope(NSIModel):
    schema_name = "authority#/$defs/MutationEnvelope"
    operation_id: str
    run_id: str
    expected_revision: int
    actor: ActorRef
    operation: str
    payload: dict[str, object]

class IntakeOntology(NSIModel):
    """Runtime aggregate of versioned taxonomy definitions."""
    predicates: list[PredicateDefinition]
    dimensions: list[RequirementDimensionDefinition]

TypedValue = TypedStringValue | TypedIntegerValue | TypedNumberValue | TypedBooleanValue | TypedNullValue | TypedEntityValue

class OwnerDecision(NSIModel):
    schema_name = "status#/$defs/OwnerDecision"
    id: str
    run_id: str
    revision: int
    actor: OwnerRef
    question_id: str
    answer: TypedValue
    reason: str
    created_at: str

class IntakeClaim(NSIModel):
    schema_name = "claim#/$defs/IntakeClaim"
    id: str
    category: IntakeCategory
    subject: EntityRef
    predicate_id: str
    value: TypedValue
    origin: ClaimOrigin
    state: ClaimState
    modality: ClaimModality
    polarity: Polarity
    truth: Literal['true', 'false', 'unknown']
    asserted_by: ActorRef
    evidence: list[EvidenceSpan]
    premise_ids: list[str]
    admitted_by: OwnerRef | RuleEngineRef | None = None

class AcceptanceCriterion(NSIModel):
    schema_name = "contract#/$defs/AcceptanceCriterion"
    id: str
    requirement_ids: list[str]
    description: str
    verification_method: str
    expected_result: TypedValue

class IntakeContract(NSIModel):
    schema_name = "contract#/$defs/IntakeContract"
    requirements: list[RequirementClause]
    acceptance_criteria: list[AcceptanceCriterion]
    formalizations: list[Formalization]
    waivers: list[WaiverDefinition]

class IntakeSnapshot(NSIModel):
    """Revisioned source and claim snapshot, independent of rendered layout."""
    run_id: str
    revision: int
    state: IntakeRunState
    artifacts: list[EvidenceArtifact]
    entities: list[EntityRef]
    claims: list[IntakeClaim]
    ontology: IntakeOntology

    @model_validator(mode="after")
    def nonnegative_revision(self) -> Self:
        if self.revision < 0:
            raise ValueError("revision must be nonnegative")
        return self

__all__ = [
    "Applicability",
    "TypedStringValue",
    "TypedIntegerValue",
    "TypedNumberValue",
    "TypedBooleanValue",
    "TypedNullValue",
    "TypedEntityValue",
    "IntakeRunState",
    "DerivationReceipt",
    "Gap",
    "Conflict",
    "ClarificationQuestion",
    "OwnerDecision",
    "ClaimOrigin",
    "ClaimState",
    "ClaimModality",
    "Polarity",
    "EvidenceArtifact",
    "EvidenceSpan",
    "IntakeClaim",
    "RequirementClause",
    "AcceptanceCriterion",
    "Formalization",
    "FormalCheckResult",
    "Binding",
    "RatifiedFormalBinding",
    "IntakeContract",
    "IntakeReadinessAssessment",
    "RatificationReceipt",
    "IntakeReadinessReceipt",
    "IntakePublicationReceipt",
    "IntakeCategory",
    "EntityRef",
    "PredicateDefinition",
    "RequirementDimensionDefinition",
    "ActorKind",
    "ActorRef",
    "OwnerRef",
    "RuleEngineRef",
    "WaiverPolicy",
    "WaiverDefinition",
    "AuthorityMatrixRule",
    "MutationEnvelope",
    "TypedValue",
    "TruthValue",
    "IntakeOntology",
    "IntakeSnapshot",
]
