"""OMP-405: the ``omp-work stop`` operational commands.

``stop check`` is the systemd ``ExecCondition=`` probe for the host unit
timers: exit 0 when the agent stop is not engaged, 1 when it is (the unit start
is skipped), and 255 on any error, including an unreachable service (the unit
start fails so ``Restart=`` retries). ``stop status`` prints the workspace's
``StopStatusView`` as JSON; ``engage`` and ``release`` submit the matching
command envelope with fresh identifiers and print the receipt.

Credentials come from the shared client config
(``$XDG_CONFIG_HOME/omp-work/client.json``, written by
``write_client_config``); ``--client-config`` and ``--bearer-file`` override
the path and the capability file it names.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - systemctl is invoked as an argv list; no shell
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ..v1.api_models import CommandResponse, StopStatusView
from ..v1.client import WorkClient
from ..v1.models import (
    CommandEnvelope,
    EngageStopCommand,
    ReleaseStopCommand,
    StopReasonPayload,
)

__all__ = [
    "API_VERSION",
    "check",
    "default_systemd_dir",
    "engage",
    "install_guards",
    "load_client",
    "release",
    "status",
    "tick",
    "watch",
    "watch_tick",
]

API_VERSION = "work.omp.dev/v1"


def load_client(
    client_config: Path, bearer_file: Path | None = None
) -> tuple[WorkClient, UUID]:
    """Build the WorkClient the client config names. ``bearer_file`` overrides
    the capability file the config points at (the ``--bearer-file`` flag)."""
    config = json.loads(client_config.read_text(encoding="utf-8"))
    if bearer_file is None:
        bearer_file = Path(str(config["bearer_file"]))
    workspace_id = UUID(str(config["workspace_id"]))
    return (
        WorkClient(str(config["base_url"]), workspace_id, bearer_file),
        workspace_id,
    )


def status(client: WorkClient) -> StopStatusView:
    return client.stop_status()


def check(client: WorkClient) -> int:
    """ExecCondition result: 0 running, 1 stopped, 255 on any error. systemd
    logs the diagnostic, so the failing reason goes to stderr."""
    try:
        stopped = client.stop_status().stopped
    except Exception as error:
        print(f"stop: {error}", file=sys.stderr)
        return 255
    return 1 if stopped else 0


def engage(client: WorkClient, workspace_id: UUID, reason: str) -> CommandResponse:
    return client.execute(_envelope(workspace_id, "engage_stop", reason))


def release(client: WorkClient, workspace_id: UUID, reason: str) -> CommandResponse:
    return client.execute(_envelope(workspace_id, "release_stop", reason))


def _envelope(
    workspace_id: UUID, command_type: str, reason: str
) -> CommandEnvelope:
    payload = StopReasonPayload(reason=reason)
    command = (
        EngageStopCommand(type="engage_stop", payload=payload)
        if command_type == "engage_stop"
        else ReleaseStopCommand(type="release_stop", payload=payload)
    )
    return CommandEnvelope(
        api_version=API_VERSION,
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )


def _default_run_cmd(cmd: list[str]) -> int:
    result = subprocess.run(cmd, capture_output=True)  # nosec B603 - argv list, no shell; executable is the literal "systemctl" and the unit name is one argument
    return result.returncode


def watch_tick(
    units: Sequence[str],
    last_known_stopped: bool | Callable[[], bool | StopStatusView] | WorkClient = False,
    command_runner: Callable[[list[str]], Any] | None = None,
    *,
    status_source: Callable[[], bool | StopStatusView] | WorkClient | None = None,
    read_status: Callable[[], bool | StopStatusView] | WorkClient | None = None,
    run_cmd: Callable[[list[str]], Any] | None = None,
    paused: set[str] | None = None,
) -> bool:
    """Evaluate one tick of the stop watcher.

    Reads the current workspace stop status. If stopped (or if reading fails
    after having been stopped, preserving the last known enforcing state),
    checks whether each unit is active via ``systemctl --user is-active <unit>``
    and stops it with ``systemctl --user stop <unit>`` if active. Every unit it
    stops joins ``paused``.

    A tick that reads released starts each unit in ``paused`` once with
    ``systemctl --user start <unit>`` and then clears the set, so the owner's
    release resumes exactly the units the stop paused. ``paused`` is the
    caller's set: ``watch`` keeps one across ticks. A unit that was never
    stopped is never started.
    """
    if callable(last_known_stopped) or isinstance(last_known_stopped, WorkClient):
        actual_status_source = last_known_stopped
        actual_last_known = False
    else:
        actual_status_source = status_source or read_status
        actual_last_known = bool(last_known_stopped)

    runner = run_cmd or command_runner or _default_run_cmd

    try:
        if actual_status_source is not None:
            if callable(actual_status_source):
                raw = actual_status_source()
            else:
                raw = actual_status_source.stop_status()
            if hasattr(raw, "stopped"):
                current_stopped = bool(raw.stopped)
            else:
                current_stopped = bool(raw)
        else:
            current_stopped = actual_last_known
    except Exception:
        # On a failed read keep the last known state (stays enforcing).
        current_stopped = actual_last_known

    if current_stopped:
        stopped_units = paused if paused is not None else set()
        for unit in units:
            res = runner(["systemctl", "--user", "is-active", unit])
            rc = res.returncode if hasattr(res, "returncode") else res
            if rc == 0:
                runner(["systemctl", "--user", "stop", unit])
                stopped_units.add(unit)
        return current_stopped

    if paused:
        for unit in sorted(paused):
            runner(["systemctl", "--user", "start", unit])
        paused.clear()

    return current_stopped


tick = watch_tick


def watch(
    client: WorkClient,
    units: Sequence[str],
    interval: float = 5.0,
    *,
    max_ticks: int | None = None,
    stop_event: Any = None,
    run_cmd: Callable[[list[str]], Any] | None = None,
) -> None:
    """Periodically tick the stop watcher against the provided WorkClient."""
    last_known_stopped = False
    paused: set[str] = set()
    ticks = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if max_ticks is not None and ticks >= max_ticks:
            break
        last_known_stopped = watch_tick(
            units,
            last_known_stopped,
            status_source=client,
            run_cmd=run_cmd,
            paused=paused,
        )
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval)


GUARD_DROP_IN_NAME = "50-omp-agent-stop.conf"
KILLMODE_OVERRIDE_DROP_IN_NAME = "zz-omp-agent-stop-killmode.conf"
KILLMODE_OVERRIDE_CONTENT = "[Service]\nKillMode=control-group\n"
_GUARD_DROP_IN_NAMES = {GUARD_DROP_IN_NAME, KILLMODE_OVERRIDE_DROP_IN_NAME}


def _parse_service_kill_mode_assignments(path: Path) -> list[str]:
    """Return all KillMode assignment values in [Service] sections in path.

    An empty string represents a reset (KillMode=). Lines outside [Service],
    comments, and other directives are ignored.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    in_service = False
    assignments: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            in_service = section_name == "Service"
            continue
        if in_service and "=" in line:
            key, _, val = line.partition("=")
            if key.strip() == "KillMode":
                assignments.append(val.strip().strip("\"'"))
    return assignments


