"""OMP-503: the installer renders the ops.alarm/ops.digest push timer.

The work-ledger installer replaces ``alarms run`` / ``alarms digest`` on the
host with a five-minute ``omp-work events push`` timer. The rendered service
must invoke the installed Python with the shared capability bearer file, the
timer must fire every five minutes, and no rendered unit may fall back to the
removed ``alarms``/``grokbot`` wiring or an ``EnvironmentFile``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

import omp_work.__main__ as omp_work_main
from omp_work.operations import stop as stop_ops

INSTALL_SCRIPT = (
    Path(__file__).resolve().parents[3] / "infra" / "work-ledger" / "install.sh"
)


def _render(tmp_path: Path) -> tuple[Path, Path]:
    """Render units with an argv-printing fake python and a spaced XDG config."""
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir()
    fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_python.chmod(0o755)
    config = tmp_path / "xdg config with space"
    units = tmp_path / "review-units"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            "--render-only",
            "--python",
            str(fake_python),
            "--unit-dir",
            str(units),
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(config),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return units, config


def test_events_push_service_runs_the_events_push_subcommand(tmp_path: Path) -> None:
    """ExecStart decodes to the installed python running the events push command."""
    units, config = _render(tmp_path)
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-events-push.service").read_text().splitlines()
        if line.startswith("ExecStart=")
    )
    probe = subprocess.run(
        shlex.split(command.replace("%%", "%")), capture_output=True, text=True
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.splitlines() == [
        "-I",
        "-B",
        "-m",
        "omp_work",
        "events",
        "push",
        "--client-config",
        str(config / "omp-work" / "client.json"),
        "--bearer-file",
        str(config / "omp" / "work-ledger" / "capabilities" / "event-push.json"),
    ]


def test_events_push_argv_is_accepted_by_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered argv parses as ``events push`` and reaches client loading.

    A removed subcommand would be rejected by argparse with exit code 2; the
    credential failure surfaced by ``load_client`` proves the subcommand exists.
    """
    units, _config = _render(tmp_path)
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-events-push.service").read_text().splitlines()
        if line.startswith("ExecStart=")
    )
    probe = subprocess.run(
        shlex.split(command.replace("%%", "%")), capture_output=True, text=True
    )
    assert probe.returncode == 0, probe.stderr
    argv = probe.stdout.splitlines()
    cli_argv = argv[argv.index("omp_work") + 1 :]

    def fail_load_client(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("no credentials in the rendered-unit probe")

    monkeypatch.setattr(stop_ops, "load_client", fail_load_client)
    assert omp_work_main.main(cli_argv) == 255


def test_events_push_timer_and_units_drop_alarm_wiring(tmp_path: Path) -> None:
    """The timer fires every five minutes and no unit name or body regresses."""
    units, _config = _render(tmp_path)
    assert (units / "omp-events-push.timer").read_text() == (
        "[Timer]\nOnCalendar=*:0/5\nPersistent=true\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    rendered = {path.name: path.read_text() for path in units.iterdir()}
    assert "omp-events-push.service" in rendered
    for name, content in rendered.items():
        lowered = content.lower()
        for banned in ("alarms", "grokbot", "environmentfile"):
            assert banned not in lowered, f"{name} mentions {banned!r}"
