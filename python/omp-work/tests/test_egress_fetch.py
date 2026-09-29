"""Tests for pinned egress fetch and push (OMP-431)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from omp_work.egress_fetch import (
    EgressRefused,
    ResearchFetchService,
    check_push_destination,
    deliver_push,
    open_pinned,
)
from omp_work.egress_policy import Identity, MemoryRecorder, blocked_address
from omp_work.standing_policy import StandingPolicy

GLOBAL = "93.184.216.34"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
IDENTITY = Identity(
    workspace_id="ws-1",
    project_id="proj-1",
    mission_id="mission-7",
    worker_id="worker-7",
    stage="research",
)
BLOCKED = [
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "fe80::1",
    "169.254.169.254",
    "168.63.129.16",
    "fd00:ec2::254",
    "::ffff:127.0.0.1",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
]
TIMEOUT = 15.0


class ScriptResolver:
    """Returns each scripted answer once, in order."""

    def __init__(self, script: list[list[str]]) -> None:
        self.script = [list(item) for item in script]
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if not self.script:
            raise AssertionError(f"resolver exhausted at {host}:{port}")
        return list(self.script.pop(0))


class MapResolver:
    """Returns the same addresses on every call."""

    def __init__(self, ips: list[str]) -> None:
        self.ips = list(ips)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return list(self.ips)


class PinConnector:
    """Maps 93.184.216.34 onto a local stub. Any other address is refused."""

    def __init__(self, stub: HTTPStub | None) -> None:
        self.stub = stub
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((ip, port, timeout))
        if ip != GLOBAL or self.stub is None:
            raise AssertionError(f"connector called for {ip}")
        return socket.create_connection(("127.0.0.1", self.stub.port), timeout)


def http_response(
    status: int,
    reason: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
) -> bytes:
    pairs = list(headers)
    if not any(name.lower() == "content-length" for name, _ in pairs):
        pairs.append(("Content-Length", str(len(body))))
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name}: {value}" for name, value in pairs)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def read_http_message(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, sep, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip() or b"0")
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + sep + rest[:length]


def parse_request(raw: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.setdefault(name.lower(), value.strip())
    return lines[0], headers, body


class HTTPStub:
    """Local HTTP(S) server. Records each request and replies from a queue."""

    def __init__(
        self,
        responses: list[bytes],
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[bytes] = []
        self.snis: list[str | None] = []
        self.errors: list[BaseException] = []
        self._tls = tls
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="egress-stub", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._handle(raw)
            except Exception as exc:
                self.errors.append(exc)
            finally:
                try:
                    raw.close()
                except OSError:
                    pass

    def _handle(self, raw: socket.socket) -> None:
        raw.settimeout(TIMEOUT)
        conn: socket.socket = raw
        if self._tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self._tls[0], self._tls[1])

            def sni_cb(ssock: ssl.SSLSocket, name: str | None, _initial: ssl.SSLContext) -> None:
                self.snis.append(name)

            context.sni_callback = sni_cb
            conn = context.wrap_socket(raw, server_side=True)
        data = read_http_message(conn)
        self.requests.append(data)
        reply = self.responses.pop(0) if self.responses else http_response(200, "OK", [], b"")
        conn.sendall(reply)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def write_cert(tmp: Path, hostname: str) -> tuple[Path, Path, Path]:
    """Return ``(ca cert, leaf cert, leaf key)`` for ``hostname``."""
    ca_key = tmp / "ca.key"
    ca_crt = tmp / "ca.crt"
    leaf_key = tmp / "leaf.key"
    leaf_csr = tmp / "leaf.csr"
    leaf_crt = tmp / "leaf.crt"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(ca_key), "-out", str(ca_crt),
            "-days", "2", "-subj", "/CN=Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(leaf_key), "-out", str(leaf_csr),
            "-subj", f"/CN={hostname}",
        ],
        check=True,
        capture_output=True,
    )
    ext = tmp / "ext.cnf"
    ext.write_text(
        "subjectAltName=DNS:" + hostname + "\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(leaf_csr),
            "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(leaf_crt), "-days", "2",
            "-extfile", str(ext),
        ],
        check=True,
        capture_output=True,
    )
    return ca_crt, leaf_crt, leaf_key


def grant(
    *destinations: str,
    expires_at: datetime | None = None,
    action_class: str = "network_access",
    policy_id: str = "pol-1",
) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class=action_class,
        destinations=destinations,
        decision_id="dec-1",
        expires_at=expires_at,
    )


def service(
    recorder: MemoryRecorder,
    resolver: MapResolver | ScriptResolver,
    connector: PinConnector,
    ca_bundle: str | None = None,
) -> ResearchFetchService:
    return ResearchFetchService(recorder, resolver, connector, ca_bundle, timeout=TIMEOUT)


def assert_untouched(connector: PinConnector, stub: HTTPStub) -> None:
    assert connector.calls == []
    assert stub.requests == []


def fetch_record(recorder: MemoryRecorder):
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.channel == "fetch"
    assert record.mission_id == IDENTITY.mission_id
    assert record.worker_id == IDENTITY.worker_id
    assert record.workspace_id == IDENTITY.workspace_id
    return record


@pytest.mark.parametrize("ip", BLOCKED)
def test_fetch_refuses_blocked_name(ip: str) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([ip])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    url = "http://blocked.test/a"
    try:
        with pytest.raises(EgressRefused) as exc:
            service(recorder, resolver, connector).fetch(IDENTITY, "GET", url, {}, None)
        assert exc.value.code == blocked_address(ip)
        assert exc.value.ip == ip
        assert_untouched(connector, stub)
        record = fetch_record(recorder)
        assert record.url == url
        assert record.outcome == "refused"
        assert record.code == blocked_address(ip)
        assert record.ip == ip
        assert len(resolver.calls) == 1
    finally:
        stub.close()


def test_fetch_refuses_public_registration_then_loopback_at_connect() -> None:
    recorder = MemoryRecorder()
    resolver = ScriptResolver([[GLOBAL], ["127.0.0.1"]])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    url = "http://example.com/a"
    try:
        with pytest.raises(EgressRefused) as exc:
            service(recorder, resolver, connector).fetch(IDENTITY, "GET", url, {}, None)
        assert exc.value.code == "not_global"
        assert exc.value.ip == "127.0.0.1"
        assert resolver.calls == [("example.com", 80), ("example.com", 80)]
        assert_untouched(connector, stub)
        record = fetch_record(recorder)
        assert record.url == url
        assert record.outcome == "refused"
        assert record.code == "not_global"
    finally:
        stub.close()


def test_fetch_refuses_when_any_address_is_blocked() -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL, "127.0.0.1"])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    try:
        with pytest.raises(EgressRefused) as exc:
            service(recorder, resolver, connector).fetch(
                IDENTITY, "GET", "http://example.com/a", {}, None
            )
        assert exc.value.code == "not_global"
        assert len(resolver.calls) == 1
        assert_untouched(connector, stub)
    finally:
        stub.close()


def test_fetch_connects_to_the_fresh_lookup_not_the_registration_address() -> None:
    recorder = MemoryRecorder()
    resolver = ScriptResolver([["1.1.1.1"], [GLOBAL]])
    stub = HTTPStub([http_response(200, "OK", [], b"ok")])
    connector = PinConnector(stub)
    try:
        result = service(recorder, resolver, connector).fetch(
            IDENTITY, "GET", "http://example.com/a", {}, None
        )
        assert result.status == 200
        assert result.body == b"ok"
        assert [call[0] for call in connector.calls] == [GLOBAL]
        assert resolver.calls == [("example.com", 80), ("example.com", 80)]
        record = fetch_record(recorder)
        assert record.outcome == "allowed"
        assert record.ip == GLOBAL
    finally:
        stub.close()


@pytest.mark.parametrize(
    ("method", "url", "headers", "body", "code"),
    [
        ("GET", "https://example.com/a", {"Authorization": "Bearer x"}, None, "auth_header"),
        ("GET", "https://example.com/a", {"Proxy-Authorization": "Basic x"}, None, "auth_header"),
        ("GET", "https://example.com/a", {"Cookie": "a=1"}, None, "auth_header"),
        ("GET", "https://user:secret@example.com/a", {}, None, "url_credentials"),
        ("GET", "https://example.com/a?access_token=", {}, None, "query_credential"),
        ("GET", "https://example.com/a?api_key=", {}, None, "query_credential"),
        ("DELETE", "https://example.com/a", {}, None, "method_not_allowed"),
        ("PUT", "https://example.com/a", {}, None, "method_not_allowed"),
        ("POST", "https://example.com/api/items", {}, None, "method_not_allowed"),
        ("POST", "https://example.com/checkout", {}, None, "method_not_allowed"),
        ("POST", "https://example.com/terms/accept", {}, None, "method_not_allowed"),
        ("POST", "https://example.com/oauth/token", {}, None, "method_not_allowed"),
        ("GET", "https://example.com/a", {}, b"payload", "request_body"),
    ],
)
def test_research_kinds_refused(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    code: str,
) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    try:
        with pytest.raises(EgressRefused) as exc:
            service(recorder, resolver, connector).fetch(IDENTITY, method, url, headers, body)
        assert exc.value.code == code
        assert resolver.calls == []
        assert_untouched(connector, stub)
        record = fetch_record(recorder)
        assert record.url == url
        assert record.outcome == "refused"
        assert record.code == code
        assert record.method == method.upper()
    finally:
        stub.close()


@pytest.mark.parametrize("url", ["ftp://example.com/a", "file:///etc/passwd"])
def test_fetch_refuses_non_http_scheme(url: str) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    try:
        with pytest.raises(EgressRefused) as exc:
            service(recorder, resolver, connector).fetch(IDENTITY, "GET", url, {}, None)
        assert exc.value.code == "scheme_not_allowed"
        assert resolver.calls == []
        assert_untouched(connector, stub)
        record = fetch_record(recorder)
        assert record.url == url
        assert record.outcome == "refused"
        assert record.code == "scheme_not_allowed"
    finally:
        stub.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_allowed_fetch_reaches_stub_without_credentials(method: str) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    payload = b"" if method == "HEAD" else b"hello"
    response = http_response(
        200,
        "OK",
        [
            ("X-Kept", "yes"),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("Set-Cookie2", "c=3"),
        ],
        payload,
    )
    stub = HTTPStub([response])
    connector = PinConnector(stub)
    url = "http://Example.COM./a?q=1"
    try:
        result = service(recorder, resolver, connector).fetch(
            IDENTITY, method, url, {"X-Trace": "t"}, None
        )
        assert result.status == 200
        assert result.body == payload
        names = [name.lower() for name, _ in result.headers]
        assert "set-cookie" not in names
        assert "set-cookie2" not in names
        assert ("X-Kept", "yes") in result.headers
        assert len(stub.requests) == 1
        start, headers, body = parse_request(stub.requests[0])
        assert start == f"{method} /a?q=1 HTTP/1.1"
        assert headers["host"] == "example.com"
        assert headers["x-trace"] == "t"
        assert "cookie" not in headers
        assert "authorization" not in headers
        assert "proxy-authorization" not in headers
        assert body == b""
        assert [call[0] for call in connector.calls] == [GLOBAL]
        record = fetch_record(recorder)
        assert record.url == url
        assert record.outcome == "allowed"
        assert record.ip == GLOBAL
        assert record.method == method
        assert record.host == "example.com"
        assert record.code is None
        assert record.klass == "research"
        assert len(resolver.calls) == 2
        assert stub.errors == []
    finally:
        stub.close()


def test_fetch_does_not_keep_a_cookie_jar() -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub(
        [
            http_response(200, "OK", [("Set-Cookie", "a=1"), ("Set-Cookie2", "b=2")], b"one"),
            http_response(200, "OK", [], b"two"),
        ]
    )
    connector = PinConnector(stub)
    try:
        client = service(recorder, resolver, connector)
        first = client.fetch(IDENTITY, "GET", "http://example.com/a", {}, None)
        second = client.fetch(IDENTITY, "GET", "http://example.com/a", {}, None)
        assert first.body == b"one"
        assert second.body == b"two"
        assert "set-cookie" not in [name.lower() for name, _ in first.headers]
        assert len(stub.requests) == 2
        for raw in stub.requests:
            _, headers, _ = parse_request(raw)
            assert "cookie" not in headers
            assert "authorization" not in headers
        assert len(connector.calls) == 2
        assert [record.outcome for record in recorder.records] == ["allowed", "allowed"]
    finally:
        stub.close()


@pytest.mark.parametrize(
    ("status", "reason", "extra"),
    [
        (302, "Found", [("Location", "http://127.0.0.1/secret")]),
        (401, "Unauthorized", [("WWW-Authenticate", "Bearer")]),
    ],
)
def test_fetch_returns_redirect_and_unauthorized_as_is(
    status: int,
    reason: str,
    extra: list[tuple[str, str]],
) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub([http_response(status, reason, extra, b"nope")])
    connector = PinConnector(stub)
    try:
        result = service(recorder, resolver, connector).fetch(
            IDENTITY, "GET", "http://example.com/a", {}, None
        )
        assert result.status == status
        assert result.body == b"nope"
        assert len(stub.requests) == 1
        assert len(connector.calls) == 1
        assert [host for host, _ in resolver.calls] == ["example.com", "example.com"]
        if status == 302:
            assert ("Location", "http://127.0.0.1/secret") in result.headers
        record = fetch_record(recorder)
        assert record.outcome == "allowed"
        assert record.url == "http://example.com/a"
    finally:
        stub.close()


def test_https_get_verifies_certificate_and_sni(tmp_path: Path) -> None:
    ca, leaf, key = write_cert(tmp_path, "example.com")
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub(
        [http_response(200, "OK", [("Set-Cookie", "a=1")], b"secure")],
        tls=(str(leaf), str(key)),
    )
    connector = PinConnector(stub)
    try:
        result = service(recorder, resolver, connector, str(ca)).fetch(
            IDENTITY, "GET", "https://example.com/secure", {}, None
        )
        assert result.status == 200
        assert result.body == b"secure"
        assert stub.snis == ["example.com"]
        assert "set-cookie" not in [name.lower() for name, _ in result.headers]
        start, headers, _ = parse_request(stub.requests[0])
        assert start == "GET /secure HTTP/1.1"
        assert headers["host"] == "example.com"
        assert "cookie" not in headers
        assert "authorization" not in headers
        record = fetch_record(recorder)
        assert record.outcome == "allowed"
        assert record.protocol == "https"
        assert record.ip == GLOBAL
        assert stub.errors == []
    finally:
        stub.close()


def test_https_fetch_records_tls_verification_failure(tmp_path: Path) -> None:
    ca, leaf, key = write_cert(tmp_path, "other.example")
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub([http_response(200, "OK", [], b"")], tls=(str(leaf), str(key)))
    connector = PinConnector(stub)
    try:
        with pytest.raises(ssl.SSLError):
            service(recorder, resolver, connector, str(ca)).fetch(
                IDENTITY, "GET", "https://example.com/a", {}, None
            )
        record = fetch_record(recorder)
        assert record.outcome == "refused"
        assert record.code == "tls_error"
        assert record.url == "https://example.com/a"
        assert connector.calls and connector.calls[0][0] == GLOBAL
    finally:
        stub.close()


def test_open_pinned_refuses_blocked_address_without_connecting() -> None:
    resolver = MapResolver(["10.0.0.1"])
    connector = PinConnector(None)
    with pytest.raises(EgressRefused) as exc:
        open_pinned("example.com", 443, resolver, connector, False, timeout=TIMEOUT)
    assert exc.value.code == "not_global"
    assert connector.calls == []
    assert resolver.calls == [("example.com", 443)]


def test_open_pinned_resolves_again_on_the_next_call() -> None:
    resolver = ScriptResolver([[GLOBAL], ["127.0.0.1"]])
    stub = HTTPStub([http_response(200, "OK", [], b"")])
    connector = PinConnector(stub)
    try:
        sock = open_pinned("example.com", 80, resolver, connector, False, timeout=TIMEOUT)
        sock.close()
        assert [call[0] for call in connector.calls] == [GLOBAL]
        with pytest.raises(EgressRefused) as exc:
            open_pinned("example.com", 80, resolver, connector, False, timeout=TIMEOUT)
        assert exc.value.code == "not_global"
        assert len(connector.calls) == 1
    finally:
        stub.close()


@pytest.mark.parametrize("ip", BLOCKED)
def test_push_refuses_blocked_name(ip: str) -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([ip])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    url = "https://blocked.test/hook"
    try:
        with pytest.raises(EgressRefused) as exc:
            deliver_push(
                url,
                b"{}",
                {},
                [grant("blocked.test", expires_at=NOW + timedelta(hours=1))],
                recorder,
                resolver,
                connector,
                NOW,
                timeout=TIMEOUT,
            )
        assert exc.value.code == blocked_address(ip)
        assert_untouched(connector, stub)
        assert len(recorder.records) == 1
        record = recorder.records[0]
        assert record.channel == "push"
        assert record.url == url
        assert record.outcome == "refused"
        assert record.code == blocked_address(ip)
        assert record.method == "POST"
        assert record.ip == ip
    finally:
        stub.close()


def test_push_refuses_public_registration_then_loopback_at_connect() -> None:
    recorder = MemoryRecorder()
    resolver = ScriptResolver([[GLOBAL], ["127.0.0.1"]])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    url = "https://example.com/hook"
    try:
        with pytest.raises(EgressRefused) as exc:
            deliver_push(
                url,
                b"{}",
                {},
                [grant("example.com")],
                recorder,
                resolver,
                connector,
                NOW,
                timeout=TIMEOUT,
            )
        assert exc.value.code == "not_global"
        assert resolver.calls == [("example.com", 443), ("example.com", 443)]
        assert_untouched(connector, stub)
        record = recorder.records[0]
        assert record.channel == "push"
        assert record.url == url
        assert record.outcome == "refused"
        assert record.klass == "standing"
        assert record.policy_id == "pol-1"
    finally:
        stub.close()


def test_push_refuses_http_uncovered_and_expired() -> None:
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub([])
    connector = PinConnector(stub)
    try:
        cases = [
            ("http://example.com/hook", [grant("example.com")], NOW, "scheme_not_allowed"),
            (
                "https://example.com/hook",
                [grant("other.example")],
                NOW,
                "destination_not_allowed",
            ),
            (
                "https://example.com/hook",
                [grant("example.com", expires_at=NOW)],
                NOW,
                "destination_not_allowed",
            ),
            (
                "https://example.com/hook",
                [grant("example.com", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))],
                datetime(2099, 1, 1, tzinfo=timezone.utc),
                "destination_not_allowed",
            ),
            (
                "https://example.com/hook",
                [grant("example.com", action_class="push_branch")],
                NOW,
                "destination_not_allowed",
            ),
        ]
        for url, policies, moment, code in cases:
            before = len(recorder.records)
            with pytest.raises(EgressRefused) as exc:
                deliver_push(
                    url, b"{}", {}, policies, recorder, resolver, connector, moment, timeout=TIMEOUT
                )
            assert exc.value.code == code
            record = recorder.records[-1]
            assert len(recorder.records) == before + 1
            assert record.channel == "push"
            assert record.url == url
            assert record.outcome == "refused"
            assert record.code == code
        assert resolver.calls == []
        assert_untouched(connector, stub)
    finally:
        stub.close()


def test_push_grant_uses_caller_now_not_the_wall_clock() -> None:
    # Expired on the wall clock (2026), still active at the caller's now.
    policy = grant("example.com", expires_at=datetime(2020, 6, 1, tzinfo=timezone.utc))
    assert (
        check_push_destination(
            "https://example.com/hook",
            [policy],
            datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        == "pol-1"
    )


@pytest.mark.parametrize(
    "destination",
    [
        "example.com",
        "https://example.com",
        "https://example.com/",
        "https://Example.COM.",
        "example.com:443",
    ],
)
def test_push_origin_matches_network_access_destination(destination: str) -> None:
    assert (
        check_push_destination("https://example.com/hook?x=1", [grant(destination)], NOW)
        == "pol-1"
    )


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.com/hook",
        "example.com:8443",
        "https://user@example.com",
        "http://example.com",
        "other.example",
    ],
)
def test_push_origin_rejects_destination_that_does_not_grant(destination: str) -> None:
    with pytest.raises(EgressRefused) as exc:
        check_push_destination("https://example.com/hook", [grant(destination)], NOW)
    assert exc.value.code == "destination_not_allowed"


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "169.254.169.254", "64:ff9b::a9fe:a9fe"])
def test_push_refuses_blocked_ip_literal(ip: str) -> None:
    host = f"[{ip}]" if ":" in ip else ip
    resolver = MapResolver([GLOBAL])
    with pytest.raises(EgressRefused) as exc:
        check_push_destination(f"https://{host}/x", [grant(ip)], NOW)
    assert exc.value.code == blocked_address(ip)
    assert resolver.calls == []


def test_push_refuses_userinfo() -> None:
    with pytest.raises(EgressRefused) as exc:
        check_push_destination("https://user:secret@example.com/hook", [grant("example.com")], NOW)
    assert exc.value.code == "url_credentials"


def test_deliver_push_posts_through_pinned_tls(tmp_path: Path) -> None:
    ca, leaf, key = write_cert(tmp_path, "example.com")
    recorder = MemoryRecorder()
    resolver = MapResolver([GLOBAL])
    stub = HTTPStub(
        [http_response(204, "No Content", [("Set-Cookie", "a=1"), ("X-Push", "ok")], b"")],
        tls=(str(leaf), str(key)),
    )
    connector = PinConnector(stub)
    url = "https://example.com/hook"
    body = b'{"n":1}'
    try:
        result = deliver_push(
            url,
            body,
            {"X-Trace": "p"},
            [grant("example.com", expires_at=NOW + timedelta(days=1))],
            recorder,
            resolver,
            connector,
            NOW,
            ca_bundle=str(ca),
            timeout=TIMEOUT,
        )
        assert result.status == 204
        assert "set-cookie" not in [name.lower() for name, _ in result.headers]
        assert ("X-Push", "ok") in result.headers
        start, headers, raw_body = parse_request(stub.requests[0])
        assert start == "POST /hook HTTP/1.1"
        assert headers["host"] == "example.com"
        assert headers["x-trace"] == "p"
        assert "cookie" not in headers
        assert "authorization" not in headers
        assert raw_body == body
        assert stub.snis == ["example.com"]
        assert [call[0] for call in connector.calls] == [GLOBAL]
        assert len(recorder.records) == 1
        record = recorder.records[0]
        assert record.channel == "push"
        assert record.url == url
        assert record.outcome == "allowed"
        assert record.method == "POST"
        assert record.ip == GLOBAL
        assert record.policy_id == "pol-1"
        assert record.klass == "standing"
        assert record.at == NOW
        assert stub.errors == []
    finally:
        stub.close()
