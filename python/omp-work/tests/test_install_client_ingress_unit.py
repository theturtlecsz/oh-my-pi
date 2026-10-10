"""OMP-490-s03: the installer renders the loopback client ingress service.

The work-ledger installer renders omp-work-client-ingress.service for the
loopback client ingress gate (OMP-490). The rendered service must invoke the
installed Python with `ingress serve`, the quoted spaced config path, the
configured ingress port, and the upstream URL derived from the HTTP port.
The unit must require and order after omp-work-service.service, define an
[Install] section with WantedBy=default.target, carry no EnvironmentFile,
and contain no "grok" reference.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

import omp_work.__main__ as omp_work_main

INSTALL_SCRIPT = (
    Path(__file__).resolve().parents[3] / "infra" / "work-ledger" / "install.sh"
)


def _render(
    tmp_path: Path,
    ingress_port: int | str | None = None,
    http_port: int | str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Render units with an argv-printing fake python and a spaced XDG config."""
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir(parents=True, exist_ok=True)
    fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_python.chmod(0o755)
    config = tmp_path / "xdg config with space"
    units = tmp_path / "review-units"
    cmd = [
        "bash",
        str(INSTALL_SCRIPT),
        "--render-only",
        "--python",
        str(fake_python),
        "--unit-dir",
        str(units),
    ]
    if ingress_port is not None:
        cmd.extend(["--ingress-port", str(ingress_port)])
    if http_port is not None:
        cmd.extend(["--http-port", str(http_port)])
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return units, config


def test_client_ingress_service_exec_start_splits_and_matches_custom_ports(
    tmp_path: Path,
) -> None:
    """ExecStart decodes to the installed python running ingress serve with given ports."""
    units, config = _render(tmp_path, ingress_port=54333, http_port=54322)
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-work-client-ingress.service").read_text().splitlines()
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
        "ingress",
        "serve",
        "--config",
        str(config / "omp" / "work-ledger" / "client-ingress.json"),
        "--port",
        "54333",
        "--upstream",
        "http://127.0.0.1:54322",
    ]


def test_client_ingress_service_exec_start_defaults(tmp_path: Path) -> None:
    """ExecStart uses default ports 54323 and 54322 when arguments are omitted."""
    units, config = _render(tmp_path)
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-work-client-ingress.service").read_text().splitlines()
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
        "ingress",
        "serve",
        "--config",
        str(config / "omp" / "work-ledger" / "client-ingress.json"),
        "--port",
        "54323",
        "--upstream",
        "http://127.0.0.1:54322",
    ]


def test_client_ingress_service_honors_env_port(tmp_path: Path) -> None:
    """OMP_WORK_INGRESS_PORT environment variable controls the default ingress port."""
    units, config = _render(
        tmp_path, extra_env={"OMP_WORK_INGRESS_PORT": "54399"}
    )
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-work-client-ingress.service").read_text().splitlines()
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
        "ingress",
        "serve",
        "--config",
        str(config / "omp" / "work-ledger" / "client-ingress.json"),
        "--port",
        "54399",
        "--upstream",
        "http://127.0.0.1:54322",
    ]


def test_client_ingress_argv_is_accepted_by_argparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered ExecStart argv parses through omp_work.__main__ as ingress serve."""
    units, config = _render(tmp_path, ingress_port=54345, http_port=54325)
    command = next(
        line.removeprefix("ExecStart=")
        for line in (units / "omp-work-client-ingress.service").read_text().splitlines()
        if line.startswith("ExecStart=")
    )
    probe = subprocess.run(
        shlex.split(command.replace("%%", "%")), capture_output=True, text=True
    )
    assert probe.returncode == 0, probe.stderr
    argv = probe.stdout.splitlines()
    cli_argv = argv[argv.index("omp_work") + 1 :]

    captured_args: list[object] = []

    def fake_ingress_run(args: object) -> int:
        captured_args.append(args)
        return 0

    monkeypatch.setattr(omp_work_main.client_ingress, "run", fake_ingress_run)
    assert omp_work_main.main(cli_argv) == 0
    assert len(captured_args) == 1
    args: object = captured_args[0]
    assert getattr(args, "command") == "ingress"
    assert getattr(args, "ingress_command") == "serve"
    assert getattr(args, "config") == config / "omp" / "work-ledger" / "client-ingress.json"
    assert getattr(args, "port") == 54345
    assert getattr(args, "upstream") == "http://127.0.0.1:54325"


def test_client_ingress_unit_dependencies_and_install_target(tmp_path: Path) -> None:
    """The unit Requires/After omp-work-service.service and installs to default.target."""
    units, _config = _render(tmp_path)
    content = (units / "omp-work-client-ingress.service").read_text()
    lines = [line.strip() for line in content.splitlines()]

    assert "Requires=omp-work-service.service" in lines
    assert "After=omp-work-service.service" in lines
    assert "[Install]" in lines
    assert "WantedBy=default.target" in lines
    assert "Restart=on-failure" in lines


def test_client_ingress_has_no_environment_file_and_no_grok(tmp_path: Path) -> None:
    """The unit has no EnvironmentFile and no occurrence of 'grok' (any case)."""
    units, _config = _render(tmp_path)
    rendered = {path.name: path.read_text() for path in units.iterdir()}
    assert "omp-work-client-ingress.service" in rendered

    ingress_content = rendered["omp-work-client-ingress.service"]
    lowered = ingress_content.lower()
    assert "environmentfile" not in lowered
    assert "grok" not in lowered

    for name, content in rendered.items():
        assert "grok" not in content.lower(), f"{name} mentions 'grok'"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--ingress-port", "abc"),
        ("--ingress-port", "543:23"),
        ("--http-port", "543:22"),
        ("--postgres-port", "543:21"),
    ],
)
def test_non_numeric_port_exits_2(tmp_path: Path, option: str, value: str) -> None:
    """Reject nonnumeric ports before the installer writes unusable units."""
    units = tmp_path / "review-units"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            "--render-only",
            "--python",
            "/candidate/python",
            "--unit-dir",
            str(units),
            option,
            value,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "ports must be numeric" in result.stderr
    assert not units.exists()
