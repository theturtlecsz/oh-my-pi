"""Shared admitted-runtime process fixtures; never replace CLI/session machinery."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest


@dataclass(frozen=True)
class InstalledRelease:
    root: Path
    digest: str

    def command(self, state: Path, workspace: Path, *arguments: str) -> list[str]:
        return [
            str(self.root / "bin/omp"),
            str(state),
            str(workspace),
            self.digest,
            *arguments,
        ]


@pytest.fixture
def installed_release() -> InstalledRelease:
    root = os.environ.get("OMP_INSTALLED_RELEASE")
    digest = os.environ.get("OMP_INSTALLED_MANIFEST_SHA256")
    if not root or not digest:
        pytest.skip(
            "installed-process qualification requires OMP_INSTALLED_RELEASE "
            "and OMP_INSTALLED_MANIFEST_SHA256"
        )
    release = InstalledRelease(Path(root).resolve(strict=True), digest)
    assert (release.root / "bin/omp").is_file(), "selected release lacks its launcher"
    return release


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(arguments: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        check=False,
        args=arguments,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-6000:]
    return result.stdout


@contextlib.contextmanager
def _process(
    arguments: list[str], cwd: Path, env: dict[str, str], log: Path
) -> Generator[subprocess.Popen[str]]:
    with log.open("w") as stderr:
        child = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            yield child
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
            if child.stdin:
                child.stdin.close()
            if child.stdout:
                child.stdout.close()


class RpcProcess:
    def __init__(self, child: subprocess.Popen[str], log: Path):
        self.child = child
        self.log = log
        self.stdout_log = log.with_suffix(".rpc.jsonl")
        self.events: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        try:
            with self.stdout_log.open("w") as captured:
                for line in self.child.stdout:
                    captured.write(line)
                    captured.flush()
                    self.events.put(line)
        except (OSError, ValueError):
            # Process cleanup may close stdout while this reader observes EOF.
            pass
        finally:
            self.events.put(None)

    def send(self, command: str, **fields: object) -> str:
        request_id = str(uuid4())
        assert self.child.stdin is not None
        self.child.stdin.write(
            json.dumps({"id": request_id, "type": command, **fields}) + "\n"
        )
        self.child.stdin.flush()
        return request_id

    def until(
        self, predicate: Callable[[dict[str, object]], bool]
    ) -> dict[str, object]:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                line = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                pytest.fail(
                    f"installed RPC process exited: {self.log.read_text()[-6000:]}"
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"installed RPC emitted non-JSON stdout: {line[:400]}")
            assert isinstance(event, dict)
            if event.get("type") == "response" and event.get("success") is False:
                pytest.fail(f"installed RPC refused request: {event}")
            if predicate(event):
                return event
        pytest.fail(f"installed RPC response timed out: {self.log.read_text()[-6000:]}")

    def request(self, command: str) -> dict[str, object]:
        request_id = self.send(command)
        response = self.until(
            lambda event: (
                event.get("type") == "response" and event.get("id") == request_id
            )
        )
        assert response["success"] is True
        return cast(dict[str, object], response.get("data", {}))

    def work_status(self) -> None:
        request_id = self.send("prompt", message="/work status")
        notice = self.until(
            lambda event: (
                event.get("type") == "extension_ui_request"
                and event.get("method") == "notify"
                and "service:" in str(event.get("message", ""))
            )
        )
        message = str(notice["message"])
        assert "service: ready" in message, message
        assert "authority: work" in message, message
        result = self.until(
            lambda event: (
                event.get("type") == "prompt_result" and event.get("id") == request_id
            )
        )
        assert result["agentInvoked"] is False, "read-only work command invoked a model"


def _health(
    base_url: str, child: subprocess.Popen[str], log: Path
) -> dict[str, object]:
    deadline = time.monotonic() + 90
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            assert child.poll() is None, log.read_text()[-6000:]
            try:
                response = client.get(f"{base_url}/v1/health/ready")
                response.raise_for_status()
                result = response.json()
                if result["ready"]:
                    assert result["live"] is True
                    # Fresh disposable databases have no backup or restore history.
                    # Those operational notices are expected; runtime/schema alerts are not.
                    assert set(result["alerts"]) <= {
                        "BACKUP_MISSING",
                        "RESTORE_DRILL_MISSING",
                    }
                    return result
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
    pytest.fail(f"installed service did not become ready: {log.read_text()[-6000:]}")
