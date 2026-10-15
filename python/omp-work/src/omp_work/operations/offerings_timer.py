"""Install the daily model-offerings check as a systemd user timer (OMP-615-s09).

The unit runs ``omp_work offerings check`` with no ``WorkingDirectory``, so
every path in ExecStart is absolute and every capture path the sources file
names must be too: the check resolves them against the process CWD. Nothing is
enabled or started here; the enable commands are printed for the owner.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..routing.offerings_evaluation import default_record_dir
from ..routing.offerings_inputs import parse_sources
from .stop import default_systemd_dir

SERVICE_NAME = "omp-offerings-check.service"
TIMER_NAME = "omp-offerings-check.timer"
ENABLE_COMMANDS = (
    "systemctl --user daemon-reload",
    f"systemctl --user enable --now {TIMER_NAME}",
)

_SERVICE = """\
[Unit]
Description=Daily read-only model offerings check

[Service]
Type=oneshot
UMask=0077
SuccessExitStatus=3
ExecStart={exec_start}
"""

_TIMER = f"""\
[Unit]
Description=Run the daily model offerings check

[Timer]
OnCalendar=daily
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _quote(argument: str, field: str) -> str:
    """One ExecStart word: double-quoted, with systemd escapes and specifiers."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in argument):
        raise ValueError(f"{field}: control character cannot be written to a unit file")
    escaped = (
        argument.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    )
    return f'"{escaped}"'


def _absolute(value: str | os.PathLike[str], field: str) -> str:
    text = os.fspath(value)
    if not os.path.isabs(text):
        raise ValueError(f"{field}: must be an absolute path, got {text!r}")
    return text


def install_timer(
    sources: str | os.PathLike[str],
    record_dir: str | os.PathLike[str] | None = None,
    omp: str | os.PathLike[str] | None = None,
    systemd_dir: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Write the service and timer units; refuse before writing anything."""
    sources_path = os.path.abspath(os.fspath(sources))
    try:
        raw = Path(sources_path).read_bytes()
    except OSError as exc:
        raise ValueError(f"sources: cannot read {sources_path!r}: {exc.strerror}") from exc
    parsed = parse_sources(raw)  # ValueError naming the offending field
    _absolute(parsed.notices, "notices")
    for lane in parsed.lanes.values():
        if lane.path is not None:
            _absolute(lane.path, f"lanes.{lane.lane}.path")

    record = os.path.abspath(os.fspath(record_dir if record_dir is not None else default_record_dir()))
    omp_found = shutil.which(os.fspath(omp) if omp is not None else "omp")
    if omp_found is None:
        raise ValueError(f"omp: executable not found ({omp if omp is not None else 'omp'!r})")
    python = os.path.abspath(sys.executable)

    words = [
        python, "-I", "-B", "-m", "omp_work", "offerings", "check",
        "--sources", sources_path, "--record-dir", record, "--omp", os.path.abspath(omp_found),
    ]
    fields = ["python", "", "", "", "", "", "", "", "sources", "", "record_dir", "", "omp"]
    exec_start = " ".join(_quote(word, field or "argument") for word, field in zip(words, fields))

    target = Path(systemd_dir) if systemd_dir is not None else default_systemd_dir()
    target.mkdir(parents=True, exist_ok=True)
    service = target / SERVICE_NAME
    timer = target / TIMER_NAME
    service.write_text(_SERVICE.format(exec_start=exec_start), encoding="utf-8")
    timer.write_text(_TIMER, encoding="utf-8")
    for command in ENABLE_COMMANDS:
        print(command)
    return [service, timer]
