"""omp584-start-metrics.patch: Flood.spawn records plan-start / implement-start history events.

The tests stage flood.py in pytest's temp dir, apply the patch there and execute that copy. Set
OMP584_FLOOD_SOURCE to a directory holding an already-patched flood.py to run against it unpatched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "infra" / "automation-user" / "omp584-start-metrics.patch"
LIVE_FLOOD = Path.home() / "flood" / "flood.py"
SLEEP = [sys.executable, "-c", "import time; time.sleep(2)"]


def stage(tmp_path: Path) -> Path:
    dest = tmp_path / "stage"
    dest.mkdir()
    source = os.environ.get("OMP584_FLOOD_SOURCE")
    if source:
        shutil.copy(Path(source) / "flood.py", dest / "flood.py")
        return dest / "flood.py"
    if not LIVE_FLOOD.is_file():
        pytest.skip("no flood.py to patch (set OMP584_FLOOD_SOURCE)")
    shutil.copy(LIVE_FLOOD, dest / "flood.py")
    subprocess.run(["patch", "-p1", "--forward", "-d", str(dest), "-i", str(PATCH)], check=True, capture_output=True)
    return dest / "flood.py"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"flood_staged_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    def __init__(self, tmp_path: Path, **cfg) -> None:
        self.path = stage(tmp_path)
        self.mod = load(self.path)
        self.dir = tmp_path / "run"
        self.dir.mkdir()
        self.procs: list[subprocess.Popen] = []
        flood = self.mod.Flood.__new__(self.mod.Flood)   # spawn needs no repo, gh or worktree
        flood.cfg = {"start_gap_sec": 7, **cfg}
        flood.cfg_dir = self.dir
        flood.logs_dir = self.dir / "logs"
        flood.logs_dir.mkdir()
        flood.wt_dir = self.dir / "wt"
        flood.slots, flood.test_slots, flood.test_lock = 4, 2, None
        flood.providers = {
            "alpha": {"role": "implement", "max_parallel": 3},
            "beta": {"role": "implement"},
            "rev": {"role": "review", "max_parallel": 9},
        }
        flood.fence, flood.control_globs, flood.procs = {}, [], {}
        flood.state = self.mod.State(self.dir / "state.json")
        flood.proposed_dir = lambda tid: self.dir / "proposed" / tid
        self.flood = flood

    def spawn(self, tid: str, phase: str, provider: str, argv=SLEEP) -> bool:
        ok = self.flood.spawn({"id": tid}, phase, provider, argv, self.dir, 1)
        proc = self.flood.procs.get(tid)
        if ok:
            self.procs.append(proc)
        return ok

    def persisted(self, tid: str) -> list[dict]:
        return json.loads((self.dir / "state.json").read_text())["tasks"][tid]["history"]

    def close(self) -> None:
        # Let each short-lived command exit so its rc wrapper reaps it. Keep handles
        # across retries, which replace the task's entry in flood.procs.
        for proc in self.procs:
            proc.wait(timeout=10)


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


def test_implement_start_counts_only_live_implementers_and_reports_caps(harness):
    flood = harness.flood
    assert harness.spawn("other", "implement", "alpha")
    assert harness.spawn("tester", "test", "beta")
    assert harness.spawn("reviewer", "review", "rev")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    flood.state.task("ghost").update(status="running", phase="implement",
                                     job={"pid": dead.pid, "provider": "beta", "phase": "implement"})

    assert harness.spawn("mine", "implement", "alpha")

    event = harness.persisted("mine")[-1]
    assert event["phase"] == "implement-start"
    assert event["attempt"] == 1
    assert event["running_implementers"] == 2   # other + mine; test/review and dead ghost excluded
    assert (event["slots"], event["test_slots"], event["start_gap_sec"]) == (4, 2, 7.0)
    assert event["occupied_slots"] == 5         # scheduler counts all five running tasks
    assert event["providers"] == {"alpha": {"max_parallel": 3, "load": 2},
                                  "beta": {"max_parallel": 1, "load": 2}}   # review provider absent
    assert event["at"].endswith("Z")
    assert len(event["code_sha256"]) == 64


def test_implementer_exiting_before_measurement_is_not_counted(harness, monkeypatch):
    flood = harness.flood
    start_event = flood.start_event

    def after_exit(tid, phase):
        flood.procs[tid].wait(timeout=10)
        return start_event(tid, phase)

    monkeypatch.setattr(flood, "start_event", after_exit)
    assert harness.spawn("quick", "implement", "alpha", [sys.executable, "-c", "pass"])
    event = harness.persisted("quick")[-1]
    assert event["running_implementers"] == 0
    # Existing scheduler accounting still uses running state, even after PID exit.
    assert event["occupied_slots"] == 1
    assert event["providers"]["alpha"]["load"] == 1


def test_plan_start_has_provenance_only_and_runs_before_save(harness):
    assert harness.spawn("planner", "plan", "alpha")
    # read back from disk: the event was appended before the existing state.save
    assert harness.persisted("planner") == [
        {"phase": "plan-start", "at": harness.persisted("planner")[0]["at"], "attempt": 1,
         "code_sha256": harness.mod.CODE_SHA256}]


def test_failed_spawn_records_no_start_and_retry_gets_next_attempt(harness):
    assert not harness.spawn("flaky", "implement", "alpha", ["definitely-not-installed-omp584"])
    assert harness.flood.state.task("flaky")["history"] == []
    assert harness.flood.state.task("flaky")["attempts"] == 0

    assert harness.spawn("flaky", "implement", "alpha")
    harness.flood.procs["flaky"].wait(timeout=10)
    st = harness.flood.state.task("flaky")
    st["history"].append({"phase": "implement", "rc": 1, "attempt": 1})   # finish event, unchanged shape
    st.update(status="pending", job=None)
    assert harness.spawn("flaky", "implement", "alpha")

    starts = [h for h in harness.persisted("flaky") if h["phase"] == "implement-start"]
    assert [h["attempt"] for h in starts] == [1, 2]
    assert [h["running_implementers"] for h in starts] == [1, 1]


def test_code_hash_is_taken_at_load_not_from_later_disk_edit(harness):
    loaded = hashlib.sha256(harness.path.read_bytes()).hexdigest()
    assert harness.mod.CODE_SHA256 == loaded
    harness.path.write_text(harness.path.read_text() + "\n# edited after load\n")
    assert hashlib.sha256(harness.path.read_bytes()).hexdigest() != loaded

    assert harness.spawn("late", "implement", "alpha")

    assert harness.persisted("late")[-1]["code_sha256"] == loaded
