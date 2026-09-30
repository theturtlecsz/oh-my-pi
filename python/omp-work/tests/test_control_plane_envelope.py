"""Tests for control plane command envelope classification, binding, and tier gate."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from omp_work.action_tiers import TIER1, TIER2, TIER3
from omp_work.control_plane.envelope import (
    bind_command,
    classify_command,
    tier_gate,
)
from omp_work.control_plane.gate import (
    CheckContext,
    ControlPlaneFacts,
    OwnerAuthorization,
    Proposal,
    Refusal,
    evaluate,
)
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.models import (
    ApproveMissionCommand,
    CommandEnvelope,
    EngageStopCommand,
    ReleaseStopCommand,
    ReviseMissionCommand,
    SetMissionStatusCommand,
    SubmitMissionCommand,
)
from omp_work.v1.service import WorkService

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("00000000-0000-0000-0000-0000000000ff")


# ---------------------------------------------------------------------------
# classify_command
# ---------------------------------------------------------------------------


def test_every_scope_classifies_into_a_tier_or_none() -> None:
    # The scope table is the whole classification input for a command with no
    # type-specific override; each scope must yield a listed class or None.
    listed = TIER1 | TIER2 | TIER3
    command = EngageStopCommand(type="engage_stop", payload={"reason": "x"})
    for scope in WorkService._scopes.values():
        result = classify_command(command, scope)
        assert result is None or result in listed


@pytest.mark.parametrize("scope", ["work.mutate", "work.execute", "work.close"])
def test_mutate_execute_close_scopes_classify_as_update_mission_state(scope: str) -> None:
    command = EngageStopCommand(type="engage_stop", payload={"reason": "x"})
    assert classify_command(command, scope) == "update_mission_state"
    assert "update_mission_state" in TIER1


def test_approve_scope_classifies_as_publish_as_owner_and_stop_as_pause_resume() -> None:
    command = EngageStopCommand(type="engage_stop", payload={"reason": "x"})
    assert classify_command(command, "work.approve") == "publish_as_owner"
    assert "publish_as_owner" in TIER3
    assert classify_command(command, "work.stop") == "pause_resume"
    assert "pause_resume" in TIER1


def test_unknown_scope_classifies_as_none() -> None:
    command = EngageStopCommand(type="engage_stop", payload={"reason": "x"})
    assert classify_command(command, "work.import") is None
    assert classify_command(command, "work.read") is None


def _mission_draft() -> dict[str, object]:
    return {
        "project_id": str(uuid4()),
        "objective": "do the thing",
        "risk_policy": "risk-1",
        "approval_policy": "approval-1",
        "effort_policy": "effort-1",
    }


def test_submit_revise_approve_mission_override_to_broaden_scope() -> None:
    mid = uuid4()
    submit = SubmitMissionCommand(
        type="submit_mission", payload={"mission_id": mid, "draft": _mission_draft()}
    )
    revise = ReviseMissionCommand(
        type="revise_mission",
        payload={"mission_id": mid, "base_revision": 1, "draft": _mission_draft()},
    )
    approve = ApproveMissionCommand(
        type="approve_mission",
        payload={
            "mission_id": mid,
            "revision": 1,
            "basis_kind": "decision",
            "basis_id": "d-1",
        },
    )
    assert classify_command(submit, "work.mutate") == "broaden_scope"
    assert classify_command(revise, "work.mutate") == "broaden_scope"
    assert classify_command(approve, "work.approve") == "broaden_scope"
    assert "broaden_scope" in TIER3


def test_release_stop_override_to_disable_safeguards() -> None:
    command = ReleaseStopCommand(type="release_stop", payload={"reason": "x"})
    # The override wins over the approve scope's ordinary publish_as_owner class.
    assert classify_command(command, "work.approve") == "disable_safeguards"
    assert "disable_safeguards" in TIER3


def _status_command(target: str) -> SetMissionStatusCommand:
    return SetMissionStatusCommand(
        type="set_mission_status",
        payload={
            "mission_id": uuid4(),
            "target_status": target,
            "cause_kind": "principal",
        },
    )


@pytest.mark.parametrize("target", ["running", "paused", "blocked"])
def test_set_mission_status_running_paused_blocked_is_pause_resume(target: str) -> None:
    assert classify_command(_status_command(target), "work.execute") == "pause_resume"


@pytest.mark.parametrize("target", ["completed", "failed"])
def test_set_mission_status_terminal_is_update_mission_state(target: str) -> None:
    assert (
        classify_command(_status_command(target), "work.execute")
        == "update_mission_state"
    )


def test_set_mission_status_abandoned_is_unclassified() -> None:
    assert classify_command(_status_command("abandoned"), "work.execute") is None


# ---------------------------------------------------------------------------
# bind_command
# ---------------------------------------------------------------------------


def _envelope(command: object) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=WORKSPACE,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )


def test_bind_command_fills_type_class_and_mission_from_command() -> None:
    mid = uuid4()
    command = SubmitMissionCommand(
        type="submit_mission", payload={"mission_id": mid, "draft": _mission_draft()}
    )
    proposal = Proposal(proposal_id=uuid4(), workspace_id=WORKSPACE)
    bound = bind_command(proposal, _envelope(command), "work.mutate")
    assert isinstance(bound, Proposal)
    assert bound.command_type == "submit_mission"
    assert bound.action_class == "broaden_scope"
    assert bound.mission_id == mid


def test_bind_command_revise_mission_labelled_update_mission_state_mismatches() -> None:
    mid = uuid4()
    command = ReviseMissionCommand(
        type="revise_mission",
        payload={"mission_id": mid, "base_revision": 1, "draft": _mission_draft()},
    )
    proposal = Proposal(
        proposal_id=uuid4(),
        workspace_id=WORKSPACE,
        command_type="revise_mission",
        action_class="update_mission_state",
        mission_id=mid,
    )
    refusal = bind_command(proposal, _envelope(command), "work.mutate")
    assert isinstance(refusal, Refusal)
    assert refusal.check == "command_binding"
    assert refusal.code == "classification_mismatch"
    assert refusal.raise_decision is False


def test_bind_command_mission_id_mismatch_refuses() -> None:
    command = SubmitMissionCommand(
        type="submit_mission", payload={"mission_id": uuid4(), "draft": _mission_draft()}
    )
    proposal = Proposal(proposal_id=uuid4(), workspace_id=WORKSPACE, mission_id=uuid4())
    refusal = bind_command(proposal, _envelope(command), "work.mutate")
    assert isinstance(refusal, Refusal)
    assert refusal.code == "classification_mismatch"


def test_bind_command_workspace_and_operation_mismatch_refuse() -> None:
    command = EngageStopCommand(type="engage_stop", payload={"reason": "x"})
    env = _envelope(command)

    wrong_ws = Proposal(proposal_id=uuid4(), workspace_id=uuid4())
    assert isinstance(bind_command(wrong_ws, env, "work.stop"), Refusal)

    wrong_op = Proposal(proposal_id=uuid4(), workspace_id=WORKSPACE, operation="read")
    refusal = bind_command(wrong_op, env, "work.stop")
    assert isinstance(refusal, Refusal)
    assert refusal.code == "classification_mismatch"


# ---------------------------------------------------------------------------
# tier_gate
# ---------------------------------------------------------------------------


def _ctx(
    *,
    policies: tuple[StandingPolicy, ...] = (),
    repos: tuple[RepositoryRecord, ...] = (),
    verify_signature=None,
) -> CheckContext:
    return CheckContext(
        facts=ControlPlaneFacts(
            now=NOW,
            current_revision=1,
            standing_policies=policies,
            repositories=repos,
        ),
        verify_signature=verify_signature,
    )


def _run_tier_gate(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    result = tier_gate(proposal, ctx)
    assert result is None or isinstance(result, Refusal)
    return result


def test_tier_gate_named_and_tier1_passes_without_authorization() -> None:
    assert tier_gate.name == "tier_gate"
    proposal = Proposal(proposal_id=uuid4(), action_class="modify_files")
    assert _run_tier_gate(proposal, _ctx()) is None


def test_tier_gate_unclassified_unsigned_refuses_unlisted() -> None:
    proposal = Proposal(proposal_id=uuid4(), action_class=None)
    refusal = _run_tier_gate(proposal, _ctx())
    assert refusal is not None
    assert refusal.check == "tier_gate"
    assert refusal.code == "unclassified_action"
    assert refusal.raise_decision is True
    assert refusal.action_class == "unlisted"


def test_tier_gate_unclassified_blank_unsigned_refuses() -> None:
    proposal = Proposal(proposal_id=uuid4(), action_class="  ")
    refusal = _run_tier_gate(proposal, _ctx())
    assert refusal is not None
    assert refusal.code == "unclassified_action"
    assert refusal.action_class == "unlisted"


def test_tier_gate_unlisted_class_unsigned_refuses_unlisted() -> None:
    proposal = Proposal(proposal_id=uuid4(), action_class="not_a_real_class")
    refusal = _run_tier_gate(proposal, _ctx())
    assert refusal is not None
    assert refusal.code == "unclassified_action"
    assert refusal.action_class == "unlisted"


def test_tier_gate_unclassified_signed_passes() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class=None,
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=FUTURE),
    )
    ctx = _ctx(verify_signature=lambda msg, sig: True)
    assert _run_tier_gate(proposal, ctx) is None


def test_tier_gate_unclassified_invalid_signature_refuses_unlisted() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class=None,
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=FUTURE),
    )
    ctx = _ctx(verify_signature=lambda msg, sig: False)
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "unclassified_action"
    assert refusal.action_class == "unlisted"


def test_tier_gate_tier3_unsigned_refuses_with_helper_code() -> None:
    proposal = Proposal(proposal_id=uuid4(), action_class="merge_protected_branch")
    refusal = _run_tier_gate(proposal, _ctx())
    assert refusal is not None
    assert refusal.check == "tier_gate"
    assert refusal.code == "owner_signature_missing"
    assert refusal.raise_decision is True
    assert refusal.action_class == "merge_protected_branch"


def test_tier_gate_tier3_invalid_refuses() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=FUTURE),
    )
    ctx = _ctx(verify_signature=lambda msg, sig: False)
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "owner_signature_invalid"
    assert refusal.action_class == "merge_protected_branch"


def test_tier_gate_tier3_expired_refuses() -> None:
    past = datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc)
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=past),
    )
    ctx = _ctx(verify_signature=lambda msg, sig: True)
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "owner_signature_expired"
    assert refusal.action_class == "merge_protected_branch"


def test_tier_gate_tier3_raising_refuses() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=FUTURE),
    )

    def crashing(msg: bytes, sig: str) -> bool:
        raise RuntimeError("verifier crashed")

    ctx = _ctx(verify_signature=crashing)
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "check_error"
    assert refusal.action_class == "merge_protected_branch"


def test_tier_gate_tier3_valid_passes() -> None:
    proposal = Proposal(
        proposal_id=uuid4(),
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig", expires_at=FUTURE),
    )
    ctx = _ctx(verify_signature=lambda msg, sig: True)
    assert _run_tier_gate(proposal, ctx) is None


# --- tier 2 ---


def _push_policy(
    *, policy_id: str, repos: tuple[str, ...] = ("repo-1",)
) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="push_branch",
        repositories=repos,
        branch_patterns=("feature/*",),
        decision_id="d-1",
    )


def _push_action() -> ActionRequest:
    return ActionRequest(
        action_class="push_branch", repository="repo-1", branch="feature/x"
    )


def _push_repo() -> RepositoryRecord:
    return RepositoryRecord(
        key="repo-1", default_branch="main", automation_ci_secret_free=True
    )


def test_tier_gate_tier2_without_action_refuses_missing() -> None:
    proposal = Proposal(proposal_id=uuid4(), action_class="push_branch", action=None)
    refusal = _run_tier_gate(proposal, _ctx())
    assert refusal is not None
    assert refusal.code == "standing_policy_missing"
    assert refusal.raise_decision is True


def test_tier_gate_tier2_without_covering_policy_refuses_missing() -> None:
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    refusal = _run_tier_gate(proposal, _ctx(repos=(_push_repo(),)))
    assert refusal is not None
    assert refusal.code == "standing_policy_missing"


def test_tier_gate_tier2_single_covering_policy_passes() -> None:
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    ctx = _ctx(policies=(_push_policy(policy_id="p-1"),), repos=(_push_repo(),))
    assert _run_tier_gate(proposal, ctx) is None


def test_tier_gate_tier2_two_covering_policies_with_equal_bounds_pass() -> None:
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    ctx = _ctx(
        policies=(_push_policy(policy_id="p-1"), _push_policy(policy_id="p-2")),
        repos=(_push_repo(),),
    )
    assert _run_tier_gate(proposal, ctx) is None


def test_tier_gate_tier2_two_covering_policies_with_differing_bounds_conflict() -> None:
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    ctx = _ctx(
        policies=(
            _push_policy(policy_id="p-1", repos=("repo-1",)),
            _push_policy(policy_id="p-2", repos=("repo-1", "repo-2")),
        ),
        repos=(_push_repo(),),
    )
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "policy_conflict"
    assert refusal.raise_decision is True


def test_tier_gate_tier2_non_covering_class_refuses_missing() -> None:
    # A policy of a different action class never covers the request.
    policy = StandingPolicy(
        policy_id="p-net",
        action_class="network_access",
        destinations=("example.com",),
        decision_id="d-1",
    )
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    ctx = _ctx(policies=(policy,), repos=(_push_repo(),))
    refusal = _run_tier_gate(proposal, ctx)
    assert refusal is not None
    assert refusal.code == "standing_policy_missing"


def test_tier_gate_tier2_covers_error_propagates_through_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = Proposal(
        proposal_id=uuid4(), action_class="push_branch", action=_push_action()
    )
    ctx = _ctx(policies=(_push_policy(policy_id="p-1"),), repos=(_push_repo(),))

    def raising_covers(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("policy store unavailable")

    import omp_work.control_plane.envelope as envelope_module

    monkeypatch.setattr(envelope_module, "covers", raising_covers)

    verdict = evaluate(proposal, ctx, [tier_gate])
    assert len(verdict.refusals) == 1
    assert verdict.refusals[0].check == "tier_gate"
    assert verdict.refusals[0].code == "check_error"
    assert verdict.refusals[0].raise_decision is True
