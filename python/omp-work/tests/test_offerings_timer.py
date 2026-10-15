"""Installed offerings timer: the unit's decoded ExecStart must run the check."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from omp_work.__main__ import main
from omp_work.operations.offerings_timer import install_timer

from test_offerings_evaluation_cli import (
    install_stub,
    now,
    records_of,
    tree_hashes,
    write_captures,
)


def _decode(exec_start: str) -> list[str]:
    """systemd word splitting: shell-like quotes, then `$$`/`%%` collapse per word."""
    return [word.replace("$$", "$").replace("%%", "%") for word in shlex.split(exec_start)]


def _exec_start(unit: Path) -> str:
    (line,) = [l for l in unit.read_text().splitlines() if l.startswith("ExecStart=")]
    return line[len("ExecStart="):]


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides=None):
    root = tmp_path / "we ird $dir %h"
    captures = write_captures(root / "captures", checked_at=now(), overrides=overrides)
    stub = install_stub(monkeypatch, root, captures)
    units = root / "units"
    records = root / "records"
    written = install_timer(captures.sources, records, stub, units)
    return captures, records, written


def test_unit_escapes_and_decoded_execstart_runs_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    captures, records, (service, timer) = _install(tmp_path, monkeypatch)
    text = service.read_text()
    assert "$$" in text and "%%" in text
    assert "UMask=0077" in text and "SuccessExitStatus=3" in text and "Type=oneshot" in text
    assert "daemon-reload" in capsys.readouterr().out
    before = tree_hashes(captures.sources.parent)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(_decode(_exec_start(service)), cwd=elsewhere, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert len(records_of(records)) == 1
    assert tree_hashes(captures.sources.parent) == before


def test_timer_unit_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, _, (_, timer) = _install(tmp_path, monkeypatch)
    lines = timer.read_text().splitlines()
    for line in (
        "OnCalendar=daily",
        "Persistent=true",
        "Unit=omp-offerings-check.service",
        "WantedBy=timers.target",
    ):
        assert line in lines


def test_urgent_finding_exits_three_through_the_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, records, (service, _) = _install(
        tmp_path, monkeypatch, overrides={"gemini_flash": ["gemini-2.0-flash"]}
    )
    done = subprocess.run(_decode(_exec_start(service)), cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 3, done.stderr
    assert len(records_of(records)) == 1


def _refused(tmp_path: Path, sources: Path, omp: str, match: str):
    units = tmp_path / "units"
    with pytest.raises(ValueError, match=match):
        install_timer(sources, tmp_path / "r", omp, units)
    assert not units.exists() or not list(units.iterdir())


def test_relative_in_source_paths_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captures = write_captures(tmp_path / "c", checked_at=now())
    stub = install_stub(monkeypatch, tmp_path, captures)
    doc = json.loads(captures.sources.read_text())

    doc["notices"] = "notices.json"
    captures.sources.write_text(json.dumps(doc))
    _refused(tmp_path, captures.sources, stub, "notices")

    captures = write_captures(tmp_path / "c", checked_at=now())
    doc = json.loads(captures.sources.read_text())
    doc["lanes"]["kimi"]["path"] = "listing-kimi.json"
    captures.sources.write_text(json.dumps(doc))
    _refused(tmp_path, captures.sources, stub, r"lanes\.kimi\.path")


def test_missing_sources_and_unfindable_omp_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captures = write_captures(tmp_path / "c", checked_at=now())
    stub = install_stub(monkeypatch, tmp_path, captures)
    _refused(tmp_path, tmp_path / "nope.json", stub, "sources")
    _refused(tmp_path, captures.sources, str(tmp_path / "no-such-omp"), "omp")


@pytest.mark.parametrize("custom_records", [False, True])
def test_cli_installs_units_and_prints_activation_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    custom_records: bool,
):
    captures = write_captures(tmp_path / "c", checked_at=now())
    stub = install_stub(monkeypatch, tmp_path, captures)
    units = tmp_path / "units"
    args = [
        "offerings", "install-timer", "--sources", str(captures.sources),
        "--omp", stub, "--systemd-dir", str(units),
    ]
    records = tmp_path / "records"
    if custom_records:
        args.extend(["--record-dir", str(records)])

    assert main(args) == 0

    service = units / "omp-offerings-check.service"
    assert service.is_file()
    assert (units / "omp-offerings-check.timer").is_file()
    if custom_records:
        words = _decode(_exec_start(service))
        assert words[words.index("--record-dir") + 1] == str(records)
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.splitlines() == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now omp-offerings-check.timer",
    ]


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [("relative", "lanes.kimi.path"), ("missing", "sources"),
     ("malformed", "not valid JSON"), ("omp", "omp")],
)
def test_cli_refuses_invalid_install_without_units_or_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    failure: str, diagnostic: str,
):
    captures = write_captures(tmp_path / "c", checked_at=now())
    stub = install_stub(monkeypatch, tmp_path, captures)
    sources = captures.sources
    if failure == "relative":
        doc = json.loads(sources.read_text())
        doc["lanes"]["kimi"]["path"] = "listing-kimi.json"
        sources.write_text(json.dumps(doc))
    elif failure == "missing":
        sources = tmp_path / "missing.json"
    elif failure == "malformed":
        sources.write_text("{")
    else:
        stub = str(tmp_path / "no-such-omp")
    units = tmp_path / "units"

    assert main([
        "offerings", "install-timer", "--sources", str(sources),
        "--omp", stub, "--systemd-dir", str(units),
    ]) == 2

    assert not units.exists() or not list(units.iterdir())
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("offerings install-timer: ")
    assert diagnostic in output.err


@pytest.mark.parametrize(
    ("args", "options"),
    [(["offerings", "install-timer", "--help"],
      ["--sources", "--record-dir", "--omp", "--systemd-dir"]),
     (["offerings", "--help"], ["install-timer"])],
)
def test_cli_help_lists_timer_command_and_options(
    args: list[str], options: list[str], capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    for option in options:
        assert option in output.out
