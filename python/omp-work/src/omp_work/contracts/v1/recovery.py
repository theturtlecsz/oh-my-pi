"""R03 recovery helpers for v1 completion/closeout recoverability (decision 0002).

Mechanical implementation by Run Owner after paid empty-soft failures (C14/C16/C19 aborted).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CloseoutState = Literal["open", "committed", "acknowledged", "closed", "failed"]


@dataclass
class CloseoutRecord:
    id: str
    state: CloseoutState
    revision: int = 0
    ack_token: str | None = None


class RecoveryError(ValueError):
    pass


def apply_commit(record: CloseoutRecord, *, next_revision: int | None = None) -> CloseoutRecord:
    """Commit closeout attempt — durable intent before ack."""
    if record.state not in ("open", "failed"):
        raise RecoveryError(f"cannot commit from state={record.state}")
    rev = record.revision + 1 if next_revision is None else next_revision
    if rev <= record.revision:
        raise RecoveryError("revision must advance on commit")
    return CloseoutRecord(id=record.id, state="committed", revision=rev, ack_token=None)


def recover_unacked_commit(record: CloseoutRecord, *, evidence_trusted: bool) -> CloseoutRecord:
    """Recover after committed-but-unacknowledged: do not invent scientific success.

    If evidence is untrusted, recovery may re-ack path or fail closed — never jump to closed-success.
    """
    if record.state != "committed":
        raise RecoveryError(f"recover_unacked_commit requires committed, got {record.state}")
    if not evidence_trusted:
        # Fail closed: remain committed/failed for retry — execution status ≠ scientific success
        return CloseoutRecord(id=record.id, state="failed", revision=record.revision, ack_token=None)
    return CloseoutRecord(id=record.id, state="acknowledged", revision=record.revision, ack_token=f"ack-{record.revision}")


def acknowledge(record: CloseoutRecord, ack_token: str) -> CloseoutRecord:
    if record.state != "acknowledged" and record.state != "committed":
        # allow ack from committed if token presented after trusted recovery
        if record.state != "committed":
            raise RecoveryError(f"cannot ack from state={record.state}")
    if record.state == "committed":
        # direct ack only when caller already proved trust out-of-band
        return CloseoutRecord(id=record.id, state="acknowledged", revision=record.revision, ack_token=ack_token)
    if record.ack_token and record.ack_token != ack_token:
        raise RecoveryError("ack token mismatch")
    return CloseoutRecord(id=record.id, state="acknowledged", revision=record.revision, ack_token=ack_token)


def close(record: CloseoutRecord) -> CloseoutRecord:
    if record.state != "acknowledged":
        raise RecoveryError("close requires acknowledged (committed-unacked must recover first)")
    return CloseoutRecord(id=record.id, state="closed", revision=record.revision, ack_token=record.ack_token)


def execution_status_is_not_scientific_success(closeout_state: str, evidence_trust: str) -> None:
    if closeout_state in ("committed", "acknowledged", "closed") and evidence_trust == "untrusted":
        if closeout_state == "closed":
            raise RecoveryError("untrusted evidence cannot yield closed scientific success")
