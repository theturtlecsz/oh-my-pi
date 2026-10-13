from __future__ import annotations

import copy
from pathlib import Path
import pytest

from omp_work import intake_support_corpus as corpus
from omp_work.routing.model_review import (
    CLOSED_CHECKS,
    KNOWN_ROLES,
    TaskSet,
    grade,
    load_task_set,
    qualify,
    task_set_sha256,
)

EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals/intake_support/v1"
INPUTS, MANIFEST = EVALS_ROOT / "inputs", EVALS_ROOT / "manifest.json"


# ---------------------------------------------------------------------------
# Distinct check branches
# ---------------------------------------------------------------------------


def test_check_branch_exact_text():
    check = {"check": "exact_text", "expected": "VERIFICATION_SUCCESSFUL"}

    # Exact match passes
    assert grade(check, "VERIFICATION_SUCCESSFUL") is True
    # Stripped match passes
    assert grade(check, "  VERIFICATION_SUCCESSFUL\n") is True
    # Mismatch fails
    assert grade(check, "VERIFICATION_FAILED") is False
    assert grade(check, "") is False
    # Non-string/non-scalar fails
    assert grade(check, None) is False
    assert grade(check, {"text": "VERIFICATION_SUCCESSFUL"}) is False


def test_check_branch_contains_all():
    check = {
        "check": "contains_all",
        "expected": ["qualification-tasks.v1.json", "model_review.py"],
    }

    # All substrings present passes
    assert grade(check, "Added qualification-tasks.v1.json and model_review.py") is True
    # Missing one substring fails
    assert grade(check, "Added qualification-tasks.v1.json only") is False
    # None or non-string fails
    assert grade(check, None) is False
    assert grade(check, 12345) is False


def test_check_branch_verdict_equals():
    check = {"check": "verdict_equals", "expected": "supports"}

    # Exact string match
    assert grade(check, "supports") is True
    # Case-insensitive / whitespace match
    assert grade(check, " SUPPORTS \n") is True
    # Dict containing verdict
    assert grade(check, {"verdict": "supports"}) is True
    assert grade(check, {"verdict": "SUPPORTS"}) is True

    # Dummy receipt object with verdict attribute
    class DummyReceipt:
        def __init__(self, verdict: str):
            self.verdict = verdict

    assert grade(check, DummyReceipt("supports")) is True
    assert grade(check, DummyReceipt("contradicts")) is False

    # Wrong verdict fails
    assert grade(check, "contradicts") is False
    assert grade(check, "neither") is False
    assert grade(check, None) is False


def test_check_branch_unknown_check_rejected():
    with pytest.raises(ValueError, match="unknown check type"):
        grade({"check": "regex_match", "expected": ".*"}, "output")


# ---------------------------------------------------------------------------
# Packaged task set and corpus API gold exclusion
# ---------------------------------------------------------------------------


def test_packaged_task_set_valid():
    ts = load_task_set()
    assert isinstance(ts, TaskSet)
    assert ts.version == "qualification-tasks.v1"
    assert len(ts) >= 7

    # Ensure all required roles have at least one task
    required_roles = {
        "plan",
        "implement",
        "verify",
        "research",
        "intake_support_judge",
        "review",
        "operator_session",
    }
    for role in required_roles:
        tasks = ts.tasks_for_role(role)
        assert len(tasks) >= 1, f"missing tasks for role: {role}"


