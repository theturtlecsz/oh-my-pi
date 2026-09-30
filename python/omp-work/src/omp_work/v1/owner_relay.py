"""Translate a relayed owner intent into a mission command (OMP-416).

Runs inside the store's serializable transaction. A present owner signature
is checked against ``<config_dir>/owner_allowed_signers`` over
:func:`relay_signature_message`; a signature that does not verify is
``approval_required``. ``pause``, ``resume``, and ``request_cancellation``
become ``set_mission_status`` (paused, running, abandoned) with cause
``principal``. ``change_priority`` revises the effective draft at
``payload.revision``. The recorded domain event is that translated mission
command plus ``relay``, so ``latest_mission`` reads the mission snapshot
unchanged. ``confirm_scope``, ``edit_scope``, and ``answer_decision`` are
``unavailable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg

from .api_models import RelayOwnerIntentResult
from .missions import effective_draft, execute as execute_mission, latest_mission
from .models import (
    CommandEnvelope,
    RelayOwnerIntentCommand,
    RelayOwnerIntentPayload,
    ReviseMissionCommand,
    ReviseMissionPayload,
    SetMissionStatusCommand,
    SetMissionStatusPayload,
)
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
    signed = _signed(config_dir, envelope.workspace_id, payload)
    if payload.intent in _STATUS_TARGETS:
        event = _set_status(cur, envelope, payload, actor_id, actor_kind)
    elif payload.intent == "change_priority":
        event = _change_priority(cur, envelope, payload, actor_id, actor_kind)
    else:
        raise WorkStoreError("unavailable")
    result = RelayOwnerIntentResult(
        type="relay_owner_intent",
        intent=payload.intent,
        mission=event["mission"],
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
    command: SetMissionStatusCommand | ReviseMissionCommand,
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
