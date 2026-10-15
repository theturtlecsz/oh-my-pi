"""Installed offerings timer: the unit's decoded ExecStart must run the check."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

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
