"""Authenticated readback sends the generated contract digest, and a timeout keeps the probe error."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fake_workservice import FakeWorkService
from omp_harbor_eval import EvidenceWriter, ProbeError, RpcAdapter, ServiceProbe, load_evidence, load_fixture
from omp_harbor_eval.netns import ExecProbe

FAKE_RPC = Path(__file__).with_name("fake_omp_rpc.py")
DOCKER = Path(__file__).with_name("fake_docker.py")
CONTRACT_TS = Path(__file__).resolve().parents[3] / "packages" / "work-client" / "src" / "contract.ts"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
BEARER = "harbor-test-token"
COMMAND = "/execute"
SESSION_ID = "sess-1"
EXECUTION = f"/v1/workspaces/{urllib.parse.quote(WORKSPACE, safe='')}/execution"
WORK_ITEM = f"/v1/work-items/{urllib.parse.quote(WORK_ID, safe='')}"
_DIGEST_RE = re.compile(r'export const WORK_CONTRACT_SHA256 = "([0-9a-f]{64})"')


def generated_digest() -> str:
    match = _DIGEST_RE.search(CONTRACT_TS.read_text(encoding="utf-8"))
    assert match is not None, f"generated contract has no WORK_CONTRACT_SHA256: {CONTRACT_TS}"
    return match.group(1)


def _mismatch(digest: str, host: str | None) -> dict[str, Any]:
    return {
        "error": {
            "code": "contract_mismatch",
            "request_id": None,
            "correlation_id": None,
            "diagnostics": [
                f"host contract digest: {host or 'missing'}",
                f"service contract digest: {digest}",
                "restart the OMP session",
            ],
        }
    }


class ContractGate:
    """Loopback WorkService that refuses an authenticated read with the wrong digest."""

    def __init__(self, digest: str, *, refuse_execution: bool = False) -> None:
        self.digest = digest
        self.refuse_execution = refuse_execution
        self.calls: list[dict[str, Any]] = []
        self.base_url = ""
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ContractGate:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def start(self) -> str:
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                gate._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._httpd = Server(("127.0.0.1", 0), Handler)
        port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="contract-gate", daemon=True)
        self._thread.start()
        return self.base_url

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        contract = handler.headers.get("x-omp-contract-sha256")
        status, document = self._dispatch(path, handler, contract)
        with self._lock:
            self.calls.append({"path": path, "status": status, "contract": contract})
        body = json.dumps(document).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _dispatch(
        self, path: str, handler: BaseHTTPRequestHandler, contract: str | None
    ) -> tuple[int, dict[str, Any]]:
        if path == "/v1/health/ready":
            return 200, {"live": True, "ready": True, "alerts": []}
        if path not in {EXECUTION, WORK_ITEM}:
            return 404, {"error": {"code": "not_found"}}
        authorization = handler.headers.get("Authorization", "")
        workspace = handler.headers.get("X-OMP-Workspace-ID", "")
        if authorization != f"Bearer {BEARER}" or workspace != WORKSPACE:
            return 401, {"error": {"code": "unauthenticated"}}
        if path == EXECUTION and self.refuse_execution:
            return 409, _mismatch(self.digest, contract)
        if contract != self.digest:
            return 409, _mismatch(self.digest, contract)
        if path == EXECUTION:
            return 200, {
                "grant": {"state": "active"},
                "items": [{"work_id": WORK_ID, "phase": "active"}],
                "active_item": {"work_id": WORK_ID, "phase": "active"},
            }
        return 200, {"work_id": WORK_ID, "state": "completed"}


def _open(url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    document = json.loads(raw.decode("utf-8"))
    assert isinstance(document, dict)
    return status, document


def _assert_probe_calls(calls: list[dict[str, Any]], digest: str) -> None:
    assert [call["path"] for call in calls] == ["/v1/health/ready", EXECUTION, WORK_ITEM]
    assert calls[0]["contract"] is None
    assert calls[1]["contract"] == digest
    assert calls[2]["contract"] == digest
    assert calls[1]["status"] == 200
    assert calls[2]["status"] == 200


def _views() -> dict[str, Any]:
    return {
        "health": {"live": True, "ready": True, "alerts": []},
        "execution": {
            "grant": {"state": "active"},
            "items": [{"work_id": WORK_ID, "phase": "active"}],
            "active_item": {"work_id": WORK_ID, "phase": "active"},
        },
        "work_item": {"work_id": WORK_ID, "state": "completed"},
    }


def test_reads_fail_without_the_contract_header_and_pass_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = generated_digest()
    fake_dir = tmp_path / "docker"
    fake_dir.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(fake_dir))
    (fake_dir / "config.json").write_text(
        json.dumps({"containers": {}, "fail": [], "omp": [], "responses": {}}), encoding="utf-8"
    )

    with ContractGate(digest) as gate:
        status, body = _open(
            gate.base_url + EXECUTION,
            {"Accept": "application/json", "Authorization": f"Bearer {BEARER}", "X-OMP-Workspace-ID": WORKSPACE},
        )
        assert status == 409
        assert body["error"]["code"] == "contract_mismatch"
        assert gate.calls[-1]["contract"] is None

        service = ServiceProbe(gate.base_url, BEARER, WORKSPACE)
        assert service.read() == _views()
        _assert_probe_calls(gate.calls[-3:], digest)

        executed = ExecProbe(gate.base_url, BEARER, WORKSPACE, "w1", docker=str(DOCKER))
        assert executed.read() == _views()
        _assert_probe_calls(gate.calls[-3:], digest)

        before = len(gate.calls)
        outcome, evidence_dir = _run(tmp_path, ServiceProbe(gate.base_url, BEARER, WORKSPACE), timeout_s=2)
        assert outcome == "completed"
        for call in gate.calls[before:]:
            if call["path"] == "/v1/health/ready":
                assert call["contract"] is None
            else:
                assert call["contract"] == digest
                assert call["status"] == 200

    loaded = open_sealed(evidence_dir)
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
    assert loaded.read_json("service-readback.json")["work_item"]["state"] == "completed"
    text = "\n".join(" ".join(argv) for argv in _exec_argvs(fake_dir))
    assert digest not in text
    assert BEARER not in text


def test_timeout_reason_includes_the_last_probe_error(tmp_path: Path) -> None:
    digest = generated_digest()
    pointer = f"/v1/workspaces/{WORKSPACE}/execution returned 409 contract_mismatch"
    with ContractGate(digest, refuse_execution=True) as gate:
        probe = ServiceProbe(gate.base_url, BEARER, WORKSPACE)
        with pytest.raises(ProbeError, match=re.escape(pointer)):
            probe.read()
        outcome, evidence_dir = _run(tmp_path, ServiceProbe(gate.base_url, BEARER, WORKSPACE), timeout_s=0.5)
        assert all(call["contract"] == digest for call in gate.calls if call["path"] == EXECUTION)

    assert outcome == "timeout"
    loaded = open_sealed(evidence_dir)
    document = loaded.read_json("outcome.json")
    assert document["outcome"] == "timeout"
    assert document["prompts_sent"] == 1
    assert document["reason"].startswith("timed out after 0.5s waiting for /work_item/state")
    assert f"last probe error: {pointer}" in document["reason"]
    assert loaded.read_json("service-readback.json") == {}


def test_timeout_without_a_probe_failure_omits_the_probe_clause(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures", timeout_s=0.5)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"

    def execution() -> dict[str, Any]:
        return {"grant": {"state": "active"}, "active_item": {"work_id": WORK_ID}}

    with FakeWorkService(
        bearer=BEARER,
        workspace_id=WORKSPACE,
        ready=True,
        execution=execution,
        work_item=lambda: {"work_id": WORK_ID, "state": "running"},
    ) as service:
        outcome = RpcAdapter(
            _rpc_command(record, session_file, evidence_dir),
            tmp_path,
            None,
            ServiceProbe(service.base_url, BEARER, WORKSPACE),
            writer,
            fixture.scenario,
        ).run()

    assert outcome == "timeout"
    reason = open_sealed(evidence_dir).read_json("outcome.json")["reason"]
    assert reason.startswith("timed out after 0.5s waiting for /work_item/state")
    assert "last probe error:" not in reason


def _run(tmp_path: Path, probe: ServiceProbe, *, timeout_s: float) -> tuple[str, Path]:
    fixture = _write_fixture(tmp_path / "fixtures", timeout_s=timeout_s)
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    record = tmp_path / "rpc-record"
    session_file = tmp_path / "session" / "session.jsonl"
    outcome = RpcAdapter(
        _rpc_command(record, session_file, evidence_dir),
        tmp_path,
        None,
        probe,
        writer,
        fixture.scenario,
    ).run()
    return outcome, evidence_dir


def _write_fixture(root: Path, *, timeout_s: float) -> Any:
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


def _rpc_command(record: Path, session_file: Path, evidence: Path) -> list[str]:
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


def open_sealed(directory: Path) -> Any:
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def _exec_argvs(fake_dir: Path) -> list[list[str]]:
    path = fake_dir / "calls.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    argv_rows: list[list[str]] = []
    for row in rows:
        argv = row.get("argv")
        if isinstance(argv, list) and len(argv) > 1 and argv[1] == "exec":
            argv_rows.append(argv)
    return argv_rows
