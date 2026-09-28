"""Parity capture: equal runs compare empty, and a changed field is named."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from omp_harbor_eval.parity import capture, compare

SRC = Path(__file__).resolve().parents[1] / "src"


def _session() -> str:
    rows = [
        {"type": "session", "id": "sess-1"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "/execute"}]}},
        {"type": "model_change", "model": "openai/gpt-4o"},
        {"type": "model_change", "model": "anthropic/claude", "role": "editor"},
        {"type": "execution_state", "state": "admitted"},
        {"type": "custom", "customType": "execution_state", "data": {"state": "completed"}},
    ]
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def _readback(*, mode: str = "single") -> dict:
    return {
        "health": {"ready": True},
        "execution": {
            "grant": {
                "mode": mode,
                "state": "completed",
                "terminal_reason": None,
                "max_continuations": 8,
                "max_close_attempts": 5,
                "max_no_progress": 3,
            },
            "active_item": {
                "work_id": "w1",
                "position": 0,
                "phase": "executing",
                "criteria_revision_id": "rev-sealed",
                "terminal_reason": "done",
                "original_request": "/execute",
            },
        },
        "work_item": {"work_id": "w1", "state": "completed", "revision": {"revision_id": "rev-work"}},
    }


def _write(tmp_path: Path, *, mode: str = "single") -> tuple[Path, Path]:
    session = tmp_path / "session.jsonl"
    readback = tmp_path / "readback.json"
    session.write_text(_session(), encoding="utf-8")
    readback.write_text(json.dumps(_readback(mode=mode)) + "\n", encoding="utf-8")
    return session, readback


def _expected() -> dict:
    return {
        "authorization": {
            "mode": "single",
            "limits": {"max_continuations": 8, "max_close_attempts": 5, "max_no_progress": 3},
            "sealed_revision": "rev-sealed",
        },
        "routing": {
            "command": "/execute",
            "model_roles": [
                {"model": "openai/gpt-4o", "role": "default"},
                {"model": "anthropic/claude", "role": "editor"},
            ],
        },
        "transitions": ["admitted", "completed"],
        "result": {
            "grant": {"state": "completed", "terminal_reason": None},
            "item": {"work_id": "w1", "phase": "executing", "state": "completed", "terminal_reason": "done"},
        },
    }


def test_capture_and_compare(tmp_path: Path) -> None:
    session, readback = _write(tmp_path)
    first = capture(session, readback)
    assert first == _expected()
    assert compare(first, capture(session, readback)) == []

    changed = tmp_path / "changed"
    changed.mkdir()
    session_b, readback_b = _write(changed, mode="queue")
    assert compare(first, capture(session_b, readback_b)) == ["authorization"]

    routing = json.loads(json.dumps(first))
    routing["routing"]["model_roles"][0]["role"] = "planner"
    assert compare(first, routing) == ["routing"]

    transitions = json.loads(json.dumps(first))
    transitions["transitions"] = ["admitted", "failed"]
    assert compare(first, transitions) == ["transitions"]

    result = json.loads(json.dumps(first))
    result["result"]["item"]["state"] = "failed"
    assert compare(first, result) == ["result"]


def test_transitions_fall_back_to_the_grant_state(tmp_path: Path) -> None:
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "session", "id": "sess-1"}) + "\n", encoding="utf-8")
    readback = tmp_path / "readback.json"
    document = _readback()
    readback.write_text(json.dumps(document) + "\n", encoding="utf-8")
    assert capture(session, readback)["transitions"] == ["completed"]
    assert capture(session, readback)["routing"]["command"] == "/execute"


def test_compare_cli(tmp_path: Path) -> None:
    session, readback = _write(tmp_path)
    out = tmp_path / "capture.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    captured = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.parity",
            "capture",
            "--session",
            str(session),
            "--readback",
            str(readback),
            "--out",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert captured.returncode == 0, captured.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == capture(session, readback)

    same = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.parity", "compare", str(out), str(out)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert same.returncode == 0, same.stderr
    assert json.loads(same.stdout) == []

    other = tmp_path / "other.json"
    document = json.loads(out.read_text(encoding="utf-8"))
    document["result"]["grant"]["state"] = "stopped"
    other.write_text(json.dumps(document) + "\n", encoding="utf-8")
    diff = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.parity", "compare", str(out), str(other)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert diff.returncode == 1
    assert json.loads(diff.stdout) == ["result"]
