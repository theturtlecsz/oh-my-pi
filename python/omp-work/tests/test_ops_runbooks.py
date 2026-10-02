"""OMP-519-s03: the credential-rotation drill runbook must stay executable.

Failure mode defended: a runbook whose command blocks drift from the CLI they
claim to use. A reader following
``docs/runbooks/omp-work-credential-rotation-drill.md`` would run a command path
that no longer exists, or pass a flag the parser dropped, and only discover the
drift mid-incident. This parses every fenced ``sh`` line that invokes the
``omp-work`` CLI, replays its command path through ``omp_work.__main__.main``
with ``--help``, and requires the help text to name each flag on the line. It
also requires exactly one ``text evidence`` block carrying every evidence field,
so the drill cannot lose the record it is supposed to leave behind.

The outage drill runbook is on the same list. The command-path check and the
evidence check cover it. Its evidence block also carries ``Outage start:`` and
``Outage end:``. The restart-set, mission-window, and failure-recovery checks
stay on the credential-rotation runbook.

It also defends against operational runbook drift:
- Claiming units outside ``docs/work-ledger-operations.md`` must restart or
  misidentifying the restart set (``OperationsConfig.connection_kwargs`` reads
  secrets per connection; per-transaction connections and Postgres advisory locks
  mean no units must restart for credential pickup).
- Misidentifying the queued mission window (requires ``approved``, not
  ``awaiting_confirmation``) or lacking commands to discover queued missions.
- Inverting failure recovery by overwriting the working password with a rejected
  credential when rotation rolls back.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from omp_work.__main__ import main

_RUNBOOK_DIR = Path(__file__).resolve().parents[3] / "docs" / "runbooks"

RUNBOOKS = (
    _RUNBOOK_DIR / "omp-work-credential-rotation-drill.md",
    _RUNBOOK_DIR / "omp-work-outage-drill.md",
)

_CREDENTIAL_RUNBOOK = "omp-work-credential-rotation-drill.md"
_OUTAGE_RUNBOOK = "omp-work-outage-drill.md"

EVIDENCE_LABELS = (
    "Date (UTC):",
    "Host:",
    "Deploy sha:",
    "Role:",
    "Mission id:",
    "Steps run:",
    "Observed:",
    "Result (PASS/FAIL):",
    "Follow-up items:",
)

_OUTAGE_EVIDENCE_LABELS = (
    "Outage start:",
    "Outage end:",
)

_CLI_PREFIX = "uv run --project python/omp-work omp-work "
_FENCE = re.compile(r"^```(.*)$")


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


def _cli_lines(text: str) -> list[tuple[list[str], list[str]]]:
    """Command paths and their ``--flags`` for every fenced ``sh`` CLI line."""
    parsed: list[tuple[list[str], list[str]]] = []
    for info, lines in _fenced_blocks(text):
        if info != "sh":
            continue
        for line in lines:
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


def _credential_runbooks() -> tuple[Path, ...]:
    found = tuple(runbook for runbook in RUNBOOKS if runbook.name == _CREDENTIAL_RUNBOOK)
    assert found, "credential-rotation runbook missing from RUNBOOKS"
    return found


def _evidence_labels(runbook: Path) -> tuple[str, ...]:
    if runbook.name == _OUTAGE_RUNBOOK:
        return (*EVIDENCE_LABELS, *_OUTAGE_EVIDENCE_LABELS)
    return EVIDENCE_LABELS


def _help_for(path: list[str]) -> str:
    capture = io.StringIO()
    with pytest.raises(SystemExit) as exited:
        with contextlib.redirect_stdout(capture):
            main([*path, "--help"])
    assert exited.value.code == 0, f"{' '.join(path)} --help exited {exited.value.code}"
    return capture.getvalue()


def test_every_runbook_cli_line_has_a_live_command_path_and_help() -> None:
    for runbook in RUNBOOKS:
        text = runbook.read_text(encoding="utf-8")
        lines = _cli_lines(text)
        assert lines, f"{runbook} declares no omp-work sh commands"

        for path, flags in lines:
            assert path, "an omp-work line has no command path before its first flag"
            help_text = _help_for(path)
            for flag in flags:
                assert flag in help_text, (
                    f"{runbook}: {flag} is not named by "
                    f"`omp-work {' '.join(path)} --help`"
                )


def test_runbook_has_exactly_one_complete_evidence_block() -> None:
    for runbook in RUNBOOKS:
        text = runbook.read_text(encoding="utf-8")
        evidence = [
            lines
            for info, lines in _fenced_blocks(text)
            if info == "text evidence"
        ]
        assert len(evidence) == 1, (
            f"{runbook}: expected exactly one `text evidence` block, got {len(evidence)}"
        )
        body = "\n".join(evidence[0])
        for label in _evidence_labels(runbook):
            assert label in body, f"{runbook}: evidence block omits {label!r}"


def test_runbook_restart_set_and_unit_list_accuracy() -> None:
    for runbook in _credential_runbooks():
        text = runbook.read_text(encoding="utf-8")
        assert "connection_kwargs" in text, f"{runbook}: must cite connection_kwargs"
        assert "read_secret" in text, f"{runbook}: must explain read_secret on each connection"
        assert "docs/work-ledger-operations.md" in text, (
            f"{runbook}: must cite docs/work-ledger-operations.md unit list"
        )
        assert "no units must restart" in text.lower(), (
            f"{runbook}: must state that no units must restart to pick up the new password"
        )
        assert "omp-work-postgres.service" in text, (
            f"{runbook}: must account for omp-work-postgres.service remaining up"
        )


def test_runbook_mission_window_and_discovery() -> None:
    for runbook in _credential_runbooks():
        text = runbook.read_text(encoding="utf-8")
        assert "approved" in text, f"{runbook}: must require an approved mission"
        assert "awaiting_confirmation" in text, (
            f"{runbook}: must distinguish approved from awaiting_confirmation"
        )
        assert "projects show" in text, (
            f"{runbook}: must provide projects show to discover open missions"
        )
        assert "orchestrator status" in text, (
            f"{runbook}: must use orchestrator status to inspect and confirm pickup"
        )
        assert "open_missions" in text, (
            f"{runbook}: must name the open_missions field of projects show"
        )


def test_runbook_failure_recovery_distinguishes_rollback_from_crash() -> None:
    for runbook in _credential_runbooks():
        text = runbook.read_text(encoding="utf-8")
        assert "credential rotation failed; recovery credential retained" in text, (
            f"{runbook}: must name the exact rotation failure error"
        )
        assert "rm <config>/<role>.next" in text, (
            f"{runbook}: must delete rejected candidate on transaction rollback"
        )
        assert "mv <config>/<role>.next <config>/<role>" in text, (
            f"{runbook}: must promote candidate only for crash after commit"
        )
