"""ADR 0004 control plane checks: high-risk review and contract freeze."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import fnmatch

from omp_work.control_plane.gate import Check, CheckContext, Proposal, Refusal

__all__ = [
    "BLAST_RADIUS_PATTERNS",
    "FROZEN_CONTRACT_PATHS",
    "check_contract_freeze",
    "check_high_risk_review",
    "contract_freeze",
    "high_risk_review",
]

_P = "python/omp-work/src/omp_work"

BLAST_RADIUS_PATTERNS: tuple[str, ...] = (
    "*ACTIVE-POLICY*",
    # ledger
    f"{_P}/v1/*",
    f"{_P}/operations/migrations/*",
    f"{_P}/operations/jobs_migrations/*",
    f"{_P}/operations/sql/*",
    # auth
    f"{_P}/operations/capabilities.py",
    f"{_P}/v1/owner_signature.py",
    f"{_P}/control_plane/*",
    # economy
    f"{_P}/jobs/budget.py",
    f"{_P}/spend_budget.py",
    f"{_P}/mission_budget.py",
    f"{_P}/economy_effort.py",
    # deploy/release
    ".github/workflows/*",
    "scripts/release*",
    "infra/*",
)

FROZEN_CONTRACT_PATHS: tuple[str, ...] = (
    f"{_P}/contracts/v1/*.json",
    "packages/work-client/src/contract.ts",
)


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    clean = path.removeprefix("./")
    return any(
        fnmatch.fnmatchcase(clean, pat) or fnmatch.fnmatchcase(path, pat)
        for pat in patterns
    )


def check_high_risk_review(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """ADR 0004 §3: high-risk blast radius review check."""
    if not proposal.touched_paths:
        return None

    if not any(_matches_any(p, BLAST_RADIUS_PATTERNS) for p in proposal.touched_paths):
        return None

    if proposal.reviewer_required is True:
        return None

    if proposal.reviewer_override_reason is not None:
        non_blank_count = sum(1 for c in proposal.reviewer_override_reason if not c.isspace())
        if non_blank_count >= 20:
            return None

    return Refusal(
        check="high_risk_review",
        code="review_required",
        raise_decision=False,
    )


def check_contract_freeze(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """ADR 0004 §4, D30: contract freeze check."""
    if proposal.contract_sha256 is not None:
        if proposal.contract_sha256 not in ctx.facts.approved_contract_sha256:
            return Refusal(
                check="contract_freeze",
                code="contract_not_approved",
                raise_decision=True,
                action_class="contract_hash",
            )
        return None

    if any(_matches_any(p, FROZEN_CONTRACT_PATHS) for p in proposal.touched_paths):
        return Refusal(
            check="contract_freeze",
            code="contract_frozen",
            raise_decision=True,
            action_class="contract_hash",
        )

    return None


high_risk_review = Check(name="high_risk_review", fn=check_high_risk_review)
object.__setattr__(high_risk_review, "__name__", "high_risk_review")

contract_freeze = Check(name="contract_freeze", fn=check_contract_freeze)
object.__setattr__(contract_freeze, "__name__", "contract_freeze")
