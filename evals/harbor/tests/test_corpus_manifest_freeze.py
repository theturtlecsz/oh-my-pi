"""Tests for frozen Harbor corpus manifest, freeze reproducibility, and plan scheduling."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from omp_harbor_eval.corpus import extract_patch_paths, freeze
from omp_harbor_eval.fixtures import load_fixture
from omp_harbor_eval.run_plan import load_plan, schedule

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "evals" / "harbor" / "corpus" / "manifest.json"
PLAN_PATH = REPO_ROOT / "evals" / "harbor" / "plans" / "h5-fleet9-v1.json"


@pytest.fixture(autouse=True)
def _ensure_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


def _expected_base_commit() -> str:
    manifest_rel = str(MANIFEST_PATH.relative_to(REPO_ROOT))
    diff_res = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", manifest_rel],
        cwd=REPO_ROOT,
        check=False,
    )
    if diff_res.returncode != 0:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()

    last_commit = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", manifest_rel],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    return subprocess.check_output(
        ["git", "rev-parse", f"{last_commit}^"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()


def test_manifest_base_commit_and_frozen_bytes() -> None:
    expected_base = _expected_base_commit()
    raw_text = MANIFEST_PATH.read_text(encoding="utf-8")
    doc = json.loads(raw_text)

    assert doc["base_commit"] == expected_base
    assert len(doc["tasks"]) == 30
    assert doc["batches"] == [
        "evals/harbor/corpus/batches/a.json",
        "evals/harbor/corpus/batches/b.json",
    ]

    frozen = freeze(doc["batches"], doc["base_commit"])
    expected_text = json.dumps(frozen, indent=2, sort_keys=True) + "\n"
    assert raw_text == expected_text
    assert MANIFEST_PATH.read_bytes() == expected_text.encode("utf-8")


def test_extract_patch_paths_and_long_horizon() -> None:
    doc = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for task_id, task_data in doc["tasks"].items():
        fixture_dir = REPO_ROOT / task_data["fixture"]
        fixture_obj = load_fixture(fixture_dir.parent, task_id)
        solution_path = fixture_dir / fixture_obj.solution_patch
        solution_text = solution_path.read_text(encoding="utf-8")
        assert isinstance(solution_text, str)

        extracted = extract_patch_paths(solution_text)
        assert extracted == task_data["source_paths"], f"task {task_id} source_paths mismatch"
        assert task_data["long_horizon"] == (len(extracted) >= 4), f"task {task_id} long_horizon mismatch"


def test_plan_schedule_routing_arms_long_horizon() -> None:
    plan = load_plan(PLAN_PATH)
    trials = schedule(plan)

    routing_trials = [t for t in trials if t.experiment_id == "routing"]
    assert len(routing_trials) == 180

    arms_trials = [t for t in trials if t.experiment_id == "arms"]
    arm_counts = Counter(t.arm_id for t in arms_trials)
    assert arm_counts["A"] == 60
    assert arm_counts["B"] == 60
    assert arm_counts["F"] == 60

    lh_trials = [t for t in trials if t.experiment_id == "long_horizon"]
    assert len(lh_trials) >= 24

    manifest_tasks = plan.manifest["tasks"]
    flagged_tasks = [tid for tid, tdata in manifest_tasks.items() if tdata.get("long_horizon") is True]
    assert len(flagged_tasks) >= 6

    lh_task_counts = Counter(t.task_id for t in lh_trials)
    assert set(lh_task_counts.keys()) == set(flagged_tasks)
    for tid in flagged_tasks:
        assert lh_task_counts[tid] == 4
