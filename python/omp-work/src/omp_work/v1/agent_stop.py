from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import psycopg

from .models import Command, CommandEnvelope


def allowed_while_stopped(command: Command) -> bool:
    """OMP-405: the only commands an engaged workspace still accepts.

    Stop control itself (so the owner can always release) and a grant's
    pause/stop/cancel transitions (so a running agent halts instead of wedging).
    Everything else is refused before any handler runs.
    """
    if command.type in {"engage_stop", "release_stop", "record_alarm_signal"}:
        return True
    if command.type == "relay_owner_intent" and command.payload.intent == "release_stop":
        return True
    return (
        command.type == "set_execution_state"
        and command.payload.target_state in {"paused", "stopped", "canceled"}
    )


def stop_result(envelope: CommandEnvelope) -> dict[str, object]:
    return {
        "type": envelope.command.type,
        "stopped": envelope.command.type == "engage_stop",
        "reason": envelope.command.payload.reason,
    }


def read_stop_state(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
) -> dict[str, object]:
    cur.execute(
        "SELECT event_type, actor_kind, payload->>'reason' AS reason, occurred_at "
        "FROM omp_audit.domain_events "
        "WHERE workspace_id = %s "
        "  AND aggregate_id = %s "
        "  AND event_type IN ('engage_stop', 'release_stop') "
        "  AND outcome = 'applied' "
        "ORDER BY sequence DESC "
        "LIMIT 1",
        (workspace_id, workspace_id),
    )
    row = cur.fetchone()
    if row is None:
        return {
            "workspace_id": str(workspace_id),
            "stopped": False,
            "reason": None,
            "changed_at": None,
            "changed_by_actor_kind": None,
        }
    occurred_at = row["occurred_at"]
    return {
        "workspace_id": str(workspace_id),
        "stopped": row["event_type"] == "engage_stop",
        "reason": row["reason"],
        "changed_at": (
            occurred_at.isoformat()
            if hasattr(occurred_at, "isoformat")
            else occurred_at
        ),
        "changed_by_actor_kind": row["actor_kind"],
    }
