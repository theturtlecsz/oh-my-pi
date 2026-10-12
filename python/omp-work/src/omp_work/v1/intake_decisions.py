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


def _latest_findings(body: IntakeRevisionBody) -> tuple[
    dict[str, nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion], dict[str, tuple[str, ...]],
]:
    """Body findings override history; the last conflict record owns its gap links."""
    findings: dict[str, nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion] = {}
    gap_links: dict[str, tuple[str, ...]] = {}
    for record in body.history.issues:
        findings[record.issue.id] = record.issue
        if isinstance(record.issue, nsi.Conflict):
            gap_links[record.issue.id] = record.gaps
    for finding in [*body.gaps, *body.conflicts, *body.questions]:
        findings[finding.id] = finding
    return findings, gap_links


def _settlement_membership(
    body: IntakeRevisionBody, question: nsi.ClarificationQuestion,
    findings: dict[str, nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion],
    gap_links: dict[str, tuple[str, ...]],
) -> tuple[set[str], set[str]]:
    """Resolve current settlement targets and validate their snapshot claim references."""
    current = findings.get(question.id)
    _require(isinstance(current, nsi.ClarificationQuestion))
    linked = {current.id, *current.conflict_ids}
    gap_ids = set(current.gap_ids)
    required: set[str] = set()
    for conflict_id in current.conflict_ids:
        conflict = findings.get(conflict_id)
        _require(isinstance(conflict, nsi.Conflict))
        required.update(conflict.claim_ids)
        gap_ids.update(gap_links.get(conflict_id, ()))
    for gap_id in gap_ids:
        gap = findings.get(gap_id)
        _require(isinstance(gap, nsi.Gap))
        required.add(gap.premise_id)
    _require(required <= {claim.id for claim in body.snapshot.claims})
    linked.update(gap_ids)
    return linked, required


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
    findings, gap_links = _latest_findings(body)
    question = findings.get(decision.question_id)
    _require(isinstance(question, nsi.ClarificationQuestion))
    linked, required = _settlement_membership(body, question, findings, gap_links)
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
    artifact_id = f"instruction:{decision.id}"
    _require(all(a.id != artifact_id for a in body.snapshot.artifacts))
    # Revision rows need these foreign-key targets even when only history retained them.
    present = {i.id for i in [*body.questions, *body.conflicts]}
    for finding in findings.values():
        if finding.id not in linked or finding.id in present:
            continue
        if isinstance(finding, nsi.ClarificationQuestion):
            body.questions.append(finding.model_copy(deep=True))
        elif isinstance(finding, nsi.Conflict):
            body.conflicts.append(finding.model_copy(deep=True))
    history.decisions.append(record)
    raw = payload.instruction.text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    history.instruction_hashes[decision.id] = digest
    if relay is not None:
        history.relays[decision.id] = relay
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


def unresolved_findings(body: IntakeRevisionBody) -> tuple[
    nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion, ...,
]:
    """Only active owner coverage of current membership settles linked findings."""
    findings, gap_links = _latest_findings(body)
    superseded = {d.superseded_decision_id for d in body.history.decisions}
    decisions = {d.decision.id: (d.decision, d.settled_claim_ids) for d in body.decisions}
    # Original history owns coverage; revision copies may have stale or broader links.
    decisions.update({d.decision.id: (d.decision, d.claim_ids) for d in body.history.decisions})
    resolved: set[str] = set()
    claim_ids = {c.id for c in body.snapshot.claims}
    for decision_id, (decision, settled) in decisions.items():
        if decision_id in superseded:
            continue
        question = findings.get(decision.question_id)
        _require(isinstance(question, nsi.ClarificationQuestion))
        _require(set(settled) <= claim_ids)
        linked, required = _settlement_membership(body, question, findings, gap_links)
        if decision.actor.kind == "owner" and required <= set(settled):
            resolved.update(linked)
    return tuple(finding for finding in findings.values() if finding.id not in resolved)


def open_issues(body: IntakeRevisionBody) -> tuple[IntakeIssueRecord, ...]:
    """Project remaining conflicts/questions with the latest conflict gap links."""
    _, gap_links = _latest_findings(body)
    return tuple(IntakeIssueRecord(
        issue=issue,
        gaps=tuple(issue.gap_ids) if isinstance(issue, nsi.ClarificationQuestion)
        else gap_links.get(issue.id, ()),
    ) for issue in unresolved_findings(body)
        if isinstance(issue, (nsi.Conflict, nsi.ClarificationQuestion)))


def compute_settlement_assessment(
    body: IntakeRevisionBody, assessment: nsi.IntakeReadinessAssessment,
) -> nsi.IntakeReadinessAssessment:
    """Copy an assessment, replacing its gaps and conflicts from the shared resolver.

    An open conflict yields CONFLICTED. A remaining gap or question yields NEEDS_SPEC.
    With no blockers the supplied status is kept. Settlement does not certify READY.
    Binding, issuer, timestamp, and receipt ids stay on the copy.
    """
    unresolved = unresolved_findings(body)
    gaps = [item.model_copy(deep=True) for item in unresolved if isinstance(item, nsi.Gap)]
    conflicts = [item.model_copy(deep=True) for item in unresolved if isinstance(item, nsi.Conflict)]
    if conflicts:
        status = "CONFLICTED"
    elif gaps or any(isinstance(item, nsi.ClarificationQuestion) for item in unresolved):
        status = "NEEDS_SPEC"
    else:
        status = assessment.status
    return assessment.model_copy(deep=True, update={"gaps": gaps, "conflicts": conflicts, "status": status})
