"""Admission, paid-work, and acceptance checks for control-plane proposals.

A proposal is paid work when ``operation == "dispatch"`` or ``command_type``
is in ``PAID_WORK_COMMANDS``. ``admission_control`` applies to paid work.
``paid_work_gate`` applies to paid work and to any proposal with ``cost_usd``.
``acceptance_semantics`` applies when ``command_type`` is ``complete_work`` or
``complete_execution_item``. Every other proposal passes all three checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from omp_work.control_plane.gate import (
    AcceptanceFact,
    Check,
    CheckContext,
    Proposal,
    Refusal,
)
from omp_work.economy_effort import validate_effort

__all__ = [
    "PAID_WORK_COMMANDS",
    "acceptance_semantics",
    "admission_control",
    "paid_work_gate",
]

PAID_WORK_COMMANDS = frozenset(
    {
        "begin_execution",
        "reserve_auditor_launch",
        "claim_research_replicate",
        "admit_research_campaign",
    }
)

_ACCEPTANCE_COMMANDS = frozenset({"complete_work", "complete_execution_item"})


def _is_paid_work(proposal: Proposal) -> bool:
    return proposal.operation == "dispatch" or proposal.command_type in PAID_WORK_COMMANDS


def _refuse(check: str, code: str, *, decision: bool = False) -> Refusal:
    return Refusal(check=check, code=code, raise_decision=decision)


def _admission_control(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Refuse paid work that lacks an approved mission, effort, reservation, or capacity.

    Order is mission_not_approved, effort_invalid, reservation_missing,
    capacity_full. None of these raise a decision.
    """
    if not _is_paid_work(proposal):
        return None
    mission_id = proposal.mission_id
    mission = None if mission_id is None else ctx.facts.missions.get(mission_id)
    if mission is None or not mission.approved:
        return _refuse("admission_control", "mission_not_approved")
    if not validate_effort(proposal.effort, proposal.effort_reason).get("ok"):
        return _refuse("admission_control", "effort_invalid")
    if not proposal.reservation_id:
        return _refuse("admission_control", "reservation_missing")
    if mission.in_flight >= mission.capacity:
        return _refuse("admission_control", "capacity_full")
    return None


def _paid_work_gate(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Refuse paid work, and any priced proposal, that lacks cost or budget.

    Order is cost_missing, reservation_missing, budget_overrun. Only
    budget_overrun (cost_usd greater than the reservation's remaining_usd)
    raises a decision. A reservation for another mission is reservation_missing.
    """
    if not _is_paid_work(proposal) and proposal.cost_usd is None:
        return None
    if proposal.cost_usd is None:
        return _refuse("paid_work_gate", "cost_missing")
    reservation_id = proposal.reservation_id
    reservation = (
        ctx.facts.reservations.get(reservation_id) if reservation_id else None
    )
    if reservation is None or reservation.mission_id != proposal.mission_id:
        return _refuse("paid_work_gate", "reservation_missing")
    if proposal.cost_usd > reservation.remaining_usd:
        return _refuse("paid_work_gate", "budget_overrun", decision=True)
    return None


def _has_evidence_refs(evidence: object, criterion: str) -> bool:
    """True when evidence maps the criterion to at least one non-empty string ref."""
    if not isinstance(evidence, Mapping):
        return False
    return _refs_nonempty(evidence.get(criterion))


def _refs_nonempty(refs: object) -> bool:
    if isinstance(refs, str):
        return bool(refs)
    if isinstance(refs, Iterable) and not isinstance(refs, Mapping):
        return any(isinstance(item, str) and item for item in refs)
    return False


def _same_actor(left: object, right: object) -> bool:
    return left == right or str(left) == str(right)


def _acceptance_semantics(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Refuse completion that lacks sealed criteria, evidence, or an independent PASS.

    Applies to complete_work and complete_execution_item. Order is
    acceptance_unknown, criteria_not_sealed, evidence_missing,
    review_not_passed, self_review. None of these raise a decision.
    """
    if proposal.command_type not in _ACCEPTANCE_COMMANDS:
        return None
    target_id = proposal.target_id
    fact: AcceptanceFact | None = (
        None if target_id is None else ctx.facts.acceptance.get(target_id)
    )
    if fact is None:
        return _refuse("acceptance_semantics", "acceptance_unknown")
    if not fact.sealed_criteria:
        return _refuse("acceptance_semantics", "criteria_not_sealed")
    if any(
        not _has_evidence_refs(fact.evidence, criterion)
        for criterion in fact.sealed_criteria
    ):
        return _refuse("acceptance_semantics", "evidence_missing")
    if fact.verdict != "PASS":
        return _refuse("acceptance_semantics", "review_not_passed")
    if fact.reviewer_id is None or _same_actor(fact.reviewer_id, fact.author_id):
        return _refuse("acceptance_semantics", "self_review")
    return None


admission_control = Check(name="admission_control", fn=_admission_control)
paid_work_gate = Check(name="paid_work_gate", fn=_paid_work_gate)
acceptance_semantics = Check(name="acceptance_semantics", fn=_acceptance_semantics)
