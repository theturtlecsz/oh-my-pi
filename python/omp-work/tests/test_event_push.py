"""OMP-415-s07: signed event push runner over a fake client and loopback stub.

The runner is exercised end to end: it reads every client's subscriptions,
refuses an unsafe destination without sending, delivers only subscribed types
with a per-subscription signed, bearer-less POST whose Idempotency-Key is the
mission_event_id, and advances each cursor only past what was delivered.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from omp_work import event_push
from omp_work.__main__ import main
from omp_work.event_push import (
    body_bytes,
    load_master_key,
    run_push,
    send_signed,
    signature,
    subscription_key,
    verify,
)
from omp_work.grokbot import GrokbotError
from omp_work.operations import stop as stop_ops

MASTER = bytes(range(32))
WORKSPACE = UUID("00000000-0000-7000-8000-0000000004f0")
SUB_A = UUID("00000000-0000-7000-8000-0000000004a1")
SUB_B = UUID("00000000-0000-7000-8000-0000000004b2")


class _StubHandler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def stub():
    _StubHandler.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _subscription(
    subscription_id: UUID,
    client_id: UUID,
    push_url: str | None,
    *,
    event_types: list[str] | None = None,
    cursor: int = 0,
) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription_id),
        "client_id": str(client_id),
        "push_url": push_url,
        "event_types": event_types or ["mission.started", "important_finding"],
        "cursor_sequence": cursor,
        "deleted": False,
    }


def _event(sequence: int, type_: str) -> dict[str, Any]:
    return {
        "mission_event_id": str(uuid5(NAMESPACE_URL, f"ev:{sequence}:{type_}")),
        "sequence": sequence,
        "mission_id": str(WORKSPACE),
        "type": type_,
        "trigger": "t",
        "occurred_at": "2026-09-30T12:00:00+00:00",
        "source_event_id": str(uuid4()),
        "evidence_refs": [],
    }


class _Client:
    def __init__(
        self, subscriptions: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> None:
        self.subscriptions = subscriptions
        self.events = events
        self.advances: list[tuple[str, int, Any]] = []
        self.mission_event_calls: list[int] = []

    def event_subscriptions(self) -> dict[str, Any]:
        return {"subscriptions": [dict(row) for row in self.subscriptions]}

    def mission_events(
        self,
        after_sequence: int = 0,
        limit: int = 500,
        mission_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.mission_event_calls.append(after_sequence)
        remaining = [e for e in self.events if e["sequence"] > after_sequence]
        page = remaining[:limit]
        return {
            "events": page,
            "watermark_sequence": self.events[-1]["sequence"] if self.events else 0,
            "next_after_sequence": page[-1]["sequence"] if page else after_sequence,
            "has_more": len(remaining) > len(page),
        }

    def execute(self, envelope: Any) -> Any:
        command = envelope.command
        payload = command.payload
        self.advances.append(
            (
                str(payload.subscription_id),
                int(payload.after_sequence),
                envelope.operation_id,
            )
        )
        return {"command": command.type}


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        event_push,
        "check_destination",
        lambda url, *, allowed_hosts, resolve=None: None,
    )


def _expected_op(subscription_id: UUID, sequence: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"omp-push-cursor:{subscription_id}:{sequence}")


def test_signature_verify_rejects_changed_body_and_other_key() -> None:
    body = {"b": 1, "a": [2, 3]}
    header = signature(MASTER, "idem-1", body)
    assert header.startswith("v1=")
    assert verify(MASTER, "idem-1", body, header) is True
    assert verify(MASTER, "idem-1", {"b": 1, "a": [2, 4]}, header) is False
    assert verify(b"x" * 32, "idem-1", body, header) is False
    assert verify(MASTER, "other-idem", body, header) is False
    assert verify(MASTER, "idem-1", body, None) is False
    assert verify(MASTER, "idem-1", body, "v1=deadbeef") is False
    assert verify(MASTER, "idem-1", body, "") is False
    assert verify(MASTER, "idem-1", body, "v1=") is False
    # compare_digest raises TypeError on a non-ASCII str; verify must not
    assert verify(MASTER, "idem-1", body, "v1=é") is False
    assert verify(MASTER, "idem-1", body, "é" * 80) is False
    # compact, sorted-key JSON is the signed preimage
    assert body_bytes(body) == b'{"a":[2,3],"b":1}'


def test_subscription_key_is_the_per_sub_key_used_for_delivery() -> None:
    import hashlib
    import hmac

    key = subscription_key(MASTER, SUB_A)
    expected = hmac.new(
        MASTER, b"omp-push-subscription\0" + str(SUB_A).encode(), hashlib.sha256
    ).digest()
    assert key == expected
    assert subscription_key(MASTER, SUB_A) == key
    assert subscription_key(MASTER, SUB_B) != key


def test_each_subscribed_event_sent_once_signed_with_push_key(
    stub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    events = [
        _event(1, "mission.started"),
        _event(2, "important_finding"),
        _event(3, "mission.progressed"),
    ]
    sub = _subscription(SUB_A, uuid4(), f"{stub}/a", event_types=["mission.started"])
    client = _Client([sub], events)

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
    )

    assert result == {"pushed": 1}
    assert len(_StubHandler.requests) == 1
    request = _StubHandler.requests[0]
    headers = request["headers"]
    started = events[0]
    assert headers["idempotency-key"] == started["mission_event_id"]
    assert "authorization" not in headers
    assert headers["x-omp-signature"] == signature(
        subscription_key(MASTER, SUB_A), started["mission_event_id"], request["body"]
    )
    assert client.advances == [(str(SUB_A), 3, _expected_op(SUB_A, 3))]


def test_second_clients_subscription_is_delivered_and_advanced(
    stub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    events = [_event(1, "mission.started")]
    client = _Client(
        [
            _subscription(SUB_A, uuid4(), f"{stub}/a", event_types=["mission.started"]),
            _subscription(SUB_B, uuid4(), f"{stub}/b", event_types=["mission.started"]),
        ],
        events,
    )

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
    )

    assert result == {"pushed": 2}
    assert len(_StubHandler.requests) == 2
    assert [row[0] for row in client.advances] == [str(SUB_A), str(SUB_B)]
    assert client.advances[1] == (str(SUB_B), 1, _expected_op(SUB_B, 1))
    # each subscription signs with its own key over the same body
    for request in _StubHandler.requests:
        assert request["headers"]["idempotency-key"] == events[0]["mission_event_id"]


def test_unsubscribed_types_are_skipped_and_cursor_passes_them(
    stub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    events = [
        _event(1, "mission.started"),
        _event(2, "important_finding"),
        _event(3, "mission.started"),
    ]
    sub = _subscription(
        SUB_A, uuid4(), f"{stub}/a", event_types=["mission.started"], cursor=0
    )
    client = _Client([sub], events)

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
    )

    assert result == {"pushed": 2}
    keys = [r["headers"]["idempotency-key"] for r in _StubHandler.requests]
    assert keys == [events[0]["mission_event_id"], events[2]["mission_event_id"]]
    # the skipped event is not delivered but the cursor still moves past it
    assert client.advances == [(str(SUB_A), 3, _expected_op(SUB_A, 3))]


def test_pull_subscription_does_not_block_a_later_push(
    stub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    events = [_event(1, "mission.started")]
    blank = uuid4()
    client = _Client(
        [
            _subscription(SUB_A, uuid4(), None, event_types=["mission.started"]),
            _subscription(blank, uuid4(), "", event_types=["mission.started"]),
            _subscription(SUB_B, uuid4(), f"{stub}/b", event_types=["mission.started"]),
        ],
        events,
    )

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
    )

    assert result == {"pushed": 1}
    assert len(_StubHandler.requests) == 1
    assert _StubHandler.requests[0]["path"] == "/b"
    assert (
        _StubHandler.requests[0]["headers"]["idempotency-key"]
        == events[0]["mission_event_id"]
    )
    # the pull rows are not a refusal and their cursors stay put
    assert client.advances == [(str(SUB_B), 1, _expected_op(SUB_B, 1))]
    assert client.mission_event_calls == [0]
    assert all(row[0] != str(blank) for row in client.advances)


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """Records the method, path, and headers, then answers with ``status``."""

    requests: ClassVar[list[dict[str, Any]]] = []
    location: ClassVar[str] = ""
    status: ClassVar[int] = 200

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        self.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw,
            }
        )
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._record()

    def do_POST(self) -> None:
        self._record()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _RedirectHandler(_CaptureHandler):
    requests: ClassVar[list[dict[str, Any]]] = []
    status = 302


class _SinkHandler(_CaptureHandler):
    requests: ClassVar[list[dict[str, Any]]] = []
    status = 200
    location = ""


def _serve(handler: type[http.server.BaseHTTPRequestHandler]):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop(server: http.server.HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_signed_push_rejects_redirect_and_does_not_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow(monkeypatch)
    _RedirectHandler.requests = []
    _SinkHandler.requests = []
    sink_server, sink_thread, private = _serve(_SinkHandler)
    _RedirectHandler.location = f"{private}/private"
    redirect_server, redirect_thread, redirect_url = _serve(_RedirectHandler)
    try:
        events = [_event(1, "mission.started")]
        client = _Client(
            [
                _subscription(
                    SUB_A,
                    uuid4(),
                    f"{redirect_url}/hook",
                    event_types=["mission.started"],
                )
            ],
            events,
        )

        def sending(url: str, idem: str, body: Any, *, key: bytes) -> None:
            send_signed(url, idem, body, key=key, sleep=lambda _s: None)

        result = run_push(
            client,
            workspace_id=WORKSPACE,
            master_key=MASTER,
            allowed_hosts={"127.0.0.1"},
            send=sending,
        )
    finally:
        _stop(redirect_server, redirect_thread)
        _stop(sink_server, sink_thread)

    assert result["failed"].startswith("HTTP 302")
    assert client.advances == []
    # the Location target never sees the POST, a follow-up GET, or the signature
    assert _SinkHandler.requests == []
    assert [row["method"] for row in _RedirectHandler.requests] == ["POST", "POST", "POST"]
    assert all(row["path"] == "/hook" for row in _RedirectHandler.requests)
    assert all("x-omp-signature" in row["headers"] for row in _RedirectHandler.requests)


def test_refused_destination_sends_nothing(
    stub: str,
) -> None:
    client = _Client([_subscription(SUB_A, uuid4(), "http://hooks.example/hook")], [])

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"hooks.example"},
    )

    assert result == {"refused": "scheme"}
    assert _StubHandler.requests == []
    assert client.advances == []
    assert client.mission_event_calls == []

    client_2 = _Client(
        [_subscription(SUB_B, uuid4(), "https://hooks.example/hook")], []
    )
    assert run_push(
        client_2,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"other.example"},
    ) == {"refused": "host_not_allowed"}
    assert _StubHandler.requests == []


def test_rerun_after_mid_page_failure_resumes_past_last_delivered() -> None:
    events = [
        _event(1, "mission.started"),
        _event(2, "mission.started"),
        _event(3, "mission.started"),
    ]
    sub = _subscription(SUB_A, uuid4(), "https://hooks.example/a", cursor=0)
    client = _Client([sub], events)
    third = events[2]["mission_event_id"]
    first_run: list[str] = []

    def flaky(url: str, idem: str, body: Any, *, key: bytes) -> None:
        first_run.append(idem)
        if idem == third:
            raise GrokbotError("boom")

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"hooks.example"},
        resolve=lambda host: ["8.8.8.8"],
        send=flaky,
    )

    assert result == {"failed": "boom"}
    # the two delivered events advanced the cursor; seq3 did not
    assert client.advances == [(str(SUB_A), 2, _expected_op(SUB_A, 2))]

    second_run: list[str] = []

    def ok(url: str, idem: str, body: Any, *, key: bytes) -> None:
        second_run.append(idem)

    rerun = _Client([sub], events)
    rerun.subscriptions = [
        _subscription(SUB_A, uuid4(), "https://hooks.example/a", cursor=2)
    ]
    result = run_push(
        rerun,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"hooks.example"},
        resolve=lambda host: ["8.8.8.8"],
        send=ok,
    )

    assert result == {"pushed": 1}
    # only the undelivered event is resent; no gap and no duplicate
    assert second_run == [third]
    assert rerun.mission_event_calls == [2]
    assert rerun.advances == [(str(SUB_A), 3, _expected_op(SUB_A, 3))]


def test_send_signed_survives_two_500s_and_raises_on_third(
    stub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"k": "v"}
    key = subscription_key(MASTER, SUB_A)
    original = event_push.grokbot_send
    sleeps: list[float] = []

    # Two failures then success: delivered once, slept 1s then 2s.
    remaining = {"n": 2}

    def recovering(url, token, idem, payload, *, headers=None, timeout=10):
        if remaining["n"] > 0:
            remaining["n"] -= 1
            raise GrokbotError("HTTP 500", status_code=500)
        return original(url, token, idem, payload, headers=headers, timeout=timeout)

    monkeypatch.setattr(event_push, "grokbot_send", recovering)
    send_signed(f"{stub}/hook", "idem", body, key=key, sleep=sleeps.append)
    assert remaining["n"] == 0
    assert sleeps == [1.0, 2.0]
    assert len(_StubHandler.requests) == 1
    request = _StubHandler.requests[0]
    assert "authorization" not in request["headers"]
    assert request["headers"]["x-omp-signature"] == signature(key, "idem", body)

    # A third failure is raised after two more attempts.
    calls = {"n": 0}

    def always_fail(url, token, idem, payload, *, headers=None, timeout=10):
        calls["n"] += 1
        raise GrokbotError("HTTP 500", status_code=500)

    monkeypatch.setattr(event_push, "grokbot_send", always_fail)
    with pytest.raises(GrokbotError):
        send_signed(f"{stub}/hook", "idem", body, key=key, sleep=lambda _s: None)
    assert calls["n"] == 3


def test_load_master_key_refuses_group_or_other_readable_and_short(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "push-signing.key"

    with pytest.raises(ValueError):
        load_master_key(config)

    path.write_bytes(b"short")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        load_master_key(config)

    path.write_bytes(MASTER)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_master_key(config)

    path.chmod(0o600)
    assert load_master_key(config) == MASTER


def test_push_key_command_prints_hex_and_refuses_0644(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "omp" / "work-ledger"
    config.mkdir(parents=True)
    key_file = config / "push-signing.key"
    key_file.write_bytes(MASTER)
    key_file.chmod(0o600)

    assert main(["events", "push-key", "--subscription", str(SUB_A)]) == 0
    assert capsys.readouterr().out.strip() == subscription_key(MASTER, SUB_A).hex()

    key_file.chmod(0o644)
    assert main(["events", "push-key", "--subscription", str(SUB_A)]) == 2
    assert "unsafe push-signing key" in capsys.readouterr().err


def test_push_command_carries_the_workclients_cursor_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    from omp_work.operations.capabilities import write_client_config
    from omp_work.operations.config import OperationsConfig
    from omp_work.v1.client import WorkClient

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "omp" / "work-ledger"
    config.mkdir(parents=True)
    key_file = config / "push-signing.key"
    key_file.write_bytes(MASTER)
    key_file.chmod(0o600)
    (config / "push-destinations.json").write_text(
        json.dumps({"allowed_hosts": ["hooks.example"]})
    )
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": "push-token"}))
    bearer.chmod(0o600)
    ops_config = OperationsConfig(
        config_dir=config,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    write_client_config(
        ops_config,
        workspace_id=WORKSPACE,
        owner_id=uuid4(),
        base_url="http://127.0.0.1:54322",
        bearer_file=bearer,
    )

    event = _event(4, "mission.started")
    cursors: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/event-subscriptions"):
            return httpx.Response(
                200,
                json={
                    "subscriptions": [
                        _subscription(SUB_A, uuid4(), "https://hooks.example/h")
                    ]
                },
            )
        if request.url.path.endswith("/mission-events"):
            return httpx.Response(
                200,
                json={
                    "events": [event],
                    "watermark_sequence": 4,
                    "next_after_sequence": 4,
                    "has_more": False,
                },
            )
        body = json.loads(request.content)
        cursors.append(body)
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
                    "type": "advance_event_cursor",
                    "subscription": _subscription(SUB_A, uuid4(), None, cursor=4),
                },
            },
        )

    def load_client(client_config: Path, bearer_file: Path | None = None):
        return (
            WorkClient(
                "http://127.0.0.1:54322",
                WORKSPACE,
                bearer,
                transport=httpx.MockTransport(handler),
            ),
            WORKSPACE,
        )

    monkeypatch.setattr(stop_ops, "load_client", load_client)
    monkeypatch.setattr(
        event_push,
        "check_destination",
        lambda url, *, allowed_hosts, resolve=None: None,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        event_push,
        "send_signed",
        lambda url, idem, body, *, key: sent.append((url, idem)),
    )

    assert (
        main(["events", "push", "--client-config", str(tmp_path / "unused.json")]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {"pushed": 1}
    assert sent == [("https://hooks.example/h", event["mission_event_id"])]
    assert cursors[0]["command"] == {
        "type": "advance_event_cursor",
        "payload": {"subscription_id": str(SUB_A), "after_sequence": 4},
    }
    assert cursors[0]["operation_id"] == str(_expected_op(SUB_A, 4))
