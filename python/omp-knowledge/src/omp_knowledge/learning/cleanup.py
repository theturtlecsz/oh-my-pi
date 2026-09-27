from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .models import StrictModel
from .store import LearningStore


@runtime_checkable
class CleanupTarget(Protocol):
    """Protocol for targets that perform cleanup actions on procedures."""

    def clean(self, procedure_id: str, action: str) -> None:
        """Perform cleanup for a procedure. Raises on failure."""
        ...


class NativeCommittedTarget:
    """Confirms that the procedure's native change has been committed.

    - withdraw: procedure status must be 'withdrawn'.
    - narrow: must have an accepted corrections row with to_version <= current_version.
    """

    def __init__(self, store: LearningStore) -> None:
        self.store = store

    def clean(self, procedure_id: str, action: str) -> None:
        row = self.store.execute(
            "SELECT current_version, status FROM procedures WHERE procedure_id = ?",
            (procedure_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Procedure {procedure_id} not found")

        current_version = int(row["current_version"])
        status = str(row["status"])

        if action == "withdraw":
            if status != "withdrawn":
                raise RuntimeError(
                    f"Procedure {procedure_id} status is {status!r}, expected 'withdrawn'"
                )
        elif action == "narrow":
            corr = self.store.execute(
                """
                SELECT 1 FROM corrections
                WHERE procedure_id = ?
                  AND action = 'narrow'
                  AND status = 'accepted'
                  AND to_version IS NOT NULL
                  AND to_version <= ?
                LIMIT 1
                """,
                (procedure_id, current_version),
            ).fetchone()
            if corr is None:
                raise RuntimeError(
                    f"Procedure {procedure_id} has no accepted narrow correction committed with to_version <= {current_version}"
                )
        else:
            raise ValueError(f"Unknown cleanup action {action!r}")


class CleanupItem(StrictModel):
    queue_id: int
    procedure_id: str
    action: str
    attempts: int
    done: bool
    last_error: str | None = None


class CleanupRun(StrictModel):
    run_id: str
    processed: int
    succeeded: int
    failed: int
    items: tuple[CleanupItem, ...] = ()


def drain_cleanup(
    store: LearningStore,
    target: CleanupTarget,
    *,
    limit: int = 50,
) -> CleanupRun:
    """Drain pending rows from cleanup_queue with done_at IS NULL ordered by queue_id.

    Each row is processed in its own transaction:
    - Increments attempts.
    - On success: sets done_at and clears last_error.
    - On failure: stores "<ExcType>: <msg>" in last_error and leaves the row pending.
    """
    run_id = str(uuid4())
    if limit <= 0:
        return CleanupRun(
            run_id=run_id,
            processed=0,
            succeeded=0,
            failed=0,
            items=(),
        )

    rows = store.execute(
        """
        SELECT queue_id, procedure_id, action
        FROM cleanup_queue
        WHERE done_at IS NULL
        ORDER BY queue_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    items: list[CleanupItem] = []
    succeeded = 0
    failed = 0

    for row in rows:
        queue_id = int(row["queue_id"])
        procedure_id = str(row["procedure_id"])
        action = str(row["action"])

        clean_error: str | None = None
        new_attempts = 0
        try:
            with store.transaction() as conn:
                target.clean(procedure_id, action)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE cleanup_queue
                    SET attempts = attempts + 1,
                        done_at = ?,
                        last_error = NULL
                    WHERE queue_id = ?
                    """,
                    (now, queue_id),
                )
                updated = conn.execute(
                    "SELECT attempts FROM cleanup_queue WHERE queue_id = ?",
                    (queue_id,),
                ).fetchone()
                new_attempts = int(updated["attempts"]) if updated else 1
        except Exception as exc:  # noqa: BLE001
            clean_error = f"{type(exc).__name__}: {exc}"

        if clean_error is not None:
            failed += 1
            with store.transaction() as conn:
                conn.execute(
                    """
                    UPDATE cleanup_queue
                    SET attempts = attempts + 1,
                        last_error = ?
                    WHERE queue_id = ?
                    """,
                    (clean_error, queue_id),
                )
                updated = conn.execute(
                    "SELECT attempts FROM cleanup_queue WHERE queue_id = ?",
                    (queue_id,),
                ).fetchone()
                new_attempts = int(updated["attempts"]) if updated else 1
            items.append(
                CleanupItem(
                    queue_id=queue_id,
                    procedure_id=procedure_id,
                    action=action,
                    attempts=new_attempts,
                    done=False,
                    last_error=clean_error,
                )
            )
        else:
            succeeded += 1
            items.append(
                CleanupItem(
                    queue_id=queue_id,
                    procedure_id=procedure_id,
                    action=action,
                    attempts=new_attempts,
                    done=True,
                    last_error=None,
                )
            )

    return CleanupRun(
        run_id=run_id,
        processed=len(items),
        succeeded=succeeded,
        failed=failed,
        items=tuple(items),
    )


__all__ = [
    "CleanupItem",
    "CleanupRun",
    "CleanupTarget",
    "NativeCommittedTarget",
    "drain_cleanup",
]
