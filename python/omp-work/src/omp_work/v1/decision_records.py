"""OMP-414 decision records rebuilt from applied domain events. No decision table.

Applied draft_mission_intake events on the mission aggregate contribute a
pending decision when payload["decision"] is an object, in sequence with
create_decision and answer_decision. A null decision adds nothing.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from .api_models import AnswerDecisionResult, CreateDecisionResult
from .models import CommandEnvelope
from .owner_signature import decision_signature_message, verify_owner_signature
from .store_shared import WorkStoreError

if TYPE_CHECKING:
    import psycopg


class _Record(NamedTuple):
    view: dict[str, object]
    resume_state: str | None


def create_decision(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    payload = envelope.command.payload
    decision_id = str(payload.decision_id)
    if any(
        str(record.view["decision_id"]) == decision_id
        for record in _load(cur, envelope.workspace_id)
    ):
        raise WorkStoreError("revision_conflict", ("decision_exists",))
    cur.execute("SELECT clock_timestamp() AS created_at")
    created_row = cur.fetchone()
    if created_row is None:
        raise WorkStoreError("unavailable")
    created_at = created_row["created_at"]
    result = CreateDecisionResult(
        type="create_decision",
        decision_id=payload.decision_id,
        project_id=payload.project_id,
        mission_id=payload.mission_id,
        action_class=payload.action_class,
        created_at=created_at,
    ).model_dump(mode="json")
    event = {
        "type": "create_decision",
        "status": "pending",
        "decision": payload.model_dump(mode="json"),
    }
    return result, event


def answer_decision(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    owner_allowed_signers: Path,
) -> dict[str, object]:
    payload = envelope.command.payload
    decision_id = str(payload.decision_id)
    record = next(
        (
            item
            for item in _load(cur, envelope.workspace_id)
            if str(item.view["decision_id"]) == decision_id
        ),
        None,
    )
    if record is None:
        raise WorkStoreError("invalid_request", ("decision_not_found",))
    if record.view["status"] == "answered":
        raise WorkStoreError("revision_conflict", ("decision_already_answered",))
    if payload.answer not in record.view["options"]:
        raise WorkStoreError("invalid_request", ("answer_not_an_option",))
    action_class = record.view["action_class"]
    if action_class is not None:
        if not isinstance(action_class, str):
            raise WorkStoreError("invalid_request")
        target_sha256 = record.view.get("target_sha256")
        if target_sha256 is not None and payload.expires_at is None:
            raise WorkStoreError("approval_required", ("expires_at_required",))
        if payload.expires_at is not None:
            cur.execute("SELECT clock_timestamp() AS now")
            now_row = cur.fetchone()
            if now_row is None:
                raise WorkStoreError("unavailable")
            now = now_row["now"]
            if payload.expires_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                raise WorkStoreError("approval_required", ("authorization_expired",))
        if payload.owner_signature is None:
            raise WorkStoreError("approval_required", ("owner_signature_required",))
        message = decision_signature_message(
            workspace_id=envelope.workspace_id,
            decision_id=payload.decision_id,
            action_class=action_class,
            answer=payload.answer,
            target_sha256=str(target_sha256) if target_sha256 is not None else None,
            expires_at=payload.expires_at,
        )
        if not verify_owner_signature(
            owner_allowed_signers, message, payload.owner_signature
        ):
            raise WorkStoreError("approval_required", ("owner_signature_invalid",))
    mission_id = record.view["mission_id"]
    return AnswerDecisionResult(
        type="answer_decision",
        decision_id=payload.decision_id,
        mission_id=mission_id if isinstance(mission_id, str) else None,
        answer=payload.answer,
        resume_state=record.resume_state,
        expires_at=payload.expires_at,
    ).model_dump(mode="json")


def find_decision(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    decision_id: UUID | str,
) -> dict[str, object] | None:
    target_id = str(decision_id)
    for record in _load(cur, workspace_id):
        if str(record.view["decision_id"]) == target_id:
            return record.view
    return None


def list_decisions(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    *,
    status: str | None = None,
    mission_id: str | None = None,
) -> list[dict[str, object]]:
    views: list[dict[str, object]] = []
    for record in _load(cur, workspace_id):
        view = record.view
        if status is not None and view["status"] != status:
            continue
        if mission_id is not None and view["mission_id"] != mission_id:
            continue
        views.append(view)
    return views


def _load(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
) -> list[_Record]:
    cur.execute(
        "SELECT event_type, payload, occurred_at "
        "FROM omp_audit.domain_events "
        "WHERE workspace_id = %s "
        "  AND outcome = 'applied' "
        "  AND ("
        "    (aggregate_id = %s "
        "      AND event_type IN ('create_decision', 'answer_decision')) "
        "    OR (aggregate_type = 'mission' "
        "      AND event_type = 'draft_mission_intake')"
        "  ) "
        "ORDER BY sequence ASC",
        (workspace_id, workspace_id),
    )
    records: dict[str, _Record] = {}
    order: list[str] = []
    for row in cur.fetchall():
        body = _object(row["payload"])
        event_type = row["event_type"]
        if event_type == "create_decision" or (
            event_type == "draft_mission_intake" and isinstance(body.get("decision"), dict)
        ):
            decision = body["decision"]
            decision_id = str(decision["decision_id"])
            if decision_id in records:
                continue
            raw_resume = decision.get("resume_state")
            records[decision_id] = _Record(
                view=_pending_view(decision),
                resume_state=raw_resume if isinstance(raw_resume, str) else None,
            )
            order.append(decision_id)
            continue
        if event_type == "draft_mission_intake":
            continue
        decision_id = str(body["decision_id"])
        record = records.get(decision_id)
        if record is None or record.view["status"] == "answered":
            continue
        occurred_at = row["occurred_at"]
        record.view["status"] = "answered"
        record.view["answer"] = body["answer"]
        record.view["answered_at"] = (
            occurred_at.isoformat()
            if hasattr(occurred_at, "isoformat")
            else occurred_at
        )
        record.view["expires_at"] = body.get("expires_at")
    return [records[decision_id] for decision_id in order]


def _pending_view(decision: dict[str, object]) -> dict[str, object]:
    return {
        "decision_id": decision["decision_id"],
        "project_id": decision["project_id"],
        "mission_id": decision.get("mission_id"),
        "status": "pending",
        "question": decision["question"],
        "why_it_matters": decision["why_it_matters"],
        "risk_of_delay": decision["risk_of_delay"],
        "options": decision["options"],
        "evidence_refs": decision.get("evidence_refs", []),
        "default_if_any": decision.get("default_if_any"),
        "risk_of_each_choice": decision["risk_of_each_choice"],
        "action_class": decision.get("action_class"),
        "target_sha256": decision.get("target_sha256"),
        "answer": None,
        "answered_at": None,
        "expires_at": None,
    }


def _object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        return value
    raise TypeError("decision event payload is not an object")
