"""Qualify candidate Flood bytes in isolation; never repair selected source."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest


ASSETS = Path(__file__).resolve().parents[3] / "infra" / "automation-user"
FIXTURE = Path(__file__).parent / "fixtures" / "omp622" / "flood-4fb53b8.py.txt"
ITEM = "OMP-622"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active_rules_path(candidate: Path) -> Path:
    name = "planner-independence.md" if b"planner-independence.md" in (candidate / "flood.py").read_bytes() else "omp584-planner-rules.md"
    return candidate / name


def source_snapshot(source: Path) -> dict:
    return {
        str(path.relative_to(source)): (digest(path), path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file() and path.suffix in (".py", ".pyc", ".md")
    }


def command(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    for key in tuple(os.environ):
        if key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CONFIG",
                   "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT") or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key)
    (home / "gitconfig").write_text("[user]\n name = Exact Candidate\n email = exact@example.invalid\n")
    dest = tmp_path / "candidate"
    dest.mkdir()
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    source = Path(selected) if selected is not None else FIXTURE.parent
    before = source_snapshot(source)
    try:
        if selected is not None:
            assert (source / "flood.py").is_file(), "OMP584_FLOOD_SOURCE must contain flood.py"
            for path in source.iterdir():
                if path.is_file() and path.suffix in (".py", ".md"):
                    shutil.copyfile(path, dest / path.name, follow_symlinks=True)
            assert (dest / "flood.py").read_bytes() == (source / "flood.py").read_bytes()
        else:
            shutil.copyfile(FIXTURE, dest / "flood.py")
            result = command(["git", "apply", str(ASSETS / "omp584-planner-prompt.patch")], dest)
            assert result.returncode == 0, result.stderr
        for name in ("planner-independence.md", "omp584-planner-rules.md"):
            if not (dest / name).is_file():
                shutil.copyfile(ASSETS / "omp584-planner-rules.md", dest / name)
        for argv in (["git", "init", "-b", "main"], ["git", "add", "."],
                     ["git", "commit", "-m", "candidate"]):
            result = command(argv, dest)
            assert result.returncode == 0, result.stderr
        yield dest
    finally:
        assert source_snapshot(source) == before, "qualification changed source bytes, mtimes or files"


def load(candidate: Path):
    spec = importlib.util.spec_from_file_location(f"omp622_exact_{uuid.uuid4().hex}", candidate / "flood.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    def __init__(self, candidate: Path, tmp_path: Path):
        self.module = load(candidate)
        self.cfg_dir = tmp_path / "config"
        self.cfg_dir.mkdir()
        self.cfg_file = self.cfg_dir / "isolated.json"
        reviewer = {"provider": "reviewer", "cmd": [
            sys.executable, "-c", "print('VERDICT: PASS')", "{prompt_file}", "{prompt}",
        ]}
        cfg = {
            "repo": str(candidate), "main_branch": "main", "tasks_dir": "exact-tasks",
            "worktrees_dir": str(tmp_path / "worktrees"), "control_files": [], "global_links": {},
            "rider_dir": str(tmp_path / "rider"), "rider_cwd": str(candidate),
            "providers": {"worker": {"role": "implement"}, "reviewer": {"role": "review"}},
            "reviewer": reviewer,
        }
        self.cfg_file.write_text(json.dumps(cfg))
        self.flood = self.module.Flood(cfg, self.cfg_dir)
        self.flood.cfg_file = self.cfg_file

    def close(self):
        for proc in self.flood.procs.values():
            proc.wait()


@pytest.fixture
def harness(candidate: Path, tmp_path: Path):
    h = Harness(candidate, tmp_path)
    try:
        yield h
    finally:
        h.close()


def slice_text(number: int, files: str, dependencies: str, work: str, excluded: str = "Other files.") -> str:
    return (
        f"---\nid: {ITEM}-s{number:02d}\nkind: implement\nmax_minutes: 90\n"
        f"files: [{files}]\ndepends_on: [{dependencies}]\n---\n"
        f"{work}\nExtend tests/test_exact.py.\n\nDone when:\n- Exact tests pass.\n\n"
        f"Do not touch:\n- {excluded}\n"
    )


def test_check_plan_requires_affirmative_dependency_use(harness, candidate):
    flood = harness.flood
    pdir = flood.proposed_dir(ITEM)
    pdir.mkdir(parents=True)
    (pdir / f"{ITEM}-s01.md").write_text(slice_text(1, "src/producer.py", "", "Create producer artifact."))
    consumer = pdir / f"{ITEM}-s02.md"
    for work, excluded, expected in (
        ("Implement independent consumer.", "Other files.", 1),
        ("Consumer reads src/producer.py.", "Other files.", 0),
        ("Implement independent consumer.", "reads src/producer.py", 1),
    ):
        consumer.write_text(slice_text(2, "src/consumer.py", f"{ITEM}-s01", work, excluded))
        result = command([sys.executable, str(candidate / "flood.py"), "check-plan", ITEM,
                          "--config", str(harness.cfg_file)], candidate)
        assert result.returncode == expected, result.stdout + result.stderr
        if expected:
            assert f"{ITEM}-s02: depends_on {ITEM}-s01" in result.stdout
        else:
            assert "OK: 2 slices pass the plan gate" in result.stdout
        assert "loaded " not in result.stdout + result.stderr


def test_real_spawn_records_import_hash_and_implement_attempts(harness, candidate):
    flood = harness.flood
    imported = digest(candidate / "flood.py")
    with (candidate / "flood.py").open("ab") as handle:
        handle.write(b"\n# disk edit after import\n")
    assert digest(candidate / "flood.py") != imported
    for tid, phase in (("plan", "plan"), ("implement", "implement"), ("implement", "implement")):
        assert flood.spawn({"id": tid}, phase, "worker", [sys.executable, "-c", "pass"], candidate, 1)
        assert flood.procs[tid].wait(timeout=10) == 0
        flood.state.task(tid).update(status="pending", job=None)
    persisted = json.loads(flood.state.path.read_text())["tasks"]
    plan = persisted["plan"]["history"]
    implement = persisted["implement"]["history"]
    assert [event["phase"] for event in plan] == ["plan-start"]
    assert [event["phase"] for event in implement] == ["implement-start", "implement-start"]
    assert [event["attempt"] for event in implement] == [1, 2]
    assert all(event["code_sha256"] == imported for event in plan + implement)


@pytest.mark.parametrize("continuation", [
    {"attempts": 1, "history": []},
    {"attempts": 0, "history": [{"phase": "plan-review", "verdict": "FAIL"}]},
])
def test_prompts_keep_loaded_rule_bytes(candidate, tmp_path, continuation):
    loaded_bytes = b"# Exact rules\r\n" + "Read producer artifacts — 独立 slices.\r\n".encode()
    for name in ("planner-independence.md", "omp584-planner-rules.md"):
        target = candidate / name
        if target.is_file():
            target.write_bytes(loaded_bytes)
    h = Harness(candidate, tmp_path)
    try:
        flood = h.flood
        assert h.module.PLANNER_RULES_SHA256 == hashlib.sha256(loaded_bytes).hexdigest()
        replacement = b"Replacement rules must not reach running prompts.\n"
        for name in ("planner-independence.md", "omp584-planner-rules.md"):
            target = candidate / name
            if target.is_file():
                target.write_bytes(replacement)
        task = {"id": ITEM, "body": "Plan exact candidate qualification."}
        st = flood.state.task(ITEM)
        wt, pdir = flood.plan_worktree(ITEM), flood.proposed_dir(ITEM)
        wt.mkdir(parents=True)
        pdir.mkdir(parents=True)
        (pdir / f"{ITEM}-s01.md").write_text(slice_text(1, "src/producer.py", "", "Create artifact."))
        for state in ({"attempts": 0, "history": []}, continuation):
            st.update(**state, last_error="Prior plan failed.", note="Keep slices independent.")
            planner = flood.plan_prompt(task, st, wt, pdir)
            flood.start_plan_review(task)
            proc = flood.procs[ITEM]
            assert proc.wait(timeout=10) == 0
            job = st["job"]
            reviewer = Path(job["argv"][-2]).read_bytes().decode()
            assert job["argv"][-1] == reviewer
            output = Path(job["log"]).read_bytes()[job["log_offset"]:].decode()
            assert output.splitlines() == ["VERDICT: PASS"]
            for prompt in (planner, reviewer):
                assert loaded_bytes.decode() in prompt
                assert "Replacement rules" not in prompt
            if state is continuation:
                assert "Prior plan failed." in planner
                assert "Keep slices independent." in planner
            else:
                assert "# Continuation" not in planner
            st.update(status="pending", job=None)
    finally:
        h.close()


def test_run_once_emits_one_loaded_record(harness, candidate, monkeypatch, capsys):
    code_hash = digest(candidate / "flood.py")
    rules_hash = digest(active_rules_path(candidate))
    head = command(["git", "rev-parse", "HEAD"], candidate)
    assert head.returncode == 0, head.stderr
    with (candidate / "flood.py").open("ab") as handle:
        handle.write(b"\n# changed after load\n")
    for name in ("planner-independence.md", "omp584-planner-rules.md"):
        target = candidate / name
        if target.is_file():
            target.write_bytes(b"changed after load\n")
    monkeypatch.setattr(harness.flood, "tick", harness.flood.state.save)
    capsys.readouterr()
    assert harness.flood.run(once=True) == 0
    records = [json.loads(line.split(" loaded ", 1)[1]) for line in capsys.readouterr().out.splitlines()
               if " loaded " in line]
    assert len(records) == 1
    record = records[0]
    assert record == {"event": "loaded", "at": record["at"], "pid": os.getpid(),
                      "code_sha256": code_hash, "rules_sha256": rules_hash, "external_head": head.stdout.strip()}
    assert dt.datetime.fromisoformat(record["at"]).utcoffset() == dt.timedelta(0)
    assert harness.flood.state.path.is_file()
