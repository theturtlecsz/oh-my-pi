"""Translate a relayed owner intent into a mission command (OMP-416).

Runs inside the store's serializable transaction. A present owner signature
is checked against ``<config_dir>/owner_allowed_signers`` over
:func:`relay_signature_message`; a signature that does not verify is
``approval_required``. ``pause``, ``resume``, and ``request_cancellation``
become ``set_mission_status`` (paused, running, abandoned) with cause
``principal``. ``change_priority`` revises the effective draft at
``payload.revision``. ``confirm_scope`` and ``edit_scope`` apply the pending
intake decision via ``answer_mission_draft``; broadening requires an owner
signature. ``answer_decision`` checks action_class with OMP-403 signatures or
records controller answers when unset. The recorded domain event is that
translated command plus ``relay``, so ``latest_mission`` and ``list_decisions``
read their snapshots unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg

from omp_work.mission_scope import material_cases

from .api_models import RelayOwnerIntentResult
from .decision_records import answer_decision as execute_answer_decision, find_record
from .mission_intake import answer_mission_draft
from .missions import effective_draft, execute as execute_mission, latest_mission
from .models import (
    AnswerDecisionCommand,
    AnswerDecisionPayload,
    AnswerMissionDraftCommand,
    AnswerMissionDraftPayload,
    CommandEnvelope,
    InstructionProvenance,
    MissionDraftEditedAnswer,
    MissionDraftOptionAnswer,
    OwnerInstruction,
    RelayOwnerIntentCommand,
    RelayOwnerIntentPayload,
    ReviseMissionCommand,
    ReviseMissionPayload,
    SetMissionStatusCommand,
    SetMissionStatusPayload,
)
from .owner_controller import designated_controller
from .owner_signature import relay_signature_message, verify_owner_signature
from .store_shared import WorkStoreError

_StatusTarget = Literal["paused", "running", "abandoned"]
_STATUS_TARGETS: dict[str, _StatusTarget] = {
    "pause": "paused",
    "resume": "running",
    "request_cancellation": "abandoned",
}


def relay_owner_intent(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    actor_id: UUID,
    actor_kind: str,
    config_dir: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Apply one relay and return ``(RelayOwnerIntentResult, domain event)``."""
    command = envelope.command
    if not isinstance(command, RelayOwnerIntentCommand):
        raise TypeError("relay dispatched with a non-relay command")
    payload = command.payload

    if payload.intent in _STATUS_TARGETS:
        signed = _signed(config_dir, envelope.workspace_id, payload)
        event = _set_status(cur, envelope, payload, actor_id, actor_kind)
        mission = event["mission"]
        decision_id = None
        answer = None
    elif payload.intent == "change_priority":
        signed = _signed(config_dir, envelope.workspace_id, payload)
        event = _change_priority(cur, envelope, payload, actor_id, actor_kind)
        mission = event["mission"]
        decision_id = None
        answer = None
    elif payload.intent in ("confirm_scope", "edit_scope"):
        signed = _signed(config_dir, envelope.workspace_id, payload)
        event = _confirm_or_edit_scope(
            cur, envelope, payload, actor_id, actor_kind, signed
        )
        mission = event["mission"]
        decision_id = None
        answer = None
    elif payload.intent == "answer_decision":
        event, signed = _answer_decision(
            cur, envelope, payload, actor_id, actor_kind, config_dir
        )
        mission = None
        decision_id = payload.decision_id
        answer = payload.answer
    else:
        raise WorkStoreError("unavailable")

    result = RelayOwnerIntentResult(
        type="relay_owner_intent",
        intent=payload.intent,
        mission=mission,
        decision_id=decision_id,
        answer=answer,
        relay={
            "instruction": payload.instruction,
            "relayed_by": actor_id,
            "signed": signed,
        },
    ).model_dump(mode="json")
    return result, {**event, "relay": result["relay"]}


def _signed(
    config_dir: Path, workspace_id: UUID, payload: RelayOwnerIntentPayload
) -> bool:
    signature = payload.owner_signature
    if signature is None:
        return False
    if not verify_owner_signature(
        Path(config_dir) / "owner_allowed_signers",
        relay_signature_message(workspace_id, payload),
        signature,
    ):
        raise WorkStoreError("approval_required", ("owner_signature_invalid",))
    return True


def _mission_id(payload: RelayOwnerIntentPayload) -> UUID:
    mission_id = payload.mission_id
    if not isinstance(mission_id, UUID):
        raise WorkStoreError("invalid_request")
    return mission_id


def _translated(
    envelope: CommandEnvelope,
    command: (
        SetMissionStatusCommand
        | ReviseMissionCommand
        | AnswerMissionDraftCommand
        | AnswerDecisionCommand
    ),
) -> CommandEnvelope:
    return CommandEnvelope(
        api_version=envelope.api_version,
        workspace_id=envelope.workspace_id,
        operation_id=envelope.operation_id,
        request_id=envelope.request_id,
        correlation_id=envelope.correlation_id,
        command=command,
    )


