"""OMP-584 report classifies isolated history and defers slice evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "infra/automation-user/omp584-report.py"
CUTOFF = "2026-10-01T00:00:00Z"
AFTER = "2026-10-01T00:00:01Z"
SHA = "a" * 64
STUB = '''
def parse_front_matter(text):
    meta = {}
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for line in lines[1:lines.index("---", 1)]:
            key, sep, value = line.partition(":")
            if sep:
                value = value.strip()
                meta[key.strip()] = [] if key.strip() == "depends_on" and value in ("", "[]") else value
    return meta, text

def is_owner(meta):
    return str(meta.get("owner", "")).lower() in ("true", "yes", "1")
'''


class Evidence:
    def __init__(self, tmp_path):
        self.directory = tmp_path / "flood"
        self.directory.mkdir()
        (self.directory / "tasks").mkdir()
        source = os.environ.get("OMP584_FLOOD_SOURCE")
        if source:
            for name in ("flood.py", "planner-independence.md"):
                shutil.copyfile(Path(source) / name, self.directory / name)
        else:
            (self.directory / "flood.py").write_text(STUB)
        self.rollout = tmp_path / "rollout.json"
        self.rollout.write_text(json.dumps(dict(activated_at=CUTOFF, flood_sha256=SHA, repo_commit="echo-me", baseline=1)))
        self.state = self.directory / "flood-state.json"
        self.state.write_text('{"tasks": {}}')
        self.output = tmp_path / "report.json"
        self.tasks = {}

    def slice(self, name, depends="[]", owner="false", parked=""):
        dep = "" if depends is None else f"depends_on: {depends}\n"
        (self.directory / "tasks" / f"{name}.md{parked}").write_text(f"---\n{dep}owner: {owner}\n---\nTask body\n")
        return name

    def plan(self, name, roots=2, at=AFTER, sha=SHA):
        path = self.directory / "tasks" / f"{name}.md"
        path.write_text("---\ndepends_on: []\ndepends_on: []\n---\nTask body\n")
        slices = [self.slice(f"{name}-s{i}", "[]" if i < roots else "[upstream]") for i in range(3)]
        self.tasks[name] = dict(slices=slices, history=[
            dict(phase="plan-start", attempt=1, at=at, code_sha256=sha),
            dict(phase="plan-review", attempt=1, verdict="pAsS", at=CUTOFF)])
        return self.tasks[name]

    def starts(self, count=10, running=3, **fields):
        self.tasks["unfinished"] = {"history": [dict(phase="implement-start", attempt=i, at=AFTER,
            code_sha256=SHA, running_implementers=running, **fields) for i in range(count)]}

    def run(self, write_state=True):
        if write_state:
            self.state.write_text(json.dumps({"tasks": self.tasks}))
        before = {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        completed = subprocess.run([sys.executable, str(SCRIPT), "--flood-dir", str(self.directory),
            "--rollout", str(self.rollout), "--output", str(self.output)], capture_output=True, text=True)
        after = {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        assert after == before
        assert {str(p.relative_to(self.directory)) for p in self.directory.rglob("*") if p.is_dir()} == {"tasks"}
        return completed.returncode, json.loads(self.output.read_text())


@pytest.fixture
def evidence(tmp_path):
    return Evidence(tmp_path)


def test_empty_history_is_pending_and_hashes_raw_inputs(evidence):
    code, report = evidence.run()
    assert code == 2
    assert report["status"] == "pending"
    assert report["plan_count"] == report["start_count"] == 0
    assert report["median"] is None and report["error"] is None
    assert report["cap"]["proven"] is False
    assert report["cutoff"] == dict(activated_at=CUTOFF, flood_sha256=SHA, repo_commit="echo-me", baseline=3.0)
    assert report["input_hashes"] == dict(flood_state_sha256=hashlib.sha256(evidence.state.read_bytes()).hexdigest(),
        rollout_sha256=hashlib.sha256(evidence.rollout.read_bytes()).hexdigest())


@pytest.mark.parametrize("input_name,content", [
    ("rollout", None), ("rollout", "bad json"), ("rollout", "[]"),
    ("rollout", '{"activated_at":"yesterday","flood_sha256":"x","repo_commit":"y"}'),
    ("rollout", '{"activated_at":"2026-10-01T00:00:00Z","flood_sha256":"","repo_commit":"y"}'),
    ("state", None), ("state", "bad json"), ("state", "[]"), ("state", '{"tasks":[]}'),
])
def test_invalid_inputs_still_write_failure(evidence, input_name, content):
    path = getattr(evidence, input_name)
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    code, report = evidence.run(write_state=False)
    assert code == 2 and report["status"] == "failure" and report["error"]
    key = "rollout_sha256" if input_name == "rollout" else "flood_state_sha256"
    assert report["input_hashes"][key] == (None if content is None else hashlib.sha256(content.encode()).hexdigest())


@pytest.mark.parametrize("source", ["def is_owner(meta): return False\n", "raise RuntimeError('import broke')\n",
    "def parse_front_matter(text): return {}, text\n"])
def test_missing_functions_or_import_error_fail(evidence, source):
    (evidence.directory / "flood.py").write_text(source)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and "flood import" in report["error"]


@pytest.mark.parametrize("function", ["parse_front_matter", "is_owner"])
def test_slice_calls_are_deferred_without_losing_classified_counts(evidence, function):
    (evidence.directory / "flood.py").write_text(STUB + f"\ndef {function}(value): raise RuntimeError('call broke')\n")
    evidence.plan("plan")
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 1 and report["start_count"] == 10
    assert report["verdicts"] == {"plans": "pending", "starts": "met"}


def test_qualifiers_sorted_once_with_independence_pending(evidence):
    for i in reversed(range(10)):
        evidence.plan(f"p{i}")
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plans"] == [dict(id=f"p{i}", attempt=1, code_sha256=SHA,
        slices=None, roots=None, implementer_slices=None, implementer_roots=None) for i in range(10)]
    assert report["plan_count"] == 10 and report["independent_plan_count"] == 0
    assert report["verdicts"] == {"plans": "pending", "starts": "met"}


@pytest.mark.parametrize("parked", [".failed", ".dropped"])
def test_slice_files_are_deferred_even_when_only_parked_twin_exists(evidence, parked):
    task = evidence.plan("p")
    task["slices"] = [evidence.slice("parked", parked=parked)]
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 1


def test_excluded_plans_are_not_read_and_review_time_is_not_cutoff(evidence):
    for name, at, sha in [("equal", CUTOFF, SHA), ("old", "2026-09-30T23:59:59Z", SHA), ("wrong", AFTER, "b")]:
        task = evidence.plan(name, at=at, sha=sha)
        task["slices"] = ["absent"]
    evidence.tasks["legacy"] = dict(slices=["absent"], planned_at=AFTER,
        history=[dict(phase="plan-review", verdict="PASS")])
    evidence.plan("included")
    code, report = evidence.run()
    assert code == 2 and report["error"] is None and report["plan_count"] == 1
    assert report["plans"][0]["id"] == "included"


@pytest.mark.parametrize("history", [
    [dict(phase="plan-start", attempt=1, at=AFTER, code_sha256=SHA)],
    [dict(phase="plan-start", attempt=2, at=AFTER, code_sha256=SHA), dict(phase="plan-review", attempt=1, verdict="PASS")],
    [dict(phase="plan-start", attempt=1, at=AFTER, code_sha256=SHA)] * 2 + [dict(phase="plan-review", attempt=1, verdict="PASS")],
    [dict(phase="plan-start"), dict(phase="plan-review", verdict="PASS")],
    [dict(phase="plan-start", attempt=None, at=AFTER, code_sha256=SHA),
     dict(phase="plan-review", attempt=None, verdict="PASS")],
    [dict(phase="plan-start", attempt=1, at=None, code_sha256=SHA), dict(phase="plan-review", attempt=1, verdict="PASS")],
])
def test_missing_success_ambiguous_attempt_or_malformed_start_fail(evidence, history):
    evidence.tasks["p"] = dict(slices=["absent"], history=history)
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert report["start_count"] == 10 and report["verdicts"]["starts"] == "met"


def test_last_pass_attempt_wins_over_retries(evidence):
    task = evidence.plan("p")
    task["history"] += [dict(phase="plan-review", attempt=1, verdict="FAIL"),
        dict(phase="plan-start", attempt=2, at=AFTER, code_sha256=SHA),
        dict(phase="plan-review", attempt=2, verdict="PASS")]
    _, report = evidence.run()
    assert report["plan_count"] == 1 and report["plans"][0]["attempt"] == 2


@pytest.mark.parametrize("fields,kind", [
    ({"slots": 2, "occupied_slots": 2}, "slots"),
    ({"providers": {"alpha": {"load": 2, "max_parallel": 2}, "beta": {"load": 1, "max_parallel": 1}}}, "providers"),
    ({"slots": 2, "occupied_slots": 3, "providers": {"alpha": {"load": 1, "max_parallel": 1}}}, "slots"),
    ({"slots": True, "occupied_slots": 2}, None),
    ({"slots": 2, "occupied_slots": 1}, None),
    ({"providers": {"alpha": {"load": 2, "max_parallel": 2}, "spare": {"load": 0, "max_parallel": 1}}}, None),
    ({"providers": {"alpha": {"load": True, "max_parallel": 1}}}, None),
    ({"providers": {}}, None),
])
def test_low_median_needs_complete_capacity_proof(evidence, fields, kind):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.starts(**fields)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending"
    assert report["verdicts"]["starts"] == ("met" if kind else "unmet")
    assert report["cap"]["proven"] == bool(kind) and report["cap"]["kind"] == kind
    assert report["median"] == 3 and report["cutoff"]["baseline"] == 3.0


@pytest.mark.parametrize("fields,change", [
    ({"slots": 2, "occupied_slots": 2}, {"slots": 3}),
    ({"providers": {"a": {"load": 1, "max_parallel": 1}}}, {"providers": {"b": {"load": 1, "max_parallel": 1}}}),
])
def test_cap_proof_must_cover_every_included_event(evidence, fields, change):
    evidence.starts(**fields)
    evidence.tasks["unfinished"]["history"][-1].update(change)
    _, report = evidence.run()
    assert report["cap"]["proven"] is False and report["verdicts"]["starts"] == "unmet"


def test_start_cutoff_retries_and_median_use_event_counts(evidence):
    evidence.starts(count=10, running=2)
    history = evidence.tasks["unfinished"]["history"]
    for event in history[5:]:
        event["running_implementers"] = 5
    history += [dict(phase="implement-start", at=CUTOFF, code_sha256=SHA),
                dict(phase="implement-start", at=AFTER, code_sha256="old")]
    _, report = evidence.run()
    assert report["start_count"] == 10 and report["median"] == 3.5
    assert report["status"] == "pending" and report["verdicts"] == {"plans": "pending", "starts": "met"}


@pytest.mark.parametrize("change", [{"at": None}, {"code_sha256": None}, {"running_implementers": True}])
def test_malformed_implement_start_fails_without_losing_other_counts(evidence, change):
    evidence.starts(running=4)
    evidence.tasks["unfinished"]["history"].append(dict(phase="implement-start", at=AFTER,
        code_sha256=SHA, running_implementers=4) | change)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and report["start_count"] == 10


def test_short_start_side_and_plan_independence_stay_pending(evidence):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.starts(count=9, running=4)
    evidence.tasks["never_planned"] = dict(slices=["absent"], history=[])
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["verdicts"] == {"plans": "pending", "starts": "pending"}
    assert report["plan_count"] == 10 and report["start_count"] == 9


def test_pass_without_start_is_absent_beside_ten_qualifiers(evidence):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.tasks["legacy"] = dict(slices=["absent"], planned_at=AFTER,
        history=[dict(phase="plan-review", attempt=1, verdict="PASS")])
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 10
    assert "legacy" not in [plan["id"] for plan in report["plans"]]


def test_later_planned_at_cannot_qualify_earlier_start(evidence):
    task = evidence.plan("early", at=CUTOFF)
    task.update(planned_at=AFTER, slices=["absent"])
    code, report = evidence.run()
    assert code == 2 and report["error"] is None and report["plan_count"] == 0


def test_fail_then_pass_counts_once(evidence):
    task = evidence.plan("retry")
    task["history"].insert(1, dict(phase="plan-review", attempt=1, verdict="FAIL"))
    code, report = evidence.run()
    assert code == 2 and report["error"] is None
    assert report["plan_count"] == 1 and report["plans"][0]["id"] == "retry"


@pytest.mark.parametrize("attempts", [[2], [1, 1]])
def test_ambiguous_attempt_keeps_other_qualifiers(evidence, attempts):
    evidence.plan("qualifier")
    evidence.tasks["ambiguous-plan"] = dict(slices=["absent"], history=[
        dict(phase="plan-start", attempt=attempt, at=AFTER, code_sha256=SHA) for attempt in attempts
    ] + [dict(phase="plan-review", attempt=1, verdict="PASS")])
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "ambiguous" in report["error"] and "ambiguous-plan" in report["error"]
    assert report["plan_count"] == 1 and report["plans"][0]["id"] == "qualifier"


def test_ten_fail_only_plans_cannot_succeed(evidence):
    for i in range(10):
        task = evidence.plan(f"failed{i}")
        task["history"][-1]["verdict"] = "FAIL"
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and report["plan_count"] == 0
    assert "missing" in report["error"]
    for i in range(10):
        assert f"failed{i}" in report["error"]


def test_last_review_fail_overrides_earlier_pass(evidence):
    task = evidence.plan("failed-final")
    task["history"].append(dict(phase="plan-review", attempt=1, verdict="FAIL"))
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and report["plan_count"] == 0
    assert "missing" in report["error"] and "failed-final" in report["error"]


def test_wrong_hash_excluded_without_error(evidence):
    task = evidence.plan("wrong", sha="b" * 64)
    task["slices"] = ["absent"]
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending"
    assert report["plans"] == [] and report["plan_count"] == 0 and report["error"] is None


@pytest.mark.parametrize("task", [None, [], {"history": None}, {"history": {}}, {"history": [None]}])
def test_malformed_task_history_fails_and_keeps_qualifiers(evidence, task):
    evidence.plan("qualifier")
    evidence.tasks["bad"] = task
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"] and "bad" in report["error"]
    assert report["plan_count"] == 1


@pytest.mark.parametrize("change", [{"at": None}, {"at": "yesterday"},
    {"at": "2026-10-01T00:00:01+00:00"}, {"code_sha256": None}, {"code_sha256": 123}])
def test_malformed_plan_start_fails_and_keeps_qualifiers(evidence, change):
    evidence.plan("qualifier")
    task = evidence.plan("bad")
    task["history"][0].update(change)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"] and "bad" in report["error"]
    assert report["plan_count"] == 1


def test_missing_history_and_non_promoted_tasks_are_excluded(evidence):
    evidence.tasks.update(missing=dict(slices=["absent"]), empty=dict(slices=[], history=[
        dict(phase="plan-start")]), non_list=dict(slices="absent", history=[dict(phase="plan-start")]))
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 0
