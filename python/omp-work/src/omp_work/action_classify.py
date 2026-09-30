"""Pure action classification from an operation and its resolved target (OMP-403).

Classification depends only on the submitted ``Operation`` and the
``ResolvedTarget`` the control plane resolved for it. A submission's own
``tier``/``label``/``description``/``action_class`` claims are dropped by
``parse_submission`` and never influence the outcome (D40: an action no tier
lists is tier 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import fnmatch
import hashlib
import json
from typing import Any

from omp_work.action_tiers import TIER1, tier_of
from omp_work.standing_policy import RepositoryRecord

__all__ = [
    "NONPROD_ENVIRONMENTS",
    "SAFEGUARD_PATTERNS",
    "Classification",
    "Operation",
    "ResolvedTarget",
    "classify",
    "parse_submission",
]

# Environments explicitly outside production. Everything else (including an
# absent/unknown environment) is treated as production (fail closed).
NONPROD_ENVIRONMENTS: frozenset[str] = frozenset(
    {
        "dev",
        "development",
        "test",
        "testing",
        "qa",
        "stage",
        "staging",
        "sandbox",
        "preview",
        "local",
        "ci",
        "nonprod",
        "non-production",
        "non_prod",
    }
)

# Paths whose change disables or bypasses a safeguard.
SAFEGUARD_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    ".github/actions/**",
    ".github/rulesets/**",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    ".gitlab-ci.yml",
)

_OPERATION_FIELDS: tuple[str, ...] = (
    "kind",
    "repository",
    "branch",
    "base_branch",
    "commit",
    "force",
    "environment",
    "destination",
    "resource_id",
    "resource_type",
    "amount_usd",
)


@dataclass(frozen=True)
class Operation:
    kind: str
    repository: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit: str | None = None
    force: bool = False
    environment: str | None = None
    destination: str | None = None
    resource_id: str | None = None
    resource_type: str | None = None
    amount_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.amount_usd is not None and not isinstance(self.amount_usd, Decimal):
            object.__setattr__(self, "amount_usd", Decimal(str(self.amount_usd)))


@dataclass(frozen=True)
class ResolvedTarget:
    repository: RepositoryRecord | None = None
    commit: str | None = None
    changed_paths: tuple[str, ...] = ()
    disposable: bool = False
    data_inside: bool = False
    secret_in_envelope: bool = False

    def __post_init__(self) -> None:
        if self.changed_paths is None:
            object.__setattr__(self, "changed_paths", ())
        elif not isinstance(self.changed_paths, tuple):
            object.__setattr__(self, "changed_paths", tuple(self.changed_paths))


@dataclass(frozen=True)
class Classification:
    action_class: str
    tier: int
    target_sha256: str


def parse_submission(mapping: Mapping[str, Any]) -> Operation:
    """Build an Operation from a submission mapping, dropping every other key."""
    if not isinstance(mapping, Mapping):
        raise TypeError(f"submission must be a mapping, got {type(mapping).__name__}")
    kind = mapping.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("submission must carry a non-empty 'kind'")
    kwargs = {name: mapping[name] for name in _OPERATION_FIELDS if name in mapping}
    return Operation(**kwargs)


def _touches_safeguards(paths: tuple[str, ...]) -> bool:
    for path in paths:
        for pattern in SAFEGUARD_PATTERNS:
            if fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def _touches_protected(op: Operation, resolved: ResolvedTarget) -> bool:
    repo = resolved.repository
    if repo is None:
        return False
    names = [name for name in (op.branch, op.base_branch) if name]
    if not names:
        return False
    patterns = (repo.default_branch, *repo.protected_branches)
    for name in names:
        for pattern in patterns:
            if fnmatch.fnmatchcase(name, pattern):
                return True
    return False


def _classify_action_class(op: Operation, resolved: ResolvedTarget) -> str:
    kind = op.kind
    paths = resolved.changed_paths

    if kind in TIER1:
        return kind

    if kind == "create_pull_request":
        if _touches_safeguards(paths):
            return "disable_safeguards"
        return "create_pull_request"

    if kind == "network_access" or kind == "billing_change":
        return kind

    if kind in ("git_push", "merge", "delete_branch"):
        # A protected/default target or a forced update escalates first; any
        # remaining push/merge that rewrites a safeguard path escalates too.
        if op.force or _touches_protected(op, resolved):
            return "merge_protected_branch"
        if kind in ("git_push", "merge") and _touches_safeguards(paths):
            return "disable_safeguards"
        return "push_branch"

    if kind in ("external_update", "deploy"):
        environment = (op.environment or "").strip().lower()
        if environment in NONPROD_ENVIRONMENTS:
            return "nonprod_update"
        return "production_deploy"

    if kind == "spend":
        return "spend_beyond_threshold"
    if kind == "cloud_create":
        return "disposable_cloud"
    if kind == "cloud_delete":
        if resolved.data_inside:
            return "delete_persistent_data"
        if resolved.disposable:
            return "disposable_cloud"
        return "destructive_infra"
    if kind == "secret_read":
        if resolved.secret_in_envelope:
            return "read_state"
        return "outside_secrets"
    if kind == "infra_destroy":
        return "destructive_infra"
    if kind in ("credential_change", "access_grant_change"):
        return "credential_change"
    if kind == "delete_data":
        return "delete_persistent_data"
    if kind == "publish":
        return "publish_as_owner"
    if kind == "scope_broaden":
        return "broaden_scope"
    if kind in ("safeguard_change", "budget_ceiling_raise"):
        return "disable_safeguards"
    return "unlisted"


def _operation_payload(op: Operation) -> dict[str, Any]:
    return {
        "kind": op.kind,
        "repository": op.repository,
        "branch": op.branch,
        "base_branch": op.base_branch,
        "commit": op.commit,
        "force": bool(op.force),
        "environment": op.environment,
        "destination": op.destination,
        "resource_id": op.resource_id,
        "resource_type": op.resource_type,
        "amount_usd": None if op.amount_usd is None else str(op.amount_usd),
    }


def _target_sha256(op: Operation, action_class: str, resolved: ResolvedTarget) -> str:
    payload = {
        "operation": _operation_payload(op),
        "action_class": action_class,
        "commit": resolved.commit,
        "changed_paths": sorted(resolved.changed_paths),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify(op: Operation, resolved: ResolvedTarget) -> Classification:
    """Classify an operation against its resolved target into an action class."""
    action_class = _classify_action_class(op, resolved)
    return Classification(
        action_class=action_class,
        tier=tier_of(action_class),
        target_sha256=_target_sha256(op, action_class, resolved),
    )