def _inspect_unit_kill_mode(
    unit: str,
    target_dir: Path,
) -> tuple[str | None, Path | None]:
    """Inspect unit file and drop-ins for unit in target_dir to find effective KillMode.

    Returns (effective_kill_mode, effective_source_file).
    If a drop-in sorting after zz-omp-agent-stop-killmode.conf sets KillMode in
    [Service], raises ValueError naming the unit and the drop-in file.
    """
    effective_kill_mode: str | None = None
    effective_source: Path | None = None

    unit_file = target_dir / unit
    if unit_file.is_file():
        for val in _parse_service_kill_mode_assignments(unit_file):
            if not val:
                effective_kill_mode = None
                effective_source = None
            else:
                effective_kill_mode = val
                effective_source = unit_file

    drop_in_dir = target_dir / f"{unit}.d"
    if drop_in_dir.is_dir():
        drop_ins = sorted(
            [p for p in drop_in_dir.glob("*.conf") if p.is_file()],
            key=lambda p: p.name,
        )
        for conf_file in drop_ins:
            if conf_file.name in _GUARD_DROP_IN_NAMES:
                continue
            if conf_file.name > KILLMODE_OVERRIDE_DROP_IN_NAME:
                assignments = _parse_service_kill_mode_assignments(conf_file)
                if assignments:
                    raise ValueError(
                        f"{unit}: drop-in {conf_file.name} sorts after "
                        f"{KILLMODE_OVERRIDE_DROP_IN_NAME} and sets KillMode"
                    )
            else:
                for val in _parse_service_kill_mode_assignments(conf_file):
                    if not val:
                        effective_kill_mode = None
                        effective_source = None
                    else:
                        effective_kill_mode = val
                        effective_source = conf_file

    return effective_kill_mode, effective_source


