"""Staged Flood copies retain the rules loaded by their selected source."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_flood_independence_gate as gate
import test_flood_planner_prompt_cli as prompt
import test_flood_planner_rollout as planner_rollout
import test_flood_rollout_provenance as provenance
import test_omp622_exact as exact
import test_flood_start_metrics as metrics


@pytest.mark.parametrize("helper", ["metrics", "gate", "prompt"])
@pytest.mark.parametrize("rules_name", ["planner-independence.md", "omp584-planner-rules.md"])
def test_selected_rules_load_from_staged_copy(tmp_path, monkeypatch, helper, rules_name):
    source = tmp_path / "source"
    source.mkdir()
    rules = source / rules_name
    rules.write_bytes(b"Selected deployment rules.\r\n")
    script = source / "flood.py"
    script.write_text(
        "from pathlib import Path\n"
        f"PLANNER_RULES_BYTES = Path(__file__).with_name({rules_name!r}).read_bytes()\n"
    )
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(source))
    if helper == "gate":
        destination = tmp_path / "stage"
        destination.mkdir()
        staged, originals = gate._resolve_stage(destination)
        assert rules in originals
    else:
        staged = {"metrics": metrics.stage, "prompt": prompt.stage}[helper](tmp_path)

    module = metrics.load(staged)

    assert module.PLANNER_RULES_BYTES == b"Selected deployment rules.\r\n"
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()} == before


@pytest.mark.parametrize("with_rules", [False, True])
def test_start_metrics_suite_with_selected_source(tmp_path, monkeypatch, with_rules):
    source = tmp_path / "source"
    source.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "omp622" / "flood-4fb53b8.py.txt"
    code = fixture.read_text()
    if with_rules:
        (source / "planner-independence.md").write_text("Selected deployment rules.\n")
        code += '\nPLANNER_RULES_BYTES = Path(__file__).with_name("planner-independence.md").read_bytes()\n'
    (source / "flood.py").write_text(code)
    # Gate options belong to the parent run. Reusing its basetemp would let
    # nested pytest delete the selected source before staging it.
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--basetemp={tmp_path}")
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(metrics.__file__)), "-q",
         "--basetemp", str(tmp_path / "child-basetemp")],
        env={**child_env, "OMP584_FLOOD_SOURCE": str(source)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 passed" in result.stdout
    assert (source / "flood.py").read_text() == code
    if with_rules:
        assert (source / "planner-independence.md").read_text() == "Selected deployment rules.\n"


@pytest.mark.parametrize("helper", [planner_rollout, provenance, exact])
@pytest.mark.parametrize("rules_name", ["planner-independence.md", "omp584-planner-rules.md"])
def test_rollout_staging_preserves_selected_rules(tmp_path, monkeypatch, helper, rules_name):
    source = tmp_path / "source"
    source.mkdir()
    (source / rules_name).write_bytes(b"Selected rollout rules.\r\n")
    (source / "flood.py").write_text(
        "from pathlib import Path\n"
        f"PLANNER_RULES_BYTES = Path(__file__).with_name({rules_name!r}).read_bytes()\n"
    )
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(source))
    monkeypatch.setenv("OMP584_TEST_EXACT", "1")
    if helper is provenance:
        # This test exercises staging; patch composition has separate coverage.
        monkeypatch.setattr(provenance, "compose", lambda directory, patches: None)
    fixture = helper.candidate.__wrapped__(tmp_path, monkeypatch)
    try:
        candidate = next(fixture)
        if helper is provenance:
            candidate, _ = candidate
        elif helper is exact:
            candidate = candidate / "flood.py"
        assert metrics.load(candidate).PLANNER_RULES_BYTES == b"Selected rollout rules.\r\n"
    finally:
        fixture.close()
