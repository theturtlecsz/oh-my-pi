"""Tests for OMP-622 rollout staging (preflight+stage).

Verifies:
- incremental: original→staged diff adds exactly the planner patch lines;
  other bytes kept; applied-parts = planner-prompt; no legacy-composable.
- planner partial (only a PLANNER_INDEPENDENCE line), and planner hunk context altered:
  full script exits nonzero naming planner-prompt.
- all cases: checkout unchanged, no evidence dir.
"""
from __future__ import annotations

import difflib
from pathlib import Path
import shutil
import subprocess

import pytest

from omp622_rollout_helpers import (
    ASSETS,
    checkout_snapshot,
    create_checkout,
    run_driver,
    run_script,
)


def test_stage_incremental_adds_exactly_planner_lines(tmp_path):
    """Incremental rollout stages only the missing planner prompt part and records it."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    before = checkout_snapshot(source, env=env)

    result = run_driver(
        source,
        evidence,
        functions=["omp584_preflight", "omp584_stage"],
        preserve_stage=True,
        env=env,
    )
    assert result.returncode == 0, f"Driver failed: {result.stderr}\nSTDOUT:\n{result.stdout}"
    assert result.stage_dir is not None and result.stage_dir.is_dir()

    try:
        stage = result.stage_dir

        applied_parts_file = stage / "applied-parts"
        assert applied_parts_file.is_file(), "applied-parts file missing"
        assert applied_parts_file.read_text().splitlines() == ["planner-prompt"]

        assert not (stage / "legacy-composable").exists(), "fixture should not be legacy-composable"

        original_flood = stage / "original-flood.py"
        staged_flood = stage / "flood.py"
        assert original_flood.is_file()
        assert staged_flood.is_file()

        assert original_flood.read_bytes() == (source / "flood.py").read_bytes()
        orig_lines = original_flood.read_bytes().decode("utf-8").splitlines(keepends=True)
        staged_lines = staged_flood.read_bytes().decode("utf-8").splitlines(keepends=True)
        diff = list(difflib.unified_diff(orig_lines, staged_lines, fromfile="original-flood.py", tofile="flood.py"))

        deletions = [line for line in diff if line.startswith("-") and not line.startswith("---")]
        assert deletions == [], f"Unexpected deletions in staged diff: {deletions}"

        added_diff = [line[1:].rstrip("\r\n") for line in diff if line.startswith("+") and not line.startswith("+++")]

        planner_patch = (ASSETS / "omp584-planner-prompt.patch").read_text(encoding="utf-8")
        expected_added = [
            line[1:]
            for line in planner_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        assert added_diff == expected_added

        # Reconstruct exact original bytes by removing only inserted planner lines.
        matcher = difflib.SequenceMatcher(a=orig_lines, b=staged_lines, autojunk=False)
        preserved = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            assert tag in ("equal", "insert"), "Installed code changed"
            if tag == "equal":
                preserved.extend(staged_lines[j1:j2])
        assert "".join(preserved).encode("utf-8") == original_flood.read_bytes()
        assert "part dependencies: present" in result.stderr
        assert "part start-metrics: present" in result.stderr
        assert "part planner-prompt: applied" in result.stderr
    finally:
        if result.stage_dir and result.stage_dir.exists():
            shutil.rmtree(result.stage_dir, ignore_errors=True)

    assert not evidence.exists(), "Evidence directory must not be created during preflight+stage"
    assert checkout_snapshot(source, env=env) == before, "Checkout was modified during preflight+stage"


def test_full_script_planner_partial_fails_naming_planner_prompt(tmp_path):
    """A flood.py with only a PLANNER_INDEPENDENCE line fails and names planner-prompt."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"

    flood_path = source / "flood.py"
    flood_text = flood_path.read_text(encoding="utf-8")
    flood_path.write_text(flood_text + "\nPLANNER_INDEPENDENCE = 'partial prompt'\n", encoding="utf-8")

    subprocess.run(
        ["git", "commit", "-am", "add partial planner independence"],
        cwd=source,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    before = checkout_snapshot(source, env=env)

    result = run_script(source, evidence, env=env)
    assert result.returncode == 2
    assert "planner-prompt: absent markers:" in result.stderr
    assert not evidence.exists(), "Evidence directory must not be created on failure"
    assert checkout_snapshot(source, env=env) == before, "Checkout was modified on failure"


def test_full_script_planner_hunk_context_altered_fails_naming_planner_prompt(tmp_path):
    """A missing planner prompt whose hunk context is altered fails and names planner-prompt."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"

    flood_path = source / "flood.py"
    flood_text = flood_path.read_text(encoding="utf-8")
    target_phrase = "depends on every other slice) touches a changelog."
    replacement = "depends on every other slice) touches a release note."
    assert target_phrase in flood_text, "Expected hunk context phrase not found in fixture"
    flood_path.write_text(flood_text.replace(target_phrase, replacement, 1), encoding="utf-8")

    subprocess.run(
        ["git", "commit", "-am", "alter planner hunk context"],
        cwd=source,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    before = checkout_snapshot(source, env=env)

    result = run_script(source, evidence, env=env)
    assert result.returncode == 2
    assert "error: part planner-prompt is missing and its patch does not apply" in result.stderr
    assert not evidence.exists(), "Evidence directory must not be created on failure"
    assert checkout_snapshot(source, env=env) == before, "Checkout was modified on failure"
