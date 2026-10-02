"""OMP-514-s01: per-push_url bearer tokens for the signed event push runner.

``load_push_bearers`` maps an exact push_url to its token file's stripped text,
and only a row whose stripped push_url is bound there carries that token as its
POST Authorization bearer. An ops.alarm row with a configured bearer delivers
one signed POST with ``Authorization: Bearer <token>``, a verifying
``X-OMP-Signature``, and the alert's Idempotency-Key; an unconfigured row
delivers without an Authorization header. A 401 whose reason echoes the token
is reported as ``{"failed": ...}`` with the token redacted from the result and
never present in a persisted cursor envelope or a log record. A missing or
unsafe bearer config is a ValueError naming a path, never the token.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from pathlib import Path
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from omp_work import event_push
from omp_work.event_push import (
    load_push_bearers,
    run_push,
    send_signed,
    subscription_key,
    verify,
)
from omp_work.v1.api_models import DomainEventView

MASTER = bytes(range(32))
WORKSPACE = UUID("00000000-0000-7000-8000-0000000004f0")
SUB_A = UUID("00000000-0000-7000-8000-0000000004a1")
SUB_B = UUID("00000000-0000-7000-8000-0000000004b2")
TOKEN = "sekrit-push-token-9f2a"


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Records each POST; answers 200, or 401 once ``fail_after`` is passed."""

    requests: ClassVar[list[dict[str, Any]]] = []
    fail_after: ClassVar[int | None] = None
    fail_reason: ClassVar[str] = "denied"

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
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            self.send_response(401, self.fail_reason)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def stub():
    _StubHandler.requests = []
    _StubHandler.fail_after = None
    _StubHandler.fail_reason = "denied"
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


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        event_push,
        "check_destination",
        lambda url, *, allowed_hosts, resolve=None: None,
    )


def _write_config(config: Path, token_files: dict[str, str]) -> Path:
    config.mkdir(exist_ok=True)
    path = config / "push-bearer.json"
    path.write_text(json.dumps({"token_files": token_files}))
    path.chmod(0o600)
    return path


def _write_token(tmp_path: Path, text: str, *, mode: int = 0o600) -> Path:
    path = tmp_path / "push-token"
    path.write_text(text)
    path.chmod(mode)
    return path


def _event(
    sequence: int,
    *,
    event_type: str = "record_alarm_signal",
    payload: dict[str, Any] | None = None,
) -> DomainEventView:
    return DomainEventView(
        event_id=uuid5(NAMESPACE_URL, f"ops-ev:{sequence}"),
        sequence=sequence,
        workspace_id=WORKSPACE,
        aggregate_type="work_item",
        aggregate_id=uuid4(),
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="automation",
        capability_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        operation_id=uuid4(),
        causation_id=uuid4(),
        event_type=event_type,
        outcome="applied",
        payload=payload if payload is not None else {},
        payload_sha256="b" * 64,
        previous_event_sha256=None,
        event_sha256="a" * 64,
        occurred_at="2026-09-30T12:00:00+00:00",
    )


def _signal(sequence: int, kind: str) -> DomainEventView:
    return _event(sequence, payload={"signal": kind, "reason": kind})


def _subscription(subscription_id: UUID, push_url: str) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription_id),
        "client_id": str(uuid4()),
        "push_url": push_url,
        "event_types": ["ops.alarm"],
        "cursor_sequence": 0,
        "deleted": False,
    }


class _Client:
    def __init__(
        self, subscriptions: list[dict[str, Any]], events: list[DomainEventView]
    ) -> None:
        self.subscriptions = [dict(row) for row in subscriptions]
        self.events_all = list(events)
        self.advances: list[tuple[str, int]] = []
        self.envelopes: list[Any] = []

    def event_subscriptions(self) -> dict[str, Any]:
        return {"subscriptions": [dict(row) for row in self.subscriptions]}

    def events(self, after_sequence: int = 0, limit: int = 500) -> dict[str, Any]:
        remaining = [e for e in self.events_all if e.sequence > after_sequence]
        page = remaining[:limit]
        return {
            "events": tuple(page),
            "watermark_sequence": max((e.sequence for e in self.events_all), default=0),
            "next_after_sequence": page[-1].sequence if page else after_sequence,
            "has_more": len(remaining) > len(page),
        }

    def execute(self, envelope: Any) -> Any:
        payload = envelope.command.payload
        self.advances.append(
            (str(payload.subscription_id), int(payload.after_sequence))
        )
        self.envelopes.append(envelope)
        return {"command": envelope.command.type}


