"""Asyncio egress HTTP proxy for sandboxed workers (OMP-431).

Each sandbox identity gets exactly one unix socket, ``<dir>/proxy.sock`` (mode
0600), created by :meth:`EgressGateway.open_sandbox` and removed by
:meth:`EgressGateway.close_sandbox`. The identity is the sandbox binding, never
anything a client sends: request bytes are carried to the upstream destination
untouched and a header such as ``X-OMP-Mission`` is never read.

Every client connection carries exactly one request. The policy is read fresh
from ``policy_source(identity, now)`` for that request, and every client
response carries ``Connection: close``. The connection is then closed, so bytes
a client pipelines behind the first request are never forwarded and the client
reads EOF.

Plain absolute-form requests (``GET http://host/path HTTP/1.1``):

* an egress refusal is answered ``403`` with a JSON ``{"code": ...}`` body;
* a research identity delegates to the s03 fetch service and relays its
  :class:`FetchResult` (the fetch service records the call itself);
* a model-proxy destination is forwarded to ``model_proxy_upstream``;
* an allowed registry, remote, or standing destination is forwarded to
  ``resolver(host)`` and ``connector(ip, port, timeout)`` in origin-form.

The policy stores a registry as an ``https`` origin, which :func:`decide` never
matches for an ``http`` request. An ``http`` absolute-form request whose
destination the policy also defines as an ``https`` origin is re-judged as that
https origin (:func:`_plain_decision`), so a named registry or remote is
reachable in plain form while a genuinely unnamed destination still refuses.

``CONNECT host:port`` is judged as an ``https`` tunnel. Model and standing
destinations are tunnelled and recorded. Registry, remote, and research
destinations need TLS inspection:

* with ``tls`` unset they are refused ``tls_inspection_required`` and recorded;
* with ``tls`` set to a :class:`GatewayCA` the gateway replies ``200``, upgrades
  the client connection to a CA-signed leaf for the host, reads exactly one
  inner request, and re-runs the plain-http path for it as an ``https`` request
  with ``tunnel=False`` (the same one-request, ``Connection: close`` rule). The
  upstream leg is TLS with SNI equal to the host, verified against system trust
  or ``upstream_ca_bundle``; research goes to the s03 fetch service as an
  ``https`` URL. An upstream certificate that does not verify fails the
  request, records it, and no inner request reaches the upstream.

Anything else is refused with the verdict :func:`decide` returns; an IP literal,
named or not, is never a client-reachable destination.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from omp_work.egress_fetch import Connector, EgressRefused, FetchResult, Resolver
from omp_work.egress_policy import (
    EgressPolicy,
    EgressRecord,
    EgressRecorder,
    Identity,
    Request,
    Verdict,
    _is_expired,
    _norm_host,
    _parse_destination,
    _parse_host_port,
    _request_port,
    decide,
)
from omp_work.egress_tls import GatewayCA

__all__ = ["EgressGateway", "PolicySource", "ProxyRequest", "ResearchFetch"]

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PLAIN_SCHEMES = frozenset({"http", "https"})
# A tunnel to one of these classes needs TLS inspection.
_INSPECT_CLASSES = frozenset({"registry", "remote", "research"})
# The absolute-form scheme an inspected tunnel uses for the inner request and
# for the upstream leg. The client's absolute-form target is ignored.
_INSPECT_SCHEME = "https"

_REASONS: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    502: "Bad Gateway",
}
_MAX_HEAD = 65536
_DROP_REQUEST_HEADERS = frozenset({"connection", "proxy-connection", "keep-alive"})
_DROP_RESPONSE_HEADERS = frozenset({"connection", "content-length", "transfer-encoding"})

PolicySource = Callable[[Identity, datetime], EgressPolicy]


class ResearchFetch(Protocol):
    """The s03 fetch surface the gateway delegates research requests to."""

    def fetch(
        self,
        identity: Identity,
        method: str,
        url: str,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
        body: bytes | str | None,
    ) -> FetchResult:
        ...


@dataclass(frozen=True)
class ProxyRequest:
    """One parsed client request. A connection carries at most one."""

    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _response_bytes(
    status: int,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    reason: str | None = None,
    content_length: int | None = None,
) -> bytes:
    """Frame a client response. ``Connection`` and ``Content-Length`` are forced.

    ``content_length`` overrides the byte length, which a HEAD response needs:
    it carries the length the GET would return but no body.
    """
    kept = [(n, v) for n, v in headers if n.lower() not in _DROP_RESPONSE_HEADERS]
    length = len(body) if content_length is None else content_length
    kept.append(("Content-Length", str(length)))
    kept.append(("Connection", "close"))
    text = reason if reason is not None else _REASONS.get(status, "")
    head = f"HTTP/1.1 {status} {text}\r\n"
    head += "".join(f"{n}: {v}\r\n" for n, v in kept)
    return (head + "\r\n").encode("iso-8859-1") + body


def _refusal_bytes(code: str) -> bytes:
    return _response_bytes(
        403, [("Content-Type", "application/json")], json.dumps({"code": code}).encode("utf-8")
    )


async def _read_head(reader: asyncio.StreamReader, limit: int = _MAX_HEAD) -> bytes | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        data += chunk
        if len(data) > limit:
            return None
    return data


def _parse_head(blob: bytes) -> tuple[list[str], list[tuple[str, str]]]:
    lines = blob.decode("iso-8859-1").split("\r\n")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name, value.strip()))
    return lines, headers


def _content_length(headers: Sequence[tuple[str, str]]) -> int | None:
    for name, value in headers:
        if name.lower() == "content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _tunnel_request(host: str, port: int, headers: Sequence[tuple[str, str]]) -> Request:
    """The decision request for a CONNECT tunnel: an ``https`` CONNECT to ``host:port``."""
    return Request(
        scheme=_INSPECT_SCHEME,
        host=host,
        port=port,
        method="CONNECT",
        path="",
        query="",
        headers=headers,
        has_body=False,
        tunnel=True,
    )


async def _read_request(reader: asyncio.StreamReader) -> ProxyRequest | None:
    """Read exactly one request. Bytes past its body are dropped with the buffer."""
    head = await _read_head(reader)
    if head is None:
        return None
    blob, _, rest = head.partition(b"\r\n\r\n")
    lines, headers = _parse_head(blob)
    start = lines[0].split(" ")
    if len(start) != 3 or not start[0] or not start[1]:
        return None
    method, target, version = start
    length = _content_length(headers)
    if length is None:
        # No declared body: everything after the head is dropped, including a
        # pipelined second request.
        return ProxyRequest(method.upper(), target, version, tuple(headers), b"")
    body = rest
    while len(body) < length:
        chunk = await reader.read(length - len(body))
        if not chunk:
            break
        body += chunk
    return ProxyRequest(method.upper(), target, version, tuple(headers), body[:length])


async def _read_upstream_response(
    reader: asyncio.StreamReader, head_only: bool = False
) -> tuple[int, str, list[tuple[str, str]], bytes] | None:
    head = await _read_head(reader)
    if head is None:
        return None
    blob, _, rest = head.partition(b"\r\n\r\n")
    lines, headers = _parse_head(blob)
    start = lines[0].split(" ", 2)
    status = int(start[1]) if len(start) > 1 and start[1].isdigit() else 0
    reason = start[2] if len(start) > 2 else ""
    if head_only:
        # A response to HEAD carries no body, whatever its Content-Length says.
        return status, reason, headers, b""
    chunked = any(
        n.lower() == "transfer-encoding" and "chunked" in v.lower() for n, v in headers
    )
    length = _content_length(headers)
    body = rest
    if chunked:
        body = await _read_chunked(reader, rest)
    elif length is not None:
        while len(body) < length:
            chunk = await reader.read(length - len(body))
            if not chunk:
                break
            body += chunk
        body = body[:length]
    else:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            body += chunk
    return status, reason, headers, body


async def _read_chunked(reader: asyncio.StreamReader, rest: bytes) -> bytes:
    buffer = rest
    body = b""
    while True:
        while b"\r\n" not in buffer:
            chunk = await reader.read(4096)
            if not chunk:
                return body
            buffer += chunk
        line, _, buffer = buffer.partition(b"\r\n")
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return body
        if size == 0:
            return body
        while len(buffer) < size + 2:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buffer += chunk
        body += buffer[:size]
        buffer = buffer[size + 2 :]


class EgressGateway:
    """Default-deny egress proxy serving one unix socket per sandbox identity."""

    def __init__(
        self,
        policy_source: PolicySource,
        recorder: EgressRecorder,
        fetch: ResearchFetch | None,
        resolver: Resolver,
        connector: Connector,
        model_proxy_upstream: tuple[str, int] | None,
        clock: Callable[[], datetime],
        tls: GatewayCA | None = None,
        upstream_ca_bundle: str | None = None,
    ) -> None:
        self._policy_source = policy_source
        self._recorder = recorder
        self._fetch = fetch
        self._resolver = resolver
        self._connector = connector
        self._model_proxy_upstream = model_proxy_upstream
        self._clock = clock
        self._tls = tls
        self._upstream_ca_bundle = upstream_ca_bundle
        self._timeout = _DEFAULT_TIMEOUT
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[Identity, tuple[asyncio.AbstractServer, Path]] = {}
        self._lock = threading.Lock()

    # -- sandboxes ---------------------------------------------------------

    def open_sandbox(self, identity: Identity, directory: str | os.PathLike[str]) -> Path:
        """Bind ``<directory>/proxy.sock`` (0600) to ``identity`` and start serving.

        Returns the socket path. A second call for the same identity returns the
        same path. The identity is the whole trust anchor; it is never read from
        a request.
        """
        with self._lock:
            loop = self._ensure_loop()
            existing = self._servers.get(identity)
            if existing is not None:
                return existing[1]
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            socket_path = root / "proxy.sock"
            if socket_path.exists():
                socket_path.unlink()
            server = asyncio.run_coroutine_threadsafe(
                self._start_server(identity, socket_path), loop
            ).result(timeout=5.0)
            self._servers[identity] = (server, socket_path)
            return socket_path

    def close_sandbox(self, identity: Identity) -> None:
        """Stop serving ``identity``'s socket and remove it."""
        with self._lock:
            entry = self._servers.pop(identity, None)
        if entry is None:
            return
        server, socket_path = entry
        loop = self._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(_stop_server(server), loop).result(timeout=5.0)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run, name="egress-gateway", daemon=True)
        thread.start()
        self._loop = loop
        self._thread = thread
        return loop

    async def _start_server(
        self, identity: Identity, socket_path: Path
    ) -> asyncio.AbstractServer:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, bound: Identity = identity
        ) -> None:
            await self._handle(bound, reader, writer)

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        os.chmod(socket_path, 0o600)
        return server

    # -- request handling --------------------------------------------------

    async def _handle(
        self,
        identity: Identity,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await _read_request(reader)
            if request is not None:
                now = self._clock()
                policy = self._policy_source(identity, now)
                if request.method == "CONNECT":
                    await self._handle_connect(identity, policy, request, now, reader, writer)
                else:
                    await self._handle_plain(identity, policy, request, now, writer)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _handle_plain(
        self,
        identity: Identity,
        policy: EgressPolicy,
        request: ProxyRequest,
        now: datetime,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed = urlsplit(request.target)
        scheme = parsed.scheme.lower()
        try:
            host = _norm_host(parsed.hostname or "")
            raw_port = parsed.port
        except ValueError:
            host, raw_port = "", None
        if scheme not in _PLAIN_SCHEMES or not host:
            self._refuse(identity, request, now, scheme, "", raw_port, "invalid", writer)
            return
        await self._exchange(
            identity,
            policy,
            request,
            now,
            writer,
            scheme=scheme,
            host=host,
            raw_port=raw_port,
            path=parsed.path or "/",
            query=parsed.query,
            tunnel=False,
            upstream_tls=False,
            target_url=request.target,
        )

    async def _exchange(
        self,
        identity: Identity,
        policy: EgressPolicy,
        request: ProxyRequest,
        now: datetime,
        writer: asyncio.StreamWriter,
        *,
        scheme: str,
        host: str,
        raw_port: int | None,
        path: str,
        query: str,
        tunnel: bool,
        upstream_tls: bool,
        target_url: str,
    ) -> None:
        """Decide and run one request against ``host:port``.

        The plain-http path and the inspected-tunnel path share this body, so
        both obey the same one-request, ``Connection: close`` rule. An
        inspected tunnel passes its authority in and ``scheme="https"`` with
        ``upstream_tls=True``; the inner request's own host is never used.
        """
        probe = Request(
            scheme=scheme,
            host=host,
            port=raw_port,
            method=request.method,
            path=path,
            query=query,
            headers=request.headers,
            has_body=bool(request.body),
            tunnel=tunnel,
        )
        verdict, effective = _plain_decision(policy, identity, probe, now)

        # A research identity is handed to the s03 fetch service for both its
        # allow and its refusal, so the service owns the research verdict and
        # records it (channel fetch) with the request url.
        if verdict.klass == "research":
            await self._serve_research(identity, request, target_url, now, writer)
            return

        if not verdict.allowed:
            self._refuse(
                identity, request, now, effective.scheme, effective.host,
                effective.port, verdict.code or "destination_not_allowed", writer,
                url=target_url,
            )
            return

        if verdict.klass == "model":
            upstream = self._model_proxy_upstream
            if upstream is None:
                self._refuse(
                    identity, request, now, effective.scheme, effective.host,
                    effective.port, "destination_not_allowed", writer, url=target_url,
                )
                return
            dial_host, dial_port = upstream
        else:
            dial_host, dial_port = effective.host, _request_port(effective)

        try:
            up_reader, up_writer, ip = await self._dial(dial_host, dial_port, tls=upstream_tls)
        except ssl.SSLError:
            self._refuse(
                identity, request, now, effective.scheme, effective.host,
                effective.port, "tls_error", writer, url=target_url,
            )
            return
        except (OSError, EgressRefused):
            self._refuse(
                identity, request, now, effective.scheme, effective.host,
                effective.port, "upstream_failed", writer, url=target_url,
            )
            return

        try:
            self._forward_request(up_writer, request, effective)
            await up_writer.drain()
            parsed_response = await _read_upstream_response(
                up_reader, head_only=request.method == "HEAD"
            )
        except (ConnectionError, OSError):
            parsed_response = None
        finally:
            _close_writer(up_writer)

        if parsed_response is None:
            self._refuse(
                identity, request, now, effective.scheme, effective.host,
                effective.port, "upstream_failed", writer, url=target_url,
            )
            return

        status, reason, headers, body = parsed_response
        declared = _content_length(headers)
        url = _absolute_url(effective.scheme, effective.host, effective.port, effective.path, effective.query)
        self._record(
            identity, now, channel="http", protocol=effective.scheme, host=effective.host,
            ip=ip, port=_request_port(effective), method=request.method, url=url,
            klass=verdict.klass, outcome="allowed", code=None, policy_id=verdict.policy_id,
        )
        writer.write(
            _response_bytes(
                status, headers, body, reason or _REASONS.get(status, ""), content_length=declared
            )
        )
        await _drain(writer)

    async def _serve_research(
        self,
        identity: Identity,
        request: ProxyRequest,
        url: str,
        now: datetime,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Delegate to the s03 fetch service, which records the call itself."""
        if self._fetch is None:
            await self._handle_plain_unavailable(identity, request, now, writer, url)
            return
        try:
            result = self._fetch.fetch(identity, request.method, url, request.headers, request.body)
        except EgressRefused as exc:
            writer.write(_refusal_bytes(exc.code))
            await _drain(writer)
            return
        writer.write(_response_bytes(result.status, list(result.headers), result.body))
        await _drain(writer)

    async def _handle_plain_unavailable(
        self,
        identity: Identity,
        request: ProxyRequest,
        now: datetime,
        writer: asyncio.StreamWriter,
        url: str | None = None,
    ) -> None:
        parsed = urlsplit(request.target)
        self._refuse(
            identity, request, now, parsed.scheme.lower(),
            _norm_host(parsed.hostname or ""), parsed.port, "destination_not_allowed",
            writer, url=url,
        )

    async def _handle_connect(
        self,
        identity: Identity,
        policy: EgressPolicy,
        request: ProxyRequest,
        now: datetime,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed = _parse_host_port(request.target)
        if parsed is None:
            self._record_tunnel(identity, now, request.target, 0, None, "none", "refused", "invalid", None)
            writer.write(_refusal_bytes("invalid"))
            await _drain(writer)
            return
        host, port = parsed
        klass = _tunnel_class(policy, identity, host, port, now)

        if klass == "model":
            upstream = self._model_proxy_upstream
            if upstream is None:
                self._record_tunnel(identity, now, host, port, None, "model", "refused", "destination_not_allowed", None)
                writer.write(_refusal_bytes("destination_not_allowed"))
                await _drain(writer)
                return
            await self._open_tunnel(identity, now, host, port, upstream, "model", None, reader, writer)
            return

        if klass == "standing":
            await self._open_tunnel(identity, now, host, port, (host, port), "standing", None, reader, writer)
            return

        if klass in _INSPECT_CLASSES:
            ca = self._tls
            if ca is not None:
                await self._inspect_tunnel(ca, identity, policy, host, port, now, reader, writer)
                return
            self._record_tunnel(identity, now, host, port, None, klass, "refused", "tls_inspection_required", None)
            writer.write(_refusal_bytes("tls_inspection_required"))
            await _drain(writer)
            return

        verdict = decide(policy, identity, _tunnel_request(host, port, request.headers), now)
        code = verdict.code or "destination_not_allowed"
        self._record_tunnel(identity, now, host, port, None, verdict.klass, "refused", code, verdict.policy_id)
        writer.write(_refusal_bytes(code))
        await _drain(writer)

    async def _inspect_tunnel(
        self,
        ca: GatewayCA,
        identity: Identity,
        policy: EgressPolicy,
        host: str,
        port: int,
        now: datetime,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Terminate client TLS with a CA-signed leaf, then proxy one inner request.

        The client trusts ``ca.ca_cert_path``, so it verifies the gateway's
        leaf and the gateway reads the inner request. The tunnel authority
        decides: the inner request's own host is ignored. No record is written
        here; the shared path records the inner request the way a plain https
        request is recorded, and the upstream leg is TLS verified against the
        host.
        """
        context = ca.leaf_context(host)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await _drain(writer)
        try:
            await writer.start_tls(context)
        except (ssl.SSLError, ConnectionError, OSError):
            return
        inner = await _read_request(reader)
        if inner is None:
            return
        parts = urlsplit(inner.target)
        path = parts.path or "/"
        query = parts.query
        target_url = f"{_INSPECT_SCHEME}://{host}:{port}{path}"
        if query:
            target_url = f"{target_url}?{query}"
        await self._exchange(
            identity,
            policy,
            inner,
            now,
            writer,
            scheme=_INSPECT_SCHEME,
            host=host,
            raw_port=port,
            path=path,
            query=query,
            tunnel=False,
            upstream_tls=True,
            target_url=target_url,
        )

    async def _open_tunnel(
        self,
        identity: Identity,
        now: datetime,
        host: str,
        port: int,
        upstream: tuple[str, int],
        klass: str,
        policy_id: str | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            up_reader, up_writer, ip = await self._dial(*upstream)
        except (OSError, EgressRefused):
            self._record_tunnel(identity, now, host, port, None, klass, "refused", "upstream_failed", policy_id)
            writer.write(_refusal_bytes("upstream_failed"))
            await _drain(writer)
            return

        self._record_tunnel(identity, now, host, port, ip, klass, "allowed", None, policy_id)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await _drain(writer)
        await _pump(reader, writer, up_reader, up_writer)

    # -- plumbing ----------------------------------------------------------

    async def _dial(
        self, host: str, port: int, tls: bool = False
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        addresses = [str(item) for item in self._resolver(host, port)]
        if not addresses:
            raise OSError(f"no address for {host}:{port}")
        ip = addresses[0]
        sock = self._connector(ip, port, self._timeout)
        sock.setblocking(False)
        try:
            if tls:
                context = ssl.create_default_context()
                if self._upstream_ca_bundle is not None:
                    context.load_verify_locations(cafile=self._upstream_ca_bundle)
                reader, writer = await asyncio.open_connection(
                    sock=sock, ssl=context, server_hostname=host
                )
            else:
                reader, writer = await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise
        return reader, writer, ip

    def _forward_request(
        self, writer: asyncio.StreamWriter, request: ProxyRequest, effective: Request
    ) -> None:
        target = effective.path or "/"
        if effective.query:
            target = f"{target}?{effective.query}"
        lines = [f"{request.method} {target} {request.version}"]
        for name, value in request.headers:
            if name.lower() in _DROP_REQUEST_HEADERS:
                continue
            lines.append(f"{name}: {value}")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
        writer.write(head + request.body)

    def _refuse(
        self,
        identity: Identity,
        request: ProxyRequest,
        now: datetime,
        scheme: str,
        host: str,
        port: int | None,
        code: str,
        writer: asyncio.StreamWriter,
        url: str | None = None,
    ) -> None:
        self._record(
            identity, now, channel="http", protocol=scheme, host=host, ip=None,
            port=port if port is not None else _DEFAULT_PORTS.get(scheme, 0),
            method=request.method, url=url if url is not None else request.target,
            klass="none", outcome="refused", code=code, policy_id=None,
        )
        writer.write(_refusal_bytes(code))

    def _record(self, identity: Identity, now: datetime, **fields: object) -> None:
        self._recorder.record(
            EgressRecord(
                workspace_id=identity.workspace_id,
                project_id=identity.project_id,
                mission_id=identity.mission_id,
                worker_id=identity.worker_id,
                stage=identity.stage,
                channel=str(fields["channel"]),
                protocol=str(fields["protocol"]),
                host=str(fields["host"]),
                ip=fields["ip"],  # type: ignore[arg-type]
                port=int(fields["port"]),  # type: ignore[arg-type]
                method=str(fields["method"]),
                url=str(fields["url"]),
                klass=str(fields["klass"]),
                outcome=str(fields["outcome"]),  # type: ignore[arg-type]
                code=fields["code"],  # type: ignore[arg-type]
                policy_id=fields["policy_id"],  # type: ignore[arg-type]
                at=now,
            )
        )

    def _record_tunnel(
        self,
        identity: Identity,
        now: datetime,
        host: str,
        port: int,
        ip: str | None,
        klass: str,
        outcome: str,
        code: str | None,
        policy_id: str | None,
    ) -> None:
        self._record(
            identity, now, channel="connect", protocol="https", host=host, ip=ip, port=port,
            method="CONNECT", url=f"https://{host}:{port}", klass=klass, outcome=outcome,
            code=code, policy_id=policy_id,
        )


def _plain_decision(
    policy: EgressPolicy, identity: Identity, request: Request, now: datetime
) -> tuple[Verdict, Request]:
    """Decide a plain request, re-judging an http request as its https origin.

    A registry is stored as an https origin and never matches an http request.
    When the plain request is only the catch-all ``none`` refusal, the same
    request is judged as https so an origin the policy names as https is still
    reachable by an absolute-form http request. Any other verdict is returned
    unchanged, so a genuinely unmatched destination still refuses.
    """
    verdict = decide(policy, identity, request, now)
    if verdict.allowed or verdict.klass != "none" or request.scheme.lower() != "http":
        return verdict, request
    promoted = replace(request, scheme="https")
    other = decide(policy, identity, promoted, now)
    if other.allowed or other.klass != "none":
        return other, promoted
    return verdict, request


def _tunnel_class(
    policy: EgressPolicy, identity: Identity, host: str, port: int, now: datetime
) -> str | None:
    """Classify a CONNECT destination the way :func:`decide` orders classes."""
    if (host, port) == policy.model_proxy:
        return "model"
    if identity.stage == "research":
        return "research"
    target = ("https", host, port)
    for entry in policy.standing:
        if _is_expired(entry, now):
            continue
        for raw in entry.destinations:
            if _parse_destination(raw) == target:
                return "standing"
    if (host, port) in policy.registries:
        return "registry"
    for scheme, remote_host, remote_port, _repo in policy.remotes:
        if scheme == "https" and remote_host == host and remote_port == port:
            return "remote"
    return None


def _absolute_url(scheme: str, host: str, port: int | None, path: str, query: str) -> str:
    authority = host if port is None else f"{host}:{port}"
    url = f"{scheme}://{authority}{path}"
    if query:
        url = f"{url}?{query}"
    return url


async def _pump(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            _close_writer(writer)

    await asyncio.gather(
        forward(client_reader, up_writer),
        forward(up_reader, client_writer),
    )


async def _stop_server(server: asyncio.AbstractServer) -> None:
    server.close()
    await server.wait_closed()


def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except (ConnectionError, OSError):
        pass


async def _drain(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
