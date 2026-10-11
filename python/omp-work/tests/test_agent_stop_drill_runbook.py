"""OMP-447: the agent-stop drill runbook can reach the WebUI Stop control.

Failure mode defended: the operator procedure forbids minting a client
capability, so the WebUI has no bearer. ``docs/webui-oversight.md`` requires
``ops capabilities client`` (scopes ``work.read``, ``work.client``, and
``work.stop``) and forbids the owner bearer for the daemon. ``--stop-only``
drops the two read scopes the panel needs. A Grok Bot credential
(``ops capabilities grokbot``, a capability named grokbot, or a grokbot bearer
on Stop or release) is outside this drill.

The test replays every fenced ``sh`` line that starts
``uv run --project python/omp-work omp-work`` through
``omp_work.__main__.main`` with ``--help``, and requires that help to name
each ``--flag`` on the line.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from omp_work.__main__ import main

_RUNBOOK = (
    Path(__file__).resolve().parents[3] / "docs" / "runbooks" / "agent-stop-drill.md"
)

_CLI_PREFIX = "uv run --project python/omp-work omp-work "
_FENCE = re.compile(r"^```(.*)$")

_CLIENT_SCOPES = ("work.client", "work.read", "work.stop")


def _fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    info: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence is not None:
            if info is None:
                info = fence.group(1).strip()
                lines = []
            else:
                blocks.append((info, lines))
                info = None
            continue
        if info is not None:
            lines.append(line)
    return blocks


def _sh_lines(text: str) -> list[str]:
    lines: list[str] = []
    for info, body in _fenced_blocks(text):
        if info != "sh":
            continue
        lines.extend(line.strip() for line in body if line.strip())
    return lines


def _cli_lines(text: str) -> list[tuple[str, list[str], list[str]]]:
    parsed: list[tuple[str, list[str], list[str]]] = []
    for line in _sh_lines(text):
        if not line.startswith(_CLI_PREFIX):
            continue
        tokens = line[len(_CLI_PREFIX) :].split()
        path: list[str] = []
        for token in tokens:
            if token.startswith("-") or token.startswith("<"):
                break
            path.append(token)
        flags = [token for token in tokens if token.startswith("--")]
        parsed.append((line, path, flags))
    return parsed


def _help_for(path: list[str]) -> str:
    capture = io.StringIO()
    with pytest.raises(SystemExit) as exited:
        with contextlib.redirect_stdout(capture):
            main([*path, "--help"])
    assert exited.value.code == 0, f"{' '.join(path)} --help exited {exited.value.code}"
    return capture.getvalue()


def test_runbook_cli_lines_match_live_help() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    lines = _cli_lines(text)
    assert lines, f"{_RUNBOOK} declares no omp-work sh commands"
    for line, path, flags in lines:
        assert path, f"no command path before the first flag: {line}"
        help_text = _help_for(path)
        for flag in flags:
            assert flag in help_text, (
                f"{flag} is not named by `omp-work {' '.join(path)} --help` ({line})"
            )


def test_webui_client_is_minted_and_grokbot_is_not_a_command() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    client_lines = [
        line
        for line, path, _flags in _cli_lines(text)
        if path == ["ops", "capabilities", "client"]
    ]
    assert client_lines == [
        "uv run --project python/omp-work omp-work ops capabilities client "
        "--name client --workspace-id \"$W\""
    ]
    assert "--stop-only" not in client_lines[0]

    for line in _sh_lines(text):
        assert "grokbot" not in line, f"fenced command names grokbot: {line}"

    assert "ops capabilities grokbot" in text
    assert "do not mint a client capability" not in text
    assert "OMP_WORK_CAPABILITY_FILE" in text
    for scope in _CLIENT_SCOPES:
        assert scope in text
    assert "docs/webui-oversight.md" in text
    assert "clientEngageStop" in text


def test_release_uses_the_owner_bearer_from_client_json() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    release_lines = [
        line
        for line, path, _flags in _cli_lines(text)
        if path == ["stop", "release"]
    ]
    assert release_lines == [
        "uv run --project python/omp-work omp-work stop release --reason drill-over"
    ]
    assert "--bearer-file" not in release_lines[0]

    init_lines = [
        line
        for line, path, flags in _cli_lines(text)
        if path == ["ops", "capabilities", "init"]
    ]
    assert len(init_lines) == 1
    assert "--workspace-id" in init_lines[0]
    assert "--owner-id" in init_lines[0]


def test_drill_checks_cover_the_units_cgroup_prompt_and_restarts() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "flood.service",
        "flood-operator.service",
        "continuous-admit.service",
        "media-watch-agent.service",
        "OMP-527",
        "50-omp-agent-stop.conf",
        "zz-omp-agent-stop-killmode.conf",
        "KillMode=process",
        "cgroup.procs",
        "ExecCondition",
        "omp -p 'say ready'",
        "Agent stop engaged",
        "NRestarts",
        "mark-done OMP-405-s08",
        "omp-447-drill-",
    ):
        assert needle in text, f"runbook omits {needle!r}"

    evidence = [
        lines
        for info, lines in _fenced_blocks(text)
        if info == "text evidence"
    ]
    assert len(evidence) == 1
    body = "\n".join(evidence[0])
    for label in (
        "Date (UTC):",
        "Host:",
        "Deploy sha:",
        "Role:",
        "WebUI sha:",
        "Client capability path:",
        "Client scopes:",
        "Steps run:",
        "Observed:",
        "Result (PASS/FAIL):",
        "Follow-up items:",
        "Drill file:",
    ):
        assert label in body, f"evidence block omits {label!r}"
