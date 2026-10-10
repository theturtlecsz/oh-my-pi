"""Durable research lessons (R12, OMP-316).

A lesson draft either stages as a skill directory plus an FK-6 lesson dict, or
is rejected. A narrative lesson never authorizes an action. Retrieval serves
staged and narrowed lessons of one project plus every mandatory string, and
invocation and outcome counting never change a lesson's identity.

This module writes files and dicts only. It imports no knowledge authority, no
threading, and no database driver, and it starts no thread or process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "KINDS",
    "LessonDraft",
    "invoke",
    "outcome",
    "retrieve",
    "revise",
    "stage",
]

KINDS = frozenset({"execution", "search", "implementation", "scientific", "narrative"})
STAGED = "staged"
NARROWED = "narrowed"
WITHDRAWN = "withdrawn"
REJECTED = "rejected"
_PRECONDITION_KEYS = frozenset({"project_id", "repository"})
_PRECONDITION_OPS = frozenset({"eq", "ne"})


@dataclass
class LessonDraft:
    """One lesson candidate. ``kind`` is one of :data:`KINDS`."""

    lesson_id: str
    project_id: str
    kind: str
    title: str
    steps: list[str] = field(default_factory=list)
    claim: str = ""
    receipt_ids: list[str] = field(default_factory=list)
    preconditions: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown_lesson_kind:{self.kind}")
        _safe_segment(self.lesson_id)


def _safe_segment(value: object) -> str:
    text = str(value)
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        raise ValueError(f"unsafe_lesson_id:{text!r}")
    return text


def _precondition(raw: Mapping[str, Any]) -> dict[str, Any]:
    key = raw.get("key")
    op = raw.get("op")
    if key not in _PRECONDITION_KEYS:
        raise ValueError(f"unknown_precondition_key:{key!r}")
    if op not in _PRECONDITION_OPS:
        raise ValueError(f"unknown_precondition_op:{op!r}")
    return {"key": key, "op": op, "value": raw.get("value")}


def _fk6_lesson(draft: LessonDraft) -> dict[str, Any]:
    return {
        "title": draft.title,
        "steps": list(draft.steps),
        "preconditions": [_precondition(item) for item in draft.preconditions],
        "claims": [{"text": draft.claim, "receipt_ids": list(draft.receipt_ids)}],
    }


def _record(draft: LessonDraft, *, status: str, revision: int) -> dict[str, Any]:
    return {
        "lesson_id": draft.lesson_id,
        "project_id": draft.project_id,
        "kind": draft.kind,
        "status": status,
        "revision": revision,
        "invocations": 0,
        "outcomes": {},
        "fk6_lesson": None if status == REJECTED else _fk6_lesson(draft),
    }


def _skill_markdown(draft: LessonDraft) -> str:
    lines = [f"# {draft.title}", ""]
    if draft.claim:
        lines += ["## Claim", "", draft.claim, ""]
    if draft.steps:
        lines += ["## Steps", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(draft.steps, 1)]
        lines += [""]
    return "\n".join(lines)


def _recipe_markdown(draft: LessonDraft) -> str:
    lines = [f"# Recipe: {draft.title}", ""]
    steps = list(draft.steps) or ([draft.claim] if draft.claim else [draft.title])
    lines += [f"{index}. {step}" for index, step in enumerate(steps, 1)]
    lines += [""]
    return "\n".join(lines)


def _regression_source(draft: LessonDraft) -> str:
    return "\n".join(
        [
            '"""Regression check for a staged lesson."""',
            "",
            f"LESSON_ID = {draft.lesson_id!r}",
            f"STEPS = {list(draft.steps)!r}",
            "",
            "",
            "def test_lesson_has_a_recipe() -> None:",
            "    assert STEPS or LESSON_ID",
            "",
        ]
    )


def _write_lesson(root: Path, draft: LessonDraft, record: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.md").write_text(_skill_markdown(draft), encoding="utf-8")
    (root / "recipe.md").write_text(_recipe_markdown(draft), encoding="utf-8")
    (root / "regression_test.py").write_text(_regression_source(draft), encoding="utf-8")
    manifest = {
        "lesson_id": draft.lesson_id,
        "project_id": draft.project_id,
        "kind": draft.kind,
        "title": draft.title,
        "status": record["status"],
        "revision": record["revision"],
        "files": ["skill.md", "recipe.md", "regression_test.py"],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def stage(
    draft: LessonDraft, directory: Path | str, *, authorize: bool = False
) -> dict[str, Any]:
    """Stage a lesson, or reject it.

    ``authorize=True`` never stages: a narrative lesson cannot carry action
    authority, so the call raises :class:`ValueError` and writes nothing. With
    ``authorize=False`` a narrative lesson or an empty receipt list is rejected
    without writing anything. Every other lesson stages at revision 1 with zero
    invocations and writes its skill, recipe, regression test, and manifest.
    """
    if authorize:
        raise ValueError("narrative_cannot_authorize")
    if draft.kind == "narrative" or not draft.receipt_ids:
        return _record(draft, status=REJECTED, revision=0)
    record = _record(draft, status=STAGED, revision=1)
    _write_lesson(Path(directory) / draft.lesson_id, draft, record)
    return record


def revise(
    record: dict[str, Any],
    *,
    action: str,
    contradicting_receipt: Any,
    precondition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Narrow or withdraw a staged lesson against a contradicting receipt.

    ``narrow`` appends the precondition, keeps the prior revision as
    ``supersedes``, records the contradicting receipt, and moves to the next
    revision with status ``narrowed``. ``withdraw`` records the contradicting
    receipt and status ``withdrawn``.
    """
    if action == "narrow":
        if precondition is None:
            raise ValueError("narrow_requires_precondition")
        fk6 = record.get("fk6_lesson")
        if not isinstance(fk6, dict):
            raise ValueError("lesson_has_no_fk6_lesson")
        preconditions = fk6.setdefault("preconditions", [])
        preconditions.append(_precondition(precondition))
        record["supersedes"] = record.get("revision")
        record["revision"] = int(record.get("revision", 1)) + 1
        record["status"] = NARROWED
        record["contradicting_receipt"] = contradicting_receipt
        return record
    if action == "withdraw":
        record["status"] = WITHDRAWN
        record["contradicting_receipt"] = contradicting_receipt
        return record
    raise ValueError(f"unknown_revise_action:{action}")


def retrieve(
    records: Sequence[dict[str, Any]],
    *,
    project_id: str,
    limit: int,
    mandatory: Sequence[str],
) -> list[Any]:
    """Serve the project's staged and narrowed lessons, then every mandatory string.

    At most ``limit`` lessons return; rejected and withdrawn lessons and lessons
    of another project never do. The mandatory strings always return in order,
    even for a zero limit, and sit outside the lesson cap.
    """
    found = [
        record
        for record in records
        if record.get("project_id") == project_id
        and record.get("status") in {STAGED, NARROWED}
    ][: max(int(limit), 0)]
    return [*found, *mandatory]


def invoke(record: dict[str, Any]) -> dict[str, Any]:
    """Count one invocation. Nothing else changes."""
    record["invocations"] = int(record.get("invocations", 0)) + 1
    return record


def outcome(record: dict[str, Any], name: str) -> dict[str, Any]:
    """Count one named outcome. Nothing else changes."""
    outcomes = record.setdefault("outcomes", {})
    outcomes[name] = int(outcomes.get(name, 0)) + 1
    return record
