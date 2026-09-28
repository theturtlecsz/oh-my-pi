"""RpcAdapter waits for the work service and keeps the last probe failure."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from omp_harbor_eval import EvidenceWriter, ProbeError, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.netns import SCRIPT

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute"
SESSION_ID = "sess-1"
REFUSED = "/v1/health/ready unavailable: [Errno 111] Connection refused"
NOT_READY = {"alerts": ["not_ready"], "live": True, "ready": False}


def write_fixture(root: Path, *, timeout_s: float = 3.0) -> object:
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


def open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def rpc_command(record: Path, session_file: Path, evidence: Path) -> list[str]:
    return [
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


def prompt_count(record: Path) -> int:
    text = (record / "commands.jsonl").read_text(encoding="utf-8")
    commands = [json.loads(line)["command"] for line in text.splitlines() if line]
    return sum(command["type"] == "prompt" for command in commands)


class _TerminalProbe(ServiceProbe):
    """Ready probe with a canned terminal readback, so a sent prompt can finish."""

    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9", BEARER, WORKSPACE)
        self.ready_calls = 0

    def _get(self, path: str, *, auth: bool) -> dict[str, Any]:
        if path == "/v1/health/ready":
            return {"live": True, "ready": True}
        if path.endswith("/execution"):
            return {
                "grant": {"state": "active"},
                "items": [{"work_id": WORK_ID, "phase": "active"}],
                "active_item": {"work_id": WORK_ID, "phase": "active"},
            }
        if path.startswith("/v1/work-items/"):
            return {"work_id": WORK_ID, "state": "completed"}
        raise ProbeError(f"unhandled canned path: {path}")


class TwiceThenReady(_TerminalProbe):
    """Two refused reads, then ready."""

    def ready(self) -> bool:
        self.ready_calls += 1
        if self.ready_calls < 3:
            self.last_failure = REFUSED
            return False
        self.last_failure = None
        return True


class ClosedPortProbe(ServiceProbe):
    """Real loopback probe aimed at a port nothing is listening on."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, BEARER, WORKSPACE)
        self.ready_calls = 0

    def ready(self) -> bool:
        self.ready_calls += 1
        return super().ready()


class NotReadyDocument(_TerminalProbe):
    """The health route answers, and the document says the service is not ready."""

    def ready(self) -> bool:
        self.ready_calls += 1
        self.last_failure = json.dumps(NOT_READY, sort_keys=True)
        return False


def run_adapter(tmp_path: Path, probe: ServiceProbe, *, ready_timeout_s: float) -> tuple[str, Path, Path]:
    fixture = write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    adapter = RpcAdapter(
        rpc_command(record, session_file, evidence_dir),
        tmp_path,
        None,
        probe,
        writer,
        fixture.scenario,
        ready_timeout_s=ready_timeout_s,
    )
    return adapter.run(), evidence_dir, record


def test_prompt_is_sent_after_two_probe_failures(tmp_path: Path) -> None:
    probe = TwiceThenReady()
    outcome, evidence_dir, record = run_adapter(tmp_path, probe, ready_timeout_s=2.0)

    assert probe.ready_calls == 3
    assert outcome == "completed"
    assert prompt_count(record) == 1
    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}


def test_deadline_records_connection_refused(tmp_path: Path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    probe = ClosedPortProbe(f"http://127.0.0.1:{port}")

    outcome, evidence_dir, record = run_adapter(tmp_path, probe, ready_timeout_s=0.25)

    assert outcome == "harness_error"
    assert probe.ready_calls >= 2
    assert prompt_count(record) == 0
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["outcome"] == "harness_error"
    assert document["prompts_sent"] == 0
    assert document["reason"].startswith("service not ready: ")
    assert "Connection refused" in document["reason"]


def test_deadline_records_the_ready_document(tmp_path: Path) -> None:
    probe = NotReadyDocument()
    outcome, evidence_dir, record = run_adapter(tmp_path, probe, ready_timeout_s=0.25)

    assert outcome == "harness_error"
    assert probe.ready_calls >= 2
    assert prompt_count(record) == 0
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["prompts_sent"] == 0
    assert document["reason"].startswith("service not ready: ")
    assert json.dumps(NOT_READY, sort_keys=True) in document["reason"]


def test_worker_probe_script_reports_connection_refused() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, f"http://127.0.0.1:{port}/v1/health/ready"],
        input=b"",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    assert b"Connection refused" in completed.stderr
