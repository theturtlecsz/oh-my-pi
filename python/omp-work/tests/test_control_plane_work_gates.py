"""Tests for omp_work.control_plane.work_gates admission, budget, and acceptance checks."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from omp_work.action_tiers import TIER1
from omp_work.control_plane.gate import (
    AcceptanceFact,
    Check,
    CheckContext,
    ControlPlaneFacts,
    MissionFact,
    Proposal,
    Refusal,
    ReservationFact,
    evaluate,
)
from omp_work.control_plane.work_gates import (
    PAID_WORK_COMMANDS,
    acceptance_semantics,
    admission_control,
    paid_work_gate,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
MISSION_ID = UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_MISSION_ID = UUID("00000000-0000-0000-0000-0000000000a2")
GATES = (admission_control, paid_work_gate, acceptance_semantics)


def _ctx(
    *,
    missions: dict[UUID, MissionFact] | None = None,
    reservations: dict[str, ReservationFact] | None = None,
    acceptance: dict[str, AcceptanceFact] | None = None,
) -> CheckContext:
    return CheckContext(
        facts=ControlPlaneFacts(
            now=NOW,
            current_revision=1,
            missions={} if missions is None else missions,
            reservations={} if reservations is None else reservations,
            acceptance={} if acceptance is None else acceptance,
        )
    )


def _mission(*, approved: bool = True, in_flight: int = 0, capacity: int = 1) -> MissionFact:
    return MissionFact(approved=approved, in_flight=in_flight, capacity=capacity)


def _expect(
    check: Check,
    proposal: Proposal,
    ctx: CheckContext,
    code: str,
    *,
    decision: bool = False,
) -> None:
    result = check(proposal, ctx)
    assert isinstance(result, Refusal)
    assert result.check == check.name
    assert result.code == code
    assert result.raise_decision is decision


def _passing_fact() -> AcceptanceFact:
    return AcceptanceFact(
        sealed_criteria=("c1", "c2"),
        evidence={"c1": ("e1",), "c2": ("e2",)},
        author_id="author-1",
        reviewer_id="reviewer-1",
        verdict="PASS",
    )


def test_admission_control_mission_not_approved() -> None:
    # Effort is also invalid; mission is the earlier refusal.
    unapproved = Proposal(
        operation="dispatch",
        command_type="read_state",
        mission_id=MISSION_ID,
        effort=None,
    )
    _expect(
        admission_control,
        Proposal(operation="dispatch", command_type="read_state", mission_id=None, effort=None),
        _ctx(missions={MISSION_ID: _mission()}),
        "mission_not_approved",
    )
    _expect(admission_control, unapproved, _ctx(), "mission_not_approved")
    _expect(
        admission_control,
        unapproved,
        _ctx(missions={MISSION_ID: _mission(approved=False)}),
        "mission_not_approved",
    )


def test_admission_control_effort_invalid() -> None:
    # No reservation either; effort is the earlier refusal.
    cases = (
        (None, None),
        ("e1", None),
        ("E2", None),
        ("E2", ""),
        ("E4", "because"),
        ("E4", "too short"),
    )
    ctx = _ctx(missions={MISSION_ID: _mission()})
    for effort, reason in cases:
        _expect(
            admission_control,
            Proposal(
                operation="dispatch",
                command_type="read_state",
                mission_id=MISSION_ID,
                effort=effort,
                effort_reason=reason,
                reservation_id=None,
            ),
            ctx,
            "effort_invalid",
        )


def test_admission_control_reservation_missing() -> None:
    # Capacity is full; a missing reservation id is the earlier refusal.
    ctx = _ctx(missions={MISSION_ID: _mission(in_flight=5, capacity=1)})
    for reservation_id in (None, ""):
        _expect(
            admission_control,
            Proposal(
                operation="dispatch",
                command_type="read_state",
                mission_id=MISSION_ID,
                effort="E1",
                reservation_id=reservation_id,
            ),
            ctx,
            "reservation_missing",
        )


def test_admission_control_capacity_full() -> None:
    for in_flight, capacity in ((1, 1), (2, 1), (0, 0)):
        _expect(
            admission_control,
            Proposal(
                operation="dispatch",
                command_type="read_state",
                mission_id=MISSION_ID,
                effort="E1",
                reservation_id="res-1",
            ),
            _ctx(missions={MISSION_ID: _mission(in_flight=in_flight, capacity=capacity)}),
            "capacity_full",
        )


def test_admission_control_passes() -> None:
    long_reason = "the blast radius is the whole fleet"
    ctx = _ctx(missions={MISSION_ID: _mission(in_flight=0, capacity=1)})
    for effort, reason in (("E1", None), ("E4", long_reason)):
        assert (
            admission_control(
                Proposal(
                    operation="dispatch",
                    command_type="read_state",
                    mission_id=MISSION_ID,
                    effort=effort,
                    effort_reason=reason,
                    reservation_id="res-not-in-facts",
                ),
                ctx,
            )
            is None
        )


def test_paid_work_gate_cost_missing() -> None:
    # Reservation is also absent; cost is the earlier refusal.
    _expect(
        paid_work_gate,
        Proposal(operation="command", command_type="begin_execution", cost_usd=None, reservation_id=None),
        _ctx(),
        "cost_missing",
    )
    _expect(
        paid_work_gate,
        Proposal(
            operation="dispatch",
            command_type="read_state",
            mission_id=MISSION_ID,
            cost_usd=None,
            reservation_id="res-1",
        ),
        _ctx(
            reservations={
                "res-1": ReservationFact(mission_id=MISSION_ID, remaining_usd=Decimal("10")),
            }
        ),
        "cost_missing",
    )


def test_paid_work_gate_reservation_missing() -> None:
    # Cost is over the other mission's remaining budget; the mission mismatch is earlier.
    over = Decimal("50")
    ctx = _ctx(
        reservations={
            "other": ReservationFact(mission_id=OTHER_MISSION_ID, remaining_usd=Decimal("1")),
        }
    )
    cases = (
        Proposal(
            operation="dispatch",
            mission_id=MISSION_ID,
            cost_usd=over,
            reservation_id=None,
        ),
        Proposal(
            operation="dispatch",
            mission_id=MISSION_ID,
            cost_usd=over,
            reservation_id="",
        ),
        Proposal(
            operation="dispatch",
            mission_id=MISSION_ID,
            cost_usd=over,
            reservation_id="missing",
        ),
        Proposal(
            operation="dispatch",
            mission_id=MISSION_ID,
            cost_usd=over,
            reservation_id="other",
        ),
        Proposal(
            operation="command",
            command_type="read_state",
            action_class="read_state",
            mission_id=MISSION_ID,
            cost_usd=Decimal("1.00"),
            reservation_id=None,
        ),
    )
    for proposal in cases:
        _expect(paid_work_gate, proposal, ctx, "reservation_missing")
    assert admission_control(cases[-1], ctx) is None


def test_paid_work_gate_budget_overrun() -> None:
    ctx = _ctx(
        reservations={
            "res-1": ReservationFact(mission_id=MISSION_ID, remaining_usd=Decimal("10.00")),
        }
    )
    _expect(
        paid_work_gate,
        Proposal(
            operation="dispatch",
            mission_id=MISSION_ID,
            reservation_id="res-1",
            cost_usd=Decimal("10.01"),
        ),
        ctx,
        "budget_overrun",
        decision=True,
    )


def test_paid_work_gate_passes() -> None:
    ctx = _ctx(
        reservations={
            "res-1": ReservationFact(mission_id=MISSION_ID, remaining_usd=Decimal("10.00")),
        }
    )
    assert (
        paid_work_gate(
            Proposal(
                operation="dispatch",
                mission_id=MISSION_ID,
                reservation_id="res-1",
                cost_usd=Decimal("10.00"),
            ),
            ctx,
        )
        is None
    )
    assert (
        paid_work_gate(
            Proposal(
                operation="command",
                command_type="read_state",
                action_class="read_state",
                mission_id=MISSION_ID,
                reservation_id="res-1",
                cost_usd=Decimal("1.00"),
            ),
            ctx,
        )
        is None
    )


def test_acceptance_semantics_acceptance_unknown() -> None:
    fact = _passing_fact()
    ctx = _ctx(acceptance={"known": fact})
    for command_type in ("complete_work", "complete_execution_item"):
        _expect(
            acceptance_semantics,
            Proposal(command_type=command_type, target_id=None),
            ctx,
            "acceptance_unknown",
        )
        _expect(
            acceptance_semantics,
            Proposal(command_type=command_type, target_id="missing"),
            ctx,
            "acceptance_unknown",
        )


def test_acceptance_semantics_criteria_not_sealed() -> None:
    # Verdict and reviewer would also fail; empty criteria is the earlier refusal.
    ctx = _ctx(
        acceptance={
            "item-1": AcceptanceFact(
                sealed_criteria=(),
                evidence=(),
                author_id="author-1",
                reviewer_id=None,
                verdict="FAIL",
            )
        }
    )
    _expect(
        acceptance_semantics,
        Proposal(command_type="complete_work", target_id="item-1"),
        ctx,
        "criteria_not_sealed",
    )


def test_acceptance_semantics_evidence_missing() -> None:
    # Verdict and reviewer would also fail; missing refs is the earlier refusal.
    facts = {
        "partial": AcceptanceFact(
            sealed_criteria=("c1", "c2"),
            evidence={"c1": ("e1",)},
            author_id="author-1",
            reviewer_id=None,
            verdict="FAIL",
        ),
        "empty": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence={"c1": ()},
            author_id="author-1",
            reviewer_id=None,
            verdict="FAIL",
        ),
        "blank": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence={"c1": ("",)},
            author_id="author-1",
            reviewer_id=None,
            verdict="FAIL",
        ),
        "unmapped": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence=(),
            author_id="author-1",
            reviewer_id=None,
            verdict="FAIL",
        ),
    }
    ctx = _ctx(acceptance=facts)
    for target_id in facts:
        _expect(
            acceptance_semantics,
            Proposal(command_type="complete_work", target_id=target_id),
            ctx,
            "evidence_missing",
        )


def test_acceptance_semantics_review_not_passed() -> None:
    # Reviewer is missing too; the verdict is the earlier refusal.
    for verdict in ("FAIL", "pass", None):
        ctx = _ctx(
            acceptance={
                "item-1": AcceptanceFact(
                    sealed_criteria=("c1",),
                    evidence={"c1": ("e1",)},
                    author_id="author-1",
                    reviewer_id=None,
                    verdict=verdict,
                )
            }
        )
        _expect(
            acceptance_semantics,
            Proposal(command_type="complete_work", target_id="item-1"),
            ctx,
            "review_not_passed",
        )


def test_acceptance_semantics_self_review() -> None:
    author = UUID("00000000-0000-0000-0000-0000000000b1")
    facts = {
        "no-reviewer": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence={"c1": ("e1",)},
            author_id="author-1",
            reviewer_id=None,
            verdict="PASS",
        ),
        "same": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence={"c1": ("e1",)},
            author_id="author-1",
            reviewer_id="author-1",
            verdict="PASS",
        ),
        "same-uuid-string": AcceptanceFact(
            sealed_criteria=("c1",),
            evidence={"c1": ("e1",)},
            author_id=author,
            reviewer_id=str(author),
            verdict="PASS",
        ),
    }
    ctx = _ctx(acceptance=facts)
    for target_id in facts:
        _expect(
            acceptance_semantics,
            Proposal(command_type="complete_execution_item", target_id=target_id),
            ctx,
            "self_review",
        )


def test_acceptance_semantics_passes() -> None:
    ctx = _ctx(acceptance={"item-1": _passing_fact()})
    for command_type in ("complete_work", "complete_execution_item"):
        assert (
            acceptance_semantics(
                Proposal(command_type=command_type, target_id="item-1"),
                ctx,
            )
            is None
        )


def test_begin_execution_without_reservation_refused_by_both_gates() -> None:
    assert PAID_WORK_COMMANDS == frozenset(
        {
            "begin_execution",
            "reserve_auditor_launch",
            "claim_research_replicate",
            "admit_research_campaign",
        }
    )
    proposal = Proposal(
        operation="command",
        command_type="begin_execution",
        mission_id=MISSION_ID,
        effort="E1",
        cost_usd=Decimal("1.00"),
        reservation_id=None,
    )
    verdict = evaluate(
        proposal,
        _ctx(missions={MISSION_ID: _mission(in_flight=0, capacity=1)}),
        GATES,
    )
    assert [(item.check, item.code, item.raise_decision) for item in verdict.refusals] == [
        ("admission_control", "reservation_missing", False),
        ("paid_work_gate", "reservation_missing", False),
    ]


def test_non_paid_tier1_command_without_cost_passes() -> None:
    assert "read_state" in TIER1
    assert "read_state" not in PAID_WORK_COMMANDS
    proposal = Proposal(
        operation="command",
        command_type="read_state",
        action_class="read_state",
        cost_usd=None,
        mission_id=None,
        reservation_id=None,
        target_id=None,
        effort=None,
    )
    verdict = evaluate(proposal, _ctx(), GATES)
    assert verdict.allowed is True
    assert verdict.refusals == ()
    assert verdict.raises_decision is False
