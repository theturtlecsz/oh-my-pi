"""NSI-2 writes using a caller-owned cursor and transaction.

Artifact URIs are immutable byte bindings within a run. Bytes come exclusively
from the caller; this module never resolves a URI. Callers must roll back on any
failure, including deferred constraint failures at commit.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import Cursor, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .contracts.nsi.v1 import models


class IntakeRepositoryError(ValueError):
    """A rejected repository mutation; message is a stable reason code."""


@dataclass(frozen=True)
class LockedHead:
    workspace_id: UUID
    run_id: str
    mission_id: UUID
    current_revision: int


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class DecisionRecord(_Record):
    decision: models.OwnerDecision
    settled_claim_ids: list[str] = Field(default_factory=list)


class AssessmentRecord(_Record):
    id: str
    assessment: models.IntakeReadinessAssessment


class InstructionProvenance(_Record):
    id: str
    decision_id: str
    claim_id: str
    artifact_id: str
    instruction: str


class IntakeRevisionBody(_Record):
    """Storage envelope around unchanged NSI/v1 records.

    Binding hashes describe the appended snapshot. Assessments retain their own
    assessed revision. Artifact bytes are keyed by snapshot artifact ID.
    """
    workspace_id: UUID
    snapshot: models.IntakeSnapshot
    binding: models.Binding
    artifact_bytes: dict[str, bytes]
    conflicts: list[models.Conflict] = Field(default_factory=list)
    gaps: list[models.Gap] = Field(default_factory=list)
    questions: list[models.ClarificationQuestion] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    instructions: list[InstructionProvenance] = Field(default_factory=list)
    criteria: list[models.AcceptanceCriterion] = Field(default_factory=list)
    assessments: list[AssessmentRecord] = Field(default_factory=list)
    derivations: list[models.DerivationReceipt] = Field(default_factory=list)
    supersessions: dict[str, list[str]] = Field(default_factory=dict)


def lock_revision(
    cur: Cursor, *, workspace_id: UUID, run_id: str, expected_revision: int,
    mission_id: UUID, operation_id: UUID,
) -> LockedHead:
    """Serialize writers, including competing first writes, without committing."""
    key = (workspace_id, run_id)
    cur.execute(
        "SELECT mission_id,current_revision FROM omp_work.intake_heads "
        "WHERE workspace_id=%s AND run_id=%s FOR UPDATE", key,
    )
    row = cur.fetchone()
    if row is None:
        if expected_revision != -1:
            raise IntakeRepositoryError("stale_revision")
        cur.execute(
            "SELECT 1 FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
            (workspace_id, mission_id),
        )
        if cur.fetchone() is None:
            raise IntakeRepositoryError("stale_revision")
        cur.execute(
            "INSERT INTO omp_work.intake_heads "
            "(workspace_id,run_id,mission_id,current_revision,operation_id,nsi_version) "
            "VALUES (%s,%s,%s,0,%s,'nsi/v1') ON CONFLICT DO NOTHING RETURNING current_revision",
            (*key, mission_id, operation_id),
        )
        inserted = cur.fetchone() is not None
        cur.execute(
            "SELECT mission_id,current_revision FROM omp_work.intake_heads "
            "WHERE workspace_id=%s AND run_id=%s FOR UPDATE", key,
        )
        row = cur.fetchone()
        if inserted:
            return LockedHead(workspace_id, run_id, mission_id, -1)
    if row is None or row[0] != mission_id or row[1] != expected_revision:
        raise IntakeRepositoryError("stale_revision")
    return LockedHead(workspace_id, run_id, mission_id, row[1])


def _insert(cur: Cursor, table: str, values: dict[str, object]) -> None:
    cur.execute(
        sql.SQL("INSERT INTO omp_work.{} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ), list(values.values()),
    )


def append_revision(
    cur: Cursor, locked: LockedHead, body: IntakeRevisionBody | dict[str, object], *,
    operation_id: UUID,
) -> int:
    """Validate and append all records, advancing the head only after inserts."""
    # Revalidate model instances too: callers may have mutated nested records.
    body = IntakeRevisionBody.model_validate(
        body.model_dump(exclude_unset=True) if isinstance(body, IntakeRevisionBody) else body,
    )
    snapshot, binding = body.snapshot, body.binding
    revision = locked.current_revision + 1
    if (body.workspace_id != locked.workspace_id
        or (snapshot.run_id, snapshot.revision) != (locked.run_id, revision)
        or (binding.run_id, binding.revision) != (locked.run_id, revision)):
        raise IntakeRepositoryError("scope_mismatch")
    for record in [*(item.decision for item in body.decisions), *body.derivations]:
        if (record.run_id, record.revision) != (locked.run_id, revision):
            raise IntakeRepositoryError("scope_mismatch")
    for item in body.assessments:
        if item.assessment.run_id != locked.run_id:
            raise IntakeRepositoryError("scope_mismatch")
    entities = {entity.id: entity.type for entity in snapshot.entities}
    if len(entities) != len(snapshot.entities):
        raise IntakeRepositoryError("entity_type_mismatch")

    def entity_ref(ref: models.EntityRef) -> None:
        if entities.get(ref.id) != ref.type:
            raise IntakeRepositoryError("entity_type_mismatch")

    def value_fields(value: models.TypedValue, column: str) -> dict[str, object]:
        fields: dict[str, object] = {column: Jsonb(value.model_dump())}
        if isinstance(value, models.TypedEntityValue):
            entity_ref(value.value)
            fields.update(value_entity_id=value.value.id, value_entity_type=value.value.type)
        return fields

    for claim in snapshot.claims:
        entity_ref(claim.subject)
        value_fields(claim.value, "value")
    for item in body.decisions:
        value_fields(item.decision.answer, "answer")
    for criterion in body.criteria:
        value_fields(criterion.expected_result, "expected_result")
    if set(body.artifact_bytes) != {artifact.id for artifact in snapshot.artifacts}:
        raise IntakeRepositoryError("artifact_bytes_mismatch")
    uri_bytes: dict[str, bytes] = {}
    for artifact in snapshot.artifacts:
        data = body.artifact_bytes[artifact.id]
        if artifact.uri in uri_bytes and uri_bytes[artifact.uri] != data:
            raise IntakeRepositoryError("uri_rebind")
        uri_bytes[artifact.uri] = data
        cur.execute(
            "SELECT 1 FROM omp_work.intake_artifacts "
            "WHERE workspace_id=%s AND run_id=%s AND uri=%s AND bytes<>%s LIMIT 1",
            (locked.workspace_id, locked.run_id, artifact.uri, data),
        )
        if cur.fetchone() is not None:
            raise IntakeRepositoryError("uri_rebind")

    common = dict(workspace_id=locked.workspace_id, run_id=locked.run_id,
                  revision=revision, operation_id=operation_id, nsi_version="nsi/v1")

    def put(table: str, **fields: object) -> None:
        _insert(cur, "intake_" + table, common | fields)

    put("revisions", state=snapshot.state, **{
        name: getattr(binding, name) for name in
        ("source_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "contract_hash")
    })
    for artifact in snapshot.artifacts:
        put("artifacts", artifact_id=artifact.id, bytes=body.artifact_bytes[artifact.id],
            uri=artifact.uri, media_type=artifact.media_type, content_hash=artifact.content_hash)
    for kind, definitions in (("predicate", snapshot.ontology.predicates),
                              ("dimension", snapshot.ontology.dimensions)):
        for definition in definitions:
            fields = definition.model_dump(exclude={"id", "applicability"})
            put("ontology", definition_id=definition.id, kind=kind, **fields)
    for entity in snapshot.entities:
        put("entities", entity_id=entity.id, entity_type=entity.type)
    for claim in snapshot.claims:
        put("claims", claim_id=claim.id, category=claim.category, subject_entity_id=claim.subject.id,
            predicate_id=claim.predicate_id, origin=claim.origin, state=claim.state,
            modality=claim.modality, polarity=claim.polarity, truth=claim.truth,
            asserted_by=claim.asserted_by.id, admitted_by=claim.admitted_by.id if claim.admitted_by else None,
            **value_fields(claim.value, "value"))
    for claim in snapshot.claims:
        for evidence in claim.evidence:
            put("claim_evidence", claim_id=claim.id, **evidence.model_dump())
        for premise in claim.premise_ids:
            put("claim_premises", claim_id=claim.id, premise_id=premise)
    for claim_id, superseded_ids in body.supersessions.items():
        for superseded in superseded_ids:
            put("claim_supersessions", claim_id=claim_id, superseded_claim_id=superseded)
    for conflict in body.conflicts:
        put("issues", issue_id=conflict.id, kind="conflict", predicate_id=conflict.predicate_id,
            reason=conflict.reason)
        for claim_id in conflict.claim_ids:
            put("issue_claims", issue_id=conflict.id, claim_id=claim_id)
    for gap in body.gaps:
        put("issues", issue_id=gap.id, kind="gap", **gap.model_dump(exclude={"id"}))
    for question in body.questions:
        put("issues", issue_id=question.id, kind="question", reason=question.question,
            question=question.question, category=question.category)
        for issue in question.gap_ids + question.conflict_ids:
            put("question_issues", question_id=question.id, issue_id=issue)
    for item in body.decisions:
        decision = item.decision
        put("owner_decisions", decision_id=decision.id, question_id=decision.question_id,
            owner_id=decision.actor.id, reason=decision.reason, created_at=decision.created_at,
            **value_fields(decision.answer, "answer"))
        for claim_id in item.settled_claim_ids:
            put("settled_claims", decision_id=decision.id, claim_id=claim_id)
    for instruction in body.instructions:
        put("instruction_provenances", provenance_id=instruction.id,
            **instruction.model_dump(exclude={"id"}))
    for criterion in body.criteria:
        put("criteria", criterion_id=criterion.id,
            **criterion.model_dump(exclude={"id", "expected_result"}),
            **value_fields(criterion.expected_result, "expected_result"))
    for item in body.assessments:
        assessment = item.assessment
        put("assessments", revision=assessment.revision, assessment_id=item.id, assessed_revision=assessment.revision,
            status=assessment.status.lower(), assessed_by=assessment.assessed_by.id,
            created_at=assessment.created_at)
    for derivation in body.derivations:
        put("derivations", derivation_id=derivation.id, rule_id=derivation.rule_id,
            derived_by=derivation.derived_by.id, created_at=derivation.created_at)
        for role, ids in (("premise", derivation.premise_ids), ("conclusion", derivation.conclusion_ids)):
            for claim_id in ids:
                put("derivation_claims", derivation_id=derivation.id, claim_id=claim_id, role=role)
    if locked.current_revision >= 0:
        cur.execute(
            "UPDATE omp_work.intake_heads SET current_revision=%s "
            "WHERE workspace_id=%s AND run_id=%s AND current_revision=%s",
            (revision, locked.workspace_id, locked.run_id, locked.current_revision),
        )
        if cur.rowcount != 1:
            raise IntakeRepositoryError("stale_revision")
    return revision
