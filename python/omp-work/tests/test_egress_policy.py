"""Tests for the egress address policy (OMP-431)."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import pytest

from omp_work.egress_policy import blocked_address


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        # Metadata endpoints (on the unwrapped address).
        ("168.63.129.16", "metadata"),
        ("169.254.169.254", "metadata"),
        ("100.100.100.200", "metadata"),
        ("fd00:ec2::254", "metadata"),
        # NAT64 well-known prefix: the embedded IPv4 is judged.
        ("64:ff9b::7f00:1", "not_global"),
        ("64:ff9b::a9fe:a9fe", "metadata"),
        # IPv4-compatible ::/96.
        ("::127.0.0.1", "not_global"),
        # IPv4-mapped ::ffff:0:0/96.
        ("::ffff:10.0.0.1", "not_global"),
        # Multicast is refused even though ipaddress reports it global.
        ("224.0.0.1", "not_global"),
        # 6to4 2002::/16 embeds 127.0.0.1.
        ("2002:7f00:1::", "not_global"),
        # 64:ff9b:1::/48 metadata via the RFC 6052 layout (failed review before).
        ("64:ff9b:1:a83f:81:1000::", "metadata"),
        # 64:ff9b:1::/48 with a non-zero u-octet.
        ("64:ff9b:1:5db8:ffd8:2200::", "nat64_u_octet"),
        # Teredo: judge the client (127.0.0.1), not the server.
        ("2001:0:4136:e378:8000:63bf:80ff:fffe", "not_global"),
        # Unparseable input.
        ("not-an-ip", "invalid"),
    ],
)
def test_blocked_address_refused(address: str, reason: str) -> None:
    assert blocked_address(address) == reason


@pytest.mark.parametrize(
    "address",
    [
        "93.184.216.34",
        # 64:ff9b:1::/48 with a zero u-octet resolves to 93.184.216.34.
        "64:ff9b:1:5db8:d8:2200::",
        "2606:4700::1111",
    ],
)
def test_blocked_address_allowed(address: str) -> None:
    assert blocked_address(address) is None


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        (IPv4Address("168.63.129.16"), "metadata"),
        (IPv6Address("64:ff9b:1:5db8:ffd8:2200::"), "nat64_u_octet"),
    ],
)
def test_blocked_address_accepts_address_objects(
    address: IPv4Address | IPv6Address,
    reason: str,
) -> None:
    assert blocked_address(address) == reason