def test_judge_input_excludes_gold_using_corpus_api():
    """Verify judge tasks reuse intake_support_corpus gold separation and never contain gold."""
    pairs = corpus.load_pairs(INPUTS, MANIFEST)
    pairs_by_id = {p.id: p for p in pairs}

    ts = load_task_set()
    judge_tasks = ts.tasks_for_role("intake_support_judge")
    assert len(judge_tasks) >= 1

    for task in judge_tasks:
        task_input = task["input"]
        # Must only contain allowed judge fields from corpus API
        assert set(task_input.keys()) <= set(corpus.JUDGE_FIELDS)

        # Must strictly exclude gold, rationale, and near_miss
        assert "gold" not in task_input
        assert "rationale" not in task_input
        assert "near_miss" not in task_input

        # Check against corpus.judge_input(pair)
        pair_id = task_input["id"]
        assert pair_id in pairs_by_id
        corpus_input = corpus.judge_input(pairs_by_id[pair_id])

        assert task_input == corpus_input
        assert "gold" not in corpus_input


# ---------------------------------------------------------------------------
# Wrong, missing, and extra output failure paths
# ---------------------------------------------------------------------------


def test_wrong_output_fails_qualification():
    ts = load_task_set()
    outputs = {"qual-plan-01": "Wrong plan with missing deliverables"}

    res = qualify("plan", outputs, task_set=ts)
    assert res.outcome == "fail"
    assert not res.passed
    assert "qual-plan-01" in res.failed_ids
    assert res.passed_ids == []


def test_missing_output_fails_qualification():
    ts = load_task_set()
    # Missing all tasks for the role
    res = qualify("plan", {}, task_set=ts)
    assert res.outcome == "fail"
    assert not res.passed
    assert "qual-plan-01" in res.failed_ids

    # For a role with multiple tasks (judge has qual-judge-01 and qual-judge-02),
    # providing only one task must fail
    judge_tasks = ts.tasks_for_role("intake_support_judge")
    assert len(judge_tasks) >= 2
    partial_outputs = {judge_tasks[0]["id"]: "supports"}
    res_judge = qualify("intake_support_judge", partial_outputs, task_set=ts)
    assert res_judge.outcome == "fail"
    assert judge_tasks[1]["id"] in res_judge.failed_ids


def test_extra_output_id_fails_qualification():
    ts = load_task_set()
    outputs = {
        "qual-plan-01": "Includes qualification-tasks.v1.json and model_review.py",
        "unexpected-extra-task-id": "extraneous output",
    }
    res = qualify("plan", outputs, task_set=ts)
    assert res.outcome == "fail"
    assert "unexpected-extra-task-id" in res.failed_ids


def test_unknown_role_raises_value_error():
    with pytest.raises(ValueError, match="unknown role"):
        qualify("non_existent_role", {})


# ---------------------------------------------------------------------------
# Complete correct set passes
# ---------------------------------------------------------------------------


def test_complete_correct_set_passes():
    ts = load_task_set()

    correct_answers_by_role = {
        "plan": {
            "qual-plan-01": "Deliverables include qualification-tasks.v1.json and model_review.py implementation.",
        },
        "implement": {
            "qual-implement-01": "Use check.get('expected') and return False if output is None.",
        },
        "verify": {
            "qual-verify-01": "VERIFICATION_SUCCESSFUL",
        },
        "research": {
            "qual-research-01": "Provider comparison of USD/M input/output and median/p95 latency metrics.",
        },
        "intake_support_judge": {
            "qual-judge-01": "supports",
            "qual-judge-02": "neither",
        },
        "review": {
            "qual-review-01": "Identified security defect: bypass of PermissionError check.",
        },
        "operator_session": {
            "qual-operator-01": "Initiate node drain before shutdown sequence.",
        },
    }

    digest = task_set_sha256(ts)

    for role, outputs in correct_answers_by_role.items():
        res = qualify(
            role,
            outputs,
            task_set=ts,
            caller="orchestrator",
            model="test-model-v1",
            provider="test-provider",
            rubric_evidence={"capability": "high", "cost_usd_m_input": 0.50},
        )
        assert res.outcome == "pass"
        assert res.passed is True
        assert res.failed_ids == []
        assert set(res.passed_ids) == set(outputs.keys())
        assert res.digest == digest
        assert res.caller == "orchestrator"
        assert res.model == "test-model-v1"
        assert res.provider == "test-provider"
        assert res.rubric_evidence["capability"] == "high"

        # Verify tuple unpacking
        unpacked_digest, unpacked_passed, unpacked_failed, unpacked_outcome = res
        assert unpacked_digest == digest
        assert unpacked_outcome == "pass"

        # Verify dict export
        record = res.to_dict()
        assert record["outcome"] == "pass"
        assert record["digest"] == digest
        assert record["recorded_at"] is not None


