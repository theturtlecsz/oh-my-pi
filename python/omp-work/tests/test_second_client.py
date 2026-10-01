"""OMP-424-s01: contract tests for stdlib-only second_client fixture.

Exercises:
1. `python -I -S second_client.py operations` prints the 17 client_contract names;
2. inbox accepts a body signed with event_push.signature and subscription_key,
   rejects a changed body or another subscription's key;
3. --dry-run bodies of submit, confirm, answer, pause, subscribe validate as
   DraftMissionIntakePayload, RelayOwnerIntentPayload, CommandEnvelope; paths
   match the contract.
4. HTTP identity and command operations over loopback HTTP.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from omp_work import contract_sha256, event_push, load_contract
from omp_work.v1.models import (
    CommandEnvelope,
    DraftMissionIntakePayload,
    RelayOwnerIntentPayload,
)

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "second_client.py"


def _run_client(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-I", "-S", str(_FIXTURE_PATH), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def test_operations_prints_17_client_contract_names() -> None:
    """1. `python -I -S second_client.py operations` prints the 17 client_contract names."""
    res = _run_client("operations")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert isinstance(data, dict)
    assert "operations" in data

    contract = load_contract()
    expected_ops = [op.name for op in contract.client_contract.operations]
    assert len(expected_ops) == 17
    assert data["operations"] == expected_ops
    assert data["count"] == 17


def test_inbox_verifies_valid_signature_and_rejects_tampered_or_wrong_key(
    tmp_path: Path,
) -> None:
    """2. inbox accepts a body signed with event_push.signature and subscription_key,

    rejects a changed body or another subscription's key.
    """
    master_key = bytes(range(32))
    sub_a = uuid4()
    sub_b = uuid4()

    key_a = event_push.subscription_key(master_key, sub_a)
    key_b = event_push.subscription_key(master_key, sub_b)

    event_id = str(uuid4())
    body = {
        "mission_event_id": event_id,
        "type": "mission.started",
        "data": {"statement": "Initial start"},
    }

    sig_a = event_push.signature(key_a, event_id, body)
    sig_b = event_push.signature(key_b, event_id, body)

    # 1. Valid push with key A
    pushes_file = tmp_path / "pushes.json"
    valid_record = {
        "headers": {"X-OMP-Signature": sig_a, "Idempotency-Key": event_id},
        "body": body,
    }
    pushes_file.write_text(json.dumps([valid_record]))

    res = _run_client("inbox", "--push-key", key_a.hex(), "--pushes", str(pushes_file))
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["status"] == "accepted"
    assert len(out["accepted"]) == 1
    assert len(out["rejected"]) == 0
    assert out["accepted"][0]["body"] == body

    # 2. Tampered body with key A's signature (should be rejected)
    tampered_record = {
        "headers": {"X-OMP-Signature": sig_a, "Idempotency-Key": event_id},
        "body": dict(body, data={"statement": "Tampered content"}),
    }
    pushes_file.write_text(json.dumps([tampered_record]))
    res = _run_client("inbox", "--push-key", key_a.hex(), "--pushes", str(pushes_file))
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["status"] == "rejected"
    assert len(out["accepted"]) == 0
    assert len(out["rejected"]) == 1

    # 3. Signed with another subscription's key (key B), verified with key A (should be rejected)
    wrong_key_record = {
        "headers": {"X-OMP-Signature": sig_b, "Idempotency-Key": event_id},
        "body": body,
    }
    pushes_file.write_text(json.dumps([wrong_key_record]))
    res = _run_client("inbox", "--push-key", key_a.hex(), "--pushes", str(pushes_file))
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["status"] == "rejected"
    assert len(out["accepted"]) == 0
    assert len(out["rejected"]) == 1

    # 4. Mixed batch in one file: 1 valid, 2 invalid
    pushes_file.write_text(
        json.dumps([valid_record, tampered_record, wrong_key_record])
    )
    res = _run_client("inbox", "--push-key", key_a.hex(), "--pushes", str(pushes_file))
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["status"] == "mixed"
    assert len(out["accepted"]) == 1
    assert len(out["rejected"]) == 2
    assert out["total"] == 3


def test_dry_run_bodies_and_contract_paths() -> None:
    """3. --dry-run bodies of submit, confirm, answer, pause, subscribe validate as

    DraftMissionIntakePayload, RelayOwnerIntentPayload, CommandEnvelope; paths
    match the contract.
    """
    contract = load_contract()
    ops = {op.name: op for op in contract.client_contract.operations}

    ws = str(uuid4())
    proj = str(uuid4())
    mission = str(uuid4())
    decision = str(uuid4())
    sub_id = str(uuid4())

    common_flags = [
        "--dry-run",
        "--project-id",
        proj,
        "--mission-id",
        mission,
        "--decision-id",
        decision,
        "--revision",
        "2",
        "--answer",
        "proceed-with-plan",
        "--instruction-text",
        "Confirming scope for mission",
        "--message-ref",
        "msg-99",
    ]

    # Submit
    res = _run_client("submit", *common_flags)
    assert res.returncode == 0
    submit_data = json.loads(res.stdout)
    assert submit_data["method"] == "POST"
    expected_submit_path = ops["mission.intake"].path.format(
        workspace_id=submit_data["path"].split("/")[3]
    )
    assert submit_data["path"] == expected_submit_path
    validated_intake = DraftMissionIntakePayload.model_validate(submit_data["body"])
    assert str(validated_intake.mission_id) == mission
    assert str(validated_intake.scope.project_id) == proj

    # Confirm
    res = _run_client("confirm", *common_flags)
    assert res.returncode == 0
    confirm_data = json.loads(res.stdout)
    assert confirm_data["method"] == "POST"
    ws_id = confirm_data["path"].split("/")[3]
    expected_confirm_path = ops["mission.scope.confirm"].path.format(
        workspace_id=ws_id, mission_id=mission
    )
    assert confirm_data["path"] == expected_confirm_path
    validated_confirm = RelayOwnerIntentPayload.model_validate(confirm_data["body"])
    assert validated_confirm.intent == "confirm_scope"
    assert str(validated_confirm.mission_id) == mission
    assert str(validated_confirm.decision_id) == decision
    assert validated_confirm.revision == 2
    assert validated_confirm.instruction.owner_authored is True
    assert validated_confirm.instruction.text == "Confirming scope for mission"

    # Answer
    res = _run_client("answer", *common_flags)
    assert res.returncode == 0
    answer_data = json.loads(res.stdout)
    assert answer_data["method"] == "POST"
    ws_id = answer_data["path"].split("/")[3]
    expected_answer_path = ops["decision.answer"].path.format(
        workspace_id=ws_id, decision_id=decision
    )
    assert answer_data["path"] == expected_answer_path
    validated_answer = RelayOwnerIntentPayload.model_validate(answer_data["body"])
    assert validated_answer.intent == "answer_decision"
    assert str(validated_answer.decision_id) == decision
    assert validated_answer.answer == "proceed-with-plan"
    assert validated_answer.instruction.owner_authored is True

    # Pause
    res = _run_client("pause", *common_flags)
    assert res.returncode == 0
    pause_data = json.loads(res.stdout)
    assert pause_data["method"] == "POST"
    ws_id = pause_data["path"].split("/")[3]
    expected_pause_path = ops["mission.pause"].path.format(
        workspace_id=ws_id, mission_id=mission
    )
    assert pause_data["path"] == expected_pause_path
    validated_pause = RelayOwnerIntentPayload.model_validate(pause_data["body"])
    assert validated_pause.intent == "pause"
    assert str(validated_pause.mission_id) == mission
    assert validated_pause.instruction.owner_authored is True

    # Subscribe
    res = _run_client(
        "subscribe",
        "--dry-run",
        "--subscription-id",
        sub_id,
        "--event-types",
        "mission.started,mission.progressed",
    )
    assert res.returncode == 0
    subscribe_data = json.loads(res.stdout)
    assert subscribe_data["method"] == "POST"
    assert subscribe_data["path"] == "/v1/commands"
    validated_envelope = CommandEnvelope.model_validate(subscribe_data["body"])
    assert validated_envelope.api_version == "work.omp.dev/v1"
    assert validated_envelope.command.type == "put_event_subscription"
    assert str(validated_envelope.command.payload.subscription_id) == sub_id
    assert validated_envelope.command.payload.event_types == (
        "mission.started",
        "mission.progressed",
    )


class _MockWorkServerHandler(http.server.BaseHTTPRequestHandler):
    recorded_requests: ClassVar[list[dict[str, Any]]] = []
    expected_contract_sha256: ClassVar[str] = ""

    def do_GET(self) -> None:
        self.recorded_requests.append(
            {
                "method": "GET",
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        if self.path == "/v1/health/ready":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "live": True,
                        "ready": True,
                        "service_fingerprint": "srv-fp-424",
                        "alerts": [],
                    }
                ).encode("utf-8")
            )
            return

        # Check contract sha256 on other routes
        received_sha = self.headers.get("X-OMP-Contract-SHA256")
        if (
            self.expected_contract_sha256
            and received_sha != self.expected_contract_sha256
        ):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "code": "contract_mismatch",
                            "diagnostics": ["hash refused"],
                        }
                    }
                ).encode("utf-8")
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "outcome": "read",
                    "operation": "project.list",
                    "contract": "client.omp.dev/v1",
                    "result": [{"project_id": "00000000-0000-7000-8000-000000000020"}],
                }
            ).encode("utf-8")
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        parsed_body = json.loads(raw.decode("utf-8")) if raw else {}
        self.recorded_requests.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": parsed_body,
            }
        )
        received_sha = self.headers.get("X-OMP-Contract-SHA256")
        if (
            self.expected_contract_sha256
            and received_sha != self.expected_contract_sha256
        ):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": {"code": "contract_mismatch"}}).encode("utf-8")
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "outcome": "applied",
                    "operation": "mutation",
                    "contract": "client.omp.dev/v1",
                    "result": {"status": "applied"},
                }
            ).encode("utf-8")
        )

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def mock_server():
    _MockWorkServerHandler.recorded_requests = []
    _MockWorkServerHandler.expected_contract_sha256 = contract_sha256()
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockWorkServerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_identity_and_http_roundtrip(tmp_path: Path, mock_server: str) -> None:
    """Identity queries ready + project.list, exiting 1 if contract hash is refused."""
    ws = "00000000-0000-7000-8000-000000000010"
    config_file = tmp_path / "client-config.json"
    valid_sha = contract_sha256()

    config_file.write_text(
        json.dumps(
            {
                "base_url": mock_server,
                "workspace_id": ws,
                "capability": "test-bearer-token",
                "contract_sha256": valid_sha,
            }
        )
    )

    # 1. Identity with valid contract sha256 -> succeeds and prints service_fingerprint and sha256
    res = _run_client("--config", str(config_file), "identity")
    assert res.returncode == 0
    id_data = json.loads(res.stdout)
    assert id_data["service_fingerprint"] == "srv-fp-424"
    assert id_data["contract_sha256"] == valid_sha

    # Verify headers sent
    recorded = _MockWorkServerHandler.recorded_requests
    get_projects = [r for r in recorded if "/client/projects" in r["path"]][0]
    assert get_projects["headers"]["authorization"] == "Bearer test-bearer-token"
    assert get_projects["headers"]["x-omp-contract-sha256"] == valid_sha

    # 2. Identity with refused contract hash -> exits 1
    bad_config_file = tmp_path / "bad-config.json"
    bad_config_file.write_text(
        json.dumps(
            {
                "base_url": mock_server,
                "workspace_id": ws,
                "capability": "test-bearer-token",
                "contract_sha256": "0" * 64,
            }
        )
    )
    res_bad = _run_client("--config", str(bad_config_file), "identity", check=False)
    assert res_bad.returncode == 1
    bad_out = json.loads(res_bad.stdout)
    assert "error" in bad_out

    # 3. HTTP projects read -> succeeds
    res_proj = _run_client("--config", str(config_file), "projects")
    assert res_proj.returncode == 0
    proj_out = json.loads(res_proj.stdout)
    assert proj_out["outcome"] == "read"

    # 4. HTTP confirm mutation -> sends {request_id, payload} in body
    _MockWorkServerHandler.recorded_requests.clear()
    res_conf = _run_client(
        "--config",
        str(config_file),
        "confirm",
        "--mission-id",
        "00000000-0000-7000-8000-000000000030",
        "--decision-id",
        "00000000-0000-7000-8000-000000000041",
    )
    assert res_conf.returncode == 0
    conf_req = [r for r in _MockWorkServerHandler.recorded_requests if r["method"] == "POST"][0]
    assert "request_id" in conf_req["body"]
    assert "payload" in conf_req["body"]
    assert conf_req["body"]["payload"]["intent"] == "confirm_scope"
