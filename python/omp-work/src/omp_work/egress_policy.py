"""Egress address policy: refuse non-public, tunneled, and metadata targets (OMP-431).

Pure stdlib. An IPv6 transition address is unwrapped to the embedded IPv4 and
that address is judged, apart from the RFC 6052 NAT64 ``64:ff9b:1::/48`` prefix
whose u-octet must be zero.

Reason codes: ``invalid`` (unparseable input), ``metadata`` (cloud instance
metadata endpoint), ``nat64_u_octet`` (RFC 6052 NAT64 address with a non-zero
u-octet), and ``not_global`` (not globally routable, or multicast).

:func:`build_policy` compiles the owner-signed :class:`ProjectEgress` record,
the active ``network_access`` standing policies and the model proxy into an
:class:`EgressPolicy`: registries and remotes are normalized to origins only,
and malformed or non-https/http entries are dropped.

:func:`egress_change_kind` classifies changes between two egress records as
``create``, ``narrow``, or ``widen``.

:func:`decide` evaluates a :class:`Request` against an :class:`EgressPolicy`
and an :class:`Identity`, returning a :class:`Verdict`: the model proxy first,
then the research stage, then standing destinations, then project registries,
otherwise ``destination_not_allowed``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

from omp_work.standing_change import ChangeKind
from omp_work.standing_policy import StandingPolicy

__all__ = [
    "EgressPolicy",
    "EgressRecord",
    "EgressRecorder",
    "Identity",
    "MemoryRecorder",
    "ProjectEgress",
    "Request",
    "Stage",
    "Verdict",
    "blocked_address",
    "build_policy",
    "decide",
    "egress_change_kind",
    "research_refusal",
]

_METADATA: frozenset[IPv4Address | IPv6Address] = frozenset(
    {
        ip_address("169.254.169.254"),
        ip_address("168.63.129.16"),
        ip_address("100.100.100.200"),
        ip_address("fd00:ec2::254"),
    }
)

# IPv4-compatible ::/96 (the unspecified :: and loopback ::1 are excluded).
_COMPATIBLE = IPv6Network("::/96")
# 6to4 2002::/16: embedded IPv4 in bits 16-47.
_SIX_TO_FOUR = IPv6Network("2002::/16")
# NAT64 well-known prefix 64:ff9b::/96 and local-use prefix 64:ff9b:1::/48.
_NAT64_WKP = IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = IPv6Network("64:ff9b:1::/48")


def _parse(ip: str | IPv4Address | IPv6Address) -> IPv4Address | IPv6Address | None:
    if isinstance(ip, (IPv4Address, IPv6Address)):
        return ip
    try:
        return ip_address(ip)
    except ValueError:
        return None


def _embedded_ipv4(ip: IPv6Address) -> IPv4Address | None:
    """Return the IPv4 address embedded in an IPv6 transition format, else None."""
    n = int(ip)

    mapped = ip.ipv4_mapped
    if mapped is not None:
        return mapped

    if ip in _COMPATIBLE and n > 1:
        return IPv4Address(n & 0xFFFFFFFF)

    if ip in _SIX_TO_FOUR:
        return IPv4Address((n >> 80) & 0xFFFFFFFF)

    teredo = ip.teredo
    if teredo is not None:
        # Teredo 2001::/32: judge the client, not the server address.
        return teredo[1]

    if ip in _NAT64_WKP:
        return IPv4Address(n & 0xFFFFFFFF)

    if ip in _NAT64_LOCAL:
        # RFC 6052 section 2.2: the IPv4 bytes are not contiguous — bits 48-63
        # carry its high half, bits 72-87 its low half.
        return IPv4Address((((n >> 64) & 0xFFFF) << 16) | ((n >> 40) & 0xFFFF))

    return None


def blocked_address(ip: str | IPv4Address | IPv6Address) -> str | None:
    """Return a refusal reason for an egress target address, or None if allowed."""
    address = _parse(ip)
    if address is None:
        return "invalid"

    embedded = _embedded_ipv4(address) if isinstance(address, IPv6Address) else None
    if embedded is not None:
        if isinstance(address, IPv6Address) and address in _NAT64_LOCAL:
            if (int(address) >> 56) & 0xFF:
                return "nat64_u_octet"
        address = embedded

    if address in _METADATA:
        return "metadata"

    if address.is_global and not address.is_multicast:
        return None
    return "not_global"


_HANDLED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_DEFAULT_PORTS: dict[str, int] = {"https": 443, "http": 80}


def _norm_host(host: str) -> str:
    """Normalize a host: lower-case, drop a trailing dot, strip brackets, canonical IP."""
    text = host.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ip_address(text))
    except ValueError:
        return text


def _parse_host_port(text: str) -> tuple[str, int] | None:
    """Parse ``host:port`` or ``[v6]:port``, requiring an explicit port."""
    candidate = text.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    if candidate.startswith("["):
        end = candidate.find("]")
        if end == -1:
            return None
        host = _norm_host(candidate[1:end])
        rest = candidate[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return (host, int(rest[1:]))
        return None
    if ":" in candidate:
        head, _, tail = candidate.rpartition(":")
        if not tail.isdigit() or ":" in head or not head:
            return None
        return (_norm_host(head), int(tail))
    return None


def _parse_registry(value: object) -> tuple[str, int] | None:
    """Return ``(https host, port)`` for an ``https://`` registry, else ``None``.

    Only ``https://host[:port]`` with an optional trailing ``/`` is kept; any
    other scheme, a path, a query, a fragment, or userinfo grants nothing.
    """
    if not isinstance(value, str):
        return None
    parts = urlsplit(value.strip())
    if parts.scheme.lower() != "https":
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return (_norm_host(host), port or 443)


def _parse_remote(value: object) -> tuple[str, str, int, str] | None:
    """Return ``(scheme, host, port, repo path)`` for an ``http(s)`` remote, else ``None``.

    Userinfo, a query, a fragment, an empty repo path, a trailing ``/``, a
    trailing ``.git``, and a percent-decoded path segment that is empty, ``.``
    or ``..`` are all dropped. Any other scheme (``ssh``, the scp-like
    ``git@host:path``) grants nothing.
    """
    if not isinstance(value, str):
        return None
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    if scheme not in _HANDLED_SCHEMES:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return None
    for segment in path.split("/")[1:]:
        decoded = unquote(segment)
        if decoded in ("", ".", ".."):
            return None
    return (scheme, _norm_host(host), port or _DEFAULT_PORTS[scheme], path)


def _parse_destination(value: object) -> tuple[str, str, int] | None:
    """Return ``(scheme, host, port)`` for a standing destination, else ``None``.

    Accepts ``https://h[:p]``, ``http://h[:p]``, ``h:p`` (https), ``h``
    (https, port 443), and a bracketed IPv6 literal ``[v6]`` or ``[v6]:port``
    (https). A path other than ``/``, a query, a fragment, or userinfo on the
    destination grants nothing, as does another scheme. The request's own path
    and query are not part of this parse.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "//" in text:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in _HANDLED_SCHEMES:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if parts.query or parts.fragment or parts.path not in ("", "/"):
            return None
        try:
            host = parts.hostname
            port = parts.port
        except ValueError:
            return None
        if not host:
            return None
        candidate = host
        explicit_port = port
    else:
        scheme = "https"
        if text.startswith("["):
            end = text.find("]")
            if end == -1:
                return None
            candidate = text[1:end]
            rest = text[end + 1 :]
            if not rest:
                explicit_port = None
            elif rest.startswith(":") and rest[1:].isdigit():
                explicit_port = int(rest[1:])
            else:
                return None
        elif ":" in text:
            head, _, tail = text.rpartition(":")
            if not tail.isdigit() or ":" in head or not head:
                return None
            candidate = head
            explicit_port = int(tail)
        else:
            candidate = text
            explicit_port = None
    host = _norm_host(candidate)
    if not host:
        return None
    return (scheme, host, explicit_port if explicit_port is not None else 443)


