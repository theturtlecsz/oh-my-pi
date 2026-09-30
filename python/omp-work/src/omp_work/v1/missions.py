"""Mission submit and read. State is the mission snapshot on the latest applied
mission event (no mission table).
"""

from __future__ import annotations

import json
from uuid import UUID

import psycopg

from omp_work.mission_budget import admit_mission_budget

from .api_models import MissionDrawn, MissionTransition, MissionView
from .models import CommandEnvelope, MissionStatus, SubmitMissionCommand
from .store_shared import WorkStoreError

_MISSION_EVENTS = (
    "submit_mission",
    "revise_mission",
    "approve_mission",
    "set_mission_status",
    "link_mission_work",
)

_NEW_SCOPE_RULE = "D29.new_scope"


def execute(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, SubmitMissionCommand):
        raise WorkStoreError("unavailable")
    return _submit(cur, envelope, actor_id, actor_kind)


def read_mission(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    mission_id: UUID | str,
) -> dict[str, object]:
    """The mission on the latest applied mission event, or invalid_request."""
    aggregate_id = _parse_id(mission_id)
    mission = _latest_mission(cur, workspace_id, aggregate_id)
    if mission is None:
        raise WorkStoreError("invalid_request")
    return mission


def _submit(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    assert isinstance(command, SubmitMissionCommand)
    draft = command.payload.draft
    mission_id = command.payload.mission_id
    workspace_id = envelope.workspace_id

    cur.execute(
        "SELECT provenance FROM omp_work.projects WHERE workspace_id=%s AND project_id=%s",
        (workspace_id, draft.project_id),
    )
    project = cur.fetchone()
    if project is None:
        raise WorkStoreError("invalid_request")
    if draft.continuation_of is not None and not _has_event(
        cur, workspace_id, draft.continuation_of
    ):
        raise WorkStoreError("invalid_request")
    if draft.parent_mission is not None and not _has_event(
        cur, workspace_id, draft.parent_mission
    ):
        raise WorkStoreError("invalid_request")
    if _has_event(cur, workspace_id, mission_id):
        raise WorkStoreError("revision_conflict")

    if draft.budget_policy is not None:
        effective: object = draft.budget_policy.model_dump(mode="json")
        budget_source: str | None = "mission"
    else:
        standing = _standing_budget(project["provenance"])
        if standing is None:
            effective = None
            budget_source = None
        else:
            effective = standing
            budget_source = "project"

    admission = admit_mission_budget(
        {
            "mission_id": str(mission_id),
            "project_id": str(draft.project_id),
            "budget_policy": effective,
        }
    )
    cur.execute("SELECT clock_timestamp() AS created_at")
    created_row = cur.fetchone()
    if created_row is None:
        raise WorkStoreError("unavailable")
    created_at = created_row["created_at"]

    if admission.state == "held":
        hold = _json_safe(admission.decision)
        if not isinstance(hold, dict):
            raise WorkStoreError("unavailable")
        cause_id = str(hold["decision_id"])
        status = MissionStatus.BLOCKED
        cause_kind = "decision"
        budget = None
        budget_source = None
    else:
        hold = None
        cause_id = _NEW_SCOPE_RULE
        status = MissionStatus.AWAITING_CONFIRMATION
        cause_kind = "policy_rule"
        budget = admission.budget

    transition = MissionTransition(
        from_status=MissionStatus.DRAFT,
        to_status=status,
        cause_kind=cause_kind,
        cause_id=cause_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        at=created_at,
        revision=1,
    )
    view = MissionView(
        **draft.model_dump(),
        mission_id=mission_id,
        created_by=actor_id,
        created_at=created_at,
        revision=1,
        status=status,
        budget=budget,
        budget_source=budget_source,
        hold_decision=hold,
        transitions=(transition,),
        drawn=MissionDrawn(usd="0", tokens=0, wall_clock_seconds=0),
    )
    mission = view.model_dump(mode="json")
    # Store the form CommandResponse will emit, so a later read matches the POST.
    mission = MissionView.model_validate(mission).model_dump(mode="json")
    return {"type": "submit_mission", "mission": mission}


def _standing_budget(provenance: object) -> object | None:
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    if not isinstance(provenance, dict):
        return None
    return provenance.get("standing_budget")


def _has_event(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, mission_id: UUID
) -> bool:
    cur.execute(
        "SELECT 1 FROM omp_audit.domain_events"
        " WHERE workspace_id=%s AND aggregate_type='mission' AND aggregate_id=%s"
        " LIMIT 1",
        (workspace_id, mission_id),
    )
    return cur.fetchone() is not None


def _latest_mission(
    cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, mission_id: UUID
) -> dict[str, object] | None:
    cur.execute(
        "SELECT payload FROM omp_audit.domain_events"
        " WHERE workspace_id=%s AND aggregate_type='mission' AND aggregate_id=%s"
        " AND outcome='applied' AND event_type = ANY(%s)"
        " ORDER BY sequence DESC LIMIT 1",
        (workspace_id, mission_id, list(_MISSION_EVENTS)),
    )
    row = cur.fetchone()
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    mission = payload.get("mission")
    if not isinstance(mission, dict):
        return None
    return mission


def _parse_id(mission_id: UUID | str) -> UUID:
    if isinstance(mission_id, UUID):
        return mission_id
    try:
        return UUID(mission_id)
    except ValueError:
        raise WorkStoreError("invalid_request") from None


def _json_safe(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
