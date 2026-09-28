"""A command error before the first agent turn is the trial result."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
SESSION_ID = "sess-1"


def _fixture(root: Path, *, timeout_s: float) -> object:
    directory = root / "f1"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f1",
        "scored_experiment": "structured-amendment",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    scenario = {
        "command": "/execute OMP-1",
        "terminal": {"pointer": "/work_item/state", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def _drive(tmp_path: Path, *, timeout_s: float, events: list[dict], work_state: dict):
    fixture = _fixture(tmp_path / "fixtures", timeout_s=timeout_s)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps(events), encoding="utf-8")
    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        SESSION_ID,
        "--events",
        str(events_path),
    ]

    def execution() -> dict:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    started = time.monotonic()
    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_state,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()
    elapsed = time.monotonic() - started
    digest = hashlib.sha256((evidence_dir / "manifest.json").read_bytes()).hexdigest()
    loaded = load_evidence(evidence_dir, digest)
    return outcome, elapsed, loaded, record


def _prompt_count(record: Path) -> int:
    text = (record / "commands.jsonl").read_text(encoding="utf-8")
    commands = [json.loads(line)["command"] for line in text.splitlines() if line]
    return sum(command["type"] == "prompt" for command in commands)


def test_extension_error_before_turn_ends_the_trial(tmp_path: Path) -> None:
    reason = "issue OMP-1 not found"
    outcome, elapsed, loaded, record = _drive(
        tmp_path,
        timeout_s=30,
        events=[{"type": "extension_error", "error": reason, "event": "command", "extensionPath": "command:execute"}],
        work_state={"work_id": WORK_ID, "state": "running"},
    )
    assert outcome == "harness_error"
    assert elapsed < 2
    assert loaded.read_json("outcome.json") == {"outcome": "harness_error", "reason": reason, "prompts_sent": 1}
    assert _prompt_count(record) == 1
    frames = loaded.read_jsonl("rpc-transcript.jsonl")
    assert any(
        row["direction"] == "in" and row["frame"].get("type") == "extension_error" and row["frame"].get("error") == reason
        for row in frames
    )


def test_error_event_before_turn_uses_its_message(tmp_path: Path) -> None:
    outcome, elapsed, loaded, record = _drive(
        tmp_path,
        timeout_s=30,
        events=[{"type": "error", "message": "rpc exploded"}],
        work_state={"work_id": WORK_ID, "state": "running"},
    )
    assert outcome == "harness_error"
    assert elapsed < 2
    assert loaded.read_json("outcome.json")["reason"] == "rpc exploded"
    assert _prompt_count(record) == 1


def test_error_after_agent_start_does_not_abort(tmp_path: Path) -> None:
    outcome, elapsed, loaded, _record = _drive(
        tmp_path,
        timeout_s=3,
        events=[{"type": "agent_start"}, {"type": "extension_error", "error": "late"}],
        work_state={"work_id": WORK_ID, "state": "completed"},
    )
    assert outcome == "completed"
    assert elapsed < 2
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
