"""Seeded ledger plus the worker origin let /execute pass both admission checks.

f1's OMP-1 and f2's OMP-246 are in the project named by .work-project. The
worker repo's origin is the local bare repo worker-origin.sh builds, the same
script Dockerfile.agent runs. /execute then gets past "fetch origin main
failed" and "does not exist in Work Ledger". The next gate (branch protection)
is what remains, and it does not need a network.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from omp_harbor_eval.ledger_seed import main as seed_main
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from test_ledger_seed import ACTOR_ID, F1_SEED, F2_SEED, WORKSPACE_ID, _credentials, _headers

HARBOR = Path(__file__).resolve().parents[1]
REPO = HARBOR.parents[1]
ORIGIN_SCRIPT = HARBOR / "docker" / "worker-origin.sh"
DOCKERFILE = HARBOR / "docker" / "Dockerfile.agent"
CLI_PATH = REPO / "packages" / "coding-agent" / "src" / "cli.ts"
INSTALL = REPO / "session-system" / "install.sh"
PROJECT_NAME = "The Bookends"
PROJECT_ID = "00000000-0000-7000-8000-0000000000b1"


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)
    assert run.returncode == 0, f"git {' '.join(args)}: {run.stderr}"
    return run.stdout.strip()


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _get(port: int, path: str) -> dict:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=_headers())
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.load(response)
    assert isinstance(body, dict)
    return body


def _wait_live(port: int, process: subprocess.Popen[str], log: Path) -> None:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/v1/health/live"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(log.read_text(encoding="utf-8")[-4000:])
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.1)
    raise AssertionError(f"work service did not become live\n{log.read_text(encoding='utf-8')[-4000:]}")


def _frame_reader(process: subprocess.Popen[str], raw: list[str]) -> queue.Queue[dict | None]:
    """Stdout JSON objects. ``None`` is EOF.

    A thread blocks on ``readline`` so a line already in the text buffer is not
    hidden from ``select`` on the pipe.
    """

    frames: queue.Queue[dict | None] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            raw.append(line)
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                frames.put(frame)
        frames.put(None)

    threading.Thread(target=read, daemon=True).start()
    return frames


def _next_frame(frames: queue.Queue[dict | None], deadline: float) -> dict | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return frames.get(timeout=remaining)
    except queue.Empty:
        return None


def _notify_messages(frames: list[dict]) -> list[str]:
    messages: list[str] = []
    for frame in frames:
        if frame.get("type") != "extension_ui_request" or frame.get("method") != "notify":
            continue
        message = frame.get("message")
        if isinstance(message, str):
            messages.append(message)
    return messages


def test_execute_passes_origin_and_project_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "worker-origin.sh /workspace /opt/harbor/origin.git" in dockerfile
    assert "chown -R agent:agent /workspace /home/agent /opt/harbor/origin.git" in dockerfile
    node_modules = REPO / "node_modules"
    if not node_modules.is_dir():
        raise AssertionError("node_modules is required to start omp")

    sys.path.insert(0, str(REPO / "python" / "omp-work" / "tests"))
    from installed_runtime_support import ReservedPort
    from pg_native import native_postgres

    pg = ReservedPort()
    http = ReservedPort()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OMP_WORK_POSTGRES_PORT", str(pg.port))
    config = OperationsConfig.defaults()
    owner = _credentials(config)

    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "omp-harbor@localhost")
    _git(repo, "config", "user.name", "omp-harbor")
    (repo / "README").write_text("seeded tree\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "initial harbor worker tree")
    origin = tmp_path / "origin.git"
    setup = subprocess.run(
        ["sh", str(ORIGIN_SCRIPT), str(repo), str(origin)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert setup.returncode == 0, setup.stderr
    (repo / ".work-project").write_text(f"{PROJECT_NAME}\n", encoding="utf-8")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    assert Path(_git(repo, "remote", "get-url", "origin")).resolve() == origin.resolve()
    fetched = subprocess.run(
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert fetched.returncode == 0, fetched.stderr
    assert _git(repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD") == ""

    home = tmp_path / "home"
    home.mkdir()
    installed = subprocess.run(
        ["bash", str(INSTALL), "--copy"],
        cwd=str(REPO),
        env={**os.environ, "HOME": str(home)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    client_dir = home / ".config" / "omp-work"
    client_dir.mkdir(parents=True)
    (client_dir / "client.json").write_text(
        json.dumps(
            {
                "base_url": f"http://127.0.0.1:{http.port}",
                "workspace_id": str(WORKSPACE_ID),
                "owner_id": str(ACTOR_ID),
                "bearer_file": str(owner),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gh_dir = tmp_path / "bin"
    gh_dir.mkdir()
    gh = gh_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'gh api refused' >&2\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)

    server_log = tmp_path / "workservice.log"
    server: subprocess.Popen[str] | None = None
    omp: subprocess.Popen[str] | None = None
    try:
        with native_postgres(tmp_path / "pg", pg.port, reserve=pg):
            bootstrap(config)
            assert seed_main([str(F1_SEED)]) == 0
            assert seed_main([str(F2_SEED)]) == 0
            assert seed_main([str(F1_SEED)]) == 0
            log_handle = server_log.open("w", encoding="utf-8")
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import uvicorn\n"
                    "from pathlib import Path\n"
                    "from omp_work.operations.config import OperationsConfig\n"
                    "from omp_work.v1.server import create_app\n"
                    "app = create_app(OperationsConfig.defaults(), capabilities_dir=Path(r'''"
                    + str(owner.parent)
                    + "'''))\n"
                    f"uvicorn.run(app, host='127.0.0.1', port={http.port}, access_log=False)\n",
                ],
                env=os.environ.copy(),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log_handle.close()
            _wait_live(http.port, server, server_log)
            http.close()

            tree = _get(http.port, f"/v1/workspaces/{WORKSPACE_ID}/tree")
            projects = tree.get("projects")
            assert isinstance(projects, list)
            named = [project for project in projects if project.get("name") == PROJECT_NAME]
            assert len(named) == 1
            assert named[0]["project_id"] == PROJECT_ID
            for key in ("OMP-1", "OMP-246"):
                item = _get(http.port, f"/v1/work-items/{key}")
                assert item["project_id"] == PROJECT_ID
                assert item["alias"]["key"] == key

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{gh_dir}{os.pathsep}{env.get('PATH', '')}"
            env.pop("XDG_CONFIG_HOME", None)
            env.pop("OMP_WORK_BEARER", None)
            omp = subprocess.Popen(
                ["bun", str(CLI_PATH), "--mode", "rpc"],
                cwd=repo,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert omp.stdin is not None
            stderr_parts: list[str] = []

            def drain_stderr() -> None:
                if omp is None or omp.stderr is None:
                    return
                stderr_parts.append(omp.stderr.read())

            stderr_thread = threading.Thread(target=drain_stderr)
            stderr_thread.start()
            frames: list[dict] = []
            raw: list[str] = []
            incoming = _frame_reader(omp, raw)
            saw_ready = False
            unfinished = ""
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    frame = _next_frame(incoming, deadline)
                    if frame is None:
                        break
                    frames.append(frame)
                    if frame.get("type") == "ready":
                        saw_ready = True
                        break
                if saw_ready:
                    quiet = time.monotonic() + 2
                    while time.monotonic() < quiet:
                        frame = _next_frame(incoming, quiet)
                        if frame is not None:
                            frames.append(frame)
                    for index, command in enumerate(("/execute OMP-1", "/execute OMP-246")):
                        prompt_id = f"p{index}"
                        omp.stdin.write(json.dumps({"type": "prompt", "id": prompt_id, "message": command}) + "\n")
                        omp.stdin.flush()
                        prompt_deadline = time.monotonic() + 25
                        finished = False
                        while time.monotonic() < prompt_deadline:
                            frame = _next_frame(incoming, prompt_deadline)
                            if frame is None:
                                break
                            frames.append(frame)
                            if frame.get("type") == "prompt_result" and frame.get("id") == prompt_id:
                                finished = True
                                break
                        if not finished:
                            unfinished = command
                            break
            finally:
                code = omp.poll()
                _stop(omp)
                stderr_thread.join(timeout=5)
            detail = (
                f"exit={code}\nraw={''.join(raw)[-4000:]}\n"
                f"stderr={''.join(stderr_parts)[-4000:]}\nnotifies={_notify_messages(frames)}"
            )
            assert saw_ready, f"omp did not become ready\n{detail}"
            assert unfinished == "", f"{unfinished} did not finish\n{detail}"
            messages = _notify_messages(frames)
            text = "\n".join(messages)
            detail = f"notifies={messages}\nstderr={''.join(stderr_parts)[-4000:]}"
            assert "fetch origin" not in text, detail
            assert "does not exist in Work Ledger" not in text, detail
            assert "does not exist in the Work Ledger" not in text, detail
            assert text.count("branch protection") >= 2, detail
    finally:
        _stop(omp)
        _stop(server)
        http.close()
