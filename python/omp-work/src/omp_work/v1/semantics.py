from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from .models import (
    Anomaly,
    Candidate,
    CloseAttempt,
    CloseAttemptState,
    CompletionBlocker,
    CompletionInput,
    ContractExamples,
    EvidenceKind,
    EvidenceReceipt,
    RelationEdge,
    RelationKind,
)


def revision_decision(
    current_hash: str, proposed_hash: str
) -> Literal["noop", "append"]:
    return "noop" if current_hash == proposed_hash else "append"


def would_create_cycle(
    edges: tuple[RelationEdge, ...], candidate: RelationEdge
) -> bool:
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


def replay_decision(
    stored_request_sha256: str | None, request_sha256: str
) -> Literal["execute", "replay", "conflict"]:
    if stored_request_sha256 is None:
        return "execute"
    return "replay" if stored_request_sha256 == request_sha256 else "conflict"


def evidence_is_fresh(
    receipt: EvidenceReceipt, candidate: Candidate, revision_id: UUID
) -> bool:
    if (
        receipt.work_id != candidate.work_id
        or receipt.revision_id != revision_id
        or receipt.candidate_id != candidate.candidate_id
    ):
        return False
    if receipt.kind in {EvidenceKind.VERIFICATION, EvidenceKind.AUDIT}:
        return (
            receipt.candidate_sha256 == candidate.candidate_sha256
            and receipt.candidate_commit == candidate.commit_sha
        )
    return True


def completion_blockers(
    input: CompletionInput,
    *,
    attempt: CloseAttempt | None = None,
    pending_delivery_count: int = 0,
) -> tuple[CompletionBlocker, ...]:
    if not input.closeout_requested:
        return (
            CompletionBlocker(
                code="closeout_missing", detail="closeout intent is required"
            ),
        )
    attempt_blockers: list[CompletionBlocker] = []
    if attempt is None:
        attempt_blockers.append(
            CompletionBlocker(
                code="attempt_missing",
                detail="completion requires a live close attempt on the current candidate",
            )
        )
    else:
        if attempt.state is not CloseAttemptState.CLOSEOUT_REQUESTED:
            attempt_blockers.append(
                CompletionBlocker(
                    code="attempt_not_requested",
                    detail=f"the current close attempt is {attempt.state.value}, not closeout_requested",
                )
            )
        if (
            attempt.candidate_id != input.candidate.candidate_id
            or attempt.candidate_sha256 != input.candidate.candidate_sha256
            or attempt.candidate_commit != input.candidate.commit_sha
        ):
            attempt_blockers.append(
                CompletionBlocker(
                    code="stale_evidence",
                    detail="the close attempt binds a different candidate than the current one",
                )
            )
    if pending_delivery_count > 0:
        attempt_blockers.append(
            CompletionBlocker(
                code="delivery_pending",
                detail=f"{pending_delivery_count} close-attempt event(s) still owe an owner delivery (delivered or waived)",
            )
        )
    fresh = tuple(
        receipt
        for receipt in input.receipts
        if evidence_is_fresh(receipt, input.candidate, input.current_revision_id)
    )
    stale = (
        input.candidate.work_id != input.work_id
        or input.candidate.revision_id != input.current_revision_id
        or len(fresh) != len(input.receipts)
    )
    kinds = {receipt.kind for receipt in fresh}
    blockers: list[CompletionBlocker] = attempt_blockers
    if input.candidate.kind != "final" or input.candidate.commit_sha is None:
        blockers.append(
            CompletionBlocker(
                code="candidate_not_final",
                detail="completion requires a finalized candidate bound to an exact commit",
            )
        )
    if EvidenceKind.CLOSEOUT not in kinds:
        blockers.append(
            CompletionBlocker(
                code="closeout_missing",
                detail="current candidate lacks closeout review evidence",
            )
        )
    if EvidenceKind.PLAN not in kinds:
        blockers.append(
            CompletionBlocker(
                code="plan_missing", detail="current candidate lacks plan evidence"
            )
        )
    if EvidenceKind.VERIFICATION not in kinds:
        blockers.append(
            CompletionBlocker(
                code="verification_missing",
                detail="current candidate lacks verification evidence",
            )
        )
    audits = sorted(
        (receipt for receipt in fresh if receipt.kind is EvidenceKind.AUDIT),
        key=lambda receipt: (receipt.issued_at, str(receipt.receipt_id)),
    )
    if not audits or not audits[-1].independent or audits[-1].verdict != "PASS":
        blockers.append(
            CompletionBlocker(
                code="audit_missing",
                detail="current candidate lacks a current independent PASS audit",
            )
        )

    def _push_satisfied(receipt: EvidenceReceipt) -> bool:
        if receipt.kind is not EvidenceKind.PUSH:
            return False
        if receipt.remote_commit == input.candidate.commit_sha:
            return True
        # OMP-99 containment shape: the receipt names the candidate and a
        # DIFFERENT non-null remote tip that contains it (host-attested).
        return (
            receipt.candidate_commit == input.candidate.commit_sha
            and receipt.remote_commit is not None
            and receipt.remote_commit != input.candidate.commit_sha
        )

    if not any(_push_satisfied(receipt) for receipt in fresh):
        blockers.append(
            CompletionBlocker(
                code="push_unverified",
                detail="remote ref does not resolve to candidate commit",
            )
        )
    if stale:
        blockers.append(
            CompletionBlocker(
                code="stale_evidence",
                detail="one or more receipts or the candidate bind a different work, revision, or candidate",
            )
        )
    return tuple(blockers)


