"""Tests for Harbor corpus_build: materialize, gradeable, check, and freeze gradeability."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omp_harbor_eval.corpus import load_batch, main as corpus_main
from omp_harbor_eval.corpus_build import (
    apply,
    gradeable,
    main as corpus_build_main,
    materialize,
    run_tests,
    worktree,
)
from omp_harbor_eval.fixtures import Fixture, IndependentTest
from omp_harbor_eval.verify import REPO_ROOT


def test_corpus_tasks_anchor_is_tracked_for_clean_checkouts() -> None:
    anchor = "evals/harbor/corpus/tasks/.gitkeep"
    tracked = subprocess.run(
        ["git", "ls-files", "--", anchor],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.splitlines() == [anchor]


def _init_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Initialize a git repo with a root commit, a bug commit (C^), and a fix commit (C)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    # Initial commit so all later commits have parents
    init_file = repo / "README.md"
    init_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    c_root = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 1: buggy addition (a - b)
    calc = repo / "calc.ts"
    calc.write_text("export function add(a: number, b: number): number {\n    return a - b;\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "buggy"], cwd=repo, check=True, capture_output=True)
    c_parent = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 2: fixed addition (a + b) and test
    calc.write_text("export function add(a: number, b: number): number {\n    return a + b;\n}\n", encoding="utf-8")
    test_file = repo / "calc.test.ts"
    test_file.write_text(
        "import { test, expect } from \"bun:test\";\nimport { add } from \"./calc\";\n\ntest(\"adds\", () => {\n    expect(add(1, 2)).toBe(3);\n});\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.ts", "calc.test.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix and test"], cwd=repo, check=True, capture_output=True)
    c_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    return repo, c_root, c_parent, c_sha


def test_materialize_check_and_load_batch_tmp_repo(tmp_path: Path) -> None:
    repo, _, _, c_sha = _init_repo(tmp_path)
    tasks_root = tmp_path / "evals" / "harbor" / "corpus" / "tasks"
    out_dir = tasks_root / "task_add"

    spec = {
        "task_id": "task_add",
        "category": "localized_bug",
        "title": "Fix addition in calc.ts",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix the addition operator in calc.ts so calc.test.ts passes.",
        "acceptance_criteria": ["addition returns the sum of the inputs"],
    }

    result_dir = materialize(spec, repo=repo, out=out_dir)
    assert result_dir == out_dir
    assert (out_dir / "seed.patch").is_file()
    assert (out_dir / "solution.patch").is_file()
    assert (out_dir / "instruction.md").is_file()
    assert (out_dir / "ledger-seed.json").is_file()
    assert (out_dir / "fixture.json").is_file()
    assert (out_dir / "independent" / "calc.test.ts").is_file()
    assert (out_dir / "task.toml").is_file()
    assert (out_dir / "scenario.json").is_file()
    assert (out_dir / "tests" / "test.sh").is_file()
    assert (out_dir / "solution" / "solve.sh").is_file()
    assert (out_dir / "environment" / "docker-compose.yaml").is_file()

    # Verify check / gradeable
    res = gradeable(out_dir, repo=repo, commit=c_sha)
    assert res["task_id"] == "task_add"
    assert res["seed_fails"] is True
    assert res["solution_passes"] is True
    assert res["ok"] is True

    # Verify check CLI
    check_code = corpus_build_main(["check", "--fixture", str(out_dir), "--repo", str(repo), "--commit", c_sha])
    assert check_code == 0

    # Verify load_batch
    batch_file = tmp_path / "evals" / "harbor" / "corpus" / "batch.json"
    batch_file.parent.mkdir(parents=True, exist_ok=True)
    batch_file.write_text(
        json.dumps({
            "tasks": [
                {
                    "task_id": "task_add",
                    "category": "localized_bug",
                    "fixture": str(out_dir),
                    "source_commit": c_sha,
                    "rationale": "Real fix addition bug",
                }
            ]
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_batch(batch_file)
    assert len(loaded) == 1
    assert loaded[0]["task_id"] == "task_add"
    assert loaded[0]["source_paths"] == ["calc.ts"]
    assert loaded[0]["long_horizon"] is False


def test_empty_files_solution_passes_false(tmp_path: Path) -> None:
    repo, _, _, c_sha = _init_repo(tmp_path)
    out_dir = tmp_path / "evals" / "harbor" / "corpus" / "tasks" / "task_empty_files"

    spec = {
        "task_id": "task_empty_files",
        "category": "localized_bug",
        "title": "Fix addition with empty files",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix addition in calc.ts.",
        "acceptance_criteria": ["addition works"],
    }
    materialize(spec, repo=repo, out=out_dir)

    # Corrupt fixture.json so test files list is empty
    fixture_json_path = out_dir / "fixture.json"
    fixture_data = json.loads(fixture_json_path.read_text(encoding="utf-8"))
    fixture_data["independent_tests"][0]["files"] = []
    fixture_json_path.write_text(json.dumps(fixture_data, indent=2) + "\n", encoding="utf-8")

    res = gradeable(out_dir, repo=repo, commit=c_sha)
    assert res["seed_fails"] is True
    assert res["solution_passes"] is False
    assert res["ok"] is False

    exit_code = corpus_build_main(["check", "--fixture", str(out_dir), "--repo", str(repo), "--commit", c_sha])
    assert exit_code == 1


def test_no_fix_seed_fails_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    # Initial commit with add(a, b) = a + b and comment v1
    calc = repo / "calc.ts"
    calc.write_text("// version 1\nexport function add(a: number, b: number): number {\n    return a + b;\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)

    # Commit 2 changes only the comment to v2, and adds test (addition was never broken)
    calc.write_text("// version 2\nexport function add(a: number, b: number): number {\n    return a + b;\n}\n", encoding="utf-8")
    test_file = repo / "calc.test.ts"
    test_file.write_text(
        "import { test, expect } from \"bun:test\";\nimport { add } from \"./calc\";\n\ntest(\"adds\", () => {\n    expect(add(1, 2)).toBe(3);\n});\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.ts", "calc.test.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2 comment and test"], cwd=repo, check=True, capture_output=True)
    c_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    out_dir = tmp_path / "evals" / "harbor" / "corpus" / "tasks" / "task_no_fix"

    spec = {
        "task_id": "task_no_fix",
        "category": "localized_bug",
        "title": "Task without fix",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Do nothing.",
        "acceptance_criteria": ["addition works"],
    }
    materialize(spec, repo=repo, out=out_dir)

    res = gradeable(out_dir, repo=repo, commit=c_sha)
    assert res["seed_fails"] is False
    assert res["solution_passes"] is True
    assert res["ok"] is False

    exit_code = corpus_build_main(["check", "--fixture", str(out_dir), "--repo", str(repo), "--commit", c_sha])
    assert exit_code == 1


def test_seeded_spec_ok(tmp_path: Path) -> None:
    repo, _, _, c_sha = _init_repo(tmp_path)
    out_dir = tmp_path / "evals" / "harbor" / "corpus" / "tasks" / "task_seeded"

    defect = (
        "diff --git a/calc.ts b/calc.ts\n"
        "--- a/calc.ts\n"
        "+++ b/calc.ts\n"
        "@@ -1,3 +1,3 @@\n"
        " export function add(a: number, b: number): number {\n"
        "-    return a + b;\n"
        "+    return a * b;\n"
        " }\n"
    )

    spec = {
        "task_id": "task_seeded",
        "category": "seeded_defect",
        "title": "Repair multiplication defect",
        "source_commit": c_sha,
        "defect_patch": defect,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Restore addition in calc.ts.",
        "acceptance_criteria": ["addition works"],
    }

    materialize(spec, repo=repo, out=out_dir)
    res = gradeable(out_dir, repo=repo, commit=c_sha)
    assert res["task_id"] == "task_seeded"
    assert res["seed_fails"] is True
    assert res["solution_passes"] is True
    assert res["ok"] is True


def test_empty_diff_and_leak_refused(tmp_path: Path) -> None:
    repo, c_root, c_parent, c_sha = _init_repo(tmp_path)
    out_dir = tmp_path / "out"

    # Commit 3 that does not modify calc.ts:
    doc = repo / "DOCS.md"
    doc.write_text("documentation\n", encoding="utf-8")
    subprocess.run(["git", "add", "DOCS.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "docs only"], cwd=repo, check=True, capture_output=True)
    c_docs = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # 1. Empty diff: commit that changed nothing in source_paths
    spec_empty = {
        "task_id": "task_empty",
        "category": "localized_bug",
        "title": "Empty diff task",
        "source_commit": c_docs,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix something.",
        "acceptance_criteria": ["passes"],
    }
    with pytest.raises(ValueError, match="empty diff"):
        materialize(spec_empty, repo=repo, out=out_dir)

    # 2. Untouched path in source_paths
    spec_untouched = {
        "task_id": "task_untouched",
        "category": "localized_bug",
        "title": "Untouched path task",
        "source_commit": c_sha,
        "source_paths": ["calc.ts", "nonexistent.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix something.",
        "acceptance_criteria": ["passes"],
    }
    with pytest.raises(ValueError, match="untouched path"):
        materialize(spec_untouched, repo=repo, out=out_dir)

    # 3. Leak refused: withheld_detail in title
    spec_leak_title = {
        "task_id": "task_leak",
        "category": "localized_bug",
        "title": "Fix secret_token issue",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix something.",
        "acceptance_criteria": ["passes"],
        "withheld_detail": "SECRET_TOKEN",
    }
    with pytest.raises(ValueError, match="withheld_detail leaked into title"):
        materialize(spec_leak_title, repo=repo, out=out_dir)

    # 4. Leak refused: withheld_detail in instruction (case-insensitive)
    spec_leak_inst = {
        "task_id": "task_leak",
        "category": "localized_bug",
        "title": "Fix problem",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Do not leak secret_token here.",
        "acceptance_criteria": ["passes"],
        "withheld_detail": "SECRET_TOKEN",
    }
    with pytest.raises(ValueError, match="withheld_detail leaked into instruction"):
        materialize(spec_leak_inst, repo=repo, out=out_dir)

    # 5. Leak refused: withheld_detail in AC
    spec_leak_ac = {
        "task_id": "task_leak",
        "category": "localized_bug",
        "title": "Fix problem",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Fix it.",
        "acceptance_criteria": ["verify secret_token is hidden"],
        "withheld_detail": "SECRET_TOKEN",
    }
    with pytest.raises(ValueError, match="withheld_detail leaked into acceptance criteria"):
        materialize(spec_leak_ac, repo=repo, out=out_dir)


def test_freeze_seed_passing_test_exits_1_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    calc = repo / "calc.ts"
    calc.write_text("// version 1\nexport function add(a: number, b: number): number {\n    return a + b;\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)

    calc.write_text("// version 2\nexport function add(a: number, b: number): number {\n    return a + b;\n}\n", encoding="utf-8")
    test_file = repo / "calc.test.ts"
    test_file.write_text(
        "import { test, expect } from \"bun:test\";\nimport { add } from \"./calc\";\n\ntest(\"adds\", () => {\n    expect(add(1, 2)).toBe(3);\n});\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.ts", "calc.test.ts"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2 comment and test"], cwd=repo, check=True, capture_output=True)
    c_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    tasks_root = tmp_path / "evals" / "harbor" / "corpus" / "tasks"
    out_dir = tasks_root / "task_passing_seed"

    spec = {
        "task_id": "task_passing_seed",
        "category": "localized_bug",
        "title": "Task passing on seed",
        "source_commit": c_sha,
        "source_paths": ["calc.ts"],
        "test_target": "calc.test.ts",
        "runner": "bun",
        "instruction": "Do nothing.",
        "acceptance_criteria": ["works"],
    }
    materialize(spec, repo=repo, out=out_dir)

    batch_file = tmp_path / "batch.json"
    batch_file.write_text(
        json.dumps({
            "tasks": [
                {
                    "task_id": "task_passing_seed",
                    "category": "localized_bug",
                    "fixture": str(out_dir),
                    "source_commit": c_sha,
                    "rationale": "Passing seed",
                }
            ]
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest_path = tmp_path / "manifest.json"
    exit_code = corpus_main([
        "freeze",
        "--batch",
        str(batch_file),
        "--base-commit",
        c_sha,
        "--repo",
        str(repo),
        "--out",
        str(manifest_path),
    ])
    assert exit_code == 1
    assert not manifest_path.exists()
    captured = capsys.readouterr()
    assert "task not gradeable: task_passing_seed" in captured.err


def test_run_tests_tree_node_modules_resolves_to_repo_root(tmp_path: Path) -> None:
    repo, _, _, c_sha = _init_repo(tmp_path)
    wt = worktree(repo, c_sha)
    try:
        run_tests(wt, [])
        link = wt / "node_modules"
        assert link.is_symlink()
        assert link.resolve() == (REPO_ROOT / "node_modules").resolve()
    finally:
        wt.cleanup()
