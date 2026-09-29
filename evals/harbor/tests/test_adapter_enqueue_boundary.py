"""f2 crash injection arms on the /execute prompt and fires at the queued outbox."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.adapter import kill_process_group

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute OMP-246"
SESSION_ID = "sess-1"

HEADER = b'{"type":"session","id":"sess-1"}\n'
PENDING = (
    b'{"type":"custom","customType":"work-now-execute-outbox",'
    b'"data":{"status":"pending","grantId":"g1"}}\n'
)
QUEUED = (
    b'{"type":"custom","customType":"work-now-execute-outbox",'
    b'"data":{"status":"queued","grantId":"g1"}}\n'
)


def _write_fixture(root: Path, scenario: dict) -> object:
    directory = root / "f2"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f2",
        "scored_experiment": "repair",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f2")


def _open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def _prompt_seen(record: Path) -> bool:
    path = record / "commands.jsonl"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        command = json.loads(line)["command"]
        if command.get("type") == "prompt" and command.get("message") == COMMAND:
            return True
    return False


def _wait_until(predicate, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for the enqueue-boundary condition")
        time.sleep(0.02)


def test_f2_scenario_arms_at_enqueue() -> None:
    fixture = load_fixture(FIXTURES, "f2")
    assert fixture.scenario.kill_at is not None
    assert fixture.scenario.kill_at.boundary == "enqueue"
    assert fixture.scenario.kill_at.match == {"type": "prompt", "message": COMMAND}


def test_kill_at_boundary_must_be_enqueue(tmp_path: Path) -> None:
    scenario = {
        "command": COMMAND,
        "terminal": {"pointer": "/execution/grant/state", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "kill_at": {"match": {"type": "prompt", "message": COMMAND}, "boundary": "prompt"},
        "timeout_s": 1,
    }
    with pytest.raises(ValueError, match="enqueue"):
        _write_fixture(tmp_path / "fixtures", scenario)


def test_killer_waits_for_the_queued_execute_outbox(tmp_path: Path) -> None:
    """The /execute prompt arms the crash. pending does not fire it. queued fires it once."""

    scenario = {
        "command": COMMAND,
        "terminal": {"pointer": "/execution/grant/state", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "kill_at": {"match": {"type": "prompt", "message": COMMAND}, "boundary": "enqueue"},
        "timeout_s": 8,
    }
    fixture = _write_fixture(tmp_path / "fixtures", scenario)
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
        "--ui-requests",
        str(tmp_path / "ui-requests.json"),
    ]
    (tmp_path / "ui-requests.json").write_text("[]\n", encoding="utf-8")

    body = {"value": HEADER}
    returned: list[bytes] = []
    paths: list[str] = []
    returned_lock = threading.Lock()
    calls: list[int] = []
    grant_state = {"value": "active"}

    def session_reader(path: str) -> bytes:
        payload = body["value"]
        with returned_lock:
            paths.append(path)
            returned.append(payload)
        return payload

    def saw(payload: bytes) -> bool:
        with returned_lock:
            return payload in returned

    def killer(process) -> None:
        calls.append(process.pid)
        grant_state["value"] = "completed"
        kill_process_group(process)

    def execution() -> dict:
        return {
            "grant": {"state": grant_state["value"]},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        }

    def work_item() -> dict:
        return {"work_id": WORK_ID, "state": "running"}

    holder: dict[str, object] = {}

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=work_item,
    ) as service:
        adapter = RpcAdapter(
            command,
            tmp_path,
            None,
            ServiceProbe(service.base_url, BEARER, WORKSPACE),
            writer,
            fixture.scenario,
            killer=killer,
            session_reader=session_reader,
        )

        def run() -> None:
            try:
                holder["outcome"] = adapter.run()
            except Exception as exc:  # noqa: BLE001 - the assertion below reports it
                holder["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        try:
            _wait_until(lambda: _prompt_seen(record), 5)
            _wait_until(lambda: saw(HEADER), 5)
            assert calls == []
            assert paths == [str(session_file)] * len(paths)
            body["value"] = HEADER + PENDING
            _wait_until(lambda: saw(HEADER + PENDING), 5)
            assert calls == []
            body["value"] = HEADER + PENDING + QUEUED
            _wait_until(lambda: len(calls) == 1, 5)
        finally:
            thread.join(12)

    assert "error" not in holder, holder.get("error")
    assert not thread.is_alive()
    assert holder["outcome"] == "completed"
    assert calls and len(calls) == 1
    loaded = _open_sealed(evidence_dir)
    prompts = [
        frame
        for row in loaded.read_jsonl("rpc-transcript.jsonl")
        if row.get("direction") == "out" and isinstance(row.get("frame"), dict)
        for frame in [row["frame"]]
        if frame.get("type") == "prompt"
    ]
    assert [frame.get("message") for frame in prompts] == [COMMAND]
    assert loaded.read_json("outcome.json")["prompts_sent"] == 1
    assert loaded.read_json("session.json")["restarts"] == [{"id": SESSION_ID, "file": str(session_file)}]
