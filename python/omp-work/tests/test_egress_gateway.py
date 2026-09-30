"""Tests for the asyncio egress gateway (OMP-431).

The gateway serves one unix socket per sandbox identity. Every test drives a
real client connection with :func:`asyncio.run`, a local 127.0.0.1 stub per
upstream address, a resolver that answers the fake global address, and a
connector that maps that address onto the stub. The upstream stub records every
request byte, so a refusal is proven by the stub being untouched.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from omp_work.egress_fetch import ResearchFetchService
from omp_work.egress_gateway import EgressGateway
from omp_work.egress_policy import (
    EgressPolicy,
    Identity,
    MemoryRecorder,
    ProjectEgress,
    build_policy,
)
from omp_work.standing_policy import StandingPolicy

GLOBAL = "93.184.216.34"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TIMEOUT = 15.0
IDENTITY = Identity(
    workspace_id="ws-1",
    project_id="proj-1",
    mission_id="mission-7",
    worker_id="worker-7",
    stage="repository",
)
RESEARCH = Identity(
    workspace_id="ws-1",
    project_id="proj-1",
    mission_id="mission-7",
    worker_id="worker-7",
    stage="research",
)
MODEL_PROXY = "proxy.example.com:8443"
UPSTREAM = ("model-upstream.test", 9443)


def http_response(
    status: int, reason: str, headers: list[tuple[str, str]], body: bytes = b""
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


def parse_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if len(lines[0].split(" ")) > 1 else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.setdefault(name.lower(), value.strip())
    return status, headers, body


class SyncServer:
    """Threaded 127.0.0.1 server. Records each request and replies from a queue."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[bytes] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="egress-gw-stub", daemon=True)
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
                raw.settimeout(TIMEOUT)
                data = read_http_message(raw)
                self.requests.append(data)
                reply = self.responses.pop(0) if self.responses else http_response(200, "OK", [], b"")
                raw.sendall(reply)
            except Exception as exc:
                self.errors.append(exc)
            finally:
                try:
                    raw.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class MapResolver:
    """Returns the fake global address for every name."""

    def __init__(self, ips: list[str] | None = None) -> None:
        self.ips = list(ips or [GLOBAL])
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return list(self.ips)


class PinConnector:
    """Maps the fake global address onto a local stub. Anything else is an error."""

    def __init__(self, stubs: dict[str, SyncServer]) -> None:
        self.stubs = stubs
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((ip, port, timeout))
        stub = self.stubs.get(ip)
        if stub is None:
            raise OSError(f"no stub for {ip}:{port}")
        return socket.create_connection(("127.0.0.1", stub.port), timeout)


def grant(*destinations: str, expires_at: datetime | None = None, policy_id: str = "pol-1") -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="network_access",
        destinations=destinations,
        decision_id="dec-1",
        expires_at=expires_at,
    )


def policy(
    *,
    registries: tuple[str, ...] = (),
    remotes: tuple[str, ...] = (),
    standing: tuple[StandingPolicy, ...] = (),
    model_proxy: str = MODEL_PROXY,
) -> EgressPolicy:
    return build_policy(
        ProjectEgress(registries=registries, remotes=remotes, decision_id="dec-egress"),
        standing,
        model_proxy,
    )


class Harness:
    """A gateway, one sandbox socket, stubs per address, and a recorder."""

    def __init__(
        self,
        tmp_path: Path,
        policy_value: EgressPolicy,
        *,
        identity: Identity = IDENTITY,
        upstream: tuple[str, int] | None = None,
        want_fetch: bool = False,
    ) -> None:
        self.policy = policy_value
        self.identity = identity
        self.recorder = MemoryRecorder()
        self.stubs: dict[str, SyncServer] = {}
        self.resolver = MapResolver()
        self.connector = PinConnector(self.stubs)
        self.fetch = (
            ResearchFetchService(self.recorder, self.resolver, self.connector, None, TIMEOUT)
            if want_fetch
            else None
        )
        self.gateway = EgressGateway(
            policy_source=self._policy_source,
            recorder=self.recorder,
            fetch=self.fetch,
            resolver=self.resolver,
            connector=self.connector,
            model_proxy_upstream=upstream,
            clock=lambda: NOW,
        )
        self.dir = tmp_path / "sandbox"
        self.sock = self.gateway.open_sandbox(identity, self.dir)

    def _policy_source(self, identity: Identity, now: datetime) -> EgressPolicy:
        return self.policy

    def add_stub(self, ip: str = GLOBAL, responses: list[bytes] | None = None) -> SyncServer:
        stub = SyncServer(responses)
        self.stubs[ip] = stub
        return stub

    def close(self) -> None:
        self.gateway.close_sandbox(self.identity)
        for stub in self.stubs.values():
            stub.close()


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def _exchange(sock_path: Path, raw: bytes, timeout: float = 5.0) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(_read_all(reader), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def send(sock_path: Path, raw: bytes) -> bytes:
    return asyncio.run(_exchange(sock_path, raw))


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    return data


async def _connect(sock_path: Path, authority: str, extra: bytes = b"") -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode() + extra)
        await writer.drain()
        head = await asyncio.wait_for(_read_head(reader), 5.0)
        rest = b""
        if b"200" in head.split(b"\r\n", 1)[0]:
            try:
                rest = await asyncio.wait_for(_read_all(reader), 1.0)
            except TimeoutError:
                rest = b""
        return head, rest
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def connect(sock_path: Path, authority: str) -> bytes:
    head, _ = asyncio.run(_connect(sock_path, authority))
    return head


