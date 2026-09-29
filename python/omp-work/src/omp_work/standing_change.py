"""Standing change authority and authorization rules (OMP-418)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = [
    "KNOWN_ACTOR_KINDS",
    "WORKER_ACTOR_KINDS",
    "ChangeAuthority",
    "ChangeKind",
    "StandingChangeRefused",
    "authorize_standing_change",
    "owner_signed",
]


class ChangeKind(StrEnum):
    create = "create"
    widen = "widen"
    extend = "extend"
    narrow = "narrow"
    revoke = "revoke"


KNOWN_ACTOR_KINDS: frozenset[str] = frozenset(
    {"owner", "grokbot", "automation", "task-agent"}
)
WORKER_ACTOR_KINDS: frozenset[str] = frozenset({"automation", "task-agent"})


@dataclass(frozen=True)
class ChangeAuthority:
    requested_by_kind: str
    decision_id: UUID | str | None = None
    answered_by_kind: str | None = None


def owner_signed(authority: ChangeAuthority) -> bool:
    """Return whether the change authority carries the owner signature."""
    return authority.answered_by_kind == "owner"


class StandingChangeRefused(Exception):
    """Raised when a standing change is refused by authority rules."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def authorize_standing_change(
    kind: ChangeKind | str,
    authority: ChangeAuthority,
) -> None:
    """Authorize a standing change against change authority rules.

    Raises StandingChangeRefused(code) if authorization fails.
    """
    try:
        change_kind = ChangeKind(kind)
    except ValueError:
        raise StandingChangeRefused(
            "unknown_kind", f"Unknown change kind: {kind!r}"
        )

    if authority.requested_by_kind not in KNOWN_ACTOR_KINDS:
        raise StandingChangeRefused(
            "unknown_actor", f"Unknown actor kind: {authority.requested_by_kind!r}"
        )

    if change_kind in (ChangeKind.narrow, ChangeKind.revoke):
        return

    # create, widen, extend
    if authority.requested_by_kind in WORKER_ACTOR_KINDS:
        raise StandingChangeRefused(
            "worker_not_permitted",
            f"Worker actor {authority.requested_by_kind!r} cannot request {change_kind.value}",
        )

    if not authority.decision_id:
        raise StandingChangeRefused(
            "decision_required",
            f"A decision_id is required for {change_kind.value}",
        )

    if not owner_signed(authority):
        raise StandingChangeRefused(
            "owner_signature_required",
            f"Decision answer must be 'owner', got {authority.answered_by_kind!r}",
        )
