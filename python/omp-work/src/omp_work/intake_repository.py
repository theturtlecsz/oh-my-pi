"""NSI-2 reads and writes using a caller-owned cursor and transaction.

Artifact URIs are immutable byte bindings within a run. Bytes come exclusively
from the caller; this module never resolves a URI. Callers must roll back on any
failure, including deferred constraint failures at commit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from psycopg import Cursor, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .contracts.nsi.v1 import models
from .contracts.nsi.v1.validate import content_hash
from .intake_support import SupportReceipt
from .v1.api_models import IntakeAssessmentView, IntakeCriterionLink, IntakeDecisionRecord, IntakeIssueRecord

INTAKE_REVISION_ADVANCED = "intake_revision_advanced"


class IntakeRepositoryError(ValueError):
    """A rejected repository operation; message is a stable reason code."""


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


class EvidenceLinkRecord(_Record):
    claim_id: str
    artifact_id: str
    start_byte: int
    end_byte: int
    quote: str | None = None
    relation: Literal["supports", "contradicts", "neither"]
    support_receipt: SupportReceipt | None = Field(default=None, exclude_if=lambda value: value is None)


class DecisionHistory(_Record):
    decisions: list[IntakeDecisionRecord] = Field(default_factory=list)
    issues: list[IntakeIssueRecord] = Field(default_factory=list)
    criteria: list[IntakeCriterionLink] = Field(default_factory=list)
    instruction_hashes: dict[str, str] = Field(default_factory=dict)
    claim_supersession_decisions: dict[str, str] = Field(default_factory=dict)
    relays: dict[str, dict[str, object]] = Field(default_factory=dict)


class IntakeRevisionBody(_Record):
    """Storage envelope around unchanged NSI/v1 records.

    Binding hashes describe the appended snapshot. Assessments retain their own
    assessed revision. Artifact bytes are keyed by snapshot artifact ID.
    """
    workspace_id: UUID
    snapshot: models.IntakeSnapshot
    binding: models.Binding
    artifact_bytes: dict[str, bytes]
    evidence_links: list[EvidenceLinkRecord] = Field(default_factory=list)
    conflicts: list[models.Conflict] = Field(default_factory=list)
    gaps: list[models.Gap] = Field(default_factory=list)
    questions: list[models.ClarificationQuestion] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    instructions: list[InstructionProvenance] = Field(default_factory=list)
    criteria: list[models.AcceptanceCriterion] = Field(default_factory=list)
    assessments: list[AssessmentRecord] = Field(default_factory=list)
    derivations: list[models.DerivationReceipt] = Field(default_factory=list)
    supersessions: dict[str, list[str]] = Field(default_factory=dict)
    history: DecisionHistory = Field(default_factory=DecisionHistory)


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


def append_assessment(
    cur: Cursor, locked: LockedHead, view: IntakeAssessmentView, operation_id: UUID,
) -> None:
    """Append evaluation records at the locked semantic revision, without advancing it.

    Evaluation bindings belong to the evaluation, not the revision's legacy
    hash placeholders. Full views retain provenance, issues and receipt links.
    """
    view = IntakeAssessmentView.model_validate_json(view.model_dump_json(exclude_unset=True))
    binding = view.binding
    if ((binding.run_id, binding.revision) != (locked.run_id, locked.current_revision)
        or (view.provenance.run_id, view.provenance.revision) != (locked.run_id, locked.current_revision)):
        raise IntakeRepositoryError("scope_mismatch")
    binding_fields = binding.model_dump()
    for record in (view.assessment, *view.receipts):
        if {name: getattr(record, name) for name in binding_fields} != binding_fields:
            raise IntakeRepositoryError("scope_mismatch")
    if set(view.assessment.derivation_receipt_ids) != {receipt.id for receipt in view.receipts}:
        raise IntakeRepositoryError("receipt_links_mismatch")
    common = dict(workspace_id=locked.workspace_id, run_id=locked.run_id,
                  revision=locked.current_revision, operation_id=operation_id, nsi_version="nsi/v1")
    assessment = view.assessment
    _insert(cur, "intake_assessments", common | dict(
        assessment_id=view.id, assessed_revision=assessment.revision,
        status=assessment.status.lower(), assessed_by=assessment.assessed_by.id,
        created_at=assessment.created_at,
        full_record=Jsonb(view.model_dump(mode="json", exclude_unset=True)),
    ))
    # Full receipts also name requirements and criteria. This relation indexes
    # only snapshot claims; the complete typed references remain in full_record.
    cur.execute(
        "SELECT claim_id FROM omp_work.intake_claims "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        (locked.workspace_id, locked.run_id, locked.current_revision),
    )
    claim_ids = {row[0] for row in cur.fetchall()}
    for receipt in view.receipts:
        _insert(cur, "intake_derivations", common | dict(
            derivation_id=receipt.id, rule_id=receipt.rule_id,
            derived_by=receipt.derived_by.id, created_at=receipt.created_at,
            full_record=Jsonb(receipt.model_dump(mode="json", exclude_unset=True)),
        ))
        for role, ids in (("premise", receipt.premise_ids), ("conclusion", receipt.conclusion_ids)):
            for claim_id in ids:
                if claim_id not in claim_ids:
                    continue
                _insert(cur, "intake_derivation_claims", common | dict(
                    derivation_id=receipt.id, claim_id=claim_id, role=role,
                ))


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
            uri=artifact.uri, media_type=artifact.media_type, content_hash=artifact.content_hash,
            source_revision=artifact.revision, source_actor=Jsonb(artifact.actor.model_dump()))
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
            asserted_kind=claim.asserted_by.kind,
            admitted_kind=claim.admitted_by.kind if claim.admitted_by else None,
            **value_fields(claim.value, "value"))
    links = {(link.claim_id, link.artifact_id, link.start_byte, link.end_byte): link
             for link in body.evidence_links}
    for claim in snapshot.claims:
        for evidence in claim.evidence:
            link = links.get((claim.id, evidence.artifact_id, evidence.start_byte, evidence.end_byte))
            put("claim_evidence", claim_id=claim.id, **evidence.model_dump(),
                relation=link.relation if link is not None else "neither",
                support_receipt=Jsonb(link.support_receipt.model_dump(mode="json"))
                if link is not None and link.support_receipt is not None else None)
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
    put("decision_history", records=Jsonb(body.history.model_dump(mode="json")))
    if locked.current_revision >= 0:
        cur.execute(
            "UPDATE omp_work.intake_heads SET current_revision=%s "
            "WHERE workspace_id=%s AND run_id=%s AND current_revision=%s",
            (revision, locked.workspace_id, locked.run_id, locked.current_revision),
        )
        if cur.rowcount != 1:
            raise IntakeRepositoryError("stale_revision")
        hold_bindings(cur, workspace_id=locked.workspace_id, mission_id=locked.mission_id,
                      code=INTAKE_REVISION_ADVANCED)
    return revision


def hold_bindings(cur: Cursor, *, workspace_id: UUID, mission_id: UUID, code: str) -> None:
    """Hold every open mission binding; its revision and assessment stay put.

    Bindings are history: a hold only marks the row so a consumer stops acting
    on the revision it was admitted on. The append that advances the head owns
    the transaction, so the hold is atomic with the new revision.
    """
    cur.execute(
        "UPDATE omp_work.mission_intake_bindings SET state='held', hold_code=%s "
        "WHERE workspace_id=%s AND mission_id=%s AND state IN ('prepared','admitted')",
        (code, workspace_id, mission_id),
    )


@dataclass(frozen=True)
class IntakeRevisionView:
    revision: int
    snapshot: models.IntakeSnapshot
    artifact_bytes: dict[str, bytes]
    rule_bundle_hash: str
    schema_hash: str
    body: IntakeRevisionBody
    assessments: tuple[IntakeAssessmentView, ...] = ()

    @property
    def claim_set_hash(self) -> str:
        return revision_hashes(self.snapshot, self.artifact_bytes)[0]

    @property
    def source_hash(self) -> str:
        return revision_hashes(self.snapshot, self.artifact_bytes)[1]


def revision_hashes(
    snapshot: models.IntakeSnapshot, artifact_bytes: dict[str, bytes],
) -> tuple[str, str]:
    """Hash the persisted claim/source projection, without invented metadata.

    NSI-2 does not store actor kinds, artifact actor/source revision, or dimension
    applicability. Those cannot participate in a reproducible repository hash.
    Entity value types, evidence spans, and exact source bytes do participate.
    """
    claims = []
    for claim in snapshot.claims:
        record = claim.model_dump(exclude={"asserted_by", "admitted_by"})
        record["evidence"] = sorted(record["evidence"], key=lambda span:
                                    (span["artifact_id"], span["start_byte"], span["end_byte"]))
        record["asserted_by"] = claim.asserted_by.id
        record["admitted_by"] = claim.admitted_by.id if claim.admitted_by else None
        claims.append(record)
    sources = [dict(id=artifact.id, uri=artifact.uri, media_type=artifact.media_type,
                    digest=artifact.content_hash, bytes=artifact_bytes[artifact.id].hex())
               for artifact in snapshot.artifacts]
    return content_hash({"claims": claims}), content_hash({"artifacts": sources})


def read_revision(
    cur: Cursor, *, workspace_id: UUID, run_id: str, revision: int,
) -> IntakeRevisionView:
    """Read immutable history in the caller transaction, with workspace RLS.

    Missing and invisible revision keys have the same error. Legacy NULL
    provenance falls back to the writing operation (service), storage revision,
    and origin-derived claim actor kinds. Applicability is unrestricted.
    Hashes exclude actor kinds, source revision, and evidence relation.
    """
    key = (workspace_id, run_id, revision)

    def rows(table: str, *, operation_id: str | None = None) -> list[dict]:
        scope = "revision" if operation_id is None else "operation_id"
        cur.execute(sql.SQL(
            "SELECT to_jsonb(t) FROM omp_work.{} t "
            "WHERE workspace_id=%s AND run_id=%s AND {}=%s ORDER BY to_jsonb(t)::text"
        ).format(sql.Identifier("intake_" + table), sql.Identifier(scope)),
            key if operation_id is None else (workspace_id, run_id, operation_id))
        return [row[0] for row in cur.fetchall()]

    revisions = rows("revisions")
    if not revisions:
        raise IntakeRepositoryError("revision_not_found")
    stored = revisions[0]
    binding_fields = {name: stored[name] for name in
                      ("contract_hash", "claim_set_hash", "source_hash", "rule_bundle_hash", "schema_hash")}
    binding = models.Binding(run_id=run_id, revision=revision, **binding_fields)
    artifacts = rows("artifacts")
    cur.execute(
        "SELECT artifact_id,bytes FROM omp_work.intake_artifacts "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s", key,
    )
    artifact_bytes = dict(cur.fetchall())
    entities = [models.EntityRef(id=row["entity_id"], type=row["entity_type"])
                for row in rows("entities")]
    entity_refs = {entity.id: entity for entity in entities}
    evidence, premises = rows("claim_evidence"), rows("claim_premises")
    origin_kinds = dict(owner_statement="owner", policy_rule="rule_engine",
                        repository_observation="tool", tool_receipt="tool",
                        model_proposal="model", rule_derivation="rule_engine")
    claims = []
    for row in rows("claims"):
        fields = {name: row[name] for name in
                  ("category", "predicate_id", "value", "origin", "state", "modality", "polarity", "truth")}
        fields.update(id=row["claim_id"], subject=entity_refs[row["subject_entity_id"]],
                      asserted_by=dict(id=row["asserted_by"], kind=row["asserted_kind"]
                                       if row["asserted_kind"] is not None else origin_kinds[row["origin"]]),
                      evidence=[{name: span[name] for name in
                                 ("artifact_id", "start_byte", "end_byte", "quote")
                                 if span[name] is not None}
                                for span in sorted(evidence, key=lambda span:
                                                   (span["artifact_id"], span["start_byte"], span["end_byte"]))
                                if span["claim_id"] == row["claim_id"]],
                      premise_ids=[link["premise_id"] for link in premises
                                   if link["claim_id"] == row["claim_id"]])
        if row["admitted_by"] is not None:
            fields["admitted_by"] = dict(id=row["admitted_by"], kind=
                                        row["admitted_kind"] if row["admitted_kind"] is not None else
                                        "rule_engine" if row["origin"] in ("policy_rule", "rule_derivation") else "owner")
        claims.append(models.IntakeClaim.model_validate(fields))
    predicates, dimensions = [], []
    for row in rows("ontology"):
        if row["kind"] == "predicate":
            predicates.append(models.PredicateDefinition.model_validate(dict(
                id=row["definition_id"], **{name: row[name] for name in
                ("title", "subject_types", "object_types", "cardinality", "exclusivity", "transitivity", "authority")})))
        else:
            dimensions.append(models.RequirementDimensionDefinition.model_validate(dict(
                id=row["definition_id"], applicability=dict(entity_types=[], predicate_ids=[]),
                **{name: row[name] for name in
                   ("title", "category", "required_premises", "materiality", "waiver_policy")})))
    snapshot = models.IntakeSnapshot(
        run_id=run_id, revision=revision, state=stored["state"], entities=entities, claims=claims,
        ontology=models.IntakeOntology(predicates=predicates, dimensions=dimensions),
        artifacts=[models.EvidenceArtifact(
            id=row["artifact_id"], uri=row["uri"], content_hash=row["content_hash"],
            media_type=row["media_type"], revision=row["source_revision"]
            if row["source_revision"] is not None else str(revision),
            actor=models.ActorRef.model_validate(row["source_actor"])
            if row["source_actor"] is not None else
            models.ActorRef(id=row["operation_id"], kind="service")) for row in artifacts],
    )
    issues, issue_claims, question_issues = rows("issues"), rows("issue_claims"), rows("question_issues")
    conflicts = [models.Conflict(
        id=row["issue_id"], predicate_id=row["predicate_id"], reason=row["reason"],
        claim_ids=[link["claim_id"] for link in issue_claims if link["issue_id"] == row["issue_id"]])
        for row in issues if row["kind"] == "conflict"]
    gaps = [models.Gap.model_validate(dict(id=row["issue_id"], **{name: row[name] for name in
            ("dimension_id", "premise_id", "reason", "materiality", "waiver_policy")}))
            for row in issues if row["kind"] == "gap"]
    issue_kinds = {row["issue_id"]: row["kind"] for row in issues}
    questions = [models.ClarificationQuestion(
        id=row["issue_id"], question=row["question"], category=row["category"],
        gap_ids=[link["issue_id"] for link in question_issues
                 if link["question_id"] == row["issue_id"] and issue_kinds[link["issue_id"]] == "gap"],
        conflict_ids=[link["issue_id"] for link in question_issues
                      if link["question_id"] == row["issue_id"] and issue_kinds[link["issue_id"]] == "conflict"])
        for row in issues if row["kind"] == "question"]
    settled = rows("settled_claims")
    decisions = [DecisionRecord(
        decision=models.OwnerDecision(
            id=row["decision_id"], run_id=run_id, revision=revision,
            actor=models.OwnerRef(id=row["owner_id"], kind="owner"),
            **{name: row[name] for name in ("question_id", "answer", "reason", "created_at")}),
        settled_claim_ids=[link["claim_id"] for link in settled if link["decision_id"] == row["decision_id"]])
        for row in rows("owner_decisions")]
    derivation_rows = rows("derivations")
    derivation_claims = rows("derivation_claims")
    derivations = [models.DerivationReceipt(
        id=row["derivation_id"], **binding.model_dump(), rule_id=row["rule_id"],
        derived_by=models.RuleEngineRef(id=row["derived_by"], kind="rule_engine"), created_at=row["created_at"],
        premise_ids=[link["claim_id"] for link in derivation_claims
                     if link["derivation_id"] == row["derivation_id"] and link["role"] == "premise"],
        conclusion_ids=[link["claim_id"] for link in derivation_claims
                        if link["derivation_id"] == row["derivation_id"] and link["role"] == "conclusion"])
        for row in derivation_rows if row["full_record"] is None]
    assessments = []
    full_receipts = {
        row["derivation_id"]: models.DerivationReceipt.model_validate(row["full_record"])
        for row in derivation_rows if row["full_record"] is not None
    }
    assessment_views = tuple(
        IntakeAssessmentView.model_validate_json(json.dumps(row["full_record"]))
        for row in rows("assessments") if row["full_record"] is not None
    )
    assessment_views = tuple(view.model_copy(update={
        "receipts": tuple(full_receipts[receipt.id] for receipt in view.receipts),
    }) for view in assessment_views)
    # Legacy semantic appends may own assessments stored at a prior revision.
    # Keep that read path only for summaries; full records are revision-local.
    for row in rows("assessments", operation_id=stored["operation_id"]):
        if row["full_record"] is not None:
            continue
        cur.execute(
            "SELECT source_hash,claim_set_hash,rule_bundle_hash,schema_hash,contract_hash "
            "FROM omp_work.intake_revisions WHERE workspace_id=%s AND run_id=%s AND revision=%s",
            (workspace_id, run_id, row["assessed_revision"]),
        )
        assessed = cur.fetchone()
        if assessed is None:
            raise IntakeRepositoryError("revision_not_found")
        assessed_binding = dict(zip(
            ("source_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "contract_hash"), assessed))
        assessments.append(AssessmentRecord(id=row["assessment_id"], assessment=models.IntakeReadinessAssessment(
            run_id=run_id, revision=row["assessed_revision"], **assessed_binding,
            status=row["status"].upper(), assessed_by=models.RuleEngineRef(id=row["assessed_by"], kind="rule_engine"),
            created_at=row["created_at"], gaps=gaps, conflicts=conflicts,
            derivation_receipt_ids=[receipt.id for receipt in derivations])))
    supersessions = {}
    for row in rows("claim_supersessions"):
        supersessions.setdefault(row["claim_id"], []).append(row["superseded_claim_id"])
    history_rows = rows("decision_history")
    body = IntakeRevisionBody(
        history=DecisionHistory.model_validate(history_rows[0]["records"]) if history_rows else DecisionHistory(),
        workspace_id=workspace_id, snapshot=snapshot, binding=binding, artifact_bytes=artifact_bytes,
        evidence_links=[EvidenceLinkRecord.model_validate({name: row[name] for name in
                        ("claim_id", "artifact_id", "start_byte", "end_byte", "quote", "relation", "support_receipt")})
                        for row in evidence],
        conflicts=conflicts, gaps=gaps, questions=questions, decisions=decisions,
        instructions=[InstructionProvenance.model_validate(dict(id=row["provenance_id"],
                      **{name: row[name] for name in ("decision_id", "claim_id", "artifact_id", "instruction")}))
                      for row in rows("instruction_provenances")],
        criteria=[models.AcceptanceCriterion.model_validate(dict(id=row["criterion_id"],
                  **{name: row[name] for name in ("requirement_ids", "description", "verification_method", "expected_result")}))
                  for row in rows("criteria")],
        assessments=assessments, derivations=derivations, supersessions=supersessions,
    )
    # Evaluation records stay outside the semantic body so copying it into a
    # subsequent semantic revision cannot carry forward an old READY assessment.
    return IntakeRevisionView(
        revision, snapshot, artifact_bytes, binding.rule_bundle_hash, binding.schema_hash,
        body, assessment_views,
    )
