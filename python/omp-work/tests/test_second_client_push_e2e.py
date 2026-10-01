"""OMP-424-s04: the second client receives the journey through the same push path as Grok Bot.

One push run delivers both subscriptions. Each client's inbox verifies its
pushes with its own key and rejects them with the other client's key. The
second client's verified pushes include decision.required and mission.completed
for its mission, and each Idempotency-Key is the mission_event_id its events
read reports.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from omp_work import event_push
from omp_work.__main__ import main
from omp_work.operations.capabilities import provision_event_push
from omp_work.v1.models import MISSION_EVENT_TYPES
from second_client_world import Capability, World, world

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_HOSTS = ("grokbot.example", "second-client.example")
_PUSH_IP = "93.184.216.34"
_EVENT_TYPES = ",".join(MISSION_EVENT_TYPES)


def _install_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str | None, str, dict[str, str], object]]:
    """Record grokbot_send and answer the two push hosts from getaddrinfo.

    The recorder keeps the arguments send_signed passed. check_destination,
    run_push, send_signed, and signature stay on their real implementations.
    """
    captures: list[tuple[str, str | None, str, dict[str, str], object]] = []

    def record_send(
        url: str,
        token: str | None,
        idempotency_key: str,
        body: object,
        *,
        timeout: float = 10,
        headers: dict[str, str] | None = None,
    ) -> None:
        del timeout
        captures.append(
            (str(url), token, str(idempotency_key), dict(headers or {}), body)
        )

    real_getaddrinfo = event_push.socket.getaddrinfo

    def getaddrinfo(host: object, port: object = None, *args: object, **kwargs: object):
        name = (
            host.decode("ascii", "replace")
            if isinstance(host, bytes)
            else str(host or "")
        )
        name = name.strip().lower().rstrip(".")
        if name in _HOSTS:
            return [
                (
                    event_push.socket.AF_INET,
                    event_push.socket.SOCK_STREAM,
                    event_push.socket.IPPROTO_TCP,
                    "",
                    (_PUSH_IP, 0),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(event_push, "grokbot_send", record_send)
    monkeypatch.setattr(event_push.socket, "getaddrinfo", getaddrinfo)
    return captures


def _subscribe(
    w: World, capability: Capability, host: str, subscription_id: str
) -> None:
    code, body = w.client(
        capability,
        "subscribe",
        "--subscription-id",
        subscription_id,
        "--push-url",
        f"https://{host}/hook",
        "--event-types",
        _EVENT_TYPES,
    )
    assert code == 0, body
    subscription = body["result"]["subscription"]
    assert subscription["subscription_id"] == subscription_id
    assert subscription["push_url"] == f"https://{host}/hook"
    assert set(subscription["event_types"]) == set(MISSION_EVENT_TYPES)


def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code if code is not None else 1, captured.out, captured.err


def _push_key(subscription_id: str, capsys: pytest.CaptureFixture[str]) -> str:
    code, out, err = _cli(
        ["events", "push-key", "--subscription", subscription_id], capsys
    )
    assert code == 0, err
    key = out.strip()
    assert len(key) == 64 and all(char in "0123456789abcdef" for char in key)
    return key


def _header(headers: dict[str, object], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _body(item: dict[str, object]) -> dict[str, object]:
    body = item["body"]
    if isinstance(body, str):
        parsed = json.loads(body)
    else:
        parsed = body
    assert isinstance(parsed, dict)
    return parsed


def _inbox(
    w: World, capability: Capability, key_hex: str, pushes: Path
) -> dict[str, object]:
    code, body = w.client(
        capability, "inbox", "--push-key", key_hex, "--pushes", str(pushes)
    )
    assert code == 0, body
    return body


def test_second_client_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(MISSION_EVENT_TYPES) == 8
    captures = _install_seams(monkeypatch)

    with world(tmp_path, push_hosts=list(_HOSTS)) as w:
        signing_key = w.config.config_dir / "push-signing.key"
        signing_key.write_bytes(secrets.token_bytes(32))
        signing_key.chmod(0o600)
        assert len(signing_key.read_bytes()) == 32
        assert stat.S_IMODE(signing_key.stat().st_mode) == 0o600

        bearer = provision_event_push(w.config, w.workspace_id)
        client_config = tmp_path / "event-push-client.json"
        client_config.write_text(
            json.dumps(
                {
                    "base_url": w.base_url,
                    "workspace_id": str(w.workspace_id),
                    "owner_id": str(w.owner_id),
                    "bearer_file": str(bearer),
                }
            ),
            encoding="utf-8",
        )

        grokbot = w.mint("grokbot")
        second = w.mint("second-client")
        grokbot_subscription = str(uuid4())
        second_subscription = str(uuid4())
        _subscribe(w, grokbot, "grokbot.example", grokbot_subscription)
        _subscribe(w, second, "second-client.example", second_subscription)
        w.designate(second.actor_id)
        w.run_journey(second)

        monkeypatch.setenv("XDG_CONFIG_HOME", str(w.config.config_dir.parent.parent))
        capsys.readouterr()
        assert captures == []
        code, out, err = _cli(
            ["events", "push", "--client-config", str(client_config)], capsys
        )
        assert code == 0, err
        assert json.loads(out) == {"pushed": len(captures)}
        assert len(captures) > 0

        by_host: dict[str, list[dict[str, object]]] = {host: [] for host in _HOSTS}
        for url, token, idempotency_key, headers, body in captures:
            assert token is None
            assert _header(headers, "Authorization") is None
            host = urlsplit(url).hostname or ""
            assert host in by_host
            assert url == f"https://{host}/hook"
            raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            by_host[host].append(
                {
                    "headers": {**headers, "Idempotency-Key": idempotency_key},
                    "body": parsed,
                }
            )
        assert all(by_host[host] for host in _HOSTS)

        grokbot_key = _push_key(grokbot_subscription, capsys)
        second_key = _push_key(second_subscription, capsys)
        assert grokbot_key != second_key

        pushes_dir = {
            "grokbot.example": tmp_path / "grokbot-pushes.json",
            "second-client.example": tmp_path / "second-client-pushes.json",
        }
        for host, path in pushes_dir.items():
            path.write_text(json.dumps(by_host[host]), encoding="utf-8")

        clients = {
            "grokbot.example": (grokbot, grokbot_key, second_key),
            "second-client.example": (second, second_key, grokbot_key),
        }
        for host, (capability, own_key, other_key) in clients.items():
            path = pushes_dir[host]
            own = _inbox(w, capability, own_key, path)
            assert own["status"] == "accepted"
            assert own["rejected_count"] == 0
            assert own["accepted_count"] == own["total"] == len(by_host[host])
            other = _inbox(w, capability, other_key, path)
            assert other["status"] == "rejected"
            assert other["accepted_count"] == 0
            assert other["rejected_count"] == other["total"] == len(by_host[host])

        verified = _inbox(w, second, second_key, pushes_dir["second-client.example"])[
            "verified"
        ]
        assert isinstance(verified, list)
        mission_id = str(w.mission_id)
        types_for_mission = {
            str(_body(item).get("type"))
            for item in verified
            if isinstance(item, dict)
            and str(_body(item).get("mission_id")) == mission_id
        }
        assert {"decision.required", "mission.completed"} <= types_for_mission

        code, events = w.client(second, "events")
        assert code == 0, events
        rows = events["events"]
        assert isinstance(rows, list)
        reported = {str(row["mission_event_id"]): row for row in rows}
        for item in verified:
            assert isinstance(item, dict)
            event = _body(item)
            idempotency_key = _header(item["headers"], "Idempotency-Key")
            event_id = str(event["mission_event_id"])
            assert idempotency_key == event_id
            assert event_id in reported
            assert reported[event_id]["type"] == event["type"]
            assert str(reported[event_id]["mission_id"]) == str(event["mission_id"])