def get_request(host: str, path: str = "/a", headers: list[str] | None = None) -> bytes:
    lines = [f"GET http://{host}{path} HTTP/1.1", f"Host: {host}"]
    lines.extend(headers or [])
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def request(method: str, host: str, path: str = "/a", body: bytes = b"") -> bytes:
    lines = [f"{method} http://{host}{path} HTTP/1.1", f"Host: {host}"]
    if body or method in {"POST", "PUT"}:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def assert_refused(raw: bytes, code: str) -> None:
    status, _, body = parse_response(raw)
    assert status == 403
    assert json.loads(body) == {"code": code}


def only_record(harness: Harness):
    assert len(harness.recorder.records) == 1
    record = harness.recorder.records[0]
    assert record.mission_id == harness.identity.mission_id
    assert record.worker_id == harness.identity.worker_id
    assert record.workspace_id == harness.identity.workspace_id
    return record


def assert_untouched(harness: Harness, stub: SyncServer) -> None:
    assert harness.connector.calls == []
    assert stub.requests == []


# -- allowed reads --------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_registry_plain_read_allowed_without_standing(tmp_path: Path, method: str) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub(responses=[http_response(200, "OK", [("X-Kept", "yes")], b"pkg")])
    try:
        raw = send(harness.sock, request(method, "reg.example.com", "/v2/lib"))
        status, headers, body = parse_response(raw)
        assert status == 200
        assert headers["connection"] == "close"
        assert headers["x-kept"] == "yes"
        assert body == (b"" if method == "HEAD" else b"pkg")
        start, sent_headers, sent_body = parse_request(stub.requests[0])
        assert start == f"{method} /v2/lib HTTP/1.1"
        assert sent_headers["host"] == "reg.example.com"
        assert sent_headers["connection"] == "close"
        assert sent_body == b""
        assert [call[0] for call in harness.connector.calls] == [GLOBAL]
        assert harness.resolver.calls == [("reg.example.com", 443)]
        record = only_record(harness)
        assert record.klass == "registry"
        assert record.outcome == "allowed"
        assert record.host == "reg.example.com"
        assert record.channel == "http"
        assert stub.errors == []
    finally:
        harness.close()


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_registry_plain_write_refused(tmp_path: Path, method: str) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, request(method, "reg.example.com", "/v2/lib", b"data"))
        assert_refused(raw, "method_not_allowed")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.outcome == "refused"
        assert record.code == "method_not_allowed"
        assert record.channel == "http"
    finally:
        harness.close()


def test_remote_upload_pack_allowed(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, policy(remotes=("https://git.example.com/org/repo",))
    )
    stub = harness.add_stub(responses=[http_response(200, "OK", [], b"refs")])
    try:
        info = send(
            harness.sock,
            get_request("git.example.com", "/org/repo.git/info/refs?service=git-upload-pack"),
        )
        status, _, body = parse_response(info)
        assert status == 200
        assert body == b"refs"
        post = send(
            harness.sock,
            request("POST", "git.example.com", "/org/repo.git/git-upload-pack", b"want"),
        )
        assert parse_response(post)[0] == 200
        start, headers, body_sent = parse_request(stub.requests[0])
        assert start == "GET /org/repo.git/info/refs?service=git-upload-pack HTTP/1.1"
        assert headers["connection"] == "close"
        assert body_sent == b""
        start2, _, body2 = parse_request(stub.requests[1])
        assert start2 == "POST /org/repo.git/git-upload-pack HTTP/1.1"
        assert body2 == b"want"
        assert harness.connector.calls and harness.connector.calls[0][0] == GLOBAL
        records = harness.recorder.records
        assert [record.klass for record in records] == ["remote", "remote"]
        assert all(record.outcome == "allowed" for record in records)
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/org/repo.git/info/refs?service=git-receive-pack"),
        ("POST", "/org/repo.git/git-receive-pack"),
    ],
)
def test_remote_receive_pack_refused(tmp_path: Path, method: str, path: str) -> None:
    harness = Harness(
        tmp_path, policy(remotes=("https://git.example.com/org/repo",))
    )
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, request(method, "git.example.com", path, b"push"))
        assert_refused(raw, "git_receive_pack")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.code == "git_receive_pack"
        assert record.outcome == "refused"
    finally:
        harness.close()


