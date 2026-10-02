"""OMP-514-s02: CLI test for event push runner bearer token integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from omp_work import event_push
from omp_work.__main__ import main
from omp_work.operations import stop as stop_ops

MASTER = bytes(range(32))
WORKSPACE = UUID("00000000-0000-7000-8000-0000000004f0")
SUB_A = UUID("00000000-0000-7000-8000-0000000004a1")
TOKEN = "sekrit-push-token-9f2a"
_UNSET = object()


def _subscription(
    subscription_id: UUID,
    client_id: UUID,
    push_url: str | None,
    *,
    event_types: list[str] | None = None,
    cursor: int = 0,
) -> dict[str, Any]:
    return {
        "subscription_id": str(subscription_id),
        "client_id": str(client_id),
        "push_url": push_url,
        "event_types": event_types or ["mission.started", "important_finding"],
        "cursor_sequence": cursor,
        "deleted": False,
    }


def _event(sequence: int, type_: str) -> dict[str, Any]:
    return {
        "mission_event_id": str(uuid5(NAMESPACE_URL, f"ev:{sequence}:{type_}")),
        "sequence": sequence,
        "mission_id": str(WORKSPACE),
        "type": type_,
        "trigger": "t",
        "occurred_at": "2026-09-30T12:00:00+00:00",
        "source_event_id": str(uuid4()),
        "evidence_refs": [],
    }


class _FakeClient:
    def __init__(
        self, subscriptions: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> None:
        self.subscriptions = subscriptions
        self.events = events
        self.advances: list[Any] = []

    def event_subscriptions(self) -> dict[str, Any]:
        return {"subscriptions": [dict(row) for row in self.subscriptions]}

    def mission_events(
        self,
        after_sequence: int = 0,
        limit: int = 500,
        mission_id: UUID | None = None,
    ) -> dict[str, Any]:
        remaining = [e for e in self.events if e["sequence"] > after_sequence]
        page = remaining[:limit]
        return {
            "events": page,
            "watermark_sequence": self.events[-1]["sequence"] if self.events else 0,
            "next_after_sequence": page[-1]["sequence"] if page else after_sequence,
            "has_more": len(remaining) > len(page),
        }

    def execute(self, envelope: Any) -> Any:
        self.advances.append(envelope)
        return {"command": envelope.command.type}

    def close(self) -> None:
        pass


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        idem: str,
        body: Any,
        *,
        key: bytes,
        token: Any = _UNSET,
        **kwargs: Any,
    ) -> None:
        self.calls.append(
            {
                "url": url,
                "idem": idem,
                "body": body,
                "key": key,
                "token": token,
                "kwargs": kwargs,
            }
        )


def _setup_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_FakeClient, _Recorder, Path, Path, Path]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "omp" / "work-ledger"
    config.mkdir(parents=True)
    key_file = config / "push-signing.key"
    key_file.write_bytes(MASTER)
    key_file.chmod(0o600)
    (config / "push-destinations.json").write_text(
        json.dumps({"allowed_hosts": ["hooks.example"]})
    )
    token_file = tmp_path / "token.txt"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    bearer_config = config / "push-bearer.json"
    bearer_config.write_text(
        json.dumps({"token_files": {"https://hooks.example/h": str(token_file)}})
    )
    bearer_config.chmod(0o600)

    sub = _subscription(SUB_A, uuid4(), "https://hooks.example/h")
    event = _event(4, "mission.started")
    client = _FakeClient([sub], [event])

    recorder = _Recorder()
    monkeypatch.setattr(
        stop_ops, "load_client", lambda _c, _b=None: (client, WORKSPACE)
    )
    monkeypatch.setattr(
        event_push,
        "check_destination",
        lambda url, *, allowed_hosts, resolve=None: None,
    )
    monkeypatch.setattr(event_push, "send_signed", recorder)

    return client, recorder, token_file, bearer_config, config


def test_push_command_configured_bearer_sends_token_and_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, recorder, _token_file, _bearer_config, _config = _setup_env(
        tmp_path, monkeypatch
    )

    rc = main(["events", "push", "--client-config", str(tmp_path / "unused.json")])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"pushed": 1}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == "https://hooks.example/h"
    assert recorder.calls[0]["token"] == TOKEN
    assert len(client.advances) == 1
    assert client.advances[0].command.type == "advance_event_cursor"


def test_push_command_refuses_0644_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, recorder, token_file, _bearer_config, _config = _setup_env(
        tmp_path, monkeypatch
    )
    token_file.chmod(0o644)

    rc = main(["events", "push", "--client-config", str(tmp_path / "unused.json")])

    assert rc == 2
    assert len(recorder.calls) == 0
    assert len(client.advances) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert captured.err.startswith("events: ")
    assert "unsafe token file permissions" in captured.err


def test_push_command_refuses_relative_token_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, recorder, _token_file, bearer_config, _config = _setup_env(
        tmp_path, monkeypatch
    )
    bearer_config.write_text(
        json.dumps({"token_files": {"https://hooks.example/h": "relative/token.txt"}})
    )

    rc = main(["events", "push", "--client-config", str(tmp_path / "unused.json")])

    assert rc == 2
    assert len(recorder.calls) == 0
    assert len(client.advances) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert captured.err.startswith("events: ")
    assert "token path must be absolute" in captured.err


def test_push_command_without_push_bearer_calls_recorder_with_no_token_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, recorder, _token_file, bearer_config, _config = _setup_env(
        tmp_path, monkeypatch
    )
    bearer_config.unlink()

    rc = main(["events", "push", "--client-config", str(tmp_path / "unused.json")])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"pushed": 1}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == "https://hooks.example/h"
    assert recorder.calls[0]["token"] is _UNSET
    assert len(client.advances) == 1
    assert client.advances[0].command.type == "advance_event_cursor"
