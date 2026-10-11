"""Intake wire commands over caller-owned repository transactions."""
from __future__ import annotations

import hashlib
from uuid import UUID

from psycopg import Cursor

from ..contracts.nsi.v1 import models as nsi
from ..contracts.nsi.v1.validate import content_hash
from ..intake_repository import (
    IntakeRepositoryError, IntakeRevisionBody, append_revision, lock_revision,
    read_revision, revision_hashes,
)
from .api_models import IntakeProvenance, IntakeRecordView
from .models import CommandEnvelope, IntakeClaimEvidenceLink, StartIntakeCommand
from .store_shared import WorkStoreError


def intake_actor(kind: str, actor_id: object) -> nsi.ActorRef:
    if kind in ("agent", "task-agent"):
        kind = "model"
    if kind not in ("owner", "model", "tool", "rule_engine"):
        raise WorkStoreError("forbidden")
    return nsi.ActorRef(id=str(actor_id), kind=kind)


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
    raise WorkStoreError("unavailable")


def record_intake_claim(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> dict[str, object]:
    raise WorkStoreError("unavailable")


def execute_intake(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> tuple[dict[str, object], dict[str, object]]:
    handlers = {
        "start_intake": start_intake,
        "register_intake_evidence": register_intake_evidence,
        "record_intake_claim": record_intake_claim,
    }
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
        criterion_links=(), decisions=(), issues=(), traces=(), assessments=(),
        provenance=IntakeProvenance(
            run_id=run_id, revision=revision + 1, source_sha256=hashlib.sha256(source).hexdigest(),
        ),
    )
