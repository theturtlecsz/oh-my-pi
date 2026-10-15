"""Mission intake bindings: which revision and PASS receipt admitted a mission.

A binding is append-only history per mission. ``prepare`` records the revision a
mission is about to be planned on; ``admit`` re-checks the typed gate in
``intake_gate.verify`` and, only for a genuine PASS bound to that revision,
records the receipt that carried it. Advancing the intake head holds every open
row (``intake_repository.hold_bindings``), never rewriting its revision or
receipt, so a consumer reading the newest row sees the mission held until a new
PASS exists. The gate decision lives in the orchestrator; this module only
persists its outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import Cursor

from ..intake_repository import hold_bindings
from .intake_gate import verify

__all__ = [
    "Binding",
    "IntakeBindingRefused",
    "admit",
    "hold",
    "prepare",
    "read",
]


class IntakeBindingRefused(Exception):
    """A refused binding operation; ``code`` is the gate's stable reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Binding:
    binding_id: UUID
    run_id: str
    revision: int
    assessment_id: str | None
    state: str
    hold_code: str | None


def _append(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, run_id: str,
    revision: int, assessment_id: str | None, state: str,
) -> UUID:
    binding_id = uuid4()
    cur.execute(
        "INSERT INTO omp_work.mission_intake_bindings "
        "(binding_id,workspace_id,mission_id,run_id,revision,assessment_id,state) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (binding_id, workspace_id, mission_id, run_id, revision, assessment_id, state),
    )
    return binding_id


def prepare(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, run_id: str, revision: int,
) -> UUID:
    """Record that a mission is about to be planned on ``revision``."""
    return _append(cur, workspace_id=workspace_id, mission_id=mission_id, run_id=run_id,
                   revision=revision, assessment_id=None, state="prepared")


def admit(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, run_id: str, revision: int,
    assessment_id: str,
) -> UUID:
    """Admit a mission on ``revision`` once ``assessment_id`` is a real PASS.

    The receipt is not trusted by presence: ``verify`` re-derives the revision's
    binding and refuses a missing, stale, or forged receipt, so a planted
    assessment id surfaces as ``intake_receipt_forged``.
    """
    refusal = verify(cur, workspace_id=workspace_id, mission_id=mission_id,
                     revision=revision, assessment_id=assessment_id)
    if refusal is not None:
        raise IntakeBindingRefused(refusal.code)
    return _append(cur, workspace_id=workspace_id, mission_id=mission_id, run_id=run_id,
                   revision=revision, assessment_id=assessment_id, state="admitted")


def read(cur: Cursor, *, workspace_id: UUID, mission_id: UUID) -> Binding | None:
    """The newest binding row for the mission, or ``None`` when it has none."""
    cur.execute(
        "SELECT binding_id,run_id,revision,assessment_id,state,hold_code "
        "FROM omp_work.mission_intake_bindings "
        "WHERE workspace_id=%s AND mission_id=%s "
        "ORDER BY created_at DESC,binding_id DESC LIMIT 1",
        (workspace_id, mission_id),
    )
    row = cur.fetchone()
    return None if row is None else Binding(*row)


def hold(cur: Cursor, *, workspace_id: UUID, mission_id: UUID, code: str) -> None:
    """Hold every open binding of the mission with the stable ``code``."""
    hold_bindings(cur, workspace_id=workspace_id, mission_id=mission_id, code=code)
