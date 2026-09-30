"""OMP-405-s05: the ``omp-work stop`` CLI and the client credential class.

Without a database. The three contracts defended here:

- ``stop check`` maps the workspace stop state to the systemd ``ExecCondition``
  exit codes — 0 running, 1 stopped, 255 when the service cannot be reached;
- ``stop engage`` submits a fresh ``engage_stop`` envelope carrying the reason;
- ``omp-work ops capabilities client --name N [--stop-only]`` mints a mode-0600
  capability whose ``actor_kind`` is ``client`` — ``work.read``/``work.client``/
  ``work.stop`` by default, ``work.stop`` alone with ``--stop-only`` — and that
  token is accepted by the real WorkService: it engages the stop at the store
  and reads the stop status, while ``release_stop`` is refused 403.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.__main__ import main
from omp_work.operations import stop as stop_ops
from omp_work.operations.capabilities import write_client_config
from omp_work.operations.config import OperationsConfig
from omp_work.v1.client import WorkClient
from omp_work.v1.models import OperationReceipt, OperationState
from omp_work.v1.server import create_app

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
Handler = Callable[[httpx.Request], httpx.Response]


def _bearer(tmp_path: Path, token: str = "stop-token") -> Path:
    path = tmp_path / "bearer.json"
    path.write_text(json.dumps({"token": token}))
    path.chmod(0o600)
    return path


def _client(
    tmp_path: Path, handler: Handler, bearer: Path | None = None
) -> WorkClient:
    return WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        bearer or _bearer(tmp_path),
        transport=httpx.MockTransport(handler),
    )


def _status_body(stopped: bool) -> dict[str, object]:
    return {
        "workspace_id": str(WORKSPACE),
        "stopped": stopped,
        "reason": "runaway loop" if stopped else None,
        "changed_at": "2026-09-29T00:00:00+00:00" if stopped else None,
        "changed_by_actor_kind": "grokbot" if stopped else None,
    }


def _status_handler(stopped: bool) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(stopped))

    return handler


def test_stop_status_reads_the_workspace_view(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_status_body(True))

    view = _client(tmp_path, handler).stop_status()
    assert seen == [f"/v1/workspaces/{WORKSPACE}/stop"]
    assert view.stopped is True
    assert view.reason == "runaway loop"
    assert view.changed_by_actor_kind == "grokbot"


def test_check_exit_codes(tmp_path: Path) -> None:
    assert stop_ops.check(_client(tmp_path, _status_handler(False))) == 0
    assert stop_ops.check(_client(tmp_path, _status_handler(True))) == 1

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down", request=request)

    assert stop_ops.check(_client(tmp_path, unreachable)) == 255


def test_stop_cli_resolves_credentials_and_maps_condition_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    bearer = _bearer(tmp_path)
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    write_client_config(
        config,
        workspace_id=WORKSPACE,
        owner_id=uuid4(),
        base_url="http://127.0.0.1:54322",
        bearer_file=bearer,
    )

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down", request=request)

    resolved: list[tuple[Path, Path | None]] = []

    def fake_load(client_config: Path, bearer_file: Path | None = None):
        resolved.append((client_config, bearer_file))
        return _client(tmp_path, unreachable, bearer_file or bearer), WORKSPACE

    monkeypatch.setattr(stop_ops, "load_client", fake_load)

    # No flags: the shared XDG client config, and an unreachable service is 255.
    assert main(["stop", "check"]) == 255
    assert resolved[-1][0] == tmp_path / "omp-work" / "client.json"
    assert resolved[-1][1] is None
    assert "stop:" in capsys.readouterr().err

    # --client-config and --bearer-file override both paths.
    other = tmp_path / "other" / "client.json"
    override_bearer = tmp_path / "override.json"
    assert (
        main(
            [
                "stop",
                "check",
                "--client-config",
                str(other),
                "--bearer-file",
                str(override_bearer),
            ]
        )
        == 255
    )
    assert resolved[-1] == (other, override_bearer)


def test_stop_cli_prints_status_and_engage_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json=_status_body(True))
        body = json.loads(request.content)
        commands.append(body)
        return httpx.Response(
            200,
            json={
                "receipt": {
                    "operation_id": body["operation_id"],
                    "request_id": body["request_id"],
                    "state": "applied",
                    "request_sha256": "0" * 64,
                    "result_sha256": "1" * 64,
                },
                "result": {
                    "type": "engage_stop",
                    "stopped": True,
                    "reason": body["command"]["payload"]["reason"],
                },
            },
        )

    monkeypatch.setattr(
        stop_ops,
        "load_client",
        lambda client_config, bearer_file=None: (
            _client(tmp_path, handler),
            WORKSPACE,
        ),
    )

    assert main(["stop", "status"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["stopped"] is True
    assert view["reason"] == "runaway loop"

    assert main(["stop", "engage", "--reason", "runaway loop"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["result"] == {
        "type": "engage_stop",
        "stopped": True,
        "reason": "runaway loop",
    }
    assert commands[0]["command"]["payload"] == {"reason": "runaway loop"}


def test_engage_and_release_post_their_envelopes(tmp_path: Path) -> None:
    commands: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/commands"
        body = json.loads(request.content)
        commands.append(body)
        return httpx.Response(
            200,
            json={
                "receipt": {
                    "operation_id": body["operation_id"],
                    "request_id": body["request_id"],
                    "state": "applied",
                    "request_sha256": "0" * 64,
                    "result_sha256": "1" * 64,
                },
                "result": {
                    "type": body["command"]["type"],
                    "stopped": body["command"]["type"] == "engage_stop",
                    "reason": body["command"]["payload"]["reason"],
                },
            },
        )

    client = _client(tmp_path, handler)
    engaged = stop_ops.engage(client, WORKSPACE, "runaway loop")
    released = stop_ops.release(client, WORKSPACE, "all clear")
    assert engaged.result.type == "engage_stop"
    assert released.result.type == "release_stop"

    engage, release = commands
    assert engage["api_version"] == "work.omp.dev/v1"
    assert engage["workspace_id"] == str(WORKSPACE)
    assert engage["command"] == {
        "type": "engage_stop",
        "payload": {"reason": "runaway loop"},
    }
    assert release["command"] == {
        "type": "release_stop",
        "payload": {"reason": "all clear"},
    }
    # Fresh identifiers per submission.
    assert engage["operation_id"] != engage["request_id"]
    assert engage["operation_id"] != engage["correlation_id"]


def _capability_path(xdg: Path, name: str) -> Path:
    return xdg / "omp" / "work-ledger" / "capabilities" / f"{name}.json"


class _RecordingStore:
    """Fake WorkStore: records the command and derives the stop status."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.stopped = False
        self.reason: str | None = None

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
            }
        )
        self.stopped = envelope.command.type == "engage_stop"
        self.reason = envelope.command.payload.reason
        return (
            OperationReceipt(
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                state=OperationState.APPLIED,
                request_sha256="0" * 64,
                result_sha256="1" * 64,
            ),
            {
                "type": envelope.command.type,
                "stopped": self.stopped,
                "reason": self.reason,
            },
        )

    def stop_status(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        return {
            "workspace_id": str(workspace_id),
            "stopped": self.stopped,
            "reason": self.reason,
            "changed_at": "2026-09-29T00:00:00+00:00" if self.reason else None,
            "changed_by_actor_kind": "grokbot" if self.reason else None,
        }


def test_client_capability_file_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    workspace_id = uuid4()

    # The default client reads the ledger and engages the stop.
    assert (
        main(["ops", "capabilities", "client", "--name", "client", "--workspace-id", str(workspace_id)])
        == 0
    )
    path = _capability_path(tmp_path, "client")
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert data["actor_kind"] == "client"
    assert data["scopes"] == ["work.client", "work.read", "work.stop"]
    assert data["workspaces"] == [str(workspace_id)]
    assert data["token"] and data["actor_id"]

    # --stop-only narrows the same principal to work.stop alone.
    assert (
        main(
            [
                "ops",
                "capabilities",
                "client",
                "--name",
                "stop-watch",
                "--stop-only",
                "--workspace-id",
                str(workspace_id),
            ]
        )
        == 0
    )
    stop_only = json.loads(_capability_path(tmp_path, "stop-watch").read_text())
    assert stop_only["actor_kind"] == "client"
    assert stop_only["scopes"] == ["work.stop"]
    assert stop_only["workspaces"] == [str(workspace_id)]
    assert stop_only["token"] and stop_only["actor_id"]


def _envelope(command_type: str, reason: str) -> dict[str, object]:
    return {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORKSPACE),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": command_type, "payload": {"reason": reason}},
    }


