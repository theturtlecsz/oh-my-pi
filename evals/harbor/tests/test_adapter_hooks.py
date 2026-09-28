"""Tests for RpcAdapter session_reader and before_seal hooks."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from omp_harbor_eval import (
    EvidenceWriter,
    ProbeError,
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


class CannedServiceProbe(ServiceProbe):
    """Loopback probe whose _get returns canned ready and terminal views without binding sockets."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9",
        bearer: str = BEARER,
        workspace_id: str = WORKSPACE,
        work_id: str = WORK_ID,
        terminal_state: str = "completed",
    ) -> None:
        super().__init__(base_url, bearer, workspace_id)
        self.work_id = work_id
        self.terminal_state = terminal_state

    def _get(self, path: str, *, auth: bool) -> dict[str, Any]:
        if path == "/v1/health/ready":
            return {"ready": True}
        if path == f"/v1/workspaces/{urllib.parse.quote(self.workspace_id, safe='')}/execution":
            return {
                "grant": {"state": "active"},
                "items": [{"work_id": self.work_id, "phase": "active"}],
                "active_item": {"work_id": self.work_id, "phase": "active"},
            }
        if path == f"/v1/work-items/{urllib.parse.quote(self.work_id, safe='')}":
            return {"work_id": self.work_id, "state": self.terminal_state}
        raise ProbeError(f"unhandled canned path: {path}")


def write_fixture(root: Path, *, timeout_s: float = 3.0, accepted: list[str] | None = None) -> object:
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
        "terminal": {"pointer": "/work_item/state", "in": ["completed"] if accepted is None else accepted},
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


def assert_manifest_at_eof(record: Path) -> None:
    assert json.loads((record / "eof.json").read_text(encoding="utf-8")) == {"manifest_exists": True}


def _drive_hooks(
    tmp_path: Path,
    *,
    session_reader=None,
    before_seal=None,
    terminal_state: str = "completed",
    timeout_s: float = 3.0,
):
    fixture = write_fixture(tmp_path / "fixtures", timeout_s=timeout_s, accepted=[terminal_state])
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
    probe = CannedServiceProbe(terminal_state=terminal_state)
    adapter = RpcAdapter(
        command,
        tmp_path,
        None,
        probe,
        writer,
        fixture.scenario,
        session_reader=session_reader,
        before_seal=before_seal,
    )
    outcome = adapter.run()
    return outcome, writer, evidence_dir, record, session_file


def test_session_reader_receives_get_state_file_and_writes_bytes(tmp_path: Path) -> None:
    called_with: list[str] = []

    def reader(path: str) -> bytes:
        called_with.append(path)
        return b'{"type":"custom_session","id":"sess-custom"}\n'

    outcome, _writer, evidence_dir, record, session_file = _drive_hooks(tmp_path, session_reader=reader)
    assert outcome == "completed"
    assert len(called_with) == 1
    assert called_with[0] == str(session_file)

    loaded = open_sealed(evidence_dir)
    assert loaded.read_bytes("session.jsonl") == b'{"type":"custom_session","id":"sess-custom"}\n'
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    assert_manifest_at_eof(record)


def test_before_seal_adds_file_to_manifest_and_evidence(tmp_path: Path) -> None:
    order_checks: dict[str, bool] = {}

    def hook(writer: EvidenceWriter) -> None:
        order_checks["service_readback_exists"] = (writer.directory / "service-readback.json").is_file()
        order_checks["session_json_exists"] = (writer.directory / "session.json").is_file()
        order_checks["session_jsonl_exists"] = (writer.directory / "session.jsonl").is_file()
        order_checks["outcome_exists"] = (writer.directory / "outcome.json").exists()
        order_checks["manifest_exists"] = (writer.directory / "manifest.json").exists()
        writer.add_file("worker-repo.bundle", b"bundle-binary-content\n")

    outcome, _writer, evidence_dir, record, _session_file = _drive_hooks(tmp_path, before_seal=hook)
    assert outcome == "completed"
    assert order_checks == {
        "service_readback_exists": True,
        "session_json_exists": True,
        "session_jsonl_exists": True,
        "outcome_exists": False,
        "manifest_exists": False,
    }

    loaded = open_sealed(evidence_dir)
    assert "worker-repo.bundle" in loaded.files
    assert loaded.read_bytes("worker-repo.bundle") == b"bundle-binary-content\n"
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    assert_manifest_at_eof(record)


def test_raising_before_seal_gives_harness_error_and_seals_directory(tmp_path: Path) -> None:
    def failing_hook(writer: EvidenceWriter) -> None:
        writer.add_file("worker-repo.bundle", b"bundle-pre-crash\n")
        raise RuntimeError("failed to finalize worker bundle")

    outcome, _writer, evidence_dir, record, _session_file = _drive_hooks(tmp_path, before_seal=failing_hook)
    assert outcome == "harness_error"

    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": "before_seal: failed to finalize worker bundle",
        "prompts_sent": 1,
    }
    assert "worker-repo.bundle" in loaded.files
    assert loaded.read_bytes("worker-repo.bundle") == b"bundle-pre-crash\n"
    assert_manifest_at_eof(record)


def test_raising_session_reader_gives_harness_error_and_seals_directory(tmp_path: Path) -> None:
    before_seal_ran: list[bool] = []

    def failing_reader(path: str) -> bytes:
        raise RuntimeError("container read timeout")

    def hook(writer: EvidenceWriter) -> None:
        before_seal_ran.append(True)

    outcome, _writer, evidence_dir, record, _session_file = _drive_hooks(
        tmp_path,
        session_reader=failing_reader,
        before_seal=hook,
    )
    assert outcome == "harness_error"
    assert not before_seal_ran

    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": "session_reader: container read timeout",
        "prompts_sent": 1,
    }
    assert loaded.read_bytes("session.jsonl") == b""
    assert_manifest_at_eof(record)


def test_default_hooks_preserve_host_session_read(tmp_path: Path) -> None:
    outcome, _writer, evidence_dir, record, session_file = _drive_hooks(tmp_path)
    assert outcome == "completed"

    loaded = open_sealed(evidence_dir)
    assert loaded.read_bytes("session.jsonl") == session_file.read_bytes()
    assert "worker-repo.bundle" not in loaded.files
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    assert_manifest_at_eof(record)
