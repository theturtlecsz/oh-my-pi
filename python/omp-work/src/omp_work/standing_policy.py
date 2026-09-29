"""Standing policy records, validation, coverage and change classification (OMP-418)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import fnmatch
from typing import Any
from uuid import UUID

from omp_work.action_tiers import TIER2
from omp_work.standing_change import ChangeKind

__all__ = [
    "CLASS_BOUNDS",
    "ActionRequest",
    "PolicyRefused",
    "RepositoryRecord",
    "StandingPolicy",
    "covers",
    "policy_change_kind",
    "validate_policy",
]

CLASS_BOUNDS: dict[str, tuple[str, ...]] = {
    "push_branch": ("repositories", "branch_patterns"),
    "create_pull_request": ("repositories", "branch_patterns"),
    "nonprod_update": ("destinations", "resource_types"),
    "network_access": ("destinations",),
    "spend_beyond_threshold": ("money_limit_usd",),
    "disposable_cloud": ("destinations", "resource_types", "money_limit_usd"),
}

_WILDCARDS = ("*", "?", "[")


@dataclass(frozen=True)
class RepositoryRecord:
    key: str
    default_branch: str
    protected_branches: tuple[str, ...] = ()
    automation_ci_secret_free: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.protected_branches, tuple):
            object.__setattr__(
                self, "protected_branches", tuple(self.protected_branches)
            )


@dataclass(frozen=True)
class ActionRequest:
    action_class: str
    repository: str | None = None
    branch: str | None = None
    destination: str | None = None
    resource_type: str | None = None
    amount_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.amount_usd is not None and not isinstance(self.amount_usd, Decimal):
            object.__setattr__(self, "amount_usd", Decimal(str(self.amount_usd)))


@dataclass(frozen=True)
class StandingPolicy:
    policy_id: UUID | str
    action_class: str
    repositories: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()
    branch_patterns: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    money_limit_usd: Decimal | None = None
    expires_at: datetime | None = None
    decision_id: UUID | str | None = None

    def __post_init__(self) -> None:
        for field in ("repositories", "destinations", "branch_patterns", "resource_types"):
            val = getattr(self, field)
            if val is not None and not isinstance(val, tuple):
                object.__setattr__(self, field, tuple(val))
            elif val is None:
                object.__setattr__(self, field, ())
        if self.money_limit_usd is not None and not isinstance(self.money_limit_usd, Decimal):
            object.__setattr__(self, "money_limit_usd", Decimal(str(self.money_limit_usd)))
        if isinstance(self.expires_at, str):
            object.__setattr__(self, "expires_at", datetime.fromisoformat(self.expires_at))


class PolicyRefused(Exception):
    """Raised when a standing policy fails validation."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _to_repo_map(repos: Any) -> dict[str, RepositoryRecord]:
    if isinstance(repos, Mapping):
        return dict(repos)
    if isinstance(repos, (list, tuple, set, frozenset)):
        return {r.key: r for r in repos if hasattr(r, "key")}
    return {}


def validate_policy(
    policy: StandingPolicy,
    repos: Any,
) -> None:
    """Validate a standing policy against tier 2 rules and repository constraints.

    Raises PolicyRefused(code) if validation fails.
    """
    if policy.action_class not in TIER2:
        raise PolicyRefused(
            "not_tier2", f"Action class {policy.action_class!r} is not tier 2"
        )

    if not policy.decision_id:
        raise PolicyRefused(
            "missing_decision", "Standing policy requires a decision_id"
        )

    required_bounds = CLASS_BOUNDS.get(policy.action_class, ())
    for bound in required_bounds:
        if bound == "money_limit_usd":
            if policy.money_limit_usd is None:
                raise PolicyRefused(
                    "missing_bound", f"Missing required bound: {bound}"
                )
        else:
            val = getattr(policy, bound, ())
            if not val or len(val) == 0:
                raise PolicyRefused(
                    "missing_bound", f"Missing required bound: {bound}"
                )

    if policy.money_limit_usd is not None and policy.money_limit_usd <= 0:
        raise PolicyRefused(
            "money_limit",
            f"Money limit must be positive, got {policy.money_limit_usd}",
        )

    for repo in policy.repositories:
        if any(ch in repo for ch in _WILDCARDS):
            raise PolicyRefused(
                "wildcard", f"Wildcard not permitted in repository: {repo!r}"
            )
    for dest in policy.destinations:
        if any(ch in dest for ch in _WILDCARDS):
            raise PolicyRefused(
                "wildcard", f"Wildcard not permitted in destination: {dest!r}"
            )

    repo_map = _to_repo_map(repos)
    for repo_key in policy.repositories:
        if repo_key not in repo_map:
            raise PolicyRefused(
                "unknown_repository", f"Unknown repository: {repo_key!r}"
            )

    for repo_key in policy.repositories:
        repo = repo_map[repo_key]
        for pattern in policy.branch_patterns:
            if fnmatch.fnmatch(repo.default_branch, pattern):
                raise PolicyRefused(
                    "protected_branch",
                    f"Branch pattern {pattern!r} matches default branch {repo.default_branch!r} in {repo_key}",
                )
            for prot in repo.protected_branches:
                if fnmatch.fnmatch(prot, pattern) or fnmatch.fnmatch(pattern, prot):
                    raise PolicyRefused(
                        "protected_branch",
                        f"Branch pattern {pattern!r} matches protected branch {prot!r} in {repo_key}",
                    )


