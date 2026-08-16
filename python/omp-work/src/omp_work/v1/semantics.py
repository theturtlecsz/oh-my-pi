from __future__ import annotations

from typing import Literal
from uuid import UUID

from .models import Anomaly, Candidate, CompletionBlocker, CompletionInput, ContractExamples, EvidenceKind, EvidenceReceipt, RelationEdge, RelationKind


def revision_decision(current_hash: str, proposed_hash: str) -> Literal["noop", "append"]:
    return "noop" if current_hash == proposed_hash else "append"


def would_create_cycle(edges: tuple[RelationEdge, ...], candidate: RelationEdge) -> bool:
    if not candidate.active or candidate.kind is RelationKind.RELATED:
        return False
    if candidate.source_work_id == candidate.target_work_id:
        return True
    graph: dict[UUID, set[UUID]] = {}
    for edge in (*edges, candidate):
        if edge.active and edge.kind is candidate.kind:
            graph.setdefault(edge.source_work_id, set()).add(edge.target_work_id)
    pending = [candidate.target_work_id]
    seen: set[UUID] = set()
    while pending:
        node = pending.pop()
        if node == candidate.source_work_id:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, ()))
    return False


def replay_decision(stored_request_sha256: str | None, request_sha256: str) -> Literal["execute", "replay", "conflict"]:
    if stored_request_sha256 is None:
        return "execute"
    return "replay" if stored_request_sha256 == request_sha256 else "conflict"


def evidence_is_fresh(receipt: EvidenceReceipt, candidate: Candidate, revision_id: UUID) -> bool:
    if receipt.work_id != candidate.work_id or receipt.revision_id != revision_id or receipt.candidate_id != candidate.candidate_id:
        return False
    if receipt.kind in {EvidenceKind.VERIFICATION, EvidenceKind.AUDIT}:
        return receipt.candidate_sha256 == candidate.candidate_sha256 and receipt.candidate_commit == candidate.commit_sha
    return True


def completion_blockers(input: CompletionInput) -> tuple[CompletionBlocker, ...]:
    if not input.closeout_requested:
        return (CompletionBlocker(code="closeout_missing", detail="closeout intent is required"),)
    fresh = tuple(receipt for receipt in input.receipts if evidence_is_fresh(receipt, input.candidate, input.current_revision_id))
    stale = input.candidate.work_id != input.work_id or input.candidate.revision_id != input.current_revision_id or len(fresh) != len(input.receipts)
    kinds = {receipt.kind for receipt in fresh}
    blockers: list[CompletionBlocker] = []
    if EvidenceKind.PLAN not in kinds:
        blockers.append(CompletionBlocker(code="plan_missing", detail="current candidate lacks plan evidence"))
    if EvidenceKind.VERIFICATION not in kinds:
        blockers.append(CompletionBlocker(code="verification_missing", detail="current candidate lacks verification evidence"))
    audits = sorted(
        (receipt for receipt in fresh if receipt.kind is EvidenceKind.AUDIT),
        key=lambda receipt: (receipt.issued_at, str(receipt.receipt_id)),
    )
    if not audits or not audits[-1].independent or audits[-1].verdict != "PASS":
        blockers.append(CompletionBlocker(code="audit_missing", detail="current candidate lacks a current independent PASS audit"))
    if not any(receipt.kind is EvidenceKind.PUSH and receipt.remote_commit == input.candidate.commit_sha for receipt in fresh):
        blockers.append(CompletionBlocker(code="push_unverified", detail="remote ref does not resolve to candidate commit"))
    if stale:
        blockers.append(CompletionBlocker(code="stale_evidence", detail="one or more receipts or the candidate bind a different work, revision, or candidate"))
    return tuple(blockers)


def validate_cutover_manifest(manifest_anomalies: tuple[Anomaly, ...], parity_differences: tuple[str, ...]) -> None:
    allowed = {"attachment_content_unavailable", "unsupported_non_workflow_object"}
    if parity_differences or any(anomaly.disposition != "quarantined" or anomaly.code not in allowed for anomaly in manifest_anomalies):
        raise ValueError("cutover invariant failed")


def validate_examples(examples: ContractExamples) -> None:
    immutable = examples.immutable_revision
    if revision_decision(immutable.current_hash, immutable.proposed_same_hash) != immutable.same_content or revision_decision(immutable.current_hash, immutable.proposed_changed_hash) != immutable.changed_content:
        raise ValueError("immutable revision example failed")
    relation = examples.relation_cycle
    if relation.edges != ("A blocks B", "B blocks C") or relation.rejected != "C blocks A" or not relation.related_triangle:
        raise ValueError("relation cycle example failed")
    idempotency = examples.idempotency
    if replay_decision(idempotency.stored_request_sha256, idempotency.request_sha256) != "replay" or replay_decision(idempotency.stored_request_sha256, idempotency.changed_request_sha256) != "conflict":
        raise ValueError("idempotency example failed")
    stale = examples.stale_evidence
    if stale.bound_revision == stale.current_revision:
        raise ValueError("stale evidence example failed")
    pushed = examples.pushed_branch
    if pushed.candidate_commit != pushed.matching_remote or pushed.candidate_commit == pushed.mismatched_remote:
        raise ValueError("pushed branch example failed")
