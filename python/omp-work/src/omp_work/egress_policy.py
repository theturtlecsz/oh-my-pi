"""Egress address policy: refuse non-public, tunneled, and metadata targets (OMP-431).

Pure stdlib. An IPv6 transition address is unwrapped to the embedded IPv4 and
that address is judged, apart from the RFC 6052 NAT64 ``64:ff9b:1::/48`` prefix
whose u-octet must be zero.

Reason codes: ``invalid`` (unparseable input), ``metadata`` (cloud instance
metadata endpoint), ``nat64_u_octet`` (RFC 6052 NAT64 address with a non-zero
u-octet), and ``not_global`` (not globally routable, or multicast).
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address

__all__ = [
    "blocked_address",
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
