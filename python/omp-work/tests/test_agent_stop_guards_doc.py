"""OMP-537-s02: the agent-stop guard section of the operations doc stays whole.

Failure mode defended: a reader following ``## Agent stop guards`` in
``docs/work-ledger-operations.md`` runs a command whose path or flag the parser
no longer defines, or loses the section, its ``### continuous-admit`` record, or
the disposition line that hands the decision to OMP-537-s04. The test parses
every fenced ``sh`` line that invokes ``python -m omp_work``, replays its
command path through ``omp_work.__main__.main`` with ``--help``, and requires
the help text to name each ``--flag`` on the line.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from omp_work.__main__ import main

_DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "work-ledger-operations.md"
)

_SECTION_HEADING = "## Agent stop guards"
_SUBSECTION_HEADING = "### continuous-admit"
_LAST_LINE = (
    "Keep or remove: decided in OMP-537-s04, recorded on OMP-537 in the Work Ledger."
)

_CLI_PREFIX = "python -m omp_work "
_FENCE = re.compile(r"^```(.*)$")


def _section(text: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(_SECTION_HEADING)
    except ValueError:
        pytest.fail(f"{_DOC} has no {_SECTION_HEADING!r} section")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return lines[start:end]


def _fenced_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    info: str | None = None
    body: list[str] = []
    for line in lines:
        fence = _FENCE.match(line)
        if fence is not None:
            if info is None:
                info = fence.group(1).strip()
                body = []
            else:
                blocks.append((info, body))
                info = None
            continue
        if info is not None:
            body.append(line)
    return blocks


def _cli_lines(lines: list[str]) -> list[tuple[list[str], list[str]]]:
    parsed: list[tuple[list[str], list[str]]] = []
    for info, body in _fenced_blocks(lines):
        if info != "sh":
            continue
        for line in body:
            stripped = line.strip()
            if not stripped.startswith(_CLI_PREFIX):
                continue
            tokens = stripped[len(_CLI_PREFIX) :].split()
            path: list[str] = []
            for token in tokens:
                if token.startswith("-") or token.startswith("<"):
                    break
                path.append(token)
            flags = [token for token in tokens if token.startswith("--")]
            parsed.append((path, flags))
    return parsed


def _help_for(path: list[str]) -> str:
    capture = io.StringIO()
    with pytest.raises(SystemExit) as exited:
        with contextlib.redirect_stdout(capture):
            main([*path, "--help"])
    assert exited.value.code == 0, f"{' '.join(path)} --help exited {exited.value.code}"
    return capture.getvalue()


def test_agent_stop_guards_section_and_cli_block_are_present_and_live() -> None:
    lines = _section(_DOC.read_text(encoding="utf-8"))
    assert _SUBSECTION_HEADING in lines, (
        f"{_DOC}: {_SUBSECTION_HEADING!r} record is missing"
    )

    content = [line for line in lines if line.strip()]
    assert content[-1] == _LAST_LINE, (
        f"{_DOC}: {_SECTION_HEADING!r} must end with the OMP-537-s04 disposition line"
    )

    cli_lines = _cli_lines(lines)
    assert cli_lines, (
        f"{_DOC}: {_SECTION_HEADING!r} declares no `python -m omp_work` sh command"
    )
    for path, flags in cli_lines:
        assert path, "an omp_work line has no command path before its first flag"
        help_text = _help_for(path)
        for flag in flags:
            assert flag in help_text, (
                f"{_DOC}: {flag} is not named by "
                f"`omp-work {' '.join(path)} --help`"
            )