def _is_expired(policy: StandingPolicy, now: datetime) -> bool:
    expires = policy.expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= now


def _parse_egress_targets(
    egress: ProjectEgress,
) -> tuple[set[tuple[str, int]], set[tuple[str, str, int, str]]]:
    registries: set[tuple[str, int]] = set()
    remotes: set[tuple[str, str, int, str]] = set()
    for raw in egress.registries:
        parsed = _parse_registry(raw)
        if parsed is not None:
            registries.add(parsed)
    for raw in egress.remotes:
        parsed = _parse_remote(raw)
        if parsed is not None:
            remotes.add(parsed)
    return registries, remotes


def build_policy(
    egress: ProjectEgress | None,
    standing: Iterable[StandingPolicy],
    model_proxy: str,
) -> EgressPolicy:
    """Compile an egress policy. The only constructor for :class:`EgressPolicy`.

    ``model_proxy`` is required and must carry a port. Registries are kept only
    as an https origin; remotes only as an http(s) URL with a non-empty repo
    path; standing policies only when they are ``network_access`` and not yet
    expired. No worktree input.
    """
    parsed_proxy = _parse_host_port(model_proxy)
    if parsed_proxy is None:
        raise ValueError(f"model_proxy requires host:port, got {model_proxy!r}")
    proxy_host, proxy_port = parsed_proxy

    if egress is not None:
        registries, remotes = _parse_egress_targets(egress)
    else:
        registries, remotes = set(), set()

    now = datetime.now(UTC)
    kept: list[StandingPolicy] = []
    for policy in standing:
        if policy.action_class != "network_access":
            continue
        if _is_expired(policy, now):
            continue
        kept.append(policy)

    return EgressPolicy(
        model_proxy=(proxy_host, proxy_port),
        registries=frozenset(registries),
        remotes=frozenset(remotes),
        standing=tuple(kept),
        decision_id=egress.decision_id if egress is not None else None,
    )


