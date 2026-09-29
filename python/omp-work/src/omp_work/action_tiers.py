"""Action class tiers and classification under ADR 0005 (D35/D40)."""

from __future__ import annotations

__all__ = [
    "SPENDING_CLASSES",
    "TIER1",
    "TIER2",
    "TIER3",
    "tier_of",
]

TIER1: frozenset[str] = frozenset(
    {
        "read_state",
        "create_worktree",
        "modify_files",
        "run_checks",
        "commit_isolated_branch",
        "research",
        "update_internal_artifact",
        "retry_worker",
        "reroute_model",
        "pause_resume",
        "update_mission_state",
        "emit_event",
        "produce_candidate",
    }
)

TIER2: frozenset[str] = frozenset(
    {
        "push_branch",
        "create_pull_request",
        "nonprod_update",
        "spend_beyond_threshold",
        "network_access",
        "disposable_cloud",
    }
)

TIER3: frozenset[str] = frozenset(
    {
        "merge_protected_branch",
        "production_deploy",
        "destructive_infra",
        "credential_change",
        "delete_persistent_data",
        "billing_change",
        "publish_as_owner",
        "broaden_scope",
        "outside_secrets",
        "disable_safeguards",
    }
)

SPENDING_CLASSES: frozenset[str] = frozenset(
    {
        "spend_beyond_threshold",
        "disposable_cloud",
    }
)


def tier_of(action_class: str) -> int:
    """Return the safety tier (1, 2, or 3) for an action class id.

    Any unlisted class id returns 3 (D40).
    """
    if action_class in TIER1:
        return 1
    if action_class in TIER2:
        return 2
    return 3
