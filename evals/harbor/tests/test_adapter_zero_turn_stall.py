"""A trial that never starts a turn records a named stall, not a session-reader harness_error."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute"
SESSION_ID = "sess-1"
READER_ERROR = "cat: /home/agent/omp-sessions/x.jsonl: No such file or directory"
ONE_STEP = [{"text": "done"}]


def write_fixture(root: Path, *, timeout_s: float, model_script: list) -> object:
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
        "command": COMMAND,
        "terminal": {"pointer": "/work_item/state", "in": ["completed"]},
        "model_script": model_script,
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def failing_reader(_path: str) -> bytes:
    raise RuntimeError(READER_ERROR)


def _drive(
    tmp_path: Path,
    *,
    model_script: list,
    events: list[dict] | None = None,
    before_seal=None,
):
    fixture = write_fixture(tmp_path / "fixtures", timeout_s=0.35, model_script=model_script)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events_path = None
    if events is not None:
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
    ]
    if events_path is not None:
        command.extend(["--events", str(events_path)])

    def execution() -> dict:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=lambda: {"work_id": WORK_ID, "state": "running"},
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(
            command,
            tmp_path,
            None,
            probe,
            writer,
            fixture.scenario,
            session_reader=failing_reader,
            before_seal=before_seal,
        ).run()
    return outcome, open_sealed(evidence_dir)


def test_zero_turn_timeout_records_stall_and_keeps_session_read_error(tmp_path: Path) -> None:
    ran: list[bool] = []

    def before_seal(_writer: EvidenceWriter) -> None:
        ran.append(True)

    outcome, loaded = _drive(tmp_path, model_script=ONE_STEP, before_seal=before_seal)
    document = loaded.read_json("outcome.json")
    assert outcome == "timeout"
    assert document["outcome"] == "timeout"
    assert document["stall"] == "no_agent_turn"
    assert "no agent turn started" in document["reason"]
    assert document["session_read_error"] == f"session_reader: {READER_ERROR}"
    assert document["prompts_sent"] == 1
    assert ran == [True]


def test_agent_start_session_read_failure_is_harness_error(tmp_path: Path) -> None:
    ran: list[bool] = []

    def before_seal(_writer: EvidenceWriter) -> None:
        ran.append(True)

    outcome, loaded = _drive(
        tmp_path,
        model_script=ONE_STEP,
        events=[{"type": "agent_start"}],
        before_seal=before_seal,
    )
    document = loaded.read_json("outcome.json")
    assert outcome == "harness_error"
    assert document["outcome"] == "harness_error"
    assert document["reason"] == f"session_reader: {READER_ERROR}"
    assert "stall" not in document
    assert not ran


def test_empty_model_script_timeout_has_no_stall(tmp_path: Path) -> None:
    outcome, loaded = _drive(tmp_path, model_script=[])
    document = loaded.read_json("outcome.json")
    assert outcome == "timeout"
    assert document["outcome"] == "timeout"
    assert "stall" not in document
    assert "no agent turn started" not in document["reason"]
    assert document["session_read_error"] == f"session_reader: {READER_ERROR}"
