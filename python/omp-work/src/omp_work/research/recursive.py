"""Immutable inputs and history for bounded R13 recursive research.

These records do not generate or activate children. A caller grant retains the
signed authority binding; signature verification and standing-policy coverage
remain the caller's responsibility. Method identity pins the generator, while
proposal equivalence pins recipe/spec content independently of display names.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator, model_validator

from omp_work.research.accounting import ResourceLimits
from omp_work.v1 import canonical
from omp_work.v1.models import StrictModel, hex64

_Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Count = Annotated[int, Field(strict=True, ge=0)]
_BOOKKEEPING = frozenset({"id", "name", "label"})


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("window timestamps must be UTC")
    return value


def _limits(value: ResourceLimits) -> ResourceLimits:
    if (
        isinstance(value.max_wall_clock_seconds, bool)
        or not isinstance(value.max_wall_clock_seconds, (int, float))
        or not math.isfinite(value.max_wall_clock_seconds)
        or value.max_wall_clock_seconds <= 0
        or isinstance(value.max_model_tokens, bool)
        or not isinstance(value.max_model_tokens, int)
        or value.max_model_tokens <= 0
        or not isinstance(value.max_usd, Decimal)
        or not value.max_usd.is_finite()
        or value.max_usd <= 0
    ):
        raise ValueError("resource caps must be finite and positive")
    return value


class _WindowCaps(StrictModel):
    window_start: AwareDatetime
    window_end: AwareDatetime
    max_depth: _Count
    max_descendants: _Count
    limits: ResourceLimits

    _validate_utc = field_validator("window_start", "window_end")(_utc)
    _validate_limits = field_validator("limits")(_limits)

    @model_validator(mode="after")
    def ordered_window(self) -> _WindowCaps:
        if self.window_start >= self.window_end:
            raise ValueError("window must have start < end")
        return self


class TaskEvidence(StrictModel):
    task_id: _Nonempty
    content_sha256: hex64


class UpstreamAsset(StrictModel):
    identity: _Nonempty
    content_sha256: hex64


class RecursiveStudy(_WindowCaps):
    study_id: _Nonempty
    parent_id: _Nonempty | None
    depth: _Count
    method_version: _Nonempty
    method_sha256: hex64
    lower_researcher_sha256: hex64
    objective: _Nonempty
    evaluator_component_sha256: hex64
    evaluator_artifact_sha256: hex64
    protocol_sha256: hex64
    dev: tuple[TaskEvidence, ...] = Field(min_length=1)
    confirmation: tuple[TaskEvidence, ...] = Field(min_length=1)
    upstream_assets: tuple[UpstreamAsset, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def study_bindings(self) -> RecursiveStudy:
        if (self.parent_id is None) != (self.depth == 0):
            raise ValueError("root requires explicit depth 0; children require a parent")
        if self.depth > self.max_depth:
            raise ValueError("depth exceeds max_depth")
        _disjoint_evidence(self.dev + self.confirmation)
        identities = [asset.identity for asset in self.upstream_assets]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate upstream asset identity")
        return self


class CallerGrant(_WindowCaps):
    caller_id: _Nonempty
    policy_id: _Nonempty
    decision_id: _Nonempty
    signature_sha256: hex64

    def signing_bytes(self) -> bytes:
        """Canonical payload bound by the caller's signature, excluding itself."""
        return canonical.canonical_json(
            self.model_dump(mode="json", exclude={"signature_sha256"})
        ).encode("utf-8")


