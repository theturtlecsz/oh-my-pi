"""Tests for pure action classification (OMP-403)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from omp_work.action_classify import (
    Classification,
    Operation,
    ResolvedTarget,
    classify,
    parse_submission,
)
from omp_work.action_tiers import TIER2, TIER3, tier_of
from omp_work.standing_policy import RepositoryRecord


def make_repo() -> RepositoryRecord:
    return RepositoryRecord(
        key="repo-alpha",
        default_branch="main",
        protected_branches=("release/*", "prod"),
        automation_ci_secret_free=True,
    )


# ==============================================================================
# parse_submission keeps operation fields and drops submitted claims.
# ==============================================================================


def test_parse_submission_keeps_operation_fields() -> None:
    op = parse_submission(
        {
            "kind": "git_push",
            "repository": "repo-alpha",
            "branch": "feature/x",
            "base_branch": "main",
            "commit": "abc123",
            "force": True,
            "environment": "staging",
            "destination": "host:5432",
            "resource_id": "res-1",
            "resource_type": "database",
            "amount_usd": "12.50",
        }
    )
    assert op.kind == "git_push"
    assert op.repository == "repo-alpha"
    assert op.branch == "feature/x"
    assert op.base_branch == "main"
    assert op.commit == "abc123"
    assert op.force is True
    assert op.environment == "staging"
    assert op.destination == "host:5432"
    assert op.resource_id == "res-1"
    assert op.resource_type == "database"
    assert op.amount_usd == Decimal("12.50")


def test_parse_submission_drops_claimed_classification() -> None:
    op = parse_submission(
        {
            "kind": "delete_branch",
            "branch": "main",
            "tier": 1,
            "label": "routine",
            "description": "totally safe",
            "action_class": "read_state",
        }
    )
    assert op == Operation(kind="delete_branch", branch="main")


def test_parse_submission_requires_kind() -> None:
    with pytest.raises(ValueError):
        parse_submission({"tier": 1})


# ==============================================================================
# TIER1 ids and same-class kinds
# ==============================================================================


def test_tier1_ids_stay_tier1() -> None:
    for kind in ("read_state", "modify_files", "run_checks", "commit_isolated_branch"):
        result = classify(Operation(kind=kind), ResolvedTarget())
        assert result.action_class == kind
        assert result.tier == 1


def test_same_class_kinds() -> None:
    for kind, expected in (
        ("create_pull_request", "create_pull_request"),
        ("network_access", "network_access"),
        ("billing_change", "billing_change"),
    ):
        result = classify(Operation(kind=kind), ResolvedTarget())
        assert result.action_class == expected
        assert result.tier == tier_of(expected)


# ==============================================================================
# Every tier 2 and tier 3 class is reachable.
# ==============================================================================


def test_every_tier2_and_tier3_class_reachable() -> None:
    reachable: dict[str, Classification] = {}

    def note(op: Operation, resolved: ResolvedTarget) -> None:
        result = classify(op, resolved)
        reachable[result.action_class] = result

    repo = make_repo()
    note(
        Operation(kind="git_push", repository="repo-alpha", branch="feature/x"),
        ResolvedTarget(repository=repo),
    )
    note(Operation(kind="create_pull_request", repository="repo-alpha"), ResolvedTarget(repository=repo))
    note(Operation(kind="deploy", environment="staging"), ResolvedTarget())
    note(Operation(kind="spend", amount_usd="5"), ResolvedTarget())
    note(Operation(kind="network_access"), ResolvedTarget())
    note(Operation(kind="cloud_create", resource_type="vm"), ResolvedTarget(disposable=True))
    note(
        Operation(kind="merge", repository="repo-alpha", branch="main"),
        ResolvedTarget(repository=repo),
    )
    note(Operation(kind="deploy", environment="production"), ResolvedTarget())
    note(Operation(kind="infra_destroy"), ResolvedTarget())
    note(Operation(kind="credential_change"), ResolvedTarget())
    note(Operation(kind="delete_data"), ResolvedTarget())
    note(Operation(kind="publish"), ResolvedTarget())
    note(Operation(kind="scope_broaden"), ResolvedTarget())
    note(Operation(kind="secret_read"), ResolvedTarget(secret_in_envelope=False))
    note(Operation(kind="safeguard_change"), ResolvedTarget())
    note(Operation(kind="billing_change"), ResolvedTarget())

    assert TIER2.issubset(reachable)
    assert TIER3.issubset(reachable)
    for action_class, result in reachable.items():
        assert result.tier == tier_of(action_class)


def test_unknown_kind_is_unlisted_tier3() -> None:
    result = classify(Operation(kind="brand_new_thing"), ResolvedTarget())
    assert result.action_class == "unlisted"
    assert result.tier == 3


# ==============================================================================
# git push / merge / delete branch
# ==============================================================================


def test_merge_to_main_labelled_tier1_is_tier3() -> None:
    op = parse_submission(
        {"kind": "merge", "repository": "repo-alpha", "branch": "main", "tier": 1}
    )
    result = classify(op, ResolvedTarget(repository=make_repo()))
    assert result.action_class == "merge_protected_branch"
    assert result.tier == 3


def test_push_to_protected_pattern_and_forced() -> None:
    repo = make_repo()
    for branch in ("release/1.2", "prod"):
        result = classify(
            Operation(kind="git_push", repository="repo-alpha", branch=branch),
            ResolvedTarget(repository=repo),
        )
        assert result.action_class == "merge_protected_branch"
        assert result.tier == 3
    forced = classify(
        Operation(kind="git_push", repository="repo-alpha", branch="feature/x", force=True),
        ResolvedTarget(repository=repo),
    )
    assert forced.action_class == "merge_protected_branch"
    assert forced.tier == 3


def test_plain_feature_push_is_push_branch() -> None:
    result = classify(
        Operation(kind="git_push", repository="repo-alpha", branch="feature/x"),
        ResolvedTarget(repository=make_repo()),
    )
    assert result.action_class == "push_branch"
    assert result.tier == 2


def test_delete_feature_branch_is_push_branch() -> None:
    result = classify(
        Operation(kind="delete_branch", repository="repo-alpha", branch="feature/x"),
        ResolvedTarget(repository=make_repo()),
    )
    assert result.action_class == "push_branch"


# ==============================================================================
# Safeguard paths
# ==============================================================================


def test_feature_push_touching_workflows_is_disable_safeguards() -> None:
    result = classify(
        Operation(kind="git_push", repository="repo-alpha", branch="feature/x"),
        ResolvedTarget(repository=make_repo(), changed_paths=(".github/workflows/ci.yml",)),
    )
    assert result.action_class == "disable_safeguards"
    assert result.tier == 3


def test_protected_branch_wins_over_safeguard_path() -> None:
    result = classify(
        Operation(kind="git_push", repository="repo-alpha", branch="main"),
        ResolvedTarget(repository=make_repo(), changed_paths=(".github/workflows/ci.yml",)),
    )
    assert result.action_class == "merge_protected_branch"
    assert result.tier == 3


def test_delete_branch_ignores_changed_paths() -> None:
    result = classify(
        Operation(kind="delete_branch", repository="repo-alpha", branch="feature/x"),
        ResolvedTarget(repository=make_repo(), changed_paths=(".github/workflows/ci.yml",)),
    )
    assert result.action_class == "push_branch"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/build/action.yml",
        ".github/rulesets/main.json",
        ".github/CODEOWNERS",
        "CODEOWNERS",
        ".gitlab-ci.yml",
    ],
)
def test_safeguard_path_variants(path: str) -> None:
    result = classify(
        Operation(kind="create_pull_request", repository="repo-alpha"),
        ResolvedTarget(repository=make_repo(), changed_paths=(path,)),
    )
    assert result.action_class == "disable_safeguards"
    assert result.tier == 3


# ==============================================================================
# external_update / deploy / spend / cloud
# ==============================================================================


@pytest.mark.parametrize("environment", ["staging", "dev", "sandbox", "ci"])
def test_external_update_nonprod(environment: str) -> None:
    result = classify(Operation(kind="external_update", environment=environment), ResolvedTarget())
    assert result.action_class == "nonprod_update"
    assert result.tier == 2


@pytest.mark.parametrize("environment", ["production", None, "prod-east-unknown"])
def test_external_update_production_or_unknown(environment: str | None) -> None:
    result = classify(Operation(kind="external_update", environment=environment), ResolvedTarget())
    assert result.action_class == "production_deploy"
    assert result.tier == 3


def test_spend_is_spend_beyond_threshold() -> None:
    result = classify(Operation(kind="spend", amount_usd="999.99"), ResolvedTarget())
    assert result.action_class == "spend_beyond_threshold"
    assert result.tier == 2


def test_cloud_create_is_disposable_cloud() -> None:
    result = classify(Operation(kind="cloud_create", resource_type="vm"), ResolvedTarget())
    assert result.action_class == "disposable_cloud"
    assert result.tier == 2


def test_cloud_delete_data_inside_is_persistent() -> None:
    result = classify(
        Operation(kind="cloud_delete", resource_id="db-1"),
        ResolvedTarget(data_inside=True, disposable=True),
    )
    assert result.action_class == "delete_persistent_data"
    assert result.tier == 3


def test_cloud_delete_disposable_is_disposable_cloud() -> None:
    result = classify(
        Operation(kind="cloud_delete", resource_id="vm-1"),
        ResolvedTarget(disposable=True),
    )
    assert result.action_class == "disposable_cloud"
    assert result.tier == 2


def test_cloud_delete_otherwise_destructive_infra() -> None:
    result = classify(
        Operation(kind="cloud_delete", resource_id="vm-1"),
        ResolvedTarget(disposable=False, data_inside=False),
    )
    assert result.action_class == "destructive_infra"
    assert result.tier == 3


# ==============================================================================
# secret_read
# ==============================================================================


def test_secret_read_inside_envelope_is_read_state() -> None:
    result = classify(Operation(kind="secret_read"), ResolvedTarget(secret_in_envelope=True))
    assert result.action_class == "read_state"
    assert result.tier == 1


def test_secret_read_outside_envelope_is_outside_secrets() -> None:
    result = classify(Operation(kind="secret_read"), ResolvedTarget(secret_in_envelope=False))
    assert result.action_class == "outside_secrets"
    assert result.tier == 3


# ==============================================================================
# target digest
# ==============================================================================


def test_digest_is_deterministic() -> None:
    op = Operation(kind="git_push", repository="repo-alpha", branch="feature/x")
    resolved = ResolvedTarget(repository=make_repo(), commit="c0ffee", changed_paths=("b.py", "a.py"))
    assert classify(op, resolved).target_sha256 == classify(op, resolved).target_sha256


def test_digest_varies_by_commit() -> None:
    op = Operation(kind="git_push", repository="repo-alpha", branch="feature/x")
    first = classify(op, ResolvedTarget(repository=make_repo(), commit="1111"))
    second = classify(op, ResolvedTarget(repository=make_repo(), commit="2222"))
    assert first.target_sha256 != second.target_sha256


def test_digest_varies_by_branch() -> None:
    resolved = ResolvedTarget(repository=make_repo(), commit="abc")
    first = classify(Operation(kind="git_push", repository="repo-alpha", branch="feature/x"), resolved)
    second = classify(Operation(kind="git_push", repository="repo-alpha", branch="feature/y"), resolved)
    assert first.target_sha256 != second.target_sha256


def test_digest_varies_by_paths() -> None:
    op = Operation(kind="git_push", repository="repo-alpha", branch="feature/x")
    first = classify(op, ResolvedTarget(repository=make_repo(), changed_paths=("a.py",)))
    second = classify(op, ResolvedTarget(repository=make_repo(), changed_paths=("a.py", "b.py")))
    assert first.target_sha256 != second.target_sha256


def test_digest_ignores_path_order() -> None:
    op = Operation(kind="git_push", repository="repo-alpha", branch="feature/x")
    first = classify(
        op, ResolvedTarget(repository=make_repo(), changed_paths=("a.py", "b.py"))
    )
    second = classify(
        op, ResolvedTarget(repository=make_repo(), changed_paths=("b.py", "a.py"))
    )
    assert first.target_sha256 == second.target_sha256


def test_digest_is_hex_sha256() -> None:
    digest = classify(Operation(kind="read_state"), ResolvedTarget()).target_sha256
    assert len(digest) == 64
    assert digest == digest.lower()