def _set_status(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    payload: RelayOwnerIntentPayload,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    target = _STATUS_TARGETS[payload.intent]
    translated = _translated(
        envelope,
        SetMissionStatusCommand(
            type="set_mission_status",
            payload=SetMissionStatusPayload(
                mission_id=_mission_id(payload),
                target_status=target,
                cause_kind="principal",
            ),
        ),
    )
    return execute_mission(cur, translated, actor_id, actor_kind)


def _change_priority(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    payload: RelayOwnerIntentPayload,
    actor_id: UUID,
    actor_kind: str,
) -> dict[str, object]:
    mission_id = _mission_id(payload)
    if payload.revision is None or payload.priority is None:
        raise WorkStoreError("invalid_request")
    view = latest_mission(cur, envelope.workspace_id, mission_id)
    if view is None:
        raise WorkStoreError("invalid_request")
    draft = effective_draft(view).model_copy(update={"priority": payload.priority})
    translated = _translated(
        envelope,
        ReviseMissionCommand(
            type="revise_mission",
            payload=ReviseMissionPayload(
                mission_id=mission_id,
                base_revision=payload.revision,
                draft=draft,
            ),
        ),
    )
    return execute_mission(cur, translated, actor_id, actor_kind)


def _confirm_or_edit_scope(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    payload: RelayOwnerIntentPayload,
    actor_id: UUID,
    actor_kind: str,
    signed: bool,
) -> dict[str, object]:
    mission_id = _mission_id(payload)
    decision_id = payload.decision_id
    revision = payload.revision
    if decision_id is None or revision is None:
        raise WorkStoreError("invalid_request")

    record = find_record(cur, envelope.workspace_id, decision_id)
    if (
        record is None
        or not record.is_intake
        or str(record.view.get("mission_id")) != str(mission_id)
    ):
        raise WorkStoreError("invalid_request", ("decision_not_found",))
    if record.view.get("status") == "answered":
        raise WorkStoreError("revision_conflict", ("decision_already_answered",))
    if record.view.get("status") != "pending":
        raise WorkStoreError("revision_conflict")

    view = latest_mission(cur, envelope.workspace_id, mission_id)
    if view is None:
        raise WorkStoreError("invalid_request", ("decision_not_found",))

    decision_revision: int | None = None
    for ref in record.view.get("evidence_refs", ()):
        prefix = f"mission:{mission_id}@"
        if isinstance(ref, str) and ref.startswith(prefix):
            try:
                decision_revision = int(ref[len(prefix):])
                break
            except ValueError:
                pass

    if (
        revision != view.revision
        or decision_revision is None
        or revision != decision_revision
    ):
        raise WorkStoreError("revision_conflict")

    if payload.intent == "confirm_scope":
        target_draft = effective_draft(view)
        answer = MissionDraftOptionAnswer(kind="option", option="confirm")
    else:
        if payload.draft is None:
            raise WorkStoreError("invalid_request")
        target_draft = payload.draft
        answer = MissionDraftEditedAnswer(kind="edited_draft", draft=payload.draft)

    if view.approved_scope is not None:
        approved_envelope = view.approved_scope.envelope.model_dump(mode="json")
        revised_draft = target_draft.model_dump(mode="json")
        cases = material_cases(approved_envelope, revised_draft)
        broadened = "c" in cases or "undecidable" in cases
    else:
        decision_project = record.view.get("project_id")
        differs = str(target_draft.project_id) != str(decision_project)
        cur.execute(
            "SELECT r.key FROM omp_work.project_repositories pr"
            " JOIN omp_work.repositories r ON r.workspace_id = pr.workspace_id"
            " AND r.repository_id = pr.repository_id"
            " WHERE pr.workspace_id = %s AND pr.project_id = %s",
            (envelope.workspace_id, target_draft.project_id),
        )
        registered_keys = {row["key"] for row in cur.fetchall()}
        unregistered = any(
            repo not in registered_keys for repo in target_draft.repositories
        )
        broadened = differs or unregistered

    if broadened and not signed:
        raise WorkStoreError("approval_required", ("broaden_scope",))

    answer_cmd = AnswerMissionDraftCommand(
        type="answer_mission_draft",
        payload=AnswerMissionDraftPayload(
            decision_id=decision_id,
            mission_id=mission_id,
            revision=revision,
            answer=answer,
            instruction=OwnerInstruction(
                text=payload.instruction.text,
                provenance=InstructionProvenance(
                    channel="relay",
                    message_ref=payload.instruction.source_message_ref,
                    received_at=payload.instruction.received_at,
                ),
            ),
        ),
    )
    translated = _translated(envelope, answer_cmd)
    _res, event = answer_mission_draft(
        cur, translated, actor_id, actor_kind, relayed=True
    )
    return event


def _answer_decision(
    cur: psycopg.Cursor[dict[str, object]],
    envelope: CommandEnvelope,
    payload: RelayOwnerIntentPayload,
    actor_id: UUID,
    actor_kind: str,
    config_dir: Path,
) -> tuple[dict[str, object], bool]:
    decision_id = payload.decision_id
    if decision_id is None or payload.answer is None:
        raise WorkStoreError("invalid_request")

    record = find_record(cur, envelope.workspace_id, decision_id)
    if record is None:
        raise WorkStoreError("unavailable")
    if record.is_intake:
        raise WorkStoreError("invalid_request", ("answer_with_answer_mission_draft",))

    action_class = record.view.get("action_class")
    if action_class is not None:
        translated = _translated(
            envelope,
            AnswerDecisionCommand(
                type="answer_decision",
                payload=AnswerDecisionPayload(
                    decision_id=decision_id,
                    answer=payload.answer,
                    owner_signature=payload.owner_signature,
                    expires_at=payload.expires_at,
                ),
            ),
        )
        event = execute_answer_decision(
            cur, translated, config_dir / "owner_allowed_signers"
        )
        signed = payload.owner_signature is not None
    else:
        controller = designated_controller(config_dir, envelope.workspace_id)
        if controller is None or actor_id != controller:
            raise WorkStoreError("unavailable")

        translated = _translated(
            envelope,
            AnswerDecisionCommand(
                type="answer_decision",
                payload=AnswerDecisionPayload(
                    decision_id=decision_id,
                    answer=payload.answer,
                    owner_signature=payload.owner_signature,
                    expires_at=payload.expires_at,
                ),
            ),
        )
        event = execute_answer_decision(
            cur, translated, config_dir / "owner_allowed_signers"
        )
        signed = False

    return event, signed
