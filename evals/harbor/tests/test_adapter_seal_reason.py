"""Test that RpcAdapter._seal preserves the initial failure reason when session_reader fails."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from omp_harbor_eval import (
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


class UnreadyServiceProbe(ServiceProbe):
    """ServiceProbe whose ready() returns False."""

    def ready(self) -> bool:
        return False


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


def test_session_reader_failure_preserves_initial_harness_error(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixtures")
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
    probe = UnreadyServiceProbe("http://127.0.0.1:9", BEARER, WORKSPACE)

    def failing_reader(_path: str) -> bytes:
        raise RuntimeError("cat: x.jsonl: No such file or directory")

    adapter = RpcAdapter(
        command,
        tmp_path,
        None,
        probe,
        writer,
        fixture.scenario,
        session_reader=failing_reader,
    )
    outcome = adapter.run()
    assert outcome == "harness_error"

    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": "service not ready",
        "prompts_sent": 0,
        "session_read_error": "session_reader: cat: x.jsonl: No such file or directory",
    }
