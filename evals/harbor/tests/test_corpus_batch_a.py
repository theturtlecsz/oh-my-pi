"""Tests for Harbor corpus batch A: 14 tasks from real fixes in HEAD history."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from omp_harbor_eval.corpus import load_batch

REPO_ROOT = Path(__file__).resolve().parents[3]
BATCH_PATH = REPO_ROOT / "evals" / "harbor" / "corpus" / "batches" / "a.json"

_EXPECTED_CATEGORIES = {
    "localized_bug": 4,
    "multi_file_feature": 3,
    "difficult_debugging": 3,
    "migration": 2,
    "seeded_defect": 2,
}


def _tasks():
    return load_batch(BATCH_PATH)


def test_batch_a_has_fourteen_tasks_with_expected_category_counts() -> None:
    tasks = _tasks()
    assert len(tasks) == 14
    assert dict(Counter(t["category"] for t in tasks)) == _EXPECTED_CATEGORIES
    assert sum(_EXPECTED_CATEGORIES.values()) == 14


def test_batch_a_has_at_least_three_long_horizon_tasks() -> None:
    tasks = _tasks()
    long_horizon = [t["task_id"] for t in tasks if t["long_horizon"]]
    assert len(long_horizon) >= 3
    for task in tasks:
        assert task["long_horizon"] == (len(task["source_paths"]) >= 4)


def test_batch_a_source_paths_exclude_test_files() -> None:
    for task in _tasks():
        spec_path = BATCH_PATH.parent.parent / "specs" / f"{task['task_id']}.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        for source_paths in (task["source_paths"], spec["source_paths"]):
            for source_path in source_paths:
                basename = Path(source_path).name
                assert ".test." not in basename, (task["task_id"], source_path)
                assert not basename.startswith("test_"), (task["task_id"], source_path)
                assert not basename.endswith("_test.py"), (task["task_id"], source_path)


def test_batch_a_seed_deletes_target_and_solution_does_not_add_it() -> None:
    for task in _tasks():
        fixture_dir = Path(task["fixture"])
        if not fixture_dir.is_absolute():
            fixture_dir = (BATCH_PATH.parent / task["fixture"]).resolve()

        fixture = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))
        test = fixture["independent_tests"][0]
        target = test["target"].split("::")[0]
        basename = Path(target).name

        assert test["files"] == [f"independent/{basename}"], task["task_id"]

        seed = (fixture_dir / "seed.patch").read_text(encoding="utf-8")
        assert f"--- a/{target}" in seed, task["task_id"]
        assert "+++ /dev/null" in seed, task["task_id"]

        solution = (fixture_dir / "solution.patch").read_text(encoding="utf-8")
        assert f"diff --git a/{target} b/{target}" not in solution, task["task_id"]
        assert f"+++ b/{target}" not in solution, task["task_id"]
