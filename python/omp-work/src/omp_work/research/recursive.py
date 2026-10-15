"""Immutable inputs and history for bounded R13 recursive research.

These records do not generate children. Admission of studies and proposals
always checks live standing authority; artifacts and restored history are never
authority. Method identity pins the generator, while
proposal equivalence pins recipe/spec content independently of display names.
Termination latches a reason without removing admitted studies, proposals, or
usage, and blocks every later admission.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Decimal, InvalidOperation, localcontext
from typing import Annotated

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from omp_work.research import mechanism, mechanism_recipe
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


def _semantic(payload: bytes) -> dict[str, object]:
    return {key: value for key, value in json.loads(payload).items() if key not in _BOOKKEEPING}


class _R13Inputs(StrictModel):
    recipe: bytes
    spec: bytes

    _seal = field_validator("recipe", "spec", mode="before")(seal_json)

    @model_validator(mode="after")
    def r13_recipe(self) -> _R13Inputs:
        mechanism_recipe.canonical_recipe(json.loads(self.recipe))
        return self

    @property
    def spec_sha256(self) -> str:
        return canonical.sha256(_semantic(self.spec))

    @property
    def equivalence_sha256(self) -> str:
        return canonical.sha256({
            "recipe": mechanism_recipe.canonical_recipe(json.loads(self.recipe)),
            "spec": _semantic(self.spec),
        })


class RecursiveProposal(_R13Inputs):
    proposal_id: _Nonempty
    study_id: _Nonempty


class RecursiveStudy(_WindowCaps, _R13Inputs):
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


class RecursiveUsage(StrictModel):
    study_id: _Nonempty
    compute_seconds: float = Field(ge=0, allow_inf_nan=False)
    tokens: _Count
    usd: Decimal = Field(ge=0, allow_inf_nan=False)


def _total_usd(usage: tuple[RecursiveUsage, ...]) -> Decimal:
    """Sum validated nonnegative deltas exactly, independent of ambient precision."""
    values = [item.usd for item in usage if item.usd]
    if not values:
        return Decimal(0)
    # Preserve every decimal place, plus enough carry digits for all events.
    exponent = min(value.as_tuple().exponent for value in values)
    with localcontext() as context:
        context.prec = max(value.adjusted() for value in values) - exponent + 1 + len(str(len(values)))
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        return sum(values, Decimal(0))


class RecursionState(StrictModel):
    # Append-only admission history; termination never removes a study.
    studies: tuple[RecursiveStudy, ...] = ()
    grant: CallerGrant
    proposal_history: tuple[RecursiveProposal, ...] = ()
    usage: tuple[RecursiveUsage, ...] = ()
    terminal_reason: _Nonempty | None = None
    confirmation_history: bytes = b"{}"

    @model_validator(mode="after")
    def history_bindings(self) -> RecursionState:
        if not self.studies:
            if self.proposal_history or self.usage:
                raise ValueError("proposal/usage references unknown study")
            return self
        from omp_work.research.recursive_confirmation import validate_history

        validate_history(self)
        for index, study in enumerate(self.studies):
            reason = _tree_reason(self.studies[:index], study, self.grant)
            if reason:
                raise ValueError(reason)
            reason = _content_reason(self.studies[:index], self.proposal_history, study)
            if reason:
                raise ValueError(reason)
        known = {study.study_id for study in self.studies}
        if any(item.study_id not in known for item in self.proposal_history + self.usage):
            raise ValueError("proposal/usage references unknown study")
        for index, proposal in enumerate(self.proposal_history):
            reason = _content_reason(self.studies, self.proposal_history[:index], proposal)
            if reason:
                raise ValueError(reason)
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
        total_compute = sum(item.compute_seconds for item in self.usage)
        total_tokens = sum(item.tokens for item in self.usage)
        total_usd = _total_usd(self.usage)
        if (
            total_compute >= limits.max_wall_clock_seconds
            or total_tokens >= limits.max_model_tokens
            or total_usd >= limits.max_usd
            or total_compute >= self.grant.limits.max_wall_clock_seconds
            or total_tokens >= self.grant.limits.max_model_tokens
            or total_usd >= self.grant.limits.max_usd
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


def _refuse(state: RecursionState, reason: str) -> RecursionState:
    """Latch the first terminal reason without removing any admitted state."""
    return state.model_copy(update={"terminal_reason": state.terminal_reason or reason})


def _admission_gate(
    state: RecursionState,
    grant: CallerGrant | None,
    policy: StandingPolicy | None,
    authority: ChangeAuthority | None,
    action: ActionRequest | None,
    repositories: Mapping[str, RepositoryRecord] | Sequence[RepositoryRecord] | None,
    now: datetime | None,
) -> str | None:
    """Live authority/window gate shared by study and proposal admission.

    Snapshots carry none of this context; every admission supplies the original
    signed grant and live authority again.
    """
    if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
        return "utc_now_required"
    if (
        not isinstance(grant, CallerGrant)
        or not isinstance(policy, StandingPolicy)
        or not isinstance(authority, ChangeAuthority)
        or not isinstance(action, ActionRequest)
        or not isinstance(repositories, (Mapping, Sequence))
        or isinstance(repositories, (str, bytes))
    ):
        return "authority_context_required"
    records = repositories.values() if isinstance(repositories, Mapping) else repositories
    if any(not isinstance(record, RepositoryRecord) for record in records):
        return "invalid_repositories"
    try:
        grant = CallerGrant.model_validate(grant.model_dump())
    except (ValidationError, AttributeError):
        return "invalid_admission"
    if grant != state.grant:
        return "grant_changed"
    try:
        authorize_standing_change(ChangeKind.create, authority)
        validate_policy(policy, repositories)
        if (
            str(policy.policy_id) != grant.policy_id
            or str(policy.decision_id) != grant.decision_id
            or str(authority.decision_id) != grant.decision_id
        ):
            return "authority_binding_mismatch"
        if (
            policy.action_class != "spend_beyond_threshold"
            or policy.money_limit_usd is None
            or not policy.money_limit_usd.is_finite()
            or grant.limits.max_usd > policy.money_limit_usd
            or policy.expires_at is None
            or policy.expires_at.utcoffset() != timedelta(0)
            or grant.window_end > policy.expires_at
        ):
            return "policy_grant_bounds"
        if action.amount_usd is None or not action.amount_usd.is_finite():
            return "invalid_action"
        if not covers(policy, action, repositories, now):
            return "policy_not_covered"
    except (StandingChangeRefused, PolicyRefused) as error:
        return error.code
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        return "invalid_authority_context"
    if not grant.window_start <= now < grant.window_end:
        return "outside_window"
    # Live authority is supplied again; retained state must satisfy the same
    # structural invariants as JSON restoration, including proposal history.
    for index, admitted in enumerate(state.studies):
        try:
            admitted = RecursiveStudy.model_validate(admitted.model_dump())
        except (ValidationError, AttributeError):
            return "invalid_history"
        reason = _tree_reason(state.studies[:index], admitted, grant)
        if reason:
            return reason
    try:
        RecursionState.model_validate(state.model_dump())
    except (ValidationError, AttributeError):
        return "invalid_history"
    return None


def _study_live_reason(state: RecursionState, study: RecursiveStudy, now: datetime) -> str | None:
    """Half-open window and tree-cumulative quota gate across candidate, ancestors, and grant."""
    known = {item.study_id: item for item in state.studies}
    total_compute = sum(item.compute_seconds for item in state.usage)
    total_tokens = sum(item.tokens for item in state.usage)
    total_usd = _total_usd(state.usage)

    current: RecursiveStudy | None = study
    while current is not None:
        if not current.window_start <= now < current.window_end:
            return "outside_window"
        if (
            total_compute >= current.limits.max_wall_clock_seconds
            or total_tokens >= current.limits.max_model_tokens
            or total_usd >= current.limits.max_usd
        ):
            return "quota_exhausted"
        if current.parent_id is None:
            break
        current = known.get(current.parent_id)

    if (
        total_compute >= state.grant.limits.max_wall_clock_seconds
        or total_tokens >= state.grant.limits.max_model_tokens
        or total_usd >= state.grant.limits.max_usd
    ):
        return "quota_exhausted"

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
    if state.terminal_reason is not None:
        return state
    reason = _admission_gate(state, grant, policy, authority, action, repositories, now)
    if reason is not None:
        return _refuse(state, reason)
    try:
        study = RecursiveStudy.model_validate(study.model_dump())
    except (ValidationError, AttributeError):
        return _refuse(state, "invalid_admission")
    reason = _tree_reason(state.studies, study, state.grant)
    if reason:
        return _refuse(state, reason)
    reason = _content_reason(state.studies, state.proposal_history, study)
    if reason:
        return _refuse(state, reason)
    if not isinstance(now, datetime):
        raise AssertionError("admission gate must validate now as a datetime")
    reason = _study_live_reason(state, study, now)
    if reason:
        return _refuse(state, reason)
    try:
        return RecursionState.model_validate({
            **state.model_dump(), "studies": (*state.studies, study),
        })
    except ValidationError:
        return _refuse(state, "invalid_history")


def terminate(state: RecursionState, reason: str) -> RecursionState:
    """Latch a terminal reason, preserving studies, proposals, usage, and any prior reason.

    Termination blocks every later admission and generates no fallback or
    automatic children; restored snapshots carry the latched reason.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("terminal reason must be a nonempty string")
    return _refuse(state, reason)


