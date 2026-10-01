"""Item budget accounting and threshold alerts on the native jobs substrate (OMP-404).

An item's budget is the ``payload.draft.budget`` of its ``intake_publication``
receipt, which OMP-404-s01 makes mandatory before a bounded intake publishes.
Spend is measured across the item's native job tree — the jobs of the root work
item plus every ``parent_job_id`` descendant, whichever work item it belongs to
— plus the item's own job-less usage.

When spend reaches 50%, 80%, or 100% of a dimension's limit, one
``budget_alert`` outbox row is written in ``committed`` state. The event id is
the stable hash of (workspace, work, dimension, threshold), so re-checking after
every new usage row never writes the same alert twice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from omp_work.contracts.v1.recovery import CloseoutRecord, apply_commit
from omp_work.jobs.cancel import cancel_item_tree
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.canonical import sha256
from omp_work.v1.models import ItemBudget

__all__ = [
    "ItemSpend",
    "alert_subagents_exceeded",
    "check_item_budget",
    "item_budget",
    "item_spend",
    "root_work_id",
    "sweep_item_budgets",
]

_THRESHOLD_PERCENTS = (50, 80)
_EXCEEDED_PERCENT = 100
_THRESHOLD_EVENT = "budget.threshold_reached"
_EXCEEDED_EVENT = "budget.exceeded"
_BUDGET_REASON = "budget_exceeded"


@dataclass(frozen=True)
class ItemSpend:
    """Accumulated spend of one item's job tree and job-less usage."""

    usd: Decimal
    tokens: int
    wall_clock_seconds: int
    subagents: int


