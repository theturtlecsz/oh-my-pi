"""OMP-423-s01: Mission standing view: mission snapshot plus pending decisions and jobs in flight."""

from __future__ import annotations

from uuid import UUID

import psycopg

from . import decision_records
from .missions import read_mission
from .store_shared import row_json


def mission_standing(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    mission_id: UUID | str,
) -> dict[str, object]:
    """Return read_mission(...) plus pending_decisions and jobs_in_flight."""
    if not isinstance(workspace_id, UUID):
        workspace_id = UUID(str(workspace_id))

    mission = read_mission(cur, workspace_id, mission_id)
    mission_uuid_str = str(mission["mission_id"])
    pending_decisions = decision_records.list_decisions(
        cur,
        workspace_id,
        status="pending",
        mission_id=mission_uuid_str,
    )

    links = mission.get("links") or []
    link_work_ids: list[UUID] = []
    if isinstance(links, (list, tuple)):
        for link in links:
            if isinstance(link, dict) and "work_id" in link:
                wid = link["work_id"]
                if isinstance(wid, UUID):
                    link_work_ids.append(wid)
                elif isinstance(wid, str):
                    try:
                        link_work_ids.append(UUID(wid))
                    except (ValueError, TypeError):
                        pass

    if not link_work_ids:
        jobs_in_flight: list[dict[str, object]] = []
    else:
        cur.execute(
            "SELECT job_id, work_id, kind, status, attempt, lease_expires_at"
            " FROM omp_jobs.jobs"
            " WHERE workspace_id=%s AND source='native' AND status IN ('backlog','admitted') AND work_id = ANY(%s)"
            " ORDER BY created_at, job_id",
            (workspace_id, link_work_ids),
        )
        jobs_in_flight = [
            r for row in cur.fetchall() if (r := row_json(row)) is not None
        ]

    result = dict(mission)
    result["pending_decisions"] = pending_decisions
    result["jobs_in_flight"] = jobs_in_flight
    return result