_GRANT_KEYS = frozenset(CallerGrant.model_fields) | {"grant"}


def _replacement_reason(proposal: _R13Inputs) -> str | None:
    """R13 inputs cannot carry replacements for pinned bindings or grant fields."""
    for payload in (proposal.recipe, proposal.spec):
        keys = set(json.loads(payload)) - _BOOKKEEPING
        if keys & frozenset(_PINNED_BINDINGS):
            return "binding_changed"
        if keys & _GRANT_KEYS:
            return "grant_changed"
    return None


def _content_reason(
    studies: tuple[RecursiveStudy, ...],
    history: tuple[RecursiveProposal, ...],
    candidate: RecursiveStudy | RecursiveProposal,
) -> str | None:
    """Shared baseline and identity contract for admission and restoration."""
    reason = _replacement_reason(candidate)
    if reason:
        return reason
    if isinstance(candidate, RecursiveProposal) and any(
        item.proposal_id == candidate.proposal_id for item in history
    ):
        return "reused_proposal_id"
    known = {item.study_id: item for item in studies}
    cursor = candidate.parent_id if isinstance(candidate, RecursiveStudy) else candidate.study_id
    ancestors: list[RecursiveStudy] = []
    while cursor is not None:
        ancestor = known[cursor]
        ancestors.append(ancestor)
        cursor = ancestor.parent_id
    baselines = studies if isinstance(candidate, RecursiveProposal) else tuple(ancestors)
    if any(
        item.equivalence_sha256 == candidate.equivalence_sha256
        for item in (*baselines, *history)
    ):
        return "equivalent_proposal"
    if studies and candidate.spec_sha256 != studies[0].spec_sha256:
        return "spec_changed"
    return None


