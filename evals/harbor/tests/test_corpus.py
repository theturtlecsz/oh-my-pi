"""Tests for Harbor corpus: manifest, load_batch, freeze, verify."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omp_harbor_eval.corpus import (
    CATEGORIES,
    extract_patch_paths,
    freeze,
    load_batch,
    main,
    task_sha256,
    verify,
)

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


def _write_batch(path: Path, tasks: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": tasks}, indent=2) + "\n", encoding="utf-8")
    return path


def test_extract_patch_paths_and_long_horizon() -> None:
    paths_4 = extract_patch_paths(_PATCH_4_FILES)
    assert paths_4 == ["a.ts", "b.ts", "c.ts", "d.ts"]
    assert len(paths_4) >= 4

    paths_3 = extract_patch_paths(_PATCH_3_FILES)
    assert paths_3 == ["a.ts", "b.ts", "c.ts"]
    assert len(paths_3) < 4


def test_load_batch_4_file_solution_long_horizon(tmp_path: Path) -> None:
    fdir = _create_fixture_dir(tmp_path, "task_long", solution_patch=_PATCH_4_FILES)
    batch_file = _write_batch(
        tmp_path / "batch.json",
        [
            {
                "task_id": "task_long",
                "category": "localized_bug",
                "fixture": str(fdir),
                "source_commit": "c1",
                "rationale": "r1",
            }
        ],
    )
    loaded = load_batch(batch_file)
    assert len(loaded) == 1
    assert loaded[0]["long_horizon"] is True
    assert loaded[0]["source_paths"] == ["a.ts", "b.ts", "c.ts", "d.ts"]


def test_load_batch_3_file_solution_not_long_horizon(tmp_path: Path) -> None:
    fdir = _create_fixture_dir(tmp_path, "task_short", solution_patch=_PATCH_3_FILES)
    batch_file = _write_batch(
        tmp_path / "batch.json",
        [
            {
                "task_id": "task_short",
                "category": "localized_bug",
                "fixture": str(fdir),
                "source_commit": "c1",
                "rationale": "r1",
            }
        ],
    )
    loaded = load_batch(batch_file)
    assert len(loaded) == 1
    assert loaded[0]["long_horizon"] is False


def test_load_batch_refusals(tmp_path: Path) -> None:
    # 1. missing required keys
    b1 = _write_batch(
        tmp_path / "b1.json",
        [{"task_id": "task_1", "category": "localized_bug"}],
    )
    with pytest.raises(ValueError, match="task task_1"):
        load_batch(b1)

    # 2. invalid category
    fdir = _create_fixture_dir(tmp_path, "task_cat")
    b2 = _write_batch(
        tmp_path / "b2.json",
        [{
            "task_id": "task_cat",
            "category": "non_existent_category",
            "fixture": str(fdir),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_cat"):
        load_batch(b2)

    # 3. fixture not under evals/harbor
    fdir_outside = _create_fixture_dir(tmp_path, "task_outside", subpath="other/dir")
    b3 = _write_batch(
        tmp_path / "b3.json",
        [{
            "task_id": "task_outside",
            "category": "localized_bug",
            "fixture": str(fdir_outside),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_outside"):
        load_batch(b3)

    # 4. fixture directory does not end in task_id
    fdir_mismatch = tmp_path / "evals" / "harbor" / "corpus" / "tasks" / "different_name"
    fdir_mismatch.mkdir(parents=True)
    b4 = _write_batch(
        tmp_path / "b4.json",
        [{
            "task_id": "task_mismatch",
            "category": "localized_bug",
            "fixture": str(fdir_mismatch),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_mismatch"):
        load_batch(b4)

    # 5. load_fixture fails
    fdir_bad = _create_fixture_dir(tmp_path, "task_bad", invalid_fixture_json=True)
    b5 = _write_batch(
        tmp_path / "b5.json",
        [{
            "task_id": "task_bad",
            "category": "localized_bug",
            "fixture": str(fdir_bad),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_bad"):
        load_batch(b5)

    # 6. seed patch is empty
    fdir_empty_seed = _create_fixture_dir(tmp_path, "task_empty_seed", seed_patch="")
    b6 = _write_batch(
        tmp_path / "b6.json",
        [{
            "task_id": "task_empty_seed",
            "category": "localized_bug",
            "fixture": str(fdir_empty_seed),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_empty_seed"):
        load_batch(b6)

    # 7. solution patch is empty
    fdir_empty_sol = _create_fixture_dir(tmp_path, "task_empty_sol", solution_patch="")
    b7 = _write_batch(
        tmp_path / "b7.json",
        [{
            "task_id": "task_empty_sol",
            "category": "localized_bug",
            "fixture": str(fdir_empty_sol),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_empty_sol"):
        load_batch(b7)

    # 8. seed and solution patches are identical
    same_patch = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n-1\n+2\n"
    fdir_same = _create_fixture_dir(tmp_path, "task_same", seed_patch=same_patch, solution_patch=same_patch)
    b8 = _write_batch(
        tmp_path / "b8.json",
        [{
            "task_id": "task_same",
            "category": "localized_bug",
            "fixture": str(fdir_same),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_same"):
        load_batch(b8)

    # 9. missing independent_tests_pass rule
    fdir_no_rule = _create_fixture_dir(
        tmp_path,
        "task_no_rule",
        rules=[{"type": "transcript_count", "count": 1}],
    )
    b9 = _write_batch(
        tmp_path / "b9.json",
        [{
            "task_id": "task_no_rule",
            "category": "localized_bug",
            "fixture": str(fdir_no_rule),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_no_rule"):
        load_batch(b9)

    # 10. under corpus/tasks/ test missing files entry with target basename
    fdir_bad_files = _create_fixture_dir(
        tmp_path,
        "task_bad_files",
        test_files=["other/unrelated.ts"],
    )
    b10 = _write_batch(
        tmp_path / "b10.json",
        [{
            "task_id": "task_bad_files",
            "category": "localized_bug",
            "fixture": str(fdir_bad_files),
            "source_commit": "c1",
            "rationale": "r1",
        }],
    )
    with pytest.raises(ValueError, match="task task_bad_files"):
        load_batch(b10)


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
                "fixture": str(fdir),
                "source_commit": f"commit_{task_idx}",
                "rationale": f"rationale for {tid}",
            })
    return tasks


def test_freeze_refusals(tmp_path: Path) -> None:
    tasks = _generate_32_tasks(tmp_path)

    # 1. Fewer than 30 tasks
    batch_short = _write_batch(tmp_path / "b_short.json", tasks[:29])
    with pytest.raises(ValueError, match="between 30 and 50"):
        freeze([batch_short], "base1")

    # 2. More than 50 tasks
    extra_tasks = list(tasks)
    for i in range(33, 53):
        tid = f"task_{i:02d}"
        fdir = _create_fixture_dir(tmp_path, tid, solution_patch=_PATCH_3_FILES)
        extra_tasks.append({
            "task_id": tid,
            "category": "localized_bug",
            "fixture": str(fdir),
            "source_commit": f"commit_{i}",
            "rationale": f"rationale for {tid}",
        })
    batch_long = _write_batch(tmp_path / "b_long.json", extra_tasks)
    with pytest.raises(ValueError, match="between 30 and 50"):
        freeze([batch_long], "base1")

    # 3. Duplicate task_id
    dup_tasks = list(tasks)
    dup_tasks[1] = dict(dup_tasks[0])
    batch_dup = _write_batch(tmp_path / "b_dup.json", dup_tasks)
    with pytest.raises(ValueError, match="duplicate task_id"):
        freeze([batch_dup], "base1")

    # 4. Missing category (e.g. replace all seeded_defect with localized_bug)
    missing_cat_tasks = []
    for t in tasks:
        t_copy = dict(t)
        if t_copy["category"] == "seeded_defect":
            t_copy["category"] = "localized_bug"
        missing_cat_tasks.append(t_copy)
    batch_missing_cat = _write_batch(tmp_path / "b_missing_cat.json", missing_cat_tasks)
    with pytest.raises(ValueError, match="missing categories"):
        freeze([batch_missing_cat], "base1")

    # 5. Fewer than 3 long_horizon
    no_long_tasks = []
    for t in tasks:
        tid = f"{t['task_id']}_short"
        fdir = _create_fixture_dir(tmp_path / "short_fixtures", tid, solution_patch=_PATCH_3_FILES)
        no_long_tasks.append({
            "task_id": tid,
            "category": t["category"],
            "fixture": str(fdir),
            "source_commit": t["source_commit"],
            "rationale": t["rationale"],
        })
    batch_no_long = _write_batch(tmp_path / "b_no_long.json", no_long_tasks)
    with pytest.raises(ValueError, match="long_horizon"):
        freeze([batch_no_long], "base1")


def test_digest_ignores_batch_order(tmp_path: Path) -> None:
    tasks = _generate_32_tasks(tmp_path)
    b1_tasks = tasks[:16]
    b2_tasks = tasks[16:]
    b1_path = _write_batch(tmp_path / "b1.json", b1_tasks)
    b2_path = _write_batch(tmp_path / "b2.json", b2_tasks)

    m1 = freeze([b1_path, b2_path], "commit_root_123")
    m2 = freeze([b2_path, b1_path], "commit_root_123")

    assert m1["corpus_sha256"] == m2["corpus_sha256"]
    assert m1["batches"] == m2["batches"]
    assert m1["tasks"] == m2["tasks"]


def test_seed_edit_drifts_and_verify(tmp_path: Path) -> None:
    tasks = _generate_32_tasks(tmp_path)
    b_path = _write_batch(tmp_path / "batch.json", tasks)
    manifest = freeze([b_path], "commit_root_123")

    # Clean manifest verifies with zero drift
    assert verify(manifest) == []

    # Edit seed.patch in task_01
    fdir = Path(tasks[0]["fixture"])
    seed_file = fdir / "seed.patch"
    seed_file.write_text("edited seed content that alters task_sha256\n", encoding="utf-8")

    drift = verify(manifest)
    assert len(drift) > 0
    assert any("task_01" in d for d in drift)


def test_cli_verify(tmp_path: Path) -> None:
    tasks = _generate_32_tasks(tmp_path)
    b_path = _write_batch(tmp_path / "batch.json", tasks)
    manifest = freeze([b_path], "commit_root_123")
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # CLI verify returns 0 on clean manifest
    rc = main(["verify", "--manifest", str(manifest_file)])
    assert rc == 0

    # Alter seed.patch -> CLI returns 1
    fdir = Path(tasks[0]["fixture"])
    (fdir / "seed.patch").write_text("tampered\n", encoding="utf-8")
    rc_drift = main(["verify", "--manifest", str(manifest_file)])
    assert rc_drift == 1
