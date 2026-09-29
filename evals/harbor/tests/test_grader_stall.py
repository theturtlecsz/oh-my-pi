"""A no_agent_turn stall cannot be labeled expected_failure:seeded_defect.

The crash injection fires only after the continuation is queued, so a trial
that never started a turn never reached the seeded code path. The grader must
surface that as ``stalled:no_agent_turn`` instead of hiding it behind the
seeded-defect label.
"""

from __future__ import annotations

import json
from pathlib import Path

from omp_harbor_eval import EvidenceWriter, grade, load_fixture, validate
from omp_harbor_eval.grader import SEEDED_LABEL, STALLED_LABEL

READBACK = "service-readback.json"
TRANSCRIPT = "transcript.jsonl"
OUTCOME = "outcome.json"
TESTS = "independent-tests.json"
ACK_TARGET = "session-system/tests/f2-continuation-ack.test.ts"

COMPLETED_READBACK = {
    "execution": {
        "grant": {"continuations_scheduled": 0, "state": "completed"},
        "items": [{"close_attempts_started": 1}],
    }
}


def write_fixture(root: Path, fixture_id: str = "f2") -> object:
    directory = root / fixture_id
    directory.mkdir(parents=True)
    fixture = {
        "id": fixture_id,
        "scored_experiment": "repair",
        "seed_patch": "seed.patch",
        "solution_patch": "solution.patch",
        "independent_tests": [{"runner": "bun", "target": ACK_TARGET}],
        "scenario": "scenario.json",
        "rules": [
            {"type": "readback_equals", "pointer": "/execution/grant/continuations_scheduled", "value": 0},
            {"type": "readback_equals", "pointer": "/execution/items/0/close_attempts_started", "value": 1},
            {"type": "readback_equals", "pointer": "/execution/grant/state", "value": "completed"},
            {"type": "outcome_is", "outcome": "completed"},
            {"type": "independent_tests_pass"},
        ],
    }
    scenario = {
        "command": "/execute OMP-246",
        "terminal": {"pointer": "/execution/grant/state", "in": ["completed"]},
        "model_script": [{"role": "assistant", "text": "done"}],
        "ui_script": [],
        "kill_at": None,
        "timeout_s": 120,
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
    outcome: str = "completed",
    stall: bool = False,
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
    writer.write_json(READBACK, COMPLETED_READBACK)
    document: dict[str, object] = {"outcome": outcome}
    if stall:
        document["stall"] = "no_agent_turn"
    writer.write_json(OUTCOME, document)
    writer.append_jsonl(TRANSCRIPT, {"type": "event", "i": 0})
    writer.write_json(TESTS, [{"runner": "bun", "target": ACK_TARGET, "passed": True}])
    return writer.seal()


def test_stalled_known_bad_failure_is_labeled_stalled_not_seeded(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    directory = tmp_path / "stalled"
    digest = write_evidence(
        directory,
        fixture,
        variant="known_bad",
        run_id="run-stall",
        nonce="nonce-stall",
        outcome="timeout",
        stall=True,
    )

    result = grade(directory, digest, fixture, run_id="run-stall", nonce="nonce-stall")
    assert result["status"] == "fail"
    assert result["label"] == STALLED_LABEL
    assert result["reasons"] == ['outcome_is: expected completed got timeout']


def test_timeout_without_stall_keeps_seeded_label(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    directory = tmp_path / "timeout"
    digest = write_evidence(
        directory,
        fixture,
        variant="known_bad",
        run_id="run-timeout",
        nonce="nonce-timeout",
        outcome="timeout",
    )

    result = grade(directory, digest, fixture, run_id="run-timeout", nonce="nonce-timeout")
    assert result["status"] == "fail"
    assert result["label"] == SEEDED_LABEL


def test_validate_rejects_a_stalled_known_bad(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    write_evidence(good, fixture, variant="known_good", run_id="run-good", nonce="nonce-good")
    write_evidence(
        bad,
        fixture,
        variant="known_bad",
        run_id="run-bad",
        nonce="nonce-bad",
        outcome="timeout",
        stall=True,
    )

    result = validate(fixture, good, bad)
    assert result["ok"] is False
    assert result["verdicts"]["known_good"] == "pass"
    assert result["verdicts"]["known_bad"] == "fail"
    assert result["grades"]["known_bad"]["label"] == STALLED_LABEL
