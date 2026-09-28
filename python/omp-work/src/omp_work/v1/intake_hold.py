"""Bot-filed: filed_by_kind is recorded and is not owner, or the description after leading whitespace starts with [flood-created]. Approved work: not archived, state not CANCELED or CANCELLED, and not bot-filed or intake-approved. Follow-up: a bot-filed item with an active parent relation from it to approved work, or an active blocks relation between it and approved work in either direction; related and duplicate_of do not count. Every other bot-filed item is new scope, held from flood export until the owner answers approve."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

FLOOD_CREATED_PREFIX = "[flood-created]"
OWNER_FILED_KIND = "owner"
_CANCELLED_STATES = frozenset({"CANCELED", "CANCELLED"})


class ScopeClass(StrEnum):
    owner_filed = "owner_filed"
    follow_up = "follow_up"
    new_scope = "new_scope"


@dataclass(frozen=True)
class IntakeFacts:
    work_id: UUID
    state: str
    archived: bool
    filed_by_kind: str | None
    description: str
    intake_approved: bool


@dataclass(frozen=True)
class Link:
    source_work_id: UUID
    target_work_id: UUID
    kind: str
    active: bool


@dataclass(frozen=True)
class IntakeClassification:
    scope_class: ScopeClass
    held: bool


def _is_bot_filed(item: IntakeFacts) -> bool:
    if item.filed_by_kind is not None and item.filed_by_kind != OWNER_FILED_KIND:
        return True
    return item.description.lstrip().startswith(FLOOD_CREATED_PREFIX)


def _is_approved_work(item: IntakeFacts) -> bool:
    if item.archived or item.state.upper() in _CANCELLED_STATES:
        return False
    return not _is_bot_filed(item) or item.intake_approved


def classify_intake(
    items: Iterable[IntakeFacts], links: Iterable[Link]
) -> dict[UUID, IntakeClassification]:
    by_id = {item.work_id: item for item in items}
    approved = {work_id for work_id, item in by_id.items() if _is_approved_work(item)}
    follow_up: set[UUID] = set()
    for link in links:
        if not link.active or link.kind not in ("parent", "blocks"):
            continue
        if link.source_work_id not in by_id or link.target_work_id not in by_id:
            continue
        if link.kind == "parent":
            if link.target_work_id in approved:
                follow_up.add(link.source_work_id)
        else:
            if link.source_work_id in approved:
                follow_up.add(link.target_work_id)
            if link.target_work_id in approved:
                follow_up.add(link.source_work_id)
    classified: dict[UUID, IntakeClassification] = {}
    for work_id, item in by_id.items():
        if not _is_bot_filed(item):
            classified[work_id] = IntakeClassification(ScopeClass.owner_filed, False)
        elif work_id in follow_up:
            classified[work_id] = IntakeClassification(ScopeClass.follow_up, False)
        else:
            classified[work_id] = IntakeClassification(
                ScopeClass.new_scope, not item.intake_approved
            )
    return classified
