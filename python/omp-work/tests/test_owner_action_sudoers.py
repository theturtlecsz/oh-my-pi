"""Contract for the single sudoers rule that lets ompbot run the owner-action gateway.

The rule grants the restricted automation user exactly one command, run as the
owner and nobody else. A regression that broadens the run-as list (root/ALL), the
command list (a second command, a wildcard, or trailing arguments), or the file
mode would hand the automation user more authority than the gateway it is meant
to reach.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SUDOERS = REPO / "infra" / "automation-user" / "omp-owner-action.sudoers"
RELATIVE = "infra/automation-user/omp-owner-action.sudoers"

USER = "ompbot"
RUNAS = "thetu"
COMMAND = "/usr/local/libexec/omp-owner-action"
LINE = f"{USER} ALL=({RUNAS}) NOPASSWD: {COMMAND}"

# user  host=(runas) TAG: command
RULE = re.compile(
    r"^(?P<user>\S+)\s+(?P<host>\S+)\s*=\s*\((?P<runas>[^)]*)\)\s*(?P<tag>\S+)\s*:\s*(?P<command>.*)$"
)


def rule_line() -> str:
    assert SUDOERS.is_file(), f"missing {RELATIVE}"
    lines = [line.strip() for line in SUDOERS.read_text().splitlines() if line.strip()]
    assert lines == [LINE], f"sudoers must hold exactly one rule line: {lines!r}"
    return lines[0]


def parse(line: str) -> re.Match[str]:
    match = RULE.match(line)
    assert match is not None, f"unparsable sudoers line: {line!r}"
    return match


def test_sudoers_grants_only_the_gateway_as_thetu():
    match = parse(rule_line())
    assert match["user"] == USER
    # The run-as list is exactly the owner: never root, never ALL, never a list.
    assert match["runas"] == RUNAS
    assert "ALL" not in match["runas"]
    assert "root" not in match["runas"]
    # The command list is exactly the gateway path: no arguments, wildcard,
    # comma, or second command.
    assert match["command"] == COMMAND
    assert "," not in match["command"]
    assert "*" not in match["command"]
    assert match["command"].split() == [COMMAND]


def test_sudoers_has_no_comments_or_extra_lines():
    text = SUDOERS.read_text()
    assert not any(line.lstrip().startswith("#") for line in text.splitlines())
    assert text.splitlines()[-1] == LINE


def test_sudoers_mode_is_644_in_git_and_on_disk():
    assert stat.S_IMODE(SUDOERS.stat().st_mode) == 0o644
    listed = subprocess.run(
        ["git", "ls-files", "-s", "--", RELATIVE],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert listed.returncode == 0, listed.stderr
    fields = listed.stdout.split()
    if not fields:
        # Untracked (test run before the file is staged); the on-disk 0644 above
        # is what git records as 100644 once added.
        return
    assert fields[0] == "100644", listed.stdout


def test_sudoers_accepted_by_visudo():
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not installed")
    checked = subprocess.run([visudo, "-cf", str(SUDOERS)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
