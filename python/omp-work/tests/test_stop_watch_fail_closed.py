"""Stop watching recovers contract drift and bounds continuous read outages."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest

import omp_work
from omp_work.__main__ import main
from omp_work.operations import stop
from omp_work.v1 import client as client_module
from omp_work.v1.client import WorkClient


WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
TOKEN = "private-bearer-token"


class _Units:
    def __init__(self) -> None:
        self.active = {"a.service"}
        self.actions: list[tuple[str, str]] = []

    def __call__(self, command: list[str]) -> int:
        action, unit = command[2:]
        if action == "is-active":
            return 0 if unit in self.active else 3
        self.actions.append((action, unit))
        if action == "stop":
            self.active.discard(unit)
        elif action == "start":
            self.active.add(unit)
        return 0


def _client_config(tmp_path: Path) -> tuple[Path, Path]:
    bearer = tmp_path / "bearer.json"
    bearer.write_text(json.dumps({"token": TOKEN}))
    bearer.chmod(0o600)
    config = tmp_path / "client.json"
    config.write_text(json.dumps({
        "base_url": "http://work.test",
        "workspace_id": str(WORKSPACE),
        # CLI override must survive every rebuild.
        "bearer_file": str(tmp_path / "missing-bearer.json"),
    }))
    return config, bearer


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Client]:
    constructor = httpx.Client
    clients: list[httpx.Client] = []

    def build(**kwargs) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        client = constructor(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(client_module.httpx, "Client", build)
    return clients


def test_contract_mismatch_rebuilds_with_disk_digest_and_stops_same_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"paths":["binding.txt","manifest.json"]}')
    binding = bundle / "binding.txt"
    binding.write_text("old contract")
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: bundle)
    digests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        digests.append(request.headers["X-OMP-Contract-SHA256"])
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if digests[-1] != omp_work.contract_sha256():
            return httpx.Response(409, json={"error": {
                "code": "contract_mismatch",
                "diagnostics": [f"restart required\ncredential {TOKEN}"],
            }})
        return httpx.Response(200, json={"workspace_id": str(WORKSPACE), "stopped": True})

    clients = _transport(monkeypatch, handler)
    config, bearer = _client_config(tmp_path)
    client, _ = stop.load_client(config, bearer)
    binding.write_text("new contract")
    units = _Units()
    try:
        stop.watch(client, ["a.service", "b.service"], max_ticks=2, interval=0,
                   client_config=config, bearer_file=bearer, run_cmd=units)
        assert units.actions == [("stop", "a.service")]
        assert len(digests) == 3
        assert digests[0] != digests[1] == omp_work.contract_sha256()
        assert digests[2] == digests[1]
        assert clients[1].is_closed
        assert not clients[0].is_closed  # Original client remains caller-owned.
    finally:
        client.close()
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "contract_mismatch" in lines[0] and "restart required" in lines[0]
    assert TOKEN not in lines[0]


@pytest.mark.parametrize(("window", "expected"), [
    (300.0, [("stop", "a.service"), ("start", "a.service")]),
    (600.0, []),
])
def test_continuous_outage_stops_after_window_and_recovery_resumes_only_paused_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    window: float, expected: list[tuple[str, str]],
) -> None:
    now = [0.0]
    reads = 0
    units = _Units()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        reads += 1
        if reads <= 4:
            # At 0, 100, 200, 300 seconds: threshold not exceeded yet.
            assert units.actions == []
        if reads <= 5:
            return httpx.Response(503, json={"error": {
                "code": "unavailable", "diagnostics": ["service restarting"],
            }})
        return httpx.Response(200, json={"workspace_id": str(WORKSPACE), "stopped": False})

    _transport(monkeypatch, handler)
    monkeypatch.setattr(stop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(stop.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    config, bearer = _client_config(tmp_path)
    client, _ = stop.load_client(config, bearer)
    try:
        stop.watch(client, ["a.service", "b.service"], interval=100, max_ticks=7,
                   fail_closed_after=window, run_cmd=units)
    finally:
        client.close()
    assert units.actions == expected
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 5
    assert all("unavailable" in line and "service restarting" in line for line in lines)


def test_successful_read_resets_outage_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        reads += 1
        if reads == 3:
            return httpx.Response(200, json={"workspace_id": str(WORKSPACE), "stopped": False})
        raise httpx.ConnectError("connection refused", request=request)

    _transport(monkeypatch, handler)
    monkeypatch.setattr(stop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(stop.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    config, bearer = _client_config(tmp_path)
    client, _ = stop.load_client(config, bearer)
    units = _Units()
    try:
        # Total elapsed 500s, but neither continuous outage exceeds 300s.
        stop.watch(client, ["a.service"], interval=100, max_ticks=6, run_cmd=units)
    finally:
        client.close()
    assert units.actions == []


def test_failed_contract_retry_is_logged_and_remains_subject_to_outage_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = [0.0]
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(409, json={"error": {
            "code": "contract_mismatch", "diagnostics": ["deploy still pending"],
        }})

    clients = _transport(monkeypatch, handler)
    monkeypatch.setattr(stop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(stop.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    config, bearer = _client_config(tmp_path)
    client, _ = stop.load_client(config, bearer)
    units = _Units()
    try:
        stop.watch(client, ["a.service"], interval=301, max_ticks=2,
                   client_config=config, bearer_file=bearer, run_cmd=units)
        assert units.actions == [("stop", "a.service")]
        # Retry once per tick, close both superseded and final replacement.
        assert requests == 4
        assert all(client.is_closed for client in clients[1:])
    finally:
        client.close()
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == requests
    assert all("contract_mismatch" in line and "deploy still pending" in line for line in lines)


@pytest.mark.parametrize("option", [[], ["--fail-closed-after", "42"]])
def test_cli_passes_rebuild_config_and_outage_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: list[str]
) -> None:
    config, bearer = _client_config(tmp_path)
    calls: list[dict[str, object]] = []

    def watch(client: WorkClient, units: list[str], **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(stop, "watch", watch)
    assert main(["stop", "watch", "--unit", "a.service", "--client-config", str(config),
                 "--bearer-file", str(bearer), *option]) == 0
    assert calls == [{"interval": 5.0, "client_config": config,
                      "bearer_file": bearer, "fail_closed_after": 42.0 if option else 300.0}]
