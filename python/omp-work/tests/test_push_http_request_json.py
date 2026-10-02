"""Tests for request_json in push HTTP client (OMP-518-s01)."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, ClassVar

import pytest
from omp_work.push_http import PushDeliveryError, request_json


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    recorded: ClassVar[list[dict[str, Any]]] = []
    status: int = 200
    response_body: bytes = b'{"ok": true}'
    respond_empty: bool = False
    location: str | None = None
    drop_without_reply: bool = False
    truncate_response: bool = False

    def _handle(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        parsed_body = None
        if raw_body:
            try:
                parsed_body = json.loads(raw_body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed_body = raw_body

        if self.path == "/redirect-target":
            _JsonHandler.recorded.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "raw_body": raw_body,
                    "json_body": parsed_body,
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"target_hit": true}')
            return

        _JsonHandler.recorded.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "raw_body": raw_body,
                "json_body": parsed_body,
            }
        )

        if self.drop_without_reply:
            self.close_connection = True
            return

        if self.truncate_response:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"truncated":')
            self.close_connection = True
            return

        self.send_response(self.status)
        if self.location is not None:
            self.send_header("Location", self.location)
        if self.respond_empty:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def json_server():
    _JsonHandler.recorded = []
    _JsonHandler.status = 200
    _JsonHandler.response_body = b'{"ok": true}'
    _JsonHandler.respond_empty = False
    _JsonHandler.location = None
    _JsonHandler.drop_without_reply = False
    _JsonHandler.truncate_response = False

    server = http.server.HTTPServer(("127.0.0.1", 0), _JsonHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Loopback tests (socket required)
# ---------------------------------------------------------------------------


def test_get_returns_parsed_json(json_server: str) -> None:
    _JsonHandler.response_body = b'{"pull": {"number": 7}}'
    token = "get-token-111"

    status, payload = request_json("GET", f"{json_server}/repos/o/r/pulls/7", token)

    assert status == 200
    assert payload == {"pull": {"number": 7}}
    assert len(_JsonHandler.recorded) == 1
    req = _JsonHandler.recorded[0]
    assert req["method"] == "GET"
    assert req["headers"].get("Authorization") == f"Bearer {token}"
    assert req["raw_body"] == b""


def test_put_delivers_json_body_and_bearer(json_server: str) -> None:
    token = "put-token-222"
    body = {"merge_method": "merge", "sha": "abc123"}

    status, payload = request_json(
        "PUT", f"{json_server}/repos/o/r/pulls/7/merge", token, body
    )

    assert status == 200
    assert payload == {"ok": True}
    assert len(_JsonHandler.recorded) == 1
    req = _JsonHandler.recorded[0]
    assert req["method"] == "PUT"
    assert req["json_body"] == body
    assert req["headers"].get("Authorization") == f"Bearer {token}"
    assert req["headers"].get("Content-Type") == "application/json"


def test_empty_2xx_returns_none(json_server: str) -> None:
    _JsonHandler.respond_empty = True

    status, payload = request_json("GET", f"{json_server}/empty", "empty-token")

    assert status == 200
    assert payload is None


def test_404_raises_with_status_code_404(json_server: str) -> None:
    _JsonHandler.status = 404
    token = "secret-token-404"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json("GET", f"{json_server}/missing", token)

    assert exc_info.value.status_code == 404
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)


def test_302_errors_and_location_target_gets_no_request(json_server: str) -> None:
    _JsonHandler.status = 302
    _JsonHandler.location = f"{json_server}/redirect-target"
    token = "secret-token-302"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json("GET", f"{json_server}/redirecting", token)

    assert exc_info.value.status_code == 302
    assert token not in str(exc_info.value)
    assert len(_JsonHandler.recorded) == 1
    assert _JsonHandler.recorded[0]["path"] == "/redirecting"


def test_no_reply_gives_status_none(json_server: str) -> None:
    _JsonHandler.drop_without_reply = True
    token = "secret-token-drop"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json("GET", f"{json_server}/dropped", token, timeout=2)

    assert exc_info.value.status_code is None
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)


def test_short_content_length_gives_status_none(json_server: str) -> None:
    _JsonHandler.truncate_response = True
    token = "secret-token-truncated"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json("GET", f"{json_server}/truncated", token, timeout=2)

    assert exc_info.value.status_code is None
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)


def test_2xx_bad_json_gives_status_none(json_server: str) -> None:
    _JsonHandler.response_body = b"not-valid-json"
    token = "secret-token-bad-json"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json("GET", f"{json_server}/bad-json", token)

    assert exc_info.value.status_code is None
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)


def test_string_body_arrives_as_json_string(json_server: str) -> None:
    token = "string-token-123"

    status, payload = request_json("POST", f"{json_server}/string-body", token, "hello")

    assert status == 200
    assert payload == {"ok": True}
    assert len(_JsonHandler.recorded) == 1
    req = _JsonHandler.recorded[0]
    assert req["raw_body"] == b'"hello"'
    assert req["json_body"] == "hello"


def test_caller_protected_headers_dropped_no_body(json_server: str) -> None:
    token = "real-token-no-body"

    request_json(
        "GET",
        f"{json_server}/no-body-headers",
        token,
        headers={
            "authorization": "Bearer attacker",
            "content-type": "text/plain",
            "X-Custom": "present",
        },
    )

    assert len(_JsonHandler.recorded) == 1
    req = _JsonHandler.recorded[0]
    assert req["headers"].get("Authorization") == f"Bearer {token}"
    assert "Content-Type" not in req["headers"]
    assert "content-type" not in req["headers"]
    assert req["headers"].get("X-Custom") == "present"


def test_caller_protected_headers_dropped_with_body(json_server: str) -> None:
    token = "real-token-with-body"

    request_json(
        "POST",
        f"{json_server}/body-headers",
        token,
        {"key": "value"},
        headers={
            "AUTHORIZATION": "Bearer attacker",
            "CONTENT-TYPE": "text/plain",
            "X-Custom": "present",
        },
    )

    assert len(_JsonHandler.recorded) == 1
    req = _JsonHandler.recorded[0]
    assert req["headers"].get("Authorization") == f"Bearer {token}"
    assert req["headers"].get("Content-Type") == "application/json"
    assert req["headers"].get("X-Custom") == "present"


def test_caller_header_values_redacted_on_error(json_server: str) -> None:
    _JsonHandler.status = 500
    token = "secret-token-500"
    header_secret = "super-secret-signing-key"

    with pytest.raises(PushDeliveryError) as exc_info:
        request_json(
            "POST",
            f"{json_server}/server-error",
            token,
            {"data": 1},
            headers={"X-Signature": header_secret},
        )

    err_str = str(exc_info.value)
    assert token not in err_str
    assert header_secret not in err_str
    assert token not in repr(exc_info.value)
    assert header_secret not in repr(exc_info.value)
    if exc_info.value.reason:
        assert token not in exc_info.value.reason
        assert header_secret not in exc_info.value.reason


# ---------------------------------------------------------------------------
# Pre-connect tests (no server, pass where sockets are denied)
# ---------------------------------------------------------------------------


def test_bad_method_raises_before_connecting() -> None:
    for bad_method in (
        "DELETE",
        "get",
        "HEAD",
        "OPTIONS",
        "CONNECT",
        "TRACE",
        "INVALID",
    ):
        with pytest.raises(ValueError, match="(?i)method"):
            request_json(bad_method, "http://127.0.0.1:1/x", "some-token")


def test_non_loopback_http_raises_before_connecting() -> None:
    for url in ("http://example.com/pulls", "http://192.168.1.1:8080/pulls"):
        with pytest.raises(ValueError, match="(?i)loopback"):
            request_json("GET", url, "some-token")


def test_unsupported_scheme_raises_before_connecting() -> None:
    for url in ("file:///etc/hosts", "file:", "ftp://127.0.0.1/x"):
        with pytest.raises(ValueError, match="(?i)scheme|unsupported"):
            request_json("GET", url, "some-token")


def test_malformed_header_value_with_token_raises_without_token() -> None:
    token = "review-secret-token-999"
    with pytest.raises(ValueError) as exc_info:
        request_json(
            "GET",
            "http://127.0.0.1:1/check",
            token,
            headers={"X-Custom": f"{token}\ninvalid"},
        )

    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)
    assert "X-Custom" not in str(exc_info.value)


def test_malformed_header_name_raises_without_name() -> None:
    bad_names = [
        "X:Custom",
        " XCustom",
        "X\nCustom",
        "X\rCustom",
        "X\0Custom",
        "X\u1234",
    ]
    for name in bad_names:
        with pytest.raises(ValueError) as exc_info:
            request_json(
                "GET",
                "http://127.0.0.1:1/check",
                "valid-token",
                headers={name: "valid-value"},
            )
        assert name not in str(exc_info.value)
        assert name not in repr(exc_info.value)


def test_malformed_header_value_bytes_raise_without_value() -> None:
    bad_values = ["val\r\nnext", "val\rinvalid", "val\0null", "val\u1234"]
    for val in bad_values:
        with pytest.raises(ValueError) as exc_info:
            request_json(
                "GET",
                "http://127.0.0.1:1/check",
                "valid-token",
                headers={"X-Test": val},
            )
        assert val not in str(exc_info.value)
        assert val not in repr(exc_info.value)
        assert "X-Test" not in str(exc_info.value)


def test_invalid_token_raises_before_connecting() -> None:
    for bad_token in ("", None, "token\ninvalid", "token\rinvalid", "token\0invalid"):
        with pytest.raises(ValueError) as exc_info:
            request_json("GET", "http://127.0.0.1:1/check", bad_token)  # type: ignore[arg-type]
        if bad_token:
            assert bad_token not in str(exc_info.value)
            assert bad_token not in repr(exc_info.value)


def test_unserializable_body_raises_type_error_before_connecting() -> None:
    with pytest.raises(TypeError):
        request_json(
            "POST",
            "http://127.0.0.1:1/check",
            "some-token",
            body=object(),
        )
