from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, BeforeValidator


def _to_str(v: object) -> str:
    if isinstance(v, (str, UUID)):
        return str(v)
    raise ValueError(f"Expected str or UUID, got {type(v).__name__}")


StringId = Annotated[str, BeforeValidator(_to_str)]


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceIdentity(StrictModel):
    workspace_id: StringId
    event_id: StringId
    event_sequence: int
    aggregate_id: StringId
    # Plain str, not StringId: a missing id is null, and StringId rejects None.
    work_id: str | None = None
    candidate_id: str | None = None
    revision_id: str | None = None


class Attribution(StrictModel):
    model: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    source: SourceIdentity

    @field_validator("model", "profile")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


PreconditionKey = Literal["project_id", "repository"]
PreconditionOp = Literal["eq", "ne"]


class Precondition(StrictModel):
    key: PreconditionKey
    op: PreconditionOp
    value: str


class SourceSpan(StrictModel):
    """Integer offsets into canonical payload text. No payload field name."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)


class Claim(StrictModel):
    text: str
    receipt_ids: tuple[StringId, ...] = ()
    source_span: SourceSpan | None = None
    evaluator: str | None = None
    outcome: Literal["PASS", "NEEDS_FIX", "BLOCKED"] | None = None


class Lesson(StrictModel):
    title: str = Field(min_length=1)
    steps: tuple[str, ...] = Field(min_length=1)
    preconditions: tuple[Precondition, ...] = ()
    claims: tuple[Claim, ...] = ()
    research_campaign_ids: tuple[StringId, ...] = ()


UnitState = Literal["queued", "running", "succeeded", "no_lesson", "failed"]
RunStatus = Literal["succeeded", "no_lesson", "partial", "failed"]

__all__ = [
    "Attribution",
    "Claim",
    "Lesson",
    "Precondition",
    "PreconditionKey",
    "PreconditionOp",
    "RunStatus",
    "SourceIdentity",
    "SourceSpan",
    "StrictModel",
    "StringId",
    "UnitState",
]
