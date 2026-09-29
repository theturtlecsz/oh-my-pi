"""Standing mandate records, scope verification, tier 3 handling, and change classification (OMP-418)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from omp_work.action_tiers import tier_of
from omp_work.standing_change import ChangeKind

__all__ = [
    "MandateRefused",
    "MissionScopeDraft",
    "ScopeVerdict",
    "StandingMandate",
    "encounter_tier3",
    "mandate_change_kind",
    "mission_scope",
    "validate_mandate",
]


class MandateRefused(Exception):
    """Raised when a standing mandate fails validation."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class StandingMandate:
    mandate_id: UUID | str
    goals: frozenset[str] = frozenset()
    repositories: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    tier3_classes: frozenset[str] = frozenset()
    decision_id: UUID | str | None = None

    def __post_init__(self) -> None:
        for field in ("goals", "repositories", "capabilities", "tier3_classes"):
            val = getattr(self, field)
            if val is not None and not isinstance(val, frozenset):
                object.__setattr__(self, field, frozenset(str(x) for x in val))
            elif val is None:
                object.__setattr__(self, field, frozenset())


def validate_mandate(mandate: StandingMandate) -> None:
    """Validate a standing mandate against ADR 0005 rules.

    Raises MandateRefused if validation fails:
    - missing_decision: mandate has no decision_id
    - not_tier3: any action class in tier3_classes has tier_of != 3
    """
    if not mandate.decision_id:
        raise MandateRefused(
            "missing_decision", "Standing mandate requires a decision_id"
        )

    for action_class in mandate.tier3_classes:
        if tier_of(action_class) != 3:
            raise MandateRefused(
                "not_tier3",
                f"Action class {action_class!r} is not tier 3 (got tier {tier_of(action_class)})",
            )


@dataclass(frozen=True)
class MissionScopeDraft:
    goals: frozenset[str] = frozenset()
    repositories: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    tier3_classes: frozenset[str] = frozenset()
    budget_ceiling_usd: Decimal | None = None
    budget_threshold_usd: Decimal | None = None

    def __post_init__(self) -> None:
        for field in ("goals", "repositories", "capabilities", "tier3_classes"):
            val = getattr(self, field)
            if val is not None and not isinstance(val, frozenset):
                object.__setattr__(self, field, frozenset(str(x) for x in val))
            elif val is None:
                object.__setattr__(self, field, frozenset())
        if self.budget_ceiling_usd is not None and not isinstance(self.budget_ceiling_usd, Decimal):
            object.__setattr__(self, "budget_ceiling_usd", Decimal(str(self.budget_ceiling_usd)))
        if self.budget_threshold_usd is not None and not isinstance(self.budget_threshold_usd, Decimal):
            object.__setattr__(self, "budget_threshold_usd", Decimal(str(self.budget_threshold_usd)))


@dataclass(frozen=True)
class ScopeVerdict:
    status: str
    basis_mandate_id: UUID | str | None = None
    basis_decision_id: UUID | str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))


def mission_scope(
    mandate: StandingMandate | None,
    draft: MissionScopeDraft,
    standing_ceiling_usd: Decimal | None = None,
) -> ScopeVerdict:
    """Evaluate whether a mission scope draft is approved under a standing mandate.

    Returns ScopeVerdict with status "approved" if all checks pass,
    or "awaiting_confirmation" with reasons for each failure:
    - no_mandate: when mandate is None
    - no_goals: when draft.goals is empty
    - goal_outside_mandate: when a draft goal is not in mandate.goals
    - repository_outside_mandate: when a draft repository is not in mandate.repositories
    - capability_outside_mandate: when a draft capability is not in mandate.capabilities
    - tier3_class_outside_mandate: when a draft tier 3 class is not in mandate.tier3_classes
    - no_budget: neither draft ceiling nor standing ceiling is specified
    - no_standing_budget: draft ceiling is specified but project has no standing ceiling
    - budget_over_standing: draft ceiling exceeds standing ceiling
    - threshold_over_ceiling: draft threshold exceeds effective ceiling
    """
    if standing_ceiling_usd is not None and not isinstance(standing_ceiling_usd, Decimal):
        standing_ceiling_usd = Decimal(str(standing_ceiling_usd))

    reasons: list[str] = []

    if mandate is None:
        reasons.append("no_mandate")
    else:
        if not draft.goals.issubset(mandate.goals):
            reasons.append("goal_outside_mandate")
        if not draft.repositories.issubset(mandate.repositories):
            reasons.append("repository_outside_mandate")
        if not draft.capabilities.issubset(mandate.capabilities):
            reasons.append("capability_outside_mandate")
        if not draft.tier3_classes.issubset(mandate.tier3_classes):
            reasons.append("tier3_class_outside_mandate")

    if not draft.goals:
        reasons.append("no_goals")

    # Budget checks
    draft_ceiling = draft.budget_ceiling_usd
    standing_ceiling = standing_ceiling_usd

    if draft_ceiling is None and standing_ceiling is None:
        reasons.append("no_budget")
    elif draft_ceiling is not None and standing_ceiling is None:
        reasons.append("no_standing_budget")
    elif draft_ceiling is not None and standing_ceiling is not None:
        if draft_ceiling > standing_ceiling:
            reasons.append("budget_over_standing")

    effective_ceiling = draft_ceiling if draft_ceiling is not None else standing_ceiling
    if effective_ceiling is not None and draft.budget_threshold_usd is not None:
        if draft.budget_threshold_usd > effective_ceiling:
            reasons.append("threshold_over_ceiling")

    if reasons:
        return ScopeVerdict(
            status="awaiting_confirmation",
            basis_mandate_id=None,
            basis_decision_id=None,
            reasons=tuple(reasons),
        )

    return ScopeVerdict(
        status="approved",
        basis_mandate_id=mandate.mandate_id if mandate is not None else None,
        basis_decision_id=mandate.decision_id if mandate is not None else None,
        reasons=(),
    )


def encounter_tier3(
    mandate: StandingMandate | None,
    mission_status: str,
    action_class: str,
    owner_signed: bool,
) -> tuple[str, str]:
    """Encounter a tier 3 action class during mission execution (D29, D35).

    Returns (status, outcome):
    - If class in mandate: status unchanged; outcome is "allowed" if owner_signed else "blocked_owner_signature".
    - Else: status becomes "awaiting_confirmation"; outcome is "blocked_owner_signature".
    """
    if mandate is not None and action_class in mandate.tier3_classes:
        outcome = "allowed" if owner_signed else "blocked_owner_signature"
        return mission_status, outcome
    return "awaiting_confirmation", "blocked_owner_signature"


def mandate_change_kind(
    old: StandingMandate | None,
    new: StandingMandate,
) -> ChangeKind:
    """Classify the kind of change between an old standing mandate and a new one (OMP-418).

    - "create" if old is None
    - "narrow" if every set in new is a subset of old
    - "widen" otherwise
    """
    if old is None:
        return ChangeKind.create

    all_subsets = (
        new.goals.issubset(old.goals)
        and new.repositories.issubset(old.repositories)
        and new.capabilities.issubset(old.capabilities)
        and new.tier3_classes.issubset(old.tier3_classes)
    )
    if all_subsets:
        return ChangeKind.narrow

    return ChangeKind.widen
