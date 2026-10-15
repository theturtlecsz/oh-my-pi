"""OMP-584 report classifies isolated history and canonical slice evidence."""

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
START_METRICS = dict(slots=5, occupied_slots=1, providers={"alpha": {"load": 0, "max_parallel": 2}})
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
        path.write_text("---\ndepends_on: []\n---\nTask body\n")
        slices = [self.slice(f"{name}-s{i}", "[]" if i < roots else "[upstream]") for i in range(3)]
        self.tasks[name] = dict(slices=slices, history=[
            dict(phase="plan-start", attempt=1, at=at, code_sha256=sha),
            dict(phase="plan-review", attempt=1, verdict="pAsS", at=CUTOFF)])
        return self.tasks[name]

    def starts(self, count=10, running=3, **fields):
        metrics = START_METRICS | fields
        self.tasks["unfinished"] = {"history": [dict(**metrics, phase="implement-start", attempt=i, at=AFTER,
            code_sha256=SHA, running_implementers=running) for i in range(count)]}

    def run(self, write_state=True):
        if write_state:
            self.state.write_text(json.dumps({"tasks": self.tasks}))
        before = {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        completed = subprocess.run([sys.executable, str(SCRIPT), "--flood-dir", str(self.directory),
            "--rollout", str(self.rollout), "--output", str(self.output)], capture_output=True, text=True)
        assert "Traceback" not in completed.stderr
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
def test_slice_call_failures_keep_classified_counts(evidence, function):
    (evidence.directory / "flood.py").write_text(STUB + f"\ndef {function}(value): raise RuntimeError('call broke')\n")
    evidence.plan("plan")
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and "call broke" in report["error"]
    assert report["plan_count"] == 1 and report["start_count"] == 10
    assert report["verdicts"] == {"plans": "pending", "starts": "met"}


def test_qualifiers_sorted_once_with_canonical_independence(evidence):
    for i in reversed(range(10)):
        evidence.plan(f"p{i}")
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 0 and report["status"] == "ok" and report["error"] is None
    assert report["plans"] == [dict(id=f"p{i}", attempt=1, code_sha256=SHA,
        slices=3, roots=2, implementer_slices=3, implementer_roots=2) for i in range(10)]
    assert report["plan_count"] == 10 and report["independent_plan_count"] == 10
    assert report["verdicts"] == {"plans": "met", "starts": "met"}


@pytest.mark.parametrize("parked", [".failed", ".dropped"])
def test_missing_canonical_slice_fails_even_when_parked_twin_exists(evidence, parked):
    evidence.plan("qualifier")
    task = evidence.plan("p")
    slice_id = task["slices"][0]
    path = evidence.directory / "tasks" / f"{slice_id}.md"
    path.rename(path.with_name(path.name + parked))
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "missing" in report["error"] and slice_id in report["error"]
    assert report["plan_count"] == 2 and report["independent_plan_count"] == 1
    assert report["verdicts"]["plans"] != "met"


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


def test_two_starts_on_one_running_task_both_count(evidence):
    evidence.tasks["running"] = {"history": [
        dict(**START_METRICS, phase="implement-start", attempt=1, at=AFTER, code_sha256=SHA, running_implementers=3),
        dict(**START_METRICS, phase="implement-start", attempt=2, at=AFTER, code_sha256=SHA, running_implementers=4),
    ]}
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["start_count"] == 2
    assert report["starts"] == [
        dict(id="running", attempt=1, at=AFTER, running_implementers=3, code_sha256=SHA),
        dict(id="running", attempt=2, at=AFTER, running_implementers=4, code_sha256=SHA),
    ]


def test_ten_fours_with_free_slots_and_under_ten_plans_met(evidence):
    evidence.plan("p0")
    evidence.starts(count=10, running=4, slots=5, occupied_slots=1)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 1 and report["verdicts"]["plans"] == "pending"
    assert report["start_count"] == 10 and report["verdicts"]["starts"] == "met"
    assert report["median"] == 4 and report["cap"]["proven"] is False


def test_median_two_with_free_slots_and_spare_provider_unmet(evidence):
    evidence.plan("p0")
    evidence.starts(count=10, running=2, slots=5, occupied_slots=1,
                    providers={"alpha": {"load": 0, "max_parallel": 2}})
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == 1 and report["verdicts"]["plans"] == "pending"
    assert report["start_count"] == 10 and report["verdicts"]["starts"] == "unmet"
    assert report["median"] == 2 and report["cap"]["proven"] is False


def test_string_running_implementers_fails_with_median_null(evidence):
    evidence.starts(count=10, running="4")
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"]
    assert report["median"] is None
    assert evidence.output.is_file()


def test_ten_starts_equal_to_baseline_unmet(evidence):
    evidence.starts(running=3)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["median"] == 3 and report["verdicts"]["starts"] == "unmet"


def test_nine_starts_stay_pending(evidence):
    evidence.starts(count=9, running=4)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["start_count"] == 9
    assert report["verdicts"]["starts"] == "pending"


@pytest.mark.parametrize("fields", [
    {"slots": True},
    {"slots": "2"},
    {"occupied_slots": True},
    {"occupied_slots": "1"},
    {"providers": "not_an_object"},
    {"providers": {"alpha": "not_an_object"}},
    {"providers": {"alpha": {"load": True, "max_parallel": 2}}},
    {"providers": {"alpha": {"load": 1, "max_parallel": True}}},
    {"providers": {"alpha": {"load": "1", "max_parallel": 2}}},
    {"providers": {"alpha": {"load": 1, "max_parallel": "2"}}},
    {"load": True},
    {"max_parallel": True},
])
def test_matched_event_invalid_types_fail_with_malformed_and_median_null(evidence, fields):
    evidence.starts(count=10, running=4, **fields)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"]
    assert report["median"] is None
    assert evidence.output.is_file()


@pytest.mark.parametrize("field", ["running_implementers", "slots", "occupied_slots", "providers",
    "provider.load", "provider.max_parallel"])
def test_missing_matched_metrics_fail_instead_of_certifying_median(evidence, field):
    evidence.starts(running=4)
    event = evidence.tasks["unfinished"]["history"][-1]
    if field.startswith("provider."):
        event["providers"] = {"alpha": {"load": 0, "max_parallel": 2}}
        del event["providers"]["alpha"][field.split(".")[1]]
    else:
        del event[field]
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"] and field.split(".")[-1] in report["error"]
    assert report["median"] is None and report["verdicts"]["starts"] == "pending"
    assert report["start_count"] == 9


@pytest.mark.parametrize("attempt", ["2", True, [], {}])
def test_mixed_attempt_types_write_failure_without_sort_traceback(evidence, attempt):
    evidence.starts(count=2, running=4)
    evidence.tasks["unfinished"]["history"][-1]["attempt"] = attempt
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"] and "attempt" in report["error"]
    assert report["median"] is None
    assert report["starts"] == [dict(id="unfinished", attempt=0, at=AFTER,
        running_implementers=4, code_sha256=SHA)]


def test_starts_cohort_row_schema_and_sorting(evidence):
    evidence.tasks["t2"] = {"history": [
        dict(**START_METRICS, phase="implement-start", attempt=2, at="2026-10-01T00:00:02Z", code_sha256=SHA, running_implementers=3),
        dict(**START_METRICS, phase="implement-start", attempt=1, at="2026-10-01T00:00:02Z", code_sha256=SHA, running_implementers=2),
    ]}
    evidence.tasks["t1"] = {"history": [
        dict(**START_METRICS, phase="implement-start", attempt=1, at="2026-10-01T00:00:01Z", code_sha256=SHA, running_implementers=4),
        dict(**START_METRICS, phase="implement-start", attempt=1, at="2026-10-01T00:00:03Z", code_sha256=SHA, running_implementers=5),
    ]}
    code, report = evidence.run()
    assert code == 2 and report["error"] is None
    assert report["starts"] == [
        {"id": "t1", "attempt": 1, "at": "2026-10-01T00:00:01Z", "running_implementers": 4, "code_sha256": SHA},
        {"id": "t2", "attempt": 1, "at": "2026-10-01T00:00:02Z", "running_implementers": 2, "code_sha256": SHA},
        {"id": "t2", "attempt": 2, "at": "2026-10-01T00:00:02Z", "running_implementers": 3, "code_sha256": SHA},
        {"id": "t1", "attempt": 1, "at": "2026-10-01T00:00:03Z", "running_implementers": 5, "code_sha256": SHA},
    ]
    assert report["start_count"] == 4



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
    evidence.tasks["unfinished"]["history"].append(dict(**START_METRICS, phase="implement-start", at=AFTER,
        code_sha256=SHA, running_implementers=4) | change)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and report["start_count"] == 10


def test_short_start_side_stays_pending_with_independent_plans(evidence):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.starts(count=9, running=4)
    evidence.tasks["never_planned"] = dict(slices=["absent"], history=[])
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["verdicts"] == {"plans": "met", "starts": "pending"}
    assert report["plan_count"] == 10 and report["start_count"] == 9


def test_pass_without_start_is_absent_beside_ten_qualifiers(evidence):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.tasks["legacy"] = dict(slices=["absent"], planned_at=AFTER,
        history=[dict(phase="plan-review", attempt=1, verdict="PASS")])
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 0 and report["status"] == "ok" and report["error"] is None
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


@pytest.mark.parametrize("depends", [None, ""])
def test_missing_or_blank_dependencies_are_not_roots(evidence, depends):
    for i in range(10):
        task = evidence.plan(f"p{i}")
        task["slices"] = [evidence.slice(f"p{i}-s{j}", depends=depends) for j in range(2)]
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["independent_plan_count"] == 0 and report["verdicts"]["plans"] == "unmet"
    assert all(plan["roots"] == plan["implementer_roots"] == 0 for plan in report["plans"])


@pytest.mark.parametrize("owners,counts", [
    (["true", "false", "false"], (2, 2, 1)),
    (["true", "true", "false"], (2, 1, 0)),
])
def test_owner_roots_count_toward_independence_only(evidence, owners, counts):
    task = evidence.plan("p")
    task["slices"] = [evidence.slice(f"p-s{i}", depends="[]" if i < 2 else "[upstream]", owner=owner)
                      for i, owner in enumerate(owners)]
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    plan = report["plans"][0]
    assert plan["slices"] == 3
    assert (plan["roots"], plan["implementer_slices"], plan["implementer_roots"]) == counts
    assert report["independent_plan_count"] == 1


@pytest.mark.parametrize("count,independent,verdict", [(9, 9, "pending"), (10, 5, "unmet"), (10, 6, "met")])
def test_plan_verdict_requires_ten_plans_and_strict_majority(evidence, count, independent, verdict):
    for i in range(count):
        evidence.plan(f"p{i}", roots=2 if i < independent else 1)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert report["plan_count"] == count and report["independent_plan_count"] == independent
    assert report["verdicts"] == {"plans": verdict, "starts": "pending"}


@pytest.mark.parametrize("slices", [["same", "same"], [""], ["  "], [None]])
def test_malformed_slice_ids_fail_without_losing_other_qualifiers(evidence, slices):
    evidence.plan("qualifier")
    evidence.plan("bad")["slices"] = slices
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure"
    assert "malformed" in report["error"] and "bad" in report["error"]
    assert report["plan_count"] == 2 and report["independent_plan_count"] == 1
    assert next(plan for plan in report["plans"] if plan["id"] == "bad")["slices"] == len(slices)


def test_canonical_files_ignore_parked_twins(evidence):
    evidence.plan("p")
    for slice_id in evidence.tasks["p"]["slices"]:
        (evidence.directory / "tasks" / f"{slice_id}.md.failed").write_text("not front matter")
        (evidence.directory / "tasks" / f"{slice_id}.md.dropped").write_text("not front matter")
    code, report = evidence.run()
    assert code == 2 and report["error"] is None
    assert report["plans"][0]["roots"] == 2 and report["independent_plan_count"] == 1


@pytest.mark.parametrize("providers", [
    {"alpha": {"load": 0, "max_parallel": 2}},
    {"alpha": {"load": 2, "max_parallel": 2}},
])
def test_full_uniform_slots_prove_cap_and_take_precedence_over_providers(evidence, providers):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.starts(running=2, slots=2, occupied_slots=2, providers=providers)
    code, report = evidence.run()
    assert code == 0 and report["status"] == "ok"
    assert report["verdicts"] == {"plans": "met", "starts": "met"}
    assert report["cap"] == dict(proven=True, kind="slots", value=2, evidence=[
        dict(id="unfinished", attempt=i, slots=2, occupied_slots=2, providers=providers) for i in range(10)])


def test_all_uniform_providers_full_prove_cap_with_free_slots(evidence):
    providers = {"alpha": {"load": 2, "max_parallel": 2}, "beta": {"load": 11, "max_parallel": 10}}
    evidence.starts(running=3, providers=providers)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending"
    assert report["verdicts"]["starts"] == "met"
    assert report["cap"] == dict(proven=True, kind="providers", value={"alpha": 2, "beta": 10}, evidence=[
        dict(id="unfinished", attempt=i, **(START_METRICS | {"providers": providers})) for i in range(10)])


@pytest.mark.parametrize("fields,running", [
    ({"providers": {"alpha": {"load": 2, "max_parallel": 2}, "beta": {"load": 0, "max_parallel": 10}}}, 2),
    ({"slots": 8, "occupied_slots": 2}, 2),
    ({}, 3),
])
def test_spare_capacity_cannot_excuse_baseline_even_with_config_cap(evidence, fields, running):
    for i in range(10):
        evidence.plan(f"p{i}")
    (evidence.directory / "flood.json").write_text('{"slots": 1}')
    evidence.starts(running=running, **fields)
    code, report = evidence.run()
    assert code == 1 and report["status"] == "unmet" and report["error"] is None
    assert report["median"] == running and report["cap"]["proven"] is False
    assert report["verdicts"] == {"plans": "met", "starts": "unmet"}


@pytest.mark.parametrize("fields,change", [
    ({"slots": 2, "occupied_slots": 3}, {"slots": 3}),
    ({"slots": 2, "occupied_slots": 2}, {"occupied_slots": 1}),
    ({"slots": 0, "occupied_slots": 0, "providers": {}}, {}),
    ({"providers": {"": {"load": 2, "max_parallel": 2}}}, {}),
    ({"providers": {"alpha": {"load": 2, "max_parallel": 2}}},
     {"providers": {"beta": {"load": 2, "max_parallel": 2}}}),
    ({"providers": {"alpha": {"load": 2, "max_parallel": 2}}},
     {"providers": {"alpha": {"load": 1, "max_parallel": 2}}}),
])
def test_cap_requires_uniform_capacity_and_every_event_saturated(evidence, fields, change):
    evidence.starts(running=2, **fields)
    evidence.tasks["unfinished"]["history"][-1].update(change)
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending"
    assert report["cap"]["proven"] is False and report["cap"]["kind"] is None
    assert report["cap"]["value"] is None and len(report["cap"]["evidence"]) == 10
    assert report["verdicts"]["starts"] == "unmet"


def test_half_independent_plans_with_ten_good_starts_is_unmet(evidence):
    for i in range(10):
        evidence.plan(f"p{i}", roots=2 if i < 5 else 1)
    evidence.starts(running=4)
    code, report = evidence.run()
    assert code == 1 and report["status"] == "unmet"
    assert report["plan_count"] == 10 and report["independent_plan_count"] == 5
    assert report["verdicts"] == {"plans": "unmet", "starts": "met"}


def test_hard_error_overrides_met_verdicts_without_losing_counts(evidence):
    for i in range(10):
        evidence.plan(f"p{i}")
    evidence.starts(running=4)
    evidence.tasks["broken"] = {"history": None}
    code, report = evidence.run()
    assert code == 2 and report["status"] == "failure" and "malformed" in report["error"]
    assert report["plan_count"] == report["start_count"] == 10
    assert report["verdicts"] == {"plans": "met", "starts": "met"}


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_AUTHOR_NAME="omp", GIT_AUTHOR_EMAIL="omp@example.com",
               GIT_COMMITTER_NAME="omp", GIT_COMMITTER_EMAIL="omp@example.com")
    completed = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                               check=True, capture_output=True, text=True, env=env)
    return completed.stdout.strip()


