from __future__ import annotations

from typing import Any, Iterator, Literal, Protocol, runtime_checkable
from uuid import UUID

from omp_work.v1.models import EvidenceReceipt

from .models import Claim, Lesson, StrictModel

REASON_NO_CITATION = "no_citation"
REASON_INVALID_CITATION = "invalid_citation"
REASON_NARRATION_ONLY = "narration_only"
REASON_TOOL_SUCCESS_ONLY = "tool_success_only"

PolicyRejectionReason = Literal[
    "no_citation",
    "invalid_citation",
    "narration_only",
    "tool_success_only",
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


def evaluate(lesson: Lesson, reader: NativeReceipts) -> PolicyDecision:
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

    return PolicyDecision(
        accepted=True,
        reason=None,
        per_claim_results=evals,
    )


__all__ = [
    "REASON_INVALID_CITATION",
    "REASON_NARRATION_ONLY",
    "REASON_NO_CITATION",
    "REASON_TOOL_SUCCESS_ONLY",
    "ClaimEvaluation",
    "NativeReceipts",
    "PolicyDecision",
    "PolicyRejectionReason",
    "evaluate",
    "is_narration_kind",
    "is_supporting_receipt",
]
