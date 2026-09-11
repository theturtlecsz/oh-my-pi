"""Shared admitted-runtime process fixtures; never replace CLI/session machinery."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import select
import shutil
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
    github_adapter: Path | None = None

    def command(self, state: Path, workspace: Path, *arguments: str) -> list[str]:
        command = [
            str(self.root / "bin/omp"),
            str(state),
            str(workspace),
            self.digest,
            *arguments,
        ]
        if self.github_adapter is not None and "--service" not in arguments:
            bwrap = shutil.which("bwrap")
            assert bwrap, "bubblewrap is required for the isolated GitHub fixture"
            return [
                bwrap,
                "--bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "--ro-bind",
                str(self.github_adapter),
                "/usr/bin/gh",
                "--",
                *command,
            ]
        return command


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
        self.operation_gets: list[dict] = []
        self.error: str | None = None
        self.predecessor_read_path: str | None = None
        self.fail_predecessor_read = False
        self.committed_command: str | None = None
        self.command_claimed = False
        self.discard_command_response = False

    def arm_committed_command(self, command_type: str) -> None:
        """Hold one real successful command response; caller independently verifies commit."""
        assert command_type in {
            "seal_execution_criteria",
            "stamp_execution_plan",
            "set_execution_state",
        }
        with self.lock:
            assert self.committed_command is None
            self.committed_command = command_type
            self.release.clear()

    def discard_held_command_response(self) -> None:
        """After controller death, close original socket without delivering response bytes."""
        with self.lock:
            self.discard_command_response = True
        self.release.set()

    def set_predecessor_read_fault(self, keyed_path: str, enabled: bool) -> None:
        """Disconnect only an actual keyed predecessor GET; preserve upstream evidence."""
        assert keyed_path.startswith("/v1/work-items/OMP-") and "/" not in keyed_path.removeprefix("/v1/work-items/")
        with self.lock:
            self.predecessor_read_path = keyed_path
            self.fail_predecessor_read = enabled
            self._save()

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

    def operation_gets_snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(record) for record in self.operation_gets]

    def _save(self) -> None:
        # Caller holds lock. Never write request/response headers or bearer tokens.
        (self.root / "authority-proxy.json").write_text(
            json.dumps(
                {
                    "requests": self.records,
                    "operationGets": self.operation_gets,
                    "error": self.error,
                },
                indent=2,
            )
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
                # The request still reaches the real service. Fault only transport
                # delivery of this keyed GET, never fabricate a successful response.
                predecessor_record: dict | None = None
                with proxy.lock:
                    predecessor_read = self.command == "GET" and route == proxy.predecessor_read_path
                    if predecessor_read:
                        disconnect = proxy.fail_predecessor_read
                        predecessor_record = {
                            "ordinal": len(proxy.records) + 1,
                            "method": self.command, "path": self.path,
                            "startedAt": started_at, "upstreamCompletedAt": time.time(),
                            "status": status, "bodySha256": hashlib.sha256(payload).hexdigest(),
                            "bodyBytes": len(payload), "predecessorRead": True,
                            "transportDisconnected": disconnect,
                            "held": False, "responseStarted": False, "responseBytesWritten": 0,
                        }
                        response_file = proxy.root / f"predecessor-read-{predecessor_record['ordinal']}.json"
                        response_file.write_bytes(payload)
                        predecessor_record["bodyFile"] = str(response_file)
                        proxy.records.append(predecessor_record)
                        proxy._save()
                    else:
                        disconnect = False
                if disconnect:
                    self.close_connection = True
                    return
                if self.command == "GET" and route.startswith("/v1/operations/"):
                    with proxy.lock:
                        proxy.operation_gets.append(
                            {
                                "ordinal": len(proxy.operation_gets) + 1,
                                "method": self.command,
                                "path": self.path,
                                "status": status,
                                "startedAt": started_at,
                                "completedAt": time.time(),
                            }
                        )
                        proxy._save()
                qualifying = (
                    self.command == "GET"
                    and bool(proxy.execution_prefix)
                    and (
                        route == proxy.execution_prefix
                        or route.startswith(proxy.execution_prefix + "/")
                    )
                )
                record: dict | None = predecessor_record
                withholding = False
                command_type = None
                if self.command == "POST" and route == "/v1/commands":
                    try:
                        command_type = json.loads(body).get("command", {}).get("type")
                    except (ValueError, AttributeError):
                        pass
                if command_type == proxy.committed_command and command_type is not None:
                    with proxy.lock:
                        try:
                            command_response = json.loads(payload)
                            applied = (
                                command_response.get("receipt", {}).get("state") == "applied"
                                and command_response.get("result", {}).get("status") != "refused"
                            )
                        except (ValueError, AttributeError):
                            applied = False
                        withholding = not proxy.command_claimed and 200 <= status < 300 and applied
                        if withholding:
                            proxy.command_claimed = True
                        ordinal = len(proxy.records) + 1
                        request_file = proxy.root / f"authority-held-{ordinal}-request.json"
                        response_file = proxy.root / f"authority-held-{ordinal}.json"
                        request_file.write_bytes(body)
                        response_file.write_bytes(payload)
                        record = {
                            "ordinal": ordinal,
                            "method": self.command,
                            "path": self.path,
                            "startedAt": started_at,
                            "upstreamCompletedAt": time.time(),
                            "status": status,
                            "commandType": command_type,
                            "requestFile": str(request_file),
                            "requestSha256": hashlib.sha256(body).hexdigest(),
                            "bodyFile": str(response_file),
                            "bodySha256": hashlib.sha256(payload).hexdigest(),
                            "bodyBytes": len(payload),
                            "held": withholding,
                            "responseStarted": False,
                            "responseBytesWritten": 0,
                        }
                        proxy.records.append(record)
                        proxy._save()
                        if withholding:
                            proxy.held.set()
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
                if withholding and command_type is not None and proxy.discard_command_response:
                    with proxy.lock:
                        assert record is not None
                        record["discardedAfterControllerDeath"] = True
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
    def __init__(
        self, child: subprocess.Popen[str], log: Path, *, completion_stop: bool = False
    ):
        self.child = child
        self.log = log
        self.stdout_log = log.with_suffix(".rpc.jsonl")
        self.events: queue.Queue[str | None] = queue.Queue()
        self._completion_stop = completion_stop
        self._stop_lock = threading.Lock()
        self._stop_target: dict | None = None
        self.stop_requested = threading.Event()
        self.stop_record: dict | None = None
        self.reader_error: str | None = None
        self.ordinary_eof = False
        self._raw_tail = b""
        self._raw_frames: list[dict] = []
        self._drain_requested = threading.Event()
        self._drained = threading.Event()
        self._raw_eof = False
        self._reader_finished = threading.Event()
        self.reader = threading.Thread(
            target=self._read_bytes if completion_stop else self._read, daemon=True
        )
        self.reader.start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        try:
            with self.stdout_log.open("w") as captured:
                for line in self.child.stdout:
                    captured.write(line)
                    captured.flush()
                    self.events.put(line)
            self.ordinary_eof = True
        except (OSError, ValueError) as error:
            # Process cleanup may close stdout while this reader observes EOF.
            # Retain failure so a joined thread is not mistaken for clean capture.
            self.reader_error = repr(error)
        finally:
            self.events.put(None)

    def arm_completion_stop(
        self, *, group_id: int, child_id: str, tool_call_id: str, session_file: str
    ) -> None:
        assert self._completion_stop, (
            "Completion stop requires the sole byte reader from process start"
        )
        assert group_id == self.child.pid and os.getpgid(self.child.pid) == group_id
        with self._stop_lock:
            assert self._stop_target is None
            self._stop_target = {
                "groupId": group_id,
                "childId": child_id,
                "toolCallId": tool_call_id,
                "sessionFile": session_file,
            }

    def _read_bytes(self) -> None:
        """Own stdout exclusively; signal before disk logging or main-thread dispatch."""
        pending = b""
        unlogged = b""
        capture_attempted = False
        try:
            if self.child.stdout is None:
                raise OSError("RPC stdout is unavailable")
            descriptor = self.child.stdout.fileno()
            os.set_blocking(descriptor, False)
            with self.stdout_log.open("wb") as captured:
                while True:
                    select.select([descriptor], [], [], 0.05)
                    while True:
                        try:
                            chunk = os.read(descriptor, 65536)
                        except BlockingIOError:
                            if self._drain_requested.is_set():
                                with self._stop_lock:
                                    self._raw_tail = pending
                                self._drain_requested.clear()
                                self._drained.set()
                            break
                        if not chunk:
                            with self._stop_lock:
                                self._raw_eof = True
                            return
                        unlogged = chunk
                        capture_attempted = False
                        pending += chunk
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        completed: list[str] = []
                        for raw in lines:
                            line = raw.decode("utf-8")
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise TypeError("RPC frame is not an object")
                            lifecycle = event.get("type") == "subagent_lifecycle"
                            payload = event.get("payload")
                            if lifecycle and not isinstance(payload, dict):
                                raise TypeError(
                                    "Subagent lifecycle payload is not an object"
                                )
                            with self._stop_lock:
                                self._raw_frames.append(event)
                                target = self._stop_target
                                if (
                                    target
                                    and self.stop_record is None
                                    and lifecycle
                                    and (
                                        payload.get("status") == "completed"
                                        and payload.get("id") == target["childId"]
                                        and payload.get("parentToolCallId")
                                        == target["toolCallId"]
                                        and payload.get("sessionFile")
                                        == target["sessionFile"]
                                    )
                                ):
                                    record = {
                                        "signal": "SIGSTOP",
                                        "groupId": target["groupId"],
                                        "requestedAt": time.time(),
                                        "frameOrdinal": len(self._raw_frames),
                                        "trigger": event,
                                        "triggerSha256": hashlib.sha256(
                                            raw + b"\n"
                                        ).hexdigest(),
                                    }
                                    os.killpg(target["groupId"], signal.SIGSTOP)
                                    self.stop_record = record
                                    self.stop_requested.set()
                            completed.append(line + "\n")
                        capture_attempted = True
                        captured.write(chunk)
                        unlogged = b""
                        captured.flush()
                        with self._stop_lock:
                            self._raw_tail = pending
                        for line in completed:
                            self.events.put(line)
        except (OSError, ValueError, UnicodeError, TypeError, KeyError) as error:
            self.reader_error = f"{type(error).__name__}: {error}"
            if unlogged:
                if capture_attempted:
                    # A failed write may have stored a prefix; keep separate raw evidence.
                    self.stdout_log.with_suffix(".read-error.bin").write_bytes(unlogged)
                else:
                    with self.stdout_log.open("ab") as captured:
                        captured.write(unlogged)
            self.stdout_log.with_suffix(".reader-error.json").write_text(
                json.dumps(
                    {
                        "error": self.reader_error,
                        "uncertainCaptureWrite": bool(unlogged) and capture_attempted,
                    }
                )
            )
        finally:
            with self._stop_lock:
                self._raw_tail = pending
            self._reader_finished.set()
            self._drained.set()
            self.events.put(None)

    def byte_snapshot(self) -> dict:
        assert self._completion_stop
        with self._stop_lock:
            snapshot = {
                "frames": list(self._raw_frames),
                "partialBytes": self._raw_tail.hex(),
                "eof": self._raw_eof,
                "readerError": self.reader_error,
                "stop": dict(self.stop_record or {}),
            }
        self.stdout_log.with_suffix(".partial.bin").write_bytes(
            bytes.fromhex(snapshot["partialBytes"])
        )
        return snapshot

    def drain_stopped(self) -> dict:
        """Reach EAGAIN via the sole reader, or return its already terminal snapshot."""
        assert self._completion_stop
        if self._reader_finished.is_set():
            return self.byte_snapshot()
        assert self.stop_requested.is_set()
        self._drained.clear()
        self._drain_requested.set()
        if not self._reader_finished.is_set():
            assert self._drained.wait(5), "Stopped stdout did not drain"
        return self.byte_snapshot()

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