def admit_proposal(
    state: RecursionState,
    proposal: RecursiveProposal,
    grant: CallerGrant | None = None,
    *,
    policy: StandingPolicy | None = None,
    authority: ChangeAuthority | None = None,
    action: ActionRequest | None = None,
    repositories: Mapping[str, RepositoryRecord] | Sequence[RepositoryRecord] | None = None,
    now: datetime | None = None,
    confirmation: object = None,
) -> RecursionState:
    """Admit a distinct recipe under live authority and cumulative resource gates.

    Family changes require fresh native confirmation of the frozen population.
    Legacy scalar changes remain eligible without confirmation. Neither path
    activates a policy or generates a child; equivalence ignores display names.
    """
    if state.terminal_reason is not None:
        return state
    reason = _admission_gate(state, grant, policy, authority, action, repositories, now)
    if reason is not None:
        return _refuse(state, reason)
    try:
        proposal = RecursiveProposal.model_validate(proposal.model_dump())
    except (ValidationError, AttributeError):
        return _refuse(state, "invalid_admission")
    study = {item.study_id: item for item in state.studies}.get(proposal.study_id)
    if study is None:
        return _refuse(state, "unadmitted_study")
    reason = _content_reason(state.studies, state.proposal_history, proposal)
    if reason is not None:
        return _refuse(state, reason)
    assert isinstance(now, datetime)
    reason = _study_live_reason(state, study, now)
    if reason is not None:
        return _refuse(state, reason)
    try:
        mechanism.generation_job(_semantic(proposal.spec), _semantic(proposal.recipe))
    except (ValueError, TypeError, KeyError, OverflowError):
        return _refuse(state, "invalid_admission")
    from omp_work.research import recursive_confirmation

    if recursive_confirmation.structural_change(state, proposal):
        state, reason = recursive_confirmation.confirm(state, proposal, confirmation, now)
        if reason:
            return _refuse(state, reason)
    try:
        return RecursionState.model_validate({
            **state.model_dump(), "proposal_history": (*state.proposal_history, proposal),
        })
    except ValidationError:
        return _refuse(state, "invalid_history")


