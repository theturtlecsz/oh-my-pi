"""Intake wire commands over caller-owned repository transactions."""
from __future__ import annotations

from collections import Counter
import hashlib
from uuid import UUID

from psycopg import Cursor

from ..contracts.nsi.v1 import models as nsi
from ..contracts.nsi.v1.authority import AuthorityRefusal, check_claim_authority
from ..contracts.nsi.v1.validate import content_hash
from ..intake_repository import (
    EvidenceLinkRecord, IntakeRepositoryError, IntakeRevisionBody, append_revision,
    lock_revision, read_revision, revision_hashes,
)
from ..report_evidence import status_from_labels
from .api_models import IntakeProvenance, IntakeRecordView, IntakeTrace, RegisterIntakeEvidenceResult
from .intake_decisions import execute_decision_operation, intake_actor, open_issues, settlement_records
from .models import (
    CommandEnvelope, IntakeClaimEvidenceLink, RecordIntakeClaimCommand,
    RegisterIntakeEvidenceCommand, StartIntakeCommand,
)
from .store_shared import WorkStoreError


def start_intake(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> dict[str, object]:
    intake_actor(actor_kind, actor_id)
    if not isinstance(envelope.command, StartIntakeCommand):
        raise WorkStoreError("invalid_request")
    payload = envelope.command.payload
    cur.execute(
        "SELECT 1 FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
        (envelope.workspace_id, payload.mission_id),
    )
    if cur.fetchone() is None:
        raise WorkStoreError("invalid_request")
    try:
        locked = lock_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
            expected_revision=payload.expected_revision - 1,
            mission_id=payload.mission_id, operation_id=envelope.operation_id,
        )
        snapshot = nsi.IntakeSnapshot(
            run_id=payload.run_id, revision=payload.expected_revision, state="collecting",
            ontology=payload.ontology, entities=list(payload.entities), claims=[], artifacts=[],
        )
        claim_set_hash, source_hash = revision_hashes(snapshot, {})
        empty_hash = content_hash({})
        body = IntakeRevisionBody(
            workspace_id=envelope.workspace_id, snapshot=snapshot, artifact_bytes={},
            binding=nsi.Binding(
                run_id=payload.run_id, revision=snapshot.revision,
                claim_set_hash=claim_set_hash, source_hash=source_hash,
                rule_bundle_hash=empty_hash, schema_hash=empty_hash, contract_hash=empty_hash,
            ),
        )
        append_revision(cur, locked, body, operation_id=envelope.operation_id)
    except IntakeRepositoryError as exc:
        raise WorkStoreError(
            "stale_intake" if str(exc) == "stale_revision" else "invalid_request",
        ) from exc
    return dict(type=envelope.command.type, run_id=payload.run_id,
                expected_revision=payload.expected_revision, revision=payload.expected_revision + 1)


