from __future__ import annotations

from typing import Any, Iterator, Literal, Protocol, runtime_checkable
from uuid import UUID

from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import EvidenceReceipt

from .models import Claim, Lesson, SourceIdentity, StrictModel

REASON_NO_CITATION = "no_citation"
REASON_INVALID_CITATION = "invalid_citation"
REASON_NARRATION_ONLY = "narration_only"
REASON_TOOL_SUCCESS_ONLY = "tool_success_only"
REASON_UNBOUND_SOURCE = "unbound_source"
REASON_UNBOUND_RECEIPT = "unbound_receipt"
REASON_UNBOUND_CLAIM = "unbound_claim"

PolicyRejectionReason = Literal[
    "no_citation",
    "invalid_citation",
    "narration_only",
    "tool_success_only",
    "unbound_source",
    "unbound_receipt",
    "unbound_claim",
]


@runtime_checkable
class NativeReceipts(Protocol):
    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        ...


class ClaimEvaluation(StrictModel):
    claim: Claim
    supported: bool
    reason: str | None = None
    receipt_ids: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Any]:
        return iter((self.claim, self.supported, self.reason))

    def __getitem__(self, item: int) -> Any:
        return (self.claim, self.supported, self.reason)[item]


class PolicyDecision(StrictModel):
    accepted: bool
    reason: str | None = None
    per_claim_results: tuple[ClaimEvaluation, ...] = ()

    def __init__(
        self,
        accepted: bool = False,
        reason: str | None = None,
        per_claim_results: tuple[ClaimEvaluation, ...] | list[ClaimEvaluation] = (),
        **data: Any,
    ) -> None:
        if data:
            super().__init__(**data)
        else:
            super().__init__(
                accepted=accepted,
                reason=reason,
                per_claim_results=tuple(per_claim_results),
            )

    @property
    def per_claim(self) -> tuple[ClaimEvaluation, ...]:
        return self.per_claim_results

    def __iter__(self) -> Iterator[Any]:
        return iter((self.accepted, self.reason, self.per_claim_results))

    def __getitem__(self, item: int) -> Any:
        return (self.accepted, self.reason, self.per_claim_results)[item]


def _kind_str(kind: Any) -> str:
    if hasattr(kind, "value"):
        return str(kind.value).lower()
    return str(kind).lower()


_NARRATION_KINDS = {
    "plan",
    "handoff",
    "closeout",
    "intake_publication",
    "intake_admission",
}


def is_narration_kind(kind: Any) -> bool:
    k = _kind_str(kind)
    return k in _NARRATION_KINDS or k.startswith("intake")


def is_supporting_receipt(receipt: EvidenceReceipt) -> bool:
    k = _kind_str(receipt.kind)
    if k not in {"verification", "audit"}:
        return False
    return getattr(receipt, "verdict", None) == "PASS"