def item_budget(
    cur: psycopg.Cursor[dict[str, Any]], workspace_id: UUID, work_id: UUID
) -> ItemBudget | None:
    """The item's published budget, or None when it has no ``intake_publication`` receipt.

    The receipt is keyed to the published work item; a legacy publication whose
    draft carries no budget is unbudgeted, exactly like a missing receipt.
    """
    cur.execute(
        """
        SELECT payload FROM omp_evidence.receipts
        WHERE workspace_id=%s AND work_id=%s AND kind='intake_publication'
        ORDER BY issued_at DESC, receipt_id DESC
        LIMIT 1
        """,
        (workspace_id, work_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        return None
    raw = draft.get("budget")
    if raw is None:
        return None
    return ItemBudget.model_validate(raw)


def root_work_id(
    cur: psycopg.Cursor[dict[str, Any]], workspace_id: UUID, job_id: str
) -> UUID | None:
    """Work id at the top of ``job_id``'s ``parent_job_id`` chain, or None.

    The walk is bounded so a malformed self-parent cannot loop; an unknown job
    or a job with no work item returns None.
    """
    cur.execute(
        "SELECT job_id, work_id, parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
        (workspace_id, job_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    top_work_id = row["work_id"]
    seen = {str(row["job_id"])}
    current = row["parent_job_id"]
    while current is not None and str(current) not in seen:
        seen.add(str(current))
        cur.execute(
            "SELECT job_id, work_id, parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
            (workspace_id, current),
        )
        parent = cur.fetchone()
        if parent is None:
            break
        top_work_id = parent["work_id"]
        current = parent["parent_job_id"]
    return None if top_work_id is None else UUID(str(top_work_id))


def item_spend(
    cur: psycopg.Cursor[dict[str, Any]], workspace_id: UUID, work_id: UUID
) -> ItemSpend:
    """Spend of the item tree: jobs of ``work_id`` plus ``parent_job_id`` descendants.

    ``usd`` and ``tokens`` sum the tree's usage plus this work item's job-less
    usage. ``tokens`` is input + output only. ``wall_clock_seconds`` is the age
    of the oldest tree job; ``subagents`` counts tree jobs that have a parent.
    """
    cur.execute(
        """
        WITH RECURSIVE tree(job_id, parent_job_id, created_at) AS (
            SELECT job_id, parent_job_id, created_at
            FROM omp_jobs.jobs
            WHERE workspace_id=%s AND work_id=%s
            UNION
            SELECT j.job_id, j.parent_job_id, j.created_at
            FROM omp_jobs.jobs j
            JOIN tree t ON j.parent_job_id = t.job_id
            WHERE j.workspace_id=%s
        )
        SELECT
            COALESCE(EXTRACT(EPOCH FROM (clock_timestamp() - MIN(created_at)))::bigint, 0)
                AS wall_clock_seconds,
            COUNT(*) FILTER (WHERE parent_job_id IS NOT NULL) AS subagents,
            COALESCE(array_agg(job_id), '{}') AS job_ids
        FROM tree
        """,
        (workspace_id, work_id, workspace_id),
    )
    tree_row = cur.fetchone()
    job_ids = [str(job_id) for job_id in tree_row["job_ids"]]
    wall_clock_seconds = int(tree_row["wall_clock_seconds"])
    subagents = int(tree_row["subagents"])

    cur.execute(
        """
        SELECT
            COALESCE(SUM(price_usd), 0) AS usd,
            COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
                AS tokens
        FROM omp_jobs.usage_events
        WHERE workspace_id=%s
          AND (job_id_derived = ANY(%s::text[]) OR (job_id_derived IS NULL AND work_id=%s))
        """,
        (workspace_id, job_ids, work_id),
    )
    usage_row = cur.fetchone()
    return ItemSpend(
        usd=Decimal(str(usage_row["usd"])),
        tokens=int(usage_row["tokens"]),
        wall_clock_seconds=wall_clock_seconds,
        subagents=subagents,
    )


def check_item_budget(
    store: NativeJobStore,
    cur: psycopg.Cursor[dict[str, Any]],
    *,
    workspace_id: UUID,
    actor_id: UUID,
    work_id: UUID,
    operation_id: str,
) -> tuple[str, ...]:
    """Write one outbox alert per reached threshold; return the exhausted dimensions.

    An item with no published budget is a no-op. ``usd``, ``tokens`` and
    ``wall_clock_seconds`` alert at 50% and 80% (``budget.threshold_reached``)
    and at 100% (``budget.exceeded``), which also reports the dimension as
    exhausted once spend reaches the limit. ``subagents`` alerts at 50% and 80%
    of ``max_subagents`` only, and never reports the dimension exhausted. Each
    alert is keyed by (workspace, work, dimension, threshold) and inserted with
    ON CONFLICT DO NOTHING, so a repeated check writes nothing.

    When a dimension is exhausted the item's job tree is stopped in the same
    transaction (``cancel_item_tree``, reason ``budget_exceeded``); the alert
    rows and the cancellation commit together. An alert-only dimension does not
    stop anything.
    """
    budget = item_budget(cur, workspace_id, work_id)
    if budget is None:
        return ()
    spend = item_spend(cur, workspace_id, work_id)

    exhausted: list[str] = []
    for dimension, spent, limit in (
        ("usd", spend.usd, Decimal(budget.usd)),
        ("tokens", spend.tokens, budget.tokens),
        ("wall_clock_seconds", spend.wall_clock_seconds, budget.wall_clock_seconds),
    ):
        for threshold in _THRESHOLD_PERCENTS:
            if _reached(spent, limit, threshold):
                _alert(
                    cur,
                    workspace_id=workspace_id,
                    work_id=work_id,
                    operation_id=operation_id,
                    event=_THRESHOLD_EVENT,
                    dimension=dimension,
                    threshold=threshold,
                    spent=spent,
                    limit=limit,
                )
        if spent >= limit:
            _alert(
                cur,
                workspace_id=workspace_id,
                work_id=work_id,
                operation_id=operation_id,
                event=_EXCEEDED_EVENT,
                dimension=dimension,
                threshold=_EXCEEDED_PERCENT,
                spent=spent,
                limit=limit,
            )
            exhausted.append(dimension)

    if budget.max_subagents > 0:
        for threshold in _THRESHOLD_PERCENTS:
            if _reached(spend.subagents, budget.max_subagents, threshold):
                _alert(
                    cur,
                    workspace_id=workspace_id,
                    work_id=work_id,
                    operation_id=operation_id,
                    event=_THRESHOLD_EVENT,
                    dimension="subagents",
                    threshold=threshold,
                    spent=spend.subagents,
                    limit=budget.max_subagents,
                )

    if exhausted:
        cancel_item_tree(
            store,
            cur,
            workspace_id=workspace_id,
            actor_id=actor_id,
            operation_id=operation_id,
            work_id=work_id,
            reason=_BUDGET_REASON,
        )

    return tuple(exhausted)


def sweep_item_budgets(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
) -> list[UUID]:
    """Check the workspace's budgeted items that have live jobs; return stopped work ids.

    Wall-clock is the dimension a sweep exists for: it rises with no new usage
    row to trigger a check. Only work items that carry an ``intake_publication``
    budget and have at least one non-terminal job are visited, each in its own
    transaction. An item whose budget is exhausted has its tree cancelled by
    ``check_item_budget``; its work id is returned, in work id order. Items that
    are unbudgeted, already fully settled, or merely alerting are not returned.
    """
    with store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            SELECT DISTINCT j.work_id
            FROM omp_jobs.jobs j
            WHERE j.workspace_id=%s AND j.source='native' AND j.work_id IS NOT NULL
              AND j.status IN ('backlog', 'admitted', 'in_flight', 'returned', 'checking')
              AND EXISTS (
                SELECT 1 FROM omp_evidence.receipts r
                WHERE r.workspace_id=j.workspace_id AND r.work_id=j.work_id
                  AND r.kind='intake_publication'
              )
            ORDER BY j.work_id
            """,
            (workspace_id,),
        )
        work_ids = [UUID(str(row["work_id"])) for row in cur.fetchall()]

    stopped: list[UUID] = []
    for work_id in work_ids:
        with store.transaction(workspace_id, actor_id) as cur:
            if check_item_budget(
                store,
                cur,
                workspace_id=workspace_id,
                actor_id=actor_id,
                work_id=work_id,
                operation_id=operation_id,
            ):
                stopped.append(work_id)
    return stopped


def alert_subagents_exceeded(
    cur: psycopg.Cursor[dict[str, Any]],
    *,
    workspace_id: UUID,
    work_id: UUID,
    operation_id: str,
    maximum: int,
    count: int,
) -> None:
    """Write the ``budget.exceeded`` subagents alert for an item at its cap.

    ``check_item_budget`` never reports subagents exhausted, because the count
    only rises at enqueue and the refuser writes that row itself. Like every
    other budget alert it is keyed by (workspace, work, dimension, threshold),
    so a second refusal writes nothing.
    """
    _alert(
        cur,
        workspace_id=workspace_id,
        work_id=work_id,
        operation_id=operation_id,
        event=_EXCEEDED_EVENT,
        dimension="subagents",
        threshold=_EXCEEDED_PERCENT,
        spent=count,
        limit=maximum,
    )


def _reached(spent: object, limit: object, threshold: int) -> bool:
    """Percent comparison without float division: ``spent/limit >= threshold/100``."""
    return Decimal(spent) * 100 >= Decimal(limit) * threshold


def _alert(
    cur: psycopg.Cursor[dict[str, Any]],
    *,
    workspace_id: UUID,
    work_id: UUID,
    operation_id: str,
    event: str,
    dimension: str,
    threshold: int,
    spent: object,
    limit: object,
) -> None:
    event_id = sha256(
        {
            "workspace_id": str(workspace_id),
            "work_id": str(work_id),
            "dimension": dimension,
            "threshold": threshold,
        }
    )
    payload: dict[str, object] = {
        "event": event,
        "dimension": dimension,
        "threshold_percent": threshold,
        "spent": str(spent) if dimension == "usd" else int(spent),
        "limit": str(limit) if dimension == "usd" else int(limit),
        "work_id": str(work_id),
    }
    closeout = apply_commit(CloseoutRecord(event_id, "open"))
    cur.execute(
        """
        INSERT INTO omp_jobs.outbox(
            event_id, workspace_id, operation_id, kind, payload, state, revision
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            event_id,
            workspace_id,
            operation_id,
            "budget_alert",
            Jsonb(payload),
            closeout.state,
            closeout.revision,
        ),
    )
