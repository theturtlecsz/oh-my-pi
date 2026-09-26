"""Frozen data models for the deterministic fleet context compiler (OMP-311 / FK-5).

Exactly one shape is legal for every value the compiler accepts and returns, so
the budget loop and the renderer never have to re-check a partially-filled
request. Every model forbids unknown fields and is frozen; a caller that builds
an invalid request fails at construction, not mid-compile.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from omp_work.v1.models import StrictModel
from pydantic import Field

Stage = Literal["plan", "implement", "audit"]
"""The workflow stage a bundle is compiled for."""

Section = Literal["exact", "structural", "procedural", "semantic"]
"""Render order of the four bundle sections, most authoritative first."""

Status = Literal["current", "stale", "denied", "withdrawn", "missing"]
"""Whether an item may render at all. Only ``current`` items render."""

SECTIONS: tuple[Section, ...] = ("exact", "structural", "procedural", "semantic")
"""Sections in canonical order; the layout is built from this tuple alone."""


class StageIdentity(StrictModel):
    """The exact work/stage/attempt identity a bundle is bound to."""

    work_id: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    stage: Stage
    attempt_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)


class ContextItem(StrictModel):
    """One candidate context item, before budget admission."""

    section: Section
    source: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    text: str = Field(min_length=1)
    status: Status
    detail: str = ""
    mandatory: bool = False
    score: float | None = None


class Exclusion(StrictModel):
    """An item that did not render, with the reason it was withheld."""

    section: Section
    source: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    detail: str = ""


class CompileRequest(StrictModel):
    """An immutable compile request.

    ``encoding`` names the tokenizer encoding the supplied counter corresponds
    to; the compiler records the counter's own ``profile`` in the bundle so a
    consumer can tell which counter produced the counts.
    """

    identity: StageIdentity
    token_budget: int = Field(gt=0)
    encoding: str = Field(min_length=1)
    items: tuple[ContextItem, ...] = ()


@runtime_checkable
class TokenCounter(Protocol):
    """Token counter the compiler charges against the budget.

    ``profile`` identifies the counter implementation, ``count`` returns one
    token count per input text in the same order.
    """

    profile: str

    def count(self, texts: Sequence[str]) -> list[int]: ...


__all__ = [
    "SECTIONS",
    "CompileRequest",
    "ContextItem",
    "Exclusion",
    "Section",
    "Stage",
    "StageIdentity",
    "Status",
    "TokenCounter",
]
