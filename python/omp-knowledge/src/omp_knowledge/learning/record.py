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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx
from omp_work.v1.api_models import ResearchView, WorkflowView, WorkItemsPage
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import Candidate, EvidenceReceipt, ResearchDeliverableBinding
from omp_work.v1.service import WorkError
from pydantic import ValidationError

__all__ = [
    "MAX_KEY_SCAN",
    "NativeRecords",
    "NativeResearchRecords",
    "ReceiptMalformed",
    "ReceiptMissing",
    "ResolvedSource",
    "WorkflowMalformed",
    "WorkflowMissing",
    "bounded_capture_records",
    "bounded_work_record",
    "evidence_is_fresh",
    "research_record_block",
    "resolve_cited_receipt",
    "resolve_source_workflow",
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
MAX_RESEARCH_CAMPAIGNS = 32
MAX_RESEARCH_REFS = 8

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


@runtime_checkable
class NativeResearchRecords(NativeRecords, Protocol):
    """Execution records plus the work item's native research projection."""

    def research(self, key: str) -> ResearchView: ...


def research_record_block(view: ResearchView) -> dict[str, Any]:
    """Render concluded same-work campaigns and bounded candidate references.

    Sort before capping so input order cannot change which evidence is visible.
    Row omissions are counted; clipped reasons carry the shared text marker.
    """
    campaigns = sorted(
        (
            campaign for campaign in view.campaigns
            if campaign.work_id == view.work_id and campaign.state == "concluded"
        ),
        key=lambda campaign: campaign.campaign_id,
    )
    bindings_by_campaign: dict[UUID, list[ResearchDeliverableBinding]] = {}
    for binding in view.deliverable_bindings:
        if binding.work_id == view.work_id:
            bindings_by_campaign.setdefault(binding.campaign_id, []).append(binding)

    rendered = []
    for campaign in campaigns[:MAX_RESEARCH_CAMPAIGNS]:
        bindings = sorted(
            bindings_by_campaign.get(campaign.campaign_id, ()),
            key=lambda binding: binding.trial_id,
        )
        rendered.append({
            "campaign_id": str(campaign.campaign_id),
            "outcome": campaign.outcome,
            "outcome_reason": _text(campaign.outcome_reason),
            "deliverable_refs": [
                {
                    "trial_id": str(binding.trial_id),
                    "native_candidate_id": str(binding.native_candidate_id),
                    "candidate_digest": binding.candidate_digest,
                    "binding_sha256": binding.binding_sha256,
                }
                for binding in bindings[:MAX_RESEARCH_REFS]
            ],
            "truncated": {"deliverable_refs": max(0, len(bindings) - MAX_RESEARCH_REFS)},
        })
    return {
        "work_id": str(view.work_id),
        "campaigns": rendered,
        "truncated": {"campaigns": max(0, len(campaigns) - MAX_RESEARCH_CAMPAIGNS)},
    }


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


def _read_workflow(records: NativeRecords, key: str) -> dict[str, Any] | None:
    try:
        return work_record_block(records.workflow(key))
    except (WorkError, httpx.HTTPError):
        return None


def _read_research(records: NativeRecords, key: str) -> dict[str, Any] | None:
    research = getattr(records, "research", None)
    if not callable(research):
        return None
    try:
        view = research(key)
        if isinstance(view, Mapping):
            view = ResearchView.model_validate(view)
        return research_record_block(view)
    except (WorkError, httpx.HTTPError):
        return None


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
        return _read_workflow(records, key)
    except (WorkError, httpx.HTTPError):
        return None


class ReceiptMissing(LookupError):
    """A cited receipt is definitively absent (no row, unknown id, or 404)."""


class ReceiptMalformed(ValueError):
    """A cited id resolved to something that is not an evidence receipt."""


class WorkflowMalformed(ValueError):
    """The source workflow payload is not a workflow view or a workflow mapping."""


class WorkflowMissing(LookupError):
    """The source alias resolved, and the workflow itself is definitively gone."""


@dataclass(frozen=True)
class ResolvedSource:
    """Revision and candidate identity read from a workflow projection.

    A complete view also carries the candidate model, including its hash and
    commit. A mapping that only names the ids still identifies the source; it
    cannot prove a receipt that recorded a hash.
    """

    work_id: str | None
    revision_id: str | None
    candidate_id: str | None
    candidate: Candidate | None


def _is_permission(error: WorkError) -> bool:
    return error.status in {401, 403} or error.code == "forbidden"


def _is_definitive_absence(error: WorkError) -> bool:
    """A missing row. The service reports an unknown receipt as invalid_request/400."""
    if _is_permission(error):
        return False
    return error.status == 404 or error.code in {"not_found", "invalid_request"}


def _same_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _text_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def evidence_is_fresh(
    receipt: EvidenceReceipt,
    candidate: Candidate | None,
    revision_id: UUID | str,
    *,
    work_id: UUID | str | None = None,
    candidate_id: UUID | str | None = None,
) -> bool:
    """Whether a cited receipt still supports this candidate and revision.

    Lessons admitted from receipts that never recorded a candidate hash or
    commit stay fresh when the work, revision, and candidate ids still match.
    The work-service check also requires the hash and commit, which those
    receipts do not carry. A receipt that does record either value is fresh
    only when that check passes against a complete candidate.
    """
    revision = revision_id if isinstance(revision_id, UUID) else UUID(str(revision_id))
    if receipt.candidate_sha256 is None and receipt.candidate_commit is None:
        bound_work = work_id if work_id is not None else (
            None if candidate is None else candidate.work_id
        )
        bound_candidate = candidate_id if candidate_id is not None else (
            None if candidate is None else candidate.candidate_id
        )
        return (
            _same_id(receipt.work_id, bound_work)
            and _same_id(receipt.revision_id, revision)
            and _same_id(receipt.candidate_id, bound_candidate)
        )
    if candidate is None:
        return False
    from omp_work.v1.semantics import evidence_is_fresh as candidate_fresh

    return candidate_fresh(receipt, candidate, revision)


def _resolved_from_view(view: WorkflowView) -> ResolvedSource:
    candidate = view.item.candidate
    return ResolvedSource(
        work_id=str(view.item.work_id),
        revision_id=str(view.item.revision.revision_id),
        candidate_id=None if candidate is None else str(candidate.candidate_id),
        candidate=candidate,
    )


def _resolved_from_mapping(raw: Mapping[str, Any]) -> ResolvedSource:
    """Read source identity from a workflow view or the mapping capture already accepts.

    Test doubles often return the bounded fields (work, revision, candidate ids)
    without the rest of a workflow view. That mapping is still the source.
    A payload with no item mapping is malformed.
    """
    try:
        return _resolved_from_view(WorkflowView.model_validate(raw))
    except ValidationError:
        pass
    item = raw.get("item")
    if not isinstance(item, Mapping):
        raise WorkflowMalformed("workflow item missing")
    revision = item.get("revision")
    revision_id = _text_id(revision.get("revision_id")) if isinstance(revision, Mapping) else None
    candidate_raw = item.get("candidate")
    candidate_model: Candidate | None = None
    candidate_id: str | None = None
    if isinstance(candidate_raw, Candidate):
        candidate_model = candidate_raw
        candidate_id = str(candidate_raw.candidate_id)
    elif isinstance(candidate_raw, Mapping):
        candidate_id = _text_id(candidate_raw.get("candidate_id"))
        try:
            candidate_model = Candidate.model_validate(candidate_raw)
        except ValidationError:
            candidate_model = None
        else:
            candidate_id = str(candidate_model.candidate_id)
    elif candidate_raw is not None:
        raise WorkflowMalformed("workflow candidate")
    work_id = _text_id(item.get("work_id"))
    if work_id is None and candidate_model is not None:
        work_id = str(candidate_model.work_id)
    return ResolvedSource(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        candidate=candidate_model,
    )


def resolve_cited_receipt(receipts: Any, receipt_id: str | UUID) -> EvidenceReceipt:
    """Resolve one cited receipt.

    ``None``, ``KeyError``, HTTP 404, and the service's ``invalid_request`` for
    a missing row become :class:`ReceiptMissing`. A payload that is not an
    :class:`EvidenceReceipt` becomes :class:`ReceiptMalformed`. Transport errors
    and permission failures propagate.
    """
    try:
        raw = receipts.receipt(receipt_id)
    except KeyError as error:
        raise ReceiptMissing(str(receipt_id)) from error
    except WorkError as error:
        if _is_permission(error):
            raise
        if _is_definitive_absence(error):
            raise ReceiptMissing(str(receipt_id)) from error
        raise
    if raw is None:
        raise ReceiptMissing(str(receipt_id))
    if isinstance(raw, EvidenceReceipt):
        return raw
    try:
        return EvidenceReceipt.model_validate(raw)
    except ValidationError as error:
        raise ReceiptMalformed(str(receipt_id)) from error


def resolve_source_workflow(
    records: NativeRecords, source: Any
) -> ResolvedSource | None:
    """The source identity for a lesson.

    ``None`` means this records seam did not resolve the work id. A reader that
    only lists the item it is capturing leaves other lessons unresolved; that
    is not evidence the candidate is gone. Once the alias is resolved, a
    missing workflow (``None``, ``KeyError``, 404, or ``invalid_request``)
    raises :class:`WorkflowMissing`. Transport errors and permission failures
    propagate. A payload that is neither a workflow view nor a workflow
    mapping raises :class:`WorkflowMalformed`. A mapping that names the
    revision and candidate is the source even when the rest of the view is
    absent.
    """
    work_id = getattr(source, "work_id", None)
    if not isinstance(work_id, str) or not work_id.strip():
        return None
    try:
        key = resolve_work_key(records, work_id)
    except KeyError:
        return None
    except WorkError as error:
        if _is_permission(error):
            raise
        if _is_definitive_absence(error):
            return None
        raise
    if key is None:
        return None
    try:
        raw = records.workflow(key)
    except KeyError as error:
        raise WorkflowMissing(work_id) from error
    except WorkError as error:
        if _is_permission(error):
            raise
        if _is_definitive_absence(error):
            raise WorkflowMissing(work_id) from error
        raise
    if raw is None:
        raise WorkflowMissing(work_id)
    if isinstance(raw, WorkflowView):
        return _resolved_from_view(raw)
    if isinstance(raw, Mapping):
        return _resolved_from_mapping(raw)
    raise WorkflowMalformed(work_id)


def bounded_capture_records(
    records: NativeRecords, work_id: str | UUID
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The bounded execution and research records for ``work_id``.

    ``work_id`` is resolved once via ``resolve_work_key``. The workflow and
    research projections are read independently: an error on one does not
    prevent reading the other. Unresolvable keys yield ``(None, None)``.
    Missing research capability or an unavailable read yields ``None`` for
    the research record.
    """
    try:
        key = resolve_work_key(records, work_id)
        if key is None:
            return None, None
        return _read_workflow(records, key), _read_research(records, key)
    except (WorkError, httpx.HTTPError):
        return None, None

