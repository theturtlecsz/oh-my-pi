"""Tests for TLS inspection of egress tunnels (OMP-431).

The gateway is driven over its real unix socket with ``asyncio.run``. The
client trusts only ``GatewayCA.ca_cert_path``, so it verifies the CA-signed
leaf the gateway presents for a CONNECT destination. Upstream is a local
127.0.0.1 HTTPS stub signed by a separate test-owned CA, and
``upstream_ca_bundle`` names that CA. The stub records every request and SNI,
so a refusal is proven by the stub being untouched. The whole module skips
when ``openssl`` is absent, which is the only case where TLS inspection is
unavailable.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import ssl
import subprocess
import threading
from datetime import datetime, timezone
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
from omp_work.egress_tls import GatewayCA, TlsUnavailable

GLOBAL = "93.184.216.34"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TIMEOUT = 15.0
MODEL_PROXY = "proxy.example.com:8443"
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

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not found"
)


# -- fixtures and helpers -------------------------------------------------


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


def write_cert(tmp: Path, hostname: str, name: str = "") -> tuple[Path, Path, Path]:
    """Return ``(ca cert, leaf cert, leaf key)`` for an unrelated upstream CA."""
    tmp.mkdir(parents=True, exist_ok=True)
    prefix = tmp / name if name else tmp
    ca_key = Path(f"{prefix}-ca.key")
    ca_crt = Path(f"{prefix}-ca.crt")
    leaf_key = Path(f"{prefix}-leaf.key")
    leaf_csr = Path(f"{prefix}-leaf.csr")
    leaf_crt = Path(f"{prefix}-leaf.crt")
    ext = Path(f"{prefix}-leaf.ext")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(ca_key), "-out", str(ca_crt),
            "-days", "2", "-subj", "/CN=Upstream CA",
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
    ext.write_text(
        f"subjectAltName=DNS:{hostname}\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(leaf_csr),
            "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(leaf_crt), "-days", "2", "-extfile", str(ext),
        ],
        check=True,
        capture_output=True,
    )
    return ca_crt, leaf_crt, leaf_key


class TlsStub:
    """Threaded 127.0.0.1 HTTPS server. Records each request and its SNI."""

    def __init__(self, cert: str, key: str, responses: list[bytes] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[bytes] = []
        self.snis: list[str | None] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(cert, key)

        def sni_cb(_ssock: ssl.SSLSocket, name: str | None, _ctx: ssl.SSLContext) -> None:
            self.snis.append(name)

        self._ctx.sni_callback = sni_cb
        self._thread = threading.Thread(target=self._serve, name="egress-tls-stub", daemon=True)
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
                conn = self._ctx.wrap_socket(raw, server_side=True)
                data = read_http_message(conn)
                self.requests.append(data)
                reply = self.responses.pop(0) if self.responses else http_response(200, "OK", [], b"")
                conn.sendall(reply)
                conn.close()
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

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return [GLOBAL]


class PinConnector:
    """Maps the fake global address onto the stub. Anything else is an error."""

    def __init__(self) -> None:
        self.stub: TlsStub | None = None
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((ip, port, timeout))
        if ip != GLOBAL or self.stub is None:
            raise OSError(f"no stub for {ip}:{port}")
        return socket.create_connection(("127.0.0.1", self.stub.port), timeout)


def policy(
    *,
    registries: tuple[str, ...] = (),
    remotes: tuple[str, ...] = (),
) -> EgressPolicy:
    return build_policy(
        ProjectEgress(registries=registries, remotes=remotes, decision_id="dec-egress"),
        (),
        MODEL_PROXY,
    )


class Harness:
    """A GatewayCA, a gateway over one sandbox socket, a stub, and a recorder."""

    def __init__(
        self,
        tmp_path: Path,
        policy_value: EgressPolicy,
        *,
        identity: Identity = IDENTITY,
        upstream_cert: tuple[Path, Path] | None = None,
        want_fetch: bool = False,
    ) -> None:
        self.ca = GatewayCA(tmp_path / "tls")
        self.policy = policy_value
        self.identity = identity
        self.recorder = MemoryRecorder()
        self.resolver = MapResolver()
        self.connector = PinConnector()
        self.fetch = (
            ResearchFetchService(
                self.recorder,
                self.resolver,
                self.connector,
                str(upstream_cert[0]) if upstream_cert is not None else None,
                TIMEOUT,
            )
            if want_fetch
            else None
        )
        self.gateway = EgressGateway(
            policy_source=lambda identity, now: self.policy,
            recorder=self.recorder,
            fetch=self.fetch,
            resolver=self.resolver,
            connector=self.connector,
            model_proxy_upstream=None,
            clock=lambda: NOW,
            tls=self.ca,
            upstream_ca_bundle=str(upstream_cert[0]) if upstream_cert is not None else None,
        )
        self.dir = tmp_path / "sandbox"
        self.sock = self.gateway.open_sandbox(identity, self.dir)

    def add_stub(self, cert: Path, key: Path, responses: list[bytes] | None = None) -> TlsStub:
        stub = TlsStub(str(cert), str(key), responses)
        self.connector.stub = stub
        return stub

    def close(self) -> None:
        self.gateway.close_sandbox(self.identity)
        if self.connector.stub is not None:
            self.connector.stub.close()


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    return data


def client_context(ca_cert: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(ca_cert))
    return context


async def _inspect(
    sock_path: Path,
    authority: str,
    method: str,
    path: str,
    ca_cert: Path,
    body: bytes = b"",
) -> tuple[bytes, bool, bytes]:
    """CONNECT, upgrade with the gateway CA, send one inner request, read the reply."""
    host = authority.rsplit(":", 1)[0]
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        head = await _read_head(reader)
        if not head.startswith(b"HTTP/1.1 200"):
            return head, False, b""
        await writer.start_tls(client_context(ca_cert), server_hostname=host)
        lines = [f"{method} {path} HTTP/1.1", f"Host: {authority}"]
        if body or method in {"POST", "PUT"}:
            lines.append(f"Content-Length: {len(body)}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        resp = await asyncio.wait_for(_read_all(reader), 5.0)
        return head, True, resp
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def inspect(
    sock_path: Path, authority: str, method: str, path: str, ca_cert: Path, body: bytes = b""
) -> tuple[bytes, bool, bytes]:
    return asyncio.run(_inspect(sock_path, authority, method, path, ca_cert, body))


def only_record(harness: Harness):
    assert len(harness.recorder.records) == 1
    return harness.recorder.records[0]


def assert_refused(raw: bytes, code: str) -> None:
    status, _, body = parse_response(raw)
    assert status == 403
    assert json.loads(body) == {"code": code}


def assert_untouched(harness: Harness, stub: TlsStub) -> None:
    assert harness.connector.calls == []
    assert stub.requests == []


# -- allowed inspected tunnels --------------------------------------------


def test_registry_https_get_through_inspected_tunnel(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "reg.example.com")
    harness = Harness(
        tmp_path, policy(registries=("https://reg.example.com",)), upstream_cert=(upstream_ca, leaf)
    )
    stub = harness.add_stub(leaf, leaf_key, [http_response(200, "OK", [("X-Kept", "yes")], b"pkg")])
    try:
        head, upgraded, raw = inspect(
            harness.sock, "reg.example.com:443", "GET", "/v2/lib", harness.ca.ca_cert_path
        )
        assert head.split(b"\r\n", 1)[0] == b"HTTP/1.1 200 Connection Established"
        assert upgraded
        status, headers, body = parse_response(raw)
        assert status == 200
        assert headers["connection"] == "close"
        assert headers["x-kept"] == "yes"
        assert body == b"pkg"
        start, sent_headers, sent_body = parse_request(stub.requests[0])
        assert start == "GET /v2/lib HTTP/1.1"
        assert sent_headers["connection"] == "close"
        assert sent_body == b""
        assert stub.snis == ["reg.example.com"]
        assert [call[0] for call in harness.connector.calls] == [GLOBAL]
        assert harness.resolver.calls == [("reg.example.com", 443)]
        record = only_record(harness)
        assert record.channel == "http"
        assert record.protocol == "https"
        assert record.host == "reg.example.com"
        assert record.port == 443
        assert record.klass == "registry"
        assert record.outcome == "allowed"
        assert record.url == "https://reg.example.com:443/v2/lib"
        assert record.mission_id == IDENTITY.mission_id
        assert record.worker_id == IDENTITY.worker_id
        assert stub.errors == []
    finally:
        harness.close()


def test_registry_https_put_refused_recorded_stub_untouched(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "reg.example.com")
    harness = Harness(
        tmp_path, policy(registries=("https://reg.example.com",)), upstream_cert=(upstream_ca, leaf)
    )
    stub = harness.add_stub(leaf, leaf_key)
    try:
        _, upgraded, raw = inspect(
            harness.sock, "reg.example.com:443", "PUT", "/v2/lib", harness.ca.ca_cert_path, b"data"
        )
        assert upgraded
        assert_refused(raw, "method_not_allowed")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.outcome == "refused"
        assert record.code == "method_not_allowed"
        assert record.host == "reg.example.com"
        assert record.port == 443
        assert record.protocol == "https"
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/org/repo.git/info/refs?service=git-receive-pack"),
        ("POST", "/org/repo.git/git-receive-pack"),
    ],
)
def test_remote_https_receive_pack_refused_recorded(
    tmp_path: Path, method: str, path: str
) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "git.example.com")
    harness = Harness(
        tmp_path,
        policy(remotes=("https://git.example.com/org/repo",)),
        upstream_cert=(upstream_ca, leaf),
    )
    stub = harness.add_stub(leaf, leaf_key)
    try:
        _, upgraded, raw = inspect(
            harness.sock, "git.example.com:443", method, path, harness.ca.ca_cert_path, b"push"
        )
        assert upgraded
        assert_refused(raw, "git_receive_pack")
        assert_untouched(harness, stub)
        record = only_record(harness)
        assert record.outcome == "refused"
        assert record.code == "git_receive_pack"
        assert record.host == "git.example.com"
        assert record.port == 443
        assert record.method == method
        assert record.url == f"https://git.example.com:443{path}"
    finally:
        harness.close()


def test_remote_https_upload_pack_allowed_through_inspection(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "git.example.com")
    harness = Harness(
        tmp_path,
        policy(remotes=("https://git.example.com/org/repo",)),
        upstream_cert=(upstream_ca, leaf),
    )
    stub = harness.add_stub(leaf, leaf_key, [http_response(200, "OK", [], b"refs")])
    try:
        _, upgraded, raw = inspect(
            harness.sock,
            "git.example.com:443",
            "GET",
            "/org/repo.git/info/refs?service=git-upload-pack",
            harness.ca.ca_cert_path,
        )
        assert upgraded
        status, _, body = parse_response(raw)
        assert status == 200
        assert body == b"refs"
        start, _, _ = parse_request(stub.requests[0])
        assert start == "GET /org/repo.git/info/refs?service=git-upload-pack HTTP/1.1"
        record = only_record(harness)
        assert record.klass == "remote"
        assert record.outcome == "allowed"
        assert record.url == "https://git.example.com:443/org/repo.git/info/refs?service=git-upload-pack"
    finally:
        harness.close()


def test_research_https_get_recorded_with_mission_and_worker(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "news.example.com")
    harness = Harness(
        tmp_path, policy(), identity=RESEARCH, upstream_cert=(upstream_ca, leaf), want_fetch=True
    )
    stub = harness.add_stub(leaf, leaf_key, [http_response(200, "OK", [], b"feed")])
    try:
        _, upgraded, raw = inspect(
            harness.sock, "news.example.com:443", "GET", "/feed", harness.ca.ca_cert_path
        )
        assert upgraded
        status, _, body = parse_response(raw)
        assert status == 200
        assert body == b"feed"
        start, _, _ = parse_request(stub.requests[0])
        assert start == "GET /feed HTTP/1.1"
        assert stub.snis == ["news.example.com"]
        record = only_record(harness)
        assert record.channel == "fetch"
        assert record.klass == "research"
        assert record.outcome == "allowed"
        assert record.url == "https://news.example.com:443/feed"
        assert record.mission_id == RESEARCH.mission_id
        assert record.worker_id == RESEARCH.worker_id
    finally:
        harness.close()


# -- certificate authority ------------------------------------------------


def test_ca_key_is_0600_and_not_the_ca_cert_path(tmp_path: Path) -> None:
    ca = GatewayCA(tmp_path / "tls")
    ca_dir = ca.ca_cert_path.parent
    ca_key = ca_dir / "ca.key"
    assert ca.ca_cert_path.is_file()
    assert ca.ca_cert_path != ca_key
    assert ca_key.is_file()
    assert (ca_key.stat().st_mode & 0o777) == 0o600
    assert (ca_dir.stat().st_mode & 0o777) == 0o700
    # A second GatewayCA over the same directory reuses the files.
    again = GatewayCA(ca_dir)
    assert again.ca_cert_path == ca.ca_cert_path


def test_gateway_ca_requires_openssl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.egress_tls.shutil.which", lambda name: None)
    with pytest.raises(TlsUnavailable):
        GatewayCA(tmp_path / "tls")


def test_client_without_the_ca_cannot_verify_the_gateway_leaf(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", "reg.example.com")
    harness = Harness(
        tmp_path, policy(registries=("https://reg.example.com",)), upstream_cert=(upstream_ca, leaf)
    )
    stub = harness.add_stub(leaf, leaf_key)
    try:
        head, upgraded, raw = _run_untrusted(harness.sock, "reg.example.com:443")
        assert head.split(b"\r\n", 1)[0] == b"HTTP/1.1 200 Connection Established"
        assert upgraded is False
        assert raw == b""
        assert_untouched(harness, stub)
    finally:
        harness.close()


async def _untrusted_handshake(sock_path: Path, authority: str) -> tuple[bytes, bool, bytes]:
    host = authority.rsplit(":", 1)[0]
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        head = await _read_head(reader)
        try:
            await writer.start_tls(ssl.create_default_context(), server_hostname=host)
        except ssl.SSLError:
            return head, False, b""
        return head, True, b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def _run_untrusted(sock_path: Path, authority: str) -> tuple[bytes, bool, bytes]:
    return asyncio.run(_untrusted_handshake(sock_path, authority))


# -- upstream verification ------------------------------------------------


def test_upstream_cert_not_in_bundle_fails_recorded_and_stub_untouched(tmp_path: Path) -> None:
    good_ca, _, _ = write_cert(tmp_path / "good", "reg.example.com", "good")
    bad_ca, bad_leaf, bad_leaf_key = write_cert(tmp_path / "bad", "reg.example.com", "bad")
    harness = Harness(
        tmp_path,
        policy(registries=("https://reg.example.com",)),
        upstream_cert=(good_ca, good_ca),
    )
    stub = harness.add_stub(bad_leaf, bad_leaf_key)
    try:
        _, upgraded, raw = inspect(
            harness.sock, "reg.example.com:443", "GET", "/v2/lib", harness.ca.ca_cert_path
        )
        assert upgraded
        assert_refused(raw, "tls_error")
        assert stub.requests == []
        record = only_record(harness)
        assert record.outcome == "refused"
        assert record.code == "tls_error"
        assert record.host == "reg.example.com"
        assert record.protocol == "https"
        assert record.url == "https://reg.example.com:443/v2/lib"
        assert bad_ca != good_ca
    finally:
        harness.close()
