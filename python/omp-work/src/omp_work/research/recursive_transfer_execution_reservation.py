"""Reserve one execution and settle only the attempts that reservation admitted.

``reserve_execution`` calls ``reserve_task`` once and leaves ``native.history``
in place. Trials admitted by that call are the attempt ids present afterward
and absent before. Preexisting attempts, cached failures, and duplicates are
not admitted. A refusal, or a reservation that did not newly admit every
qualified arm, blocks comparison.

``settle_execution`` calls ``finish_arm`` once for each newly pending qualified
trial. Success requires a successful comparison and membership in
``verified_ids``; every other pending trial fails. The accounted denominator
is ``reserve_task``'s denominator plus those ``finish_arm`` increments. Settling
the same ledger again adds zero. Old failed, cached, and duplicate identities
gain neither a success nor a denominator increment.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_freshness import PreparedTask
from omp_work.research.recursive_transfer_history import Reservation, finish_arm, reserve_task

__all__ = [
    "ExecutionSettlement",
    "ReservationLedger",
    "reserve_execution",
    "settle_execution",
]


@dataclass(frozen=True)
class ReservationLedger:
    """The original reservation plus the trials that call newly admitted."""

    reservation: Reservation
    newly_admitted_trial_ids: tuple[str, ...]
    pending_qualified_ids: tuple[str, ...]
    comparison_allowed: bool
    # One ledger is accounted once. A later settle_execution adds zero.
    _accounted: list[bool] = field(default_factory=lambda: [False], compare=False, repr=False)


@dataclass(frozen=True)
class ExecutionSettlement:
    """Denominator added by settling one ledger. A repeat is zero."""

    denominator: int


def _attempt_ids(snapshot: dict) -> set[str]:
    return {str(item) for item in snapshot.get("attempts", [])}


def _finished_ids(snapshot: dict) -> set[str]:
    return {str(item) for item in snapshot.get("successes", [])} | {
        str(item) for item in snapshot.get("failures", [])
    }


def _allows_comparison(prepared: PreparedTask, reservation: Reservation, admitted: set[str]) -> bool:
    """Block on any refusal or any arm that was not a newly admitted qualified trial."""
    if not prepared.eligible or reservation.reasons or not prepared.steps:
        return False
    selected = reservation.baseline_trial_id
    paired = reservation.candidate_trial_id
    if selected is None or paired is None:
        return False
    if str(selected) not in admitted or str(paired) not in admitted:
        return False
    for step in prepared.steps:
        for arm in (step.baseline, step.candidate):
            if arm is None or not arm.qualified or str(arm.trial_id) not in admitted:
                return False
    return True


def reserve_execution(native: NativeEvidence, prepared: PreparedTask) -> ReservationLedger:
    """Claim ``prepared`` with one ``reserve_task`` call and classify the new attempts."""
    before = native.history.snapshot()
    reservation = reserve_task(native, prepared)
    after = native.history.snapshot()
    new_attempts = _attempt_ids(after) - _attempt_ids(before)
    finished = _finished_ids(after)
    admitted: list[str] = []
    pending: list[str] = []
    seen: set[str] = set()
    for step in prepared.steps:
        for arm in (step.baseline, step.candidate):
            if arm is None:
                continue
            trial_id = str(arm.trial_id)
            # Snapshots admit an attempt. Failures, duplicates, and cached arms are absent here.
            if trial_id in seen or trial_id not in new_attempts:
                continue
            seen.add(trial_id)
            admitted.append(trial_id)
            if arm.qualified and trial_id not in finished:
                pending.append(trial_id)
    admitted_ids = tuple(admitted)
    return ReservationLedger(
        reservation,
        admitted_ids,
        tuple(pending),
        _allows_comparison(prepared, reservation, set(admitted_ids)),
    )


def settle_execution(
    native: NativeEvidence,
    ledger: ReservationLedger,
    verified_ids: Iterable[object],
    ok: bool,
) -> ExecutionSettlement:
    """Finish newly pending trials once. Unused and unsuccessful comparisons fail."""
    if ledger._accounted[0]:
        return ExecutionSettlement(0)
    done = _finished_ids(native.history.snapshot())
    pending = [trial_id for trial_id in ledger.pending_qualified_ids if trial_id not in done]
    if ledger.pending_qualified_ids and not pending:
        ledger._accounted[0] = True
        return ExecutionSettlement(0)
    verified = {str(trial_id) for trial_id in verified_ids}
    granted = bool(ok) and ledger.comparison_allowed
    added = 0
    for trial_id in pending:
        added += finish_arm(native, trial_id, ok=granted and trial_id in verified).denominator
    ledger._accounted[0] = True
    return ExecutionSettlement(ledger.reservation.denominator + added)
