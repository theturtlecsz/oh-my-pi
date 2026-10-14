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


def _span_matches(receipt_span: SourceSpan, expected_span: SourceSpan | tuple[int, int] | dict[str, int]) -> bool:
    if isinstance(expected_span, SourceSpan):
        return receipt_span.start == expected_span.start and receipt_span.end == expected_span.end
    if isinstance(expected_span, tuple) and len(expected_span) == 2:
        return (receipt_span.start, receipt_span.end) == expected_span
    if isinstance(expected_span, dict):
        return receipt_span.start == expected_span.get("start") and receipt_span.end == expected_span.get("end")
    return False


def verify_receipt(
    receipt: SupportReceipt,
    claim_text: str,
    quote: str,
    model_id: str | SourceSpan | tuple[int, int] | dict[str, int],
    model_version: str | SourceSpan | tuple[int, int] | dict[str, int],
    prompt_version: str,
    rule_version: str,
    source_span: SourceSpan | tuple[int, int] | dict[str, int] | str,
    source_revision: str,
) -> bool:
    """Verify all receipt bindings from OMP-604 criterion 4 fail on any mismatch.

    Bindings verified: claim text hash, passage hash, source span, source revision,
    judge model id and version, prompt version, and rule version.
    """
    if isinstance(model_id, (SourceSpan, tuple, dict)):
        expected_span = model_id
        expected_revision = str(model_version)
        expected_model_id = str(prompt_version)
        expected_model_version = str(rule_version)
        expected_prompt_version = str(source_span)
        expected_rule_version = str(source_revision)
    else:
        expected_model_id = str(model_id)
        expected_model_version = str(model_version)
        expected_prompt_version = str(prompt_version)
        expected_rule_version = str(rule_version)
        expected_span = source_span  # type: ignore[assignment]
        expected_revision = str(source_revision)

    return (
        receipt.claim_text_sha256 == text_sha256(claim_text)
        and receipt.passage_sha256 == text_sha256(quote)
        and receipt.model_id == expected_model_id
        and receipt.model_version == expected_model_version
        and receipt.prompt_version == expected_prompt_version
        and receipt.rule_version == expected_rule_version
        and receipt.source_revision == expected_revision
        and _span_matches(receipt.source_span, expected_span)
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


def claim_supported(
    receipts: Sequence[SupportReceipt | tuple[Label | None, SupportReceipt | None]],
) -> bool:
    """A claim is supported only when status_from_receipts derives supported."""
    pairs: list[tuple[Label | None, SupportReceipt | None]] = [
        item if isinstance(item, tuple) else (None, item)
        for item in receipts
    ]
    return status_from_receipts(pairs) == "supported"
