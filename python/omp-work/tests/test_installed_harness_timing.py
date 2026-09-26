"""Contracts of the installed-runtime harness under host load (OMP-342).

These do not need a staged installed release: they cover the wait budgets and the
partial-safe stdout reads whose failures only surfaced on a machine running above
its core count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import installed_runtime_support as support
import pytest
from installed_runtime_support import (
    LOAD_SCALE_CAP,
    PROXY_UPSTREAM_TIMEOUT_SECONDS,
    RpcProcess,
    _NewlineCompleteLog,
    _run,
    load_scale_factor,
    scaled_timeout,
    step_label,
)

# A child that completes one JSONL record, tears the next, then finishes it: the
# live shape the installed controller produces while the suite reads its stdout.
_TORN_JSONL_CHILD = """
import sys, time
sys.stdout.write('{"type":"one"}\\n')
sys.stdout.flush()
sys.stdout.write('{"type":"two","n":')
sys.stdout.flush()
time.sleep(0.4)
sys.stdout.write('2}\\n')
sys.stdout.flush()
"""

# A child that appends many JSONL records in chunks smaller than a record, so a
# torn boundary exists while any reader is looking.
_CHUNKED_APPENDER = """
import sys
for index in range(__TOTAL__):
    record = '{"type":"event","n":%d}\\n' % index
    for start in range(0, len(record), 7):
        sys.stdout.write(record[start:start + 7])
        sys.stdout.flush()
"""

_STALLED_CHILD = """
import time
time.sleep(60)
"""


@pytest.fixture
def stabilize_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the newline-stabilize window so a loaded host cannot stretch these waits."""
    monkeypatch.setattr(support, "LINE_STABILIZE_SECONDS", 0.3, raising=True)


def _never(event: dict[str, object]) -> bool:
    return False


def _parsed(path: Path) -> list[dict[str, object]]:
    """Every record a text read hands a parser; a torn line fails the parse here."""
    return [
        json.loads(line) for line in _NewlineCompleteLog(path).read_text().splitlines()
    ]


def _rpc_process(tmp_path: Path, source: str) -> RpcProcess:
    child = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    log = tmp_path / "child.log"
    log.write_text("")
    return RpcProcess(child, log)


def _kill(rpc: RpcProcess) -> None:
    rpc.child.kill()
    rpc.child.wait(timeout=30)
    rpc.reader.join(timeout=30)


def test_text_read_never_exposes_a_partial_record(
    tmp_path: Path, stabilize_window: None
) -> None:
    """A torn trailing write is withheld, not parsed, and a later read delivers it whole."""
    log = tmp_path / "rpc.jsonl"
    log.write_text('{"type":"one"}\n{"type":"two","role":"tool"')

    assert [event["type"] for event in _parsed(log)] == ["one"]
    assert _NewlineCompleteLog(log).read_text().endswith("\n")

    log.write_text('{"type":"one"}\n{"type":"two","role":"tool"}\n')
    assert [event["type"] for event in _parsed(log)] == ["one", "two"]


def test_text_read_is_never_torn_while_a_writer_appends(tmp_path: Path) -> None:
    """Interleaved appends: every read parses, and the record prefix only grows.

    A real controller writes JSONL in small chunks, so a record boundary falls
    inside a write. Reading at record granularity must never hand a parser a torn
    line — under any schedule — and must never return fewer records than a
    previous read of the same growing file.
    """
    log = tmp_path / "rpc.jsonl"
    total = 200
    writer = subprocess.Popen(
        [sys.executable, "-c", _CHUNKED_APPENDER.replace("__TOTAL__", str(total))],
        stdout=log.open("ab"),
    )
    try:
        deadline = time.monotonic() + 60
        counts: list[int] = []
        while time.monotonic() < deadline:
            counts.append(len(_parsed(log)))
            if counts[-1] == total:
                break
        assert counts[-1] == total
        assert counts == sorted(counts)
    finally:
        writer.wait(timeout=30)


def test_text_read_stays_raw_for_terminal_byte_witnesses(
    tmp_path: Path, stabilize_window: None
) -> None:
    """Post-kill byte evidence must still see the unterminated tail."""
    log = tmp_path / "rpc.jsonl"
    log.write_bytes(b'{"type":"one"}\n{"type":"partial"')

    assert _NewlineCompleteLog(log).read_text() == '{"type":"one"}\n'
    assert _NewlineCompleteLog(log).read_bytes().endswith(b'{"type":"partial"')


