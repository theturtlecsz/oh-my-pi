"""Tests for the agent_end scenario terminal in fixtures and RpcAdapter."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.fixtures import parse_terminal

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
SESSION_ID = "sess-1"


def _write_agent_end_fixture(root: Path, *, timeout_s: float = 5.0) -> object:
    directory = root / "f_agent_end"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f_agent_end",
        "scored_experiment": "agent_end_test",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    scenario = {
        "command": "/execute OMP-1",
        "terminal": {"agent_end": True},
        "model_script": [],
        "ui_script": [],
        "timeout_s": timeout_s,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f_agent_end")


def _open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def test_parse_terminal_agent_end() -> None:
    t = parse_terminal({"agent_end": True})
    assert t.agent_end is True

    with pytest.raises(ValueError):
        parse_terminal({"agent_end": False})

    with pytest.raises(ValueError):
        parse_terminal({"agent_end": "true"})

    with pytest.raises(ValueError):
        parse_terminal({"agent_end": 1})

    with pytest.raises(ValueError):
        parse_terminal({"agent_end": True, "pointer": "/foo"})

    with pytest.raises(ValueError):
        parse_terminal({"agent_end": True, "extra": "field"})

    # Regular pointer/in
    t_regular = parse_terminal({"pointer": "/status", "in": ["completed", "failed"]})
    assert t_regular.agent_end is False
    assert t_regular.pointer == "/status"
    assert t_regular.in_ == ("completed", "failed")

    with pytest.raises(ValueError):
        parse_terminal({"pointer": "/status"})

    with pytest.raises(ValueError):
        parse_terminal({"in": ["completed"]})


def test_adapter_agent_end_completed(tmp_path: Path) -> None:
    fixture = _write_agent_end_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
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

    probe_count = {"count": 0}

    def work_item() -> dict:
        probe_count["count"] += 1
        return {
            "work_id": WORK_ID,
            "state": "running",
            "phase": "post_agent_end",
        }

    def execution() -> dict:
        return {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID}],
            "active_item": {"work_id": WORK_ID},
        }

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "completed"
    assert probe_count["count"] == 1
    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    readback = loaded.read_json("service-readback.json")
    assert readback["work_item"]["phase"] == "post_agent_end"


def test_adapter_agent_end_probe_error_keeps_last(tmp_path: Path) -> None:
    fixture = _write_agent_end_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
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

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution={"invalid": True},
        work_item={},
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "completed"
    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    assert loaded.read_json("service-readback.json") == {}


def test_adapter_agent_end_pre_turn_error(tmp_path: Path) -> None:
    fixture = _write_agent_end_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events = [
        {"type": "error", "message": "pre-turn failure"},
    ]
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

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution={"grant": {"state": "active"}, "items": [{"work_id": WORK_ID}]},
        work_item={"work_id": WORK_ID},
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "harness_error"
    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json")["outcome"] == "harness_error"
    assert "pre-turn failure" in loaded.read_json("outcome.json")["reason"]


def test_adapter_agent_end_timeout(tmp_path: Path) -> None:
    fixture = _write_agent_end_fixture(tmp_path / "fixtures", timeout_s=0.2)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
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

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution={"grant": {"state": "active"}, "items": [{"work_id": WORK_ID}]},
        work_item={"work_id": WORK_ID},
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()

    assert outcome == "timeout"
    loaded = _open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json")["outcome"] == "timeout"
