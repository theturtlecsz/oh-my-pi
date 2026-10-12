"""Owner settlements and immutable intake traces in the caller transaction."""
from __future__ import annotations

import hashlib
from uuid import UUID

from psycopg import Cursor

from ..contracts.nsi.v1 import models as nsi
from ..contracts.nsi.v1.authority import AuthorityRefusal, check_decision_authority
from ..intake_repository import (
    DecisionRecord, IntakeRepositoryError, IntakeRevisionBody, InstructionProvenance,
    append_revision, lock_revision, read_revision, revision_hashes,
)
from .api_models import IntakeCriterionLink, IntakeDecisionRecord, IntakeIssueRecord
from .models import (
    CommandEnvelope, LinkIntakeCriterionCommand, RecordIntakeDecisionCommand,
    RecordIntakeIssueCommand, SupersedeIntakeClaimCommand,
)
from .store_shared import WorkStoreError


def intake_actor(kind: str, actor_id: object) -> nsi.ActorRef:
    if kind in ("agent", "task-agent"):
        kind = "model"
    if kind not in ("owner", "model", "tool", "rule_engine"):
        raise WorkStoreError("forbidden")
    return nsi.ActorRef(id=str(actor_id), kind=kind)


def _require(condition: bool) -> None:
    if not condition:
        raise WorkStoreError("invalid_request")


def _relay(cur: Cursor, envelope: CommandEnvelope, mission_id: UUID) -> dict[str, object] | None:
    payload = envelope.command.payload
    ref = payload.verified_relay_operation_id
    if ref is None:
        return None
    cur.execute(
        "SELECT j.response, e.payload FROM omp_control.idempotent_commands j "
        "JOIN omp_audit.domain_events e USING (workspace_id,operation_id) "
        "WHERE j.workspace_id=%s AND j.operation_id=%s AND j.command_type='relay_owner_intent' "
        "AND j.state='applied' AND e.aggregate_type='mission' AND e.aggregate_id=%s "
        "AND e.outcome='applied'",
        (envelope.workspace_id, ref, mission_id),
    )
    row = cur.fetchone()
    if row is None:
        raise WorkStoreError("forbidden")
    relay = row[0].get("relay")
    if not isinstance(relay, dict) or relay != row[1].get("relay"):
        raise WorkStoreError("forbidden")
    instruction = relay.get("instruction")
    provenance = payload.instruction.provenance
    if (not isinstance(instruction, dict)
        or instruction.get("text") != payload.instruction.text
        or instruction.get("source_message_ref") != provenance.message_ref
        or instruction.get("received_at") != provenance.model_dump(mode="json")["received_at"]
        or provenance.channel != "relay"
        or not isinstance(relay.get("signed"), bool)
        or not isinstance(relay.get("relayed_by"), str)):
        raise WorkStoreError("forbidden")
    return relay


def _decision(cur: Cursor, envelope: CommandEnvelope, actor: nsi.ActorRef,
              mission_id: UUID, body: IntakeRevisionBody) -> None:
    payload = envelope.command.payload
    decision = payload.decision
    try:
        check_decision_authority(decision, actor)
    except AuthorityRefusal as exc:
        raise WorkStoreError("forbidden") from exc
    _require((decision.run_id, decision.revision) == (payload.run_id, payload.expected_revision))
    _require(bool(payload.claim_ids) and len(payload.claim_ids) == len(set(payload.claim_ids)))
    claims = {c.id: c for c in body.snapshot.claims}
    _require(set(payload.claim_ids) <= claims.keys())
    question = next((q for q in body.questions if q.id == decision.question_id), None)
    _require(question is not None)
    conflicts = {c.id: c for c in body.conflicts}
    gaps = {g.id: g for g in body.gaps}
    required = {cid for issue_id in question.conflict_ids for cid in conflicts[issue_id].claim_ids}
    gap_ids = set(question.gap_ids)
    gap_ids.update(gap_id for record in body.history.issues
                   if record.issue.id in question.conflict_ids for gap_id in record.gaps)
    required.update(gaps[issue_id].premise_id for issue_id in gap_ids)
    _require(required <= set(payload.claim_ids))
    history = body.history
    _require(all(d.decision.id != decision.id for d in history.decisions))
    _require(all(d.decision.id != decision.id for d in body.decisions))
    prior_id = payload.superseded_decision_id
    previous = next((d for d in history.decisions if d.decision.id == prior_id), None)
    if prior_id is not None:
        _require(previous is not None and previous.decision.question_id == decision.question_id)
        _require(previous.decision.actor == decision.actor)
        _require(all(d.superseded_decision_id != prior_id for d in history.decisions))
    else:
        _require(all(d.decision.question_id != decision.question_id for d in history.decisions))
    relay = _relay(cur, envelope, mission_id)
    record = IntakeDecisionRecord(
        decision=decision, claim_ids=payload.claim_ids, instruction=payload.instruction,
        superseded_decision_id=prior_id, verified_relay_operation_id=payload.verified_relay_operation_id,
    )
    history.decisions.append(record)
    raw = payload.instruction.text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    history.instruction_hashes[decision.id] = digest
    if relay is not None:
        history.relays[decision.id] = relay
    artifact_id = f"instruction:{decision.id}"
    _require(all(a.id != artifact_id for a in body.snapshot.artifacts))
    body.snapshot.artifacts.append(nsi.EvidenceArtifact(
        id=artifact_id, uri=f"intake:{payload.run_id}/instruction/{decision.id}",
        revision=str(payload.expected_revision), content_hash=digest, media_type="text/plain", actor=actor,
    ))
    body.artifact_bytes[artifact_id] = raw
    body.decisions.append(DecisionRecord(decision=decision, settled_claim_ids=list(payload.claim_ids)))
    body.instructions.extend(InstructionProvenance(
        id=f"{decision.id}:{claim_id}", decision_id=decision.id, claim_id=claim_id,
        artifact_id=artifact_id, instruction=payload.instruction.text,
    ) for claim_id in payload.claim_ids)


