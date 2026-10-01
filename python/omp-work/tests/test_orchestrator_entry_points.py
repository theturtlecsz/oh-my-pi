"""OMP-417: every orchestrator entry point carried in the disposition doc.

The doc is the D35 unattended-safety envelope a reviewer reads: each systemd
unit the installer renders must appear with a disposition, and a unit or task
kind the doc omits fails the check so the envelope cannot silently lag the code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_DOC = Path(__file__).resolve().parents[3] / "docs/orchestrator-entry-points.md"
_INSTALL = Path(__file__).resolve().parents[3] / "infra/work-ledger/install.sh"

_UNIT_RE = re.compile(r"`([a-z0-9-]+\.(?:service|timer))`")


def _render_units(tmp_path: Path) -> set[str]:
    units = tmp_path / "units"
    result = subprocess.run(
        [
            "bash",
            str(_INSTALL),
            "--render-only",
            "--python",
            "/candidate/python",
            "--unit-dir",
            str(units),
        ],
        env={
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {path.name for path in units.iterdir()}


def _assert_units_documented(doc: str, units: set[str]) -> None:
    missing = sorted(units - set(_UNIT_RE.findall(doc)))
    assert not missing, f"units missing from {_DOC.name}: {missing}"


def test_each_rendered_unit_appears_with_a_disposition(tmp_path) -> None:
    units = _render_units(tmp_path)
    assert units
    _assert_units_documented(_DOC.read_text(), units)
    for unit in units:
        line = next(
            row for row in _DOC.read_text().splitlines() if f"`{unit}`" in row
        )
        assert line.split("|")[-2].strip(" `") in {
            "refused_unattended",
            "control_plane",
            "control_plane_op",
        }


def test_an_unlisted_unit_fails_the_check() -> None:
    with pytest.raises(AssertionError):
        _assert_units_documented(_DOC.read_text(), {"omp-work-not-a-unit.service"})
