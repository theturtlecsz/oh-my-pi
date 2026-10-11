"""Exercise patched Flood prompts only from a temporary, isolated copy.

OMP584_FLOOD_SOURCE names an already-patched directory. Never import or patch
that directory; without it, skip rather than discovering machine-local source.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
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


def extract_check_plan_command(prompt: str) -> str:
    match = re.search(r"`(python3\s+[^`]*check-plan\s+[^`]+)`", prompt)
    assert match is not None, f"check-plan command missing from prompt: {prompt}"
    return match.group(1)


def make_isolated_flood_environment(staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    tasks_dir: str = "staging_tasks", cfg_name: str = "custom-flood.json"):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = Flood Tester\n\temail = flood@example.com\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Flood Tester"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "flood@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    cfg_dir = tmp_path / "flood_cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / cfg_name
    cfg = {
        "repo": str(repo_dir),
        "main_branch": "main",
        "tasks_dir": tasks_dir,
        "worktrees_dir": str(tmp_path / "worktrees"),
        "logs_dir": "custom-logs",
        "state_file": "custom-state.json",
        "findings_file": "custom-findings.md",
        "rider_dir": str(tmp_path / "rider"),
        "control_files": [],
        "providers": {
            "test-planner": {"role": "plan", "cmd": ["dummy-planner", "{prompt}"]},
            "test-reviewer": {"role": "review", "cmd": ["dummy-reviewer", "{prompt}"]},
        },
        "reviewer": {"role": "review", "cmd": ["dummy-reviewer", "{prompt}"]},
    }
    cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    mod = load(staged)
    flood = mod.Flood(cfg, cfg_dir)
    flood.cfg_file = cfg_file
    return flood, cfg_dir, cfg_file, repo_dir


def test_extracted_check_plan_command_validates_proposed_slices_and_exposes_routing(staged, tmp_path, monkeypatch):
    tasks_dir_name = "staging_tasks"
    flood, cfg_dir, cfg_file, repo_dir = make_isolated_flood_environment(
        staged, tmp_path, monkeypatch, tasks_dir=tasks_dir_name, cfg_name="custom-flood.json"
    )

    tid = "OMP-584"
    wt = flood.plan_worktree(tid)
    wt.mkdir(parents=True, exist_ok=True)
    pdir = flood.proposed_dir(tid)
    pdir.mkdir(parents=True, exist_ok=True)

    # Decoy proposed directory under default tasks_dir ("tasks") exposing wrong routing
    decoy_pdir = cfg_dir / "tasks" / "proposed" / tid
    decoy_pdir.mkdir(parents=True, exist_ok=True)
    (decoy_pdir / f"{tid}-s01.md").write_text(
        "---\n"
        f"id: {tid}-s01\n"
        "kind: implement\n"
        "max_minutes: 90\n"
        "files: [src/decoy.py]\n"
        "depends_on: []\n"
        "---\n"
        "Decoy proposed slice without required test or done when sections.\n\n"
        "Do not touch:\n"
        "- decoy\n",
        encoding="utf-8",
    )

    # Valid fixture naming test path, files, dependencies, Done when: and Do not touch:
    slice_path = pdir / f"{tid}-s01.md"
    slice_valid_content = (
        "---\n"
        f"id: {tid}-s01\n"
        "kind: implement\n"
        "max_minutes: 90\n"
        "files: [src/sample.py]\n"
        "depends_on: []\n"
        "---\n"
        "Implement sample functionality and extend tests/test_sample.py.\n\n"
        "Done when:\n"
        "- All sample tests pass cleanly.\n\n"
        "Do not touch:\n"
        "- Other source files.\n"
    )
    slice_path.write_text(slice_valid_content, encoding="utf-8")

    st = flood.state.task(tid)
    task = {"id": tid, "body": "Implement isolated sample functionality with plan check."}
    prompt = flood.plan_prompt(task, st, wt, pdir)

    # Verify actual paths in prompt
    assert str(cfg_file.resolve()) in prompt
    assert str(wt.resolve()) in prompt
    assert str(pdir.resolve()) in prompt
    assert task["id"] in prompt

    # Extract exact command and execute as subprocess against temp config/proposed files
    cmd_str = extract_check_plan_command(prompt)
    cmd_argv = shlex.split(cmd_str)
    assert cmd_argv[0] == "python3"
    assert Path(cmd_argv[1]).resolve() == staged.resolve()
    assert cmd_argv[2] == "check-plan"
    assert cmd_argv[3] == tid
    assert cmd_argv[4] == "--config"
    assert Path(cmd_argv[5]).resolve() == cfg_file.resolve()

    res_valid = subprocess.run(cmd_argv, capture_output=True, text=True)
    assert res_valid.returncode == 0
    assert "OK: 1 slices pass the plan gate" in res_valid.stdout
    assert "decoy" not in res_valid.stdout.lower()

    # Remove only Done when:; same command exits 1 with missing-condition diagnostic
    slice_without_done_when = slice_valid_content.replace(
        "Done when:\n- All sample tests pass cleanly.\n\n", ""
    )
    assert "Done when:" not in slice_without_done_when
    assert "tests/test_sample.py" in slice_without_done_when
    assert "src/sample.py" in slice_without_done_when
    slice_path.write_text(slice_without_done_when, encoding="utf-8")

    res_invalid = subprocess.run(cmd_argv, capture_output=True, text=True)
    assert res_invalid.returncode == 1
    assert f"- {tid}-s01: no \"Done when:\" section" in res_invalid.stdout
    assert "names no test file to add or extend" not in res_invalid.stdout


def test_continuation_prompt_renders_context_and_executable_self_check(staged, tmp_path, monkeypatch):
    tasks_dir_name = "staging_tasks"
    flood, cfg_dir, cfg_file, repo_dir = make_isolated_flood_environment(
        staged, tmp_path, monkeypatch, tasks_dir=tasks_dir_name, cfg_name="custom-flood.json"
    )

    tid = "OMP-584"
    wt = flood.plan_worktree(tid)
    wt.mkdir(parents=True, exist_ok=True)
    pdir = flood.proposed_dir(tid)
    pdir.mkdir(parents=True, exist_ok=True)

    # Valid proposed slice in pdir
    (pdir / f"{tid}-s01.md").write_text(
        "---\n"
        f"id: {tid}-s01\n"
        "kind: implement\n"
        "max_minutes: 90\n"
        "files: [src/sample.py]\n"
        "depends_on: []\n"
        "---\n"
        "Implement sample functionality and extend tests/test_sample.py.\n\n"
        "Done when:\n"
        "- All sample tests pass cleanly.\n\n"
        "Do not touch:\n"
        "- Other source files.\n",
        encoding="utf-8",
    )

    task = {"id": tid, "body": "Implement isolated sample functionality with continuation."}
    st = flood.state.task(tid)
    last_error_text = "Reviewer failed plan: slice OMP-584-s02 has missing dependency on s01."
    owner_note_text = "Ensure slice s02 explicitly depends on s01 and names consumed artifact."
    st.update(
        attempts=2,
        history=[{"phase": "plan-review", "rc": 1, "verdict": "FAIL"}],
        last_error=last_error_text,
        note=owner_note_text,
    )

    prompt = flood.plan_prompt(task, st, wt, pdir)

    # Verify actual task, config, worktree, and proposed paths
    assert task["id"] in prompt
    assert task["body"] in prompt
    assert str(cfg_file.resolve()) in prompt
    assert str(wt.resolve()) in prompt
    assert str(pdir.resolve()) in prompt
    assert f"Your earlier slice files are still in {pdir}" in prompt

    # Verify continuation context rendered
    assert last_error_text in prompt
    assert owner_note_text in prompt
    assert "# Continuation" in prompt

    # Verify rules delivery
    assert "Planner independence" in prompt
    assert "Find disjoint files and outputs before choosing slice boundaries or dependencies." in prompt

    # Verify no unresolved dynamic fields
    assert not re.search(r"\{[a-zA-Z_][a-zA-Z0-9_.]*\}", prompt)
    assert "None" not in prompt
    assert "unknown" not in prompt

    # Extract self-check command and execute exact command as subprocess
    cmd_str = extract_check_plan_command(prompt)
    cmd_argv = shlex.split(cmd_str)
    res = subprocess.run(cmd_argv, capture_output=True, text=True)
    assert res.returncode == 0
    assert "OK: 1 slices pass the plan gate" in res.stdout

