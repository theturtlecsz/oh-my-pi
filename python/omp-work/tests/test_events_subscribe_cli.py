"""OMP-503-s01: events subscribe and subscriptions CLI commands.

Defended contracts:
- subscribe sends one POST /v1/commands whose command is put_event_subscription with
  the given subscription_id, push_url, event_types (two --event-type give both, in
  order) and no client_id; exit 0; stdout is the response; load_client got the
  --bearer-file path.
- a 400 with diagnostics push_destination_refused: exit 1, empty stdout, stderr names it.
- missing --push-url exits 2 with no request; load_client raising exits 255.
- subscriptions issues one GET of the workspace's event-subscriptions and prints that page.
- a WorkError during subscribe/subscriptions maps to 1, while the same WorkError during
  push stays on the pre-existing 255 command-failure code.
- client.close() is called on exit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from omp_work.__main__ import main
from omp_work.operations import stop as stop_ops
from omp_work.v1.client import WorkClient, WorkError

WORKSPACE = UUID("00000000-0000-7000-8000-0000000004f0")


def _setup_configs(tmp_path: Path) -> tuple[Path, Path, Path]:
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": "default-token"}))
    bearer.chmod(0o600)

    custom_bearer = tmp_path / "custom_bearer.json"
    custom_bearer.write_text(json.dumps({"token": "custom-token"}))
    custom_bearer.chmod(0o600)

    client_config = tmp_path / "client.json"
    client_config.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:54322",
                "workspace_id": str(WORKSPACE),
                "bearer_file": str(bearer),
            }
        )
    )
    return client_config, bearer, custom_bearer


def test_subscribe_sends_put_event_subscription_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, bearer, custom_bearer = _setup_configs(tmp_path)
    sub_id = uuid4()
    push_url = "https://hooks.example/alerts"

    captured_requests: list[dict[str, Any]] = []
    load_client_calls: list[tuple[Path, Path | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/commands"
        body = json.loads(request.content)
        captured_requests.append(body)
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
                    "type": "put_event_subscription",
                    "subscription": {
                        "subscription_id": str(sub_id),
                        "client_id": str(uuid4()),
                        "push_url": push_url,
                        "event_types": ["mission.started", "mission.completed"],
                        "cursor_sequence": 0,
                        "deleted": False,
                    },
                },
            },
        )

    def load_client(cfg: Path, bf: Path | None = None) -> tuple[WorkClient, UUID]:
        load_client_calls.append((cfg, bf))
        return (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bf or bearer,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        )

    monkeypatch.setattr(stop_ops, "load_client", load_client)

    exit_code = main(
        [
            "events",
            "subscribe",
            "--client-config",
            str(client_config),
            "--bearer-file",
            str(custom_bearer),
            "--subscription-id",
            str(sub_id),
            "--push-url",
            push_url,
            "--event-type",
            "mission.started",
            "--event-type",
            "mission.completed",
        ]
    )

    assert exit_code == 0
    assert load_client_calls == [(client_config, custom_bearer)]
    assert len(captured_requests) == 1

    envelope = captured_requests[0]
    assert envelope["command"]["type"] == "put_event_subscription"
    payload = envelope["command"]["payload"]
    assert payload["subscription_id"] == str(sub_id)
    assert payload["push_url"] == push_url
    assert payload["event_types"] == ["mission.started", "mission.completed"]
    assert payload.get("client_id") is None

    out = json.loads(capsys.readouterr().out)
    assert out["receipt"]["operation_id"] == envelope["operation_id"]
    assert out["result"]["type"] == "put_event_subscription"
    assert out["result"]["subscription"]["subscription_id"] == str(sub_id)


def test_subscribe_push_destination_refused_error_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, bearer, _ = _setup_configs(tmp_path)
    sub_id = uuid4()
    push_url = "https://unallowed.example/alerts"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_request",
                    "diagnostics": [
                        "push_destination_refused",
                        "host not in allowed_hosts",
                    ],
                }
            },
        )

    def load_client(cfg: Path, bf: Path | None = None) -> tuple[WorkClient, UUID]:
        return (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bf or bearer,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        )

    monkeypatch.setattr(stop_ops, "load_client", load_client)

    exit_code = main(
        [
            "events",
            "subscribe",
            "--client-config",
            str(client_config),
            "--subscription-id",
            str(sub_id),
            "--push-url",
            push_url,
            "--event-type",
            "ops.alarm",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "push_destination_refused" in captured.err
    assert "invalid_request" in captured.err


def test_subscribe_missing_push_url_exits_2_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_config, _, _ = _setup_configs(tmp_path)
    sub_id = uuid4()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    def load_client(cfg: Path, bf: Path | None = None) -> tuple[WorkClient, UUID]:
        return (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                client_config,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        )

    monkeypatch.setattr(stop_ops, "load_client", load_client)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "events",
                "subscribe",
                "--client-config",
                str(client_config),
                "--subscription-id",
                str(sub_id),
                "--event-type",
                "ops.alarm",
            ]
        )

    assert excinfo.value.code == 2
    assert len(requests) == 0


def test_load_client_raising_exits_255(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, _, _ = _setup_configs(tmp_path)
    sub_id = uuid4()

    def raising_load_client(
        cfg: Path, bf: Path | None = None
    ) -> tuple[WorkClient, UUID]:
        raise RuntimeError("simulated client loading error")

    monkeypatch.setattr(stop_ops, "load_client", raising_load_client)

    exit_code = main(
        [
            "events",
            "subscribe",
            "--client-config",
            str(client_config),
            "--subscription-id",
            str(sub_id),
            "--push-url",
            "https://hooks.example/alerts",
            "--event-type",
            "ops.alarm",
        ]
    )

    assert exit_code == 255
    captured = capsys.readouterr()
    assert "simulated client loading error" in captured.err


def test_subscriptions_issues_get_and_prints_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, bearer, custom_bearer = _setup_configs(tmp_path)
    sub_id = uuid4()
    captured_requests: list[httpx.Request] = []
    load_client_calls: list[tuple[Path, Path | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/v1/workspaces/{WORKSPACE}/event-subscriptions"
        return httpx.Response(
            200,
            json={
                "subscriptions": [
                    {
                        "subscription_id": str(sub_id),
                        "client_id": str(uuid4()),
                        "push_url": "https://hooks.example/alerts",
                        "event_types": ["mission.started", "mission.completed"],
                        "cursor_sequence": 0,
                        "deleted": False,
                    }
                ]
            },
        )

    def load_client(cfg: Path, bf: Path | None = None) -> tuple[WorkClient, UUID]:
        load_client_calls.append((cfg, bf))
        return (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bf or bearer,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        )

    monkeypatch.setattr(stop_ops, "load_client", load_client)

    exit_code = main(
        [
            "events",
            "subscriptions",
            "--client-config",
            str(client_config),
            "--bearer-file",
            str(custom_bearer),
        ]
    )

    assert exit_code == 0
    assert load_client_calls == [(client_config, custom_bearer)]
    assert len(captured_requests) == 1

    out = json.loads(capsys.readouterr().out)
    assert len(out["subscriptions"]) == 1
    assert out["subscriptions"][0]["subscription_id"] == str(sub_id)
    assert out["subscriptions"][0]["push_url"] == "https://hooks.example/alerts"
    assert out["subscriptions"][0]["event_types"] == [
        "mission.started",
        "mission.completed",
    ]


def test_events_transport_error_exits_255_and_closes_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, bearer, _ = _setup_configs(tmp_path)
    sub_id = uuid4()
    closed = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client_instance = WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        bearer,
        transport=httpx.MockTransport(handler),
    )
    orig_close = client_instance.close

    def tracking_close() -> None:
        closed.append(True)
        orig_close()

    client_instance.close = tracking_close

    monkeypatch.setattr(
        stop_ops, "load_client", lambda cfg, bf=None: (client_instance, WORKSPACE)
    )

    exit_code = main(
        [
            "events",
            "subscribe",
            "--client-config",
            str(client_config),
            "--subscription-id",
            str(sub_id),
            "--push-url",
            "https://hooks.example/alerts",
            "--event-type",
            "ops.alarm",
        ]
    )

    assert exit_code == 255
    assert len(closed) == 1
    captured = capsys.readouterr()
    assert "connection refused" in captured.err


def test_subscribe_ops_alarm_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_config, bearer, _ = _setup_configs(tmp_path)
    sub_id = uuid4()
    push_url = "https://hooks.example/alerts"
    captured_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_requests.append(body)
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
                    "type": "put_event_subscription",
                    "subscription": {
                        "subscription_id": str(sub_id),
                        "client_id": str(uuid4()),
                        "push_url": push_url,
                        "event_types": ["ops.alarm"],
                        "cursor_sequence": 0,
                        "deleted": False,
                    },
                },
            },
        )

    monkeypatch.setattr(
        stop_ops,
        "load_client",
        lambda cfg, bf=None: (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bearer,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        ),
    )

    exit_code = main(
        [
            "events",
            "subscribe",
            "--client-config",
            str(client_config),
            "--subscription-id",
            str(sub_id),
            "--push-url",
            push_url,
            "--event-type",
            "ops.alarm",
        ]
    )

    assert exit_code == 0
    assert len(captured_requests) == 1
    assert captured_requests[0]["command"]["payload"]["event_types"] == ["ops.alarm"]


def test_push_command_failure_stays_on_255(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omp_work import event_push

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "omp" / "work-ledger"
    config.mkdir(parents=True)
    key_file = config / "push-signing.key"
    key_file.write_bytes(bytes(range(32)))
    key_file.chmod(0o600)
    (config / "push-destinations.json").write_text(
        json.dumps({"allowed_hosts": ["hooks.example"]})
    )
    client_config, bearer, _ = _setup_configs(tmp_path)

    monkeypatch.setattr(
        stop_ops,
        "load_client",
        lambda cfg, bf=None: (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bearer,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"subscriptions": []})
                ),
            ),
            WORKSPACE,
        ),
    )
    monkeypatch.setattr(
        event_push,
        "run_push",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkError("invalid_request")),
    )

    exit_code = main(["events", "push", "--client-config", str(client_config)])

    assert exit_code == 255
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_request" in captured.err
