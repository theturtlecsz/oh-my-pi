"""Fixed control-plane check list and the facts handle the service evaluates.

``CONTROL_PLANE_CHECKS`` is the eleven checks, in module order: mutation,
work gates, ADR 0004, the tier gate, then lock integrity. ``INVARIANT_CHECKS``
names the Run Owner invariant or ADR 0004 rule each check enforces.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from omp_work.control_plane.adr0004 import contract_freeze, high_risk_review
from omp_work.control_plane.envelope import tier_gate
from omp_work.control_plane.gate import Check, ControlPlaneFacts
from omp_work.control_plane.locks import lock_integrity
from omp_work.control_plane.mutation import (
    authoritative_state,
    provenance_present,
    single_mutation_path,
    worker_lifecycle,
)
from omp_work.control_plane.work_gates import (
    acceptance_semantics,
    admission_control,
    paid_work_gate,
)

__all__ = [
    "CONTROL_PLANE_CHECKS",
    "INVARIANT_CHECKS",
    "ControlPlane",
]

# Module order: mutation, work_gates, adr0004, envelope, locks.
CONTROL_PLANE_CHECKS: tuple[Check, ...] = (
    single_mutation_path,
    provenance_present,
    authoritative_state,
    worker_lifecycle,
    admission_control,
    paid_work_gate,
    acceptance_semantics,
    high_risk_review,
    contract_freeze,
    tier_gate,
    lock_integrity,
)

# Invariant or ADR 0004 rule -> the check name that enforces it.
INVARIANT_CHECKS: Mapping[str, str] = MappingProxyType(
    {
        "Single mutation authority": "single_mutation_path",
        "Provenance": "provenance_present",
        "Authoritative state": "authoritative_state",
        "Worker lifecycle": "worker_lifecycle",
        "Admission control": "admission_control",
        "Budget policy": "paid_work_gate",
        "Acceptance semantics": "acceptance_semantics",
        "Contract freeze": "contract_freeze",
        "Sole mutator": "single_mutation_path",
        "Effort launch-gate": "admission_control",
        "reviewer_required": "high_risk_review",
        "Job API freeze": "contract_freeze",
        "Protected-action gate": "tier_gate",
        "Fail closed": "tier_gate",
        "Lock integrity": "lock_integrity",
    }
)


@dataclass(frozen=True)
class ControlPlane:
    """Workspace facts, the owner-signature verifier, and the evaluate budget.

    ``facts(workspace_id)`` returns the :class:`ControlPlaneFacts` for that
    workspace. ``verify_signature`` is passed to the check context.
    ``budget_seconds`` and ``clock`` are passed to :func:`gate.evaluate`.
    """

    facts: Callable[[UUID], ControlPlaneFacts]
    verify_signature: Callable[[bytes, str], bool] | None = None
    budget_seconds: float = 10.0
    clock: Callable[[], float] | None = None