Stage = Literal["repository", "research"]


@dataclass(frozen=True)
class ProjectEgress:
    """Owner-signed project egress record: registries and repository remotes."""

    registries: tuple[str, ...]
    remotes: tuple[str, ...]
    decision_id: str | None


def egress_change_kind(
    old: ProjectEgress | None,
    new: ProjectEgress,
) -> ChangeKind:
    """Classify the change between an old and a new project egress record.

    Returns ``ChangeKind.create`` if ``old`` is ``None``. Otherwise, parses both
    records' registries and remotes using the same normalization rules as
    :func:`build_policy`. If ``new``'s parsed registry and remote sets are both
    subsets of ``old``'s (including equal), returns ``ChangeKind.narrow``.
    Otherwise, returns ``ChangeKind.widen``.
    """
    if old is None:
        return ChangeKind.create

    old_registries, old_remotes = _parse_egress_targets(old)
    new_registries, new_remotes = _parse_egress_targets(new)

    if new_registries <= old_registries and new_remotes <= old_remotes:
        return ChangeKind.narrow
    return ChangeKind.widen


@dataclass(frozen=True)
class EgressPolicy:
    """Compiled egress policy. Construct with :func:`build_policy`."""

    model_proxy: tuple[str, int]
    registries: frozenset[tuple[str, int]]
    remotes: frozenset[tuple[str, str, int, str]]
    standing: tuple[StandingPolicy, ...]
    decision_id: str | None


@dataclass(frozen=True)
class Identity:
    """Execution identity for an egress request."""

    workspace_id: str
    project_id: str
    mission_id: str | None
    worker_id: str
    stage: Stage


@dataclass(frozen=True)
class Request:
    """A normalized egress request to be decided against a policy."""

    scheme: str
    host: str
    port: int | None
    method: str
    path: str
    query: str
    headers: Mapping[str, str] | Iterable[tuple[str, str]]
    has_body: bool
    tunnel: bool


@dataclass(frozen=True)
class Verdict:
    """The outcome of an egress decision."""

    allowed: bool
    klass: Literal["model", "registry", "remote", "standing", "research", "none"]
    code: str | None
    policy_id: str | None


_AUTH_HEADERS: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "cookie"}
)
_QUERY_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {"access_token", "api_key", "apikey"}
)