REPORT_SECTIONS: tuple[str, ...] = (
    "FINDINGS",
    "ACCEPTANCE COVERAGE",
    "OUT OF SCOPE",
    "CHECKS RUN",
    "REMAINING QUESTIONS",
)
_VERDICT_LINE = re.compile(r"\AVERDICT\s*:\s*(PASS|NEEDS_FIX|BLOCKED)\b")
_OUTPUT_WRAPPER = re.compile(r"\A<output>\n?([\s\S]*?)\n?</output>\Z")


def _unwrap_object(decoded: object) -> tuple[str, str | None] | tuple[None, str]:
    """Extract (report_text, wrapper_verdict) from one direct transport wrapper object."""
    if not isinstance(decoded, dict):
        return None, "report_wrapper_invalid"
    keys = set(decoded)
    body_keys = keys & {"report", "text", "raw"}
    if len(body_keys) != 1 or not keys <= {"report", "text", "raw", "verdict"}:
        return None, "report_wrapper_invalid"
    body_key = body_keys.pop()
    if not isinstance(decoded[body_key], str):
        return None, "report_wrapper_invalid"
    verdict = None
    if "verdict" in decoded:
        verdict = decoded["verdict"]
        if not isinstance(verdict, str):
            return None, "report_wrapper_invalid"
    return decoded[body_key], verdict


def normalize_auditor_report(
    payload: object,
) -> tuple[str, Literal["PASS", "NEEDS_FIX", "BLOCKED"]] | tuple[None, str]:
    """Normalize one transport wrapper, then validate the canonical report."""
    wrapper_verdict: str | None = None
    if isinstance(payload, dict):
        unwrapped, extra = _unwrap_object(payload)
        if unwrapped is None:
            return None, extra  # type: ignore[return-value]
        text, wrapper_verdict = unwrapped, extra
    elif isinstance(payload, str):
        text = payload.strip()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            if text.startswith("{"):
                return None, "report_wrapper_serialized"
            wrapped = _OUTPUT_WRAPPER.match(text)
            if wrapped:
                text = wrapped.group(1)
        else:
            # Strict parse success routes through the sole wrapper parser:
            # a decoded non-object is a malformed wrapper, typed invalid.
            unwrapped, extra = _unwrap_object(decoded)
            if unwrapped is None:
                return None, extra  # type: ignore[return-value]
            text, wrapper_verdict = unwrapped, extra
    else:
        return None, "report_not_text"
    text = text.strip()
    if text.startswith(("{", '"', "[")):
        return None, "report_wrapper_nested"
    if text.startswith(("<output>", "<task-result")):
        return None, "report_wrapper_nested"
    verdict_line = _VERDICT_LINE.match(text)
    if not verdict_line:
        return None, "verdict_missing"
    if wrapper_verdict is not None and wrapper_verdict != verdict_line.group(1):
        return None, "report_wrapper_verdict_mismatch"
    canonical = text.replace("\r\n", "\n")
    missing = [
        section
        for section in REPORT_SECTIONS
        if not re.search(
            rf"^\s*(?:#+\s*)?{re.escape(section)}(?:[ \t]*\([^)\n]*\))?[ \t]*[:—-]?[ \t]*$",
            canonical,
            re.MULTILINE,
        )
    ]
    if missing:
        return None, "report_sections_missing"
    verdict = _VERDICT_LINE.match(canonical)
    assert verdict is not None
    return canonical, verdict.group(1)  # type: ignore[return-value]


def validate_cutover_manifest(
    manifest_anomalies: tuple[Anomaly, ...], parity_differences: tuple[str, ...]
) -> None:
    allowed = {"attachment_content_unavailable", "unsupported_non_workflow_object"}
    if parity_differences or any(
        anomaly.disposition != "quarantined" or anomaly.code not in allowed
        for anomaly in manifest_anomalies
    ):
        raise ValueError("cutover invariant failed")


def validate_examples(examples: ContractExamples) -> None:
    immutable = examples.immutable_revision
    if (
        revision_decision(immutable.current_hash, immutable.proposed_same_hash)
        != immutable.same_content
        or revision_decision(immutable.current_hash, immutable.proposed_changed_hash)
        != immutable.changed_content
    ):
        raise ValueError("immutable revision example failed")
    relation = examples.relation_cycle
    if (
        relation.edges != ("A blocks B", "B blocks C")
        or relation.rejected != "C blocks A"
        or not relation.related_triangle
    ):
        raise ValueError("relation cycle example failed")
    idempotency = examples.idempotency
    if (
        replay_decision(idempotency.stored_request_sha256, idempotency.request_sha256)
        != "replay"
        or replay_decision(
            idempotency.stored_request_sha256, idempotency.changed_request_sha256
        )
        != "conflict"
    ):
        raise ValueError("idempotency example failed")
    stale = examples.stale_evidence
    if stale.bound_revision == stale.current_revision:
        raise ValueError("stale evidence example failed")
    pushed = examples.pushed_branch
    if (
        pushed.candidate_commit != pushed.matching_remote
        or pushed.candidate_commit == pushed.mismatched_remote
    ):
        raise ValueError("pushed branch example failed")
