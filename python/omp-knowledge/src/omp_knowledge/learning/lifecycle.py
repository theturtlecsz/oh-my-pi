"""Suspend active lessons whose cited evidence no longer holds.

A sweep reads every active lesson in one workspace, including lessons whose
source work currently has no candidate. It never moves a suspended lesson back
to active. Transport and permission failures propagate and leave that lesson's
status unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .models import SourceIdentity
from .policy import is_supporting_receipt
from .record import (
    ReceiptMalformed,
    ReceiptMissing,
    WorkflowMalformed,
    WorkflowMissing,
    evidence_is_fresh,
    resolve_cited_receipt,
    resolve_source_workflow,
)
from .store import LearningStore


@dataclass(frozen=True)
class _Lesson:
    procedure_id: str
    source: SourceIdentity | None
    unit_workspace: str | None
    receipt_ids: tuple[str, ...]


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _lessons(store: LearningStore) -> list[_Lesson]:
    rows = store.execute(
        """
        SELECT
            p.procedure_id,
            v.source_json,
            (
                SELECT u.workspace_id
                FROM proposals AS pr
                JOIN units AS u ON u.unit_id = pr.unit_id
                WHERE pr.procedure_id = p.procedure_id AND pr.status = 'accepted'
                ORDER BY pr.created_at, pr.proposal_id
                LIMIT 1
            ) AS unit_workspace
        FROM procedures AS p
        JOIN procedure_versions AS v
          ON v.procedure_id = p.procedure_id AND v.version = p.current_version
        WHERE p.status = 'active'
        ORDER BY p.procedure_id
        """
    ).fetchall()
    lessons: list[_Lesson] = []
    for row in rows:
        try:
            source = SourceIdentity.model_validate_json(row["source_json"])
        except ValidationError:
            source = None
        support = store.execute(
            """
            SELECT receipt_id FROM procedure_support
            WHERE procedure_id = ?
            ORDER BY receipt_id
            """,
            (str(row["procedure_id"]),),
        ).fetchall()
        unit_workspace = row["unit_workspace"]
        lessons.append(
            _Lesson(
                procedure_id=str(row["procedure_id"]),
                source=source,
                unit_workspace=None if unit_workspace is None else str(unit_workspace),
                receipt_ids=tuple(str(item["receipt_id"]) for item in support),
            )
        )
    return lessons


def _in_workspace(lesson: _Lesson, workspace_id: str) -> bool:
    source = lesson.source
    if source is not None and str(source.workspace_id) == workspace_id:
        return True
    return lesson.unit_workspace is not None and lesson.unit_workspace == workspace_id


def workspace_has_resolvable_lessons(store: LearningStore, workspace_id: str | UUID) -> bool:
    """True when a sweep of this workspace must read receipts or the workflow.

    An active row whose source is not a :class:`SourceIdentity` and which has
    no unit in the workspace is not this workspace's lesson. The worker-context
    fixture stores that shape and must not force a client open.
    """
    workspace = str(workspace_id)
    return any(
        _in_workspace(lesson, workspace) and lesson.source is not None
        for lesson in _lessons(store)
    )


def _unsupported(lesson: _Lesson, receipts: Any, records: Any) -> bool:
    source = lesson.source
    if source is None:
        return True
    candidate = None
    revision_id = None
    view = None
    # No records seam means this run cannot see the workflow. That is not a
    # missing candidate: retry reuses the stored trace and does not re-read.
    # A version that never recorded the binding ids, such as a narrowed
    # correction, also cannot show that the candidate changed.
    bound = (
        _present(source.work_id)
        and _present(source.candidate_id)
        and _present(source.revision_id)
    )
    if records is not None and bound:
        try:
            view = resolve_source_workflow(records, source)
        except (WorkflowMalformed, WorkflowMissing):
            return True
        # An unresolved id is not a missing candidate. The records seam may
        # only know the item this drain is capturing; a later read that finds
        # the workflow with no candidate still suspends.
        if view is not None:
            if not _present(view.candidate_id) or not _present(view.revision_id):
                return True
            candidate = view.candidate
            revision_id = view.revision_id
            if str(source.revision_id) != str(revision_id):
                return True
            if str(source.candidate_id) != str(view.candidate_id):
                return True
    for receipt_id in lesson.receipt_ids:
        if receipts is None:
            raise RuntimeError("sweep requires receipts to resolve cited evidence")
        try:
            receipt = resolve_cited_receipt(receipts, receipt_id)
        except (ReceiptMissing, ReceiptMalformed):
            return True
        if not is_supporting_receipt(receipt):
            return True
        if view is not None and not evidence_is_fresh(
            receipt,
            candidate,
            revision_id,
            work_id=view.work_id,
            candidate_id=view.candidate_id,
        ):
            return True
    return False


def _suspend(store: LearningStore, procedure_id: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE procedures
            SET status = 'suspended', updated_at = ?
            WHERE procedure_id = ? AND status = 'active'
            """,
            (now, procedure_id),
        )
        return cursor.rowcount == 1


def sweep(
    store: LearningStore,
    receipts: Any,
    records: Any,
    workspace_id: str | UUID,
) -> list[str]:
    """Suspend every active lesson in ``workspace_id`` whose support no longer holds.

    Each lesson is judged on its own. A lesson that suspends is committed before
    the next lesson is read, so a later transport or permission error leaves the
    earlier suspensions in place and does not change the lesson whose read failed.
    Suspended lessons are not selected and are never returned to active.
    """
    workspace = str(workspace_id)
    suspended: list[str] = []
    for lesson in _lessons(store):
        if not _in_workspace(lesson, workspace):
            continue
        if not _unsupported(lesson, receipts, records):
            continue
        if _suspend(store, lesson.procedure_id):
            suspended.append(lesson.procedure_id)
    return suspended


__all__ = ["sweep", "workspace_has_resolvable_lessons"]