def record_usage(
    state: RecursionState,
    study_id: str | RecursiveUsage | None = None,
    compute_seconds: float | int = 0,
    tokens: int = 0,
    usd: Decimal | int = Decimal(0),
    *,
    delta: RecursiveUsage | None = None,
    usage: RecursiveUsage | None = None,
) -> RecursionState:
    """Record a finite nonnegative resource delta, terminating on quota exhaustion.

    Retains the crossing event in state.usage; USD values must be Decimal without
    float conversion. Quota comparison compares tree-cumulative usage against
    applicable envelopes (grant, target study, and every ancestor).
    """
    if usage is not None:
        target = usage
    elif delta is not None:
        target = delta
    elif isinstance(study_id, RecursiveUsage):
        target = study_id
    elif isinstance(study_id, str):
        target = None
    else:
        raise ValueError("study_id must be a nonempty string or RecursiveUsage")

    if target is not None:
        s_id = target.study_id
        c_sec = target.compute_seconds
        tok = target.tokens
        u = target.usd
    else:
        s_id = study_id
        c_sec = compute_seconds
        tok = tokens
        u = usd

    if not isinstance(s_id, str) or not s_id.strip():
        raise ValueError("study_id must be a nonempty string")

    known = {s.study_id: s for s in state.studies}
    if s_id not in known:
        raise ValueError(f"study {s_id!r} is not an admitted study")

    if isinstance(c_sec, bool) or not isinstance(c_sec, (int, float)):
        raise ValueError("compute_seconds must be a finite number")
    if not math.isfinite(c_sec):
        raise ValueError("compute_seconds must be finite")
    if c_sec < 0:
        raise ValueError("compute_seconds must be nonnegative")
    c_sec = float(c_sec)

    if isinstance(tok, bool) or not isinstance(tok, int):
        raise ValueError("tokens must be an integer")
    if tok < 0:
        raise ValueError("tokens must be nonnegative")

    if isinstance(u, float):
        raise ValueError("USD must be Decimal; float conversion not permitted")
    if isinstance(u, bool) or not isinstance(u, (Decimal, int)):
        raise ValueError("USD must be Decimal")
    if isinstance(u, int):
        u = Decimal(u)
    if not u.is_finite():
        raise ValueError("USD must be finite")
    if u < 0:
        raise ValueError("USD must be nonnegative")

    event = RecursiveUsage(
        study_id=s_id,
        compute_seconds=c_sec,
        tokens=tok,
        usd=u,
    )
    new_usage = (*state.usage, event)

    total_compute = sum(item.compute_seconds for item in new_usage)
    total_tokens = sum(item.tokens for item in new_usage)
    total_usd = _total_usd(new_usage)

    target_study = known[s_id]
    applicable_limits = [state.grant.limits, target_study.limits]
    cursor = target_study.parent_id
    while cursor is not None:
        ancestor = known[cursor]
        applicable_limits.append(ancestor.limits)
        cursor = ancestor.parent_id

    exhausted = any(
        total_compute >= lim.max_wall_clock_seconds
        or total_tokens >= lim.max_model_tokens
        or total_usd >= lim.max_usd
        for lim in applicable_limits
    )

    terminal_reason = state.terminal_reason or ("quota_exhausted" if exhausted else None)

    return RecursionState(
        studies=state.studies,
        grant=state.grant,
        proposal_history=state.proposal_history,
        usage=new_usage,
        terminal_reason=terminal_reason,
        confirmation_history=state.confirmation_history,
    )
