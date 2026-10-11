"""Immutable inputs and history for bounded R13 recursive research.

These records do not generate children. Admission always checks live standing
authority; artifacts and restored history are never authority. Method identity
pins the generator, while
proposal equivalence pins recipe/spec content independently of display names.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from omp_work.research.accounting import ResourceLimits
from omp_work.standing_change import (
    ChangeAuthority, ChangeKind, StandingChangeRefused, authorize_standing_change,
)
from omp_work.standing_policy import (
    ActionRequest, PolicyRefused, RepositoryRecord, StandingPolicy, covers, validate_policy,
)
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
    dev_sha256: hex64
    confirmation_sha256: hex64
    upstream_assets: tuple[UpstreamAsset, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def study_bindings(self) -> RecursiveStudy:
        if (self.parent_id is None) != (self.depth == 0):
            raise ValueError("root requires explicit depth 0; children require a parent")
        if self.depth > self.max_depth:
            raise ValueError("depth exceeds max_depth")
        for evidence, digest in (
            (self.dev, self.dev_sha256), (self.confirmation, self.confirmation_sha256),
        ):
            if digest != canonical.sha256(sorted(item.content_sha256 for item in evidence)):
                raise ValueError("cohort digest must bind canonical task content identities")
        _disjoint_cohorts((self,))
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


def _disjoint_cohorts(studies: tuple[RecursiveStudy, ...]) -> None:
    _disjoint_evidence(tuple(
        item for study in studies for item in study.dev + study.confirmation
    ))
    digests = [
        digest for study in studies for digest in (study.dev_sha256, study.confirmation_sha256)
    ]
    if len(set(digests)) != len(digests):
        raise ValueError("whole-cohort content digests must be disjoint across cohorts/history")


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
    # Append-only admission history; termination never removes a study.
    studies: tuple[RecursiveStudy, ...] = ()
    grant: CallerGrant
    proposal_history: tuple[RecursiveProposal, ...] = ()
    usage: tuple[RecursiveUsage, ...] = ()
    terminal_reason: _Nonempty | None = None

    @model_validator(mode="after")
    def history_bindings(self) -> RecursionState:
        if not self.studies:
            if self.proposal_history or self.usage:
                raise ValueError("proposal/usage references unknown study")
            return self
        root = self.studies[0]
        if root.parent_id is not None:
            raise ValueError("history must start with its root")
        known: dict[str, RecursiveStudy] = {}
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
        _disjoint_cohorts(self.studies)
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
        if self.terminal_reason is not None:
            return self.terminal_reason
        if not self.studies:
            return None
        root = self.studies[0]
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


def _within(study: RecursiveStudy, bounds: _WindowCaps) -> bool:
    return (
        study.window_start >= bounds.window_start
        and study.window_end <= bounds.window_end
        and study.max_depth <= bounds.max_depth
        and study.max_descendants <= bounds.max_descendants
        and study.limits.max_wall_clock_seconds <= bounds.limits.max_wall_clock_seconds
        and study.limits.max_model_tokens <= bounds.limits.max_model_tokens
        and study.limits.max_usd <= bounds.limits.max_usd
    )


_PINNED_BINDINGS = (
    "objective", "evaluator_component_sha256", "evaluator_artifact_sha256",
    "protocol_sha256", "upstream_assets", "method_version", "method_sha256",
    "lower_researcher_sha256",
)


def _subtree_ids(studies: tuple[RecursiveStudy, ...], study_id: str) -> set[str]:
    """Ordered admission history retains every descendant, regardless of outcome."""
    subtree = {study_id}
    for admitted in studies:
        if admitted.parent_id in subtree:
            subtree.add(admitted.study_id)
    return subtree


def _tree_reason(
    studies: tuple[RecursiveStudy, ...], study: RecursiveStudy, grant: CallerGrant,
) -> str | None:
    """Prospective append, used for new admissions and restored history alike."""
    known = {item.study_id: item for item in studies}
    if study.study_id in known:
        return "reused_study_id"
    if study.parent_id == study.study_id:
        return "ancestry_cycle"
    if not _within(study, grant):
        return "grant_bounds"
    if study.parent_id is None:
        if studies:
            return "second_root"
        return None if study.depth == 0 else "root_depth"
    ancestors: list[RecursiveStudy] = []
    cursor = study.parent_id
    seen = {study.study_id}
    while cursor is not None:
        if cursor in seen:
            return "ancestry_cycle"
        seen.add(cursor)
        ancestor = known.get(cursor)
        if ancestor is None:
            return "unadmitted_parent" if not ancestors else "missing_ancestor"
        ancestors.append(ancestor)
        cursor = ancestor.parent_id
    if not studies or ancestors[-1] != studies[0] or ancestors[-1].depth != 0:
        return "missing_root"
    if study.depth != ancestors[0].depth + 1:
        return "parent_depth"
    for ancestor in ancestors:
        if study.depth > ancestor.max_depth:
            return "max_depth"
        if not _within(study, ancestor):
            return "ancestor_bounds"
        if any(getattr(study, field) != getattr(ancestor, field) for field in _PINNED_BINDINGS):
            return "binding_changed"
        # Count the entire admitted subtree, including completed/terminated work.
        # Subtree size includes the ancestor, offsetting the prospective child.
        if len(_subtree_ids(studies, ancestor.study_id)) > ancestor.max_descendants:
            return "max_descendants"
    if len(studies) > grant.max_descendants:
        return "max_descendants"
    try:
        _disjoint_cohorts((*studies, study))
    except ValueError:
        return "cohort_reused"
    return None


def admit_study(
    state: RecursionState,
    study: RecursiveStudy,
    grant: CallerGrant | None = None,
    *,
    policy: StandingPolicy | None = None,
    authority: ChangeAuthority | None = None,
    action: ActionRequest | None = None,
    repositories: Mapping[str, RepositoryRecord] | Sequence[RepositoryRecord] | None = None,
    now: datetime | None = None,
) -> RecursionState:
    """Admit one study or terminate with a reason, retaining all prior history.

    Every call supplies the original signed grant and live authority context.
    No parent-spec, artifact, standalone-child or snapshot overload exists.
    """
    def refuse(reason: str) -> RecursionState:
        return state.model_copy(update={"terminal_reason": state.terminal_reason or reason})

    if state.terminal_reason is not None:
        return state
    if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
        return refuse("utc_now_required")
    if (
        not isinstance(grant, CallerGrant)
        or not isinstance(policy, StandingPolicy)
        or not isinstance(authority, ChangeAuthority)
        or not isinstance(action, ActionRequest)
        or not isinstance(repositories, (Mapping, Sequence))
        or isinstance(repositories, (str, bytes))
    ):
        return refuse("authority_context_required")
    records = repositories.values() if isinstance(repositories, Mapping) else repositories
    if any(not isinstance(record, RepositoryRecord) for record in records):
        return refuse("invalid_repositories")
    try:
        grant = CallerGrant.model_validate(grant.model_dump())
        study = RecursiveStudy.model_validate(study.model_dump())
    except (ValidationError, AttributeError):
        return refuse("invalid_admission")
    if grant != state.grant:
        return refuse("grant_changed")
    try:
        authorize_standing_change(ChangeKind.create, authority)
        validate_policy(policy, repositories)
        if (
            str(policy.policy_id) != grant.policy_id
            or str(policy.decision_id) != grant.decision_id
            or str(authority.decision_id) != grant.decision_id
        ):
            return refuse("authority_binding_mismatch")
        if (
            policy.action_class != "spend_beyond_threshold"
            or policy.money_limit_usd is None
            or not policy.money_limit_usd.is_finite()
            or grant.limits.max_usd > policy.money_limit_usd
            or policy.expires_at is None
            or policy.expires_at.utcoffset() != timedelta(0)
            or grant.window_end > policy.expires_at
        ):
            return refuse("policy_grant_bounds")
        if action.amount_usd is None or not action.amount_usd.is_finite():
            return refuse("invalid_action")
        if not covers(policy, action, repositories, now):
            return refuse("policy_not_covered")
    except (StandingChangeRefused, PolicyRefused) as error:
        return refuse(error.code)
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        return refuse("invalid_authority_context")
    if not grant.window_start <= now < grant.window_end:
        return refuse("outside_window")
    # Revalidate restored snapshots; their records cannot authorize a new call.
    for index, admitted in enumerate(state.studies):
        try:
            admitted = RecursiveStudy.model_validate(admitted.model_dump())
        except (ValidationError, AttributeError):
            return refuse("invalid_history")
        reason = _tree_reason(state.studies[:index], admitted, grant)
        if reason:
            return refuse(reason)
    reason = _tree_reason(state.studies, study, grant)
    if reason:
        return refuse(reason)
    known = {item.study_id: item for item in state.studies}
    current = study
    while True:
        if not current.window_start <= now < current.window_end:
            return refuse("outside_window")
        descendants = _subtree_ids(state.studies, current.study_id)
        usage = [item for item in state.usage if item.study_id in descendants]
        if (
            sum(item.compute_seconds for item in usage) >= current.limits.max_wall_clock_seconds
            or sum(item.tokens for item in usage) >= current.limits.max_model_tokens
            or sum((item.usd for item in usage), Decimal(0)) >= current.limits.max_usd
        ):
            return refuse("quota_exhausted")
        if current.parent_id is None:
            break
        current = known[current.parent_id]
    try:
        return RecursionState.model_validate({
            **state.model_dump(), "studies": (*state.studies, study),
        })
    except ValidationError:
        return refuse("invalid_history")
