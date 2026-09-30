"""Tests for omp_work.control_plane.mutation checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from omp_work.control_plane.gate import (
    Check,
    CheckContext,
    ControlPlaneFacts,
    LeaseClaim,
    LeaseFact,
    OwnerAuthorization,
    Proposal,
    Refusal,
    evaluate,
    verify_owner_authorization,
)
from omp_work.control_plane.mutation import (
    authoritative_state,
    provenance_present,
    single_mutation_path,
    worker_lifecycle,
)

_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def _facts(
    *,
    revision: int = 7,
    leases: dict[str, LeaseFact] | None = None,
) -> ControlPlaneFacts:
    return ControlPlaneFacts(now=_NOW, current_revision=revision, leases=leases or {})


def _ctx(**kwargs: object) -> CheckContext:
    return CheckContext(facts=_facts(**kwargs))  # type: ignore[arg-type]


def _refusals(check: Check, proposal: Proposal, ctx: CheckContext) -> tuple[Refusal, ...]:
    return evaluate(proposal, ctx, [check]).refusals


def _only(check: Check, proposal: Proposal, ctx: CheckContext) -> Refusal:
    refusals = _refusals(check, proposal, ctx)
    assert len(refusals) == 1
    return refusals[0]


# --------------------------------------------------------------------------
# single_mutation_path (single mutation path invariant, D35 lock 4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("proposer", ["model", "worker", "typed_command"])
def test_single_mutation_path_refuses_direct_write_whoever_proposes(proposer: str) -> None:
    proposal = Proposal(proposer=proposer, operation="direct_write")
    refusal = _only(single_mutation_path, proposal, _ctx())
    assert refusal.code == "direct_write_refused"
    assert refusal.raise_decision is False


@pytest.mark.parametrize("proposer", ["model", "worker", "typed_command"])
@pytest.mark.parametrize("operation", ["lease", "dispatch"])
def test_single_mutation_path_refuses_control_plane_operation(proposer: str, operation: str) -> None:
    proposal = Proposal(proposer=proposer, operation=operation)
    refusal = _only(single_mutation_path, proposal, _ctx())
    assert refusal.code == "control_plane_operation"
    assert refusal.raise_decision is False


def test_single_mutation_path_refuses_direct_write_from_the_service_too() -> None:
    # Only WorkService writes, but it does so directly, never as a proposal.
    proposal = Proposal(proposer="workservice", operation="direct_write")
    refusal = _only(single_mutation_path, proposal, _ctx())
    assert refusal.code == "direct_write_refused"


@pytest.mark.parametrize("operation", ["command", "lease", "dispatch"])
def test_single_mutation_path_allows_service_proposal(operation: str) -> None:
    proposal = Proposal(proposer="workservice", operation=operation)
    assert _refusals(single_mutation_path, proposal, _ctx()) == ()


# --------------------------------------------------------------------------
# provenance_present
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provenance", [None, "", "   ", "\t"])
def test_provenance_present_refuses_missing_or_blank(provenance: str | None) -> None:
    proposal = Proposal(provenance=provenance)
    refusal = _only(provenance_present, proposal, _ctx())
    assert refusal.code == "provenance_missing"
    assert refusal.raise_decision is True


def test_provenance_present_allows_non_blank_provenance() -> None:
    proposal = Proposal(provenance="workservice:run:0001")
    assert _refusals(provenance_present, proposal, _ctx()) == ()


# --------------------------------------------------------------------------
# authoritative_state
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", [None, "session", "cache", "workservice "])
def test_authoritative_state_refuses_non_workservice_basis(source: str | None) -> None:
    proposal = Proposal(basis_source=source, basis_revision=7)
    refusal = _only(authoritative_state, proposal, _ctx())
    assert refusal.code == "state_not_authoritative"
    assert refusal.raise_decision is False


@pytest.mark.parametrize("revision", [None, 6, 8])
def test_authoritative_state_refuses_missing_or_stale_revision(revision: int | None) -> None:
    proposal = Proposal(basis_source="workservice", basis_revision=revision)
    refusal = _only(authoritative_state, proposal, _ctx(revision=7))
    assert refusal.code == "stale_state"
    assert refusal.raise_decision is False


def test_authoritative_state_allows_current_revision() -> None:
    proposal = Proposal(basis_source="workservice", basis_revision=7)
    assert _refusals(authoritative_state, proposal, _ctx(revision=7)) == ()


# --------------------------------------------------------------------------
# worker_lifecycle
# --------------------------------------------------------------------------


def _lease_fact(job_id: str) -> LeaseFact:
    return LeaseFact(worker_id="w-1", fence=3, expires_at=_NOW + timedelta(minutes=5))


def test_worker_lifecycle_requires_lease_for_worker_proposer() -> None:
    proposal = Proposal(proposer="worker", operation="command")
    refusal = _only(worker_lifecycle, proposal, _ctx())
    assert refusal.code == "lease_missing"
    assert refusal.raise_decision is True


def test_worker_lifecycle_requires_lease_for_lease_operation() -> None:
    proposal = Proposal(proposer="model", operation="lease")
    refusal = _only(worker_lifecycle, proposal, _ctx())
    assert refusal.code == "lease_missing"
    assert refusal.raise_decision is True


def test_worker_lifecycle_requires_lease_for_unknown_job() -> None:
    job_id = str(uuid4())
    proposal = Proposal(proposer="worker", lease=LeaseClaim(job_id=job_id, worker_id="w-1", fence=3))
    refusal = _only(worker_lifecycle, proposal, _ctx())
    assert refusal.code == "lease_missing"
    assert refusal.raise_decision is True


@pytest.mark.parametrize(
    "worker_id,fence",
    [("w-2", 3), ("w-1", 4)],
)
def test_worker_lifecycle_refuses_reassigned_lease(worker_id: str, fence: int) -> None:
    job_id = "job-1"
    ctx = _ctx(leases={job_id: _lease_fact(job_id)})
    proposal = Proposal(
        proposer="worker",
        lease=LeaseClaim(job_id=job_id, worker_id=worker_id, fence=fence),
    )
    refusal = _only(worker_lifecycle, proposal, ctx)
    assert refusal.code == "lease_reassigned"
    assert refusal.raise_decision is True


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_worker_lifecycle_refuses_expired_lease(delta: timedelta) -> None:
    job_id = "job-1"
    fact = LeaseFact(worker_id="w-1", fence=3, expires_at=_NOW + delta)
    ctx = _ctx(leases={job_id: fact})
    proposal = Proposal(
        proposer="worker",
        lease=LeaseClaim(job_id=job_id, worker_id="w-1", fence=3),
    )
    refusal = _only(worker_lifecycle, proposal, ctx)
    assert refusal.code == "lease_expired"
    assert refusal.raise_decision is True


def test_worker_lifecycle_allows_current_lease() -> None:
    job_id = "job-1"
    ctx = _ctx(leases={job_id: _lease_fact(job_id)})
    proposal = Proposal(
        proposer="worker",
        lease=LeaseClaim(job_id=job_id, worker_id="w-1", fence=3),
    )
    assert _refusals(worker_lifecycle, proposal, ctx) == ()


def test_worker_lifecycle_not_required_for_model_command() -> None:
    proposal = Proposal(proposer="model", operation="command")
    assert _refusals(worker_lifecycle, proposal, _ctx()) == ()


# --------------------------------------------------------------------------
# An owner signature cannot cure provenance or worker lifecycle
# --------------------------------------------------------------------------


def test_owner_signature_cannot_cure_missing_provenance() -> None:
    future = _NOW + timedelta(hours=1)
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="worker",
        provenance=None,
        owner_authorization=OwnerAuthorization(signature="owner-sig", expires_at=future),
    )
    ctx = CheckContext(facts=_facts(), verify_signature=lambda message, signature: True)
    # The authorization itself is valid, but provenance_present never reads it.
    assert verify_owner_authorization(proposal, ctx) is None

    refusal = _only(provenance_present, proposal, ctx)
    assert refusal.code == "provenance_missing"
    assert refusal.raise_decision is True


def test_owner_signature_cannot_cure_missing_lease() -> None:
    future = _NOW + timedelta(hours=1)
    proposal = Proposal(
        proposal_id=uuid4(),
        proposer="worker",
        operation="command",
        owner_authorization=OwnerAuthorization(signature="owner-sig", expires_at=future),
    )
    ctx = CheckContext(facts=_facts(), verify_signature=lambda message, signature: True)
    assert verify_owner_authorization(proposal, ctx) is None

    refusal = _only(worker_lifecycle, proposal, ctx)
    assert refusal.code == "lease_missing"
    assert refusal.raise_decision is True
