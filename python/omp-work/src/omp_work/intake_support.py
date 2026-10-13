"""Receipt recording one intake support judgement and the model usage behind it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypedDict

from pydantic import Field, JsonValue, StrictInt, model_validator

from omp_work.contracts.nsi.v1.models import ClaimModality, Polarity
from omp_work.report_evidence import ClaimStatus, Label, status_from_labels
from omp_work.v1.canonical import canonical_json, text_sha256
from omp_work.v1.models import StrictModel, hex64

Verdict = Literal["supports", "contradicts", "neither", "abstain", "timeout", "evidence-unavailable"]
# Null, timed-out or failed judge calls: stored as their own state, never counted toward supports.
PENDING_VERDICTS: frozenset[str] = frozenset({"abstain", "timeout", "evidence-unavailable"})
Measurement = Literal["measured", "unknown"]

TokenCount = StrictInt | None


class ClaimProjection(TypedDict):
    subject_id: str
    predicate_id: str
    predicate_title: str
    value: dict[str, JsonValue]
    modality: ClaimModality
    polarity: Polarity


def render_claim_text(claim: ClaimProjection) -> str:
    """Render only claim semantics, with the caller's predicate title and full value JSON."""
    return canonical_json({
        "subject_id": claim["subject_id"],
        "predicate_id": claim["predicate_id"],
        "predicate_title": claim["predicate_title"],
        "value": claim["value"],
        "modality": claim["modality"],
        "polarity": claim["polarity"],
    })


class SourceSpan(StrictModel):
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> SourceSpan:
        if self.end < self.start:
            raise ValueError("source span end precedes start")
        return self


class SupportReceipt(StrictModel):
    verdict: Verdict
    source_span: SourceSpan
    source_revision: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    passage_sha256: hex64
    claim_text_sha256: hex64
    context_sha256: hex64
    prompt_sha256: hex64
    model_id: str = Field(min_length=1)
    measurement: Measurement
    input_tokens: TokenCount
    output_tokens: TokenCount
    usage_id: str | None

    @model_validator(mode="after")
    def _check_usage(self) -> SupportReceipt:
        counts = (self.input_tokens, self.output_tokens)
        if self.measurement == "measured":
            if any(c is None or c < 0 for c in counts):
                raise ValueError("measured usage requires nonnegative input_tokens and output_tokens")
        elif any(c is not None for c in counts):
            raise ValueError("unknown usage requires null input_tokens and output_tokens")
        return self


def verify_receipt(receipt: SupportReceipt, claim_text: str, quote: str, model_id: str) -> bool:
    """Verify exact supplied text/model bindings; other bindings remain shape-validated."""
    return (
        receipt.claim_text_sha256 == text_sha256(claim_text)
        and receipt.passage_sha256 == text_sha256(quote)
        and receipt.model_id == model_id
    )


def status_from_receipts(
    receipts: Sequence[tuple[Label | None, SupportReceipt | None]],
) -> ClaimStatus:
    """Derive status without allowing disagreement or pending judgements to confer support."""
    labels: list[Label] = []
    blocked = False
    for relation, receipt in receipts:
        if receipt is None:
            blocked = True
            continue
        verdict = receipt.verdict
        if verdict in ("supports", "contradicts", "neither"):
            labels.append(verdict)
        else:
            blocked = True
        if relation is not None and relation != verdict:
            blocked = True
    status = status_from_labels(labels)
    return "unsupported" if status == "supported" and blocked else status


def claim_supported(receipts: Sequence[SupportReceipt]) -> bool:
    """A claim is supported only with at least one supports verdict and no pending verdict."""
    if any(r.verdict in PENDING_VERDICTS for r in receipts):
        return False
    return any(r.verdict == "supports" for r in receipts)
