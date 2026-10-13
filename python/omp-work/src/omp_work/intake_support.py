"""Receipt recording one intake support judgement and the model usage behind it."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from omp_work.v1.models import StrictModel, hex64

Verdict = Literal["supports", "contradicts", "neither"]
Measurement = Literal["measured", "unknown"]

TokenCount = StrictInt | None


class SupportReceipt(StrictModel):
    verdict: Verdict
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
