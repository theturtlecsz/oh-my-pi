"""validate can grade from verify --out records when evidence has no independent-tests.json."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from omp_harbor_eval import EvidenceWriter, load_fixture, validate
from omp_harbor_eval.grader import SEEDED_LABEL

READBACK = "service-readback.json"
TRANSCRIPT = "transcript.jsonl"
OUTCOME = "outcome.json"
TESTS = "independent-tests.json"
TARGET = "tests/test_amend.py"
MISSING = f"missing file: {TESTS}"
FAILED = f"independent_tests_pass: pytest {TARGET} failed"


def _fixture(root: Path):
    directory = root / "f1"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f1",
        "scored_experiment": "structured-amendment",
        "seed_patch": "--- seed\n",
        "solution_patch": "--- solution\n",
        "independent_tests": [{"runner": "pytest", "target": TARGET}],
        "scenario": "scenario.json",
        "rules": [
            {"type": "readback_equals", "pointer": "/workflow/status", "value": "completed"},
            {"type": "transcript_count", "count": 2},
            {"type": "outcome_is", "outcome": "completed"},
            {"type": "independent_tests_pass"},
        ],
    }
    scenario = {
        "command": "execute",
        "terminal": {"pointer": "/workflow/status", "in": ["completed", "failed"]},
        "model_script": [{"role": "assistant", "text": "done"}],
        "ui_script": [],
        "kill_at": None,
        "timeout_s": 30,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def _evidence(directory: Path, fixture, *, variant: str, run_id: str, nonce: str, passed: bool | None) -> None:
    writer = EvidenceWriter(
        directory,
        run_id,
        nonce,
        fixture.id,
        fixture.digest,
        variant,
        fixture.scored_experiment,
    )
    writer.write_json(READBACK, {"workflow": {"status": "completed"}})
    writer.write_json(OUTCOME, {"outcome": "completed"})
    writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": 0})
    writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": 1})
    if passed is not None:
        writer.write_json(TESTS, [{"runner": "pytest", "target": TARGET, "passed": passed}])
    writer.seal()


def _pair(tmp_path: Path, *, include_tests: bool):
    fixture = _fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    _evidence(
        good,
        fixture,
        variant="known_good",
        run_id="run-good",
        nonce="nonce-good",
        passed=True if include_tests else None,
    )
    _evidence(
        bad,
        fixture,
        variant="known_bad",
        run_id="run-bad",
        nonce="nonce-bad",
        passed=False if include_tests else None,
    )
    return fixture, good, bad


def _verify_out(path: Path, *, passed: bool) -> None:
    record: dict[str, object] = {
        "runner": "pytest",
        "target": TARGET,
        "exit_code": 0 if passed else 1,
        "passed": passed,
    }
    if not passed:
        record["reason"] = "exit code 1"
    # The embedded grade is the opposite of the records. validate must read tests.
    document = {
        "tests": [record],
        "grade": {"status": "fail" if passed else "pass", "label": "ignored", "reasons": ["stale"]},
    }
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")


def _cli(fixture_dir: Path, good: Path, bad: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.grader",
            "validate",
            "--fixture",
            str(fixture_dir),
            "--good",
            str(good),
            "--bad",
            str(bad),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_validate_grades_from_verify_records_when_evidence_has_no_independent_tests(tmp_path: Path) -> None:
    fixture, good, bad = _pair(tmp_path, include_tests=False)
    assert TESTS not in {path.name for path in good.iterdir()}
    assert TESTS not in {path.name for path in bad.iterdir()}

    good_records = [{"runner": "pytest", "target": TARGET, "exit_code": 0, "passed": True}]
    bad_records = [{"runner": "pytest", "target": TARGET, "exit_code": 1, "passed": False, "reason": "exit code 1"}]
    graded = validate(fixture, good, bad, good_tests=good_records, bad_tests=bad_records)
    assert graded["ok"] is True
    assert graded["verdicts"] == {
        "known_good": "pass",
        "known_bad": "fail",
        "tampered": "invalid_evidence",
        "foreign": "invalid_evidence",
    }
    assert graded["grades"]["known_bad"]["label"] == SEEDED_LABEL
    assert graded["grades"]["known_bad"]["reasons"] == [FAILED]

    good_out = tmp_path / "good-verify.json"
    bad_out = tmp_path / "bad-verify.json"
    _verify_out(good_out, passed=True)
    _verify_out(bad_out, passed=False)
    checked = _cli(fixture.directory, good, bad, "--good-tests", str(good_out), "--bad-tests", str(bad_out))
    assert checked.returncode == 0, checked.stderr
    body = json.loads(checked.stdout)
    assert body["ok"] is True
    assert body["verdicts"]["known_good"] == "pass"
    assert body["verdicts"]["known_bad"] == "fail"
    assert body["grades"]["known_bad"]["reasons"] == [FAILED]
    assert body["grades"]["known_bad"]["label"] == SEEDED_LABEL


def test_validate_without_records_reads_independent_tests_json(tmp_path: Path) -> None:
    fixture, good, bad = _pair(tmp_path, include_tests=True)
    graded = validate(fixture, good, bad)
    assert graded["ok"] is True
    assert graded["grades"]["known_good"]["status"] == "pass"
    assert graded["grades"]["known_bad"]["reasons"] == [FAILED]

    checked = _cli(fixture.directory, good, bad)
    assert checked.returncode == 0, checked.stderr
    body = json.loads(checked.stdout)
    assert body["ok"] is True
    assert body["verdicts"]["known_bad"] == "fail"
    assert body["grades"]["known_bad"]["reasons"] == [FAILED]


def test_validate_without_records_rejects_missing_independent_tests_json(tmp_path: Path) -> None:
    fixture, good, bad = _pair(tmp_path, include_tests=False)
    graded = validate(fixture, good, bad)
    assert graded["ok"] is False
    assert graded["grades"]["known_good"]["reasons"] == [MISSING]
    assert graded["grades"]["known_bad"]["reasons"] == [MISSING]

    checked = _cli(fixture.directory, good, bad)
    assert checked.returncode == 0, checked.stderr
    body = json.loads(checked.stdout)
    assert body["ok"] is False
    assert body["grades"]["known_good"]["reasons"] == [MISSING]
    assert body["grades"]["known_bad"]["reasons"] == [MISSING]
