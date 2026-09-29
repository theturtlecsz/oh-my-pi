"""Tests for capturing session files relocated after startup.

Defends the adapter contract that when reading the startup session file fails
after a prompt was sent, RpcAdapter queries live session state and retries the
session read against the relocated session file.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from fake_workservice import FakeWorkService

from omp_harbor_eval import (
    EvidenceWriter,
    RpcAdapter,
    ServiceProbe,
    load_evidence,
    load_fixture,
)
from omp_harbor_eval.adapter import kill_process_group

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute OMP-1"
INITIAL_SESSION_ID = "sess-startup"
RELOCATED_SESSION_ID = "sess-relocated"


def _write_fixture(root: Path, *, timeout_s: float = 3.0) -> object:
    directory = root / "f1"
    directory.mkdir(parents=True, exist_ok=True)
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
        "model_script": [],
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def _open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def _write_kill_fixture(root: Path, *, fixture_id: str, scenario: dict) -> object:
    directory = root / fixture_id
    directory.mkdir(parents=True, exist_ok=True)
    fixture = {
        "id": fixture_id,
        "scored_experiment": "repair",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, fixture_id)


def test_relocated_session_captured_when_startup_path_missing(tmp_path: Path) -> None:
    """When the startup session file was deleted on relocation, the adapter
    queries live session state, reads the relocated file, preserves the terminal
    outcome, writes session.json with live + startup pairs, and runs before_seal."""
    fixture = _write_fixture(tmp_path / "fixtures", timeout_s=3.0)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        "run-1",
        "nonce-1",
        fixture.id,
        fixture.digest,
        "known_good",
        fixture.scored_experiment,
    )
    record = tmp_path / "rpc-record"
    startup_session_file = tmp_path / "sessions" / "startup.jsonl"
    relocated_session_file = tmp_path / "sessions" / "relocated.jsonl"

    events = [
        {"type": "agent_start"},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps(events), encoding="utf-8")
    ack_flag = tmp_path / "acked"

    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(startup_session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        INITIAL_SESSION_ID,
        "--events",
        str(events_path),
        "--ack-flag",
        str(ack_flag),
        "--relocate-session-file",
        str(relocated_session_file),
        "--relocate-session-id",
        RELOCATED_SESSION_ID,
    ]

    before_seal_ran: list[bool] = []

    def before_seal_hook(_evidence: EvidenceWriter) -> None:
        before_seal_ran.append(True)

    reader_calls: list[str] = []

    def disk_reader(path: str) -> bytes:
        reader_calls.append(path)
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"cat: {path}: No such file or directory")
        return p.read_bytes()

    def execution() -> dict[str, Any]:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    def work_item() -> dict[str, Any]:
        if ack_flag.exists():
            return {"work_id": WORK_ID, "state": "completed"}
        return {"work_id": WORK_ID, "state": "running"}

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        adapter = RpcAdapter(
            command,
            tmp_path,
            None,
            probe,
            writer,
            fixture.scenario,
            session_reader=disk_reader,
            before_seal=before_seal_hook,
        )
        outcome = adapter.run()

    assert outcome == "completed"
    assert not startup_session_file.exists()
    assert relocated_session_file.is_file()
    assert reader_calls == [str(startup_session_file), str(relocated_session_file)]
    assert before_seal_ran == [True]

    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json")["outcome"] == "completed"
    assert loaded.read_json("session.json") == {
        "id": RELOCATED_SESSION_ID,
        "file": str(relocated_session_file),
        "startup": {
            "id": INITIAL_SESSION_ID,
            "file": str(startup_session_file),
        },
    }
    assert loaded.read_bytes("session.jsonl") == relocated_session_file.read_bytes()


def test_relocated_session_both_paths_raise(tmp_path: Path) -> None:
    """When the session reader raises for both the startup path and the
    relocated path, the adapter seals harness_error, the reason starts
    with session_reader:, session.jsonl is empty, and before_seal does not run."""
    fixture = _write_fixture(tmp_path / "fixtures", timeout_s=3.0)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        "run-1",
        "nonce-1",
        fixture.id,
        fixture.digest,
        "known_good",
        fixture.scored_experiment,
    )
    record = tmp_path / "rpc-record"
    startup_session_file = tmp_path / "sessions" / "startup.jsonl"
    relocated_session_file = tmp_path / "sessions" / "relocated.jsonl"

    events = [
        {"type": "agent_start"},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps(events), encoding="utf-8")
    ack_flag = tmp_path / "acked"

    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(startup_session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        INITIAL_SESSION_ID,
        "--events",
        str(events_path),
        "--ack-flag",
        str(ack_flag),
        "--relocate-session-file",
        str(relocated_session_file),
        "--relocate-session-id",
        RELOCATED_SESSION_ID,
    ]

    before_seal_ran: list[bool] = []

    def before_seal_hook(_evidence: EvidenceWriter) -> None:
        before_seal_ran.append(True)

    reader_calls: list[str] = []

    def raising_reader(path: str) -> bytes:
        reader_calls.append(path)
        raise FileNotFoundError(f"cat: {path}: No such file or directory")

    def execution() -> dict[str, Any]:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    def work_item() -> dict[str, Any]:
        if ack_flag.exists():
            return {"work_id": WORK_ID, "state": "completed"}
        return {"work_id": WORK_ID, "state": "running"}

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        adapter = RpcAdapter(
            command,
            tmp_path,
            None,
            probe,
            writer,
            fixture.scenario,
            session_reader=raising_reader,
            before_seal=before_seal_hook,
        )
        outcome = adapter.run()

    assert outcome == "harness_error"
    assert not before_seal_ran, "before_seal must not run when session reading fails"
    assert reader_calls == [str(startup_session_file), str(relocated_session_file)]

    loaded = _open_sealed(evidence_dir)
    outcome_doc = loaded.read_json("outcome.json")
    assert outcome_doc["outcome"] == "harness_error"
    assert outcome_doc["reason"].startswith("session_reader:")
    assert loaded.read_bytes("session.jsonl") == b""


def test_f2_restart_captures_relocated_session_after_enqueue_kill(tmp_path: Path) -> None:
    """A kill at the enqueue boundary followed by a ``--session`` restart must
    still let ``_seal`` recover the relocated session file while ``_killed``
    stays set: the trial ends ``completed`` with no ``session_read_error``,
    ``session.json`` records the startup pair plus the restart, and
    ``session.jsonl`` holds the bytes the reader returned for the live file."""
    scenario = {
        "command": COMMAND,
        "terminal": {"pointer": "/execution/grant/state", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "kill_at": {"match": {"type": "prompt", "message": COMMAND}, "boundary": "enqueue"},
        "timeout_s": 15,
    }
    fixture = _write_kill_fixture(tmp_path / "fixtures", fixture_id="f2", scenario=scenario)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        "run-1",
        "nonce-1",
        fixture.id,
        fixture.digest,
        "known_good",
        fixture.scored_experiment,
    )
    record = tmp_path / "rpc-record"
    startup_session_file = tmp_path / "sessions" / "startup.jsonl"
    relocated_session_file = tmp_path / "sessions" / "relocated.jsonl"
    relocated_payload = (
        b'{"type":"session","id":"' + RELOCATED_SESSION_ID.encode() + b'"}\n'
        b'{"type":"custom","customType":"work-now-execute-outbox",'
        b'"data":{"status":"queued","grantId":"g1"}}\n'
    )

    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(startup_session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        INITIAL_SESSION_ID,
        "--relocate-session-file",
        str(relocated_session_file),
        "--relocate-session-id",
        RELOCATED_SESSION_ID,
    ]

    def session_reader(path: str) -> bytes:
        if path == str(relocated_session_file):
            return relocated_payload
        raise FileNotFoundError(f"cat: {path}: No such file or directory")

    grant_state = {"value": "active"}
    calls: list[int] = []

    def killer(process) -> None:
        calls.append(process.pid)
        grant_state["value"] = "completed"
        kill_process_group(process)

    def execution() -> dict[str, Any]:
        return {
            "grant": {"state": grant_state["value"]},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    def work_item() -> dict[str, Any]:
        return {"work_id": WORK_ID, "state": "running"}

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        adapter = RpcAdapter(
            command,
            tmp_path,
            None,
            probe,
            writer,
            fixture.scenario,
            killer=killer,
            session_reader=session_reader,
        )
        outcome = adapter.run()

    assert outcome == "completed"
    assert len(calls) == 1, "exactly one kill"
    assert not startup_session_file.exists()
    assert relocated_session_file.is_file()

    invocations = [
        json.loads(line)
        for line in (record / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(invocations) == 2
    assert invocations[1]["argv"][-2:] == ["--session", str(relocated_session_file)]

    loaded = _open_sealed(evidence_dir)
    outcome_doc = loaded.read_json("outcome.json")
    assert outcome_doc["outcome"] == "completed"
    assert "session_read_error" not in outcome_doc
    assert outcome_doc["prompts_sent"] == 1
    prompts = [
        row["frame"]
        for row in loaded.read_jsonl("rpc-transcript.jsonl")
        if row.get("direction") == "out" and isinstance(row.get("frame"), dict) and row["frame"].get("type") == "prompt"
    ]
    assert [frame.get("message") for frame in prompts] == [COMMAND]
    assert loaded.read_json("session.json") == {
        "id": RELOCATED_SESSION_ID,
        "file": str(relocated_session_file),
        "startup": {
            "id": INITIAL_SESSION_ID,
            "file": str(startup_session_file),
        },
        "restarts": [{"id": RELOCATED_SESSION_ID, "file": str(relocated_session_file)}],
    }
    assert loaded.read_bytes("session.jsonl") == relocated_payload
