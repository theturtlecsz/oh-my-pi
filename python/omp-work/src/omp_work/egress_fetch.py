"""Pinned HTTP fetch and push for egress (OMP-431).

:func:`open_pinned` resolves ``host`` when it is called and keeps no cache.
A blocked address (:func:`omp_work.egress_policy.blocked_address`) raises
:class:`EgressRefused` and the connector is not called. Otherwise the
connector is called with the first resolved address. With ``tls`` set, that
socket is wrapped with certificate verification and SNI equal to ``host``.

:class:`ResearchFetchService` vets the lookup, then calls :func:`open_pinned`,
which looks the name up again. A name that is public on the first lookup and
blocked on the second is refused with nothing connected. A non-http(s) URL
raises :class:`EgressRefused` with ``scheme_not_allowed``. A
:func:`omp_work.egress_policy.research_refusal` hit raises
:class:`EgressRefused` with that code. Nothing is sent in either case. One
request is issued: redirects are not followed, no cookie jar is kept, and no
credentials are added. ``Set-Cookie`` and ``Set-Cookie2`` are dropped from the
response. Every call is recorded on channel ``fetch`` with the request url and
the identity's mission and worker.

:func:`check_push_destination` requires https, no userinfo, and no blocked IP
literal, and requires the origin to be granted by an active ``network_access``
standing destination under the egress-policy rule. Otherwise it raises
:class:`EgressRefused`. :func:`deliver_push` runs that check again, POSTs
through :func:`open_pinned`, and records channel ``push``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import http.client
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlsplit

from omp_work.egress_policy import (
    EgressRecord,
    EgressRecorder,
    Identity,
    _is_expired,
    _norm_host,
    _parse_destination,
    blocked_address,
    research_refusal,
)
from omp_work.standing_policy import StandingPolicy

__all__ = [
    "Connector",
    "EgressRefused",
    "FetchResult",
    "ResearchFetchService",
    "Resolver",
    "check_push_destination",
    "deliver_push",
    "open_pinned",
]

_DEFAULT_TIMEOUT = 30.0
_FETCH_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"https": 443, "http": 80}
_DROPPED_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})
_PUSH_IDENTITY = Identity(
    workspace_id="",
    project_id="",
    mission_id=None,
    worker_id="",
    stage="repository",
)


class Resolver(Protocol):
    """Resolve ``host`` for ``port`` at the moment of the call. No cache."""

    def __call__(self, host: str, port: int) -> Iterable[str]:
        ...


class Connector(Protocol):
    """Open a TCP connection to ``ip`` and ``port``. Do not resolve a name."""

    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket:
        ...


class EgressRefused(Exception):
    """An egress target was refused. Nothing was sent."""

    code: str
    ip: str | None

    def __init__(self, code: str, ip: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.ip = ip


@dataclass(frozen=True)
class FetchResult:
    """One HTTP response. Redirects are not followed."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass
class _Attempt:
    """Fields gathered while one fetch or push runs, then recorded once."""

    identity: Identity
    channel: str
    method: str
    url: str
    at: datetime
    scheme: str = ""
    host: str = ""
    port: int = 0
    ip: str | None = None
    klass: str = "none"
    policy_id: str | None = None
    code: str | None = None
    outcome: str = "refused"


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _literal_block(host: str) -> str | None:
    """Refusal code when ``host`` is a blocked IP literal, else None."""
    try:
        ip_address(host)
    except ValueError:
        return None
    return blocked_address(host)


def _parse_url(url: str) -> tuple[str, str, int]:
    """Return ``(scheme, normalized host, port)``. Host is empty when the URL has none."""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        raw_host = parts.hostname
        explicit = parts.port
    except ValueError as exc:
        raise EgressRefused("invalid") from exc
    host = _norm_host(raw_host) if raw_host else ""
    port = explicit if explicit is not None else _DEFAULT_PORTS.get(scheme, 0)
    return scheme, host, port


def _request_target(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if parts.query:
        return f"{path}?{parts.query}"
    return path


def _has_body(body: bytes | str | None) -> bool:
    if body is None:
        return False
    if isinstance(body, str):
        return body != ""
    return len(body) != 0


def _as_bytes(body: bytes | str | None) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, str):
        return body.encode("utf-8")
    return bytes(body)


