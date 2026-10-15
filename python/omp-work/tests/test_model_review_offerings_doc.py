"""OMP-615-s09-s05-s03: the ``## Daily offerings check`` section of the review doc.

Failure mode defended: ``docs/model-review.md`` is the owner's written schedule
and the rubric path the governing policy and every evaluation record name. If
the section's install line drifts from the CLI — a renamed flag, a required
argument the line omits, a default that no longer points at
``default_systemd_dir()`` or ``default_record_dir()`` — a reader following the
doc installs nothing or installs a check whose records land somewhere the
policy's stated path does not name, while the doc still claims the schedule is
installed. These tests replay the doc's ``sh`` block through the real
``omp_work.__main__.main`` with a real stub ``omp`` on ``PATH`` and the
doc's own ``HOME``/XDG defaults, then run the unit's decoded ``ExecStart`` and
assert the record lands directly in ``default_record_dir()``.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from omp_work.__main__ import main
from omp_work.operations.stop import default_systemd_dir
from omp_work.routing.offerings_evaluation import default_record_dir

from test_offerings_evaluation_cli import install_stub, now, records_of, write_captures

_REPO = Path(__file__).resolve().parents[3]
_DOC = _REPO / "docs" / "model-review.md"

_SECTION = "## Daily offerings check"
_ANCHOR = "#daily-offerings-check"
_SCHEDULE = "## Owner schedule"
# The two facts that must survive the pointer's insertion: the qualification
# gate and the recorded last review.
_QUALIFICATION_GATE = "accepts a `baseline` entry only for the exact frozen recipe"
_LAST_REVIEW = "Current last review: 2026-10-14, `baseline_retained`"
_OLD_SENTENCE = "No scheduler or live provider runner is installed by this policy."

_INSTALL_PREFIX = "omp-work "
_SYSTEMCTL = [
    "systemctl --user daemon-reload",
    "systemctl --user enable --now omp-offerings-check.timer",
]

_SERVICE = "omp-offerings-check.service"
_TIMER = "omp-offerings-check.timer"


def _section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        pytest.fail(f"{_DOC}: {heading!r} section is missing")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return lines[start:end]


def _sh_block(lines: list[str]) -> list[str]:
    """The single fenced ``sh`` block's non-empty lines."""
    try:
        start = lines.index("```sh")
    except ValueError:
        pytest.fail(f"{_DOC}: {_SECTION!r} declares no ```sh block")
    try:
        end = lines.index("```", start + 1)
    except ValueError:
        pytest.fail(f"{_DOC}: the ```sh block is unterminated")
    return [line for line in lines[start + 1 : end] if line.strip()]


def _decode(exec_start: str) -> list[str]:
    """systemd word splitting: shell-like quotes, then `$$`/`%%` collapse per word."""
    return [word.replace("$$", "$").replace("%%", "%") for word in shlex.split(exec_start)]


def _exec_start(unit: Path) -> str:
    (line,) = [l for l in unit.read_text().splitlines() if l.startswith("ExecStart=")]
    return line[len("ExecStart="):]


def _doc_install_argv(block: list[str]) -> list[str]:
    """The doc's install line, expanded against the test environment, argv sans prog."""
    (line,) = [ln for ln in block if ln.startswith(_INSTALL_PREFIX)]
    tokens = shlex.split(os.path.expandvars(line))
    assert tokens[0] == _INSTALL_PREFIX.strip(), f"{_DOC}: install line does not call omp-work"
    return tokens[1:]


def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME and every XDG base under tmp_path, and a PATH whose only `omp` is the stub."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    offerings = home / "offerings"
    captures = write_captures(offerings, checked_at=now())
    stub = install_stub(monkeypatch, offerings, captures)

    # install_timer resolves the CLI name through shutil.which, so PATH must hold
    # an executable actually named `omp`; the interpreter directory stays on PATH
    # for the stub's own `#!/usr/bin/env python3` shebang.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    omp = bin_dir / "omp"
    omp.write_bytes(Path(stub).read_bytes())
    omp.chmod(omp.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)]))
    return home


def test_doc_install_line_installs_the_daily_check_at_the_documented_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    home = _isolated_env(tmp_path, monkeypatch)
    block = _sh_block(_section(_DOC.read_text(encoding="utf-8"), _SECTION))
    assert block[0] == (
        'omp-work offerings install-timer --sources "$HOME/offerings/offerings-sources.json"'
    )
    assert block[1:] == _SYSTEMCTL

    assert main(_doc_install_argv(block)) == 0

    printed = capsys.readouterr()
    assert printed.err == ""
    assert printed.out.splitlines() == _SYSTEMCTL

    units = default_systemd_dir()
    assert units == tmp_path / "config" / "systemd" / "user"
    service = units / _SERVICE
    assert service.is_file() and (units / _TIMER).is_file()
    words = _decode(_exec_start(service))
    assert words[words.index("--sources") + 1] == str(home / "offerings" / "offerings-sources.json")
    assert words[words.index("--record-dir") + 1] == str(default_record_dir())
    assert default_record_dir() == tmp_path / "state" / "omp" / "work-ledger" / "offerings"


def test_the_installed_unit_records_directly_in_the_default_record_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_env(tmp_path, monkeypatch)
    block = _sh_block(_section(_DOC.read_text(encoding="utf-8"), _SECTION))
    assert main(_doc_install_argv(block)) == 0

    records = default_record_dir()
    assert not records.exists()
    service = default_systemd_dir() / _SERVICE

    done = subprocess.run(_decode(_exec_start(service)), cwd=tmp_path, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert len(records_of(records)) == 1
    assert not [child for child in records.iterdir() if child.is_dir()]


def test_owner_schedule_points_at_the_daily_check_without_the_old_refusal():
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    schedule = _section(_DOC.read_text(encoding="utf-8"), _SCHEDULE)

    assert _OLD_SENTENCE not in lines
    assert f"[Daily offerings check]({_ANCHOR})" in "\n".join(schedule)
    # The pointer sits in the schedule but the new section follows the schedule.
    assert lines.index(_SECTION) > lines.index(_SCHEDULE)

    # The pointer must not have displaced the qualification gate or the recorded
    # last review the governing policy's check reads.
    text = "\n".join(lines)
    assert _QUALIFICATION_GATE in text
    assert _LAST_REVIEW in text
