"""Semantic retrieval from the FK-3 knowledge engine into ContextItems."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from omp_knowledge.context.models import ContextItem
from omp_knowledge.engine.protocol import KnowledgeEngine
from omp_knowledge.errors import (
    EngineTimeoutError,
    EngineUnavailableError,
    SnapshotNotPublishedError,
)


def _to_uuid(val: UUID | str) -> Any:
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, TypeError, AttributeError):
        return val


async def semantic_items(
    engine: KnowledgeEngine,
    selection: Sequence[tuple[UUID | str, UUID | str, str]],
    permitted_repo_ids: Collection[UUID | str],
    query_text: str,
    limit: int = 20,
    *,
    source: str = "retrieval",
) -> list[ContextItem]:
    """Retrieve semantic items from the FK-3 knowledge engine.

    - If a repository in selection is not permitted, engine is not called and a
      denied item is emitted.
    - SnapshotNotPublishedError produces a missing item.
    - EngineUnavailableError or EngineTimeoutError produces a missing item with
      the error code in detail.
    - Each FactRecord produces a current item `name kind file:line` with
      ref=fact_id and detail=snapshot_id.
    """
    permitted: set[Any] = set()
    for p in permitted_repo_ids:
        permitted.add(p)
        permitted.add(str(p))
        if isinstance(p, str):
            try:
                permitted.add(UUID(p))
            except (ValueError, TypeError, AttributeError):
                pass

    items: list[ContextItem] = []

    for ws, repo, snap in selection:
        if repo not in permitted and str(repo) not in permitted:
            items.append(
                ContextItem(
                    section="semantic",
                    source=source,
                    ref=str(repo),
                    text=f"repository {repo} not permitted",
                    status="denied",
                    detail="repo not permitted",
                )
            )
            continue

        ws_id = _to_uuid(ws)
        repo_id = _to_uuid(repo)

        try:
            result = await engine.query(
                workspace_id=ws_id,
                repository_id=repo_id,
                snapshot_id=snap,
                query_text=query_text,
                limit=limit,
                require_published=True,
            )
        except SnapshotNotPublishedError as exc:
            items.append(
                ContextItem(
                    section="semantic",
                    source=source,
                    ref=str(snap),
                    text=f"snapshot {snap} not published",
                    status="missing",
                    detail=getattr(exc, "code", "snapshot_not_published"),
                )
            )
            continue
        except (EngineUnavailableError, EngineTimeoutError) as exc:
            items.append(
                ContextItem(
                    section="semantic",
                    source=source,
                    ref=str(snap),
                    text=f"engine {exc.code}: {exc}",
                    status="missing",
                    detail=getattr(exc, "code", "engine_unavailable"),
                )
            )
            continue

        for fact in result.facts:
            if fact.file_path and fact.line is not None:
                loc = f"{fact.file_path}:{fact.line}"
            elif fact.file_path:
                loc = str(fact.file_path)
            else:
                loc = ""
            text = f"{fact.name} {fact.kind} {loc}".strip()
            items.append(
                ContextItem(
                    section="semantic",
                    source=source,
                    ref=fact.fact_id,
                    text=text,
                    status="current",
                    detail=fact.snapshot_id,
                    mandatory=False,
                    score=None,
                )
            )

    return items


__all__ = ["semantic_items"]
