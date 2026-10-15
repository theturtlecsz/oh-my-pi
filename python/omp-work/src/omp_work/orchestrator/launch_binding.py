"""Append-only launch coordinates captured when a mission plan is stamped."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import Cursor


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
