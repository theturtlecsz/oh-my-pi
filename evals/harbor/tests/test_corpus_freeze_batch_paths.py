"""Regression tests for nullable source commits and batch-relative freeze paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omp_harbor_eval.corpus import CATEGORIES, REPO_ROOT, freeze, load_batch, verify

_PATCH_4_FILES = """diff --git a/a.ts b/a.ts
--- a/a.ts
+++ b/a.ts
@@ -1 +1 @@
-1
+2
diff --git a/b.ts b/b.ts
--- a/b.ts
+++ b/b.ts
@@ -1 +1 @@
-1
+2
diff --git a/c.ts b/c.ts
--- a/c.ts
+++ b/c.ts
@@ -1 +1 @@
-1
+2
diff --git a/d.ts b/d.ts
--- a/d.ts
+++ b/d.ts
@@ -1 +1 @@
-1
+2
"""

_PATCH_3_FILES = """diff --git a/a.ts b/a.ts
--- a/a.ts
+++ b/a.ts
@@ -1 +1 @@
-1
+2
diff --git a/b.ts b/b.ts
--- a/b.ts
+++ b/b.ts
@@ -1 +1 @@
-1
+2
diff --git a/c.ts b/c.ts
--- a/c.ts
+++ b/c.ts
@@ -1 +1 @@
-1
+2
"""


def _create_fixture_dir(
    root: Path,
    task_id: str,
    *,
    subpath: str = "evals/harbor/corpus/tasks",
    seed_patch: str = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n-old\n+seed\n",
    solution_patch: str = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n-old\n+solution\n",
    rules: list | None = None,
    tests: list | None = None,
    test_files: list | None = None,
    invalid_fixture_json: bool = False,
) -> Path:
    dir_path = root / subpath / task_id
    dir_path.mkdir(parents=True, exist_ok=True)
    if invalid_fixture_json:
        (dir_path / "fixture.json").write_text("{bad json", encoding="utf-8")
        return dir_path

    if tests is None:
        files = test_files if test_files is not None else ["independent/target.test.ts"]
        tests = [{"runner": "bun", "target": "session-system/tests/target.test.ts", "files": files}]

    fixture = {
        "id": task_id,
        "scored_experiment": "repair",
        "seed_patch": "seed.patch",
        "solution_patch": "solution.patch",
        "independent_tests": tests,
        "scenario": "scenario.json",
        "rules": rules if rules is not None else [{"type": "independent_tests_pass"}],
    }
    scenario = {
        "command": f"/execute {task_id}",
        "terminal": {"agent_end": True},
        "model_script": [],
        "ui_script": [],
    }
    (dir_path / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (dir_path / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    (dir_path / "seed.patch").write_text(seed_patch, encoding="utf-8")
    (dir_path / "solution.patch").write_text(solution_patch, encoding="utf-8")
    for t in tests:
        for f in t.get("files", []):
            fp = dir_path / f
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("// test content\n", encoding="utf-8")
    return dir_path


def _generate_32_tasks(tmp_path: Path) -> list[dict]:
    tasks = []
    # 4 tasks for each of the 8 categories = 32 tasks
    task_idx = 0
    for cat in CATEGORIES:
        for _ in range(4):
            task_idx += 1
            tid = f"task_{task_idx:02d}"
            # Make at least 4 tasks long horizon
            sol = _PATCH_4_FILES if task_idx <= 4 else _PATCH_3_FILES
            fdir = _create_fixture_dir(tmp_path, tid, solution_patch=sol)
            tasks.append({
                "task_id": tid,
                "category": cat,
                "fixture": f"../tasks/{tid}",
                "source_commit": f"commit_{task_idx}",
                "rationale": f"rationale for {tid}",
            })
    return tasks


@pytest.mark.parametrize("source_commit", [None, "", "  ", 42])
def test_load_batch_nullable_source_commit(tmp_path: Path, source_commit: str | int | None) -> None:
    fixture = _create_fixture_dir(tmp_path, "nullable")
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"tasks": [{
        "task_id": "nullable",
        "category": "localized_bug",
        "fixture": str(fixture),
        "source_commit": source_commit,
        "rationale": "Nullable source commit regression",
    }]}), encoding="utf-8")

    if source_commit is None:
        assert load_batch(batch)[0]["source_commit"] is None
    else:
        with pytest.raises(ValueError, match="source_commit"):
            load_batch(batch)


def test_freeze_batch_relative_paths_verify_from_different_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _generate_32_tasks(tmp_path)
    batch = tmp_path / "evals/harbor/corpus/batches/x.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    manifest = freeze([batch], "c")

    assert verify(manifest) == []
    for task in tasks:
        expected = tmp_path / "evals/harbor/corpus/tasks" / task["task_id"]
        assert manifest["tasks"][task["task_id"]]["fixture"] == expected.as_posix()


def test_batch_b_nullable_source_commits_load() -> None:
    tasks = load_batch(REPO_ROOT / "evals/harbor/corpus/batches/b.json")
    nullable = {task["task_id"]: task["source_commit"] for task in tasks if task["task_id"] in {"f1", "f2"}}
    assert nullable == {"f1": None, "f2": None}
