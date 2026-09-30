"""After /execute, the copied session dir holds the execution transcript.

The banner tells the operator to stop the environment before quitting omp.
A readback taken that way (grant still active) matches the automated
known_good capture; a readback taken after quit (grant paused) does not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omp_harbor_eval.interactive_env import run_interactive_env
from omp_harbor_eval.parity import capture, compare

DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
WORK_TS = REPO_ROOT / "session-system" / "extensions" / "workflow" / "work.ts"
BEARER = "harbor-test-token"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-7000-8000-000000000001"
WS = "ws"
WORKER = "wk"
HEALTH = {"ready": True, "live": True}
REVISION_ID = "12f338f5-6088-58c4-b44c-15dab48284e1"
STOP_BEFORE_QUIT = "Stop the environment (readback) before quitting omp."
EXEC_DIR = "-.omp-wt-execute-omp-1-deadbeef-workspace"
EXEC_NAME = "2026-09-30T03-21-37-414Z_exec.jsonl"
SESSION = '{"type":"session","id":"exec"}\n'
ACTIVE_EXECUTION = {
    "grant": {
        "state": "active",
        "terminal_reason": None,
        "mode": "single",
        "max_continuations": 8,
        "max_close_attempts": 5,
        "max_no_progress": 3,
    },
    "active_item": {
        "work_id": WORK_ID,
        "phase": "criteria_pending",
        "original_request": "Initial description",
        "terminal_reason": None,
    },
}
ACTIVE_ITEM = {
    "work_id": WORK_ID,
    "state": "IN_PROGRESS",
    "revision": {"revision_id": REVISION_ID, "revision_number": 2},
}


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    _write_config(directory, responses=_responses(ACTIVE_EXECUTION, ACTIVE_ITEM))
    return directory


def test_execute_session_is_copied_and_active_readback_matches_known_good(
    fake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OMP_HARBOR_BEARER", BEARER)
    monkeypatch.setenv("OMP_HARBOR_WORKSPACE_ID", WORKSPACE)
    _prepare_workspace(fake_dir)
    out = tmp_path / "out"
    known_readback = tmp_path / "known-good-readback.json"
    known_session = tmp_path / "known-good-session.jsonl"
    known = {"execution": ACTIVE_EXECUTION, "health": HEALTH, "work_item": ACTIVE_ITEM}
    known_readback.write_text(json.dumps(known) + "\n", encoding="utf-8")
    known_session.write_text(SESSION, encoding="utf-8")

    def stop() -> None:
        captured = capsys.readouterr()
        assert STOP_BEFORE_QUIT in captured.err
        assert captured.out == (
            "key: OMP-1\n"
            f"omp: docker exec -it {WORKER} omp --model scripted/scripted "
            "--session-dir /home/agent/omp-sessions\n"
            f"session dir: {out / 'session'}\n"
        )
        home = fake_dir / "root" / "home" / "agent"
        cli = home / "omp-sessions"
        cli.mkdir(parents=True)
        (cli / "cli.jsonl").write_text("cli-session\n", encoding="utf-8")
        execution = home / ".omp" / "agent" / "sessions" / EXEC_DIR
        execution.mkdir(parents=True)
        (execution / EXEC_NAME).write_text(SESSION, encoding="utf-8")

    code = run_interactive_env("f1", out, fixtures_root=FIXTURES, docker=str(DOCKER), stop=stop)

    assert code == 0
    copied = out / "session" / EXEC_DIR / EXEC_NAME
    assert copied.read_text(encoding="utf-8") == SESSION
    assert (out / "session" / "cli.jsonl").read_text(encoding="utf-8") == "cli-session\n"
    after = capsys.readouterr()
    assert after.out == f"session file: {copied}\n"
    readback = json.loads((out / "service-readback.json").read_text(encoding="utf-8"))
    assert readback == known
    interactive = capture(copied, out / "service-readback.json")
    assert compare(interactive, capture(known_session, known_readback)) == []
    assert interactive["transitions"] == ["active"]
    assert interactive["result"]["grant"]["state"] == "active"
    assert interactive["routing"]["command"] == "Initial description"

    paused = json.loads(known_readback.read_text(encoding="utf-8"))
    paused["execution"]["grant"]["state"] = "paused"
    paused_path = tmp_path / "quit-first-readback.json"
    paused_path.write_text(json.dumps(paused) + "\n", encoding="utf-8")
    assert compare(interactive, capture(copied, paused_path)) == ["transitions", "result"]


def _responses(execution: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        WS: {"/": {"status": 404, "body": {"error": "not_found"}}},
        WORKER: {
            "/v1/health/ready": {"status": 200, "body": HEALTH},
            f"/v1/workspaces/{WORKSPACE}/execution": {"status": 200, "body": execution},
            f"/v1/work-items/{WORK_ID}": {"status": 200, "body": item},
        },
    }


def _prepare_workspace(fake_dir: Path) -> None:
    repo = fake_dir / "root" / "workspace"
    target = repo / "session-system" / "extensions" / "workflow" / "work.ts"
    target.parent.mkdir(parents=True)
    shutil.copyfile(WORK_TS, target)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {
        "containers": {"workservice": [WS], "worker": [WORKER]},
        "fail": [],
        "omp": [],
        "responses": {},
    }
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
