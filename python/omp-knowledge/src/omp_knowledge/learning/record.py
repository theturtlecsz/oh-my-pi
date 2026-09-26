"""Bounded rendering of the finished item's own WorkService execution record.

A ``complete_work`` domain event carries only its own payload (``work_id``,
``state``, ``row_version``), which is not enough evidence for a lesson. The
capture seam therefore resolves the event's ``work_id`` to the item's primary
alias key through the existing ``work-items`` read and then reads the exact
``workflow`` projection, rendering the title, acceptance criteria, receipts, and
close-attempt history into a fixed, size-bounded trace block.

The record is read over the reads the contract already declares — no new route,
no schema change, and no hosted-model call. Every read goes through the narrow
:class:`NativeRecords` seam, which ``WorkClient`` satisfies structurally, so a
test can drive it with an in-memory double over recorded projections. Nothing
here reads a clock or the environment; the same view always renders the same
block.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx
from omp_work.v1.api_models import WorkflowView, WorkItemsPage
from omp_work.v1.canonical import canonical_json
from omp_work.v1.service import WorkError

__all__ = [
    "MAX_KEY_SCAN",
    "NativeRecords",
    "bounded_work_record",
    "resolve_work_key",
    "work_record_block",
]

MAX_KEY_SCAN = 2000
"""Maximum work-item summaries scanned when resolving a work_id to its key."""

WORK_ITEMS_PAGE = 500
"""Page size for the keyset-paged work-items read (1..500 on the wire)."""

MAX_TEXT_CHARS = 2000
"""Per-field text bound; longer text is clipped with an explicit marker."""

MAX_PAYLOAD_CHARS = 2048
"""Bound on a receipt payload rendered as canonical JSON text."""

MAX_ACCEPTANCE_CRITERIA = 32
MAX_RECEIPTS = 32
MAX_CLOSE_ATTEMPTS = 8
MAX_CLOSE_ATTEMPT_EVENTS = 32
MAX_CHECKPOINT_DELIVERIES = 8
MAX_RELATIONS = 32

_TRUNCATION_MARKER = " ...[truncated]"

ViewInput = WorkflowView | Mapping[str, Any]
"""A ``WorkflowView`` or its JSON dump; both read the same fields."""


@runtime_checkable
class NativeRecords(Protocol):
    """Read-only seam for one work item's own execution record.

    ``WorkClient`` fits structurally: the alias-keyed ``workflow`` read returns
    the item's full projection, and the keyset-paged ``work-items`` read is the
    only read that links a bare ``work_id`` to that alias key.
    """

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage: ...

    def workflow(self, key: str) -> WorkflowView: ...


def _view_mapping(view: ViewInput) -> Mapping[str, Any]:
    if isinstance(view, Mapping):
        return view
    return view.model_dump(mode="json")


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return _clip(str(value))


def resolve_work_key(
    records: NativeRecords,
    work_id: str | UUID,
    *,
    scan_limit: int = MAX_KEY_SCAN,
) -> str | None:
    """Resolve a bare ``work_id`` to its primary alias key, or ``None``.

    The scan is bounded: a workspace larger than ``scan_limit`` items yields no
    resolution rather than an unbounded walk, and the caller then traces the
    event without a record instead of failing the unit.
    """
    target = str(work_id)
    cursor: tuple[datetime, UUID] | None = None
    scanned = 0
    while True:
        page = records.work_items(after=cursor, limit=WORK_ITEMS_PAGE)
        for summary in page.items:
            if str(summary.work_id) == target:
                return str(summary.key)
        scanned += len(page.items)
        if (
            not page.items
            or page.next_created_at is None
            or page.next_work_id is None
            or scanned >= scan_limit
        ):
            return None
        cursor = (page.next_created_at, page.next_work_id)


def _receipt_block(receipt: Mapping[str, Any]) -> dict[str, Any]:
    payload_text = canonical_json(receipt.get("payload") or {})
    return {
        "receipt_id": str(receipt["receipt_id"]),
        "kind": str(receipt["kind"]),
        "verdict": receipt.get("verdict"),
        "issuer": str(receipt.get("issuer") or ""),
        "issued_at": str(receipt["issued_at"]),
        "independent": bool(receipt.get("independent")),
        "candidate_id": str(receipt["candidate_id"])
        if receipt.get("candidate_id") is not None
        else None,
        "payload_sha256": str(receipt.get("payload_sha256") or ""),
        "artifact_sha256": receipt.get("artifact_sha256"),
        "candidate_commit": receipt.get("candidate_commit"),
        "remote_ref": receipt.get("remote_ref"),
        "payload_text": _clip(payload_text, MAX_PAYLOAD_CHARS),
        "payload_truncated": len(payload_text) > MAX_PAYLOAD_CHARS,
    }


def _attempt_block(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": str(attempt["attempt_id"]),
        "state": str(attempt["state"]),
        "authorization_kind": str(attempt.get("authorization_kind") or ""),
        "requested_at": str(attempt["requested_at"]),
        "closeout_requested_at": attempt.get("closeout_requested_at"),
        "completed_at": attempt.get("completed_at"),
        "candidate_commit": attempt.get("candidate_commit"),
        "repository": attempt.get("repository"),
        "diff_sha256": attempt.get("diff_sha256"),
        "launch_count": int(attempt.get("launch_count") or 0),
        "accepted_report_count": int(attempt.get("accepted_report_count") or 0),
        "terminal_reason": _text(attempt.get("terminal_reason")),
    }


def _attempt_event_block(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": str(event["event_type"]),
        "reason_code": str(event["reason_code"]),
        "reason": _text(event.get("reason")),
        "sequence": event.get("sequence"),
        "created_at": str(event["created_at"]),
    }


def _delivery_block(delivery: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "delivery_id": str(delivery["delivery_id"]),
        "status": str(delivery["status"]),
        "delivery_sequence": int(delivery["delivery_sequence"]),
        "created_at": str(delivery["created_at"]),
    }


def _relation_block(relation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(relation["kind"]),
        "source_work_id": str(relation["source_work_id"]),
        "target_work_id": str(relation["target_work_id"]),
        "active": bool(relation.get("active")),
    }


def work_record_block(view: ViewInput) -> dict[str, Any]:
    """Render one workflow projection into the bounded trace record.

    Collections are capped per kind and every free-text field is clipped, so the
    block stays a fixed shape with a ``truncated`` map naming what was cut. The
    revision (title, description, scope, acceptance criteria) and the receipt
    identities are always present: a lesson's claims must cite receipts that
    appear in the trace.
    """
    view_mapping = _view_mapping(view)
    item = view_mapping["item"]
    revision = item["revision"]
    candidate = item.get("candidate")
    project = view_mapping.get("project")
    receipts = tuple(view_mapping.get("receipts") or ())
    attempts = tuple(view_mapping.get("close_attempts") or ())
    events = tuple(view_mapping.get("close_attempt_events") or ())
    deliveries = tuple(view_mapping.get("checkpoint_deliveries") or ())
    relations = tuple(view_mapping.get("relations") or ())
    criteria = tuple(revision.get("acceptance_criteria") or ())

    return {
        "work_id": str(item["work_id"]),
        "work_key": str(item["alias"]["key"]),
        "state": str(item["state"]),
        "archived": bool(item.get("archived")),
        "project": (
            {
                "project_id": str(project["project_id"]),
                "name": str(project["name"]),
                "health": project.get("health"),
            }
            if project
            else None
        ),
        "revision": {
            "revision_id": str(revision["revision_id"]),
            "revision_number": int(revision["revision_number"]),
            "title": str(revision["title"]),
            "description": _clip(str(revision.get("description") or "")),
            "scope": _clip(str(revision.get("scope") or "")),
            "acceptance_criteria": [
                _clip(str(criterion))
                for criterion in criteria[:MAX_ACCEPTANCE_CRITERIA]
            ],
            "content_sha256": str(revision.get("content_sha256") or ""),
            "created_by": str(revision.get("created_by") or ""),
            "created_at": str(revision["created_at"]),
        },
        "candidate": (
            {
                "candidate_id": str(candidate["candidate_id"]),
                "kind": str(candidate["kind"]),
                "commit_sha": candidate.get("commit_sha"),
                "candidate_sha256": str(candidate.get("candidate_sha256") or ""),
            }
            if candidate
            else None
        ),
        "receipts": [_receipt_block(row) for row in receipts[:MAX_RECEIPTS]],
        "close_attempts": [
            _attempt_block(row) for row in attempts[:MAX_CLOSE_ATTEMPTS]
        ],
        "close_attempt_events": [
            _attempt_event_block(row) for row in events[:MAX_CLOSE_ATTEMPT_EVENTS]
        ],
        "checkpoint_deliveries": [
            _delivery_block(row) for row in deliveries[:MAX_CHECKPOINT_DELIVERIES]
        ],
        "relations": [_relation_block(row) for row in relations[:MAX_RELATIONS]],
        "truncated": {
            "acceptance_criteria": len(criteria)
            - min(len(criteria), MAX_ACCEPTANCE_CRITERIA),
            "receipts": len(receipts) - min(len(receipts), MAX_RECEIPTS),
            "close_attempts": len(attempts) - min(len(attempts), MAX_CLOSE_ATTEMPTS),
            "close_attempt_events": len(events)
            - min(len(events), MAX_CLOSE_ATTEMPT_EVENTS),
            "checkpoint_deliveries": len(deliveries)
            - min(len(deliveries), MAX_CHECKPOINT_DELIVERIES),
            "relations": len(relations) - min(len(relations), MAX_RELATIONS),
        },
    }


def bounded_work_record(
    records: NativeRecords, work_id: str | UUID
) -> dict[str, Any] | None:
    """The bounded execution record for ``work_id``, or ``None``.

    ``None`` means the item could not be read at all — unresolved alias, a
    refusal, or an unreachable service. The caller traces the event unchanged:
    an unavailable record degrades one unit's evidence, never the capture.
    """
    try:
        key = resolve_work_key(records, work_id)
        if key is None:
            return None
        return work_record_block(records.workflow(key))
    except (WorkError, httpx.HTTPError):
        return None