def test_configured_row_sends_bearer_and_verifying_signature(
    stub: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    token_file = _write_token(tmp_path, TOKEN + "\n")
    _write_config(config, {f"{stub}/alarm": str(token_file)})
    bearers = load_push_bearers(config)
    _allow(monkeypatch)

    event = _signal(1, "cost_threshold")
    client = _Client([_subscription(SUB_A, f"{stub}/alarm")], [event])

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
        bearers=bearers,
    )

    assert result == {"pushed": 1}
    request = _StubHandler.requests[0]
    headers = request["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    idem = f"{event.event_id}:cost_threshold"
    assert headers["idempotency-key"] == idem
    assert (
        verify(
            subscription_key(MASTER, SUB_A),
            idem,
            request["body"],
            headers["x-omp-signature"],
        )
        is True
    )


def test_unconfigured_row_sends_without_authorization(
    stub: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    token_file = _write_token(tmp_path, TOKEN)
    _write_config(config, {f"{stub}/alarm": str(token_file)})
    bearers = load_push_bearers(config)
    _allow(monkeypatch)

    event = _signal(1, "cost_threshold")
    client = _Client(
        [
            _subscription(SUB_A, f"{stub}/alarm"),
            _subscription(SUB_B, f"{stub}/other"),
        ],
        [event],
    )

    result = run_push(
        client,
        workspace_id=WORKSPACE,
        master_key=MASTER,
        allowed_hosts={"127.0.0.1"},
        bearers=bearers,
    )

    assert result == {"pushed": 2}
    assert [r["path"] for r in _StubHandler.requests] == ["/alarm", "/other"]
    assert _StubHandler.requests[0]["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in _StubHandler.requests[1]["headers"]


def test_401_echoing_token_is_redacted_from_result_envelope_and_logs(
    stub: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = tmp_path / "config"
    token_file = _write_token(tmp_path, TOKEN)
    _write_config(config, {f"{stub}/alarm": str(token_file)})
    bearers = load_push_bearers(config)
    _allow(monkeypatch)
    _StubHandler.fail_after = 1
    _StubHandler.fail_reason = f"denied token {TOKEN}"

    def sending(
        url: str, idem: str, body: Any, *, key: bytes, token: str | None = None
    ) -> None:
        send_signed(
            url, idem, body, key=key, token=token, attempts=1, sleep=lambda _s: None
        )

    client = _Client(
        [_subscription(SUB_A, f"{stub}/alarm")],
        [_signal(1, "cost_threshold"), _signal(2, "budget_exceeded")],
    )

    with caplog.at_level(logging.DEBUG):
        result = run_push(
            client,
            workspace_id=WORKSPACE,
            master_key=MASTER,
            allowed_hosts={"127.0.0.1"},
            send=sending,
            bearers=bearers,
        )

    assert "failed" in result
    assert "401" in result["failed"]
    # the reason did reach the message, with the echoed token replaced
    assert "denied token" in result["failed"]
    assert TOKEN not in result["failed"]
    # the first alert was delivered, so the cursor advanced to the failed one minus one
    assert client.advances == [(str(SUB_A), 1)]
    assert TOKEN not in "".join(
        envelope.model_dump_json() for envelope in client.envelopes
    )
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


def test_load_push_bearers_refuses_unsafe_shapes_without_leaking_token(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    token_file = _write_token(tmp_path, TOKEN)
    token_file.chmod(0o600)

    # relative token path
    _write_config(config, {"https://hooks.example/a": "relative/token"})
    with pytest.raises(ValueError) as relative_error:
        load_push_bearers(config)
    assert TOKEN not in str(relative_error.value)

    # token file mode & 0o077
    token_file.chmod(0o644)
    _write_config(config, {"https://hooks.example/a": str(token_file)})
    with pytest.raises(ValueError) as permission_error:
        load_push_bearers(config)
    assert TOKEN not in str(permission_error.value)

    # empty token
    token_file.chmod(0o600)
    token_file.write_text("  \n")
    with pytest.raises(ValueError) as empty_error:
        load_push_bearers(config)
    assert TOKEN not in str(empty_error.value)

    # interior newline is not a single-line token
    token_file.write_text(f"a\n{TOKEN}")
    with pytest.raises(ValueError):
        load_push_bearers(config)

    # a well-formed config loads the stripped token
    token_file.write_text(f"{TOKEN}\n")
    assert load_push_bearers(config) == {"https://hooks.example/a": TOKEN}


def test_load_push_bearers_missing_config_is_empty(tmp_path: Path) -> None:
    assert load_push_bearers(tmp_path / "absent") == {}
