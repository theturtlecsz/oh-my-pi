"""R12 durable learning: stage, revise, retrieve, and count lessons.

A narrative lesson and a lesson without a receipt are rejected and write
nothing; a narrative lesson can never authorize. A scientific lesson with a
receipt stages a skill, recipe, regression test, and manifest plus an FK-6
lesson dict. Narrow keeps a lesson retrievable, withdraw hides it, mandatory
strings always return, and invocation and outcome counting touch only counters.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from omp_work.research.lessons import (
    LessonDraft,
    invoke,
    outcome,
    retrieve,
    revise,
    stage,
)

LESSONS_SOURCE = Path(__file__).parents[1] / "src/omp_work/research/lessons.py"


_CLAIM = "Warming first avoids a cold read"
_BINDING = {
    "source_span": f"1:{1 + len(_CLAIM)}",
    "revision": "rev-1",
    "work_item": "item-1",
    "candidate": "cand-1",
    "evaluator": "eval-1",
}
_BEHAVIOUR = {"metric": "cold_reads", "op": "le", "value": 0}


def _receipt() -> dict[str, object]:
    return {
        "binding": dict(_BINDING),
        "outcome": "PASS",
        "claim": _CLAIM,
        "metrics": {"cold_reads": 0},
    }


def _resolve(receipt_id: str) -> dict[str, object] | None:
    return {"receipt-1": _receipt()}.get(receipt_id)


def _scientific(**overrides: object) -> LessonDraft:
    fields: dict[str, object] = {
        "lesson_id": "lesson-1",
        "project_id": "proj-a",
        "kind": "scientific",
        "title": "Cache warms before the read",
        "steps": ["Warm the cache", "Read the value"],
        "claim": _CLAIM,
        "receipt_ids": ["receipt-1"],
        "binding": dict(_BINDING),
        "outcome": "PASS",
        "behaviour": dict(_BEHAVIOUR),
    }
    fields.update(overrides)
    return LessonDraft(**fields)  # type: ignore[arg-type]


def test_narrative_and_receiptless_drafts_are_rejected_without_files(
    tmp_path: Path,
) -> None:
    narrative = LessonDraft(
        lesson_id="narrative-1",
        project_id="proj-a",
        kind="narrative",
        title="A story",
        receipt_ids=["receipt-1"],
    )
    receiptless = _scientific(lesson_id="no-receipt", receipt_ids=[])

    for draft in (narrative, receiptless):
        result = stage(draft, tmp_path)
        assert result["status"] == "rejected"
        assert result["fk6_lesson"] is None
        assert not (tmp_path / draft.lesson_id).exists()

    assert list(tmp_path.iterdir()) == []


def test_authorize_on_scientific_draft_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="narrative_cannot_authorize"):
        stage(_scientific(), tmp_path, authorize=True)
    assert list(tmp_path.iterdir()) == []


def test_scientific_draft_stages_skill_recipe_regression_and_manifest(
    tmp_path: Path,
) -> None:
    draft = _scientific()
    result = stage(draft, tmp_path, resolve=_resolve)

    assert result["status"] == "staged"
    assert result["revision"] == 1
    assert result["invocations"] == 0
    assert result["outcomes"] == {}

    skill_dir = tmp_path / draft.lesson_id
    for name in ("skill.md", "recipe.md", "regression_test.py", "manifest.json"):
        assert (skill_dir / name).is_file()
    assert draft.title in (skill_dir / "skill.md").read_text(encoding="utf-8")
    assert draft.steps[1] in (skill_dir / "recipe.md").read_text(encoding="utf-8")
    manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "staged"
    assert manifest["lesson_id"] == draft.lesson_id

    fk6 = result["fk6_lesson"]
    assert fk6["title"] == draft.title
    assert fk6["steps"] == draft.steps
    assert fk6["preconditions"] == []
    assert fk6["claims"] == [{"text": draft.claim, "receipt_ids": ["receipt-1"]}]


def test_narrow_records_receipt_bumps_revision_and_keeps_retrievable(
    tmp_path: Path,
) -> None:
    staged = stage(_scientific(), tmp_path, resolve=_resolve)
    narrowed = revise(
        staged,
        action="narrow",
        contradicting_receipt="receipt-2",
        precondition={"key": "project_id", "op": "eq", "value": "proj-a"},
    )

    assert narrowed["status"] == "narrowed"
    assert narrowed["revision"] == 2
    assert narrowed["supersedes"] == 1
    assert narrowed["contradicting_receipt"] == "receipt-2"
    assert narrowed["fk6_lesson"]["preconditions"] == [
        {"key": "project_id", "op": "eq", "value": "proj-a"}
    ]

    found = retrieve([narrowed], project_id="proj-a", limit=5, mandatory=[])
    assert found == [narrowed]


def test_withdraw_is_absent_from_retrieval(tmp_path: Path) -> None:
    staged = stage(_scientific(), tmp_path, resolve=_resolve)
    withdrawn = revise(
        staged, action="withdraw", contradicting_receipt="receipt-3"
    )
    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["contradicting_receipt"] == "receipt-3"
    assert retrieve([withdrawn], project_id="proj-a", limit=5, mandatory=[]) == []


def test_limit_zero_omits_lessons_but_returns_every_mandatory_string(
    tmp_path: Path,
) -> None:
    first = stage(_scientific(lesson_id="lesson-1"), tmp_path, resolve=_resolve)
    second = stage(_scientific(lesson_id="lesson-2"), tmp_path, resolve=_resolve)
    mandatory = ["always:pin-a", "always:pin-b"]

    capped = retrieve(
        [first, second], project_id="proj-a", limit=1, mandatory=mandatory
    )
    assert capped == [first, *mandatory]

    none = retrieve(
        [first, second], project_id="proj-a", limit=0, mandatory=mandatory
    )
    assert none == mandatory


def test_other_project_returns_no_lessons(tmp_path: Path) -> None:
    staged = stage(_scientific(lesson_id="lesson-1"), tmp_path, resolve=_resolve)
    assert retrieve([staged], project_id="proj-b", limit=5, mandatory=[]) == []


def test_invoke_and_outcome_change_only_the_counters(tmp_path: Path) -> None:
    staged = stage(_scientific(), tmp_path, resolve=_resolve)
    before = json.loads(json.dumps(staged))

    invoke(staged)
    invoke(staged)
    outcome(staged, "success")
    outcome(staged, "success")
    outcome(staged, "failure")

    assert staged["invocations"] == 2
    assert staged["outcomes"] == {"success": 2, "failure": 1}
    for key in ("lesson_id", "project_id", "kind", "status", "revision", "fk6_lesson"):
        assert staged[key] == before[key]


def test_module_has_no_forbidden_imports() -> None:
    tree = ast.parse(LESSONS_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"omp_knowledge", "threading", "sqlite3"})