def register_intake_evidence(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> dict[str, object]:
    actor = intake_actor(actor_kind, actor_id)
    if not isinstance(envelope.command, RegisterIntakeEvidenceCommand):
        raise WorkStoreError("invalid_request")
    payload = envelope.command.payload
    wire = payload.expected_revision
    cur.execute(
        "SELECT mission_id,current_revision FROM omp_work.intake_heads "
        "WHERE workspace_id=%s AND run_id=%s",
        (envelope.workspace_id, payload.run_id),
    )
    head = cur.fetchone()
    if head is None or head[1] != wire - 1:
        raise WorkStoreError("stale_intake")
    raw = payload.source.text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != payload.artifact.content_hash or digest != payload.source.sha256:
        raise WorkStoreError("forbidden")
    if payload.artifact.actor != actor:
        raise WorkStoreError("forbidden")
    try:
        locked = lock_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
            expected_revision=wire - 1, mission_id=head[0],
            operation_id=envelope.operation_id,
        )
        stored = read_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id, revision=wire - 1,
        )
        previous = stored.snapshot
        if any(item.id == payload.artifact.id for item in previous.artifacts):
            raise WorkStoreError("invalid_request")
        for item in previous.artifacts:
            if item.uri == payload.artifact.uri and bytes(stored.artifact_bytes[item.id]) != raw:
                raise WorkStoreError("invalid_request")
        snapshot = nsi.IntakeSnapshot(
            run_id=payload.run_id, revision=wire, state=previous.state,
            ontology=previous.ontology, entities=list(previous.entities),
            claims=list(previous.claims), artifacts=[*previous.artifacts, payload.artifact],
        )
        artifact_bytes = {
            artifact_id: bytes(data) for artifact_id, data in stored.artifact_bytes.items()
        }
        artifact_bytes[payload.artifact.id] = raw
        claim_set_hash, source_hash = revision_hashes(snapshot, artifact_bytes)
        prior = stored.body.binding
        append_revision(cur, locked, IntakeRevisionBody(
            workspace_id=envelope.workspace_id, snapshot=snapshot, artifact_bytes=artifact_bytes,
            evidence_links=list(stored.body.evidence_links),
            conflicts=stored.body.conflicts, gaps=stored.body.gaps, questions=stored.body.questions,
            decisions=settlement_records(stored.body, wire),
            instructions=[i for i in stored.body.instructions
                          if i.decision_id in {d.decision.id for d in stored.body.history.decisions}],
            criteria=stored.body.criteria,
            supersessions=stored.body.supersessions, history=stored.body.history,
            binding=nsi.Binding(
                run_id=payload.run_id, revision=wire,
                claim_set_hash=claim_set_hash, source_hash=source_hash,
                rule_bundle_hash=prior.rule_bundle_hash, schema_hash=prior.schema_hash,
                contract_hash=prior.contract_hash,
            ),
        ), operation_id=envelope.operation_id)
    except IntakeRepositoryError as exc:
        raise WorkStoreError(
            "stale_intake" if str(exc) == "stale_revision" else "invalid_request",
        ) from exc
    return RegisterIntakeEvidenceResult(
        type="register_intake_evidence", run_id=payload.run_id,
        expected_revision=wire, revision=wire + 1,
    ).model_dump(mode="json")


class _RecordIntakeClaimResult(dict):
    def __init__(self, data: dict[str, object], evidence_status: str) -> None:
        super().__init__(data)
        self._evidence_status = evidence_status

    def __iter__(self):
        yield from super().__iter__()
        yield "evidence_status"

    def keys(self):
        yield from super().keys()
        yield "evidence_status"

    def __getitem__(self, key: str) -> object:
        if key == "evidence_status":
            return self._evidence_status
        return super().__getitem__(key)