def covers(
    policy: StandingPolicy,
    action: ActionRequest,
    repos: Any,
    now: datetime | None = None,
) -> bool:
    """Return whether a standing policy covers an action request."""
    if policy.action_class != action.action_class:
        return False

    if policy.expires_at is not None:
        if now is None:
            now = datetime.now(timezone.utc)
        exp = policy.expires_at
        if exp.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        elif exp.tzinfo is None and now.tzinfo is not None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now >= exp:
            return False

    required_bounds = CLASS_BOUNDS.get(policy.action_class)
    if required_bounds is None:
        return False

    repo_map = _to_repo_map(repos)

    for bound in required_bounds:
        if bound == "repositories":
            if not action.repository or action.repository not in policy.repositories:
                return False
            repo = repo_map.get(action.repository)
            if not repo:
                return False
            if policy.action_class == "push_branch" and not repo.automation_ci_secret_free:
                return False

        elif bound == "branch_patterns":
            if not action.branch:
                return False
            repo = repo_map.get(action.repository) if action.repository else None
            if repo:
                if action.branch == repo.default_branch:
                    return False
                if action.branch in repo.protected_branches:
                    return False
                if any(fnmatch.fnmatch(action.branch, prot) for prot in repo.protected_branches):
                    return False
            if not any(fnmatch.fnmatch(action.branch, pat) for pat in policy.branch_patterns):
                return False

        elif bound == "destinations":
            if not action.destination or action.destination not in policy.destinations:
                return False

        elif bound == "resource_types":
            if not action.resource_type or action.resource_type not in policy.resource_types:
                return False

        elif bound == "money_limit_usd":
            if action.amount_usd is None or policy.money_limit_usd is None:
                return False
            try:
                amt = Decimal(str(action.amount_usd))
            except Exception:
                return False
            if amt < 0 or amt > policy.money_limit_usd:
                return False

    return True


def policy_change_kind(
    old: StandingPolicy | None,
    new: StandingPolicy,
) -> ChangeKind:
    """Classify the kind of change between an old standing policy and a new one."""
    if old is None:
        return ChangeKind.create

    if old.action_class != new.action_class:
        return ChangeKind.widen

    bounds_equal = (
        set(old.repositories) == set(new.repositories)
        and set(old.destinations) == set(new.destinations)
        and set(old.branch_patterns) == set(new.branch_patterns)
        and set(old.resource_types) == set(new.resource_types)
        and old.money_limit_usd == new.money_limit_usd
    )

    expiry_extended = False
    if old.expires_at is not None:
        if new.expires_at is None:
            expiry_extended = True
        else:
            old_exp = old.expires_at
            new_exp = new.expires_at
            if old_exp.tzinfo is not None and new_exp.tzinfo is None:
                new_exp = new_exp.replace(tzinfo=timezone.utc)
            elif old_exp.tzinfo is None and new_exp.tzinfo is not None:
                old_exp = old_exp.replace(tzinfo=timezone.utc)
            if new_exp > old_exp:
                expiry_extended = True

    if bounds_equal and expiry_extended:
        return ChangeKind.extend

    bounds_subsets = (
        set(new.repositories).issubset(set(old.repositories))
        and set(new.destinations).issubset(set(old.destinations))
        and set(new.branch_patterns).issubset(set(old.branch_patterns))
        and set(new.resource_types).issubset(set(old.resource_types))
    )

    limit_not_higher = False
    if old.money_limit_usd is None:
        limit_not_higher = True
    else:
        if new.money_limit_usd is not None and new.money_limit_usd <= old.money_limit_usd:
            limit_not_higher = True

    expiry_not_higher = False
    if old.expires_at is None:
        expiry_not_higher = True
    else:
        if new.expires_at is not None:
            old_exp = old.expires_at
            new_exp = new.expires_at
            if old_exp.tzinfo is not None and new_exp.tzinfo is None:
                new_exp = new_exp.replace(tzinfo=timezone.utc)
            elif old_exp.tzinfo is None and new_exp.tzinfo is not None:
                old_exp = old_exp.replace(tzinfo=timezone.utc)
            if new_exp <= old_exp:
                expiry_not_higher = True

    if bounds_subsets and limit_not_higher and expiry_not_higher:
        return ChangeKind.narrow

    return ChangeKind.widen
