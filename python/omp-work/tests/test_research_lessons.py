"""R12 durable learning: stage, revise, retrieve, and count lessons.

A narrative lesson and a lesson without a receipt are rejected and write
nothing; a narrative lesson can never authorize. A scientific lesson with a
receipt stages a skill, recipe, regression test, and manifest plus an FK-6
lesson dict. Narrow keeps a lesson retrievable, withdraw hides it, mandatory
strings always return, and invocation and outcome counting touch only counters.
A false repository precondition, a malformed binding or behaviour, an unrelated
PASS, a different claim, and an unresolved citation are refused with no files.
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


def _assert_rejected(result: dict[str, object], draft: LessonDraft, directory: Path) -> None:
    assert result["status"] == "rejected"
    assert result["revision"] == 0
    assert result["fk6_lesson"] is None
    assert not (directory / draft.lesson_id).exists()
    assert list(directory.iterdir()) == []


def test_false_repository_precondition_is_refused(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["repository"] = "actual"
    draft = _scientific(
        preconditions=[{"key": "repository", "op": "eq", "value": "other"}],
    )
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": receipt}.get(receipt_id),
    )
    _assert_rejected(result, draft, tmp_path)

    omitted = _scientific(
        lesson_id="no-repository",
        preconditions=[{"key": "repository", "op": "ne", "value": "other"}],
    )
    omitted_result = stage(omitted, tmp_path, resolve=_resolve)
    _assert_rejected(omitted_result, omitted, tmp_path)


def test_matching_repository_and_project_preconditions_are_admitted(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    receipt["repository"] = "actual"
    receipt["note"] = "extra"
    draft = _scientific(
        preconditions=[
            {"key": "repository", "op": "eq", "value": "actual"},
            {"key": "repository", "op": "ne", "value": "other"},
            {"key": "project_id", "op": "eq", "value": "proj-a"},
        ],
    )
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": receipt}.get(receipt_id),
    )
    assert result["status"] == "staged"
    assert result["revision"] == 1
    assert result["fk6_lesson"]["preconditions"] == [
        {"key": "repository", "op": "eq", "value": "actual"},
        {"key": "repository", "op": "ne", "value": "other"},
        {"key": "project_id", "op": "eq", "value": "proj-a"},
    ]
    assert (tmp_path / draft.lesson_id / "skill.md").is_file()


def test_repository_precondition_must_hold_on_every_receipt(tmp_path: Path) -> None:
    good = _receipt()
    good["repository"] = "actual"
    bad = _receipt()
    bad["repository"] = "other"
    draft = _scientific(
        receipt_ids=["receipt-1", "receipt-2"],
        preconditions=[{"key": "repository", "op": "eq", "value": "actual"}],
    )
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": good, "receipt-2": bad}.get(receipt_id),
    )
    _assert_rejected(result, draft, tmp_path)


def test_malformed_binding_and_behaviour_are_rejected(tmp_path: Path) -> None:
    drafts = (
        _scientific(binding=None),
        _scientific(behaviour=None),
        _scientific(behaviour={"metric": "cold_reads", "op": [], "value": 0}),
    )
    for draft in drafts:
        result = stage(draft, tmp_path, resolve=_resolve)
        _assert_rejected(result, draft, tmp_path)


def test_bad_precondition_key_or_op_raises_and_writes_nothing(tmp_path: Path) -> None:
    cases = (
        [{"key": "branch", "op": "eq", "value": "main"}],
        [{"key": "project_id", "op": "gt", "value": "proj-a"}],
        [{"key": [], "op": "eq", "value": "proj-a"}],
        [{"key": "repository", "op": [], "value": "actual"}],
    )
    for preconditions in cases:
        with pytest.raises(ValueError, match="unknown_precondition"):
            stage(_scientific(preconditions=preconditions), tmp_path, resolve=_resolve)
        assert list(tmp_path.iterdir()) == []


def test_unrelated_pass_is_refused(tmp_path: Path) -> None:
    unbound = _receipt()
    unbound["outcome"] = "PASS"
    unbound["binding"] = {**_BINDING, "evaluator": "someone-else"}
    draft = _scientific()
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": unbound}.get(receipt_id),
    )
    _assert_rejected(result, draft, tmp_path)

    blocked = _scientific(lesson_id="blocked-outcome", outcome="BLOCKED")
    passed = _receipt()
    passed["outcome"] = "PASS"
    blocked_result = stage(
        blocked,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": passed}.get(receipt_id),
    )
    _assert_rejected(blocked_result, blocked, tmp_path)

    mixed = _scientific(lesson_id="mixed-pass", receipt_ids=["receipt-1", "receipt-2"])
    unrelated = _receipt()
    unrelated["binding"] = {**_BINDING, "work_item": "other-work"}
    mixed_result = stage(
        mixed,
        tmp_path,
        resolve=lambda receipt_id: {
            "receipt-1": _receipt(),
            "receipt-2": unrelated,
        }.get(receipt_id),
    )
    _assert_rejected(mixed_result, mixed, tmp_path)


def test_matching_non_pass_outcome_is_admitted(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["outcome"] = "BLOCKED"
    draft = _scientific(outcome="BLOCKED")
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": receipt}.get(receipt_id),
    )
    assert result["status"] == "staged"
    assert result["fk6_lesson"]["outcome"] == "BLOCKED"
    assert result["fk6_lesson"]["binding"] == _BINDING


def test_different_claim_is_refused(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["claim"] = "A different claim entirely"
    draft = _scientific()
    result = stage(
        draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": receipt}.get(receipt_id),
    )
    _assert_rejected(result, draft, tmp_path)

    binding = {**_BINDING, "source_span": "0:1"}
    spanned = _receipt()
    spanned["binding"] = dict(binding)
    span_draft = _scientific(lesson_id="bad-span", binding=binding)
    span_result = stage(
        span_draft,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": spanned}.get(receipt_id),
    )
    _assert_rejected(span_result, span_draft, tmp_path)

    mixed = _scientific(lesson_id="mixed-claim", receipt_ids=["receipt-1", "receipt-2"])
    other = _receipt()
    other["claim"] = "A different claim entirely"
    mixed_result = stage(
        mixed,
        tmp_path,
        resolve=lambda receipt_id: {"receipt-1": _receipt(), "receipt-2": other}.get(receipt_id),
    )
    _assert_rejected(mixed_result, mixed, tmp_path)


def test_unresolved_citation_is_refused(tmp_path: Path) -> None:
    draft = _scientific(receipt_ids=["receipt-1", "missing-receipt"])
    result = stage(draft, tmp_path, resolve=_resolve)
    _assert_rejected(result, draft, tmp_path)


def test_generated_regression_refuses_false_evidence(tmp_path: Path) -> None:
    draft = _scientific()
    staged = stage(draft, tmp_path, resolve=_resolve)
    assert staged["status"] == "staged"
    source_path = tmp_path / draft.lesson_id / "regression_test.py"
    namespace: dict[str, object] = {"__name__": "generated_regression"}
    exec(compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec"), namespace)
    names = (
        "test_preconditions_and_claimed_behaviour_hold",
        "test_false_claimed_behaviour_is_refused",
        "test_false_precondition_is_refused",
        "test_false_repository_precondition_is_refused",
        "test_unresolved_citation_is_refused",
        "test_unrelated_pass_is_refused",
        "test_different_claim_is_refused",
    )
    for name in names:
        test = namespace[name]
        assert callable(test)
        test(tmp_path / name)


def test_module_has_no_forbidden_imports() -> None:
    tree = ast.parse(LESSONS_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"omp_knowledge", "threading", "sqlite3"})