def record_intake_claim(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> dict[str, object]:
    actor = intake_actor(actor_kind, actor_id)
    if not isinstance(envelope.command, RecordIntakeClaimCommand):
        raise WorkStoreError("invalid_request")
    payload = envelope.command.payload
    wire = payload.expected_revision
    cur.execute(
        "SELECT mission_id,current_revision FROM omp_work.intake_heads "
        "WHERE workspace_id=%s AND run_id=%s",
        (envelope.workspace_id, payload.run_id),
    )
    head = cur.fetchone()
    if head is None or head[1] != wire - 1:
        raise WorkStoreError("stale_intake")
    try:
        locked = lock_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
            expected_revision=wire - 1, mission_id=head[0],
            operation_id=envelope.operation_id,
        )
        stored = read_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id, revision=wire - 1,
        )
        previous = stored.snapshot
        claim = payload.claim
        existing_claim = next((c for c in previous.claims if c.id == claim.id), None)
        if existing_claim is not None:
            if existing_claim.state != "proposed" or claim.state != "admitted":
                raise WorkStoreError("invalid_request")
            if claim.admitted_by is None:
                raise WorkStoreError("invalid_request")
            if claim.model_dump(exclude={"state", "truth", "admitted_by"}) != existing_claim.model_dump(
                exclude={"state", "truth", "admitted_by"}
            ):
                raise WorkStoreError("invalid_request")
            stored_claim_links = [
                (link.artifact_id, link.start_byte, link.end_byte, link.quote, link.relation)
                for link in stored.body.evidence_links
                if link.claim_id == claim.id
            ]
            payload_claim_links = [
                (link.span.artifact_id, link.span.start_byte, link.span.end_byte, link.span.quote, link.relation)
                for link in payload.evidence_links
            ]
            if len(stored_claim_links) != len(payload_claim_links) or Counter(stored_claim_links) != Counter(
                payload_claim_links
            ):
                raise WorkStoreError("invalid_request")
        else:
            if claim.asserted_by != actor:
                raise WorkStoreError("forbidden")
            predicate = next((p for p in previous.ontology.predicates if p.id == claim.predicate_id), None)
            if predicate is None:
                raise WorkStoreError("invalid_request")
            stored_entities = {entity.id: entity.type for entity in previous.entities}
            subject_type = stored_entities.get(claim.subject.id)
            if subject_type is None or subject_type != claim.subject.type:
                raise WorkStoreError("invalid_request")
            if subject_type not in predicate.subject_types:
                raise WorkStoreError("invalid_request")
            if claim.value.type == "entity":
                target_id = claim.value.value.id
                target_type = stored_entities.get(target_id)
                if target_type is None or target_type != claim.value.value.type or target_type not in predicate.object_types:
                    raise WorkStoreError("invalid_request")
            previous_claim_ids = {c.id for c in previous.claims}
            for premise_id in claim.premise_ids:
                if premise_id == claim.id or premise_id not in previous_claim_ids:
                    raise WorkStoreError("invalid_request")
            if not claim.evidence or not payload.evidence_links:
                raise WorkStoreError("invalid_request")
            claim_spans = [(s.artifact_id, s.start_byte, s.end_byte, s.quote) for s in claim.evidence]
            if len(claim_spans) != len(set(claim_spans)):
                raise WorkStoreError("invalid_request")
            link_spans = [
                (link.span.artifact_id, link.span.start_byte, link.span.end_byte, link.span.quote)
                for link in payload.evidence_links
            ]
            if len(link_spans) != len(set(link_spans)):
                raise WorkStoreError("invalid_request")
            if set(claim_spans) != set(link_spans):
                raise WorkStoreError("invalid_request")
            known_artifact_ids = {a.id for a in previous.artifacts}
            if any(s.artifact_id not in known_artifact_ids for s in claim.evidence):
                raise WorkStoreError("invalid_request")

        try:
            check_claim_authority(
                claim, actor,
                predicates={p.id: p for p in previous.ontology.predicates},
                artifacts={a.id: a for a in previous.artifacts},
                repository_sources={
                    (a.uri, a.revision): bytes(stored.artifact_bytes[a.id])
                    for a in previous.artifacts
                },
            )
        except AuthorityRefusal as exc:
            raise WorkStoreError("forbidden") from exc

        added_links = [
            EvidenceLinkRecord(
                claim_id=claim.id,
                artifact_id=link.span.artifact_id,
                start_byte=link.span.start_byte,
                end_byte=link.span.end_byte,
                quote=link.span.quote,
                relation=link.relation,
            )
            for link in payload.evidence_links
        ]
        evidence_status = status_from_labels([link.relation for link in payload.evidence_links])

        if existing_claim is None:
            new_claims = [*previous.claims, claim]
            new_links = [*stored.body.evidence_links, *added_links]
        else:
            new_claims = [claim if c.id == claim.id else c for c in previous.claims]
            new_links = [link for link in stored.body.evidence_links if link.claim_id != claim.id] + added_links

        snapshot = nsi.IntakeSnapshot(
            run_id=payload.run_id, revision=wire, state=previous.state,
            ontology=previous.ontology, entities=list(previous.entities),
            claims=new_claims, artifacts=list(previous.artifacts),
        )
        artifact_bytes = {
            artifact_id: bytes(data) for artifact_id, data in stored.artifact_bytes.items()
        }
        claim_set_hash, source_hash = revision_hashes(snapshot, artifact_bytes)
        prior = stored.body.binding
        append_revision(cur, locked, IntakeRevisionBody(
            workspace_id=envelope.workspace_id, snapshot=snapshot, artifact_bytes=artifact_bytes,
            evidence_links=new_links,
            conflicts=stored.body.conflicts, gaps=stored.body.gaps, questions=stored.body.questions,
            decisions=settlement_records(stored.body, wire),
            instructions=[i for i in stored.body.instructions
                          if i.decision_id in {d.decision.id for d in stored.body.history.decisions}],
            criteria=stored.body.criteria,
            supersessions=stored.body.supersessions, history=stored.body.history,
            binding=nsi.Binding(
                run_id=payload.run_id, revision=wire,
                claim_set_hash=claim_set_hash, source_hash=source_hash,
                rule_bundle_hash=prior.rule_bundle_hash, schema_hash=prior.schema_hash,
                contract_hash=prior.contract_hash,
            ),
        ), operation_id=envelope.operation_id)
    except IntakeRepositoryError as exc:
        raise WorkStoreError(
            "stale_intake" if str(exc) == "stale_revision" else "invalid_request",
        ) from exc

    return _RecordIntakeClaimResult(
        {
            "type": envelope.command.type,
            "run_id": payload.run_id,
            "expected_revision": wire,
            "revision": wire + 1,
        },
        evidence_status,
    )