def _supersede(envelope: CommandEnvelope, actor: nsi.ActorRef, body: IntakeRevisionBody) -> None:
    if actor.kind != "owner":
        raise WorkStoreError("forbidden")
    payload = envelope.command.payload
    claims = {c.id: c for c in body.snapshot.claims}
    old, replacement = payload.old_claim_id, payload.replacement_claim_id
    _require(old != replacement and old in claims and replacement in claims)
    _require(claims[replacement].state == "admitted" and claims[old].state != "superseded")
    decision = next((d for d in body.history.decisions if d.decision.id == payload.decision_id), None)
    if decision is None:
        decision = next((d for d in body.decisions if d.decision.id == payload.decision_id), None)
    _require(decision is not None)
    try:
        check_decision_authority(decision.decision, actor)
    except AuthorityRefusal as exc:
        raise WorkStoreError("forbidden") from exc
    settled = decision.claim_ids if isinstance(decision, IntakeDecisionRecord) else decision.settled_claim_ids
    _require({old, replacement} <= set(settled))
    _require(all(d.superseded_decision_id != payload.decision_id for d in body.history.decisions))
    pending, visited = [old], set()
    while pending:
        item = pending.pop()
        _require(item != replacement)
        if item not in visited:
            visited.add(item)
            pending.extend(body.supersessions.get(item, []))
    body.supersessions.setdefault(replacement, []).append(old)
    body.history.claim_supersession_decisions[old] = payload.decision_id
    body.snapshot.claims = [c.model_copy(update={"state": "superseded"}) if c.id == old else c
                            for c in body.snapshot.claims]


def _issue(envelope: CommandEnvelope, actor: nsi.ActorRef, body: IntakeRevisionBody) -> None:
    if actor.kind not in ("owner", "rule_engine"):
        raise WorkStoreError("forbidden")
    payload = envelope.command.payload
    issue = payload.issue
    known = {i.id for i in [*body.conflicts, *body.gaps, *body.questions]}
    _require(issue.id not in known)
    _require(len(payload.gaps) == len(set(payload.gaps)))
    _require(set(payload.gaps) <= {g.id for g in body.gaps})
    if isinstance(issue, nsi.Conflict):
        _require(set(issue.claim_ids) <= {c.id for c in body.snapshot.claims})
        _require(issue.predicate_id in {p.id for p in body.snapshot.ontology.predicates})
        body.conflicts.append(issue)
    else:
        _require(set(issue.gap_ids) == set(payload.gaps))
        _require(set(issue.conflict_ids) <= {c.id for c in body.conflicts})
        body.questions.append(issue)
    body.history.issues.append(IntakeIssueRecord(issue=issue, gaps=payload.gaps))