def test_role_aliases():
    """Verify judge/intake_support_judge and operator/operator_session aliases work interchangeably."""
    ts = load_task_set()

    judge_outputs = {
        "qual-judge-01": "supports",
        "qual-judge-02": "neither",
    }
    res_judge = qualify("judge", judge_outputs, task_set=ts)
    res_intake = qualify("intake_support_judge", judge_outputs, task_set=ts)
    assert res_judge.outcome == "pass"
    assert res_intake.outcome == "pass"

    op_outputs = {
        "qual-operator-01": "drain then shutdown",
    }
    res_op = qualify("operator", op_outputs, task_set=ts)
    res_session = qualify("operator_session", op_outputs, task_set=ts)
    assert res_op.outcome == "pass"
    assert res_session.outcome == "pass"


# ---------------------------------------------------------------------------
# Malformed task set refused at file boundary
# ---------------------------------------------------------------------------


def test_malformed_task_set_duplicate_ids_refused():
    data = {
        "version": "qualification-tasks.v1",
        "tasks": [
            {
                "id": "duplicate-id",
                "role": "plan",
                "input": {},
                "checks": [{"check": "exact_text", "expected": "A"}],
            },
            {
                "id": "duplicate-id",
                "role": "plan",
                "input": {},
                "checks": [{"check": "exact_text", "expected": "B"}],
            },
        ],
    }
    with pytest.raises(ValueError, match="duplicate task id"):
        TaskSet._validate = None  # ensure clean
        from omp_work.routing.model_review import _validate_task_set_dict
        _validate_task_set_dict(data)


def test_malformed_task_set_unknown_role_refused():
    data = {
        "version": "qualification-tasks.v1",
        "tasks": [
            {
                "id": "task-01",
                "role": "invalid_unknown_role",
                "input": {},
                "checks": [{"check": "exact_text", "expected": "A"}],
            },
        ],
    }
    from omp_work.routing.model_review import _validate_task_set_dict
    with pytest.raises(ValueError, match="unknown role"):
        _validate_task_set_dict(data)


def test_malformed_task_set_unknown_check_refused():
    data = {
        "version": "qualification-tasks.v1",
        "tasks": [
            {
                "id": "task-01",
                "role": "plan",
                "input": {},
                "checks": [{"check": "unknown_unsupported_check", "expected": "A"}],
            },
        ],
    }
    from omp_work.routing.model_review import _validate_task_set_dict
    with pytest.raises(ValueError, match="unknown check type"):
        _validate_task_set_dict(data)


def test_malformed_task_set_missing_fields_refused():
    from omp_work.routing.model_review import _validate_task_set_dict

    with pytest.raises(ValueError, match="JSON object"):
        _validate_task_set_dict(["not", "a", "dict"])

    with pytest.raises(ValueError, match="version"):
        _validate_task_set_dict({"tasks": []})

    with pytest.raises(ValueError, match="empty"):
        _validate_task_set_dict({"version": "v1", "tasks": []})


# ---------------------------------------------------------------------------
# Changed input changes digest
# ---------------------------------------------------------------------------


def test_changed_input_changes_digest():
    raw = copy.deepcopy(load_task_set().to_dict())
    original_digest = task_set_sha256(raw)

    # Mutate an input field
    raw["tasks"][0]["input"]["objective"] = "Mutated objective that must alter digest"
    mutated_digest = task_set_sha256(raw)

    assert mutated_digest != original_digest
    assert len(mutated_digest) == 64
