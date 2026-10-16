"""Append-only launch coordinates captured when a mission plan is stamped."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import Cursor

from . import intake_binding, intake_gate


@dataclass(frozen=True)
class LaunchBinding:
    launch_binding_id: UUID
    workspace_id: UUID
    mission_id: UUID
    intake_binding_id: UUID
    run_id: str
    revision: int
    assessment_id: str
    plan_receipt_id: str
    plan_sha256: str
    item_id: str
    work_id: UUID
    candidate_id: str
    state: str
    hold_code: str | None
    created_at: datetime


def read(cur: Cursor, *, workspace_id: UUID, mission_id: UUID, work_id: UUID) -> LaunchBinding | None:
    """Return the newest launch row for this work item, including held history."""
    cur.execute(
        "SELECT launch_binding_id,workspace_id,mission_id,intake_binding_id,run_id,revision,"
        "assessment_id,plan_receipt_id,plan_sha256,item_id,work_id,candidate_id,state,hold_code,created_at "
        "FROM omp_work.mission_launch_bindings "
        "WHERE workspace_id=%s AND mission_id=%s AND work_id=%s "
        "ORDER BY created_at DESC,launch_binding_id DESC LIMIT 1",
        (workspace_id, mission_id, work_id),
    )
    row = cur.fetchone()
    return None if row is None else LaunchBinding(*row)


def record(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, intake_binding_id: UUID,
    run_id: str, revision: int, assessment_id: str, plan_receipt_id: str,
    plan_sha256: str, item_id: str, work_id: UUID, candidate_id: str,
) -> UUID:
    """Append a bound row unless the item's newest row already binds these values."""
    newest = read(cur, workspace_id=workspace_id, mission_id=mission_id, work_id=work_id)
    coordinates = (intake_binding_id, run_id, revision, assessment_id, plan_receipt_id,
                   plan_sha256, item_id, candidate_id)
    if newest is not None and newest.state == "bound" and coordinates == (
        newest.intake_binding_id, newest.run_id, newest.revision, newest.assessment_id,
        newest.plan_receipt_id, newest.plan_sha256, newest.item_id, newest.candidate_id,
    ):
        return newest.launch_binding_id
    launch_binding_id = uuid4()
    cur.execute(
        "INSERT INTO omp_work.mission_launch_bindings "
        "(launch_binding_id,workspace_id,mission_id,intake_binding_id,run_id,revision,"
        "assessment_id,plan_receipt_id,plan_sha256,item_id,work_id,candidate_id,state) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'bound')",
        (launch_binding_id, workspace_id, mission_id, intake_binding_id, run_id, revision,
         assessment_id, plan_receipt_id, plan_sha256, item_id, work_id, candidate_id),
    )
    return launch_binding_id


LAUNCH_BINDING_MISSING = "launch_binding_missing"
LAUNCH_BINDING_HELD = "launch_binding_held"
LAUNCH_BINDING_MISMATCH = "launch_binding_mismatch"


@dataclass(frozen=True)
class Refusal:
    """A launch the stored binding does not authorize; ``field`` names a mismatch."""

    code: str
    hold_code: str | None = None
    field: str | None = None


def _mismatch(field: str) -> Refusal:
    return Refusal(LAUNCH_BINDING_MISMATCH, field=field)


def verify(
    cur: Cursor, *, workspace_id: UUID, mission_id: UUID, grant_id: UUID, work_id: UUID,
) -> LaunchBinding | Refusal:
    """Return the item's bound launch row only while intake and the live item still match it."""
    launch = read(cur, workspace_id=workspace_id, mission_id=mission_id, work_id=work_id)
    if launch is None:
        return Refusal(LAUNCH_BINDING_MISSING)
    if launch.state != "bound":
        return Refusal(LAUNCH_BINDING_HELD, hold_code=launch.hold_code)
    newest = intake_binding.read(cur, workspace_id=workspace_id, mission_id=mission_id)
    if newest is None:
        return _mismatch("intake_revision")
    if newest.state != "admitted":
        return Refusal(LAUNCH_BINDING_HELD, hold_code=newest.hold_code)
    if (launch.intake_binding_id, launch.run_id, launch.revision) != (
        newest.binding_id, newest.run_id, newest.revision,
    ):
        return _mismatch("intake_revision")
    if launch.assessment_id != newest.assessment_id or intake_gate.verify(
        cur, workspace_id=workspace_id, mission_id=mission_id,
        revision=launch.revision, assessment_id=launch.assessment_id,
    ) is not None:
        return _mismatch("assessment_receipt")
    cur.execute(
        "SELECT item_id,plan_stamp->>'plan_receipt_id',plan_stamp->>'plan_sha256',"
        "plan_stamp->>'candidate_id' FROM omp_work.execution_grant_items "
        "WHERE workspace_id=%s AND grant_id=%s AND work_id=%s",
        (workspace_id, grant_id, work_id),
    )
    row = cur.fetchone()
    if row is None:
        return _mismatch("work_packet")
    item_id, plan_receipt_id, plan_sha256, candidate_id = (
        None if value is None else str(value) for value in row
    )
    if (launch.plan_receipt_id, launch.plan_sha256) != (plan_receipt_id, plan_sha256):
        return _mismatch("plan")
    if launch.item_id != item_id:
        return _mismatch("work_packet")
    if launch.candidate_id != candidate_id:
        return _mismatch("candidate")
    return launch