def _present_id(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value


def _source_ids(source: SourceIdentity | None) -> tuple[str, str, str] | None:
    """work, candidate, and revision, or None when source is missing or any id is blank."""
    if source is None:
        return None
    work_id = _present_id(source.work_id)
    candidate_id = _present_id(source.candidate_id)
    revision_id = _present_id(source.revision_id)
    if work_id is None or candidate_id is None or revision_id is None:
        return None
    return work_id, candidate_id, revision_id


def _same_id(left: object, right: object) -> bool:
    """String equality. None differs from every value, including None."""
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _receipt_ids_match(
    receipt: EvidenceReceipt, work_id: str, candidate_id: str, revision_id: str
) -> bool:
    return (
        _same_id(work_id, receipt.work_id)
        and _same_id(candidate_id, receipt.candidate_id)
        and _same_id(revision_id, receipt.revision_id)
    )


def _claim_bound(claim: Claim, receipt: EvidenceReceipt) -> bool:
    """A verification or audit PASS binds only when the span, issuer, and outcome match."""
    span = claim.source_span
    if span is None or span.start >= span.end:
        return False
    text = canonical_json(receipt.payload)
    if span.end > len(text):
        return False
    if text[span.start : span.end] != claim.text:
        return False
    if claim.evaluator is None or claim.evaluator != receipt.issuer:
        return False
    return claim.outcome == receipt.verdict == "PASS"


def _claim_result(claim: Claim, *, supported: bool, reason: str | None) -> ClaimEvaluation:
    return ClaimEvaluation(
        claim=claim,
        supported=supported,
        reason=reason,
        receipt_ids=tuple(str(rid) for rid in claim.receipt_ids),
    )


def _bind(
    lesson: Lesson,
    resolved_receipts: dict[str, EvidenceReceipt],
    source: SourceIdentity | None,
) -> PolicyDecision:
    ids = _source_ids(source)
    if ids is None:
        return PolicyDecision(
            accepted=False,
            reason=REASON_UNBOUND_SOURCE,
            per_claim_results=[
                _claim_result(claim, supported=False, reason=REASON_UNBOUND_SOURCE)
                for claim in lesson.claims
            ],
        )

    work_id, candidate_id, revision_id = ids
    evals: list[ClaimEvaluation] = []
    saw_unbound_receipt = False
    saw_unbound_claim = False
    for claim in lesson.claims:
        receipts = [resolved_receipts[str(rid)] for rid in claim.receipt_ids]
        if any(
            not _receipt_ids_match(receipt, work_id, candidate_id, revision_id) for receipt in receipts
        ):
            saw_unbound_receipt = True
            evals.append(_claim_result(claim, supported=False, reason=REASON_UNBOUND_RECEIPT))
            continue
        passes = [receipt for receipt in receipts if is_supporting_receipt(receipt)]
        if not passes or any(not _claim_bound(claim, receipt) for receipt in passes):
            saw_unbound_claim = True
            evals.append(_claim_result(claim, supported=False, reason=REASON_UNBOUND_CLAIM))
            continue
        evals.append(_claim_result(claim, supported=True, reason=None))

    if saw_unbound_receipt:
        return PolicyDecision(
            accepted=False,
            reason=REASON_UNBOUND_RECEIPT,
            per_claim_results=evals,
        )
    if saw_unbound_claim:
        return PolicyDecision(
            accepted=False,
            reason=REASON_UNBOUND_CLAIM,
            per_claim_results=evals,
        )
    return PolicyDecision(accepted=True, reason=None, per_claim_results=evals)


def evaluate(
    lesson: Lesson, reader: NativeReceipts, source: SourceIdentity | None = None
) -> PolicyDecision:
    # 1. no claims or a claim citing nothing -> no_citation
    if not lesson.claims:
        return PolicyDecision(
            accepted=False,
            reason=REASON_NO_CITATION,
            per_claim_results=(),
        )

    has_empty_citation = any(not claim.receipt_ids for claim in lesson.claims)
    if has_empty_citation:
        evals = [
            ClaimEvaluation(
                claim=c,
                supported=False,
                reason=REASON_NO_CITATION if not c.receipt_ids else None,
                receipt_ids=tuple(str(rid) for rid in c.receipt_ids),
            )
            for c in lesson.claims
        ]
        return PolicyDecision(
            accepted=False,
            reason=REASON_NO_CITATION,
            per_claim_results=evals,
        )

    # 2. any cited id unresolvable -> invalid_citation (one valid receipt never launders the rest)
    resolved_receipts: dict[str, EvidenceReceipt] = {}
    unresolvable_ids: set[str] = set()

    for claim in lesson.claims:
        for rid in claim.receipt_ids:
            rid_str = str(rid)
            if rid_str in resolved_receipts or rid_str in unresolvable_ids:
                continue
            try:
                r = reader.receipt(rid)
                if r is None or not isinstance(r, EvidenceReceipt):
                    unresolvable_ids.add(rid_str)
                else:
                    resolved_receipts[rid_str] = r
            except Exception:
                unresolvable_ids.add(rid_str)

    if unresolvable_ids:
        evals = [
            ClaimEvaluation(
                claim=c,
                supported=False,
                reason=REASON_INVALID_CITATION
                if any(str(rid) in unresolvable_ids for rid in c.receipt_ids)
                else None,
                receipt_ids=tuple(str(rid) for rid in c.receipt_ids),
            )
            for c in lesson.claims
        ]
        return PolicyDecision(
            accepted=False,
            reason=REASON_INVALID_CITATION,
            per_claim_results=evals,
        )

    # 3. a claim whose receipts are only plan/handoff/closeout/intake kinds -> narration_only
    # 4. a claim whose receipts include no PASS -> tool_success_only (e.g. verification, verdict None, exit_code 0 payload)
    # A claim is supported only by a verification/audit receipt with verdict PASS.

    narration_claims = False
    no_pass_claims = False
    evals: list[ClaimEvaluation] = []

    for claim in lesson.claims:
        claim_receipts = [resolved_receipts[str(rid)] for rid in claim.receipt_ids]
        claim_is_narration = all(is_narration_kind(r.kind) for r in claim_receipts)
        claim_has_pass = any(is_supporting_receipt(r) for r in claim_receipts)

        if claim_is_narration:
            narration_claims = True
            evals.append(
                ClaimEvaluation(
                    claim=claim,
                    supported=False,
                    reason=REASON_NARRATION_ONLY,
                    receipt_ids=tuple(str(rid) for rid in claim.receipt_ids),
                )
            )
        elif not claim_has_pass:
            no_pass_claims = True
            evals.append(
                ClaimEvaluation(
                    claim=claim,
                    supported=False,
                    reason=REASON_TOOL_SUCCESS_ONLY,
                    receipt_ids=tuple(str(rid) for rid in claim.receipt_ids),
                )
            )
        else:
            evals.append(
                ClaimEvaluation(
                    claim=claim,
                    supported=True,
                    reason=None,
                    receipt_ids=tuple(str(rid) for rid in claim.receipt_ids),
                )
            )

    if narration_claims:
        return PolicyDecision(
            accepted=False,
            reason=REASON_NARRATION_ONLY,
            per_claim_results=evals,
        )

    if no_pass_claims:
        return PolicyDecision(
            accepted=False,
            reason=REASON_TOOL_SUCCESS_ONLY,
            per_claim_results=evals,
        )

    # 5. missing source or a blank work_id, candidate_id, or revision_id -> unbound_source
    # 6. a cited receipt that differs on any of those ids -> unbound_receipt
    # 7. a verification/audit PASS whose span, issuer, or PASS outcome does not match -> unbound_claim
    # A non-PASS receipt never reaches this stage: it stays tool_success_only.
    return _bind(lesson, resolved_receipts, source)


__all__ = [
    "REASON_INVALID_CITATION",
    "REASON_NARRATION_ONLY",
    "REASON_NO_CITATION",
    "REASON_TOOL_SUCCESS_ONLY",
    "REASON_UNBOUND_CLAIM",
    "REASON_UNBOUND_RECEIPT",
    "REASON_UNBOUND_SOURCE",
    "ClaimEvaluation",
    "NativeReceipts",
    "PolicyDecision",
    "PolicyRejectionReason",
    "evaluate",
    "is_narration_kind",
    "is_supporting_receipt",
]
