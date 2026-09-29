"""Tests for the egress address policy (OMP-431)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv6Address

import pytest

from omp_work.egress_policy import (
    EgressRecord,
    EgressRecorder,
    Identity,
    MemoryRecorder,
    blocked_address,
    research_refusal,
)


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


def test_identity_dataclass() -> None:
    identity = Identity(
        workspace_id="ws-1",
        project_id="proj-1",
        mission_id="m-1",
        worker_id="w-1",
        stage="research",
    )
    assert identity.workspace_id == "ws-1"
    assert identity.project_id == "proj-1"
    assert identity.mission_id == "m-1"
    assert identity.worker_id == "w-1"
    assert identity.stage == "research"

    identity_no_mission = Identity(
        workspace_id="ws-1",
        project_id="proj-1",
        mission_id=None,
        worker_id="w-1",
        stage="repository",
    )
    assert identity_no_mission.mission_id is None
    assert identity_no_mission.stage == "repository"

    with pytest.raises(FrozenInstanceError):
        identity.worker_id = "w-2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method", "url", "headers", "has_body", "expected"),
    [
        # Each code alone: PUT, POST, GET+body, authorization / COOKIE header,
        # https://u:p@h/, ?API_KEY=x, ?Access%5Ftoken=x.
        ("PUT", "https://example.com/resource", {}, False, "method_not_allowed"),
        ("POST", "https://example.com/resource", {}, False, "method_not_allowed"),
        ("DELETE", "https://example.com/resource", {}, False, "method_not_allowed"),
        ("GET", "https://example.com/resource", {}, True, "request_body"),
        ("HEAD", "https://example.com/resource", {}, True, "request_body"),
        ("GET", "https://example.com/resource", {"authorization": "Bearer token"}, False, "auth_header"),
        ("GET", "https://example.com/resource", {"Authorization": "Bearer token"}, False, "auth_header"),
        ("GET", "https://example.com/resource", {"COOKIE": "session=123"}, False, "auth_header"),
        ("GET", "https://example.com/resource", {"cookie": "session=123"}, False, "auth_header"),
        ("GET", "https://example.com/resource", {"Proxy-Authorization": "Basic xyz"}, False, "auth_header"),
        ("GET", "https://example.com/resource", [("Authorization", "token")], False, "auth_header"),
        ("GET", "https://u:p@h/", {}, False, "url_credentials"),
        ("GET", "https://u@h/", {}, False, "url_credentials"),
        ("GET", "https://:p@h/", {}, False, "url_credentials"),
        ("GET", "https://@h/", {}, False, "url_credentials"),
        ("GET", "https://example.com/?API_KEY=x", {}, False, "query_credential"),
        ("GET", "https://example.com/?Access%5Ftoken=x", {}, False, "query_credential"),
        ("GET", "https://example.com/?apikey=x", {}, False, "query_credential"),
        ("GET", "https://example.com/?api_key", {}, False, "query_credential"),
        ("GET", "https://example.com/?api_key=", {}, False, "query_credential"),
        ("GET", "https://example.com/?access_token=", {}, False, "query_credential"),
        ("GET", "https://example.com/?foo=1&access_token=bar", {}, False, "query_credential"),
    ],
)
def test_research_refusal_individual_codes(
    method: str,
    url: str,
    headers: dict[str, str] | list[tuple[str, str]],
    has_body: bool,
    expected: str,
) -> None:
    assert research_refusal(method, url, headers, has_body) == expected


@pytest.mark.parametrize(
    ("method", "url", "headers", "has_body", "expected"),
    [
        # Order precedence:
        # POST+body+Authorization -> method_not_allowed
        ("POST", "https://u:p@h/?api_key=x", {"Authorization": "Bearer x"}, True, "method_not_allowed"),
        # GET+body+Cookie -> request_body
        ("GET", "https://u:p@h/?api_key=x", {"Cookie": "s=1"}, True, "request_body"),
        # header+userinfo -> auth_header
        ("GET", "https://u:p@h/?api_key=x", {"Authorization": "Bearer x"}, False, "auth_header"),
        # userinfo+query -> url_credentials
        ("GET", "https://u:p@h/?api_key=x", {}, False, "url_credentials"),
        # Allowed clean GET -> None
        ("GET", "https://example.com/a?q=1", {}, False, None),
        # Allowed clean HEAD -> None
        ("HEAD", "https://example.com/a?q=1", {"User-Agent": "test"}, False, None),
        # Unrelated query param -> None
        ("GET", "https://example.com/a?token=123", {}, False, None),
    ],
)
def test_research_refusal_precedence(
    method: str,
    url: str,
    headers: dict[str, str],
    has_body: bool,
    expected: str | None,
) -> None:
    assert research_refusal(method, url, headers, has_body) == expected


def test_egress_record_dataclass() -> None:
    now = datetime.now(timezone.utc)
    rec = EgressRecord(
        workspace_id="ws-1",
        project_id="proj-1",
        mission_id="m-1",
        worker_id="w-1",
        stage="research",
        channel="http",
        protocol="https",
        host="example.com",
        ip="93.184.216.34",
        port=443,
        method="GET",
        url="https://example.com/data",
        klass="research",
        outcome="allowed",
        code=None,
        policy_id=None,
        at=now,
    )
    assert rec.workspace_id == "ws-1"
    assert rec.project_id == "proj-1"
    assert rec.mission_id == "m-1"
    assert rec.worker_id == "w-1"
    assert rec.stage == "research"
    assert rec.channel == "http"
    assert rec.protocol == "https"
    assert rec.host == "example.com"
    assert rec.ip == "93.184.216.34"
    assert rec.port == 443
    assert rec.method == "GET"
    assert rec.url == "https://example.com/data"
    assert rec.klass == "research"
    assert rec.outcome == "allowed"
    assert rec.code is None
    assert rec.policy_id is None
    assert rec.at == now

    with pytest.raises(FrozenInstanceError):
        rec.outcome = "refused"  # type: ignore[misc]


def test_memory_recorder_protocol_and_call_order() -> None:
    recorder = MemoryRecorder()
    assert isinstance(recorder, EgressRecorder)
    assert recorder.records == []

    def dispatch(sink: EgressRecorder, record: EgressRecord) -> None:
        sink.record(record)

    now = datetime.now(timezone.utc)
    r1 = EgressRecord(
        workspace_id="ws-1",
        project_id="proj-1",
        mission_id="m-1",
        worker_id="w-1",
        stage="research",
        channel="http",
        protocol="https",
        host="example.com",
        ip="93.184.216.34",
        port=443,
        method="GET",
        url="https://example.com/1",
        klass="research",
        outcome="allowed",
        code=None,
        policy_id=None,
        at=now,
    )
    r2 = EgressRecord(
        workspace_id="ws-1",
        project_id="proj-1",
        mission_id="m-1",
        worker_id="w-1",
        stage="research",
        channel="http",
        protocol="https",
        host="example.com",
        ip="93.184.216.34",
        port=443,
        method="POST",
        url="https://example.com/2",
        klass="none",
        outcome="refused",
        code="method_not_allowed",
        policy_id=None,
        at=now,
    )
    r3 = EgressRecord(
        workspace_id="ws-2",
        project_id="proj-2",
        mission_id=None,
        worker_id="w-2",
        stage="repository",
        channel="git",
        protocol="https",
        host="github.com",
        ip=None,
        port=443,
        method="GET",
        url="https://github.com/repo.git",
        klass="remote",
        outcome="allowed",
        code=None,
        policy_id="pol-1",
        at=now,
    )

    dispatch(recorder, r1)
    dispatch(recorder, r2)
    dispatch(recorder, r3)

    assert recorder.records == [r1, r2, r3]

