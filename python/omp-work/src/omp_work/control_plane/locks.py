"""Control plane lock integrity check for D35 safety envelope locks.

Enforces that D35 locks, safety-envelope enforcement code, and safeguard
disabling cannot be modified or bypassed by automated actors (models,
workers, orchestrators, or typed commands). Only an owner-authorized proposal
with a valid unexpired signature can modify a lock or touch lock-enforcement
paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from types import MappingProxyType

from omp_work.control_plane.gate import (
    Check,
    CheckContext,
    Proposal,
    Refusal,
    verify_owner_authorization,
)

__all__ = [
    "LOCKS",
    "LOCK_ENFORCEMENT_PATHS",
    "lock_integrity",
]

_P = "python/omp-work/src/omp_work"

LOCKS: Mapping[int, str] = MappingProxyType(
    {
        1: "Repository/path allowlist",
        2: "Isolated worktree/sandbox",
        3: "No source credentials in workers",
        4: "Single mutation authority",
        5: "Lease + idempotency enforcement",
        6: "Independent verification",
        7: "Budget caps",
        8: "Network egress policy",
        9: "Protected-action gate",
        10: "Stop means pause",
        11: "Audit trail",
        12: "Fail closed",
    }
)

LOCK_ENFORCEMENT_PATHS: tuple[str, ...] = (
    f"{_P}/control_plane/*",
    f"{_P}/action_tiers.py",
    f"{_P}/standing_policy.py",
    f"{_P}/standing_change.py",
    f"{_P}/v1/owner_signature.py",
    f"{_P}/v1/decision_records.py",
    f"{_P}/v1/agent_stop.py",
    f"{_P}/jobs/lease.py",
    f"{_P}/jobs/admission.py",
    f"{_P}/jobs/stage_admission.py",
    f"{_P}/jobs/budget.py",
    f"{_P}/spend_budget.py",
    f"{_P}/mission_budget.py",
    f"{_P}/campaign_budget_guard.py",
    f"{_P}/egress_*.py",
)

_DISABLE_SAFEGUARDS = "disable_safeguards"
_LOCK_CHANGE = "lock_change"
_OWNER = "owner"


def _matches_enforcement_path(path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in LOCK_ENFORCEMENT_PATHS)


def _lock_integrity(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Enforce D35 lock integrity and safety envelope immutability."""
    is_lock_change = proposal.operation == _LOCK_CHANGE
    is_disable_safeguards = proposal.action_class == _DISABLE_SAFEGUARDS
    touches_enforcement_path = any(
        _matches_enforcement_path(p) for p in proposal.touched_paths
    )

    if not (is_lock_change or is_disable_safeguards or touches_enforcement_path):
        return None

    if is_lock_change:
        if proposal.lock_id is None or proposal.lock_id not in LOCKS:
            return Refusal(
                check="lock_integrity",
                code="lock_unknown",
                raise_decision=True,
                action_class=_DISABLE_SAFEGUARDS,
            )

    if proposal.proposer != _OWNER:
        return Refusal(
            check="lock_integrity",
            code="lock_change_not_owner",
            raise_decision=True,
            action_class=_DISABLE_SAFEGUARDS,
        )

    auth_code = verify_owner_authorization(proposal, ctx)
    if auth_code is not None:
        return Refusal(
            check="lock_integrity",
            code=auth_code,
            raise_decision=True,
            action_class=_DISABLE_SAFEGUARDS,
        )

    return None


lock_integrity = Check(name="lock_integrity", fn=_lock_integrity)
