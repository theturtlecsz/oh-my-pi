"""Shared admitted-runtime process fixtures; never replace CLI/session machinery."""

from __future__ import annotations

import contextlib
import hashlib
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
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


class AuthorityResponseProxy:
    """Forward real HTTP payloads; withhold successful authority reads at one external cut."""

    def __init__(self, upstream: str, root: Path):
        self.upstream = upstream
        self.root = root
        self.lock = threading.Lock()
        self.held = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.enabled = False
        self.cut_started = False
        self.output_path: Path | None = None
        self.execution_prefix = ""
        self.records: list[dict] = []
        self.error: str | None = None

    def arm(self, output_path: Path, execution_prefix: str) -> None:
        assert not output_path.exists(), (
            "Child output predates the intended completion cut"
        )
        with self.lock:
            self.output_path = output_path
            self.execution_prefix = execution_prefix
            self.enabled = True
            self.release.clear()

    def allow_responses(self) -> None:
        with self.lock:
            self.enabled = False
        self.release.set()

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(record) for record in self.records]

    def _save(self) -> None:
        # Caller holds lock. Never write request/response headers or bearer tokens.
        (self.root / "authority-proxy.json").write_text(
            json.dumps({"requests": self.records, "error": self.error}, indent=2)
        )

    @contextlib.contextmanager
    def serve(self) -> Generator[str]:
        proxy = self
        hop_headers = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }
        client = httpx.Client(trust_env=False, timeout=30)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def forward(self) -> None:
                started_at = time.time()
                route = urlsplit(self.path).path
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = [
                    (name, value)
                    for name, value in self.headers.items()
                    if name.lower() not in hop_headers
                ]
                try:
                    request = client.build_request(
                        self.command,
                        proxy.upstream + self.path,
                        headers=headers,
                        content=body,
                    )
                    response = client.send(request, stream=True)
                    try:
                        # iter_raw preserves compressed payload bytes; only HTTP framing changes.
                        payload = b"".join(response.iter_raw())
                        response_headers = list(response.headers.multi_items())
                        status = response.status_code
                    finally:
                        response.close()
                except httpx.HTTPError as error:
                    with proxy.lock:
                        proxy.error = (
                            f"Upstream forwarding failed: {type(error).__name__}"
                        )
                        proxy._save()
                    self.close_connection = True
                    return
                qualifying = (
                    self.command == "GET"
                    and bool(proxy.execution_prefix)
                    and (
                        route == proxy.execution_prefix
                        or route.startswith(proxy.execution_prefix + "/")
                    )
                )
                record: dict | None = None
                withholding = False
                if qualifying:
                    with proxy.lock:
                        artifact_present = (
                            proxy.output_path is not None
                            and proxy.output_path.is_file()
                        )
                        if proxy.enabled and artifact_present:
                            proxy.cut_started = True
                        withholding = (
                            proxy.enabled and proxy.cut_started and 200 <= status < 300
                        )
                        record = {
                            "ordinal": len(proxy.records) + 1,
                            "method": self.command,
                            "path": self.path,
                            "startedAt": started_at,
                            "upstreamCompletedAt": time.time(),
                            "status": status,
                            "bodySha256": hashlib.sha256(payload).hexdigest(),
                            "bodyBytes": len(payload),
                            "artifactPresent": artifact_present,
                            "held": withholding,
                            "responseStarted": False,
                            "responseBytesWritten": 0,
                        }
                        proxy.records.append(record)
                        if withholding:
                            response_file = (
                                proxy.root / f"authority-held-{record['ordinal']}.json"
                            )
                            response_file.write_bytes(payload)
                            record["bodyFile"] = str(response_file)
                        proxy._save()
                        if withholding:
                            proxy.held.set()
                if withholding and not proxy.release.wait(300):
                    with proxy.lock:
                        proxy.error = (
                            "Authority withholding exceeded bounded observation timeout"
                        )
                        proxy._save()
                    self.close_connection = True
                    return
                try:
                    if record is not None:
                        with proxy.lock:
                            record["responseStarted"] = True
                            record["responseStartedAt"] = time.time()
                            proxy._save()
                    self.send_response_only(status)
                    for name, value in response_headers:
                        if name.lower() not in hop_headers | {"content-length"}:
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
                    if record is not None:
                        with proxy.lock:
                            record["responseBytesWritten"] = len(payload)
                            proxy._save()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = forward
            do_POST = forward
            do_PUT = forward
            do_PATCH = forward
            do_DELETE = forward
            do_HEAD = forward

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            proxy.allow_responses()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            client.close()


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

    def request(self, command: str, **fields: object) -> dict[str, object]:
        request_id = self.send(command, **fields)
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
