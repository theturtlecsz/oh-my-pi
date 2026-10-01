"""OMP-430-s06: the stop watcher pauses units it stopped and resumes exactly those.

No database. Drives ``watch_tick`` directly over the status sequence
stopped, stopped, released with unit ``a`` active and unit ``b`` inactive:
unit ``a`` is stopped once and, after the release, started once; unit ``b`` is
never stopped and never started. A watcher that stopped nothing starts
nothing. The ``paused`` set is the one ``watch`` keeps across ticks.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from omp_work.operations.stop import watch, watch_tick
from omp_work.v1.api_models import StopStatusView


class _CommandRecorder:
    def __init__(self, active_units: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.active_units = set(active_units or ())

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        if (
            len(cmd) >= 4
            and cmd[0] == "systemctl"
            and cmd[1] == "--user"
            and cmd[2] == "is-active"
        ):
            return 0 if cmd[3] in self.active_units else 3
        if len(cmd) >= 4 and cmd[0] == "systemctl" and cmd[1] == "--user" and cmd[2] == "stop":
            self.active_units.discard(cmd[3])
        return 0

    def start_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[2:3] == ["start"]]

    def stop_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[2:3] == ["stop"]]


def _status(value: bool) -> Any:
    return lambda: value


def test_stopped_stopped_released_stops_a_once_starts_a_once_never_b() -> None:
    runner = _CommandRecorder(active_units={"a.service"})
    units = ["a.service", "b.service"]
    paused: set[str] = set()

    first = watch_tick(units, False, status_source=_status(True), run_cmd=runner, paused=paused)
    assert first is True
    assert paused == {"a.service"}
    assert runner.stop_calls() == [["systemctl", "--user", "stop", "a.service"]]

    second = watch_tick(units, first, status_source=_status(True), run_cmd=runner, paused=paused)
    assert second is True
    assert paused == {"a.service"}
    # The unit is already down, so the second stopped tick stops nothing more.
    assert runner.stop_calls() == [["systemctl", "--user", "stop", "a.service"]]

    released = watch_tick(units, second, status_source=_status(False), run_cmd=runner, paused=paused)
    assert released is False
    assert paused == set()
    assert runner.start_calls() == [["systemctl", "--user", "start", "a.service"]]

    # Unit b was inactive throughout: never stopped, never started.
    assert not any(call[-1] == "b.service" and call[2] in {"stop", "start"} for call in runner.calls)


def test_watcher_that_stopped_nothing_starts_nothing() -> None:
    runner = _CommandRecorder(active_units={"a.service"})
    units = ["a.service"]
    paused: set[str] = set()

    # Never stopped → the released tick has nothing to resume.
    assert watch_tick(units, False, status_source=_status(False), run_cmd=runner, paused=paused) is False
    assert paused == set()
    assert runner.start_calls() == []
    assert runner.stop_calls() == []


def test_released_tick_starts_only_the_stopped_unit_after_a_failed_read() -> None:
    runner = _CommandRecorder(active_units={"a.service", "b.service"})
    units = ["a.service", "b.service"]
    paused: set[str] = set()

    def failing_status() -> bool:
        raise RuntimeError("network down")

    # Prior stopped state; the failed read keeps enforcing and stops both.
    assert watch_tick(units, True, status_source=failing_status, run_cmd=runner, paused=paused) is True
    assert paused == {"a.service", "b.service"}

    assert watch_tick(units, True, status_source=_status(False), run_cmd=runner, paused=paused) is False
    assert paused == set()
    assert runner.start_calls() == [
        ["systemctl", "--user", "start", "a.service"],
        ["systemctl", "--user", "start", "b.service"],
    ]


class _FakeClient:
    def __init__(self, stopped: bool) -> None:
        self.stopped = stopped

    def stop_status(self) -> StopStatusView:
        return StopStatusView(
            workspace_id=str(UUID("00000000-0000-7000-8000-000000000010")),
            stopped=self.stopped,
            reason="loop",
        )


def test_watch_keeps_one_paused_set_across_ticks() -> None:
    """Two stopped ticks stop unit a once; the released tick resumes it."""
    runner = _CommandRecorder(active_units={"a.service"})
    watch(
        _FakeClient(True),  # type: ignore[arg-type]
        ["a.service"],
        interval=0.001,
        max_ticks=2,
        run_cmd=runner,
    )
    assert runner.stop_calls() == [["systemctl", "--user", "stop", "a.service"]]
    assert runner.start_calls() == []

    # The watcher's released tick starts exactly the unit it had stopped.
    runner.calls.clear()
    watch_tick(["a.service"], False, status_source=_status(False), run_cmd=runner, paused={"a.service"})
    assert runner.start_calls() == [["systemctl", "--user", "start", "a.service"]]