# -- refusals -------------------------------------------------------------


def test_unnamed_host_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("evil.test", "/r"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
        assert only_record(harness).code == "destination_not_allowed"
    finally:
        harness.close()


@pytest.mark.parametrize("authority", ["93.184.216.34:443", "[2606:2800::1]:443"])
def test_connect_ip_literal_refused(tmp_path: Path, authority: str) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub()
    try:
        raw = connect(harness.sock, authority)
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.channel == "connect"
        assert record.method == "CONNECT"
        assert record.outcome == "refused"
    finally:
        harness.close()


def test_plain_http_to_ip_literal_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("93.184.216.34", "/a"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
    finally:
        harness.close()


def test_connect_to_named_registry_requires_tls_inspection(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub()
    try:
        raw = connect(harness.sock, "reg.example.com:443")
        assert_refused(raw, "tls_inspection_required")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.klass == "registry"
        assert record.channel == "connect"
        assert record.code == "tls_inspection_required"
    finally:
        harness.close()


def test_connect_to_named_remote_requires_tls_inspection(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(remotes=("https://git.example.com/org/repo",)))
    stub = harness.add_stub()
    try:
        raw = connect(harness.sock, "git.example.com:443")
        assert_refused(raw, "tls_inspection_required")
        assert_untouched(harness, stub)
        assert only_record(harness).klass == "remote"
    finally:
        harness.close()


def test_connect_research_requires_tls_inspection(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(), identity=RESEARCH, want_fetch=True)
    stub = harness.add_stub()
    try:
        raw = connect(harness.sock, "news.example.com:443")
        assert_refused(raw, "tls_inspection_required")
        assert_untouched(harness, stub)
        assert only_record(harness).klass == "research"
    finally:
        harness.close()


# -- standing -------------------------------------------------------------


def test_standing_destination_reachable_on_its_port(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(standing=(grant("api.partner.test:8443"),)))
    stub = harness.add_stub(responses=[http_response(200, "OK", [], b"ok")])
    try:
        raw = send(harness.sock, get_request("api.partner.test:8443", "/data"))
        status, _, body = parse_response(raw)
        assert status == 200
        assert body == b"ok"
        start, _, _ = parse_request(stub.requests[0])
        assert start == "GET /data HTTP/1.1"
        assert harness.resolver.calls == [("api.partner.test", 8443)]
        record = only_record(harness)
        assert record.klass == "standing"
        assert record.policy_id == "pol-1"
        assert record.outcome == "allowed"
    finally:
        harness.close()


@pytest.mark.parametrize(
    "target",
    ["api.partner.test:443", "other.partner.test:8443"],
)
def test_standing_other_port_or_host_refused(tmp_path: Path, target: str) -> None:
    harness = Harness(tmp_path, policy(standing=(grant("api.partner.test:8443"),)))
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request(target, "/data"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
    finally:
        harness.close()


def test_standing_expired_refused(tmp_path: Path) -> None:
    expired = grant("api.partner.test:8443", expires_at=NOW - timedelta(hours=1))
    harness = Harness(tmp_path, policy(standing=(expired,)))
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("api.partner.test:8443", "/data"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
    finally:
        harness.close()


def test_standing_removed_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy())
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("api.partner.test:8443", "/data"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
    finally:
        harness.close()


# -- model proxy ----------------------------------------------------------


def test_model_proxy_forwarded_to_upstream(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(), upstream=UPSTREAM)
    stub = harness.add_stub(responses=[http_response(200, "OK", [], b"answer")])
    try:
        raw = send(harness.sock, request("POST", "proxy.example.com:8443", "/v1/chat", b"{}"))
        status, _, body = parse_response(raw)
        assert status == 200
        assert body == b"answer"
        start, headers, sent = parse_request(stub.requests[0])
        assert start == "POST /v1/chat HTTP/1.1"
        assert headers["connection"] == "close"
        assert sent == b"{}"
        assert harness.resolver.calls == [(UPSTREAM[0], UPSTREAM[1])]
        record = only_record(harness)
        assert record.klass == "model"
        assert record.outcome == "allowed"
    finally:
        harness.close()


@pytest.mark.parametrize("host", ["proxy.example.com:443", "api.anthropic.com"])
def test_model_host_other_port_or_other_host_refused(tmp_path: Path, host: str) -> None:
    harness = Harness(tmp_path, policy(), upstream=UPSTREAM)
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, request("POST", host, "/v1/chat", b"{}"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
    finally:
        harness.close()


def test_connect_model_tunnelled(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(), upstream=UPSTREAM)
    harness.add_stub()
    try:
        head = connect(harness.sock, "proxy.example.com:8443")
        assert head.split(b"\r\n", 1)[0] == b"HTTP/1.1 200 Connection Established"
        assert harness.resolver.calls == [(UPSTREAM[0], UPSTREAM[1])]
        record = only_record(harness)
        assert record.channel == "connect"
        assert record.klass == "model"
        assert record.outcome == "allowed"
    finally:
        harness.close()


# -- identity and connection lifecycle ------------------------------------


def test_x_omp_mission_header_ignored(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub(responses=[http_response(200, "OK", [], b"ok")])
    try:
        raw = send(
            harness.sock,
            get_request("reg.example.com", "/a", ["X-OMP-Mission: smuggled-mission"]),
        )
        assert parse_response(raw)[0] == 200
        record = only_record(harness)
        assert record.mission_id == IDENTITY.mission_id
        assert record.worker_id == IDENTITY.worker_id
        _, headers, _ = parse_request(stub.requests[0])
        assert headers["x-omp-mission"] == "smuggled-mission"
    finally:
        harness.close()


def test_pipelined_second_request_dropped(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(registries=("https://reg.example.com",)))
    stub = harness.add_stub(
        responses=[http_response(200, "OK", [], b"first"), http_response(200, "OK", [], b"second")]
    )
    try:
        pipelined = request("GET", "reg.example.com", "/a") + request(
            "GET", "reg.example.com", "/b"
        )
        raw = send(harness.sock, pipelined)
        status, headers, body = parse_response(raw)
        assert status == 200
        assert body == b"first"
        assert headers["connection"] == "close"
        assert len(stub.requests) == 1
        start, _, _ = parse_request(stub.requests[0])
        assert start == "GET /a HTTP/1.1"
        assert len(harness.recorder.records) == 1
        assert harness.recorder.records[0].url.endswith("/a")
    finally:
        harness.close()


def test_sandbox_socket_mode_and_close(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy())
    try:
        assert harness.sock.name == "proxy.sock"
        assert harness.sock.parent == harness.dir
        mode = harness.sock.stat().st_mode & 0o777
        assert mode == 0o600
        assert harness.gateway.open_sandbox(harness.identity, harness.dir) == harness.sock
    finally:
        harness.gateway.close_sandbox(harness.identity)
    assert not harness.sock.exists()
    for stub in harness.stubs.values():
        stub.close()


# -- research stage -------------------------------------------------------


def test_unnamed_public_get_repository_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy())
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("news.example.com", "/feed"))
        assert_refused(raw, "destination_not_allowed")
        assert_untouched(harness, stub)
        assert only_record(harness).code == "destination_not_allowed"
    finally:
        harness.close()


def test_unnamed_public_get_research_served_by_fetch(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(), identity=RESEARCH, want_fetch=True)
    stub = harness.add_stub(responses=[http_response(200, "OK", [], b"feed")])
    try:
        raw = send(harness.sock, get_request("news.example.com", "/feed"))
        status, headers, body = parse_response(raw)
        assert status == 200
        assert body == b"feed"
        assert headers["connection"] == "close"
        start, sent_headers, _ = parse_request(stub.requests[0])
        assert start == "GET /feed HTTP/1.1"
        # The s03 fetch service issues its own one-shot request; the gateway
        # adds no Connection header on that path.
        assert "connection" not in sent_headers
        record = only_record(harness)
        assert record.channel == "fetch"
        assert record.klass == "research"
        assert record.outcome == "allowed"
        assert record.url == "http://news.example.com/feed"
        assert record.mission_id == RESEARCH.mission_id
        assert record.worker_id == RESEARCH.worker_id
    finally:
        harness.close()


def test_research_fetch_refusal_relayed_as_403(tmp_path: Path) -> None:
    harness = Harness(tmp_path, policy(), identity=RESEARCH, want_fetch=True)
    stub = harness.add_stub()
    try:
        raw = send(harness.sock, get_request("news.example.com", "/feed", ["Authorization: Bearer t"]))
        assert_refused(raw, "auth_header")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.channel == "fetch"
        assert record.outcome == "refused"
        assert record.code == "auth_header"
    finally:
        harness.close()
