"""RPC adapter: one prompt, service readback, seal before the process stops."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import (
    EvidenceSealedError,
    EvidenceWriter,
    RpcAdapter,
    ServiceProbe,
    load_evidence,
    load_fixture,
)

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute"
SESSION_ID = "sess-1"
SESSION_BYTES = b'{"type":"session","id":"sess-1"}\n{"type":"note"}\n'
_FORBIDDEN = {"steer", "follow_up", "abort_and_prompt", "abort"}


def write_fixture(root: Path, *, timeout_s: float, accepted: list[str]) -> object:
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
        "terminal": {"pointer": "/work_item/state", "in": accepted},
        "model_script": [],
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def load_commands(record: Path) -> list[dict]:
    text = (record / "commands.jsonl").read_text(encoding="utf-8")
    return [json.loads(line)["command"] for line in text.splitlines() if line]


def assert_manifest_at_eof(record: Path) -> None:
    assert json.loads((record / "eof.json").read_text(encoding="utf-8")) == {"manifest_exists": True}


def assert_prompts(commands: list[dict], count: int) -> None:
    assert [command["type"] for command in commands].count("prompt") == count
    assert not any(command["type"] in _FORBIDDEN for command in commands)
    prompts = [command for command in commands if command["type"] == "prompt"]
    assert all(command["message"] == COMMAND for command in prompts)


def test_non_loopback_raises_before_process_start(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    command = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)]
    fixture = write_fixture(tmp_path / "fixtures", timeout_s=1, accepted=["completed"])
    evidence = tmp_path / "evidence"
    writer = EvidenceWriter(evidence, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment)
    with pytest.raises(ValueError, match="loopback"):
        probe = ServiceProbe("http://203.0.113.5:9", BEARER, WORKSPACE)
        RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()
    assert not marker.exists()

    for url in (
        "http://10.0.0.8:9",
        "http://127.0.0.2:9",
        "http://localhost.evil.com",
        "http://[::2]:9",
        "ftp://127.0.0.1:9",
        "http://127.0.0.1:9/v1",
        "http://user:secret@127.0.0.1:9",
    ):
        with pytest.raises(ValueError):
            ServiceProbe(url, BEARER, WORKSPACE)


def test_loopback_origins_do_not_connect() -> None:
    started = time.monotonic()
    for url in ("http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1", "https://localhost"):
        probe = ServiceProbe(url, BEARER, WORKSPACE)
        assert probe.base_url == url
    assert ServiceProbe("http://127.0.0.1:9/", BEARER, WORKSPACE).base_url == "http://127.0.0.1:9"
    assert time.monotonic() - started < 1


def _rpc_command(record: Path, session_file: Path, evidence: Path, *, events: Path | None = None, ack_flag: Path | None = None, extra: list[str] | None = None) -> list[str]:
    command = [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(session_file),
        "--evidence",
        str(evidence),
        "--session-id",
        SESSION_ID,
    ]
    if events is not None:
        command.extend(["--events", str(events)])
    if ack_flag is not None:
        command.extend(["--ack-flag", str(ack_flag)])
    if extra:
        command.extend(extra)
    return command


def _drive(
    tmp_path: Path,
    *,
    timeout_s: float,
    accepted: list[str],
    ready: bool,
    work_state,
    extra: list[str] | None = None,
    events: list[dict] | None = None,
    ack_flag: Path | None = None,
):
    fixture = write_fixture(tmp_path / "fixtures", timeout_s=timeout_s, accepted=accepted)
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
    command = _rpc_command(record, session_file, evidence_dir, events=events_path, ack_flag=ack_flag, extra=extra)

    def execution() -> dict:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=ready,
        execution=execution,
        work_item=work_state,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()
        calls = list(service.calls)
    return outcome, writer, evidence_dir, record, session_file, calls


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_run_returns_only_after_terminal_readback(tmp_path: Path, terminal: str) -> None:
    ack_flag = tmp_path / "acked"
    seen = {"after_ack": False}

    def work_item() -> dict:
        if ack_flag.exists():
            if not seen["after_ack"]:
                seen["after_ack"] = True
                return {"work_id": WORK_ID, "state": "running"}
            return {"work_id": WORK_ID, "state": terminal}
        return {"work_id": WORK_ID, "state": "running"}

    outcome, writer, evidence_dir, record, session_file, calls = _drive(
        tmp_path,
        timeout_s=3,
        accepted=[terminal],
        ready=True,
        work_state=work_item,
        ack_flag=ack_flag,
        events=[{"type": "agent_start"}, {"type": "agent_end", "messages": [], "isTerminal": True}],
    )
    assert outcome == terminal
    assert ack_flag.exists()
    commands = load_commands(record)
    assert [command["type"] for command in commands] == ["negotiate_protocol", "get_state", "prompt"]
    assert_prompts(commands, 1)
    assert_manifest_at_eof(record)

    states = [call["state"] for call in calls if call["path"] == f"/v1/work-items/{WORK_ID}"]
    assert states[0] == "running"
    assert states[-1] == terminal
    assert states.count(terminal) == 1
    paths = {call["path"] for call in calls}
    assert paths == {
        "/v1/health/ready",
        f"/v1/workspaces/{WORKSPACE}/execution",
        f"/v1/work-items/{WORK_ID}",
    }
    assert all(not call["authorized"] for call in calls if call["path"] == "/v1/health/ready")
    assert all(call["authorized"] and call["workspace"] == WORKSPACE for call in calls if call["path"] != "/v1/health/ready")

    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": terminal, "reason": "terminal", "prompts_sent": 1}
    assert loaded.read_json("service-readback.json")["work_item"]["state"] == terminal
    assert loaded.read_json("service-readback.json")["health"]["ready"] is True
    assert loaded.read_json("session.json") == {"id": SESSION_ID, "file": str(session_file)}
    assert loaded.read_bytes("session.jsonl") == SESSION_BYTES
    assert session_file.read_bytes() == SESSION_BYTES

    frames = loaded.read_jsonl("rpc-transcript.jsonl")
    prompt_at = next(
        index
        for index, row in enumerate(frames)
        if row["direction"] == "out" and row["frame"].get("type") == "prompt"
    )
    ack_at = next(
        index
        for index, row in enumerate(frames)
        if row["direction"] == "in"
        and isinstance(row["frame"], dict)
        and row["frame"].get("type") == "response"
        and row["frame"].get("command") == "prompt"
    )
    event_at = next(
        index
        for index, row in enumerate(frames)
        if row["direction"] == "in" and isinstance(row["frame"], dict) and row["frame"].get("type") == "agent_start"
    )
    assert prompt_at < ack_at < event_at
    assert frames[prompt_at]["frame"]["message"] == COMMAND
    assert frames[ack_at]["frame"]["success"] is True
    assert any(row["direction"] == "in" and isinstance(row["frame"], dict) and row["frame"].get("type") == "ready" for row in frames)
    with pytest.raises(EvidenceSealedError):
        writer.write_json("later.json", {})


def test_service_not_ready_sends_no_prompt(tmp_path: Path) -> None:
    outcome, writer, evidence_dir, record, _session_file, calls = _drive(
        tmp_path,
        timeout_s=2,
        accepted=["completed"],
        ready=False,
        work_state=lambda: {"work_id": WORK_ID, "state": "completed"},
    )
    assert outcome == "harness_error"
    commands = load_commands(record)
    assert [command["type"] for command in commands] == ["negotiate_protocol", "get_state"]
    assert_prompts(commands, 0)
    assert_manifest_at_eof(record)
    assert {call["path"] for call in calls} == {"/v1/health/ready"}
    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": "service not ready",
        "prompts_sent": 0,
    }
    assert loaded.read_json("session.json")["id"] == SESSION_ID
    assert loaded.read_bytes("session.jsonl") == SESSION_BYTES
    with pytest.raises(EvidenceSealedError):
        writer.seal()


def test_get_state_failure_sends_no_prompt(tmp_path: Path) -> None:
    outcome, _writer, evidence_dir, record, _session_file, calls = _drive(
        tmp_path,
        timeout_s=2,
        accepted=["completed"],
        ready=True,
        work_state=lambda: {"work_id": WORK_ID, "state": "completed"},
        extra=["--bad-state"],
    )
    assert outcome == "harness_error"
    commands = load_commands(record)
    assert [command["type"] for command in commands] == ["negotiate_protocol", "get_state"]
    assert_prompts(commands, 0)
    assert_manifest_at_eof(record)
    assert calls == []
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["outcome"] == "harness_error"
    assert document["prompts_sent"] == 0
    assert loaded.read_json("session.json") == {"id": None, "file": None}


def test_rejected_prompt_is_not_accepted_and_is_not_retried(tmp_path: Path) -> None:
    outcome, _writer, evidence_dir, record, _session_file, calls = _drive(
        tmp_path,
        timeout_s=2,
        accepted=["completed"],
        ready=True,
        work_state=lambda: {"work_id": WORK_ID, "state": "completed"},
        extra=["--reject-prompt"],
    )
    assert outcome == "harness_error"
    commands = load_commands(record)
    assert [command["type"] for command in commands] == ["negotiate_protocol", "get_state", "prompt"]
    assert_prompts(commands, 1)
    assert_manifest_at_eof(record)
    assert all(call["path"] == "/v1/health/ready" for call in calls)
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["outcome"] == "harness_error"
    assert document["prompts_sent"] == 1
    assert document["reason"]
    assert loaded.read_json("service-readback.json") == {}


def test_timeout_seals_without_another_prompt(tmp_path: Path) -> None:
    outcome, writer, evidence_dir, record, _session_file, calls = _drive(
        tmp_path,
        timeout_s=0.35,
        accepted=["completed"],
        ready=True,
        work_state=lambda: {"work_id": WORK_ID, "state": "running"},
    )
    assert outcome == "timeout"
    commands = load_commands(record)
    assert [command["type"] for command in commands] == ["negotiate_protocol", "get_state", "prompt"]
    assert_prompts(commands, 1)
    assert_manifest_at_eof(record)
    states = [call["state"] for call in calls if call["path"] == f"/v1/work-items/{WORK_ID}"]
    assert states
    assert set(states) == {"running"}
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["outcome"] == "timeout"
    assert document["prompts_sent"] == 1
    assert document["reason"].startswith("timed out")
    assert loaded.read_json("service-readback.json")["work_item"]["state"] == "running"
    with pytest.raises(EvidenceSealedError):
        writer.append_jsonl("rpc-transcript.jsonl", {"direction": "out", "frame": {}})
