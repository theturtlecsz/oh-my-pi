"""Control plane mutation checks for the Run Owner invariants.

Four checks built on :mod:`omp_work.control_plane.gate` cover the single
mutation path (D35 lock 4), provenance, authoritative state, and worker
lifecycle invariants. None of them reads ``owner_authorization``: a signature
cannot cure a missing provenance or a missing/expired/reassigned lease.
"""

from __future__ import annotations

from datetime import datetime, timezone

from omp_work.control_plane.gate import Check, CheckContext, Proposal, Refusal

__all__ = [
    "authoritative_state",
    "provenance_present",
    "single_mutation_path",
    "worker_lifecycle",
]

_AUTHORITATIVE_SOURCE = "workservice"
_DIRECT_WRITE_OPERATION = "direct_write"
_LEASE_OPERATION = "lease"
_CONTROL_PLANE_OPERATIONS = frozenset({"lease", "dispatch"})
_CONTROL_PLANE_PROPOSERS = frozenset({"model", "worker", "typed_command"})
_LEASE_PROPOSERS = frozenset({"worker"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _single_mutation_path(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Only WorkService writes; no non-service proposer may lease or dispatch."""
    if proposal.operation == _DIRECT_WRITE_OPERATION:
        return Refusal(
            check="single_mutation_path",
            code="direct_write_refused",
            raise_decision=False,
        )
    if proposal.operation in _CONTROL_PLANE_OPERATIONS and proposal.proposer in _CONTROL_PLANE_PROPOSERS:
        return Refusal(
            check="single_mutation_path",
            code="control_plane_operation",
            raise_decision=False,
        )
    return None


def _provenance_present(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Every proposal must carry non-blank provenance naming where it came from."""
    if proposal.provenance is None or not proposal.provenance.strip():
        return Refusal(
            check="provenance_present",
            code="provenance_missing",
            raise_decision=True,
        )
    return None


def _authoritative_state(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """Basis must come from WorkService at the current revision."""
    if proposal.basis_source != _AUTHORITATIVE_SOURCE:
        return Refusal(
            check="authoritative_state",
            code="state_not_authoritative",
            raise_decision=False,
        )
    if proposal.basis_revision is None or proposal.basis_revision != ctx.facts.current_revision:
        return Refusal(
            check="authoritative_state",
            code="stale_state",
            raise_decision=False,
        )
    return None


def _worker_lifecycle(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    """A worker proposal, or any lease operation, must hold the current lease."""
    if proposal.proposer not in _LEASE_PROPOSERS and proposal.operation != _LEASE_OPERATION:
        return None

    claim = proposal.lease
    if claim is None:
        return Refusal(check="worker_lifecycle", code="lease_missing", raise_decision=True)

    fact = ctx.facts.leases.get(str(claim.job_id))
    if fact is None:
        return Refusal(check="worker_lifecycle", code="lease_missing", raise_decision=True)

    if fact.worker_id != claim.worker_id or fact.fence != claim.fence:
        return Refusal(check="worker_lifecycle", code="lease_reassigned", raise_decision=True)

    expires_at = _as_utc(fact.expires_at)
    now = _as_utc(ctx.facts.now)
    if expires_at <= now:
        return Refusal(check="worker_lifecycle", code="lease_expired", raise_decision=True)
    return None


single_mutation_path = Check(name="single_mutation_path", fn=_single_mutation_path)
provenance_present = Check(name="provenance_present", fn=_provenance_present)
authoritative_state = Check(name="authoritative_state", fn=_authoritative_state)
worker_lifecycle = Check(name="worker_lifecycle", fn=_worker_lifecycle)
