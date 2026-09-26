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


class Claim(StrictModel):
    text: str
    receipt_ids: tuple[StringId, ...] = ()


class Lesson(StrictModel):
    title: str = Field(min_length=1)
    steps: tuple[str, ...] = Field(min_length=1)
    preconditions: tuple[Precondition, ...] = ()
    claims: tuple[Claim, ...] = ()


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
    "StrictModel",
    "StringId",
    "UnitState",
]
