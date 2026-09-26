"""Exact, structural and procedural retrieval into context items (OMP-311 / FK-6).

Three retrieval sources feed one :class:`~omp_knowledge.context.models.CompileRequest`:

- :func:`identity_from_view` binds the compilation to one exact Work identity read
  from a ``WorkflowView`` JSON;
- :func:`exact_items` emits the mandatory source revision plus one item per
  receipt, marking a receipt stale when it names a different revision or
  candidate than the item it is attached to;
- :func:`structural_items` reads symbol maps from *explicitly selected* published
  snapshots only — never the latest — refusing a repository that is not
  permitted before it touches the store;
- :func:`procedural_items` reads the FK-6 learning store read-only, applying each
  procedure's preconditions against the supplied context.

Nothing here reads a clock or the environment; every function is a pure mapping
from durable state to items, so the compiler's determinism is preserved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.v1.api_models import WorkflowView

from ..learning.models import Precondition
from .models import ContextItem, Stage, StageIdentity

__all__ = [
    "exact_items",
    "identity_from_view",
    "procedural_items",
    "structural_items",
]

ViewInput = WorkflowView | Mapping[str, Any]
"""A ``WorkflowView`` or its JSON dump; both read the same fields."""


def _view_mapping(view: ViewInput) -> Mapping[str, Any]:
    """Normalize a ``WorkflowView`` model or its JSON dump to a mapping."""
    if isinstance(view, Mapping):
        return view
    return view.model_dump(mode="json")


def identity_from_view(view: ViewInput, stage: Stage, attempt_id: str) -> StageIdentity:
    """Bind a compile to the exact work/stage/attempt identity of a workflow view."""
    item = _view_mapping(view)["item"]
    candidate = item.get("candidate")
    candidate_id = str(candidate["candidate_id"]) if candidate is not None else "none"
    return StageIdentity(
        work_id=str(item["work_id"]),
        work_key=str(item["alias"]["key"]),
        revision_id=str(item["revision"]["revision_id"]),
        stage=stage,
        attempt_id=attempt_id,
        candidate_id=candidate_id,
    )


def _revision_text(revision: Mapping[str, Any]) -> str:
    """The source revision as title, description, scope and numbered criteria."""
    lines = [
        str(revision.get("title", "")),
        str(revision.get("description", "")),
        str(revision.get("scope", "")),
    ]
    lines.extend(
        f"{position}. {criterion}"
        for position, criterion in enumerate(revision.get("acceptance_criteria") or (), start=1)
    )
    return "\n".join(lines)


def _receipt_ids(receipt: Mapping[str, Any]) -> tuple[str, str]:
    revision_id = str(receipt["revision_id"])
    candidate_id = receipt.get("candidate_id")
    return revision_id, str(candidate_id) if candidate_id is not None else "none"


def exact_items(view: ViewInput) -> tuple[ContextItem, ...]:
    """The exact section: the source revision and one item per receipt.

    A receipt is ``current`` only when both its revision and candidate match the
    item's; any other revision or candidate is ``stale`` and its detail names
    both the receipt's and the item's ids.
    """
    view = _view_mapping(view)
    item = view["item"]
    revision = item["revision"]
    revision_id = str(revision["revision_id"])
    candidate = item.get("candidate")
    candidate_id = str(candidate["candidate_id"]) if candidate is not None else "none"

    items: list[ContextItem] = [
        ContextItem(
            section="exact",
            source="work",
            ref=revision_id,
            text=_revision_text(revision),
            status="current",
            mandatory=True,
        )
    ]

    for receipt in sorted(view.get("receipts") or (), key=lambda row: str(row["receipt_id"])):
        receipt_revision, receipt_candidate = _receipt_ids(receipt)
        verdict = receipt.get("verdict") or "-"
        text = (
            f"{receipt['kind']} {verdict} receipt={receipt['receipt_id']} "
            f"payload_sha256={receipt['payload_sha256']}"
        )
        if receipt_revision == revision_id and receipt_candidate == candidate_id:
            items.append(
                ContextItem(
                    section="exact",
                    source="receipt",
                    ref=str(receipt["receipt_id"]),
                    text=text,
                    status="current",
                )
            )
        else:
            items.append(
                ContextItem(
                    section="exact",
                    source="receipt",
                    ref=str(receipt["receipt_id"]),
                    text=text,
                    status="stale",
                    detail=(
                        f"receipt revision={receipt_revision} candidate={receipt_candidate}; "
                        f"item revision={revision_id} candidate={candidate_id}"
                    ),
                )
            )
    return tuple(items)


def structural_items(
    store: StructuralPublicationStore,
    selection: Sequence[tuple[UUID, UUID, str]],
    permitted_repo_ids: Collection[str | UUID],
    limit: int,
) -> tuple[ContextItem, ...]:
    """Symbol-map items from explicitly selected published snapshots.

    A snapshot of a repository outside ``permitted_repo_ids`` is ``denied``
    without a store query; a selection that is not published is ``missing``.
    Only the caller's ``selection`` is read — never the repository's latest
    published snapshot.
    """
    permitted = {str(repository_id) for repository_id in permitted_repo_ids}
    items: list[ContextItem] = []
    for workspace_id, repository_id, snapshot_id in selection:
        repository_key = str(repository_id)
        if repository_key not in permitted:
            items.append(
                ContextItem(
                    section="structural",
                    source="enola",
                    ref=snapshot_id,
                    text=f"repository {repository_key} not permitted",
                    status="denied",
                    detail=f"repository {repository_key} is not permitted",
                )
            )
            continue
        if not store.is_published(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ):
            items.append(
                ContextItem(
                    section="structural",
                    source="enola",
                    ref=snapshot_id,
                    text=f"snapshot {snapshot_id} missing",
                    status="missing",
                    detail="snapshot is not published",
                )
            )
            continue
        result = store.query(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            text="*",
            limit=limit,
        )
        for hit in result.hits:
            line = hit.line if hit.line is not None else 0
            items.append(
                ContextItem(
                    section="structural",
                    source="enola",
                    ref=hit.structural_fact_id,
                    text=f"{hit.file}:{line} {hit.kind} {hit.name}",
                    status="current",
                )
            )
    return tuple(items)


def _preconditions_hold(
    preconditions: Sequence[Precondition], context: Mapping[str, str]
) -> bool:
    """Every precondition must hold: ``eq`` equal, ``ne`` unequal, else skip."""
    for precondition in preconditions:
        actual = context.get(precondition.key)
        actual_value = None if actual is None else str(actual)
        if precondition.op == "eq":
            if actual_value != precondition.value:
                return False
        elif actual_value == precondition.value:
            return False
    return True


def procedural_items(
    db_path: str | Path, context: Mapping[str, str]
) -> tuple[ContextItem, ...]:
    """Procedural items from the FK-6 learning store, opened read-only.

    The store file is opened with a ``mode=ro`` SQLite URI, so the retrieval
    never writes to it. Each procedure is read at its ``current_version``;
    withdrawn procedures become ``withdrawn`` items and a precondition that does
    not hold omits the procedure entirely. An absent store is a ``missing``
    item.
    """
    path = Path(db_path)
    if not path.exists():
        return (
            ContextItem(
                section="procedural",
                source="lesson",
                ref="learning-store",
                text="learning store absent",
                status="missing",
                detail=f"no learning store at {path}",
            ),
        )

    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                p.procedure_id, p.status, p.current_version,
                v.title, v.steps_json, v.preconditions_json
            FROM procedures AS p
            JOIN procedure_versions AS v
              ON v.procedure_id = p.procedure_id AND v.version = p.current_version
            ORDER BY p.procedure_id
            """
        ).fetchall()
    finally:
        connection.close()

    items: list[ContextItem] = []
    for row in rows:
        preconditions = tuple(
            Precondition.model_validate(raw)
            for raw in json.loads(row["preconditions_json"])
        )
        if not _preconditions_hold(preconditions, context):
            continue
        steps = tuple(str(step) for step in json.loads(row["steps_json"]))
        text = f"{row['title']}: " + "; ".join(steps)
        ref = f"{row['procedure_id']} @v{row['current_version']}"
        if row["status"] == "withdrawn":
            items.append(
                ContextItem(
                    section="procedural",
                    source="lesson",
                    ref=ref,
                    text=text,
                    status="withdrawn",
                    detail="procedure withdrawn",
                )
            )
        else:
            items.append(
                ContextItem(
                    section="procedural",
                    source="lesson",
                    ref=ref,
                    text=text,
                    status="current",
                )
            )
    return tuple(items)
