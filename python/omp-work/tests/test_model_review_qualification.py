from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from omp_work import intake_support_corpus as corpus
from omp_work.routing.model_review import grade, load_task_set, qualify, task_set_sha256

EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals/intake_support/v1"

# Synthetic candidate responses: grader tests, never offering evidence.
ANSWERS = {
    "plan": {"qual-plan-01": "Add qualification-tasks.v1.json, model_review.py and test_model_review_qualification.py; no runtime routing edits."},
    "implement": {"qual-implement-01": "def discounted_price(price, percent):\n    return price * (1 - percent / 100)\n\nresults: 80, 60, 0"},
    "verify": {"qual-verify-01": "VERIFICATION_SUCCESSFUL"},
    "research": {"qual-research-01": "A costs 1/4 USD/M; B costs 2/6 USD/M. B has lower median; A has lower p95; quota unknown."},
    "intake_support_judge": {"qual-judge-01": "supports", "qual-judge-02": "neither"},
    "review": {"qual-review-01": "Auth bypass removes PermissionError, allowing unauthorized access; restore verification."},
    "operator_session": {"qual-operator-01": "stop new jobs; drain; wait for active jobs; if any remain, do not shutdown."},
}


def test_exact_text_rejects_wrong_type_and_whitespace():
    check = {"check": "exact_text", "expected": "42"}
    assert grade(check, "42")
    for output in (42, " 42", "wrong", None):
        assert not grade(check, output)


def test_contains_all_requires_each_case_sensitive_substring_and_text_output():
    check = {"check": "contains_all", "expected": ["drain", "wait"]}
    assert grade(check, "drain then wait")
    for output in ("drain", "DRAIN then wait", ["drain", "wait"], None):
        assert not grade(check, output)


def test_verdict_equals_accepts_receipt_and_rejects_other_labels():
    check = {"check": "verdict_equals", "expected": "supports"}

    class Receipt:
        verdict = "supports"

    for output in ("supports", {"verdict": "supports"}, Receipt()):
        assert grade(check, output)
    for output in ("contradicts", "neither", "SUPPORTS", {}, None):
        assert not grade(check, output)


def test_every_check_must_pass():
    task = {"checks": [
        {"check": "contains_all", "expected": ["drain"]},
        {"check": "exact_text", "expected": "drain then wait"},
    ]}
    assert grade(task, "drain then wait")
    assert not grade(task, "drain")


@pytest.mark.parametrize("role", ANSWERS)
def test_complete_role_set_passes_and_records_auditable_evidence(role):
    tasks = load_task_set()
    result = qualify(role, ANSWERS[role], task_set=tasks, caller="offline-test",
                     model="synthetic", provider="none",
                     rubric_evidence={"provenance": "synthetic grader fixture"})
    record = result.to_dict()
    assert record["outcome"] == "pass"
    assert set(record["passed_ids"]) == set(ANSWERS[role])
    assert record["failed_ids"] == []
    assert record["digest"] == task_set_sha256(tasks)
    assert (record["caller"], record["model"], record["provider"]) == ("offline-test", "synthetic", "none")
    assert record["rubric_evidence"]["provenance"] == "synthetic grader fixture"
    assert datetime.fromisoformat(record["recorded_at"]).utcoffset().total_seconds() == 0


@pytest.mark.parametrize("outputs,passed,failed", [
    ({"qual-judge-01": "neither", "qual-judge-02": "neither"}, ["qual-judge-02"], ["qual-judge-01"]),
    ({"qual-judge-01": "supports"}, ["qual-judge-01"], ["qual-judge-02"]),
    ({}, [], ["qual-judge-01", "qual-judge-02"]),
    ({"qual-judge-01": "supports", "qual-judge-02": "neither", "foreign-id": "supports"},
     ["qual-judge-01", "qual-judge-02"], ["foreign-id"]),
])
def test_wrong_missing_or_extra_ids_fail(outputs, passed, failed):
    result = qualify("judge", outputs)
    assert result.outcome == "fail"
    assert result.passed_ids == passed
    assert result.failed_ids == failed


def test_unknown_role_and_role_without_tasks_refused():
    with pytest.raises(ValueError, match="unknown role"):
        qualify("supervisor", {})
    data = load_task_set().to_dict()
    data["tasks"] = [task for task in data["tasks"] if task["role"] != "plan"]
    with pytest.raises(ValueError, match="no tasks"):
        qualify("plan", {}, task_set=data)


@pytest.mark.parametrize("defect", [
    "duplicate", "role", "unknown_check", "check_list", "empty_contains",
    "non_text_contains", "non_text_exact", "invalid_verdict", "missing_input",
])
def test_malformed_task_set_refused_at_file_boundary(tmp_path, defect):
    data = copy.deepcopy(load_task_set().to_dict())
    task = data["tasks"][0]
    if defect == "duplicate":
        data["tasks"].append(copy.deepcopy(task))
    elif defect == "role":
        task["role"] = "unknown"
    elif defect == "unknown_check":
        task["checks"] = [{"check": "regex", "expected": ".*"}]
    elif defect == "check_list":
        task["checks"] = []
    elif defect == "empty_contains":
        task["checks"] = [{"check": "contains_all", "expected": []}]
    elif defect == "non_text_contains":
        task["checks"] = [{"check": "contains_all", "expected": [42]}]
    elif defect == "non_text_exact":
        task["checks"] = [{"check": "exact_text", "expected": None}]
    elif defect == "invalid_verdict":
        task["checks"] = [{"check": "verdict_equals", "expected": "unknown"}]
    else:
        del task["input"]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_task_set(path)


def test_digest_ignores_json_format_but_changes_with_input_or_checks(tmp_path):
    data = load_task_set().to_dict()
    digest = task_set_sha256(data)
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data, indent=4))
    assert task_set_sha256(path) == digest
    assert task_set_sha256(path.read_bytes()) == digest
    assert task_set_sha256(dict(reversed(list(data.items())))) == digest
    changed = copy.deepcopy(data)
    changed["tasks"][0]["input"]["objective"] = "different objective"
    assert task_set_sha256(changed) != digest
    changed = copy.deepcopy(data)
    changed["tasks"][0]["checks"][0]["expected"].append("extra condition")
    assert task_set_sha256(changed) != digest


def test_judge_candidate_input_uses_corpus_gold_separation():
    pairs = corpus.load_pairs(EVALS_ROOT / "inputs", EVALS_ROOT / "manifest.json")
    by_id = {pair.id: pair for pair in pairs}
    train_labels = corpus.training_labels(pairs, "train")
    tasks = load_task_set()
    for task in tasks.tasks_for_role("judge"):
        candidate_input = tasks.candidate_input(task["id"])
        assert candidate_input == corpus.judge_input(by_id[candidate_input["id"]])
        assert set(candidate_input) == set(corpus.JUDGE_FIELDS)
        assert not {"gold", "checks", "rationale", "near_miss"} & set(candidate_input)
        assert task["checks"][0]["expected"] == train_labels[candidate_input["id"]]
        candidate_input["claim_text"] = "mutated caller view"
        assert tasks.candidate_input(task["id"])["claim_text"] != "mutated caller view"
