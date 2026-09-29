"""Egress address policy: refuse non-public, tunneled, and metadata targets (OMP-431).

Pure stdlib. An IPv6 transition address is unwrapped to the embedded IPv4 and
that address is judged, apart from the RFC 6052 NAT64 ``64:ff9b:1::/48`` prefix
whose u-octet must be zero.

Reason codes: ``invalid`` (unparseable input), ``metadata`` (cloud instance
metadata endpoint), ``nat64_u_octet`` (RFC 6052 NAT64 address with a non-zero
u-octet), and ``not_global`` (not globally routable, or multicast).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

__all__ = [
    "EgressRecord",
    "EgressRecorder",
    "Identity",
    "MemoryRecorder",
    "Stage",
    "blocked_address",
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


Stage = Literal["repository", "research"]


@dataclass(frozen=True)
class Identity:
    """Execution identity for an egress request."""

    workspace_id: str
    project_id: str
    mission_id: str | None
    worker_id: str
    stage: Stage


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

