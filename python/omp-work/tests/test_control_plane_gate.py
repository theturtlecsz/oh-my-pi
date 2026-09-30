"""Tests for omp_work.control_plane.gate types, evaluator, and helpers."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import json
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from omp_work.control_plane.gate import (
    AcceptanceFact,
    Check,
    CheckContext,
    ControlPlaneFacts,
    DecisionPayload,
    LeaseClaim,
    LeaseFact,
    MissionFact,
    OwnerAuthorization,
    Proposal,
    Refusal,
    ReservationFact,
    Verdict,
    authorization_message,
    decision_payload,
    evaluate,
    verify_owner_authorization,
)
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.models import CreateDecisionPayload
from omp_work.v1.owner_signature import decision_signature_message


def test_proposal_dataclass_fields_and_defaults() -> None:
    proposal = Proposal()
    assert proposal.proposal_id is None
    assert proposal.workspace_id is None
    assert proposal.project_id is None
    assert proposal.mission_id is None
    assert proposal.proposer == "model"
    assert proposal.typed_command is None
    assert proposal.operation == "command"
    assert proposal.command_type is None
    assert proposal.action_class is None
    assert proposal.action is None
    assert proposal.provenance is None
    assert proposal.basis_source is None
    assert proposal.basis_revision is None
    assert proposal.lease is None
    assert proposal.effort is None
    assert proposal.effort_reason is None
    assert proposal.reservation_id is None
    assert proposal.cost_usd is None
    assert proposal.target_id is None
    assert proposal.touched_paths == ()
    assert proposal.reviewer_required is None
    assert proposal.reviewer_override_reason is None
    assert proposal.contract_sha256 is None
    assert proposal.lock_id is None
    assert proposal.owner_authorization is None


def test_proposal_post_init_coercions_and_immutability() -> None:
    pid = "00000000-0000-0000-0000-000000000001"
    wid = "00000000-0000-0000-0000-000000000002"
    prid = "00000000-0000-0000-0000-000000000003"
    mid = "00000000-0000-0000-0000-000000000004"
    proposal = Proposal(
        proposal_id=pid,  # type: ignore[arg-type]
        workspace_id=wid,  # type: ignore[arg-type]
        project_id=prid,  # type: ignore[arg-type]
        mission_id=mid,  # type: ignore[arg-type]
        touched_paths=["a.txt", "b.txt"],  # type: ignore[arg-type]
        cost_usd="12.50",  # type: ignore[arg-type]
    )
    assert proposal.proposal_id == UUID(pid)
    assert proposal.workspace_id == UUID(wid)
    assert proposal.project_id == UUID(prid)
    assert proposal.mission_id == UUID(mid)
    assert proposal.touched_paths == ("a.txt", "b.txt")
    assert proposal.cost_usd == Decimal("12.50")

    with pytest.raises(FrozenInstanceError):
        proposal.proposer = "worker"  # type: ignore[misc]


def test_fact_types_and_post_init_coercions() -> None:
    now_str = "2026-09-30T12:00:00+00:00"
    lease_fact = LeaseFact(worker_id="w-1", fence=1, expires_at=now_str)  # type: ignore[arg-type]
    assert isinstance(lease_fact.expires_at, datetime)

    mission_fact = MissionFact(approved=True, in_flight=2, capacity=5)
    assert mission_fact.approved is True
    assert mission_fact.in_flight == 2
    assert mission_fact.capacity == 5

    res_fact = ReservationFact(mission_id="00000000-0000-0000-0000-000000000001", remaining_usd="50.00")  # type: ignore[arg-type]
    assert res_fact.remaining_usd == Decimal("50.00")
    assert res_fact.mission_id == UUID("00000000-0000-0000-0000-000000000001")

    acceptance_fact = AcceptanceFact(
        sealed_criteria=["c1", "c2"],  # type: ignore[arg-type]
        evidence={"c1": ("e1",)},
        author_id="a-1",
        reviewer_id="r-1",
        verdict="PASS",
    )
    assert acceptance_fact.sealed_criteria == ("c1", "c2")

    facts = ControlPlaneFacts(
        now=now_str,  # type: ignore[arg-type]
        current_revision=42,
        leases={"job-1": lease_fact},
        missions={UUID("00000000-0000-0000-0000-000000000001"): mission_fact},
        reservations={"res-1": res_fact},
        standing_policies=[  # type: ignore[arg-type]
            StandingPolicy(policy_id="p-1", action_class="push_branch", decision_id="d-1")
        ],
        repositories=[  # type: ignore[arg-type]
            RepositoryRecord(key="repo-1", default_branch="main")
        ],
        acceptance={"target-1": acceptance_fact},
        approved_contract_sha256={"sha-1", "sha-2"},  # type: ignore[arg-type]
    )
    assert isinstance(facts.now, datetime)
    assert isinstance(facts.standing_policies, tuple)
    assert isinstance(facts.repositories, tuple)
    assert isinstance(facts.approved_contract_sha256, frozenset)
    assert "sha-1" in facts.approved_contract_sha256


def test_verdict_properties_and_truthiness() -> None:
    v_empty = Verdict()
    assert v_empty.allowed is True
    assert v_empty.authorized is True
    assert bool(v_empty) is True
    assert v_empty.deciding_refusal is None
    assert v_empty.raises_decision is False

    refusal_soft = Refusal(check="c1", code="code1", raise_decision=False)
    v_soft = Verdict(refusals=(refusal_soft,))
    assert v_soft.allowed is False
    assert bool(v_soft) is False
    assert v_soft.deciding_refusal is None
    assert v_soft.raises_decision is False

    refusal_hard = Refusal(check="c2", code="code2", raise_decision=True, action_class="unlisted")
    v_hard = Verdict(refusals=(refusal_soft, refusal_hard))
    assert v_hard.allowed is False
    assert v_hard.deciding_refusal == refusal_hard
    assert v_hard.raises_decision is True


def test_evaluate_runs_all_checks_and_collects_refusals() -> None:
    proposal = Proposal()
    ctx = CheckContext(facts=ControlPlaneFacts(now=datetime.now(timezone.utc), current_revision=1))

    c1 = Check(name="check1", fn=lambda p, c: None)
    c2 = Check(
        name="check2",
        fn=lambda p, c: Refusal(check="check2", code="fail2", raise_decision=False),
    )
    c3 = Check(
        name="check3",
        fn=lambda p, c: [
            Refusal(check="check3", code="fail3a", raise_decision=False),
            Refusal(check="check3", code="fail3b", raise_decision=True),
        ],
    )

    verdict = evaluate(proposal, ctx, [c1, c2, c3])
    assert verdict.checks_run == ("check1", "check2", "check3")
    assert len(verdict.refusals) == 3
    assert verdict.refusals[0].code == "fail2"
    assert verdict.refusals[1].code == "fail3a"
    assert verdict.refusals[2].code == "fail3b"
    assert verdict.deciding_refusal == verdict.refusals[2]


def test_evaluate_maps_exception_to_check_error() -> None:
    proposal = Proposal()
    ctx = CheckContext(facts=ControlPlaneFacts(now=datetime.now(timezone.utc), current_revision=1))

    def raising_check(p: Proposal, c: CheckContext) -> None:
        raise ValueError("database unavailable")

    c = Check(name="bad_check", fn=raising_check)
    verdict = evaluate(proposal, ctx, [c])
    assert len(verdict.refusals) == 1
    refusal = verdict.refusals[0]
    assert refusal.check == "bad_check"
    assert refusal.code == "check_error"
    assert refusal.raise_decision is True


def test_evaluate_maps_timeout_error_and_budget_overrun() -> None:
    proposal = Proposal()
    ctx = CheckContext(facts=ControlPlaneFacts(now=datetime.now(timezone.utc), current_revision=1))

    def timeout_check(p: Proposal, c: CheckContext) -> None:
        raise TimeoutError("timed out")

    c_timeout = Check(name="timeout_check", fn=timeout_check)
    verdict = evaluate(proposal, ctx, [c_timeout])
    assert len(verdict.refusals) == 1
    assert verdict.refusals[0].check == "timeout_check"
    assert verdict.refusals[0].code == "check_timeout"
    assert verdict.refusals[0].raise_decision is True

    # Test clock budget overrun
    fake_time = 0.0

    def fake_clock() -> float:
        nonlocal fake_time
        return fake_time

    def slow_check(p: Proposal, c: CheckContext) -> None:
        nonlocal fake_time
        fake_time += 15.0

    c_slow = Check(name="slow_check", fn=slow_check)
    c_after = Check(name="after_check", fn=lambda p, c: None)
    verdict_budget = evaluate(
        proposal, ctx, [c_slow, c_after], budget_seconds=10.0, clock=fake_clock
    )
    assert len(verdict_budget.refusals) == 2
    assert verdict_budget.refusals[0].check == "slow_check"
    assert verdict_budget.refusals[0].code == "check_timeout"
    assert verdict_budget.refusals[0].raise_decision is True
    assert verdict_budget.refusals[1].check == "after_check"
    assert verdict_budget.refusals[1].code == "check_timeout"
    assert verdict_budget.refusals[1].raise_decision is True


def test_authorization_message_canonical_json_structure() -> None:
    pid = UUID("00000000-0000-0000-0000-000000000010")
    wid = UUID("00000000-0000-0000-0000-000000000020")
    prid = UUID("00000000-0000-0000-0000-000000000030")
    mid = UUID("00000000-0000-0000-0000-000000000040")
    proposal = Proposal(
        proposal_id=pid,
        workspace_id=wid,
        project_id=prid,
        mission_id=mid,
        proposer="model",
        typed_command="execute",
        operation="command",
        command_type="update_state",
        action_class="update_mission_state",
        touched_paths=("b.py", "a.py"),
        lock_id=4,
    )
    expires_at = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
    msg_bytes = authorization_message(proposal, expires_at)
    assert isinstance(msg_bytes, bytes)

    parsed = json.loads(msg_bytes)
    assert parsed["kind"] == "control_plane_authorization"
    assert parsed["proposal_id"] == str(pid)
    assert parsed["workspace_id"] == str(wid)
    assert parsed["project_id"] == str(prid)
    assert parsed["mission_id"] == str(mid)
    assert parsed["proposer"] == "model"
    assert parsed["typed_command"] == "execute"
    assert parsed["operation"] == "command"
    assert parsed["command_type"] == "update_state"
    assert parsed["action_class"] == "update_mission_state"
    assert parsed["touched_paths"] == ["a.py", "b.py"]
    assert parsed["lock_id"] == 4
    assert parsed["expires_at"] == expires_at.isoformat()

    # Canonical formatting: separators are exactly (',', ':') with sorted keys
    expected_str = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert msg_bytes.decode("utf-8") == expected_str


def test_verify_owner_authorization_matrix() -> None:
    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
    facts = ControlPlaneFacts(now=now, current_revision=1)
    proposal = Proposal(proposal_id=uuid4())

    # 1. Missing authorization
    assert (
        verify_owner_authorization(proposal, facts=facts, verify_signature=lambda m, s: True)
        == "owner_signature_missing"
    )

    # 2. Missing signature string
    prop_no_sig = Proposal(
        proposal_id=uuid4(),
        owner_authorization=OwnerAuthorization(signature="", expires_at=now),
    )
    assert (
        verify_owner_authorization(prop_no_sig, facts=facts, verify_signature=lambda m, s: True)
        == "owner_signature_missing"
    )

    # 3. Missing expires_at
    prop_no_exp = Proposal(
        proposal_id=uuid4(),
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=None),
    )
    assert (
        verify_owner_authorization(prop_no_exp, facts=facts, verify_signature=lambda m, s: True)
        == "owner_signature_missing"
    )

    # 4. Expired signature
    past = datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc)
    prop_expired = Proposal(
        proposal_id=uuid4(),
        owner_authorization=OwnerAuthorization(signature="valid_sig", expires_at=past),
    )
    assert (
        verify_owner_authorization(prop_expired, facts=facts, verify_signature=lambda m, s: True)
        == "owner_signature_expired"
    )

    # 5. Verifier raising check_error
    future = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
    prop_valid_time = Proposal(
        proposal_id=uuid4(),
        owner_authorization=OwnerAuthorization(signature="some_sig", expires_at=future),
    )

    def crashing_verifier(m: bytes, s: str) -> bool:
        raise RuntimeError("verification subprocess crashed")

    assert (
        verify_owner_authorization(prop_valid_time, facts=facts, verify_signature=crashing_verifier)
        == "check_error"
    )

    # 6. Verifier returning False
    assert (
        verify_owner_authorization(
            prop_valid_time, facts=facts, verify_signature=lambda m, s: False
        )
        == "owner_signature_invalid"
    )

    # 7. Valid signature
    assert (
        verify_owner_authorization(
            prop_valid_time, facts=facts, verify_signature=lambda m, s: True
        )
        is None
    )


def test_signature_for_another_proposal_and_decision_message_are_invalid() -> None:
    wid = UUID("00000000-0000-0000-0000-000000000001")
    pid_a = UUID("00000000-0000-0000-0000-000000000002")
    pid_b = UUID("00000000-0000-0000-0000-000000000003")
    future = datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
    facts = ControlPlaneFacts(now=now, current_revision=1)

    prop_a = Proposal(
        proposal_id=pid_a,
        workspace_id=wid,
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig_a", expires_at=future),
    )
    prop_b = Proposal(
        proposal_id=pid_b,
        workspace_id=wid,
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="sig_a", expires_at=future),
    )

    msg_a = authorization_message(prop_a, future)

    # A mock verifier that only verifies signature "sig_a" for exact message bytes msg_a
    def strict_verifier(msg: bytes, sig: str) -> bool:
        return sig == "sig_a" and msg == msg_a

    ctx = CheckContext(facts=facts, verify_signature=strict_verifier)

    # Proposal A verifies successfully
    assert verify_owner_authorization(prop_a, ctx) is None

    # Proposal B with signature signed for proposal A is rejected as invalid
    assert verify_owner_authorization(prop_b, ctx) == "owner_signature_invalid"

    # Now create a signature for decision_signature_message with the exact same ids
    did = uuid5(NAMESPACE_URL, str(pid_a))
    decision_msg = decision_signature_message(
        workspace_id=wid,
        decision_id=did,
        action_class="merge_protected_branch",
        answer="authorize",
    )

    def decision_msg_verifier(msg: bytes, sig: str) -> bool:
        return sig == "decision_sig" and msg == decision_msg

    prop_with_decision_sig = Proposal(
        proposal_id=pid_a,
        workspace_id=wid,
        action_class="merge_protected_branch",
        owner_authorization=OwnerAuthorization(signature="decision_sig", expires_at=future),
    )
    ctx_decision = CheckContext(facts=facts, verify_signature=decision_msg_verifier)

    # A signature for decision_signature_message cannot be replayed for authorization_message
    assert (
        verify_owner_authorization(prop_with_decision_sig, ctx_decision)
        == "owner_signature_invalid"
    )


def test_decision_payload_determinism_and_item_access() -> None:
    pid = UUID("00000000-0000-0000-0000-000000000100")
    wid = UUID("00000000-0000-0000-0000-000000000200")
    prid = UUID("00000000-0000-0000-0000-000000000300")
    mid = UUID("00000000-0000-0000-0000-000000000400")
    proposal = Proposal(
        proposal_id=pid,
        workspace_id=wid,
        project_id=prid,
        mission_id=mid,
        action_class="modify_files",
    )
    refusal = Refusal(check="gate", code="refused", raise_decision=True)

    payload1 = decision_payload(proposal, refusal)
    payload2 = decision_payload(proposal, refusal)

    # Two payload calls must be equal
    assert payload1 == payload2
    assert payload1.decision_id == uuid5(NAMESPACE_URL, str(pid))
    assert payload1.project_id == prid
    assert payload1.mission_id == str(mid)

    # Item access works via __getitem__
    assert payload1["decision_id"] == uuid5(NAMESPACE_URL, str(pid))
    assert payload1["project_id"] == prid
    assert isinstance(payload1, CreateDecisionPayload)


def test_decision_payload_action_class_mapping() -> None:
    pid = uuid4()

    # 1. Deciding refusal carries action_class (e.g. contract_hash from s04)
    ref_contract = Refusal(
        check="contract_freeze",
        code="contract_frozen",
        raise_decision=True,
        action_class="contract_hash",
    )
    prop1 = Proposal(proposal_id=pid, action_class="modify_files")
    p1 = decision_payload(prop1, ref_contract)
    assert p1.action_class == "contract_hash"
    assert p1["action_class"] == "contract_hash"

    # 2. Refusal sets no class, proposal has a TIER3 class
    ref_general = Refusal(check="tier_gate", code="sig_missing", raise_decision=True)
    prop_tier3 = Proposal(proposal_id=pid, action_class="merge_protected_branch")
    p2 = decision_payload(prop_tier3, ref_general)
    assert p2.action_class == "merge_protected_branch"

    # 3. Refusal sets no class, proposal has no class -> "unlisted"
    prop_no_class = Proposal(proposal_id=pid, action_class=None)
    p3 = decision_payload(prop_no_class, ref_general)
    assert p3.action_class == "unlisted"

    # 4. Refusal sets no class, proposal has class not in any tier -> "unlisted"
    prop_unknown = Proposal(proposal_id=pid, action_class="nonexistent_action")
    p4 = decision_payload(prop_unknown, ref_general)
    assert p4.action_class == "unlisted"

    # 5. Refusal sets no class, proposal has TIER1 class -> None
    prop_tier1 = Proposal(proposal_id=pid, action_class="modify_files")
    p5 = decision_payload(prop_tier1, ref_general)
    assert p5.action_class is None

    # 6. Refusal sets no class, proposal has TIER2 class -> None
    prop_tier2 = Proposal(proposal_id=pid, action_class="push_branch")
    p6 = decision_payload(prop_tier2, ref_general)
    assert p6.action_class is None