def test_client_bearer_engages_and_reads_but_cannot_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert (
        main(
            [
                "ops",
                "capabilities",
                "client",
                "--name",
                "stop-watch",
                "--stop-only",
                "--workspace-id",
                str(WORKSPACE),
            ]
        )
        == 0
    )
    capabilities = _capability_path(tmp_path, "stop-watch").parent
    token = json.loads(_capability_path(tmp_path, "stop-watch").read_text())["token"]

    store = _RecordingStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    client = TestClient(
        create_app(config, capabilities_dir=capabilities, store=store)  # type: ignore[arg-type]
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-OMP-Workspace-ID": str(WORKSPACE),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    engaged = client.post(
        "/v1/commands", headers=headers, json=_envelope("engage_stop", "runaway loop")
    )
    assert engaged.status_code == 200
    assert store.calls == [
        {
            "command_type": "engage_stop",
            "actor_kind": "client",
            "required_scope": "work.stop",
        }
    ]

    status = client.get(f"/v1/workspaces/{WORKSPACE}/stop", headers=headers)
    assert status.status_code == 200
    assert status.json()["stopped"] is True
    assert status.json()["reason"] == "runaway loop"

    refused = client.post(
        "/v1/commands", headers=headers, json=_envelope("release_stop", "all clear")
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert len(store.calls) == 1