def _criterion(envelope: CommandEnvelope, actor: nsi.ActorRef, body: IntakeRevisionBody) -> None:
    if actor.kind not in ("owner", "rule_engine"):
        raise WorkStoreError("forbidden")
    payload = envelope.command.payload
    _require(all(c.id != payload.criterion.id for c in body.criteria))
    requirements = {r.id: r for r in payload.requirements}
    _require(len(requirements) == len(payload.requirements))
    _require(set(payload.criterion.requirement_ids) == requirements.keys())
    known = {r.id: r for link in body.history.criteria for r in link.requirements}
    claim_ids = {c.id for c in body.snapshot.claims}
    dimensions = {d.id for d in body.snapshot.ontology.dimensions}
    for requirement in requirements.values():
        _require(requirement.id not in known or known[requirement.id] == requirement)
        _require(bool(requirement.claim_ids) and set(requirement.claim_ids) <= claim_ids)
        _require(set(requirement.dimension_ids) <= dimensions)
    body.criteria.append(payload.criterion)
    body.history.criteria.append(IntakeCriterionLink(
        criterion=payload.criterion, requirements=payload.requirements,
    ))


def settlement_records(body: IntakeRevisionBody, revision: int) -> list[DecisionRecord]:
    """Copy settled links into a snapshot while keeping original decisions in history."""
    return [DecisionRecord(
        decision=d.decision.model_copy(update={"revision": revision}), settled_claim_ids=list(d.claim_ids),
    ) for d in body.history.decisions]


def execute_decision_operation(cur: Cursor, envelope: CommandEnvelope, actor_id: UUID,
                               actor_kind: str) -> dict[str, object]:
    actor = intake_actor(actor_kind, actor_id)
    command = envelope.command
    _require(isinstance(command, (RecordIntakeDecisionCommand, SupersedeIntakeClaimCommand,
                                 RecordIntakeIssueCommand, LinkIntakeCriterionCommand)))
    payload = command.payload
    wire = payload.expected_revision
    cur.execute("SELECT mission_id,current_revision FROM omp_work.intake_heads "
                "WHERE workspace_id=%s AND run_id=%s", (envelope.workspace_id, payload.run_id))
    head = cur.fetchone()
    if head is None or head[1] != wire - 1:
        raise WorkStoreError("stale_intake")
    try:
        locked = lock_revision(cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
                               expected_revision=wire - 1, mission_id=head[0], operation_id=envelope.operation_id)
        body = read_revision(cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
                             revision=wire - 1).body
        body.snapshot.revision = wire
        body.binding.revision = wire
        body.assessments = []
        body.derivations = []
        # Snapshot copies retain decisions and all links; original wire revision stays in history.
        body.decisions = [DecisionRecord(
            decision=d.decision.model_copy(update={"revision": wire}), settled_claim_ids=d.settled_claim_ids,
        ) for d in body.decisions]
        present = {d.decision.id for d in body.decisions}
        body.decisions.extend(d for d in settlement_records(body, wire) if d.decision.id not in present)
        if isinstance(command, RecordIntakeDecisionCommand):
            _decision(cur, envelope, actor, head[0], body)
        elif isinstance(command, SupersedeIntakeClaimCommand):
            _supersede(envelope, actor, body)
        elif isinstance(command, RecordIntakeIssueCommand):
            _issue(envelope, actor, body)
        else:
            _criterion(envelope, actor, body)
        body.binding.claim_set_hash, body.binding.source_hash = revision_hashes(body.snapshot, body.artifact_bytes)
        append_revision(cur, locked, body, operation_id=envelope.operation_id)
    except IntakeRepositoryError as exc:
        raise WorkStoreError("stale_intake" if str(exc) == "stale_revision" else "invalid_request") from exc
    return dict(type=command.type, run_id=payload.run_id, expected_revision=wire, revision=wire + 1)


def open_issues(body: IntakeRevisionBody) -> tuple[IntakeIssueRecord, ...]:
    """Only an owner settlement closes a question and its linked findings."""
    superseded = {d.superseded_decision_id for d in body.history.decisions}
    resolved = {d.decision.question_id for d in body.history.decisions if d.decision.id not in superseded}
    for question in body.questions:
        if question.id in resolved:
            resolved.update(question.gap_ids + question.conflict_ids)
    records = {i.issue.id: i for i in body.history.issues}
    for issue in [*body.conflicts, *body.questions]:
        records.setdefault(issue.id, IntakeIssueRecord(
            issue=issue, gaps=tuple(issue.gap_ids) if isinstance(issue, nsi.ClarificationQuestion) else (),
        ))
    return tuple(i for i in records.values() if i.issue.id not in resolved)