def _header_items(
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return [(str(key), str(value)) for key, value in headers.items()]
    items: list[tuple[str, str]] = []
    for item in headers:
        if not item:
            continue
        items.append((str(item[0]), str(item[1])))
    return items


def _resolve_vetted(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve now. A blocked literal or any blocked answer raises.

    A blocked IP literal is refused before the resolver runs: the address is
    already known. A public IP literal must resolve only to itself.
    """
    literal = _literal_block(host)
    if literal is not None:
        raise EgressRefused(literal, ip=host)

    found = [str(item) for item in resolver(host, port)]
    if not found:
        raise EgressRefused("invalid")
    for ip in found:
        reason = blocked_address(ip)
        if reason is not None:
            raise EgressRefused(reason, ip=ip)

    try:
        canonical = str(ip_address(host))
    except ValueError:
        return found
    for ip in found:
        try:
            parsed = str(ip_address(ip))
        except ValueError as exc:
            raise EgressRefused("invalid", ip=ip) from exc
        if parsed != canonical:
            raise EgressRefused("invalid", ip=ip)
    return found


def _tls_wrap(sock: socket.socket, host: str, ca_bundle: str | None) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    if ca_bundle is not None:
        context.load_verify_locations(cafile=ca_bundle)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context.wrap_socket(sock, server_hostname=host)


def open_pinned(
    host: str,
    port: int,
    resolver: Resolver,
    connector: Connector,
    tls: bool,
    ca_bundle: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> socket.socket:
    """Resolve ``host`` now and connect to that IP, or refuse a blocked address.

    The lookup is not cached. When ``tls`` is true the connected socket is
    verified against ``host`` (SNI and the certificate name).
    """
    addresses = _resolve_vetted(host, port, resolver)
    ip = addresses[0]
    sock = connector(ip, port, timeout)
    try:
        sock.settimeout(timeout)
        if tls:
            wrapped = _tls_wrap(sock, host, ca_bundle)
        else:
            wrapped = sock
    except Exception:
        sock.close()
        raise
    return wrapped


def _exchange(
    sock: socket.socket,
    method: str,
    url: str,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    body: bytes | str | None,
    scheme: str,
) -> FetchResult:
    parts = urlsplit(url.strip())
    host = _norm_host(parts.hostname or "")
    port = parts.port if parts.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    conn = http.client.HTTPConnection(host, port)
    # HTTPConnection's default port is 80. Match the URL scheme so a default
    # https port is not written into the Host header.
    conn.default_port = _DEFAULT_PORTS.get(scheme, port)
    conn.sock = sock
    payload = _as_bytes(body) if _has_body(body) and method.upper() not in {"GET", "HEAD"} else None
    # Caller headers only. No Authorization, Cookie, or other credential is added.
    conn.request(method.upper(), _request_target(url), body=payload, headers=dict(_header_items(headers)))
    response = conn.getresponse()
    kept = tuple(
        (name, value)
        for name, value in response.getheaders()
        if name.lower() not in _DROPPED_RESPONSE_HEADERS
    )
    data = response.read()
    return FetchResult(status=response.status, headers=kept, body=data)


def _transmit(
    host: str,
    port: int,
    resolver: Resolver,
    connector: Connector,
    tls: bool,
    ca_bundle: str | None,
    timeout: float,
    method: str,
    url: str,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    body: bytes | str | None,
    scheme: str,
) -> tuple[FetchResult, str]:
    """Vet one lookup, then connect with a second lookup and exchange once."""
    _resolve_vetted(host, port, resolver)
    pinned: list[str] = []

    def _remember(ip: str, remembered_port: int, remembered_timeout: float) -> socket.socket:
        pinned.append(ip)
        return connector(ip, remembered_port, remembered_timeout)

    sock = open_pinned(host, port, resolver, _remember, tls, ca_bundle, timeout)
    try:
        result = _exchange(sock, method, url, headers, body, scheme)
        if not pinned:
            raise EgressRefused("invalid")
        return result, pinned[0]
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _emit(recorder: EgressRecorder, attempt: _Attempt) -> None:
    recorder.record(
        EgressRecord(
            workspace_id=attempt.identity.workspace_id,
            project_id=attempt.identity.project_id,
            mission_id=attempt.identity.mission_id,
            worker_id=attempt.identity.worker_id,
            stage=attempt.identity.stage,
            channel=attempt.channel,
            protocol=attempt.scheme,
            host=attempt.host,
            ip=attempt.ip,
            port=attempt.port,
            method=attempt.method,
            url=attempt.url,
            klass=attempt.klass,
            outcome=attempt.outcome,  # type: ignore[arg-type]
            code=attempt.code,
            policy_id=attempt.policy_id,
            at=attempt.at,
        )
    )


def _run(recorder: EgressRecorder, attempt: _Attempt, action) -> FetchResult:
    try:
        result, ip = action()
    except EgressRefused as exc:
        attempt.code = exc.code
        if exc.ip is not None:
            attempt.ip = exc.ip
        attempt.outcome = "refused"
        raise
    except ssl.SSLError:
        attempt.code = "tls_error"
        attempt.outcome = "refused"
        raise
    except Exception:
        attempt.code = "exchange_failed"
        attempt.outcome = "refused"
        raise
    else:
        attempt.ip = ip
        attempt.code = None
        attempt.outcome = "allowed"
        return result
    finally:
        _emit(recorder, attempt)


def _fill_endpoint(attempt: _Attempt, url: str) -> tuple[str, str, int]:
    scheme, host, port = _parse_url(url)
    attempt.scheme = scheme
    attempt.host = host
    attempt.port = port
    return scheme, host, port


class ResearchFetchService:
    """One-shot research HTTP. Records every call, including refusals."""

    def __init__(
        self,
        recorder: EgressRecorder,
        resolver: Resolver,
        connector: Connector,
        ca_bundle: str | None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._recorder = recorder
        self._resolver = resolver
        self._connector = connector
        self._ca_bundle = ca_bundle
        self._timeout = timeout

    def fetch(
        self,
        identity: Identity,
        method: str,
        url: str,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
        body: bytes | str | None,
    ) -> FetchResult:
        """Fetch ``url`` once, or raise :class:`EgressRefused` with nothing sent."""
        attempt = _Attempt(
            identity=identity,
            channel="fetch",
            method=method.upper(),
            url=url,
            at=datetime.now(UTC),
        )

        def action() -> tuple[FetchResult, str]:
            scheme, host, port = _fill_endpoint(attempt, url)
            if scheme not in _FETCH_SCHEMES:
                raise EgressRefused("scheme_not_allowed")
            if not host:
                raise EgressRefused("invalid")
            attempt.klass = "research"
            refusal = research_refusal(method, url, _header_items(headers), _has_body(body))
            if refusal is not None:
                raise EgressRefused(refusal)
            return _transmit(
                host,
                port,
                self._resolver,
                self._connector,
                scheme == "https",
                self._ca_bundle,
                self._timeout,
                method,
                url,
                headers,
                body,
                scheme,
            )

        return _run(self._recorder, attempt, action)


def _granted_policy(
    scheme: str,
    host: str,
    port: int,
    standing: Iterable[StandingPolicy],
    now: datetime,
) -> StandingPolicy | None:
    target = (scheme, host, port)
    moment = _aware(now)
    for policy in standing:
        if policy.action_class != "network_access":
            continue
        if _is_expired(policy, moment):
            continue
        for raw in policy.destinations:
            if _parse_destination(raw) == target:
                return policy
    return None


def check_push_destination(
    url: str,
    standing: Iterable[StandingPolicy],
    now: datetime,
) -> str:
    """Return the granting policy id, or raise :class:`EgressRefused`.

    The URL must be https, carry no userinfo, and not be a blocked IP literal.
    Its origin must match an unexpired ``network_access`` destination under
    the egress-policy destination rule (scheme, host, and port).
    """
    scheme, host, port = _parse_url(url)
    if scheme != "https":
        raise EgressRefused("scheme_not_allowed")
    if not host:
        raise EgressRefused("invalid")
    parts = urlsplit(url.strip())
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EgressRefused("url_credentials")
    literal = _literal_block(host)
    if literal is not None:
        raise EgressRefused(literal, ip=host)
    policy = _granted_policy(scheme, host, port, standing, now)
    if policy is None:
        raise EgressRefused("destination_not_allowed")
    return str(policy.policy_id)


def deliver_push(
    url: str,
    body: bytes | str | None,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    standing: Iterable[StandingPolicy],
    recorder: EgressRecorder,
    resolver: Resolver,
    connector: Connector,
    now: datetime,
    ca_bundle: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> FetchResult:
    """Re-check ``url``, POST once through :func:`open_pinned`, and record it."""
    moment = _aware(now)
    attempt = _Attempt(
        identity=_PUSH_IDENTITY,
        channel="push",
        method="POST",
        url=url,
        at=moment,
    )

    def action() -> tuple[FetchResult, str]:
        scheme, host, port = _fill_endpoint(attempt, url)
        attempt.policy_id = check_push_destination(url, standing, moment)
        attempt.klass = "standing"
        return _transmit(
            host,
            port,
            resolver,
            connector,
            True,
            ca_bundle,
            timeout,
            "POST",
            url,
            headers,
            b"" if body is None else body,
            scheme,
        )

    return _run(recorder, attempt, action)
