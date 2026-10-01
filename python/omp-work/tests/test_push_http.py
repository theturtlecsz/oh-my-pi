"""Tests for push HTTP client (OMP-406, OMP-415)."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from typing import Any

import pytest

from omp_work.push_http import PushDeliveryError, send


class _StubHandler(http.server.BaseHTTPRequestHandler):
    recorded_requests: list[dict[str, Any]] = []
    status_to_return: int = 200

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else None

        self.recorded_requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": parsed_body,
            }
        )

        if self.status_to_return == 200:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(self.status_to_return)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "internal error"}')

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging in tests
        pass


@pytest.fixture
def stub_server():
    _StubHandler.recorded_requests = []
    _StubHandler.status_to_return = 200

    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_send_gets_body_and_three_headers(stub_server: str) -> None:
    token = "secret-token-abc"
    idempotency_key = "idemp-key-123"
    body = {"type": "alert", "kind": "cost_threshold", "workspace_id": "ws-1"}

    send(
        url=f"{stub_server}/api/alerts",
        token=token,
        idempotency_key=idempotency_key,
        body=body,
    )

    assert len(_StubHandler.recorded_requests) == 1
    req = _StubHandler.recorded_requests[0]

    assert req["body"] == body
    headers = req["headers"]
    assert headers.get("Authorization") == f"Bearer {token}"
    assert headers.get("Idempotency-Key") == idempotency_key
    assert headers.get("Content-Type") == "application/json"


def test_send_500_raises_push_delivery_error_without_token(stub_server: str) -> None:
    _StubHandler.status_to_return = 500
    token = "super-secret-token-98765"
    idempotency_key = "key-500"
    body = {"test": "data"}

    with pytest.raises(PushDeliveryError) as exc_info:
        send(
            url=f"{stub_server}/api/alerts",
            token=token,
            idempotency_key=idempotency_key,
            body=body,
        )

    err = exc_info.value
    err_str = str(err)
    assert token not in err_str
    assert token not in repr(err)
    assert "500" in err_str or (err.status_code == 500)


def test_send_refuses_file_and_http_example() -> None:
    token = "some-token"
    key = "some-key"
    body = {"msg": "test"}

    # file: must be refused with ValueError before connection
    with pytest.raises(ValueError, match="(?i)scheme|unsupported"):
        send("file:///etc/hosts", token, key, body)

    with pytest.raises(ValueError, match="(?i)scheme|unsupported"):
        send("file:", token, key, body)

    # http://example.com must be refused with ValueError before connection
    with pytest.raises(ValueError, match="(?i)loopback"):
        send("http://example.com/alerts", token, key, body)

    with pytest.raises(ValueError, match="(?i)loopback"):
        send("http://192.168.1.1:8080/alerts", token, key, body)


def test_send_network_error_without_token() -> None:
    # Find an unused loopback port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        unused_port = s.getsockname()[1]

    token = "network-secret-token"
    with pytest.raises(PushDeliveryError) as exc_info:
        send(
            f"http://127.0.0.1:{unused_port}/alerts",
            token,
            "key",
            {"test": 1},
            timeout=1,
        )

    err_str = str(exc_info.value)
    assert token not in err_str