def default_systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _validate_target_units(units: Sequence[str]) -> None:
    if not units:
        raise ValueError("at least one target unit is required")
    for unit in units:
        if (
            unit in {"omp-agent-stop.service", "omp-agent-stop"}
            or unit.startswith("omp-work-")
        ):
            raise ValueError(f"refusing target unit: {unit}")


def install_guards(
    units: Sequence[str],
    systemd_dir: Path | str | None = None,
    interval: float | int = 5,
) -> list[Path]:
    """Install systemd drop-in guards and watcher service.

    Writes per-unit drop-in files DIR/<U>.d/50-omp-agent-stop.conf with:
    [Service]
    ExecCondition=<sys.executable> -m omp_work stop check
    TimeoutStopSec=20

    For each unit, checks the effective KillMode across DIR/<U> and
    DIR/<U>.d/*.conf. If KillMode is 'process' or 'none', writes
    DIR/<U>.d/zz-omp-agent-stop-killmode.conf with:
    [Service]
    KillMode=control-group

    and prints an override message to stdout. Raises ValueError before writing
    any files if any drop-in sorting after zz-omp-agent-stop-killmode.conf sets
    KillMode.

    And writes DIR/omp-agent-stop.service with:
    [Unit]
    Description=OMP agent stop watcher

    [Service]
    ExecStart=<sys.executable> -m omp_work stop watch --unit <U> ... --interval <interval>
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=default.target

    Refuses omp-agent-stop.service and omp-work-* as targets.
    Prints the daemon-reload/enable commands for the owner.
    """
    _validate_target_units(units)
    target_dir = Path(systemd_dir) if systemd_dir is not None else default_systemd_dir()
    unique_units = list(dict.fromkeys(units))

    unit_overrides: dict[str, tuple[str, Path]] = {}
    for unit in unique_units:
        mode, source = _inspect_unit_kill_mode(unit, target_dir)
        if mode in {"process", "none"}:
            assert source is not None
            unit_overrides[unit] = (mode, source)

    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    drop_in_content = (
        "[Service]\n"
        f"ExecCondition={sys.executable} -m omp_work stop check\n"
        "TimeoutStopSec=20\n"
    )
    for unit in unique_units:
        drop_in_dir = target_dir / f"{unit}.d"
        drop_in_dir.mkdir(parents=True, exist_ok=True)
        conf_path = drop_in_dir / GUARD_DROP_IN_NAME
        conf_path.write_text(drop_in_content, encoding="utf-8")
        written.append(conf_path)

        if unit in unit_overrides:
            mode, source = unit_overrides[unit]
            override_path = drop_in_dir / KILLMODE_OVERRIDE_DROP_IN_NAME
            override_path.write_text(KILLMODE_OVERRIDE_CONTENT, encoding="utf-8")
            written.append(override_path)
            print(
                f"stop: {unit}: overriding KillMode={mode} from {source.name} with KillMode=control-group"
            )

    unit_args = " ".join(f"--unit {u}" for u in unique_units)
    interval_str = (
        str(int(interval))
        if isinstance(interval, (int, float)) and interval == int(interval)
        else str(interval)
    )
    watcher_content = (
        "[Unit]\n"
        "Description=OMP agent stop watcher\n\n"
        "[Service]\n"
        f"ExecStart={sys.executable} -m omp_work stop watch {unit_args} --interval {interval_str}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    watcher_path = target_dir / "omp-agent-stop.service"
    watcher_path.write_text(watcher_content, encoding="utf-8")
    written.append(watcher_path)

    print("systemctl --user daemon-reload")
    print("systemctl --user enable --now omp-agent-stop.service")

    return written