def _disjoint_evidence(evidence: tuple[TaskEvidence, ...]) -> None:
    ids = [item.task_id for item in evidence]
    digests = [item.content_sha256 for item in evidence]
    if len(set(ids)) != len(ids) or len(set(digests)) != len(digests):
        raise ValueError("task IDs and content digests must be disjoint across cohorts/history")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def seal_json(value: object) -> bytes:
    """Snapshot a nonempty JSON object as immutable canonical UTF-8 bytes."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value, parse_constant=_reject_constant)
    if not isinstance(value, dict) or not value:
        raise ValueError("recipe/spec must be a nonempty JSON object")
    # Validate JSON types and finite numbers before canonical's default=str can
    # turn arbitrary Python objects into a different, apparently valid recipe.
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except TypeError as error:
        raise ValueError("recipe/spec must contain only JSON values") from error
    return canonical.canonical_json(json.loads(encoded)).encode("utf-8")


class RecursiveProposal(StrictModel):
    proposal_id: _Nonempty
    study_id: _Nonempty
    recipe: bytes
    spec: bytes

    _seal = field_validator("recipe", "spec", mode="before")(seal_json)

    @model_validator(mode="after")
    def r13_recipe(self) -> RecursiveProposal:
        recipe = json.loads(self.recipe)
        parameters = {key: value for key, value in recipe.items() if key not in _BOOKKEEPING}
        if set(parameters) != {"min_metric"}:
            raise ValueError("R13 recipe supports min_metric only")
        metric = parameters["min_metric"]
        if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric):
            raise ValueError("min_metric must be a finite number")
        return self

    @property
    def equivalence_sha256(self) -> str:
        return canonical.sha256({
            field: {key: value for key, value in json.loads(payload).items() if key not in _BOOKKEEPING}
            for field, payload in (("recipe", self.recipe), ("spec", self.spec))
        })


class RecursiveUsage(StrictModel):
    study_id: _Nonempty
    compute_seconds: float = Field(ge=0, allow_inf_nan=False)
    tokens: _Count
    usd: Decimal = Field(ge=0, allow_inf_nan=False)


class RecursionState(StrictModel):
    studies: tuple[RecursiveStudy, ...] = Field(min_length=1)
    grant: CallerGrant
    proposal_history: tuple[RecursiveProposal, ...] = ()
    usage: tuple[RecursiveUsage, ...] = ()
    terminal_reason: _Nonempty | None = None

    @model_validator(mode="after")
    def history_bindings(self) -> RecursionState:
        root = self.studies[0]
        if root.parent_id is not None:
            raise ValueError("history must start with its root")
        known: dict[str, RecursiveStudy] = {}
        evidence: tuple[TaskEvidence, ...] = ()
        for study in self.studies:
            if study.study_id in known:
                raise ValueError("duplicate study ID")
            if study is not root:
                parent = known.get(study.parent_id)
                if parent is None or study.depth != parent.depth + 1:
                    raise ValueError("history requires one root and ordered parent/depth bindings")
            if (
                study.depth > root.max_depth
                or study.window_start < root.window_start
                or study.window_end > root.window_end
            ):
                raise ValueError("study exceeds root bounds")
            known[study.study_id] = study
            evidence += study.dev + study.confirmation
        _disjoint_evidence(evidence)
        if len(self.studies) - 1 > root.max_descendants:
            raise ValueError("history exceeds max_descendants")
        grant = self.grant
        if (
            root.window_start < grant.window_start
            or root.window_end > grant.window_end
            or root.max_depth > grant.max_depth
            or root.max_descendants > grant.max_descendants
            or root.limits.max_wall_clock_seconds > grant.limits.max_wall_clock_seconds
            or root.limits.max_model_tokens > grant.limits.max_model_tokens
            or root.limits.max_usd > grant.limits.max_usd
        ):
            raise ValueError("root exceeds caller grant")
        proposal_ids = [proposal.proposal_id for proposal in self.proposal_history]
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("duplicate proposal ID")
        if any(item.study_id not in known for item in self.proposal_history + self.usage):
            raise ValueError("proposal/usage references unknown study")
        return self

    def refuse_reason(self, now: datetime) -> str | None:
        """Tree-cumulative gate before child admission; [start,end), equality exhausted."""
        if now.tzinfo is None:
            raise ValueError("admission time must be aware UTC")
        _utc(now)
        root = self.studies[0]
        if self.terminal_reason is not None:
            return self.terminal_reason
        if not root.window_start <= now < root.window_end:
            return "outside_window"
        limits = root.limits
        if (
            sum(item.compute_seconds for item in self.usage) >= limits.max_wall_clock_seconds
            or sum(item.tokens for item in self.usage) >= limits.max_model_tokens
            or sum((item.usd for item in self.usage), Decimal(0)) >= limits.max_usd
        ):
            return "quota_exhausted"
        if root.max_depth == 0:
            return "max_depth"
        if len(self.studies) - 1 >= root.max_descendants:
            return "max_descendants"
        return None