def execute_intake(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> tuple[dict[str, object], dict[str, object]]:
    handlers = {
        "start_intake": start_intake,
        "register_intake_evidence": register_intake_evidence,
        "record_intake_claim": record_intake_claim,
    }
    handlers.update(dict.fromkeys(("record_intake_decision", "supersede_intake_claim",
                                   "record_intake_issue", "link_intake_criterion"), execute_decision_operation))
    handler = handlers.get(envelope.command.type)
    if handler is None:
        raise WorkStoreError("invalid_request")
    result = handler(cur, envelope, actor_id, actor_kind)
    return result, dict(result)


def read_intake_view(cur: Cursor, workspace_id: UUID, value: str) -> IntakeRecordView:
    run_id = value
    revision = None
    if "@" in value:
        run_id, _, wire_revision = value.rpartition("@")
        if not wire_revision.isdecimal() or int(wire_revision) < 1:
            raise WorkStoreError("invalid_request")
        revision = int(wire_revision) - 1
    cur.execute(
        "SELECT mission_id,current_revision FROM omp_work.intake_heads "
        "WHERE workspace_id=%s AND run_id=%s", (workspace_id, run_id),
    )
    head = cur.fetchone()
    if head is None:
        raise WorkStoreError("invalid_request")
    if revision is None:
        revision = head[1]
    try:
        stored = read_revision(cur, workspace_id=workspace_id, run_id=run_id, revision=revision)
    except IntakeRepositoryError as exc:
        raise WorkStoreError("invalid_request") from exc
    source = next(iter(stored.artifact_bytes.values())) if len(stored.artifact_bytes) == 1 else b""
    return IntakeRecordView(
        workspace_id=workspace_id, mission_id=head[0], run_id=run_id, revision=revision + 1,
        snapshot=stored.snapshot,
        evidence_links=tuple(IntakeClaimEvidenceLink(
            span=nsi.EvidenceSpan(artifact_id=link.artifact_id, start_byte=link.start_byte,
                                  end_byte=link.end_byte, quote=link.quote),
            relation=link.relation,
        ) for link in stored.body.evidence_links),
        criterion_links=tuple(stored.body.history.criteria),
        decisions=tuple(stored.body.history.decisions), issues=open_issues(stored.body),
        traces=tuple(IntakeTrace(
            criterion_id=link.criterion.id, requirement_ids=tuple(link.criterion.requirement_ids),
            claim_ids=tuple(dict.fromkeys(cid for r in link.requirements for cid in r.claim_ids)),
        ) for link in stored.body.history.criteria), assessments=(),
        provenance=IntakeProvenance(
            run_id=run_id, revision=revision + 1, source_sha256=hashlib.sha256(source).hexdigest(),
        ),
    )
