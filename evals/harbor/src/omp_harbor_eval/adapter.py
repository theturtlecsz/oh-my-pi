"""Drive one Harbor scenario over the normal RPC ``/execute`` path.

``ServiceProbe`` reads loopback WorkService routes. ``RpcAdapter.run`` waits
for a ready frame, a good ``get_state``, and a ready probe, sends
``scenario.command`` once, and polls until the readback pointer is terminal.
It seals the evidence directory before it stops the RPC process.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import omp_rpc

from .evidence import EvidenceWriter
from .fixtures import Scenario
from .grader import OUTCOME, SERVICE_READBACK, resolve_pointer

RPC_TRANSCRIPT = "rpc-transcript.jsonl"
SESSION_NAME = "session.json"
SESSION_LOG = "session.jsonl"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_POLL_INTERVAL_S = 0.05
_DEFAULT_TIMEOUT_S = 30.0
_RPC_TIMEOUT_S = 15.0
_HTTP_TIMEOUT_S = 5.0
_MISSING = object()


class ProbeError(Exception):
    """A loopback WorkService read failed."""


class _Transcript:
    def __init__(self, evidence: EvidenceWriter) -> None:
        self._evidence = evidence
        self._lock = threading.Lock()
        self._closed = False

    def write(self, direction: str, frame: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self._evidence.append_jsonl(RPC_TRANSCRIPT, {"direction": direction, "frame": frame})

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _LoggingLines:
    """Stdout stand-in that records each RPC frame before the client parses it."""

    def __init__(self, stream: Any, transcript: _Transcript) -> None:
        self._stream = stream
        self._transcript = transcript

    def __iter__(self) -> _LoggingLines:
        return self

    def __next__(self) -> str:
        line = self._stream.readline()
        if line == "":
            raise StopIteration
        self._record(line)
        return line

    def readline(self) -> str:
        line = self._stream.readline()
        if line:
            self._record(line)
        return line

    def close(self) -> None:
        self._stream.close()

    def _record(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            frame: Any = json.loads(stripped)
        except json.JSONDecodeError:
            frame = stripped
        self._transcript.write("in", frame)


class _LoggingClient(omp_rpc.RpcClient):
    def __init__(self, transcript: _Transcript, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transcript = transcript

    def _write_json(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        self._transcript.write("out", payload)
        super()._write_json(process, payload)

    def start(self) -> _LoggingClient:
        original = subprocess.Popen
        transcript = self._transcript

        def popen(*args: Any, **kwargs: Any) -> Any:
            process = original(*args, **kwargs)
            if process.stdout is not None:
                process.stdout = _LoggingLines(process.stdout, transcript)
            return process

        subprocess.Popen = popen  # type: ignore[misc, assignment]
        try:
            super().start()
        finally:
            subprocess.Popen = original  # type: ignore[misc, assignment]
        return self


def _require_loopback(base_url: str) -> str:
    if not isinstance(base_url, str) or base_url.strip() == "":
        raise ValueError("base_url must be an http(s) loopback origin (127.0.0.1, ::1, localhost)")
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in _LOOPBACK_HOSTS or parsed.netloc == "":
        raise ValueError(
            f"base_url must be an http(s) loopback origin (127.0.0.1, ::1, localhost), got {base_url!r}"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"base_url must be an origin with no path, query, or credentials, got {base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _matches(value: Any, accepted: tuple[Any, ...]) -> bool:
    return any(value == item and type(value) is type(item) for item in accepted)


def _work_item_key(execution: Mapping[str, Any]) -> str:
    candidates: list[Any] = []
    active = execution.get("active_item")
    if isinstance(active, Mapping):
        candidates.append(active.get("work_id"))
    items = execution.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping):
                candidates.append(item.get("work_id"))
    for key in candidates:
        if isinstance(key, str) and key != "":
            return key
    raise ProbeError("execution view has no work item")


class ServiceProbe:
    """Loopback reader for ``/v1/health/ready``, the execution view, and the work item."""

    def __init__(self, base_url: str, bearer: str, workspace_id: str) -> None:
        self.base_url = _require_loopback(base_url)
        self.bearer = bearer
        self.workspace_id = workspace_id

    def ready(self) -> bool:
        try:
            document = self._get("/v1/health/ready", auth=False)
        except ProbeError:
            return False
        return document.get("ready") is True

    def read(self) -> dict[str, Any]:
        health = self._get("/v1/health/ready", auth=False)
        execution = self._get(
            f"/v1/workspaces/{urllib.parse.quote(self.workspace_id, safe='')}/execution",
            auth=True,
        )
        key = _work_item_key(execution)
        work_item = self._get(f"/v1/work-items/{urllib.parse.quote(key, safe='')}", auth=True)
        return {"health": health, "execution": execution, "work_item": work_item}

    def _get(self, path: str, *, auth: bool) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.bearer}"
            headers["X-OMP-Workspace-ID"] = self.workspace_id
        request = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ProbeError(f"{path} returned {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProbeError(f"{path} unavailable: {exc.reason}") from exc
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProbeError(f"{path} was not JSON") from exc
        if not isinstance(document, dict):
            raise ProbeError(f"{path} was not a JSON object")
        return document


class RpcAdapter:
    """One scenario, one ``prompt``, then the authoritative service readback."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: str | Path | None,
        env: Mapping[str, str] | None,
        probe: ServiceProbe,
        evidence: EvidenceWriter,
        scenario: Scenario,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty argv")
        self.command = tuple(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.probe = probe
        self.evidence = evidence
        self.scenario = scenario

    def run(self) -> str:
        """Return ``harness_error``, ``timeout``, or the terminal readback value."""

        transcript = _Transcript(self.evidence)
        client: _LoggingClient | None = None
        prompts_sent = 0
        session_id: str | None = None
        session_file: str | None = None
        readback: dict[str, Any] | None = None
        outcome = "harness_error"
        reason = "rpc client did not start"
        try:
            client = _LoggingClient(
                transcript,
                command=self.command,
                cwd=self.cwd,
                env=self.env,
                startup_timeout=_RPC_TIMEOUT_S,
                request_timeout=_RPC_TIMEOUT_S,
            )
            client.start()
            state = client.get_state()
            session_id = state.session_id
            session_file = state.session_file
            if session_id == "" or session_file is None or session_file == "":
                reason = "get_state did not return a session id and file"
            elif not self.probe.ready():
                reason = "service not ready"
            else:
                prompts_sent = 1
                client.prompt(self.scenario.command)
                outcome, reason, readback = self._wait_for_terminal()
        except Exception as exc:
            outcome = "harness_error"
            reason = str(exc) or type(exc).__name__
        return self._seal(
            transcript,
            client,
            outcome=outcome,
            reason=reason,
            prompts_sent=prompts_sent,
            session_id=session_id,
            session_file=session_file,
            readback=readback,
        )

    def _wait_for_terminal(self) -> tuple[str, str, dict[str, Any] | None]:
        timeout_s = self.scenario.timeout_s if self.scenario.timeout_s is not None else _DEFAULT_TIMEOUT_S
        pointer = self.scenario.terminal.pointer
        accepted = self.scenario.terminal.accepted
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] | None = None
        while True:
            try:
                document = self.probe.read()
            except ProbeError:
                document = None
            if document is not None:
                last = document
                try:
                    value = resolve_pointer(document, pointer)
                except (KeyError, TypeError, ValueError):
                    value = _MISSING
                if value is not _MISSING and _matches(value, accepted):
                    outcome = value if isinstance(value, str) else "completed"
                    return outcome, "terminal", last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", f"timed out after {timeout_s}s waiting for {pointer}", last
            time.sleep(min(_POLL_INTERVAL_S, remaining))

    def _seal(
        self,
        transcript: _Transcript,
        client: _LoggingClient | None,
        *,
        outcome: str,
        reason: str,
        prompts_sent: int,
        session_id: str | None,
        session_file: str | None,
        readback: dict[str, Any] | None,
    ) -> str:
        transcript.close()
        try:
            self.evidence.write_json(SERVICE_READBACK, readback if readback is not None else {})
            self.evidence.write_json(SESSION_NAME, {"id": session_id, "file": session_file})
            payload = b""
            if session_file:
                path = Path(session_file)
                if path.is_file():
                    payload = path.read_bytes()
            self.evidence.add_file(SESSION_LOG, payload)
            self.evidence.write_json(
                OUTCOME,
                {"outcome": outcome, "reason": reason, "prompts_sent": prompts_sent},
            )
            self.evidence.seal()
        finally:
            if client is not None:
                client.stop()
        return outcome