def test_later_flood_py_with_omp584_region_counts_and_revert_is_excluded(tmp_path):
    evidence = Evidence(tmp_path)
    repo = evidence.directory
    base = (repo / "flood.py").read_bytes()
    if not base.endswith(b"\n"):
        base += b"\n"
    region = b"PLANNER_REGION_OMP584 = 'omp584-region-marker'\n"
    unrelated = b"# unrelated flood.py edit\n"
    _git(repo, "init", "-b", "main")
    (repo / "flood.py").write_bytes(base)
    _git(repo, "add", "flood.py")
    _git(repo, "commit", "-m", "base")
    (repo / "flood.py").write_bytes(base + region)
    _git(repo, "add", "flood.py")
    _git(repo, "commit", "-m", "activate omp584")
    activated_commit = _git(repo, "rev-parse", "HEAD")
    activated = hashlib.sha256((repo / "flood.py").read_bytes()).hexdigest()
    (repo / "flood.py").write_bytes(base + region + unrelated)
    _git(repo, "add", "flood.py")
    _git(repo, "commit", "-m", "unrelated")
    later = hashlib.sha256((repo / "flood.py").read_bytes()).hexdigest()
    (repo / "flood.py").write_bytes(base + unrelated)
    _git(repo, "add", "flood.py")
    _git(repo, "commit", "-m", "revert omp584")
    reverted = hashlib.sha256((repo / "flood.py").read_bytes()).hexdigest()
    # The report imports the on-disk flood.py. Put the unrelated edit back so import still sees the region file.
    (repo / "flood.py").write_bytes(base + region + unrelated)
    assert later != activated and reverted not in (activated, later)
    evidence.rollout.write_text(json.dumps(dict(
        activated_at=CUTOFF, flood_sha256=activated, repo_commit="echo-me", external_commit=activated_commit)))
    for name, sha in (("kept", later), ("reverted", reverted)):
        slices = [evidence.slice(f"{name}-s{i}", "[]" if i < 2 else "[upstream]") for i in range(3)]
        evidence.tasks[name] = dict(slices=slices, history=[
            dict(phase="plan-start", attempt=1, at=AFTER, code_sha256=sha),
            dict(phase="plan-review", attempt=1, verdict="PASS")])
    evidence.tasks["runs"] = {"history": [
        dict(**START_METRICS, phase="implement-start", attempt=1, at=AFTER, code_sha256=later, running_implementers=4),
        dict(**START_METRICS, phase="implement-start", attempt=2, at=AFTER, code_sha256=reverted, running_implementers=9),
        dict(**START_METRICS, phase="implement-start", attempt=3, at=AFTER, code_sha256=activated, running_implementers=5),
    ]}
    evidence.state.write_text(json.dumps({"tasks": evidence.tasks}))
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    completed = subprocess.run([sys.executable, str(SCRIPT), "--flood-dir", str(repo),
        "--rollout", str(evidence.rollout), "--output", str(evidence.output)], capture_output=True, text=True)
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert "Traceback" not in completed.stderr
    assert after == before
    report = json.loads(evidence.output.read_text())
    assert completed.returncode == 2 and report["status"] == "pending" and report["error"] is None
    assert [plan["id"] for plan in report["plans"]] == ["kept"]
    assert report["plans"][0]["code_sha256"] == later
    assert {(row["attempt"], row["code_sha256"], row["running_implementers"]) for row in report["starts"]} == {
        (1, later, 4), (3, activated, 5)}
    assert report["start_count"] == 2 and report["median"] == 4.5


def test_repeated_plan_attempt_uses_last_start_before_review(evidence):
    evidence.plan("qualifier")
    slices = [evidence.slice(f"rerun-s{i}", "[]" if i < 2 else "[upstream]") for i in range(3)]
    evidence.tasks["rerun"] = dict(slices=slices, history=[
        dict(phase="plan-start", attempt=1, at="2026-10-01T00:00:01Z", code_sha256="b" * 64),
        dict(phase="plan-start", attempt=1, at="2026-10-01T00:00:02Z", code_sha256=SHA),
        dict(phase="plan-review", attempt=1, verdict="PASS", at="2026-10-01T00:00:03Z"),
        dict(phase="plan-start", attempt=1, at="2026-10-01T00:00:04Z", code_sha256="c" * 64),
    ])
    code, report = evidence.run()
    assert code == 2 and report["status"] == "pending" and report["error"] is None
    assert [plan["id"] for plan in report["plans"]] == ["qualifier", "rerun"]
    rerun = report["plans"][1]
    assert rerun["attempt"] == 1 and rerun["code_sha256"] == SHA and rerun["roots"] == 2
