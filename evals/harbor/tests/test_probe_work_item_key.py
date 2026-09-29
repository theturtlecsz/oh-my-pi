"""Probes read the work item by its primary alias key, never its work_id.

The real WorkService resolves ``GET /v1/work-items/{key}`` only by primary
alias, so a request carrying the execution view's ``work_id`` (a UUID) answers
400 ``invalid_request`` and the trial never reaches its terminal readback.
These tests build an execution view whose grant ``remote_ref`` names the key
while its items carry a UUID ``work_id``, serve the item only at the key path,
and prove both probe paths request the key and return the document.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.netns import ExecProbe

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-7000-8000-000000000001"
KEY = "OMP-1"
REMOTE_REF = "refs/heads/execution/omp-1-00000000000070008000000000000001"
BEARER = "harbor-test-token"
COMMAND = "/execute OMP-1"
SESSION_ID = "sess-1"
WS = "w1"
BASE_URL = "http://127.0.0.1:8080"


def _execution() -> dict[str, Any]:
    return {
        "grant": {"state": "active", "remote_ref": REMOTE_REF},
        "items": [{"work_id": WORK_ID, "phase": "active"}],
        "active_item": {"work_id": WORK_ID, "phase": "active"},
    }


def _work_item() -> dict[str, Any]:
    return {"work_id": WORK_ID, "state": "in_progress", "revision": {"revision_number": 2}}


class KeyedWorkService(FakeWorkService):
    """``FakeWorkService`` that resolves the item by primary alias, like the real service."""

    def __init__(self, *, key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.key = key

    def _dispatch(self, path: str, authorized: bool, workspace: str) -> tuple[int, dict[str, Any]]:
        prefix = "/v1/work-items/"
        if path.startswith(prefix) and path[len(prefix) :] != self.key:
            if not authorized or workspace != self.workspace_id:
                return 401, {"error": {"code": "unauthenticated"}}
            return 400, {"error": {"code": "invalid_request"}}
        return super()._dispatch(path, authorized, workspace)


def _write_fixture(root: Path) -> object:
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
        "terminal": {"pointer": "/work_item/revision/revision_number", "in": [2]},
        "model_script": [],
        "ui_script": [],
        "timeout_s": 3,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def test_service_probe_reads_the_item_by_key_not_work_id(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    events_path = tmp_path / "events.json"
    events_path.write_text(
        json.dumps([{"type": "agent_start"}, {"type": "agent_end", "messages": [], "isTerminal": True}]),
        encoding="utf-8",
    )
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

    with KeyedWorkService(
        key=KEY,
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=_execution,
        work_item=_work_item,
    ) as service:
        probe = ServiceProbe(service.base_url, BEARER, WORKSPACE)
        outcome = RpcAdapter(command, tmp_path, None, probe, writer, fixture.scenario).run()
        paths = [call["path"] for call in service.calls]

    assert outcome == "completed"
    assert f"/v1/work-items/{KEY}" in paths
    assert not any(WORK_ID in path for path in paths)
    digest = hashlib.sha256((evidence_dir / "manifest.json").read_bytes()).hexdigest()
    loaded = load_evidence(evidence_dir, digest)
    assert loaded.read_json("service-readback.json")["work_item"] == _work_item()


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    _write_config(directory)
    return directory


def test_exec_probe_reads_the_item_by_key_not_work_id(fake_dir: Path) -> None:
    _write_config(
        fake_dir,
        responses={
            WS: {
                "/v1/health/ready": {"status": 200, "body": {"live": True, "ready": True}},
                f"/v1/workspaces/{WORKSPACE}/execution": {"status": 200, "body": _execution()},
                f"/v1/work-items/{KEY}": {"status": 200, "body": _work_item()},
            }
        },
    )
    probe = ExecProbe(BASE_URL, BEARER, WORKSPACE, WS, docker=str(DOCKER))

    readback = probe.read()

    assert readback["work_item"] == _work_item()
    assert [record["argv"][7] for record in _calls(fake_dir)] == [
        f"{BASE_URL}/v1/health/ready",
        f"{BASE_URL}/v1/workspaces/{WORKSPACE}/execution",
        f"{BASE_URL}/v1/work-items/{KEY}",
    ]


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {"containers": {}, "fail": [], "omp": [], "responses": {}}
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _calls(fake_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (fake_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
