"""Tests for standing mandate records, scope verification, tier 3 handling, and change classification (OMP-418)."""

from __future__ import annotations

from decimal import Decimal
import pytest

from omp_work.standing_change import ChangeKind
from omp_work.standing_mandate import (
    MandateRefused,
    MissionScopeDraft,
    ScopeVerdict,
    StandingMandate,
    encounter_tier3,
    mandate_change_kind,
    mission_scope,
    validate_mandate,
)


@pytest.fixture
def base_mandate() -> StandingMandate:
    return StandingMandate(
        mandate_id="mandate-001",
        goals=frozenset({"build_feature", "fix_bugs"}),
        repositories=frozenset({"repo-alpha", "repo-beta"}),
        capabilities=frozenset({"git_read", "test_runner"}),
        tier3_classes=frozenset({"merge_protected_branch"}),
        decision_id="dec-418",
    )


# ==============================================================================
# 1. Mission scope: inside draft approved with mandate and decision ids
# ==============================================================================


def test_inside_draft_approved_with_mandate_and_decision_ids(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"build_feature"},
        repositories={"repo-alpha"},
        capabilities={"git_read"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=Decimal("25.00"),
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "approved"
    assert verdict.basis_mandate_id == "mandate-001"
    assert verdict.basis_decision_id == "dec-418"
    assert verdict.reasons == ()


def test_no_ceiling_draft_with_standing_ceiling_approved_inherits(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-beta"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=None,
        budget_threshold_usd=Decimal("30.00"),
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "approved"
    assert verdict.basis_mandate_id == "mandate-001"
    assert verdict.basis_decision_id == "dec-418"
    assert verdict.reasons == ()


# ==============================================================================
# 2. Each reason -> awaiting_confirmation with it
# ==============================================================================


def test_awaiting_confirmation_reason_no_mandate() -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=None,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert verdict.basis_mandate_id is None
    assert verdict.basis_decision_id is None
    assert "no_mandate" in verdict.reasons


def test_awaiting_confirmation_reason_no_goals(base_mandate: StandingMandate) -> None:
    draft = MissionScopeDraft(
        goals=frozenset(),
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "no_goals" in verdict.reasons


def test_awaiting_confirmation_reason_goal_outside_mandate(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"unauthorized_goal"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "goal_outside_mandate" in verdict.reasons


def test_awaiting_confirmation_reason_repository_outside_mandate(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"foreign-repo"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "repository_outside_mandate" in verdict.reasons


def test_awaiting_confirmation_reason_capability_outside_mandate(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"arbitrary_network"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "capability_outside_mandate" in verdict.reasons


def test_awaiting_confirmation_reason_tier3_class_outside_mandate(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes={"delete_persistent_data"},
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "tier3_class_outside_mandate" in verdict.reasons


def test_awaiting_confirmation_reason_no_budget(base_mandate: StandingMandate) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=None,
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=None,
    )

    assert verdict.status == "awaiting_confirmation"
    assert "no_budget" in verdict.reasons


def test_awaiting_confirmation_reason_no_standing_budget(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("50.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=None,
    )

    assert verdict.status == "awaiting_confirmation"
    assert "no_standing_budget" in verdict.reasons


def test_awaiting_confirmation_reason_budget_over_standing(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("150.00"),
        budget_threshold_usd=None,
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "budget_over_standing" in verdict.reasons


def test_awaiting_confirmation_reason_threshold_over_ceiling(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=Decimal("80.00"),
        budget_threshold_usd=Decimal("90.00"),
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "threshold_over_ceiling" in verdict.reasons


def test_awaiting_confirmation_reason_threshold_over_standing_ceiling_inheriting(
    base_mandate: StandingMandate,
) -> None:
    draft = MissionScopeDraft(
        goals={"fix_bugs"},
        repositories={"repo-alpha"},
        capabilities={"test_runner"},
        tier3_classes=frozenset(),
        budget_ceiling_usd=None,
        budget_threshold_usd=Decimal("110.00"),
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "threshold_over_ceiling" in verdict.reasons


def test_awaiting_confirmation_multiple_reasons(base_mandate: StandingMandate) -> None:
    draft = MissionScopeDraft(
        goals={"outside_goal"},
        repositories={"outside_repo"},
        capabilities={"outside_cap"},
        tier3_classes={"outside_t3"},
        budget_ceiling_usd=Decimal("200.00"),
        budget_threshold_usd=Decimal("250.00"),
    )
    verdict = mission_scope(
        mandate=base_mandate,
        draft=draft,
        standing_ceiling_usd=Decimal("100.00"),
    )

    assert verdict.status == "awaiting_confirmation"
    assert "goal_outside_mandate" in verdict.reasons
    assert "repository_outside_mandate" in verdict.reasons
    assert "capability_outside_mandate" in verdict.reasons
    assert "tier3_class_outside_mandate" in verdict.reasons
    assert "budget_over_standing" in verdict.reasons
    assert "threshold_over_ceiling" in verdict.reasons


# ==============================================================================
# 3. Encounter tier 3: inside vs outside mandate, owner signature effect
# ==============================================================================


def test_encounter_tier3_inside_mandate_blocked_until_owner_signed(
    base_mandate: StandingMandate,
) -> None:
    # Not signed -> status unchanged ("approved"), action blocked
    status, outcome = encounter_tier3(
        mandate=base_mandate,
        mission_status="approved",
        action_class="merge_protected_branch",
        owner_signed=False,
    )
    assert status == "approved"
    assert outcome == "blocked_owner_signature"

    # Signed -> status unchanged ("approved"), action allowed
    status, outcome = encounter_tier3(
        mandate=base_mandate,
        mission_status="approved",
        action_class="merge_protected_branch",
        owner_signed=True,
    )
    assert status == "approved"
    assert outcome == "allowed"

    # Running mission status preserved
    status, outcome = encounter_tier3(
        mandate=base_mandate,
        mission_status="running",
        action_class="merge_protected_branch",
        owner_signed=False,
    )
    assert status == "running"
    assert outcome == "blocked_owner_signature"


def test_encounter_tier3_outside_mandate_awaiting_confirmation_blocked(
    base_mandate: StandingMandate,
) -> None:
    # Class not in mandate: "delete_persistent_data"
    status, outcome = encounter_tier3(
        mandate=base_mandate,
        mission_status="approved",
        action_class="delete_persistent_data",
        owner_signed=False,
    )
    assert status == "awaiting_confirmation"
    assert outcome == "blocked_owner_signature"

    # Even with owner_signed, class outside mandate routes to awaiting_confirmation
    status, outcome = encounter_tier3(
        mandate=base_mandate,
        mission_status="approved",
        action_class="delete_persistent_data",
        owner_signed=True,
    )
    assert status == "awaiting_confirmation"
    assert outcome == "blocked_owner_signature"

    # With no mandate at all
    status, outcome = encounter_tier3(
        mandate=None,
        mission_status="approved",
        action_class="merge_protected_branch",
        owner_signed=True,
    )
    assert status == "awaiting_confirmation"
    assert outcome == "blocked_owner_signature"


# ==============================================================================
# 4. validate_mandate: tier 2 class or no decision refused
# ==============================================================================


def test_validate_mandate_refused_no_decision() -> None:
    mandate = StandingMandate(
        mandate_id="mandate-001",
        goals=frozenset({"build_feature"}),
        repositories=frozenset({"repo-alpha"}),
        capabilities=frozenset({"git_read"}),
        tier3_classes=frozenset({"merge_protected_branch"}),
        decision_id=None,
    )
    with pytest.raises(MandateRefused) as exc_info:
        validate_mandate(mandate)
    assert exc_info.value.code == "missing_decision"


def test_validate_mandate_refused_tier2_class() -> None:
    # push_branch is tier 2, not tier 3
    mandate = StandingMandate(
        mandate_id="mandate-001",
        goals=frozenset({"build_feature"}),
        repositories=frozenset({"repo-alpha"}),
        capabilities=frozenset({"git_read"}),
        tier3_classes=frozenset({"push_branch"}),
        decision_id="dec-418",
    )
    with pytest.raises(MandateRefused) as exc_info:
        validate_mandate(mandate)
    assert exc_info.value.code == "not_tier3"


def test_validate_mandate_refused_tier1_class() -> None:
    # read_state is tier 1, not tier 3
    mandate = StandingMandate(
        mandate_id="mandate-001",
        goals=frozenset({"build_feature"}),
        repositories=frozenset({"repo-alpha"}),
        capabilities=frozenset({"git_read"}),
        tier3_classes=frozenset({"read_state"}),
        decision_id="dec-418",
    )
    with pytest.raises(MandateRefused) as exc_info:
        validate_mandate(mandate)
    assert exc_info.value.code == "not_tier3"


def test_validate_mandate_valid(base_mandate: StandingMandate) -> None:
    # Must not raise
    validate_mandate(base_mandate)


# ==============================================================================
# 5. mandate_change_kind: create, narrow, widen
# ==============================================================================


def test_mandate_change_kind_create(base_mandate: StandingMandate) -> None:
    assert mandate_change_kind(None, base_mandate) == ChangeKind.create
    assert mandate_change_kind(None, base_mandate) == "create"


def test_mandate_change_kind_narrow(base_mandate: StandingMandate) -> None:
    # All subsets
    narrow_mandate = StandingMandate(
        mandate_id="mandate-002",
        goals=frozenset({"build_feature"}),  # subset
        repositories=frozenset({"repo-alpha"}),  # subset
        capabilities=frozenset({"git_read"}),  # subset
        tier3_classes=frozenset(),  # subset
        decision_id="dec-419",
    )
    assert mandate_change_kind(base_mandate, narrow_mandate) == ChangeKind.narrow
    assert mandate_change_kind(base_mandate, narrow_mandate) == "narrow"

    # Identical sets
    assert mandate_change_kind(base_mandate, base_mandate) == ChangeKind.narrow


def test_mandate_change_kind_widen(base_mandate: StandingMandate) -> None:
    # Widen goals
    widen_goals = StandingMandate(
        mandate_id="mandate-002",
        goals=frozenset({"build_feature", "fix_bugs", "new_goal"}),
        repositories=base_mandate.repositories,
        capabilities=base_mandate.capabilities,
        tier3_classes=base_mandate.tier3_classes,
        decision_id="dec-419",
    )
    assert mandate_change_kind(base_mandate, widen_goals) == ChangeKind.widen
    assert mandate_change_kind(base_mandate, widen_goals) == "widen"

    # Widen repositories
    widen_repos = StandingMandate(
        mandate_id="mandate-002",
        goals=base_mandate.goals,
        repositories=frozenset({"repo-alpha", "repo-beta", "repo-gamma"}),
        capabilities=base_mandate.capabilities,
        tier3_classes=base_mandate.tier3_classes,
        decision_id="dec-419",
    )
    assert mandate_change_kind(base_mandate, widen_repos) == ChangeKind.widen

    # Widen capabilities
    widen_caps = StandingMandate(
        mandate_id="mandate-002",
        goals=base_mandate.goals,
        repositories=base_mandate.repositories,
        capabilities=frozenset({"git_read", "test_runner", "net_admin"}),
        tier3_classes=base_mandate.tier3_classes,
        decision_id="dec-419",
    )
    assert mandate_change_kind(base_mandate, widen_caps) == ChangeKind.widen

    # Widen tier 3 classes
    widen_t3 = StandingMandate(
        mandate_id="mandate-002",
        goals=base_mandate.goals,
        repositories=base_mandate.repositories,
        capabilities=base_mandate.capabilities,
        tier3_classes=frozenset({"merge_protected_branch", "production_deploy"}),
        decision_id="dec-419",
    )
    assert mandate_change_kind(base_mandate, widen_t3) == ChangeKind.widen
