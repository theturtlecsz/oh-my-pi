"""Mission submit, revise, approve, status, link, and read. State is the mission
snapshot on the latest applied mission event (no mission table).
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import psycopg

from omp_work.jobs.budget import item_budget
from omp_work.mission_budget import MissionBudgetAdmission, admit_mission_budget
from omp_work.mission_scope import (
    TERMINAL_STATUSES,
    material_cases,
    outside_envelope,
    transition_allowed,
)

from .api_models import (
    MissionApprovedScope,
    MissionDrawn,
    MissionLink,
    MissionTransition,
    MissionView,
)
from .models import (
    ApproveMissionCommand,
    CommandEnvelope,
    ItemBudget,
    LinkMissionWorkCommand,
    MissionDraft,
    MissionStatus,
    ReviseMissionCommand,
    SetMissionStatusCommand,
    SubmitMissionCommand,
)
from .store_shared import WorkStoreError

_MISSION_EVENTS = (
    "submit_mission",
    "revise_mission",
    "approve_mission",
    "set_mission_status",
    "link_mission_work",
)

_NEW_SCOPE_RULE = "D29.new_scope"
_BUDGET_SUPPLIED_RULE = "D29.budget_supplied"
_MATERIAL_RULE_PREFIX = "D29.material:"
_LINKABLE_STATUSES = frozenset(
    {MissionStatus.APPROVED, MissionStatus.RUNNING, MissionStatus.PAUSED}
)


def execute(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    if isinstance(command, SubmitMissionCommand):
        return _submit(cur, envelope, actor_id, actor_kind)
    if isinstance(command, ReviseMissionCommand):
        return _revise(cur, envelope, actor_id, actor_kind)
    if isinstance(command, ApproveMissionCommand):
        return _approve(cur, envelope, actor_id, actor_kind)
    if isinstance(command, SetMissionStatusCommand):
        return _set_status(cur, envelope, actor_id, actor_kind)
    if isinstance(command, LinkMissionWorkCommand):
        return _link(cur, envelope)
    raise WorkStoreError("unavailable")


def effective_draft(view: MissionView | dict[str, object]) -> MissionDraft:
    if isinstance(view, dict):
        view = MissionView.model_validate(view)
    fields = {k: getattr(view, k) for k in MissionDraft.model_fields}
    fields["budget_policy"] = view.budget
    return MissionDraft(**fields)


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
    if not isinstance(command, SubmitMissionCommand):
        raise TypeError("submit dispatched with a non-submit command")
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

    admission, budget_source = _budget_admission(
        draft, project["provenance"], mission_id
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


def _revise(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, ReviseMissionCommand):
        raise TypeError("revise dispatched with a non-revise command")
    payload = command.payload
    draft = payload.draft
    workspace_id = envelope.workspace_id

    raw_mission = _latest_mission(cur, workspace_id, payload.mission_id)
    if raw_mission is None:
        raise WorkStoreError("invalid_request")
    view = MissionView.model_validate(raw_mission)

    if view.status.value in TERMINAL_STATUSES:
        raise WorkStoreError("mission_transition_refused")
    if payload.base_revision != view.revision:
        raise WorkStoreError("revision_conflict")

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

    admission, budget_source = _budget_admission(
        draft, project["provenance"], payload.mission_id
    )
    new_revision = view.revision + 1
    status = view.status
    hold = view.hold_decision
    budget = admission.budget
    source = budget_source
    cause: tuple[str, str] | None = None

    # No effective budget holds, as submit does. A held mission whose revision
    # now has a budget leaves hold before materiality. proposed_classification
    # is not read.
    if admission.state == "held":
        held = _json_safe(admission.decision)
        if not isinstance(held, dict):
            raise WorkStoreError("unavailable")
        hold = held
        budget = None
        source = None
        if view.status != MissionStatus.BLOCKED:
            status = MissionStatus.BLOCKED
            cause = ("decision", str(hold["decision_id"]))
    elif view.status == MissionStatus.BLOCKED and view.budget is None:
        status = MissionStatus.AWAITING_CONFIRMATION
        hold = None
        cause = ("policy_rule", _BUDGET_SUPPLIED_RULE)
    else:
        eff = effective_draft(
            {
                **view.model_dump(mode="json"),
                **draft.model_dump(mode="json"),
                "budget": (
                    None
                    if admission.budget is None
                    else admission.budget.model_dump(mode="json")
                ),
                "budget_source": budget_source,
            }
        )
        approved_envelope = (
            view.approved_scope.envelope.model_dump(mode="json")
            if view.approved_scope is not None
            else None
        )
        cases = material_cases(approved_envelope, eff.model_dump(mode="json"))
        if cases and view.status != MissionStatus.AWAITING_CONFIRMATION:
            status = MissionStatus.AWAITING_CONFIRMATION
            cause = ("policy_rule", _MATERIAL_RULE_PREFIX + ",".join(cases))

    transitions = view.transitions
    if cause is not None:
        cause_kind, cause_id = cause
        cur.execute("SELECT clock_timestamp() AS at")
        created_row = cur.fetchone()
        if created_row is None:
            raise WorkStoreError("unavailable")
        at = created_row["at"]
        if cause_kind == "decision":
            transition = MissionTransition(
                from_status=view.status,
                to_status=status,
                cause_kind="decision",
                cause_id=cause_id,
                actor_id=actor_id,
                actor_kind=actor_kind,
                at=at,
                revision=new_revision,
            )
        else:
            transition = MissionTransition(
                from_status=view.status,
                to_status=status,
                cause_kind="policy_rule",
                cause_id=cause_id,
                actor_id=actor_id,
                actor_kind=actor_kind,
                at=at,
                revision=new_revision,
            )
        transitions = (*view.transitions, transition)

    updated = MissionView.model_validate(
        {
            **view.model_dump(mode="json"),
            **draft.model_dump(mode="json"),
            "revision": new_revision,
            "status": status.value,
            "budget": None if budget is None else budget.model_dump(mode="json"),
            "budget_source": source,
            "hold_decision": hold,
            "transitions": [item.model_dump(mode="json") for item in transitions],
        }
    )
    mission = updated.model_dump(mode="json")
    mission = MissionView.model_validate(mission).model_dump(mode="json")
    return {"type": "revise_mission", "mission": mission}


def _approve(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, ApproveMissionCommand):
        raise TypeError("approve dispatched with a non-approve command")
    payload = command.payload
    workspace_id = envelope.workspace_id

    raw_mission = _latest_mission(cur, workspace_id, payload.mission_id)
    if raw_mission is None:
        raise WorkStoreError("invalid_request")
    view = MissionView.model_validate(raw_mission)

    if view.status != MissionStatus.AWAITING_CONFIRMATION:
        raise WorkStoreError("mission_transition_refused")
    if view.budget is None:
        raise WorkStoreError("mission_transition_refused")
    if payload.revision != view.revision:
        raise WorkStoreError("revision_conflict")

    eff_draft = effective_draft(view)
    approved_envelope = (
        view.approved_scope.envelope.model_dump(mode="json")
        if view.approved_scope is not None
        else None
    )
    cases = material_cases(approved_envelope, eff_draft.model_dump(mode="json"))
    if outside_envelope(cases) and actor_kind != "owner":
        raise WorkStoreError("approval_required")

    cur.execute("SELECT clock_timestamp() AS at")
    created_row = cur.fetchone()
    if created_row is None:
        raise WorkStoreError("unavailable")
    at = created_row["at"]

    if payload.basis_kind == "decision":
        cause_kind = "decision"
        cause_id = payload.basis_id
    elif payload.basis_kind == "standing_mandate":
        cause_kind = "policy_rule"
        cause_id = f"standing_mandate:{payload.basis_id}"
    else:
        raise WorkStoreError("invalid_request")

    approved_scope = MissionApprovedScope(
        revision=payload.revision,
        basis_kind=payload.basis_kind,
        basis_id=payload.basis_id,
        approved_by=actor_id,
        approved_by_actor_kind=actor_kind,
        approved_at=at,
        envelope=eff_draft,
    )
    transition = MissionTransition(
        from_status=view.status,
        to_status=MissionStatus.APPROVED,
        cause_kind=cause_kind,
        cause_id=cause_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        at=at,
        revision=view.revision,
    )
    updated_view = view.model_copy(
        update={
            "status": MissionStatus.APPROVED,
            "approved_scope": approved_scope,
            "transitions": (*view.transitions, transition),
        }
    )
    mission = updated_view.model_dump(mode="json")
    mission = MissionView.model_validate(mission).model_dump(mode="json")
    return {"type": "approve_mission", "mission": mission}


def _set_status(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    command = envelope.command
    if not isinstance(command, SetMissionStatusCommand):
        raise TypeError("set_status dispatched with a non-status command")
    payload = command.payload
    workspace_id = envelope.workspace_id

    raw_mission = _latest_mission(cur, workspace_id, payload.mission_id)
    if raw_mission is None:
        raise WorkStoreError("invalid_request")
    view = MissionView.model_validate(raw_mission)

    if not transition_allowed(view.status.value, payload.target_status):
        raise WorkStoreError("mission_transition_refused")

    if view.budget is None:
        if payload.target_status != "abandoned":
            raise WorkStoreError("mission_transition_refused")
        hold_id = (
            view.hold_decision.get("decision_id")
            if isinstance(view.hold_decision, dict)
            else None
        )
        if (
            payload.cause_kind != "decision"
            or hold_id is None
            or str(payload.decision_id) != str(hold_id)
        ):
            raise WorkStoreError("mission_transition_refused")

    if payload.target_status in ("running", "paused") and view.approved_scope is None:
        raise WorkStoreError("mission_transition_refused")

    if payload.cause_kind == "principal":
        cause_id = str(actor_id)
    elif payload.cause_kind == "policy_rule":
        if payload.policy_rule_id is None:
            raise AssertionError("policy_rule cause without a policy_rule_id")
        cause_id = payload.policy_rule_id
    elif payload.cause_kind == "decision":
        if payload.decision_id is None:
            raise AssertionError("decision cause without a decision_id")
        cause_id = str(payload.decision_id)
    else:
        raise WorkStoreError("invalid_request")

    cur.execute("SELECT clock_timestamp() AS at")
    created_row = cur.fetchone()
    if created_row is None:
        raise WorkStoreError("unavailable")
    at = created_row["at"]

    target_status = MissionStatus(payload.target_status)
    transition = MissionTransition(
        from_status=view.status,
        to_status=target_status,
        cause_kind=payload.cause_kind,
        cause_id=cause_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        at=at,
        revision=view.revision,
    )
    updated_view = view.model_copy(
        update={
            "status": target_status,
            "transitions": (*view.transitions, transition),
        }
    )
    mission = updated_view.model_dump(mode="json")
    mission = MissionView.model_validate(mission).model_dump(mode="json")
    return {"type": "set_mission_status", "mission": mission}


def _link(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
) -> dict[str, object]:
    """Append one work item and add its published budget to the mission draw.

    ``max_subagents`` is a per-item cap against the mission envelope, not a sum.
    """
    command = envelope.command
    if not isinstance(command, LinkMissionWorkCommand):
        raise TypeError("link dispatched with a non-link command")
    payload = command.payload
    workspace_id = envelope.workspace_id

    raw_mission = _latest_mission(cur, workspace_id, payload.mission_id)
    if raw_mission is None:
        raise WorkStoreError("invalid_request")
    view = MissionView.model_validate(raw_mission)
    if view.status not in _LINKABLE_STATUSES:
        raise WorkStoreError("mission_transition_refused")

    cur.execute(
        "SELECT 1 FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
        (workspace_id, payload.work_id),
    )
    if cur.fetchone() is None:
        raise WorkStoreError("invalid_request")
    if any(link.work_id == payload.work_id for link in view.links):
        raise WorkStoreError("invalid_request")

    budget = item_budget(cur, workspace_id, payload.work_id)
    if budget is None:
        raise WorkStoreError("invalid_request")
    # view.budget is the admitted envelope: the mission policy, or the project's standing budget.
    if view.budget is None or not _within_envelope(view.drawn, budget, view.budget):
        raise WorkStoreError("mission_budget_exceeded")

    cur.execute("SELECT clock_timestamp() AS linked_at")
    created_row = cur.fetchone()
    if created_row is None:
        raise WorkStoreError("unavailable")
    link = MissionLink(
        work_id=payload.work_id,
        budget=budget,
        linked_at=created_row["linked_at"],
    )
    drawn = MissionDrawn(
        usd=_sum_usd(view.drawn.usd, budget.usd),
        tokens=view.drawn.tokens + budget.tokens,
        wall_clock_seconds=view.drawn.wall_clock_seconds + budget.wall_clock_seconds,
    )
    updated = view.model_copy(update={"links": (*view.links, link), "drawn": drawn})
    mission = updated.model_dump(mode="json")
    mission = MissionView.model_validate(mission).model_dump(mode="json")
    return {"type": "link_mission_work", "mission": mission}


def _within_envelope(drawn: MissionDrawn, item: ItemBudget, limit: ItemBudget) -> bool:
    if item.max_subagents > limit.max_subagents:
        return False
    return (
        Decimal(drawn.usd) + Decimal(item.usd) <= Decimal(limit.usd)
        and drawn.tokens + item.tokens <= limit.tokens
        and drawn.wall_clock_seconds + item.wall_clock_seconds
        <= limit.wall_clock_seconds
    )


def _sum_usd(left: str, right: str) -> str:
    return format(Decimal(left) + Decimal(right), "f")


def _budget_admission(
    draft: MissionDraft, provenance: object, mission_id: UUID
) -> tuple[MissionBudgetAdmission, str | None]:
    """Effective budget for submit and revise: the draft, else the project's standing budget."""
    if draft.budget_policy is not None:
        effective: object = draft.budget_policy.model_dump(mode="json")
        budget_source: str | None = "mission"
    else:
        standing = _standing_budget(provenance)
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
    return admission, budget_source


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
