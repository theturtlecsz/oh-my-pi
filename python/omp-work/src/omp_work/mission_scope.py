"""D29 mission-scope rules over mission-draft mappings.

Inputs match a mission draft's ``model_dump(mode="json")``: ``project_id``,
``objective``, ``acceptance_criteria``, ``repositories``,
``requested_capabilities``, ``approval_classes``, and ``budget_policy``
(``None`` or ``usd`` / ``tokens`` / ``wall_clock_seconds`` / ``max_subagents``).
No database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "ALLOWED_TRANSITIONS",
    "MISSION_STATUSES",
    "TERMINAL_STATUSES",
    "material_cases",
    "outside_envelope",
    "transition_allowed",
]

MISSION_STATUSES: tuple[str, ...] = (
    "draft",
    "awaiting_confirmation",
    "approved",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "abandoned",
)

TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "abandoned")

# Edges other than the shared non-terminal exit to abandoned.
_OPEN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"awaiting_confirmation", "blocked"}),
    "awaiting_confirmation": frozenset({"approved", "blocked"}),
    "approved": frozenset({"running", "paused", "blocked", "awaiting_confirmation"}),
    "running": frozenset({"paused", "blocked", "completed", "failed", "awaiting_confirmation"}),
    "paused": frozenset({"running", "blocked", "awaiting_confirmation"}),
    "blocked": frozenset({"awaiting_confirmation", "running", "paused", "failed"}),
}

# Every non-terminal status may move to abandoned. Terminals have no exits.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    status: (
        frozenset()
        if status in TERMINAL_STATUSES
        else _OPEN_TRANSITIONS[status] | {"abandoned"}
    )
    for status in MISSION_STATUSES
}

_DRAFT_KEYS: tuple[str, ...] = (
    "project_id",
    "objective",
    "acceptance_criteria",
    "repositories",
    "requested_capabilities",
    "approval_classes",
    "budget_policy",
)

_BUDGET_INT_FIELDS: tuple[str, ...] = ("tokens", "wall_clock_seconds", "max_subagents")

_OWNER_GATE_CODES: frozenset[str] = frozenset({"new", "c", "undecidable"})

_UNDECIDABLE: tuple[str, ...] = ("undecidable",)


@dataclass(frozen=True)
class _Budget:
    usd: Decimal
    tokens: int
    wall_clock_seconds: int
    max_subagents: int


@dataclass(frozen=True)
class _Draft:
    project_id: str
    objective: str
    criteria: frozenset[str]
    repositories: frozenset[str]
    capabilities: frozenset[str]
    approval_classes: frozenset[str]
    budget: _Budget | None


def transition_allowed(current: str, target: str) -> bool:
    """Return whether ``current`` may move directly to ``target``."""
    if current not in MISSION_STATUSES or target not in MISSION_STATUSES:
        return False
    return target in ALLOWED_TRANSITIONS[current]


def material_cases(approved: Any, revised: Any) -> tuple[str, ...]:
    """Return sorted material-change codes between an approved draft and a revision.

    ``approved is None`` is ``("new",)``. Otherwise the codes are:

    - ``a``: a criterion whose whitespace-collapsed text is new
    - ``b``: an approved criterion is gone
    - ``c``: a different ``project_id``, or a repository that was not approved
    - ``d``: a capability or approval class that was not approved
    - ``e``: any budget field is higher (``usd`` as ``Decimal``), or the budget was removed
    - ``f``: ``objective`` differs after whitespace collapse and casefold

    A missing required key or a wrong type is ``("undecidable",)`` and no other code.
    Narrowing (fewer criteria, repositories, capabilities, or approval classes),
    a lower-or-equal budget, reordered criteria, and ignored fields such as
    constraints are not codes.
    """
    if approved is None:
        return ("new",)
    approved_draft = _parse_draft(approved)
    revised_draft = _parse_draft(revised)
    if approved_draft is None or revised_draft is None:
        return _UNDECIDABLE
    return _compare(approved_draft, revised_draft)


def outside_envelope(cases: Iterable[str]) -> bool:
    """Return whether these codes require the E4 owner gate.

    True when ``new``, ``c``, or ``undecidable`` is present.
    """
    return any(code in _OWNER_GATE_CODES for code in cases)


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _norm_objective(text: str) -> str:
    return _collapse_ws(text).casefold()


def _string_set(value: Any, *, collapse: bool) -> frozenset[str] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    items: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        items.add(_collapse_ws(item) if collapse else item)
    return frozenset(items)


def _parse_budget(value: Any) -> _Budget | None:
    """Parse a budget. ``None`` means no budget. Raise ``ValueError`` when undecidable."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("budget_policy")
    if "usd" not in value or any(field not in value for field in _BUDGET_INT_FIELDS):
        raise ValueError("budget_policy")
    usd_text = value["usd"]
    if not isinstance(usd_text, str):
        raise ValueError("usd")
    try:
        usd = Decimal(usd_text)
    except InvalidOperation:
        raise ValueError("usd") from None
    if not usd.is_finite():
        raise ValueError("usd")
    ints: dict[str, int] = {}
    for field in _BUDGET_INT_FIELDS:
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(field)
        ints[field] = number
    return _Budget(
        usd=usd,
        tokens=ints["tokens"],
        wall_clock_seconds=ints["wall_clock_seconds"],
        max_subagents=ints["max_subagents"],
    )


def _parse_draft(value: Any) -> _Draft | None:
    if not isinstance(value, Mapping):
        return None
    if any(key not in value for key in _DRAFT_KEYS):
        return None
    project_id = value["project_id"]
    objective = value["objective"]
    if not isinstance(project_id, str) or not isinstance(objective, str):
        return None
    criteria = _string_set(value["acceptance_criteria"], collapse=True)
    repositories = _string_set(value["repositories"], collapse=False)
    capabilities = _string_set(value["requested_capabilities"], collapse=False)
    approval_classes = _string_set(value["approval_classes"], collapse=False)
    if (
        criteria is None
        or repositories is None
        or capabilities is None
        or approval_classes is None
    ):
        return None
    try:
        budget = _parse_budget(value["budget_policy"])
    except ValueError:
        return None
    return _Draft(
        project_id=project_id,
        objective=objective,
        criteria=criteria,
        repositories=repositories,
        capabilities=capabilities,
        approval_classes=approval_classes,
        budget=budget,
    )


def _budget_higher_or_removed(approved: _Budget | None, revised: _Budget | None) -> bool:
    if approved is not None and revised is None:
        return True
    if approved is None or revised is None:
        return False
    return (
        revised.usd > approved.usd
        or revised.tokens > approved.tokens
        or revised.wall_clock_seconds > approved.wall_clock_seconds
        or revised.max_subagents > approved.max_subagents
    )


def _compare(approved: _Draft, revised: _Draft) -> tuple[str, ...]:
    codes: list[str] = []
    if revised.criteria - approved.criteria:
        codes.append("a")
    if approved.criteria - revised.criteria:
        codes.append("b")
    if revised.project_id != approved.project_id or revised.repositories - approved.repositories:
        codes.append("c")
    if (
        revised.capabilities - approved.capabilities
        or revised.approval_classes - approved.approval_classes
    ):
        codes.append("d")
    if _budget_higher_or_removed(approved.budget, revised.budget):
        codes.append("e")
    if _norm_objective(revised.objective) != _norm_objective(approved.objective):
        codes.append("f")
    return tuple(sorted(codes))
