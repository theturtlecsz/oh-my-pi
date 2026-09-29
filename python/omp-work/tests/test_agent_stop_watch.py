"""OMP-405-s06: tests for stop watch and install-guards.

Validates:
- stopped -> only active units get stop;
- stopped then failing read -> next tick still stops a unit that became active;
- not stopped -> no stop calls;
- released after a stop -> no start calls;
- install-guards writes exactly the expected drop-ins and watcher unit under tmp_path
  and rejects the watcher unit as a target.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from omp_work.__main__ import main
from omp_work.operations import stop as stop_ops
from omp_work.operations.stop import install_guards, watch_tick
from omp_work.v1.api_models import StopStatusView


class _CommandRecorder:
    def __init__(self, active_units: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.active_units = set(active_units or ())

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        if len(cmd) >= 4 and cmd[0] == "systemctl" and cmd[1] == "--user" and cmd[2] == "is-active":
            unit = cmd[3]
            return 0 if unit in self.active_units else 3
        return 0


def test_stopped_only_active_units_get_stop() -> None:
    runner = _CommandRecorder(active_units={"active.service"})
    units = ["active.service", "inactive.service"]

    is_stopped = watch_tick(
        units,
        last_known_stopped=False,
        status_source=lambda: True,
        run_cmd=runner,
    )

    assert is_stopped is True
    assert runner.calls == [
        ["systemctl", "--user", "is-active", "active.service"],
        ["systemctl", "--user", "stop", "active.service"],
        ["systemctl", "--user", "is-active", "inactive.service"],
    ]


def test_stopped_then_failing_read_retains_enforcement_and_stops_active_unit() -> None:
    runner = _CommandRecorder(active_units={"newly-active.service"})
    units = ["idle.service", "newly-active.service"]

    def failing_status() -> bool:
        raise RuntimeError("network down")

    # Prior state was stopped (True). On a failing read, keep last known (True).
    is_stopped = watch_tick(
        units,
        last_known_stopped=True,
        status_source=failing_status,
        run_cmd=runner,
    )

    assert is_stopped is True
    assert runner.calls == [
        ["systemctl", "--user", "is-active", "idle.service"],
        ["systemctl", "--user", "is-active", "newly-active.service"],
        ["systemctl", "--user", "stop", "newly-active.service"],
    ]


def test_not_stopped_no_stop_calls() -> None:
    runner = _CommandRecorder(active_units={"active.service"})
    units = ["active.service", "other.service"]

    is_stopped = watch_tick(
        units,
        last_known_stopped=False,
        status_source=lambda: False,
        run_cmd=runner,
    )

    assert is_stopped is False
    assert runner.calls == []
    # Explicitly ensure no stop or start calls occurred.
    assert not any("stop" in call or "start" in call for call in runner.calls)


def test_released_after_stop_no_start_calls() -> None:
    runner = _CommandRecorder(active_units=set())
    units = ["active.service"]

    # Transition from stopped (last_known=True) to released (status returns False).
    is_stopped = watch_tick(
        units,
        last_known_stopped=True,
        status_source=lambda: False,
        run_cmd=runner,
    )

    assert is_stopped is False
    # Never start a unit; after release the owner restarts units.
    assert not any("start" in call for call in runner.calls)
    assert not any("stop" in call for call in runner.calls)


def test_watch_tick_supports_stop_status_view() -> None:
    view = StopStatusView(
        workspace_id="00000000-0000-7000-8000-000000000010",
        stopped=True,
        reason="loop",
    )
    runner = _CommandRecorder(active_units={"u.service"})
    is_stopped = watch_tick(["u.service"], last_known_stopped=False, status_source=lambda: view, run_cmd=runner)
    assert is_stopped is True
    assert ["systemctl", "--user", "stop", "u.service"] in runner.calls


def test_install_guards_writes_expected_files(tmp_path: Path) -> None:
    units = ["agent-alpha.service", "agent-beta.service"]
    written = install_guards(units, systemd_dir=tmp_path, interval=5)

    expected_drop_in = (
        "[Service]\n"
        f"ExecCondition={sys.executable} -m omp_work stop check\n"
        "TimeoutStopSec=20\n"
    )

    drop_in_alpha = tmp_path / "agent-alpha.service.d" / "50-omp-agent-stop.conf"
    assert drop_in_alpha.is_file()
    assert drop_in_alpha.read_text(encoding="utf-8") == expected_drop_in

    drop_in_beta = tmp_path / "agent-beta.service.d" / "50-omp-agent-stop.conf"
    assert drop_in_beta.is_file()
    assert drop_in_beta.read_text(encoding="utf-8") == expected_drop_in

    watcher_service = tmp_path / "omp-agent-stop.service"
    assert watcher_service.is_file()
    watcher_text = watcher_service.read_text(encoding="utf-8")

    assert "[Unit]\n" in watcher_text
    assert "Description=OMP agent stop watcher\n" in watcher_text
    assert "[Service]\n" in watcher_text
    assert (
        f"ExecStart={sys.executable} -m omp_work stop watch --unit agent-alpha.service --unit agent-beta.service --interval 5\n"
        in watcher_text
    )
    assert "Restart=always\n" in watcher_text
    assert "RestartSec=5\n" in watcher_text
    assert "[Install]\n" in watcher_text
    assert "WantedBy=default.target\n" in watcher_text

    assert written == [drop_in_alpha, drop_in_beta, watcher_service]


def test_install_guards_rejects_watcher_unit_and_omp_work_targets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing target unit: omp-agent-stop.service"):
        install_guards(["omp-agent-stop.service"], systemd_dir=tmp_path)

    with pytest.raises(ValueError, match="refusing target unit: omp-agent-stop"):
        install_guards(["omp-agent-stop"], systemd_dir=tmp_path)

    with pytest.raises(ValueError, match="refusing target unit: omp-work-server"):
        install_guards(["omp-work-server"], systemd_dir=tmp_path)

    with pytest.raises(ValueError, match="refusing target unit: omp-work-daemon.service"):
        install_guards(["omp-work-daemon.service"], systemd_dir=tmp_path)

    with pytest.raises(ValueError, match="at least one target unit is required"):
        install_guards([], systemd_dir=tmp_path)


def test_cli_install_guards_writes_files_and_prints_instructions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([
        "stop",
        "install-guards",
        "--unit",
        "worker.service",
        "--systemd-dir",
        str(tmp_path),
        "--interval",
        "5",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "systemctl --user daemon-reload" in captured.out
    assert "systemctl --user enable --now omp-agent-stop.service" in captured.out

    assert (tmp_path / "worker.service.d" / "50-omp-agent-stop.conf").is_file()
    assert (tmp_path / "omp-agent-stop.service").is_file()


def test_cli_install_guards_rejects_watcher_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([
        "stop",
        "install-guards",
        "--unit",
        "omp-agent-stop.service",
        "--systemd-dir",
        str(tmp_path),
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "refusing target unit" in captured.err


def test_watch_loop_with_max_ticks() -> None:
    runner = _CommandRecorder(active_units={"agent.service"})
    view = StopStatusView(
        workspace_id="00000000-0000-7000-8000-000000000010",
        stopped=True,
        reason="loop test",
    )

    class _FakeClient:
        def stop_status(self) -> StopStatusView:
            return view

    client = _FakeClient()
    stop_ops.watch(
        client,  # type: ignore[arg-type]
        ["agent.service"],
        interval=0.001,
        max_ticks=2,
        run_cmd=runner,
    )
    assert ["systemctl", "--user", "stop", "agent.service"] in runner.calls

