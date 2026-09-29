"""Tests for standing policy records, validation, covers, and change classification (OMP-418)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from omp_work.action_tiers import (
    SPENDING_CLASSES,
    TIER1,
    TIER2,
    TIER3,
    tier_of,
)
from omp_work.standing_change import ChangeKind
from omp_work.standing_policy import (
    ActionRequest,
    PolicyRefused,
    RepositoryRecord,
    StandingPolicy,
    covers,
    policy_change_kind,
    validate_policy,
)


def test_action_tiers() -> None:
    for c in TIER1:
        assert tier_of(c) == 1
    for c in TIER2:
        assert tier_of(c) == 2
    for c in TIER3:
        assert tier_of(c) == 3
    assert tier_of("completely_unknown_action") == 3
    assert SPENDING_CLASSES == frozenset({"spend_beyond_threshold", "disposable_cloud"})



@pytest.fixture
def repos() -> dict[str, RepositoryRecord]:
    return {
        "repo-alpha": RepositoryRecord(
            key="repo-alpha",
            default_branch="main",
            protected_branches=("release/*", "prod"),
            automation_ci_secret_free=True,
        ),
        "repo-beta": RepositoryRecord(
            key="repo-beta",
            default_branch="master",
            protected_branches=("stable",),
            automation_ci_secret_free=False,
        ),
    }


# ==============================================================================
# validate_policy tests (each refusal code, including push with no branch pattern)
# ==============================================================================


def test_validate_policy_refused_not_tier2(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="read_state",  # Tier 1
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p, repos)
    assert exc_info.value.code == "not_tier2"

    p_t3 = StandingPolicy(
        policy_id="pol-2",
        action_class="production_deploy",  # Tier 3
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_t3, repos)
    assert exc_info.value.code == "not_tier2"

    p_unlisted = StandingPolicy(
        policy_id="pol-3",
        action_class="unknown_action_xyz",  # Unlisted -> Tier 3
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_unlisted, repos)
    assert exc_info.value.code == "not_tier2"


def test_validate_policy_refused_missing_decision(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.example.com",),
        decision_id=None,
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p, repos)
    assert exc_info.value.code == "missing_decision"


def test_validate_policy_refused_missing_bound_push_no_branch_pattern(
    repos: dict[str, RepositoryRecord]
) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=(),  # empty!
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p, repos)
    assert exc_info.value.code == "missing_bound"


def test_validate_policy_refused_missing_bound_other_classes(
    repos: dict[str, RepositoryRecord]
) -> None:
    # push_branch missing repositories
    p_push_no_repo = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=(),
        branch_patterns=("topic/*",),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_push_no_repo, repos)
    assert exc_info.value.code == "missing_bound"

    # nonprod_update missing resource_types
    p_nonprod = StandingPolicy(
        policy_id="pol-2",
        action_class="nonprod_update",
        destinations=("dev-env",),
        resource_types=(),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_nonprod, repos)
    assert exc_info.value.code == "missing_bound"

    # network_access missing destinations
    p_net = StandingPolicy(
        policy_id="pol-3",
        action_class="network_access",
        destinations=(),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_net, repos)
    assert exc_info.value.code == "missing_bound"

    # spend_beyond_threshold missing money_limit_usd
    p_spend = StandingPolicy(
        policy_id="pol-4",
        action_class="spend_beyond_threshold",
        money_limit_usd=None,
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_spend, repos)
    assert exc_info.value.code == "missing_bound"

    # disposable_cloud missing required bounds
    p_cloud = StandingPolicy(
        policy_id="pol-5",
        action_class="disposable_cloud",
        destinations=("aws-sandbox",),
        resource_types=(),
        money_limit_usd=Decimal("50.00"),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_cloud, repos)
    assert exc_info.value.code == "missing_bound"


def test_validate_policy_refused_money_limit(repos: dict[str, RepositoryRecord]) -> None:
    p_zero = StandingPolicy(
        policy_id="pol-1",
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal("0.00"),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_zero, repos)
    assert exc_info.value.code == "money_limit"

    p_neg = StandingPolicy(
        policy_id="pol-2",
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal("-10.00"),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_neg, repos)
    assert exc_info.value.code == "money_limit"


def test_validate_policy_refused_wildcard(repos: dict[str, RepositoryRecord]) -> None:
    # Wildcard in repository (* ? [)
    for wildcard_repo in ["repo-*", "repo-?", "repo-[a-z]"]:
        p = StandingPolicy(
            policy_id="pol-1",
            action_class="push_branch",
            repositories=(wildcard_repo,),
            branch_patterns=("topic/*",),
            decision_id="dec-1",
        )
        with pytest.raises(PolicyRefused) as exc_info:
            validate_policy(p, repos)
        assert exc_info.value.code == "wildcard"

    # Wildcard in destination (* ? [)
    for wildcard_dest in ["https://*.example.com", "https://api?.example.com", "http://[a]"]:
        p = StandingPolicy(
            policy_id="pol-2",
            action_class="network_access",
            destinations=(wildcard_dest,),
            decision_id="dec-1",
        )
        with pytest.raises(PolicyRefused) as exc_info:
            validate_policy(p, repos)
        assert exc_info.value.code == "wildcard"


def test_validate_policy_refused_unknown_repository(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("unknown-repo",),
        branch_patterns=("topic/*",),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p, repos)
    assert exc_info.value.code == "unknown_repository"


def test_validate_policy_refused_protected_branch(repos: dict[str, RepositoryRecord]) -> None:
    # Matches default branch "main" in repo-alpha
    p_default = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("main",),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_default, repos)
    assert exc_info.value.code == "protected_branch"

    p_star = StandingPolicy(
        policy_id="pol-2",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("*",),  # Matches "main"
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_star, repos)
    assert exc_info.value.code == "protected_branch"

    # Matches protected branch "release/*" in repo-alpha
    p_release = StandingPolicy(
        policy_id="pol-3",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("release/1.0",),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_release, repos)
    assert exc_info.value.code == "protected_branch"

    # Matches protected branch "prod" in repo-alpha
    p_prod = StandingPolicy(
        policy_id="pol-4",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("prod",),
        decision_id="dec-1",
    )
    with pytest.raises(PolicyRefused) as exc_info:
        validate_policy(p_prod, repos)
    assert exc_info.value.code == "protected_branch"


def test_validate_policy_valid(repos: dict[str, RepositoryRecord]) -> None:
    p_push = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("feature/*", "bugfix/*"),
        decision_id="dec-1",
    )
    validate_policy(p_push, repos)

    p_net = StandingPolicy(
        policy_id="pol-2",
        action_class="network_access",
        destinations=("https://api.github.com",),
        decision_id="dec-2",
    )
    validate_policy(p_net, repos)


# ==============================================================================
# covers tests (covers per bound, CI-secret push, expiry)
# ==============================================================================


def test_covers_different_class(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        decision_id="dec-1",
    )
    action = ActionRequest(
        action_class="spend_beyond_threshold",
        amount_usd=Decimal("10.00"),
    )
    assert covers(p, action, repos) is False


def test_covers_expiry(repos: dict[str, RepositoryRecord]) -> None:
    p_exp = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        expires_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    action = ActionRequest(
        action_class="network_access",
        destination="https://api.github.com",
    )

    before = datetime(2026, 6, 1, 11, 59, tzinfo=timezone.utc)
    exact = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    after = datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)

    assert covers(p_exp, action, repos, now=before) is True
    assert covers(p_exp, action, repos, now=exact) is False
    assert covers(p_exp, action, repos, now=after) is False

    # No expiry
    p_no_exp = StandingPolicy(
        policy_id="pol-2",
        action_class="network_access",
        destinations=("https://api.github.com",),
        expires_at=None,
        decision_id="dec-1",
    )
    assert covers(p_no_exp, action, repos, now=after) is True


def test_covers_push_branch_bounds_and_ci_secret(repos: dict[str, RepositoryRecord]) -> None:
    # repo-alpha has automation_ci_secret_free=True
    # repo-beta has automation_ci_secret_free=False
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*",),
        decision_id="dec-1",
    )

    # Valid push to repo-alpha (CI-secret-free is True)
    action_ok = ActionRequest(
        action_class="push_branch",
        repository="repo-alpha",
        branch="feat/test-1",
    )
    assert covers(p, action_ok, repos) is True

    # CI-secret push refused on repo-beta (automation_ci_secret_free is False)
    action_ci_secret = ActionRequest(
        action_class="push_branch",
        repository="repo-beta",
        branch="feat/test-1",
    )
    assert covers(p, action_ci_secret, repos) is False

    # Attempt push to default branch
    action_default = ActionRequest(
        action_class="push_branch",
        repository="repo-alpha",
        branch="main",
    )
    assert covers(p, action_default, repos) is False

    # Attempt push to protected branch
    action_prot = ActionRequest(
        action_class="push_branch",
        repository="repo-alpha",
        branch="release/2.0",
    )
    assert covers(p, action_prot, repos) is False

    # Branch does not match pattern
    action_unmatched = ActionRequest(
        action_class="push_branch",
        repository="repo-alpha",
        branch="hotfix/patch",
    )
    assert covers(p, action_unmatched, repos) is False

    # Missing repository or branch
    assert covers(p, ActionRequest(action_class="push_branch", repository=None, branch="feat/1"), repos) is False
    assert covers(p, ActionRequest(action_class="push_branch", repository="repo-alpha", branch=None), repos) is False


def test_covers_create_pull_request(repos: dict[str, RepositoryRecord]) -> None:
    # create_pull_request does NOT require automation_ci_secret_free
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="create_pull_request",
        repositories=("repo-beta",),
        branch_patterns=("feat/*",),
        decision_id="dec-1",
    )

    action_ok = ActionRequest(
        action_class="create_pull_request",
        repository="repo-beta",
        branch="feat/pr-1",
    )
    assert covers(p, action_ok, repos) is True

    # Default branch rejected
    action_default = ActionRequest(
        action_class="create_pull_request",
        repository="repo-beta",
        branch="master",
    )
    assert covers(p, action_default, repos) is False


def test_covers_nonprod_update(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="nonprod_update",
        destinations=("staging",),
        resource_types=("lambda", "s3"),
        decision_id="dec-1",
    )
    assert covers(
        p,
        ActionRequest(action_class="nonprod_update", destination="staging", resource_type="lambda"),
        repos,
    ) is True
    assert covers(
        p,
        ActionRequest(action_class="nonprod_update", destination="prod", resource_type="lambda"),
        repos,
    ) is False
    assert covers(
        p,
        ActionRequest(action_class="nonprod_update", destination="staging", resource_type="rds"),
        repos,
    ) is False
    assert covers(
        p,
        ActionRequest(action_class="nonprod_update", destination=None, resource_type="lambda"),
        repos,
    ) is False


def test_covers_network_access(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        decision_id="dec-1",
    )
    assert covers(
        p,
        ActionRequest(action_class="network_access", destination="https://api.github.com"),
        repos,
    ) is True
    assert covers(
        p,
        ActionRequest(action_class="network_access", destination="https://evil.com"),
        repos,
    ) is False
    assert covers(
        p,
        ActionRequest(action_class="network_access", destination=None),
        repos,
    ) is False


def test_covers_spend_beyond_threshold(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal("100.00"),
        decision_id="dec-1",
    )
    assert covers(
        p,
        ActionRequest(action_class="spend_beyond_threshold", amount_usd=Decimal("50.00")),
        repos,
    ) is True
    assert covers(
        p,
        ActionRequest(action_class="spend_beyond_threshold", amount_usd=Decimal("100.00")),
        repos,
    ) is True
    assert covers(
        p,
        ActionRequest(action_class="spend_beyond_threshold", amount_usd=Decimal("100.01")),
        repos,
    ) is False
    assert covers(
        p,
        ActionRequest(action_class="spend_beyond_threshold", amount_usd=None),
        repos,
    ) is False


def test_covers_disposable_cloud(repos: dict[str, RepositoryRecord]) -> None:
    p = StandingPolicy(
        policy_id="pol-1",
        action_class="disposable_cloud",
        destinations=("sandbox",),
        resource_types=("ec2",),
        money_limit_usd=Decimal("20.00"),
        decision_id="dec-1",
    )
    assert covers(
        p,
        ActionRequest(
            action_class="disposable_cloud",
            destination="sandbox",
            resource_type="ec2",
            amount_usd=Decimal("15.00"),
        ),
        repos,
    ) is True
    assert covers(
        p,
        ActionRequest(
            action_class="disposable_cloud",
            destination="sandbox",
            resource_type="ec2",
            amount_usd=Decimal("25.00"),
        ),
        repos,
    ) is False


# ==============================================================================
# policy_change_kind tests (each change kind: create, narrow, extend, widen)
# ==============================================================================


def test_policy_change_kind_create() -> None:
    new_pol = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        decision_id="dec-1",
    )
    assert policy_change_kind(None, new_pol) == ChangeKind.create


def test_policy_change_kind_narrow() -> None:
    old_pol = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*", "fix/*"),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        decision_id="dec-1",
    )

    # Subsets of bounds
    new_narrow_repos = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),  # narrowed
        branch_patterns=("feat/*", "fix/*"),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_narrow_repos) == ChangeKind.narrow

    # Lower money limit
    new_narrow_limit = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*", "fix/*"),
        money_limit_usd=Decimal("50.00"),  # lowered
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_narrow_limit) == ChangeKind.narrow

    # Earlier expiry
    new_narrow_expiry = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*", "fix/*"),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 11, 30, tzinfo=timezone.utc),  # earlier
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_narrow_expiry) == ChangeKind.narrow

    # Identical policy is narrow
    assert policy_change_kind(old_pol, old_pol) == ChangeKind.narrow


def test_policy_change_kind_extend() -> None:
    old_pol = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        decision_id="dec-1",
    )

    # Later expiry
    new_later = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_later) == ChangeKind.extend

    # Removed expiry
    new_removed = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        expires_at=None,
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_removed) == ChangeKind.extend


def test_policy_change_kind_widen() -> None:
    old_pol = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("feat/*",),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        decision_id="dec-1",
    )

    # Different class
    new_diff_class = StandingPolicy(
        policy_id="pol-1",
        action_class="network_access",
        destinations=("https://api.github.com",),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_diff_class) == ChangeKind.widen

    # Add repository
    new_add_repo = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*",),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_add_repo) == ChangeKind.widen

    # Higher money limit
    new_higher_limit = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("feat/*",),
        money_limit_usd=Decimal("200.00"),
        expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_higher_limit) == ChangeKind.widen

    # Later expiry BUT bounds also widened -> widen, not extend
    new_widen_and_later_exp = StandingPolicy(
        policy_id="pol-1",
        action_class="push_branch",
        repositories=("repo-alpha", "repo-beta"),
        branch_patterns=("feat/*",),
        money_limit_usd=Decimal("100.00"),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        decision_id="dec-1",
    )
    assert policy_change_kind(old_pol, new_widen_and_later_exp) == ChangeKind.widen
