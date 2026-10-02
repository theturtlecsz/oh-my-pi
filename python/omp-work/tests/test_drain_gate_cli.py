"""OMP-519-s02: the top-level ``omp-work drain check`` gate command.

Without a database. ``__main__`` imports the drain module and calls
``in_flight`` through the module attribute, so these tests monkeypatch
``omp_work.operations.drain.in_flight`` (restored after each test) and defend
the four contracts the deploy gate relies on:

- an empty in-flight list prints ``{"drained": true, ...}`` and exits 0;
- one running mission prints its mission_id, sets ``drained`` false, exits 1;
- a database failure prints ``drain: <error>`` on stderr, nothing on stdout,
  and exits 255;
- ``drain`` with no subcommand exits 2 without calling ``in_flight``.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from omp_work.__main__ import main
from omp_work.operations import drain as drain_ops


def _mission(mission_id: str) -> dict[str, object]:
    return {
        "mission_id": mission_id,
        "project_id": "00000000-0000-7000-8000-000000000020",
        "objective": "Drain gate",
        "status": "running",
        "created_at": "2026-10-01T00:00:00+00:00",
    }


def test_drain_check_empty_prints_drained_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(drain_ops, "in_flight", lambda config: [])
    assert main(["drain", "check"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"drained": True, "in_flight": []}
    assert captured.err == ""


def test_drain_check_in_flight_exits_one_and_lists_mission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(drain_ops, "in_flight", lambda config: [_mission("m-1")])
    assert main(["drain", "check"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["drained"] is False
    assert [mission["mission_id"] for mission in payload["in_flight"]] == ["m-1"]
    assert captured.err == ""


def test_drain_check_database_failure_exits_255(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(config):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(drain_ops, "in_flight", boom)
    assert main(["drain", "check"]) == 255
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("drain:")


def test_drain_without_subcommand_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        drain_ops, "in_flight", lambda config: calls.append(config) or []
    )
    with pytest.raises(SystemExit) as excinfo:
        main(["drain"])
    assert excinfo.value.code == 2
    assert calls == []
