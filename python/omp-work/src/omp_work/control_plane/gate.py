"""Control plane gate types, evaluator, signature verification, and decision payload generation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omp_work.action_tiers import TIER1, TIER2, TIER3
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.models import CreateDecisionPayload

__all__ = [
    "AcceptanceFact",
    "Check",
    "CheckContext",
    "ControlPlaneFacts",
    "DecisionPayload",
    "LeaseClaim",
    "LeaseFact",
    "MissionFact",
    "OwnerAuthorization",
    "Proposal",
    "Refusal",
    "ReservationFact",
    "Verdict",
    "authorization_message",
    "decision_payload",
    "evaluate",
    "verify_owner_authorization",
]


@dataclass(frozen=True)
class LeaseClaim:
    job_id: str | UUID
    worker_id: str
    fence: int


@dataclass(frozen=True)
class OwnerAuthorization:
    signature: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.expires_at, str):
            object.__setattr__(self, "expires_at", datetime.fromisoformat(self.expires_at))


@dataclass(frozen=True)
class Proposal:
    proposal_id: UUID | None = None
    workspace_id: UUID | None = None
    project_id: UUID | None = None
    mission_id: UUID | None = None
    proposer: str = "model"
    typed_command: str | None = None
    operation: str = "command"
    command_type: str | None = None
    action_class: str | None = None
    action: ActionRequest | None = None
    provenance: str | None = None
    basis_source: str | None = None
    basis_revision: int | None = None
    lease: LeaseClaim | None = None
    effort: str | None = None
    effort_reason: str | None = None
    reservation_id: str | None = None
    cost_usd: Decimal | None = None
    target_id: str | None = None
    touched_paths: tuple[str, ...] = ()
    reviewer_required: bool | None = None
    reviewer_override_reason: str | None = None
    contract_sha256: str | None = None
    lock_id: int | None = None
    owner_authorization: OwnerAuthorization | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.touched_paths, tuple):
            object.__setattr__(self, "touched_paths", tuple(self.touched_paths))
        if self.cost_usd is not None and not isinstance(self.cost_usd, Decimal):
            object.__setattr__(self, "cost_usd", Decimal(str(self.cost_usd)))
        for uuid_field in ("proposal_id", "workspace_id", "project_id", "mission_id"):
            val = getattr(self, uuid_field)
            if isinstance(val, str):
                object.__setattr__(self, uuid_field, UUID(val))


@dataclass(frozen=True)
class LeaseFact:
    worker_id: str
    fence: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.expires_at, str):
            object.__setattr__(self, "expires_at", datetime.fromisoformat(self.expires_at))


@dataclass(frozen=True)
class MissionFact:
    approved: bool
    in_flight: int = 0
    capacity: int = 1


@dataclass(frozen=True)
class ReservationFact:
    mission_id: UUID | str | None
    remaining_usd: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.remaining_usd, Decimal):
            object.__setattr__(self, "remaining_usd", Decimal(str(self.remaining_usd)))
        if isinstance(self.mission_id, str):
            try:
                object.__setattr__(self, "mission_id", UUID(self.mission_id))
            except ValueError:
                pass


@dataclass(frozen=True)
class AcceptanceFact:
    sealed_criteria: tuple[str, ...] = ()
    evidence: Any = ()
    author_id: str | UUID | None = None
    reviewer_id: str | UUID | None = None
    verdict: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sealed_criteria, tuple):
            object.__setattr__(self, "sealed_criteria", tuple(self.sealed_criteria))


@dataclass(frozen=True)
class ControlPlaneFacts:
    now: datetime
    current_revision: int
    leases: Mapping[str, LeaseFact] = field(default_factory=dict)
    missions: Mapping[UUID, MissionFact] = field(default_factory=dict)
    reservations: Mapping[str, ReservationFact] = field(default_factory=dict)
    standing_policies: tuple[StandingPolicy, ...] = ()
    repositories: tuple[RepositoryRecord, ...] = ()
    acceptance: Mapping[str, AcceptanceFact] = field(default_factory=dict)
    approved_contract_sha256: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.now, str):
            object.__setattr__(self, "now", datetime.fromisoformat(self.now))
        if not isinstance(self.standing_policies, tuple):
            object.__setattr__(self, "standing_policies", tuple(self.standing_policies))
        if not isinstance(self.repositories, tuple):
            object.__setattr__(self, "repositories", tuple(self.repositories))
        if not isinstance(self.approved_contract_sha256, frozenset):
            object.__setattr__(
                self, "approved_contract_sha256", frozenset(self.approved_contract_sha256)
            )


@dataclass(frozen=True)
class CheckContext:
    facts: ControlPlaneFacts
    verify_signature: Callable[[bytes, str], bool] | None = None


@dataclass(frozen=True)
class Refusal:
    check: str
    code: str
    raise_decision: bool
    action_class: str | None = None


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[Proposal, CheckContext], Refusal | Iterable[Refusal] | None]

    def __call__(self, proposal: Proposal, ctx: CheckContext) -> Any:
        return self.fn(proposal, ctx)


@dataclass(frozen=True)
class Verdict:
    refusals: tuple[Refusal, ...] = ()
    checks_run: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.refusals, tuple):
            object.__setattr__(self, "refusals", tuple(self.refusals))
        if not isinstance(self.checks_run, tuple):
            object.__setattr__(self, "checks_run", tuple(self.checks_run))

    @property
    def allowed(self) -> bool:
        return len(self.refusals) == 0

    @property
    def authorized(self) -> bool:
        return self.allowed

    @property
    def deciding_refusal(self) -> Refusal | None:
        return next((r for r in self.refusals if r.raise_decision), None)

    @property
    def raises_decision(self) -> bool:
        return self.deciding_refusal is not None

    def __bool__(self) -> bool:
        return self.allowed


def evaluate(
    proposal: Proposal,
    ctx: CheckContext,
    checks: Iterable[Check],
    *,
    budget_seconds: float = 10.0,
    clock: Callable[[], float] | None = None,
) -> Verdict:
    """Evaluate proposal across all checks, mapping errors/timeouts to decision-raising refusals."""
    if clock is None:
        clock = time.monotonic

    start = clock()
    refusals: list[Refusal] = []
    checks_run: list[str] = []

    for check in checks:
        name = getattr(check, "name", str(check))
        fn = getattr(check, "fn", check)
        checks_run.append(name)

        if clock() - start > budget_seconds:
            refusals.append(
                Refusal(
                    check=name,
                    code="check_timeout",
                    raise_decision=True,
                )
            )
            continue

        try:
            res = fn(proposal, ctx)
            if clock() - start > budget_seconds:
                refusals.append(
                    Refusal(
                        check=name,
                        code="check_timeout",
                        raise_decision=True,
                    )
                )
            elif res is not None:
                if isinstance(res, Refusal):
                    refusals.append(res)
                elif isinstance(res, Iterable):
                    for item in res:
                        if isinstance(item, Refusal):
                            refusals.append(item)
        except TimeoutError:
            refusals.append(
                Refusal(
                    check=name,
                    code="check_timeout",
                    raise_decision=True,
                )
            )
        except Exception:
            refusals.append(
                Refusal(
                    check=name,
                    code="check_error",
                    raise_decision=True,
                )
            )

    return Verdict(refusals=tuple(refusals), checks_run=tuple(checks_run))


def authorization_message(
    proposal: Proposal,
    expires_at: datetime | str | None = None,
) -> bytes:
    """Return canonical JSON bytes for control plane authorization."""
    if expires_at is None and proposal.owner_authorization is not None:
        expires_at = proposal.owner_authorization.expires_at

    exp_str: str | None = None
    if isinstance(expires_at, datetime):
        exp_str = expires_at.isoformat()
    elif expires_at is not None:
        exp_str = str(expires_at)

    payload: dict[str, object] = {
        "action_class": proposal.action_class,
        "command_type": proposal.command_type,
        "expires_at": exp_str,
        "kind": "control_plane_authorization",
        "lock_id": proposal.lock_id,
        "mission_id": str(proposal.mission_id) if proposal.mission_id is not None else None,
        "operation": proposal.operation,
        "project_id": str(proposal.project_id) if proposal.project_id is not None else None,
        "proposal_id": str(proposal.proposal_id) if proposal.proposal_id is not None else None,
        "proposer": proposal.proposer,
        "touched_paths": list(sorted(proposal.touched_paths)) if proposal.touched_paths else [],
        "typed_command": proposal.typed_command,
        "workspace_id": str(proposal.workspace_id) if proposal.workspace_id is not None else None,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def verify_owner_authorization(
    proposal: Proposal,
    ctx: CheckContext | None = None,
    *,
    facts: ControlPlaneFacts | None = None,
    verify_signature: Callable[[bytes, str], bool] | None = None,
) -> str | None:
    """Verify owner authorization on a proposal.

    Returns owner_signature_missing|invalid|expired, check_error, or None.
    """
    if ctx is not None:
        if facts is None:
            facts = ctx.facts
        if verify_signature is None:
            verify_signature = ctx.verify_signature

    auth = proposal.owner_authorization
    if auth is None or not auth.signature:
        return "owner_signature_missing"

    if auth.expires_at is None:
        return "owner_signature_missing"

    now = facts.now if facts is not None else None
    if now is not None:
        exp = auth.expires_at
        if exp.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        elif exp.tzinfo is None and now.tzinfo is not None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return "owner_signature_expired"

    if verify_signature is None:
        return "owner_signature_invalid"

    try:
        msg = authorization_message(proposal, auth.expires_at)
        valid = verify_signature(msg, auth.signature)
        if not valid:
            return "owner_signature_invalid"
        return None
    except Exception:
        return "check_error"


class DecisionPayload(CreateDecisionPayload):
    """CreateDecisionPayload supporting both attribute and dict item access."""

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


def decision_payload(
    proposal: Proposal,
    refusals: Verdict | Iterable[Refusal] | Refusal,
) -> DecisionPayload:
    """Generate a deterministic decision record payload for a refused proposal."""
    if isinstance(refusals, Verdict):
        refusal_list = list(refusals.refusals)
    elif isinstance(refusals, Refusal):
        refusal_list = [refusals]
    elif isinstance(refusals, Iterable):
        refusal_list = list(refusals)
    else:
        refusal_list = []

    deciding = next((r for r in refusal_list if r.raise_decision), None)

    if deciding is not None and deciding.action_class is not None:
        chosen_class = deciding.action_class
    elif proposal.action_class in TIER3:
        chosen_class = proposal.action_class
    elif proposal.action_class is None or (
        proposal.action_class not in TIER1 and proposal.action_class not in TIER2
    ):
        chosen_class = "unlisted"
    else:
        chosen_class = None

    proposal_id_str = (
        str(proposal.proposal_id)
        if proposal.proposal_id is not None
        else "00000000-0000-0000-0000-000000000000"
    )
    decision_id = uuid5(NAMESPACE_URL, proposal_id_str)

    if proposal.project_id is not None:
        project_id = proposal.project_id
    elif proposal.workspace_id is not None:
        project_id = proposal.workspace_id
    else:
        project_id = uuid5(NAMESPACE_URL, f"project:{proposal_id_str}")

    mission_id = str(proposal.mission_id) if proposal.mission_id is not None else None

    question = f"Authorize control plane proposal {proposal_id_str}?"
    why_it_matters = (
        "The action was refused by control plane checks and requires owner authorization."
    )
    risk_of_delay = (
        "The proposed action remains blocked until an owner decision is recorded."
    )
    options = ("authorize", "reject")
    risk_of_each_choice = {
        "authorize": "Executes the refused action under owner authority.",
        "reject": "Aborts the proposed action.",
    }
    default_if_any = "reject"

    return DecisionPayload(
        decision_id=decision_id,
        project_id=project_id,
        mission_id=mission_id,
        question=question,
        why_it_matters=why_it_matters,
        risk_of_delay=risk_of_delay,
        options=options,
        evidence_refs=(),
        default_if_any=default_if_any,
        risk_of_each_choice=risk_of_each_choice,
        action_class=chosen_class,  # type: ignore[arg-type]
        resume_state=None,
    )
