"""Exercise patched Flood prompts only from a temporary, isolated copy.

OMP584_FLOOD_SOURCE names an already-patched directory. Never import or patch
that directory; without it, skip rather than discovering machine-local source.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "infra" / "automation-user"


def stage(tmp_path: Path) -> Path:
    source = os.environ.get("OMP584_FLOOD_SOURCE")
    if not source:
        pytest.skip("set OMP584_FLOOD_SOURCE to an already-patched Flood directory")
    dest = tmp_path / "stage"
    dest.mkdir()
    shutil.copyfile(Path(source) / "flood.py", dest / "flood.py")
    shutil.copyfile(ASSETS / "planner-independence.md", dest / "planner-independence.md")
    shutil.copyfile(ASSETS / "omp584-planner-independence.patch", dest / "omp584-planner-independence.patch")
    return dest / "flood.py"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"flood_planner_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def staged(tmp_path):
    return stage(tmp_path)


def test_override_without_sibling_is_copied_without_repatching(staged, tmp_path, monkeypatch):
    override = tmp_path / "override"
    override.mkdir()
    shutil.copyfile(staged, override / "flood.py")
    monkeypatch.setenv("OMP584_FLOOD_SOURCE", str(override))
    second = tmp_path / "second"
    second.mkdir()

    copied = stage(second)

    assert copied.read_bytes() == staged.read_bytes()
    assert copied.with_name("planner-independence.md").read_bytes() == (ASSETS / "planner-independence.md").read_bytes()
    # An already-patched override need not ship the sibling; the staged repo
    # rules make it importable without changing the supplied source directory.
    load(copied)
    assert list(override.iterdir()) == [override / "flood.py"]


@pytest.mark.parametrize("continuation", [
    {"attempts": 1, "history": []},
    {"attempts": 0, "history": [{"phase": "plan-review"}]},
])
def test_planner_and_review_launch_keep_loaded_rules_on_continuation(staged, tmp_path, continuation):
    rules = staged.with_name("planner-independence.md")
    original = rules.read_text() + "\nTemporary independence marker: original rules.\n"
    rules.write_text(original)
    module = load(staged)
    flood = module.Flood.__new__(module.Flood)
    flood.main = "test-main"
    flood.cfg = {"prompt_notes": ["Temporary repository convention."]}
    flood.cfg_file = tmp_path / "test-config.json"
    flood.logs_dir = tmp_path / "logs"
    flood.logs_dir.mkdir()
    flood.state = module.State(tmp_path / "state.json")
    task = {"id": "OMP-584", "body": "Temporary item specification."}
    st = flood.state.task(task["id"])
    wt, pdir = tmp_path / "checkout", tmp_path / "slices"
    pdir.mkdir()
    slice_body = "Temporary slice with producer artifact output.json."
    (pdir / "OMP-584-s01.md").write_text(slice_body)
    flood.proposed_dir = lambda tid: pdir
    flood.plan_worktree = lambda tid: wt
    flood.prior_findings = lambda tid: "Temporary prior review finding."
    reviewer = {"cmd": ["stub-launch", "{prompt}", "{prompt_file}", "{worktree}"], "provider": "reviewer"}
    flood.reviewers = [reviewer]
    flood.pick_reviewer = lambda task: reviewer
    flood.reviewer_benched = lambda rv: False
    launches = []

    def spawn(task, phase, provider, argv, cwd, max_minutes):
        launches.append((phase, argv, cwd))
        return True

    flood.spawn = spawn

    def prompts():
        planner = flood.plan_prompt(task, st, wt, pdir)
        flood.start_plan_review(task)
        phase, argv, cwd = launches[-1]
        assert phase == "plan-review"
        assert cwd == wt
        assert Path(argv[2]).read_text() == argv[1]
        return planner, argv[1]

    first = prompts()
    rules.write_text("Temporary independence marker: replacement rules.\n")
    st.update(**continuation, last_error="Temporary failed planning attempt.", note="Temporary owner instruction.")
    later = prompts()

    for planner, reviewer_prompt in (first, later):
        for prompt in (planner, reviewer_prompt):
            assert original in prompt
            assert "replacement rules" not in prompt
            assert task["body"] in prompt
            assert flood.cfg["prompt_notes"][0] in prompt
            assert str(wt) in prompt
            assert str(pdir) in prompt
        assert f"python3 {staged.resolve()} check-plan {task['id']} --config {flood.cfg_file}" in planner
        assert "Temporary prior review finding." in planner
        assert slice_body in reviewer_prompt
    assert "Temporary failed planning attempt." not in first[0]
    assert st["last_error"] in later[0]
    assert st["note"] in later[0]