def test_text_read_gives_up_once_the_writer_is_gone(tmp_path: Path) -> None:
    """A dead child ends the wait immediately instead of burning the whole window."""
    log = tmp_path / "rpc.jsonl"
    log.write_bytes(b'{"type":"one"}\n{"type":"partial"')

    started = time.monotonic()
    assert (
        _NewlineCompleteLog(log, alive=lambda: False).read_text() == '{"type":"one"}\n'
    )
    assert time.monotonic() - started < support.LINE_STABILIZE_SECONDS


def test_a_log_without_a_complete_line_reads_empty(
    tmp_path: Path, stabilize_window: None
) -> None:
    """Nothing to parse is empty text, never a fragment."""
    log = tmp_path / "rpc.jsonl"
    log.write_bytes(b'{"type":"partial"')

    assert _NewlineCompleteLog(log).read_text() == ""


def test_scale_is_floored_scaled_and_capped() -> None:
    """Budgets stretch with the run queue, so a loaded host is not failed on an idle deadline."""
    assert load_scale_factor(0.0, 16) == 1.0
    assert load_scale_factor(-4.0, 16) == 1.0
    assert load_scale_factor(16.0, 16) == 2.0
    assert load_scale_factor(8.0, 16) == 1.5
    assert load_scale_factor(10_000.0, 16) == LOAD_SCALE_CAP
    assert scaled_timeout(90.0, load=16.0, cores=16) == 180.0
    assert scaled_timeout(90.0, load=10_000.0, cores=16) == 90.0 * LOAD_SCALE_CAP


def test_proxy_upstream_budget_scales_like_the_other_waits() -> None:
    """The forwarding proxy's upstream deadline also stretches, so a loaded host
    cannot abort a valid command the controller is still waiting on."""
    assert scaled_timeout(PROXY_UPSTREAM_TIMEOUT_SECONDS, load=16.0, cores=16) == (
        PROXY_UPSTREAM_TIMEOUT_SECONDS * 2
    )
    assert scaled_timeout(PROXY_UPSTREAM_TIMEOUT_SECONDS, load=10_000.0, cores=16) == (
        PROXY_UPSTREAM_TIMEOUT_SECONDS * LOAD_SCALE_CAP
    )


def test_host_load_reports_a_runnable_queue_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform that cannot report load yields an unscaled floor, not a crash."""
    monkeypatch.setattr(support.os, "getloadavg", lambda: (7.5, 1.0, 1.0), raising=True)
    assert support.host_load() == 7.5

    def unavailable() -> tuple[float, float, float]:
        raise OSError("load average unavailable")

    monkeypatch.setattr(support.os, "getloadavg", unavailable, raising=True)
    assert support.host_load() == 0.0


def test_step_label_names_the_wait_and_falls_back() -> None:
    """An unlabelled wait is reported as the source line that issued it."""
    assert step_label(_never).startswith(f"{Path(__file__).name}:")
    assert step_label(None) == "unnamed wait"


def test_run_timeout_names_the_step(tmp_path: Path) -> None:
    """A command step that overran says which command and which budget."""
    with pytest.raises(pytest.fail.Exception) as raised:
        _run(["sleep", "30"], tmp_path, dict(os.environ), timeout=0.1)

    message = str(raised.value)
    assert "step 'sleep 30' timed out" in message
    assert "0.1s" in message


def test_stdout_log_is_partial_safe_end_to_end(tmp_path: Path) -> None:
    """A real child that tears a JSONL write is still read only at record boundaries."""
    rpc = _rpc_process(tmp_path, _TORN_JSONL_CHILD)
    try:
        deadline = time.monotonic() + 30
        seen: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            seen = _parsed(rpc.stdout_log)
            if len(seen) == 2:
                break
        assert [event["type"] for event in seen] == ["one", "two"]
        assert seen[1]["n"] == 2
    finally:
        _kill(rpc)


def test_rpc_wait_timeout_names_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled RPC wait reports the command whose response never arrived."""
    monkeypatch.setattr(support, "RPC_RESPONSE_TIMEOUT_SECONDS", 0.2, raising=True)
    monkeypatch.setattr(support, "host_load", lambda: 0.0, raising=True)
    rpc = _rpc_process(tmp_path, _STALLED_CHILD)
    try:
        with pytest.raises(pytest.fail.Exception) as raised:
            rpc.until(lambda event: True, step="request stamp_execution_plan")
        message = str(raised.value)
        assert "on step request stamp_execution_plan" in message
        assert "0.2s" in message
    finally:
        _kill(rpc)
