"""Resolve one locked intake revision into a bounded projection and an exact closure.

Persisted rows in the same workspace and run are the only proof. Artifact URIs
are never fetched, and an unknown or false premise is never reported as true.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from psycopg import Cursor
from pydantic import ValidationError

from ..contracts.nsi.v1.compat import _ontology, nsi_to_bounded
from ..contracts.nsi.v1.models import (
    ActorRef,
    EvidenceArtifact,
    EvidenceSpan,
    IntakeClaim,
    OwnerRef,
    RuleEngineRef,
)
from ..contracts.nsi.v1.validate import premise_truth
from ..intake_repository import DecisionHistory, IntakeRevisionBody, revision_hashes
from .api_models import (
    IntakeAdmissionProvenance,
    IntakeClaimProvenance,
    IntakeDecisionProvenance,
    IntakeDecisionRecord,
    IntakeOperationRef,
    IntakeProvenance,
    IntakeRelayProvenance,
    IntakeSourceProvenance,
    IntakeSupersessionProvenance,
    IntakeTrace,
    RelayProvenance,
)
from .canonical import text_sha256
from .models import BoundedIntakeDraft, IntakeClaimEvidenceLink, IntakeSource, IntakeSourceSpan
from .store_shared import WorkStoreError

_SOURCE_ARTIFACT = "bounded-source"
_PREDICATE_SOURCE = "bounded.source_text"
_PREDICATE_SPAN = "bounded.source_span"
_PREDICATE_ACCEPTANCE = "bounded.acceptance_criterion"
_CERTIFYING_STATES = frozenset({"ready_for_ratification", "ratified", "published"})
_PREDICATE_FIELDS = (
    "title", "subject_types", "object_types", "cardinality", "exclusivity", "transitivity", "authority",
)
_DIMENSION_FIELDS = ("title", "category", "required_premises", "materiality", "waiver_policy")


@dataclass(frozen=True)
class AssertionRef:
    claim_id: str
    operation: IntakeOperationRef


@dataclass(frozen=True)
class ProvenanceClosure:
    projection: BoundedIntakeDraft
    closure: IntakeProvenance
    truths: tuple[tuple[str, str], ...]
    ready: bool
    assertions: tuple[AssertionRef, ...]


def _refuse(diagnostic: str) -> NoReturn:
    raise WorkStoreError("invalid_request", (diagnostic,))


def _rows(cur: Cursor, statement: str, params: tuple[object, ...], columns: tuple[str, ...]) -> list[dict[str, object]]:
    cur.execute(statement, params)
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _as_bytes(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    _refuse("inconsistent source")


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            _refuse("inconsistent source")
    if not isinstance(value, dict):
        _refuse("inconsistent source")
    return value


def _actor_pair(value: object) -> tuple[str, str]:
    payload = _as_dict(value)
    actor_id, kind = payload.get("id"), payload.get("kind")
    if not isinstance(actor_id, str) or not isinstance(kind, str) or not actor_id or not kind:
        _refuse("inconsistent source")
    return actor_id, kind


def _list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _refuse("inconsistent source")
    return list(value)


def _operation(cur: Cursor, workspace_id: UUID, operation_id: UUID) -> IntakeOperationRef:
    cur.execute(
        "SELECT event_id FROM omp_audit.domain_events "
        "WHERE workspace_id=%s AND operation_id=%s AND outcome='applied' "
        "ORDER BY sequence LIMIT 1",
        (workspace_id, operation_id),
    )
    row = cur.fetchone()
    return IntakeOperationRef(operation_id=operation_id, event_id=None if row is None else row[0])


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        _refuse("inconsistent source")
    return value


def _span_payload(claim: IntakeClaim) -> dict[str, object] | None:
    if claim.predicate_id != _PREDICATE_SPAN or claim.value.type != "string":
        return None
    try:
        payload = json.loads(claim.value.value)
    except json.JSONDecodeError:
        _refuse("inconsistent source")
    if not isinstance(payload, dict):
        _refuse("inconsistent source")
    return payload


def _slice_text(data: bytes, start: object, end: object) -> str:
    if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(data):
        _refuse("unknown span")
    try:
        return data[start:end].decode("utf-8")
    except UnicodeDecodeError:
        _refuse("unknown span")


def _walk(children: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            _refuse("cycle")
        if node in visited:
            return
        visiting.add(node)
        for nxt in children.get(node, ()):
            visit(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in children:
        visit(node)


def _ancestry(edges: dict[str, list[str]], claim_id: str) -> tuple[str, ...]:
    seen: list[str] = []
    known = set()
    queue = list(edges[claim_id])
    while queue:
        nxt = queue.pop(0)
        if nxt == claim_id or nxt in known:
            continue
        known.add(nxt)
        seen.append(nxt)
        queue.extend(edges.get(nxt, ()))
    return tuple(seen)


def resolve_provenance_closure(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceClosure:
    """Project the locked body and close every consumed premise back to persisted proof."""
    snapshot = body.snapshot
    workspace_id, run_id, revision = body.workspace_id, snapshot.run_id, snapshot.revision
    if (body.binding.run_id, body.binding.revision) != (run_id, revision):
        _refuse("inconsistent source")
    scope = (workspace_id, run_id)
    current = (*scope, revision)

    revision_columns = (
        "state", "operation_id", "source_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "contract_hash",
    )
    revisions = _rows(
        cur,
        "SELECT state,operation_id,source_hash,claim_set_hash,rule_bundle_hash,schema_hash,contract_hash "
        "FROM omp_work.intake_revisions WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        current, revision_columns,
    )
    if len(revisions) != 1 or revisions[0]["state"] != snapshot.state:
        _refuse("inconsistent source")
    stored_revision = revisions[0]
    for name in ("source_hash", "claim_set_hash", "rule_bundle_hash", "schema_hash", "contract_hash"):
        if getattr(body.binding, name) != stored_revision[name]:
            _refuse("inconsistent hash")

    artifact_columns = (
        "revision", "artifact_id", "bytes", "uri", "media_type", "content_hash",
        "source_revision", "source_actor", "operation_id",
    )
    artifact_rows = _rows(
        cur,
        "SELECT revision,artifact_id,bytes,uri,media_type,content_hash,source_revision,source_actor,operation_id "
        "FROM omp_work.intake_artifacts WHERE workspace_id=%s AND run_id=%s ORDER BY artifact_id,revision",
        scope, artifact_columns,
    )
    artifacts_by_id: dict[str, list[dict[str, object]]] = {}
    for row in artifact_rows:
        artifacts_by_id.setdefault(str(row["artifact_id"]), []).append(row)
    body_artifacts = {artifact.id: artifact for artifact in snapshot.artifacts}
    if set(body_artifacts) != set(body.artifact_bytes) or any(
        str(row["artifact_id"]) not in body_artifacts for row in artifact_rows if row["revision"] == revision
    ):
        _refuse("inconsistent source")
    current_artifact_ids = {str(row["artifact_id"]) for row in artifact_rows if row["revision"] == revision}
    if current_artifact_ids != set(body_artifacts):
        _refuse("inconsistent source")

    canonical_artifacts: dict[str, EvidenceArtifact] = {}
    artifact_bytes: dict[str, bytes] = {}
    artifact_operations: dict[str, UUID] = {}
    for artifact_id, versions in artifacts_by_id.items():
        if artifact_id not in body_artifacts:
            continue
        normalized = [dict(row, bytes=_as_bytes(row["bytes"])) for row in versions]
        first = normalized[0]
        if any(row["source_revision"] is None or row["source_actor"] is None for row in normalized):
            _refuse("inconsistent source")
        payload = _as_bytes(first["bytes"])
        content_hash = str(first["content_hash"])
        if any(_as_bytes(row["bytes"]) != payload or str(row["content_hash"]) != content_hash for row in normalized):
            _refuse("inconsistent hash")
        if hashlib.sha256(payload).hexdigest() != content_hash:
            _refuse("inconsistent hash")
        actor_id, actor_kind = _actor_pair(first["source_actor"])
        source_revision = str(first["source_revision"])
        uri, media_type = str(first["uri"]), str(first["media_type"])
        for row in normalized[1:]:
            if _actor_pair(row["source_actor"]) != (actor_id, actor_kind) or str(row["source_revision"]) != source_revision:
                _refuse("inconsistent source")
            if str(row["uri"]) != uri or str(row["media_type"]) != media_type:
                _refuse("inconsistent source")
        body_artifact = body_artifacts[artifact_id]
        body_payload = _as_bytes(body.artifact_bytes[artifact_id])
        if body_payload != payload or body_artifact.content_hash != content_hash or body_artifact.uri != uri:
            _refuse("inconsistent source")
        if body_artifact.revision != source_revision or (body_artifact.actor.id, body_artifact.actor.kind) != (actor_id, actor_kind):
            _refuse("inconsistent source")
        if body_artifact.media_type != media_type:
            _refuse("inconsistent source")
        artifact_bytes[artifact_id] = payload
        artifact_operations[artifact_id] = _require_uuid(first["operation_id"])
        canonical_artifacts[artifact_id] = EvidenceArtifact(
            id=artifact_id, uri=uri, content_hash=content_hash, revision=source_revision,
            actor=ActorRef(id=actor_id, kind=actor_kind), media_type=media_type,
        )
    claim_hash, source_hash = revision_hashes(snapshot, artifact_bytes)
    if (body.binding.claim_set_hash, body.binding.source_hash) != (claim_hash, source_hash):
        _refuse("inconsistent hash")

    claim_columns = (
        "revision", "operation_id", "claim_id", "predicate_id", "state", "truth", "value",
        "asserted_by", "asserted_kind", "admitted_by", "admitted_kind",
    )
    claim_rows = _rows(
        cur,
        "SELECT revision,operation_id,claim_id,predicate_id,state,truth,value,asserted_by,asserted_kind,"
        "admitted_by,admitted_kind FROM omp_work.intake_claims "
        "WHERE workspace_id=%s AND run_id=%s ORDER BY claim_id,revision",
        scope, claim_columns,
    )
    claims_by_id = {claim.id: claim for claim in snapshot.claims}
    if len(claims_by_id) != len(snapshot.claims):
        _refuse("inconsistent source")
    claims_sql: dict[str, list[dict[str, object]]] = {}
    for row in claim_rows:
        claims_sql.setdefault(str(row["claim_id"]), []).append(row)
    current_claims = {claim_id: versions for claim_id, versions in claims_sql.items() if any(v["revision"] == revision for v in versions)}
    if set(current_claims) != set(claims_by_id):
        _refuse("inconsistent source")

    premise_rows = _rows(
        cur,
        "SELECT claim_id,premise_id FROM omp_work.intake_claim_premises "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        current, ("claim_id", "premise_id"),
    )
    premises_sql: dict[str, list[str]] = {}
    for row in premise_rows:
        premises_sql.setdefault(str(row["claim_id"]), []).append(str(row["premise_id"]))
    for claim in snapshot.claims:
        versions = current_claims[claim.id]
        current_row = next(row for row in versions if row["revision"] == revision)
        if str(current_row["predicate_id"]) != claim.predicate_id or str(current_row["state"]) != claim.state:
            _refuse("inconsistent source")
        if str(current_row["truth"]) != claim.truth or current_row["value"] != claim.value.model_dump(mode="json"):
            _refuse("inconsistent source")
        if current_row["asserted_kind"] is None or str(current_row["asserted_by"]) != claim.asserted_by.id:
            _refuse("inconsistent source")
        if str(current_row["asserted_kind"]) != claim.asserted_by.kind:
            _refuse("inconsistent source")
        if sorted(claim.premise_ids) != sorted(premises_sql.get(claim.id, [])):
            _refuse("inconsistent source")
        if claim.admitted_by is None:
            body_admitted = None
        else:
            body_admitted = (claim.admitted_by.id, claim.admitted_by.kind)
        if current_row["admitted_by"] is None:
            sql_admitted = None
        else:
            sql_admitted = (str(current_row["admitted_by"]), None if current_row["admitted_kind"] is None else str(current_row["admitted_kind"]))
        if body_admitted != sql_admitted:
            _refuse("inconsistent source")
        if claim.state == "admitted" and (body_admitted is None or current_row["admitted_kind"] is None):
            _refuse("inconsistent source")

    evidence_columns = ("revision", "claim_id", "artifact_id", "start_byte", "end_byte", "quote", "relation")
    evidence_rows = _rows(
        cur,
        "SELECT revision,claim_id,artifact_id,start_byte,end_byte,quote,relation "
        "FROM omp_work.intake_claim_evidence WHERE workspace_id=%s AND run_id=%s "
        "ORDER BY claim_id,artifact_id,start_byte,end_byte,revision",
        scope, evidence_columns,
    )
    evidence_versions: dict[tuple[str, str, int, int], list[dict[str, object]]] = {}
    for row in evidence_rows:
        key = (str(row["claim_id"]), str(row["artifact_id"]), int(row["start_byte"]), int(row["end_byte"]))
        evidence_versions.setdefault(key, []).append(row)
    for versions in evidence_versions.values():
        if len({row["quote"] for row in versions}) != 1:
            _refuse("inconsistent quote")
        if len({row["relation"] for row in versions}) != 1:
            _refuse("inconsistent source")
    current_evidence = {}
    for key, versions in evidence_versions.items():
        found = [row for row in versions if row["revision"] == revision]
        if found:
            current_evidence[key] = found[0]
    body_evidence = {
        (link.claim_id, link.artifact_id, link.start_byte, link.end_byte, link.quote, link.relation)
        for link in body.evidence_links
    }
    sql_evidence = {
        (claim_id, artifact_id, start, end, None if row["quote"] is None else str(row["quote"]), str(row["relation"]))
        for (claim_id, artifact_id, start, end), row in current_evidence.items()
    }
    if body_evidence != sql_evidence:
        _refuse("inconsistent source")
    for claim in snapshot.claims:
        spans = {(span.artifact_id, span.start_byte, span.end_byte, span.quote) for span in claim.evidence}
        linked = {(artifact_id, start, end, quote) for claim_id, artifact_id, start, end, quote, _ in body_evidence if claim_id == claim.id}
        if spans != linked:
            _refuse("inconsistent source")

    decoded: dict[tuple[str, str, int, int], str] = {}
    for claim_id, artifact_id, start, end, quote, _relation in body_evidence:
        if artifact_id not in artifact_bytes:
            _refuse("dangling reference")
        text = _slice_text(artifact_bytes[artifact_id], start, end)
        if quote is not None and quote != text:
            _refuse("inconsistent quote")
        decoded[(claim_id, artifact_id, start, end)] = text

    span_ids: set[str] = set()
    span_claims: dict[str, IntakeClaim] = {}
    for claim in snapshot.claims:
        payload = _span_payload(claim)
        if payload is None:
            continue
        span_id, start, end, digest = payload.get("id"), payload.get("start"), payload.get("end"), payload.get("exact_text_sha256")
        if not isinstance(span_id, str) or not span_id or type(start) is not int or type(end) is not int or not isinstance(digest, str):
            _refuse("inconsistent source")
        if span_id in span_claims:
            _refuse("inconsistent source")
        span_ids.add(span_id)
        span_claims[span_id] = claim
        if claim.evidence:
            evidence = claim.evidence[0]
            if evidence.start_byte != start or evidence.end_byte != end:
                _refuse("inconsistent source")
            if text_sha256(decoded[(claim.id, evidence.artifact_id, evidence.start_byte, evidence.end_byte)]) != digest:
                _refuse("inconsistent hash")

    source_artifacts = [artifact for artifact in snapshot.artifacts if artifact.id == _SOURCE_ARTIFACT]
    if len(source_artifacts) != 1:
        _refuse("inconsistent source")
    try:
        source_text = artifact_bytes[_SOURCE_ARTIFACT].decode("utf-8")
    except UnicodeDecodeError:
        _refuse("inconsistent source")
    source_claims = [claim for claim in snapshot.claims if claim.predicate_id == _PREDICATE_SOURCE and claim.state == "admitted"]
    if len(source_claims) != 1 or source_claims[0].value.type != "string" or source_claims[0].value.value != source_text:
        _refuse("inconsistent source")

    ontology_columns = (
        "definition_id", "kind", "title", "subject_types", "object_types", "cardinality", "exclusivity",
        "transitivity", "authority", "category", "required_premises", "materiality", "waiver_policy",
    )
    ontology_rows = _rows(
        cur,
        "SELECT definition_id,kind,title,subject_types,object_types,cardinality,exclusivity,transitivity,authority,"
        "category,required_premises,materiality,waiver_policy FROM omp_work.intake_ontology "
        "WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        current, ontology_columns,
    )
    bounded = _ontology()
    bounded_predicates = {item.id: item for item in bounded.predicates}
    bounded_dimensions = {item.id: item for item in bounded.dimensions}
    supersession_rows = _rows(
        cur,
        "SELECT revision,operation_id,claim_id,superseded_claim_id FROM omp_work.intake_claim_supersessions "
        "WHERE workspace_id=%s AND run_id=%s ORDER BY claim_id,superseded_claim_id,revision",
        scope, ("revision", "operation_id", "claim_id", "superseded_claim_id"),
    )
    current_pairs = {
        (str(row["claim_id"]), str(row["superseded_claim_id"]))
        for row in supersession_rows if row["revision"] == revision
    }
    body_pairs = {
        (replacement, old)
        for replacement, olds in body.supersessions.items()
        for old in olds
    }
    if current_pairs != body_pairs:
        _refuse("inconsistent source")
    superseded_ids = {old for _replacement, old in current_pairs}
    active = [claim for claim in snapshot.claims if claim.state == "admitted" and claim.id not in superseded_ids]
    for claim in snapshot.claims:
        listed = claim.id in superseded_ids
        if claim.state == "admitted" and listed:
            _refuse("superseded reference")
        if claim.state == "superseded" and not listed:
            _refuse("inconsistent source")
    for claim in active:
        if claim.predicate_id not in bounded_predicates:
            _refuse("foreign predicate")
    predicate_rows = [row for row in ontology_rows if row["kind"] == "predicate"]
    dimension_rows = [row for row in ontology_rows if row["kind"] == "dimension"]
    if {str(row["definition_id"]) for row in predicate_rows} != set(bounded_predicates):
        _refuse("foreign ontology")
    if {str(row["definition_id"]) for row in dimension_rows} != set(bounded_dimensions):
        _refuse("foreign ontology")
    for row in predicate_rows:
        expected = bounded_predicates[str(row["definition_id"])]
        dumped = expected.model_dump()
        for field in _PREDICATE_FIELDS:
            actual = _list(row[field]) if field in ("subject_types", "object_types", "authority") else row[field]
            if actual != dumped[field]:
                _refuse("foreign ontology")
    for row in dimension_rows:
        expected = bounded_dimensions[str(row["definition_id"])]
        dumped = expected.model_dump()
        for field in _DIMENSION_FIELDS:
            actual = _list(row[field]) if field == "required_premises" else row[field]
            if actual != dumped[field]:
                _refuse("foreign ontology")

    edges: dict[str, list[str]] = {}
    for claim in snapshot.claims:
        claim_edges: list[str] = []
        for premise_id in claim.premise_ids:
            if premise_id in claims_by_id:
                target = claims_by_id[premise_id]
            elif premise_id in span_claims:
                target = span_claims[premise_id]
            else:
                _refuse("dangling reference")
            if premise_id in claims_by_id:
                claim_edges.append(premise_id)
            if claim.state != "admitted":
                continue
            if target.state == "superseded" or target.id in superseded_ids:
                _refuse("superseded reference")
            if target.state != "admitted":
                _refuse("unadmitted reference")
        edges[claim.id] = claim_edges
    _walk(edges)
    supersession_children: dict[str, list[str]] = {}
    for replacement, old in current_pairs:
        supersession_children.setdefault(replacement, []).append(old)
    _walk(supersession_children)

    history_rows = _rows(
        cur,
        "SELECT revision,records FROM omp_work.intake_decision_history "
        "WHERE workspace_id=%s AND run_id=%s ORDER BY revision",
        scope, ("revision", "records"),
    )
    if not history_rows:
        _refuse("dangling reference")
    current_history_row = next((row for row in history_rows if row["revision"] == revision), None)
    if current_history_row is None:
        _refuse("inconsistent source")
    history = DecisionHistory.model_validate(current_history_row["records"])

    history_links = {link.criterion.id: link for link in history.criteria}
    if len(history_links) != len(history.criteria):
        _refuse("inconsistent source")
    used: list[str] = []
    for claim in active:
        if claim.predicate_id == _PREDICATE_ACCEPTANCE:
            used.append(claim.id)
    used.extend(criterion.id for criterion in body.criteria)
    used.extend(link.criterion.id for link in history.criteria)
    used = list(dict.fromkeys(used))
    criterion_rows = _rows(
        cur,
        "SELECT criterion_id FROM omp_work.intake_criteria WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        current, ("criterion_id",),
    )
    if {str(row["criterion_id"]) for row in criterion_rows} != {criterion.id for criterion in body.criteria}:
        _refuse("inconsistent source")
    traces: list[IntakeTrace] = []
    for criterion_id in used:
        link = history_links.get(criterion_id)
        if link is None:
            _refuse("missing criterion trace")
        requirement_ids = list(link.criterion.requirement_ids)
        if [item.id for item in link.requirements] != requirement_ids or len(requirement_ids) != len(set(requirement_ids)):
            _refuse("missing criterion trace")
        claim_ids: list[str] = []
        for requirement in link.requirements:
            if not requirement.claim_ids:
                _refuse("dangling reference")
            for claim_id in requirement.claim_ids:
                target = claims_by_id.get(claim_id)
                if target is None:
                    _refuse("dangling reference")
                if target.state != "admitted" or claim_id in superseded_ids:
                    _refuse("unadmitted reference")
                if claim_id not in claim_ids:
                    claim_ids.append(claim_id)
            for dimension_id in requirement.dimension_ids:
                if dimension_id not in bounded_dimensions:
                    _refuse("inconsistent source")
        traces.append(IntakeTrace(
            criterion_id=link.criterion.id,
            requirement_ids=tuple(requirement_ids),
            claim_ids=tuple(claim_ids),
        ))

    claim_map = {claim.id: claim for claim in snapshot.claims}
    truths = tuple((claim.id, premise_truth(claim_map, claim.id)) for claim in active)
    ready = all(value == "true" for _claim_id, value in truths)
    assessment_rows = _rows(
        cur,
        "SELECT status FROM omp_work.intake_assessments WHERE workspace_id=%s AND run_id=%s AND revision=%s",
        current, ("status",),
    )
    certifying = snapshot.state in _CERTIFYING_STATES or any(
        str(row["status"]) == "ready_for_ratification" for row in assessment_rows
    ) or any(item.assessment.status == "READY_FOR_RATIFICATION" for item in body.assessments)
    if certifying and not ready:
        if any(value == "unknown" for _claim_id, value in truths):
            _refuse("unresolved truth")
        _refuse("false premise")

    cur.execute(
        "SELECT mission_id FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id=%s",
        scope,
    )
    head = cur.fetchone()
    if head is None:
        _refuse("dangling reference")
    mission_id = head[0]

    decision_rows = _rows(
        cur,
        "SELECT revision,operation_id,decision_id,question_id,owner_id FROM omp_work.intake_owner_decisions "
        "WHERE workspace_id=%s AND run_id=%s ORDER BY decision_id,revision",
        scope, ("revision", "operation_id", "decision_id", "question_id", "owner_id"),
    )
    decisions_sql: dict[str, list[dict[str, object]]] = {}
    for row in decision_rows:
        decisions_sql.setdefault(str(row["decision_id"]), []).append(row)
    settled_rows = _rows(
        cur,
        "SELECT revision,decision_id,claim_id FROM omp_work.intake_settled_claims "
        "WHERE workspace_id=%s AND run_id=%s",
        scope, ("revision", "decision_id", "claim_id"),
    )
    instruction_rows = _rows(
        cur,
        "SELECT revision,decision_id,claim_id,artifact_id,instruction FROM omp_work.intake_instruction_provenances "
        "WHERE workspace_id=%s AND run_id=%s ORDER BY provenance_id,revision",
        scope, ("revision", "decision_id", "claim_id", "artifact_id", "instruction"),
    )
    decision_provenance: list[IntakeDecisionProvenance] = []
    relays: list[IntakeRelayProvenance] = []
    seen_relays: set[UUID] = set()
    for record in history.decisions:
        versions = decisions_sql.get(record.decision.id)
        if not versions:
            _refuse("dangling reference")
        earliest = versions[0]
        if int(earliest["revision"]) != record.decision.revision:
            _refuse("inconsistent source")
        if str(earliest["question_id"]) != record.decision.question_id or str(earliest["owner_id"]) != record.decision.actor.id:
            _refuse("inconsistent source")
        settled = {
            str(row["claim_id"]) for row in settled_rows
            if row["decision_id"] == record.decision.id and row["revision"] == earliest["revision"]
        }
        if settled != set(record.claim_ids):
            _refuse("inconsistent source")
        text = record.instruction.text
        provenances = [row for row in instruction_rows if row["decision_id"] == record.decision.id]
        if not provenances or not any(row["revision"] == earliest["revision"] for row in provenances):
            _refuse("dangling reference")
        for provenance in provenances:
            if str(provenance["instruction"]) != text:
                _refuse("inconsistent relay" if record.verified_relay_operation_id is not None else "inconsistent source")
            artifact_id = str(provenance["artifact_id"])
            if artifact_id not in artifact_bytes or artifact_bytes[artifact_id] != text.encode("utf-8"):
                _refuse("inconsistent relay" if record.verified_relay_operation_id is not None else "inconsistent source")
        digest = history.instruction_hashes.get(record.decision.id)
        if digest is not None and digest != hashlib.sha256(text.encode("utf-8")).hexdigest():
            _refuse("inconsistent hash")
        if record.verified_relay_operation_id is not None:
            relay_model, relay_operation = _relay(cur, workspace_id, mission_id, record)
            if record.verified_relay_operation_id not in seen_relays:
                seen_relays.add(record.verified_relay_operation_id)
                relays.append(IntakeRelayProvenance(relay=relay_model, operation=relay_operation))
        decision_provenance.append(IntakeDecisionProvenance(
            record=record, operation=_operation(cur, workspace_id, _require_uuid(earliest["operation_id"])),
        ))

    supersession_versions: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in supersession_rows:
        supersession_versions.setdefault((str(row["claim_id"]), str(row["superseded_claim_id"])), []).append(row)
    supersessions: list[IntakeSupersessionProvenance] = []
    for replacement, old in sorted(current_pairs):
        decision_id = history.claim_supersession_decisions.get(old)
        if decision_id is None or all(item.decision.id != decision_id for item in history.decisions):
            _refuse("dangling reference")
        earliest = supersession_versions[(replacement, old)][0]
        supersessions.append(IntakeSupersessionProvenance(
            old_claim_id=old, replacement_claim_id=replacement, decision_id=decision_id,
            operation=_operation(cur, workspace_id, _require_uuid(earliest["operation_id"])),
        ))

    links_for: dict[str, list[IntakeClaimEvidenceLink]] = {}
    for claim_id, artifact_id, start, end, quote, relation in sorted(body_evidence, key=lambda item: (item[0], item[1], item[2], item[3])):
        span_fields = {"artifact_id": artifact_id, "start_byte": start, "end_byte": end}
        if quote is not None:
            span_fields["quote"] = quote
        links_for.setdefault(claim_id, []).append(IntakeClaimEvidenceLink(
            span=EvidenceSpan(**span_fields), relation=relation,
        ))
    claim_provenance: list[IntakeClaimProvenance] = []
    assertions: list[AssertionRef] = []
    admissions: list[IntakeAdmissionProvenance] = []
    for claim in active:
        versions = claims_sql[claim.id]
        earliest_claim = versions[0]
        if earliest_claim["asserted_kind"] is None:
            _refuse("inconsistent source")
        if (str(earliest_claim["asserted_by"]), str(earliest_claim["asserted_kind"])) != (claim.asserted_by.id, claim.asserted_by.kind):
            _refuse("inconsistent source")
        assertions.append(AssertionRef(
            claim_id=claim.id,
            operation=_operation(cur, workspace_id, _require_uuid(earliest_claim["operation_id"])),
        ))
        admitted_versions = [
            row for row in versions if row["state"] == "admitted" or row["admitted_by"] is not None
        ]
        if not admitted_versions or admitted_versions[0]["admitted_by"] is None or admitted_versions[0]["admitted_kind"] is None:
            _refuse("dangling reference")
        admission = admitted_versions[0]
        if (str(admission["admitted_by"]), str(admission["admitted_kind"])) != (claim.admitted_by.id, claim.admitted_by.kind):
            _refuse("inconsistent source")
        kind = str(admission["admitted_kind"])
        if kind == "owner":
            admitted_by: OwnerRef | RuleEngineRef = OwnerRef(id=str(admission["admitted_by"]), kind="owner")
        elif kind == "rule_engine":
            admitted_by = RuleEngineRef(id=str(admission["admitted_by"]), kind="rule_engine")
        else:
            _refuse("inconsistent source")
        admissions.append(IntakeAdmissionProvenance(
            claim_id=claim.id, admitted_by=admitted_by,
            operation=_operation(cur, workspace_id, _require_uuid(admission["operation_id"])),
        ))
        ancestry = _ancestry(edges, claim.id)
        evidence_links = {key: tuple(links_for.get(key, ())) for key in (claim.id, *ancestry)}
        artifact_ids: list[str] = []
        for key in (claim.id, *ancestry):
            for link in evidence_links[key]:
                if link.span.artifact_id not in artifact_ids:
                    artifact_ids.append(link.span.artifact_id)
        claim_provenance.append(IntakeClaimProvenance(
            claim=claim, ancestry_ids=ancestry, evidence_links=evidence_links,
            artifacts=tuple(canonical_artifacts[artifact_id] for artifact_id in artifact_ids),
        ))

    filtered_claims = [
        claim.model_copy(update={"premise_ids": [premise_id for premise_id in claim.premise_ids if premise_id in span_ids]})
        for claim in active
    ]
    try:
        projection = nsi_to_bounded(snapshot.model_copy(update={
            "ontology": bounded,
            "artifacts": [canonical_artifacts[_SOURCE_ARTIFACT]],
            "claims": filtered_claims,
            "entities": _entities(filtered_claims),
        }))
    except (ValidationError, ValueError):
        _refuse("inconsistent source")

    sources: list[IntakeSourceProvenance] = []
    referenced = {span.artifact_id for claim in active for span in claim.evidence}
    for artifact in snapshot.artifacts:
        payload = artifact_bytes[artifact.id]
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            if artifact.id in referenced:
                _refuse("inconsistent source")
            continue
        spans: list[IntakeSourceSpan] = []
        if artifact.id == _SOURCE_ARTIFACT:
            for claim in active:
                payload_span = _span_payload(claim)
                if payload_span is None or not claim.evidence or claim.evidence[0].artifact_id != artifact.id:
                    continue
                spans.append(IntakeSourceSpan(
                    id=str(payload_span["id"]), start=int(payload_span["start"]), end=int(payload_span["end"]),
                    exact_text_sha256=str(payload_span["exact_text_sha256"]),
                ))
        sources.append(IntakeSourceProvenance(
            artifact=canonical_artifacts[artifact.id],
            source=IntakeSource(text=text, sha256=text_sha256(text), spans=tuple(spans)),
            operation=_operation(cur, workspace_id, artifact_operations[artifact.id]),
        ))

    closure = IntakeProvenance(
        run_id=run_id, revision=revision,
        source_sha256=hashlib.sha256(artifact_bytes[_SOURCE_ARTIFACT]).hexdigest(),
        sources=tuple(sources), claims=tuple(claim_provenance), admissions=tuple(admissions),
        decisions=tuple(decision_provenance), supersessions=tuple(supersessions),
        relays=tuple(relays), traces=tuple(traces),
    )
    return ProvenanceClosure(
        projection=projection, closure=closure, truths=truths, ready=ready, assertions=tuple(assertions),
    )


def _entities(claims: list[IntakeClaim]):
    entities = []
    seen: set[str] = set()
    for claim in claims:
        if claim.subject.id in seen:
            continue
        seen.add(claim.subject.id)
        entities.append(claim.subject)
    return entities


def _relay(
    cur: Cursor, workspace_id: UUID, mission_id: UUID, record: IntakeDecisionRecord,
) -> tuple[RelayProvenance, IntakeOperationRef]:
    operation_id = record.verified_relay_operation_id
    if operation_id is None:
        _refuse("dangling reference")
    cur.execute(
        "SELECT j.response,e.payload,e.event_id FROM omp_control.idempotent_commands j "
        "JOIN omp_audit.domain_events e USING (workspace_id,operation_id) "
        "WHERE j.workspace_id=%s AND j.operation_id=%s AND j.command_type='relay_owner_intent' "
        "AND j.state='applied' AND e.aggregate_type='mission' AND e.aggregate_id=%s "
        "AND e.outcome='applied' ORDER BY e.sequence LIMIT 1",
        (workspace_id, operation_id, mission_id),
    )
    row = cur.fetchone()
    if row is None:
        _refuse("dangling reference")
    response, payload, event_id = row
    if not isinstance(response, dict) or not isinstance(payload, dict):
        _refuse("inconsistent relay")
    journal = response.get("relay")
    event_relay = payload.get("relay")
    if not isinstance(journal, dict) or journal != event_relay:
        _refuse("inconsistent relay")
    try:
        relay = RelayProvenance.model_validate(journal)
    except ValidationError:
        _refuse("inconsistent relay")
    instruction = record.instruction
    if (instruction.provenance.channel != "relay"
            or instruction.text != relay.instruction.text
            or instruction.provenance.message_ref != relay.instruction.source_message_ref
            or instruction.provenance.received_at != relay.instruction.received_at):
        _refuse("inconsistent relay")
    return relay, IntakeOperationRef(operation_id=operation_id, event_id=event_id)
