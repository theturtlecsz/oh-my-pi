"""R18 qualification checks and the research usage ledger (OMP-318).

Pure qualification gates for preregistered performance claims, plus the one
accounting writer the evaluator path uses. ``ResourceAccounting`` accumulates
compute seconds, model tokens, and spend against a ``ResourceLimits`` budget
and mirrors every event into ``omp_jobs.usage_events`` through
``jobs.usage.record_usage`` — the crossing event is still written, so the
ledger records exactly the spend that tripped the quota. ``refuse_reason``
exposes the admission-gate surface: once any axis is above its limit the gate
answers ``quota_exceeded``.

The comparison helpers keep R18 honest: a preregistered comparison carries no
metric values, only identity and direction; resources must match on cpu,
memory_mib, gpu, and model_calls; both canonical receipts must verify against
the preregistered protocol digest; and a 10x-or-better speedup demands at
least two independent runs. ``protected_confirmation`` maps receipt
verification onto the negative-acceptance contract, ``inject_fault`` /
``audit_fault_tolerance`` bound the fault-injection evidence, and
``run_canary`` refuses a release whose artifact identity differs from what was
tested before comparing drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from omp_work.jobs.usage import record_usage
from omp_work.research.receipt import EvaluationProtocol, TrialReceipt, verify_receipt
from omp_work.standing_change import (
    ChangeAuthority,
    ChangeKind,
    authorize_standing_change,
)
from omp_work.standing_policy import PolicyRefused, StandingPolicy, policy_change_kind

__all__ = [
    "FAULT_KINDS",
    "PreregisteredComparison",
    "ResourceAccounting",
    "ResourceLimits",
    "audit_fault_tolerance",
    "evaluate_comparison",
    "inject_fault",
    "intervene_policy",
    "matched_resources",
    "protected_confirmation",
    "register_comparison",
    "run_canary",
]

#: The only ledger write this module performs: evaluator usage on the
#: research price sheet.
_LEDGER_ROLE = "evaluator"
_LEDGER_MODEL = "research"
_LEDGER_PRICE_VERSION = "research-m4"

#: Resource axes a comparison must match on, compared as ints.
_RESOURCE_AXES = ("cpu", "memory_mib", "gpu", "model_calls")

FAULT_KINDS: frozenset[str] = frozenset({"crash", "timeout", "corrupt_receipt"})

_UNSUPPORTED_CLAIM_SPEEDUP = 10.0
_UNSUPPORTED_CLAIM_MIN_N = 2


@dataclass(frozen=True)
class ResourceLimits:
    max_wall_clock_seconds: float
    max_model_tokens: int
    max_usd: Decimal


class ResourceAccounting:
    """Accumulates R18 usage against limits and mirrors it into the ledger."""

    def __init__(
        self,
        limits: ResourceLimits,
        store: object | None = None,
        workspace_id: object | None = None,
        actor_id: object | None = None,
    ) -> None:
        self._limits = limits
        self._store = store
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._compute_seconds = 0.0
        self._tokens = 0
        self._usd = Decimal("0")

    @property
    def quota_exceeded(self) -> bool:
        limits = self._limits
        return (
            self._compute_seconds > limits.max_wall_clock_seconds
            or self._tokens > limits.max_model_tokens
            or self._usd > limits.max_usd
        )

    def record(
        self,
        *,
        operation_id: str,
        work_id: object,
        job_id: str | None,
        request_id: str,
        wall_clock_seconds: float,
        tokens: int,
        usd: Decimal | str,
    ) -> dict[str, Any] | None:
        """Accumulate one usage event; write it to the ledger when a store is bound.

        The event is accumulated and written even when it crosses the quota, so
        the ledger records the spend that tripped the limit. Returns the
        ``record_usage`` result dict, or None when no store is bound.
        """
        self._compute_seconds += float(wall_clock_seconds)
        self._tokens += int(tokens)
        self._usd += Decimal(usd)
        if self._store is None:
            return None
        return record_usage(
            self._store,
            operation_id=operation_id,
            workspace_id=self._workspace_id,
            actor_id=self._actor_id,
            work_id=work_id,
            job_id=job_id,
            request_id=request_id,
            role=_LEDGER_ROLE,
            model=_LEDGER_MODEL,
            input_tokens=tokens,
            output_tokens=0,
            cache_tokens=0,
            measurement="measured",
            price_usd=str(usd),
            price_version=_LEDGER_PRICE_VERSION,
        )

    def refuse_reason(self, now: datetime | None = None) -> Literal["quota_exceeded"] | None:
        """Admission-gate surface: refuse once any axis is above its limit."""
        return "quota_exceeded" if self.quota_exceeded else None

    def total_usage(self) -> dict[str, Any]:
        return {
            "compute_seconds": self._compute_seconds,
            "tokens": self._tokens,
            "usd": str(self._usd),
            "quota_exceeded": self.quota_exceeded,
        }


@dataclass(frozen=True)
class PreregisteredComparison:
    """A preregistered comparison binds identity and direction, never values."""

    comparison_id: str
    protocol_sha256: str
    primary: str
    direction: Literal["higher", "lower"]
    independent_n: int


def register_comparison(
    comparison_id: str,
    protocol_sha256: str,
    primary: str,
    direction: Literal["higher", "lower"],
    independent_n: int,
) -> PreregisteredComparison:
    return PreregisteredComparison(
        comparison_id=comparison_id,
        protocol_sha256=protocol_sha256,
        primary=primary,
        direction=direction,
        independent_n=independent_n,
    )


def matched_resources(
    baseline: dict[str, int], candidate: dict[str, int]
) -> Literal["matched", "invalid_comparison"]:
    """A comparison is valid only when both sides ran on identical resources."""
    for axis in _RESOURCE_AXES:
        if int(baseline[axis]) != int(candidate[axis]):
            return "invalid_comparison"
    return "matched"


def _invalid_comparison(independent_n: int) -> dict[str, Any]:
    return {
        "status": "invalid_comparison",
        "independent_n": independent_n,
        "successes": 0,
        "failures": 0,
        "denominator": 0,
    }


def evaluate_comparison(
    prereg: PreregisteredComparison,
    baseline: TrialReceipt,
    candidate: TrialReceipt,
    baseline_resources: dict[str, int],
    candidate_resources: dict[str, int],
) -> dict[str, Any]:
    """Compare two canonical receipts against a preregistered comparison.

    Mismatched resources, a receipt that fails verification, or a protocol
    digest other than the preregistered one invalidate the comparison. A
    speedup of 10x or better requires at least two independent runs.
    """
    if matched_resources(baseline_resources, candidate_resources) != "matched":
        return _invalid_comparison(prereg.independent_n)
    if verify_receipt(baseline.receipt_bytes()) or verify_receipt(
        candidate.receipt_bytes()
    ):
        return _invalid_comparison(prereg.independent_n)
    if (
        baseline.protocol_sha256 != prereg.protocol_sha256
        or candidate.protocol_sha256 != prereg.protocol_sha256
    ):
        return _invalid_comparison(prereg.independent_n)

    baseline_value = baseline.metrics[prereg.primary]
    candidate_value = candidate.metrics[prereg.primary]
    if prereg.direction == "higher":
        speedup = candidate_value / baseline_value
    else:
        speedup = baseline_value / candidate_value

    successes = int(baseline.verdict == "qualified") + int(
        candidate.verdict == "qualified"
    )
    failures = 2 - successes
    denominator = successes + failures

    if (
        speedup >= _UNSUPPORTED_CLAIM_SPEEDUP
        and prereg.independent_n < _UNSUPPORTED_CLAIM_MIN_N
    ):
        raise ValueError("unsupported_claim")

    return {
        "status": "compared",
        "speedup": speedup,
        "independent_n": prereg.independent_n,
        "successes": successes,
        "failures": failures,
        "denominator": denominator,
    }


def protected_confirmation(
    receipt: TrialReceipt, protocol: EvaluationProtocol
) -> dict[str, Any]:
    """Verify a receipt under its protocol and map the negative-control result."""
    reasons = verify_receipt(receipt.receipt_bytes(), protocol=protocol)
    if (
        receipt.negative_control == "accepted"
        or "negative_control_accepted" in reasons
    ):
        return {"status": "negative_acceptance", "reasons": reasons}
    if reasons:
        return {"status": "rejected", "reasons": reasons}
    return {"status": "confirmed", "reasons": []}


def inject_fault(kind: str) -> dict[str, Any]:
    """Record one injected fault; only the declared fault kinds exist."""
    if kind not in FAULT_KINDS:
        raise ValueError("unknown_fault")
    return {"ok": False, "fault": kind}


def audit_fault_tolerance(
    results: list[dict[str, Any]], claimed_speedup: float | None = None
) -> dict[str, Any]:
    """Publish the fault-injection denominator and refuse a lone 10x claim."""
    if (
        claimed_speedup is not None
        and claimed_speedup >= _UNSUPPORTED_CLAIM_SPEEDUP
        and len(results) < _UNSUPPORTED_CLAIM_MIN_N
    ):
        raise ValueError("unsupported_claim")
    successes = 0
    failure_kinds: list[Any] = []
    for result in results:
        if result.get("ok"):
            successes += 1
        else:
            failure_kinds.append(result.get("fault"))
    return {
        "successes": successes,
        "failures": len(results) - successes,
        "denominator": len(results),
        "failure_kinds": failure_kinds,
    }


def intervene_policy(
    old: StandingPolicy, new: StandingPolicy, authority: ChangeAuthority
) -> StandingPolicy:
    """An intervention may only narrow a standing policy, under authority rules."""
    if policy_change_kind(old, new) is not ChangeKind.narrow:
        raise PolicyRefused("intervention_must_narrow")
    authorize_standing_change(ChangeKind.narrow, authority)
    return new


def run_canary(
    reference_primary: float,
    observed_primary: float,
    tolerance: float,
    tested_artifact_sha256: str,
    release_artifact_sha256: str,
) -> dict[str, Any]:
    """Compare an operating canary against its reference within tolerance.

    The release artifact must be the artifact that was tested; relative drift
    beyond the tolerance is drift_detected.
    """
    if tested_artifact_sha256 != release_artifact_sha256:
        raise ValueError("release_identity_mismatch")
    if not 0.0 <= tolerance <= 1.0:
        raise ValueError("tolerance")
    drift = abs(observed_primary - reference_primary) / (abs(reference_primary) or 1.0)
    return {"status": "drift_detected" if drift > tolerance else "ok"}