def research_refusal(
    method: str,
    url: str,
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    has_body: bool,
) -> str | None:
    """Return a research refusal code if the request is not allowed, else None."""
    if method.upper() not in {"GET", "HEAD"}:
        return "method_not_allowed"

    if has_body:
        return "request_body"

    if isinstance(headers, Mapping):
        for name in headers:
            if str(name).lower() in _AUTH_HEADERS:
                return "auth_header"
    else:
        for item in headers:
            if item and str(item[0]).lower() in _AUTH_HEADERS:
                return "auth_header"

    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return "url_credentials"

    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _QUERY_CREDENTIAL_NAMES:
            return "query_credential"

    return None


Outcome = Literal["allowed", "refused"]


@dataclass(frozen=True)
class EgressRecord:
    """Structured record of an egress verdict and execution context."""

    workspace_id: str
    project_id: str
    mission_id: str | None
    worker_id: str
    stage: Literal["repository", "research"]
    channel: str
    protocol: str
    host: str
    ip: str | None
    port: int
    method: str
    url: str
    klass: str
    outcome: Outcome
    code: str | None
    policy_id: str | None
    at: datetime


@runtime_checkable
class EgressRecorder(Protocol):
    """Protocol for egress audit recorders."""

    def record(self, rec: EgressRecord) -> None:
        ...


class MemoryRecorder(EgressRecorder):
    """In-memory egress recorder that retains records in call order."""

    records: list[EgressRecord]

    def __init__(self, records: list[EgressRecord] | None = None) -> None:
        self.records = [] if records is None else records

    def record(self, rec: EgressRecord) -> None:
        self.records.append(rec)


def _request_port(req: Request) -> int:
    if req.port is not None:
        return req.port
    return _DEFAULT_PORTS.get(req.scheme.lower(), 443)


def _standing_verdict(policy: EgressPolicy, req: Request, now: datetime) -> Verdict | None:
    """Allow when a standing destination grants this exact scheme, host, and port.

    A non-root path, a query, userinfo, or another scheme rejects the
    destination string. Those constraints do not apply to the request.
    """
    host = _norm_host(req.host)
    scheme = req.scheme.lower()
    target = (scheme, host, _request_port(req))
    for entry in policy.standing:
        if _is_expired(entry, now):
            continue
        for raw in entry.destinations:
            if _parse_destination(raw) == target:
                return Verdict(True, "standing", None, str(entry.policy_id))
    return None


def decide(
    policy: EgressPolicy,
    identity: Identity,
    req: Request,
    now: datetime,
) -> Verdict:
    """Decide an egress request against a compiled policy.

    Ordered rules: the model proxy, then the research stage, then standing
    destinations, then project registries, otherwise a refusal. A refusal
    raised after a match keeps that match's class.
    """
    scheme = req.scheme.lower()
    host = _norm_host(req.host)
    port = _request_port(req)
    method = "CONNECT" if req.tunnel else req.method

    if (host, port) == policy.model_proxy:
        return Verdict(True, "model", None, None)

    if identity.stage == "research":
        refusal = research_refusal(method, _request_url(req, scheme), req.headers, req.has_body)
        if refusal is not None:
            return Verdict(False, "research", refusal, None)
        return Verdict(True, "research", None, None)

    standing = _standing_verdict(policy, req, now)
    if standing is not None:
        return standing

    if scheme == "https" and (host, port) in policy.registries:
        if req.tunnel or method.upper() not in {"GET", "HEAD"}:
            return Verdict(False, "registry", "method_not_allowed", policy.decision_id)
        if req.has_body:
            return Verdict(False, "registry", "request_body", policy.decision_id)
        return Verdict(True, "registry", None, policy.decision_id)

    return Verdict(False, "none", "destination_not_allowed", None)


def _request_url(req: Request, scheme: str) -> str:
    netloc = req.host
    if req.port is not None:
        netloc = f"{netloc}:{req.port}"
    url = f"{scheme}://{netloc}{req.path}"
    if req.query:
        url = f"{url}?{req.query}"
    return url

