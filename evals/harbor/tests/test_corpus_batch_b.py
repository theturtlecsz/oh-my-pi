"""Tests for the frozen Harbor corpus batch B: shape, category counts and withheld details."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from omp_harbor_eval.corpus import extract_patch_paths, load_batch

HARBOR_ROOT = Path(__file__).resolve().parents[1]
CORPUS = HARBOR_ROOT / "corpus"
BATCH = CORPUS / "batches" / "b.json"

EXPECTED_COUNTS = {
    "ambiguous_intake": 4,
    "security_concurrency": 4,
    "resume": 3,
    "migration": 2,
    "seeded_defect": 2,
    "localized_bug": 1,
}
NEW_IDS = [f"b-{n:02d}" for n in range(1, 15)]


def _entries() -> list[dict]:
    return list(load_batch(BATCH))


def _fixture_json(task_id: str) -> dict:
    return json.loads((CORPUS / "tasks" / task_id / "fixture.json").read_text(encoding="utf-8"))


def test_batch_has_sixteen_entries_with_the_exact_category_counts() -> None:
    entries = _entries()
    assert [e["task_id"] for e in entries] == [*NEW_IDS, "f1", "f2"]
    assert dict(Counter(e["category"] for e in entries)) == EXPECTED_COUNTS


def test_f1_and_f2_enter_the_batch_from_the_shared_fixtures() -> None:
    by_id = {e["task_id"]: e for e in _entries()}
    assert by_id["f1"]["fixture"] == "fixtures/f1"
    assert by_id["f2"]["fixture"] == "fixtures/f2"
    assert by_id["f1"]["category"] == "localized_bug"
    assert by_id["f2"]["category"] == "resume"


def test_at_least_three_new_tasks_are_long_horizon() -> None:
    long_horizon = [e["task_id"] for e in _entries() if e["task_id"] in NEW_IDS and e["long_horizon"]]
    assert len(long_horizon) >= 3, long_horizon
    for entry in _entries():
        assert entry["long_horizon"] == (len(entry["source_paths"]) >= 4)


@pytest.mark.parametrize("task_id", NEW_IDS)
def test_seed_deletes_the_target_and_solution_leaves_it(task_id: str) -> None:
    task_dir = CORPUS / "tasks" / task_id
    document = _fixture_json(task_id)
    (test,) = document["independent_tests"]
    target_file = test["target"].split("::")[0]

    seed = (task_dir / document["seed_patch"]).read_text(encoding="utf-8")
    solution = (task_dir / document["solution_patch"]).read_text(encoding="utf-8")
    assert target_file in extract_patch_paths(seed)
    assert f"diff --git a/{target_file} b/{target_file}\ndeleted file mode" in seed
    assert target_file not in extract_patch_paths(solution)
    assert test["files"] == [f"independent/{Path(target_file).name}"]
    assert (task_dir / test["files"][0]).is_file()


@pytest.mark.parametrize("task_id", [n for n in NEW_IDS if json.loads((CORPUS / "specs" / f"{n}.json").read_text())["category"] == "ambiguous_intake"])
def test_ambiguous_intake_never_shows_the_withheld_detail(task_id: str) -> None:
    spec = json.loads((CORPUS / "specs" / f"{task_id}.json").read_text(encoding="utf-8"))
    withheld = spec["withheld_detail"]
    details = [withheld] if isinstance(withheld, str) else list(withheld)
    assert details and all(d.strip() for d in details)

    task_dir = CORPUS / "tasks" / task_id
    seed = json.loads((task_dir / "ledger-seed.json").read_text(encoding="utf-8"))
    visible = [(task_dir / "instruction.md").read_text(encoding="utf-8")]
    for item in seed["items"]:
        visible += [item["title"], item["description"], *item["acceptance_criteria"]]
    haystack = "\n".join(visible).lower()
    for detail in details:
        assert detail.strip().lower() not in haystack


@pytest.mark.parametrize("task_id", NEW_IDS)
def test_spec_source_paths_match_the_solution_patch(task_id: str) -> None:
    spec = json.loads((CORPUS / "specs" / f"{task_id}.json").read_text(encoding="utf-8"))
    document = _fixture_json(task_id)
    solution = (CORPUS / "tasks" / task_id / document["solution_patch"]).read_text(encoding="utf-8")
    assert sorted(spec["source_paths"]) == extract_patch_paths(solution)
