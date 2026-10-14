"""Receipt admission refuses unbound or false evidence without writing lessons."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from typing import Callable

import pytest
import test_research_lessons as existing
from omp_work.research.lessons import LessonDraft, stage


def _scientific() -> LessonDraft:
    claim = "Latency meets the measured threshold"
    return LessonDraft(
        lesson_id="latency-lesson",
        project_id="proj-a",
        kind="scientific",
        title="Measured latency",
        steps=["Measure latency", "Compare with the threshold"],
        claim=claim,
        receipt_ids=["good-id"],
        preconditions=[{"key": "project_id", "op": "eq", "value": "proj-a"}],
        binding={
            "source_span": f"1:{1 + len(claim)}",
            "revision": "rev-1",
            "work_item": "item-1",
            "candidate": "candidate-1",
            "evaluator": "evaluator-1",
        },
        outcome="PASS",
        behaviour={"metric": "latency", "op": "ge", "value": 0.7},
    )


def _receipt(draft: LessonDraft) -> dict[str, object]:
    return {
        "claim": draft.claim,
        "binding": dict(draft.binding),
        "outcome": draft.outcome,
        "project_id": draft.project_id,
        "metrics": {"latency": 0.7},
    }


def _assert_rejected(record: dict[str, object], draft: LessonDraft, directory: Path) -> None:
    assert record["status"] == "rejected"
    assert record["revision"] == 0
    assert record["fk6_lesson"] is None
    assert not (directory / draft.lesson_id).exists()
    assert not directory.exists()


@pytest.mark.parametrize("explicit_none", [False, True], ids=["omitted", "explicit-none"])
def test_resolver_required(tmp_path: Path, explicit_none: bool) -> None:
    draft = _scientific()
    directory = tmp_path / "lessons"
    if explicit_none:
        record = stage(draft, directory, resolve=None)
    else:
        record = stage(draft, directory)
    _assert_rejected(record, draft, directory)


@pytest.mark.parametrize("error", [LookupError, KeyError])
def test_lookup_failure_refuses_lesson(tmp_path: Path, error: type[LookupError]) -> None:
    def resolve(receipt_id: str) -> object:
        raise error(receipt_id)

    draft = _scientific()
    directory = tmp_path / "lessons"
    _assert_rejected(stage(draft, directory, resolve=resolve), draft, directory)


@pytest.mark.parametrize(
    "raw", [None, "{invalid-json", "[]", []],
    ids=["unresolved", "invalid-json", "json-list", "python-list"],
)
def test_non_receipt_resolution_is_refused(tmp_path: Path, raw: object) -> None:
    draft = _scientific()
    directory = tmp_path / "lessons"
    _assert_rejected(stage(draft, directory, resolve=lambda _: raw), draft, directory)


def test_mixed_citations_resolve_each_id_once_and_refuse_missing(tmp_path: Path) -> None:
    draft = replace(_scientific(), receipt_ids=["good-id", "missing-id"])
    receipts = {"good-id": _receipt(draft)}
    calls: list[str] = []

    def resolve(receipt_id: str) -> object:
        calls.append(receipt_id)
        return receipts.get(receipt_id)

    directory = tmp_path / "lessons"
    _assert_rejected(stage(draft, directory, resolve=resolve), draft, directory)
    assert calls.count("good-id") == 1
    assert calls.count("missing-id") == 1
    assert len(calls) == 2


@pytest.mark.parametrize("mismatch", ["work_item", "claim"])
def test_unrelated_pass_or_different_claim_is_refused(tmp_path: Path, mismatch: str) -> None:
    draft = _scientific()
    receipt = _receipt(draft)
    if mismatch == "work_item":
        receipt["binding"] = {**draft.binding, "work_item": "other-item"}
    else:
        receipt["claim"] = "A different claim"
    directory = tmp_path / "lessons"
    _assert_rejected(stage(draft, directory, resolve=lambda _: receipt), draft, directory)


@pytest.mark.parametrize("case", ["narrative", "receiptless"])
def test_good_resolver_cannot_admit_ineligible_draft(tmp_path: Path, case: str) -> None:
    scientific = _scientific()
    draft = (
        replace(scientific, kind="narrative")
        if case == "narrative"
        else replace(scientific, receipt_ids=[])
    )
    directory = tmp_path / "lessons"
    receipts = {"good-id": _receipt(scientific)}
    _assert_rejected(stage(draft, directory, resolve=receipts.get), draft, directory)


def test_authorization_raises_even_with_bound_receipt(tmp_path: Path) -> None:
    draft = _scientific()
    directory = tmp_path / "lessons"
    with pytest.raises(ValueError, match="^narrative_cannot_authorize$"):
        stage(draft, directory, authorize=True, resolve={"good-id": _receipt(draft)}.get)
    assert not (directory / draft.lesson_id).exists()
    assert not directory.exists()


@pytest.mark.parametrize("op", ["eq", "le", "ne", "lt", "gt", "ge"])
def test_false_claimed_behaviour_is_refused(tmp_path: Path, op: str) -> None:
    draft = _scientific()
    draft.behaviour["op"] = op
    value = draft.behaviour["value"]
    wrong = {"eq": value + 1, "le": value + 1, "ge": value - 1}.get(op, value)
    receipt = _receipt(draft)
    receipt["metrics"] = {"latency": wrong}
    directory = tmp_path / "lessons"
    _assert_rejected(stage(draft, directory, resolve=lambda _: receipt), draft, directory)


def test_false_project_precondition_is_refused(tmp_path: Path) -> None:
    draft = _scientific()
    draft.preconditions = [{"key": "project_id", "op": "ne", "value": draft.project_id}]
    directory = tmp_path / "lessons"
    _assert_rejected(
        stage(draft, directory, resolve={"good-id": _receipt(draft)}.get), draft, directory,
    )


@pytest.mark.parametrize("case", ["missing-key", "empty-value", "extra-key"])
def test_malformed_binding_is_refused_even_when_receipt_matches(tmp_path: Path, case: str) -> None:
    draft = _scientific()
    if case == "missing-key":
        del draft.binding["candidate"]
    elif case == "empty-value":
        draft.binding["candidate"] = ""
    else:
        draft.binding["extra"] = "extra-value"
    directory = tmp_path / "lessons"
    _assert_rejected(
        stage(draft, directory, resolve={"good-id": _receipt(draft)}.get), draft, directory,
    )


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), True, "0.7"],
    ids=["nan", "positive-infinity", "negative-infinity", "bool", "string"],
)
def test_invalid_behaviour_value_is_refused(tmp_path: Path, value: object) -> None:
    draft = _scientific()
    draft.behaviour["value"] = value
    directory = tmp_path / "lessons"
    _assert_rejected(
        stage(draft, directory, resolve={"good-id": _receipt(draft)}.get), draft, directory,
    )


@pytest.mark.parametrize("encoding", ["mapping", "json", "extra-keys"])
def test_bound_receipt_stages_files_and_fk6(tmp_path: Path, encoding: str) -> None:
    draft = _scientific()
    receipt = _receipt(draft)
    if encoding == "extra-keys":
        receipt["note"] = "Additional receipt metadata"
    raw = json.dumps(receipt) if encoding == "json" else receipt
    directory = tmp_path / "lessons"
    record = stage(draft, directory, resolve={"good-id": raw}.get)
    assert record["status"] == "staged"
    assert record["revision"] == 1
    assert record["invocations"] == 0
    assert record["outcomes"] == {}
    fk6 = record["fk6_lesson"]
    assert fk6["binding"] == draft.binding
    assert fk6["outcome"] == draft.outcome
    assert fk6["behaviour"] == draft.behaviour
    assert fk6["claims"] == [{"text": draft.claim, "receipt_ids": draft.receipt_ids}]
    for name in ("skill.md", "recipe.md", "regression_test.py", "manifest.json"):
        assert (directory / draft.lesson_id / name).is_file()


def test_generated_regression_checks_admission_and_refusals(tmp_path: Path) -> None:
    draft = _scientific()
    staged = stage(draft, tmp_path / "source", resolve={"good-id": _receipt(draft)}.get)
    assert staged["status"] == "staged"
    source = tmp_path / "source" / draft.lesson_id / "regression_test.py"
    spec = importlib.util.spec_from_file_location("receipt_lesson_regression", source)
    assert spec is not None and spec.loader is not None
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    names = (
        "test_preconditions_and_claimed_behaviour_hold",
        "test_false_claimed_behaviour_is_refused",
        "test_false_precondition_is_refused",
        "test_unresolved_citation_is_refused",
    )
    for name in names:
        directory = tmp_path / name
        directory.mkdir()
        assert getattr(generated, name)(directory) is None
        if name == "test_preconditions_and_claimed_behaviour_hold":
            assert (directory / draft.lesson_id / "manifest.json").is_file()


@pytest.mark.parametrize(
    "test", [
        existing.test_scientific_draft_stages_skill_recipe_regression_and_manifest,
        existing.test_narrow_records_receipt_bumps_revision_and_keeps_retrievable,
        existing.test_limit_zero_omits_lessons_but_returns_every_mandatory_string,
    ], ids=["stage", "narrow", "mandatory"],
)
def test_frozen_receipt1_conflict_reported(tmp_path: Path, test: Callable[[Path], None]) -> None:
    """Previously unbound receipt-1 scenarios remain valid after binding evidence."""
    assert test(tmp_path) is None
