"""UI script answers, missing-approval block, and one same-session restart."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.adapter import kill_process_group
from omp_harbor_eval.ui_script import UiScript

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute"
SESSION_ID = "sess-1"
OTHER_SESSION_ID = "sess-2"


def write_fixture(root: Path, *, timeout_s: float, kill_at: dict | None = None) -> object:
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
        "model_script": [],
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    if kill_at is not None:
        scenario["kill_at"] = kill_at
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def outbound(loaded) -> list[dict]:
    frames = []
    for row in loaded.read_jsonl("rpc-transcript.jsonl"):
        frame = row.get("frame")
        if row.get("direction") == "out" and isinstance(frame, dict):
            frames.append(frame)
    return frames


def prompt_frames(loaded) -> list[dict]:
    return [frame for frame in outbound(loaded) if frame.get("type") == "prompt"]


def saw_response(record: Path, request_id: str) -> bool:
    path = record / "commands.jsonl"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        command = json.loads(line)["command"]
        if command.get("type") == "extension_ui_response" and command.get("id") == request_id:
            return True
    return False


def drive(
    tmp_path: Path,
    *,
    timeout_s: float,
    ui_rules: list[dict] | None,
    ui_requests: list[dict],
    kill_at: dict | None = None,
    extra: list[str] | None = None,
    killer=None,
    terminal_when_answered: str | None = None,
    work_state: str = "completed",
):
    fixture = write_fixture(tmp_path / "fixtures", timeout_s=timeout_s, kill_at=kill_at)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    ui_path = None
    if ui_rules is not None:
        ui_path = tmp_path / "ui-script.json"
        ui_path.write_text(json.dumps(ui_rules) + "\n", encoding="utf-8")
    requests_path = tmp_path / "ui-requests.json"
    requests_path.write_text(json.dumps(ui_requests), encoding="utf-8")
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
        "--ui-requests",
        str(requests_path),
    ]
    if extra:
        command.extend(extra)

    def execution() -> dict:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    def work_item() -> dict:
        if terminal_when_answered is not None and not saw_response(record, terminal_when_answered):
            return {"work_id": WORK_ID, "state": "running"}
        return {"work_id": WORK_ID, "state": work_state}

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
            ui_script=None if ui_path is None else ui_path,
            killer=killer,
        )
        outcome = adapter.run()
    loaded = open_sealed(evidence_dir)
    return outcome, loaded, record, session_file, command


def test_ui_script_load_matches_the_first_exact_title(tmp_path: Path) -> None:
    path = tmp_path / "ui.json"
    path.write_text(
        json.dumps(
            [
                {"method": "confirm", "title": "Other", "answer": {"confirmed": False}},
                {"method": "confirm", "title": "Run tool?", "answer": {"confirmed": True}},
                {"method": "input", "title": "Name", "answer": {"value": "ship"}},
                {"method": "select", "title": "Pick", "answer": {"cancel": True}},
                {"method": "editor", "title": "Edit", "answer": {"value": "body"}},
            ]
        ),
        encoding="utf-8",
    )
    script = UiScript.load(path)
    assert script.match("confirm", "Run tool?")["answer"] == {"confirmed": True}
    assert script.match("confirm", "run tool?") is None
    assert script.match("input", "Name")["answer"] == {"value": "ship"}
    assert script.match("notify", "Run tool?") is None

    broken = tmp_path / "bad.json"
    broken.write_text(json.dumps([{"method": "input", "title": "Name", "answer": {"confirmed": True}}]), encoding="utf-8")
    with pytest.raises(ValueError, match="confirmed"):
        UiScript.load(broken)


def test_scripted_confirm_is_answered_and_recorded(tmp_path: Path) -> None:
    outcome, loaded, _record, _session_file, _command = drive(
        tmp_path,
        timeout_s=3,
        ui_rules=[
            {"method": "confirm", "title": "Other", "answer": {"confirmed": False}},
            {"method": "confirm", "title": "Run tool?", "answer": {"confirmed": True}},
        ],
        ui_requests=[
            {"type": "extension_ui_request", "id": "ui-note", "method": "notify", "title": "working", "notifyType": "info"},
            {"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Run tool?", "message": "Continue?"},
        ],
        terminal_when_answered="ui-1",
    )
    assert outcome == "completed"
    prompts = prompt_frames(loaded)
    assert len(prompts) == 1
    assert prompts[0]["message"] == COMMAND
    assert [frame for frame in outbound(loaded) if frame.get("type") == "extension_ui_response"] == [
        {"type": "extension_ui_response", "id": "ui-1", "confirmed": True}
    ]
    assert loaded.read_jsonl("ui-answers.jsonl") == [
        {"id": "ui-note", "method": "notify", "status": "recorded", "title": "working"},
        {
            "id": "ui-1",
            "method": "confirm",
            "status": "answered",
            "title": "Run tool?",
            "answer": {"confirmed": True},
        },
    ]
    assert loaded.read_json("outcome.json")["prompts_sent"] == 1


def test_unscripted_confirm_cancels_and_blocks(tmp_path: Path) -> None:
    outcome, loaded, _record, _session_file, _command = drive(
        tmp_path,
        timeout_s=3,
        ui_rules=[{"method": "confirm", "title": "Other", "answer": {"confirmed": True}}],
        ui_requests=[
            {"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Run tool?", "message": "Continue?"},
        ],
        work_state="running",
    )
    assert outcome == "blocked"
    assert loaded.read_json("outcome.json") == {
        "outcome": "blocked",
        "reason": 'unscripted confirm "Run tool?": add a ui-script rule',
        "prompts_sent": 1,
    }
    frames = [frame for frame in outbound(loaded) if frame.get("type") == "extension_ui_response"]
    assert frames == [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}]
    assert not any(frame.get("confirmed") is True for frame in outbound(loaded))
    assert not any("value" in frame for frame in frames)
    assert loaded.read_jsonl("ui-answers.jsonl") == [
        {"id": "ui-1", "method": "confirm", "status": "unscripted", "title": "Run tool?"},
    ]
    assert len(prompt_frames(loaded)) == 1


def test_kill_at_restarts_once_with_the_same_session(tmp_path: Path) -> None:
    seen: list[int] = []

    def killer(process) -> None:
        seen.append(process.pid)
        kill_process_group(process)

    outcome, loaded, record, session_file, command = drive(
        tmp_path,
        timeout_s=5,
        ui_rules=[],
        ui_requests=[],
        kill_at={"match": {"type": "prompt", "message": COMMAND}},
        killer=killer,
    )
    assert outcome == "completed"
    assert seen and len(seen) == 1
    assert len(prompt_frames(loaded)) == 1
    assert loaded.read_json("outcome.json")["prompts_sent"] == 1
    assert loaded.read_json("session.json") == {
        "id": SESSION_ID,
        "file": str(session_file),
        "restarts": [{"id": SESSION_ID, "file": str(session_file)}],
    }
    invocations = [
        json.loads(line)["argv"] for line in (record / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(invocations) == 2
    assert invocations[1] == invocations[0] + ["--session", str(session_file)]
    assert invocations[0] == command[1:]


def test_changed_session_id_is_harness_error(tmp_path: Path) -> None:
    outcome, loaded, record, session_file, _command = drive(
        tmp_path,
        timeout_s=2,
        ui_rules=[],
        ui_requests=[],
        kill_at={"match": {"type": "prompt", "message": COMMAND}},
        extra=["--resume-session-id", OTHER_SESSION_ID],
        work_state="running",
    )
    assert outcome == "harness_error"
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": "restart session id differs from the original",
        "prompts_sent": 1,
    }
    assert loaded.read_json("session.json")["id"] == SESSION_ID
    assert loaded.read_json("session.json")["restarts"] == [{"id": OTHER_SESSION_ID, "file": str(session_file)}]
    assert len(prompt_frames(loaded)) == 1
    invocations = [
        json.loads(line)["argv"]
        for line in (record / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(invocations) == 2
    assert invocations[1] == invocations[0] + ["--session", str(session_file)]
