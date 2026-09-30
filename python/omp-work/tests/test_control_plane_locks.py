"""Tests for omp_work.control_plane.locks check and constants."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

import pytest

from omp_work.control_plane.gate import (
    Check,
    CheckContext,
    ControlPlaneFacts,
    OwnerAuthorization,
    Proposal,
    Refusal,
    decision_payload,
    evaluate,
)
from omp_work.control_plane.locks import (
    LOCKS,
    LOCK_ENFORCEMENT_PATHS,
    lock_integrity,
)

_P = "python/omp-work/src/omp_work"
_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
_PAST = datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc)
_FUTURE = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)


def _facts() -> ControlPlaneFacts:
    return ControlPlaneFacts(now=_NOW, current_revision=1)


def _ctx(verify_signature: object = None) -> CheckContext:
    verifier = verify_signature if callable(verify_signature) else (lambda m, s: True)
    return CheckContext(facts=_facts(), verify_signature=verifier)


def _evaluate(proposal: Proposal, ctx: CheckContext | None = None) -> tuple[Refusal, ...]:
    if ctx is None:
        ctx = _ctx()
    return evaluate(proposal, ctx, [lock_integrity]).refusals


def _only(proposal: Proposal, ctx: CheckContext | None = None) -> Refusal:
    refusals = _evaluate(proposal, ctx)
    assert len(refusals) == 1
    return refusals[0]


# --------------------------------------------------------------------------
# Constants: LOCKS and LOCK_ENFORCEMENT_PATHS
# --------------------------------------------------------------------------


def test_locks_map_and_immutability() -> None:
    assert isinstance(LOCKS, Mapping)
    assert len(LOCKS) == 12
    assert LOCKS[1] == "Repository/path allowlist"
    assert LOCKS[2] == "Isolated worktree/sandbox"
    assert LOCKS[3] == "No source credentials in workers"
    assert LOCKS[4] == "Single mutation authority"
    assert LOCKS[5] == "Lease + idempotency enforcement"
    assert LOCKS[6] == "Independent verification"
    assert LOCKS[7] == "Budget caps"
    assert LOCKS[8] == "Network egress policy"
    assert LOCKS[9] == "Protected-action gate"
    assert LOCKS[10] == "Stop means pause"
    assert LOCKS[11] == "Audit trail"
    assert LOCKS[12] == "Fail closed"

    with pytest.raises(TypeError):
        LOCKS[1] = "hacked"  # type: ignore[index]


def test_lock_enforcement_paths_coverage() -> None:
    expected_paths = {
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
    }
    assert set(LOCK_ENFORCEMENT_PATHS) == expected_paths


# --------------------------------------------------------------------------
# Required test cases
# --------------------------------------------------------------------------


def test_model_lock_change_of_lock_4_refused() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="model",
        operation="lock_change",
        lock_id=4,
    )
    refusal = _only(proposal)
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"

    payload = decision_payload(proposal, refusal)
    assert payload.action_class == "disable_safeguards"


def test_model_modify_files_touching_spend_budget_with_reviewer_required_refused() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="model",
        operation="modify_files",
        touched_paths=(f"{_P}/spend_budget.py",),
        reviewer_required=True,
    )
    refusal = _only(proposal)
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_worker_touching_jobs_lease_refused() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="worker",
        operation="modify_files",
        touched_paths=(f"{_P}/jobs/lease.py",),
    )
    refusal = _only(proposal)
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_worker_editing_control_plane_gate_refused() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="worker",
        operation="modify_files",
        touched_paths=(f"{_P}/control_plane/gate.py",),
    )
    refusal = _only(proposal)
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_orchestrator_lock_change_with_valid_signature_refused() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="orchestrator",
        operation="lock_change",
        lock_id=4,
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_FUTURE),
    )
    refusal = _only(proposal, _ctx(verify_signature=lambda m, s: True))
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_disable_safeguards_class_with_no_path_refused_for_model() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="model",
        action_class="disable_safeguards",
        touched_paths=(),
    )
    refusal = _only(proposal)
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


@pytest.mark.parametrize("proposer", ["model", "worker", "orchestrator", "typed_command"])
def test_all_non_owner_proposers_refused_on_lock_change(proposer: str) -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer=proposer,
        operation="lock_change",
        lock_id=1,
    )
    refusal = _only(proposal)
    assert refusal.code == "lock_change_not_owner"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_owner_with_no_or_expired_signature_refused_with_code() -> None:
    # 1. No authorization at all
    prop_no_auth = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="lock_change",
        lock_id=4,
        owner_authorization=None,
    )
    refusal = _only(prop_no_auth)
    assert refusal.code == "owner_signature_missing"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"

    # 2. Expired authorization
    prop_expired = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="lock_change",
        lock_id=4,
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_PAST),
    )
    refusal = _only(prop_expired)
    assert refusal.code == "owner_signature_expired"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"

    # 3. Invalid signature
    prop_invalid = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="lock_change",
        lock_id=4,
        owner_authorization=OwnerAuthorization(signature="bad_sig", expires_at=_FUTURE),
    )
    refusal = _only(prop_invalid, _ctx(verify_signature=lambda m, s: False))
    assert refusal.code == "owner_signature_invalid"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"

    # 4. Owner touching enforcement path without signature
    prop_touch_no_auth = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="modify_files",
        touched_paths=(f"{_P}/spend_budget.py",),
        owner_authorization=None,
    )
    refusal = _only(prop_touch_no_auth)
    assert refusal.code == "owner_signature_missing"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_owner_with_valid_signature_passes() -> None:
    # 1. Owner lock change
    prop_lock = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="lock_change",
        lock_id=4,
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_FUTURE),
    )
    verdict = evaluate(prop_lock, _ctx(verify_signature=lambda m, s: True), [lock_integrity])
    assert verdict.allowed is True
    assert verdict.refusals == ()

    # 2. Owner touching enforcement path
    prop_path = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        operation="modify_files",
        touched_paths=(f"{_P}/control_plane/gate.py",),
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_FUTURE),
    )
    verdict = evaluate(prop_path, _ctx(verify_signature=lambda m, s: True), [lock_integrity])
    assert verdict.allowed is True
    assert verdict.refusals == ()

    # 3. Owner disable_safeguards action_class
    prop_action = Proposal(
        proposal_id=uuid4(),
        proposer="owner",
        action_class="disable_safeguards",
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_FUTURE),
    )
    verdict = evaluate(prop_action, _ctx(verify_signature=lambda m, s: True), [lock_integrity])
    assert verdict.allowed is True
    assert verdict.refusals == ()


@pytest.mark.parametrize("proposer", ["owner", "model"])
@pytest.mark.parametrize("bad_lock_id", [None, 0, 13, 99])
def test_unknown_lock_id_refused(proposer: str, bad_lock_id: int | None) -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer=proposer,
        operation="lock_change",
        lock_id=bad_lock_id,
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=_FUTURE)
        if proposer == "owner"
        else None,
    )
    refusal = _only(proposal, _ctx(verify_signature=lambda m, s: True))
    assert refusal.check == "lock_integrity"
    assert refusal.code == "lock_unknown"
    assert refusal.raise_decision is True
    assert refusal.action_class == "disable_safeguards"


def test_unrelated_path_passes() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="model",
        operation="modify_files",
        touched_paths=(f"{_P}/something_else.py", "docs/README.md"),
    )
    verdict = evaluate(proposal, _ctx(), [lock_integrity])
    assert verdict.allowed is True
    assert verdict.refusals == ()


def test_non_matching_operation_and_action_class_pass() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="worker",
        operation="command",
        action_class="read_state",
        touched_paths=(),
    )
    verdict = evaluate(proposal, _ctx(), [lock_integrity])
    assert verdict.allowed is True
    assert verdict.refusals == ()
