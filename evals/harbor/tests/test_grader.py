"""Host-side grader controls: known-good, known-bad, tamper, and harness defects."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omp_harbor_eval import (
    EvidenceSealedError,
    EvidenceWriter,
    fixture_digest,
    grade,
    load_evidence,
    load_fixture,
    validate,
)
from omp_harbor_eval.grader import SEEDED_LABEL

READBACK = "service-readback.json"
TRANSCRIPT = "transcript.jsonl"
OUTCOME = "outcome.json"
TESTS = "independent-tests.json"
TEST_TARGET = "tests/test_amend.py"


def _rules() -> list[dict]:
    return [
        {"type": "readback_equals", "pointer": "/workflow/status", "value": "completed"},
        {"type": "transcript_count", "count": 2},
        {"type": "outcome_is", "outcome": "completed"},
        {"type": "independent_tests_pass"},
    ]


def write_fixture(root: Path, fixture_id: str = "f1", *, rules: list[dict] | None = None) -> object:
    directory = root / fixture_id
    directory.mkdir(parents=True)
    fixture = {
        "id": fixture_id,
        "scored_experiment": "structured-amendment",
        "seed_patch": "--- seed\n",
        "solution_patch": "--- solution\n",
        "independent_tests": [
            {
                "runner": "pytest",
                "target": TEST_TARGET,
                "requires": ["pytest"],
                "files": ["src/amend.py"],
            }
        ],
        "scenario": "scenario.json",
        "rules": _rules() if rules is None else rules,
    }
    scenario = {
        "command": "execute",
        "terminal": {"pointer": "/workflow/status", "in": ["completed", "failed"]},
        "model_script": [{"role": "assistant", "text": "done"}],
        "ui_script": [],
        "kill_at": None,
        "timeout_s": 30,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    return load_fixture(root, fixture_id)


def write_evidence(
    directory: Path,
    fixture,
    *,
    variant: str,
    run_id: str,
    nonce: str,
    readback: str = "completed",
    outcome: str = "completed",
    transcripts: int = 2,
    passed: bool = True,
) -> str:
    writer = EvidenceWriter(
        directory,
        run_id,
        nonce,
        fixture.id,
        fixture.digest,
        variant,
        fixture.scored_experiment,
    )
    writer.write_json(READBACK, {"workflow": {"status": readback}, "a/b": ["yes"]})
    writer.write_json(OUTCOME, {"outcome": outcome})
    for index in range(transcripts):
        writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": index})
    writer.write_json(TESTS, [{"runner": "pytest", "target": TEST_TARGET, "passed": passed}])
    return writer.seal()


def test_seal_manifest_roundtrip_and_reject_later_writes(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    directory = tmp_path / "run"
    writer = EvidenceWriter(
        directory,
        "run-1",
        "nonce-1",
        fixture.id,
        fixture.digest,
        "known_good",
        fixture.scored_experiment,
    )
    writer.write_json(READBACK, {"workflow": {"status": "completed"}})
    writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": 0})
    writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": 1})
    writer.add_file("note.txt", b"note")
    digest = writer.seal()

    manifest_bytes = (directory / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == digest
    manifest = json.loads(manifest_bytes)
    assert "manifest.json" not in manifest
    assert set(manifest) == {READBACK, TRANSCRIPT, "note.txt", "run.json"}
    for name, file_hash in manifest.items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == file_hash

    loaded = load_evidence(directory, digest)
    assert loaded.run_id == "run-1"
    assert loaded.nonce == "nonce-1"
    assert loaded.fixture_id == fixture.id
    assert loaded.fixture_digest == fixture.digest
    assert loaded.variant == "known_good"
    assert loaded.experiment == fixture.scored_experiment
    assert loaded.read_jsonl(TRANSCRIPT) == [{"type": "event", "i": 0}, {"type": "event", "i": 1}]

    with pytest.raises(EvidenceSealedError):
        writer.write_json("other.json", {})
    with pytest.raises(EvidenceSealedError):
        writer.append_jsonl(TRANSCRIPT, {"type": "later"})
    with pytest.raises(EvidenceSealedError):
        writer.add_file("more.txt", b"x")
    with pytest.raises(EvidenceSealedError):
        writer.seal()


def test_fixture_digest_and_scenario_shape(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    fixture = write_fixture(root)
    assert fixture.id == "f1"
    assert fixture.scored_experiment == "structured-amendment"
    assert fixture.seed_patch == "--- seed\n"
    assert fixture.solution_patch == "--- solution\n"
    assert fixture.independent_tests[0].runner == "pytest"
    assert fixture.independent_tests[0].target == TEST_TARGET
    assert fixture.independent_tests[0].requires == ("pytest",)
    assert fixture.independent_tests[0].files == ("src/amend.py",)
    assert fixture.scenario.command == "execute"
    assert fixture.scenario.terminal.pointer == "/workflow/status"
    assert fixture.scenario.terminal.in_ == ("completed", "failed")
    assert fixture.scenario.model_script == ({"role": "assistant", "text": "done"},)
    assert fixture.scenario.ui_script == ()
    assert fixture.scenario.kill_at is None
    assert fixture.scenario.timeout_s == 30
    assert fixture.digest == fixture_digest(fixture.directory)

    scenario = fixture.directory / "scenario.json"
    scenario.write_bytes(scenario.read_bytes() + b"\n")
    assert fixture_digest(fixture.directory) != fixture.digest

    broken_root = tmp_path / "broken-fixtures"
    broken = broken_root / "bad-count"
    broken.mkdir(parents=True)
    payload = json.loads((fixture.directory / "fixture.json").read_text(encoding="utf-8"))
    payload["id"] = "bad-count"
    payload["rules"] = [{"type": "transcript_count", "count": True}]
    (broken / "fixture.json").write_text(json.dumps(payload), encoding="utf-8")
    (broken / "scenario.json").write_text((fixture.directory / "scenario.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="count"):
        load_fixture(broken_root, "bad-count")


def test_known_good_passes_and_known_bad_is_seeded_failure(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good_hash = write_evidence(good, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")
    bad_hash = write_evidence(
        bad,
        fixture,
        variant="known_bad",
        run_id="run-bad",
        nonce="nonce-bad",
        readback="seeded_defect",
    )

    passed = grade(good, good_hash, fixture, run_id="run-good", nonce="nonce-good")
    assert passed == {"status": "pass", "label": "pass", "reasons": []}

    failed = grade(bad, bad_hash, fixture, run_id="run-bad", nonce="nonce-bad")
    assert failed["status"] == "fail"
    assert failed["label"] == SEEDED_LABEL
    assert failed["reasons"] == [
        'readback_equals: /workflow/status expected "completed" got "seeded_defect"'
    ]

    ordinary = tmp_path / "ordinary-fail"
    ordinary_hash = write_evidence(
        ordinary,
        fixture,
        variant="known_good",
        run_id="run-ordinary",
        nonce="nonce-ordinary",
        readback="seeded_defect",
    )
    ordinary_grade = grade(ordinary, ordinary_hash, fixture, run_id="run-ordinary", nonce="nonce-ordinary")
    assert ordinary_grade["status"] == "fail"
    assert ordinary_grade["label"] == "fail"


def test_each_scoring_rule_can_fail(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    short = tmp_path / "short"
    short_hash = write_evidence(
        short, fixture, variant="known_good", run_id="run-short", nonce="nonce-short", transcripts=1
    )
    assert grade(short, short_hash, fixture, run_id="run-short", nonce="nonce-short")["reasons"] == [
        "transcript_count: expected 2 got 1"
    ]

    outcome = tmp_path / "outcome"
    outcome_hash = write_evidence(
        outcome, fixture, variant="known_good", run_id="run-outcome", nonce="nonce-outcome", outcome="failed"
    )
    assert grade(outcome, outcome_hash, fixture, run_id="run-outcome", nonce="nonce-outcome")["reasons"] == [
        "outcome_is: expected completed got failed"
    ]

    tests = tmp_path / "tests-failed"
    tests_hash = write_evidence(
        tests, fixture, variant="known_bad", run_id="run-tests", nonce="nonce-tests", passed=False
    )
    failed = grade(tests, tests_hash, fixture, run_id="run-tests", nonce="nonce-tests")
    assert failed["status"] == "fail"
    assert failed["label"] == SEEDED_LABEL
    assert failed["reasons"] == [f"independent_tests_pass: pytest {TEST_TARGET} failed"]


def test_readback_pointer_escape(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path / "fixtures",
        rules=[{"type": "readback_equals", "pointer": "/a~1b/0", "value": "yes"}],
    )
    directory = tmp_path / "run"
    writer = EvidenceWriter(
        directory, "run-ptr", "nonce-ptr", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    writer.write_json(READBACK, {"a/b": ["yes"]})
    digest = writer.seal()
    assert grade(directory, digest, fixture, run_id="run-ptr", nonce="nonce-ptr")["status"] == "pass"


def test_tampered_missing_and_extra_are_invalid(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    digest = write_evidence(good, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")

    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for path in good.iterdir():
        (tampered / path.name).write_bytes(path.read_bytes())
    readback = tampered / READBACK
    readback.write_bytes(readback.read_bytes() + b"X")
    altered = grade(tampered, digest, fixture, run_id="run-good", nonce="nonce-good")
    assert altered["status"] == "invalid_evidence"
    assert altered["label"] == "invalid_evidence"
    assert altered["reasons"] == [f"altered file: {READBACK}"]

    missing = tmp_path / "missing"
    missing.mkdir()
    for path in good.iterdir():
        if path.name != TRANSCRIPT:
            (missing / path.name).write_bytes(path.read_bytes())
    gone = grade(missing, digest, fixture, run_id="run-good", nonce="nonce-good")
    assert gone["status"] == "invalid_evidence"
    assert gone["reasons"] == [f"missing file: {TRANSCRIPT}"]

    extra = tmp_path / "extra"
    extra.mkdir()
    for path in good.iterdir():
        (extra / path.name).write_bytes(path.read_bytes())
    (extra / "sneak.txt").write_bytes(b"sneak")
    added = grade(extra, digest, fixture, run_id="run-good", nonce="nonce-good")
    assert added["status"] == "invalid_evidence"
    assert added["reasons"] == ["extra file: sneak.txt"]

    mismatch = grade(good, "0" * 64, fixture, run_id="run-good", nonce="nonce-good")
    assert mismatch["reasons"] == ["manifest sha256 mismatch"]


def test_foreign_identity_is_invalid(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    directory = tmp_path / "good"
    digest = write_evidence(directory, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")

    wrong_run = grade(directory, digest, fixture, run_id="other-run", nonce="nonce-good")
    assert wrong_run["status"] == "invalid_evidence"
    assert wrong_run["reasons"] == ["foreign run_id"]

    wrong_nonce = grade(directory, digest, fixture, run_id="run-good", nonce="other-nonce")
    assert wrong_nonce["reasons"] == ["foreign nonce"]

    foreign_dir = tmp_path / "foreign-digest"
    writer = EvidenceWriter(
        foreign_dir,
        "run-good",
        "nonce-good",
        fixture.id,
        "0" * 64,
        "known_good",
        fixture.scored_experiment,
    )
    writer.write_json(READBACK, {"workflow": {"status": "completed"}})
    writer.write_json(OUTCOME, {"outcome": "completed"})
    writer.append_jsonl(TRANSCRIPT, {"i": 0})
    writer.append_jsonl(TRANSCRIPT, {"i": 1})
    writer.write_json(TESTS, [{"runner": "pytest", "target": TEST_TARGET, "passed": True}])
    foreign_hash = writer.seal()
    foreign = grade(foreign_dir, foreign_hash, fixture, run_id="run-good", nonce="nonce-good")
    assert foreign["status"] == "invalid_evidence"
    assert foreign["reasons"] == ["foreign fixture_digest"]


def test_harness_error_is_not_a_model_failure(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    directory = tmp_path / "harness"
    digest = write_evidence(
        directory,
        fixture,
        variant="known_bad",
        run_id="run-harness",
        nonce="nonce-harness",
        readback="seeded_defect",
        outcome="harness_error",
    )
    result = grade(directory, digest, fixture, run_id="run-harness", nonce="nonce-harness")
    assert result == {
        "status": "harness_defect",
        "label": "harness_defect",
        "reasons": ["outcome is harness_error"],
    }


def test_validate_accepts_the_four_verdicts_and_flags_a_passing_known_bad(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    write_evidence(good, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")
    write_evidence(
        bad, fixture, variant="known_bad", run_id="run-bad", nonce="nonce-bad", readback="seeded_defect"
    )

    accepted = validate(fixture, good, bad)
    assert accepted["ok"] is True
    assert accepted["verdicts"] == {
        "known_good": "pass",
        "known_bad": "fail",
        "tampered": "invalid_evidence",
        "foreign": "invalid_evidence",
    }
    assert accepted["grades"]["known_bad"]["label"] == SEEDED_LABEL
    assert accepted["grades"]["tampered"]["reasons"] == [f"altered file: {READBACK}"]
    assert accepted["grades"]["foreign"]["reasons"] == [
        "foreign run_id",
        "foreign nonce",
        "foreign fixture_digest",
    ]

    passing_bad = tmp_path / "passing-bad"
    write_evidence(passing_bad, fixture, variant="known_bad", run_id="run-passbad", nonce="nonce-passbad")
    flagged = validate(fixture, good, passing_bad)
    assert flagged["ok"] is False
    assert flagged["verdicts"]["known_bad"] == "pass"


def test_cli_prints_json(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good_hash = write_evidence(good, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")
    write_evidence(
        bad, fixture, variant="known_bad", run_id="run-bad", nonce="nonce-bad", readback="seeded_defect"
    )

    graded = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.grader",
            "grade",
            "--evidence",
            str(good),
            "--manifest-sha256",
            good_hash,
            "--fixture",
            str(fixture.directory),
            "--run-id",
            "run-good",
            "--nonce",
            "nonce-good",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert graded.returncode == 0, graded.stderr
    assert json.loads(graded.stdout)["status"] == "pass"

    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.grader",
            "validate",
            "--fixture",
            str(fixture.directory),
            "--good",
            str(good),
            "--bad",
            str(bad),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
    body = json.loads(checked.stdout)
    assert body["ok"] is True
    assert body["verdicts"]["known_bad"] == "fail"
