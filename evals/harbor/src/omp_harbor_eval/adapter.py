"""Drive one Harbor scenario over the normal RPC ``/execute`` path.

``ServiceProbe`` reads loopback WorkService routes. ``RpcAdapter.run`` waits
for a ready frame and a good ``get_state``, then polls the ready probe until
it reports ready or a bounded deadline passes. It sends ``scenario.command``
once and polls until the readback pointer is terminal. A deadline that passes
records the last probe failure on the outcome reason.
Scripted UI rules answer ``select``, ``confirm``, ``input``, and ``editor``
requests. A request with no rule is cancelled and the run ends ``blocked``.
An outbound frame matching ``scenario.kill_at.match`` is SIGKILLed once and
resumed with ``--session`` and no second prompt. An ``extension_error``, any
error event, or an ``extension_ui_request`` notify whose ``notifyType`` is
``error``, before the first ``agent_start`` or ``turn_start`` ends the trial
at once as ``harness_error`` with that text, ahead of a crash restart. Every
frame in both
directions is logged to ``rpc-transcript.jsonl``; the grader's
``transcript.jsonl`` instead holds one semantic record per refused ``work``
call. Optional ``session_reader``
and ``before_seal`` hooks read the session log and augment evidence before
sealing; hook exceptions record ``harness_error`` (or ``session_read_error`` if
the run already failed with ``harness_error``). When startup fails and stderr
names the worker omp log (``logs: <path>``), that file is copied to
``omp-startup.log`` before seal. The evidence directory is sealed before the
RPC process stops.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess  # nosec B404 - types and wraps RpcClient argv Popen (no shell); SIGKILL uses the resulting process
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import omp_rpc

from .evidence import EvidenceWriter
from .fixtures import Scenario
from .grader import OUTCOME, SERVICE_READBACK, TRANSCRIPT, resolve_pointer
from .ui_script import UiScript

RPC_TRANSCRIPT = "rpc-transcript.jsonl"
SESSION_NAME = "session.json"
SESSION_LOG = "session.jsonl"
UI_ANSWERS = "ui-answers.jsonl"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_POLL_INTERVAL_S = 0.05
_DEFAULT_TIMEOUT_S = 30.0
_READY_TIMEOUT_S = 30.0
_RPC_TIMEOUT_S = 15.0
_HTTP_TIMEOUT_S = 5.0
_MISSING = object()

_PREVIEW_PREFIX = "CONFIRM REQUIRED"
_WORK_TOOL = "work"
_STARTUP_LOG_NAME = "omp-startup.log"
# omp's startup watchdog prints `logs: ~/.omp/logs/omp.YYYY-MM-DD.<pid>.log`.
_STARTUP_LOG_RE = re.compile(r"logs:\s+(\S*omp\.\d{4}-\d{2}-\d{2}\.\d+\.log)")


def kill_process_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the RPC process group. This is the default adapter killer."""

    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return
    try:
        if pgid == os.getpgrp():
            os.kill(process.pid, signal.SIGKILL)
        else:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return


def _frame_subset(pattern: Any, frame: Any) -> bool:
    """True when every key in ``pattern`` is present in ``frame`` with the same value."""

    if isinstance(pattern, dict):
        if not isinstance(frame, dict):
            return False
        return all(key in frame and _frame_subset(value, frame[key]) for key, value in pattern.items())
    if isinstance(pattern, list):
        return isinstance(frame, list) and len(pattern) == len(frame) and all(
            _frame_subset(item, frame[index]) for index, item in enumerate(pattern)
        )
    return pattern == frame and type(pattern) is type(frame)


class _DeniedFileAndData(urllib.request.FileHandler, urllib.request.DataHandler):
    """Stand-ins that make ``build_opener`` omit the default file: and data: handlers."""

    def file_open(self, request: urllib.request.Request) -> None:
        raise urllib.error.URLError(f"file URLs are not permitted: {request.full_url}")

    def data_open(self, request: urllib.request.Request) -> None:
        raise urllib.error.URLError(f"data URLs are not permitted: {request.full_url}")


# Same handlers as ``urlopen`` except file: and data:, which that call would allow.
_HTTP_OPENER = urllib.request.build_opener(_DeniedFileAndData)


class ProbeError(Exception):
    """A loopback WorkService read failed."""


_TURN_TYPES = frozenset({"agent_start", "turn_start"})


def _inbound_error_text(frame: Mapping[str, Any], kind: str) -> str:
    for field in ("error", "message", "reason"):
        value = frame.get(field)
        if isinstance(value, str) and value.strip() != "":
            return value
    return kind


def _notify_error_text(frame: Mapping[str, Any]) -> str | None:
    """The text of a ``notify`` whose ``notifyType`` is ``error``, else None."""

    if frame.get("method") != "notify" or frame.get("notifyType") != "error":
        return None
    message = frame.get("message")
    if isinstance(message, str) and message.strip() != "":
        return message
    title = frame.get("title")
    if isinstance(title, str) and title.strip() != "":
        return title
    return "notify"


class _Transcript:
    """Log every RPC frame, and derive the grader's semantic decision records.

    ``rpc-transcript.jsonl`` holds every frame in both directions. The grader
    reads ``transcript.jsonl`` instead: for each ``work`` tool result the host
    refused it appends one ``{"decision": <action>, "refused": true,
    "expected_revision_id": <id or null>, "text": <tool text>}`` record, so
    f1's ``transcript_count`` rule counts refusals, not frames.
    """

    def __init__(
        self,
        evidence: EvidenceWriter,
        on_inbound: Callable[[Any], None] | None = None,
    ) -> None:
        self._evidence = evidence
        self._on_inbound = on_inbound
        self._lock = threading.Lock()
        self._closed = False
        self._work_actions: dict[str, Any] = {}

    def write(self, direction: str, frame: Any) -> None:
        callback: Callable[[Any], None] | None = None
        with self._lock:
            if self._closed:
                return
            self._evidence.append_jsonl(RPC_TRANSCRIPT, {"direction": direction, "frame": frame})
            if direction != "in":
                return
            self._observe(frame)
            callback = self._on_inbound
        if callback is not None:
            callback(frame)

    def _observe(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "tool_execution_start" and frame.get("toolName") == _WORK_TOOL:
            call_id = frame.get("toolCallId")
            if isinstance(call_id, str):
                self._work_actions[call_id] = frame.get("args")
            return
        if kind != "tool_execution_end" or frame.get("toolName") != _WORK_TOOL:
            return
        call_id = frame.get("toolCallId")
        args = self._work_actions.pop(call_id, None) if isinstance(call_id, str) else None
        record = _refusal_record(args if isinstance(args, dict) else {}, frame)
        if record is not None:
            self._evidence.append_jsonl(TRANSCRIPT, record)

    def close(self) -> None:
        with self._lock:
            self._closed = True


def _tool_text(result: Any) -> str:
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return ""
    parts = [block.get("text") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return "\n".join(parts)


def _refusal_record(args: Mapping[str, Any], end: Mapping[str, Any]) -> dict[str, Any] | None:
    """One grader transcript record when the host refused a ``work`` call.

    The host's ``deny`` shape is ``result.details.success is False`` (``okText``
    sets ``success: true``). Within that, only a refusal counts: a first-phase
    confirmation *preview* (``CONFIRM REQUIRED …``) writes nothing and is not a
    decision, so f1's pinned count stays 1. An infrastructure failure (the
    extension never loaded) fails without ``success`` and is likewise excluded.
    """

    details = end.get("result", {}).get("details") if isinstance(end.get("result"), dict) else None
    success = details.get("success") if isinstance(details, dict) else None
    text = _tool_text(end.get("result"))
    if success is not False or text.startswith(_PREVIEW_PREFIX):
        return None
    expected = args.get("expected_revision_id")
    return {
        "decision": args.get("action"),
        "refused": True,
        "expected_revision_id": expected if isinstance(expected, str) else None,
        "text": text,
    }


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
    def __init__(
        self,
        transcript: _Transcript,
        on_outbound: Callable[[subprocess.Popen[str], dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._transcript = transcript
        self._on_outbound = on_outbound

    def _write_json(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        self._transcript.write("out", payload)
        super()._write_json(process, payload)
        if self._on_outbound is not None:
            self._on_outbound(process, payload)

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

    def stop(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is not None:
                if self._stdout_thread is not None:
                    self._stdout_thread.join(timeout=2.0)
            else:
                try:
                    process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                if self._stdout_thread is not None and process.poll() is not None:
                    self._stdout_thread.join(timeout=2.0)
        super().stop()


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
        self.last_failure: str | None = None

    def ready(self) -> bool:
        self.last_failure = None
        try:
            document = self._get("/v1/health/ready", auth=False)
        except ProbeError as exc:
            self.last_failure = str(exc) or type(exc).__name__
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
            with _HTTP_OPENER.open(request, timeout=_HTTP_TIMEOUT_S) as response:
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


def _read_host_session(session_file: str) -> bytes:
    """Read the session log from the host filesystem."""

    path = Path(session_file)
    if path.is_file():
        return path.read_bytes()
    return b""


def _startup_log_path(reason: str) -> str | None:
    """Absolute omp log path named by a startup failure, or None."""

    match = _STARTUP_LOG_RE.search(reason)
    if match is None:
        return None
    path = match.group(1)
    if not path.startswith("/"):
        return None
    return path


class RpcAdapter:
    """One scenario, one ``prompt``, then the authoritative service readback.

    An error event, or a notify whose notifyType is error, before the first
    agent turn is the trial result: outcome ``harness_error`` and that text
    as the reason.
    """

    def __init__(
        self,
        command: Sequence[str],
        cwd: str | Path | None,
        env: Mapping[str, str] | None,
        probe: ServiceProbe,
        evidence: EvidenceWriter,
        scenario: Scenario,
        ui_script: UiScript | str | Path | None = None,
        killer: Callable[[subprocess.Popen[str]], None] | None = None,
        *,
        session_reader: Callable[[str], bytes] | None = None,
        before_seal: Callable[[EvidenceWriter], None] | None = None,
        ready_timeout_s: float = _READY_TIMEOUT_S,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty argv")
        self.command = tuple(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.probe = probe
        self.evidence = evidence
        self.scenario = scenario
        if ui_script is None:
            self.ui_script = UiScript.from_rules(scenario.ui_script)
        elif isinstance(ui_script, UiScript):
            self.ui_script = ui_script
        else:
            self.ui_script = UiScript.load(ui_script)
        self.killer = kill_process_group if killer is None else killer
        self.session_reader: Callable[[str], bytes] = (
            _read_host_session if session_reader is None else session_reader
        )
        self.before_seal = before_seal
        self.ready_timeout_s = ready_timeout_s

    def run(self) -> str:
        """Return ``harness_error``, ``timeout``, ``blocked``, or the terminal readback value."""

        self._reset_run()
        transcript = _Transcript(self.evidence, on_inbound=self._on_inbound)
        self._transcript = transcript
        client: _LoggingClient | None = None
        prompts_sent = 0
        session_id: str | None = None
        session_file: str | None = None
        readback: dict[str, Any] | None = None
        outcome = "harness_error"
        reason = "rpc client did not start"
        try:
            client = self._open(self.command)
            state = client.get_state()
            session_id = state.session_id
            session_file = state.session_file
            self._session_id = session_id
            self._session_file = session_file
            if session_id == "" or session_file is None or session_file == "":
                reason = "get_state did not return a session id and file"
            else:
                ready_reason = self._wait_until_ready()
                if ready_reason is not None:
                    reason = ready_reason
                else:
                    early = self._pre_turn_failure()
                    if early is not None:
                        outcome, reason, readback = early
                    else:
                        prompts_sent = 1
                        self._send_prompt(client)
                        outcome, reason, readback = self._after_prompt()
                        client = self._client
        except Exception as exc:
            outcome = "harness_error"
            reason = str(exc) or type(exc).__name__
            client = self._client
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

    def _reset_run(self) -> None:
        self._client: _LoggingClient | None = None
        self._transcript: _Transcript | None = None
        self._session_id: str | None = None
        self._session_file: str | None = None
        self._restarts: list[dict[str, str | None]] = []
        self._killed = threading.Event()
        self._blocked = threading.Event()
        self._block_reason = ""
        self._kill_armed = self.scenario.kill_at is not None
        self._restarted = False
        self._deadline: float | None = None
        self._timeout_s = _DEFAULT_TIMEOUT_S
        self._last: dict[str, Any] | None = None
        self._ui_lock = threading.Lock()
        self._ui_closed = False
        self._turn_lock = threading.Lock()
        self._agent_turn_seen = False
        self._early_error: str | None = None
        self._early_error_event = threading.Event()

    def _open(self, command: Sequence[str]) -> _LoggingClient:
        if self._transcript is None:
            raise RuntimeError("rpc transcript is not open")
        client = _LoggingClient(
            self._transcript,
            self._on_outbound,
            command=command,
            cwd=self.cwd,
            env=self.env,
            startup_timeout=_RPC_TIMEOUT_S,
            request_timeout=_RPC_TIMEOUT_S,
        )
        client.on_ui_request(lambda request, bound=client: self._on_ui(bound, request))
        self._client = client
        client.start()
        return client

    def _wait_until_ready(self) -> str | None:
        """Return None when the probe is ready, else the outcome reason."""

        deadline = time.monotonic() + self.ready_timeout_s
        reason = "service not ready"
        while True:
            early = self._pre_turn_failure()
            if early is not None:
                return early[1]
            attempt = self._ready_attempt()
            if attempt is None:
                return None
            reason = attempt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return reason
            time.sleep(min(_POLL_INTERVAL_S, remaining))

    def _ready_attempt(self) -> str | None:
        """None when this probe read is ready. Otherwise the reason so far."""

        try:
            if self.probe.ready():
                return None
        except ProbeError as exc:
            text = str(exc) or type(exc).__name__
            return f"service not ready: {text}"
        detail = self.probe.last_failure
        if isinstance(detail, str) and detail != "":
            return f"service not ready: {detail}"
        return "service not ready"

    def _send_prompt(self, client: _LoggingClient) -> None:
        try:
            client.prompt(self.scenario.command)
        except Exception:
            if not self._killed.is_set():
                raise

    def _on_inbound(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if not isinstance(kind, str):
            return
        text: str | None = None
        with self._turn_lock:
            if kind in _TURN_TYPES:
                self._agent_turn_seen = True
                return
            if self._agent_turn_seen or self._early_error is not None:
                return
            if kind == "extension_ui_request":
                text = _notify_error_text(frame)
                if text is None:
                    return
            elif kind != "error" and not kind.endswith("_error"):
                return
            else:
                text = _inbound_error_text(frame, kind)
            self._early_error = text
        if text is not None:
            self._early_error_event.set()

    def _pre_turn_failure(self) -> tuple[str, str, dict[str, Any] | None] | None:
        if not self._early_error_event.is_set():
            return None
        with self._turn_lock:
            text = self._early_error
        if text is None:
            return None
        return "harness_error", text, self._last

    def _after_prompt(self) -> tuple[str, str, dict[str, Any] | None]:
        early = self._pre_turn_failure()
        if early is not None:
            return early
        if self._blocked.is_set():
            return "blocked", self._block_reason, self._last
        if self._killed.is_set() and not self._restarted:
            return self._restart_and_wait()
        return self._wait_for_terminal()

    def _on_outbound(self, process: subprocess.Popen[str], frame: dict[str, Any]) -> None:
        if not self._kill_armed or self.scenario.kill_at is None:
            return
        if not _frame_subset(self.scenario.kill_at.match, frame):
            return
        self._kill_armed = False
        self._killed.set()
        self.killer(process)

    def _on_ui(self, client: _LoggingClient, request: omp_rpc.ExtensionUiRequest) -> None:
        if not request.requires_response():
            self._log_ui(request, "recorded", None)
            return
        rule = self.ui_script.match(request.method, request.title)
        if rule is None:
            self._log_ui(request, "unscripted", None)
            client.cancel_ui_request(request.id)
            if not self._blocked.is_set():
                title = request.title or ""
                self._block_reason = f'unscripted {request.method} "{title}": add a ui-script rule'
                self._blocked.set()
            return
        self._log_ui(request, "answered", rule["answer"])
        self._send_scripted(client, request.id, rule["answer"])

    def _send_scripted(self, client: _LoggingClient, request_id: str, answer: Mapping[str, Any]) -> None:
        if answer.get("cancel") is True:
            client.cancel_ui_request(request_id)
            return
        if "confirmed" in answer:
            client.send_ui_confirmation(request_id, bool(answer["confirmed"]))
            return
        client.send_ui_value(request_id, str(answer["value"]))

    def _log_ui(self, request: omp_rpc.ExtensionUiRequest, status: str, answer: Mapping[str, Any] | None) -> None:
        row: dict[str, Any] = {
            "id": request.id,
            "method": request.method,
            "status": status,
            "title": request.title,
        }
        if answer is not None:
            row["answer"] = dict(answer)
        with self._ui_lock:
            if self._ui_closed:
                return
            self.evidence.append_jsonl(UI_ANSWERS, row)

    def _restart_and_wait(self) -> tuple[str, str, dict[str, Any] | None]:
        # Resume the same session file. Do not send the prompt again.
        early = self._pre_turn_failure()
        if early is not None:
            return early
        self._restarted = True
        self._kill_armed = False
        old = self._client
        if old is not None:
            old.stop()
        early = self._pre_turn_failure()
        if early is not None:
            return early
        if not self._session_file:
            return "harness_error", "restart requires a session file", self._last
        client = self._open((*self.command, "--session", self._session_file))
        state = client.get_state()
        self._restarts.append({"id": state.session_id, "file": state.session_file})
        if state.session_id != self._session_id:
            return "harness_error", "restart session id differs from the original", self._last
        return self._wait_for_terminal()

    def _wait_for_terminal(self) -> tuple[str, str, dict[str, Any] | None]:
        if self._deadline is None:
            self._timeout_s = self.scenario.timeout_s if self.scenario.timeout_s is not None else _DEFAULT_TIMEOUT_S
            self._deadline = time.monotonic() + self._timeout_s
        pointer = self.scenario.terminal.pointer
        accepted = self.scenario.terminal.accepted
        while True:
            early = self._pre_turn_failure()
            if early is not None:
                return early
            if self._blocked.is_set():
                return "blocked", self._block_reason, self._last
            if self._killed.is_set() and not self._restarted:
                return self._restart_and_wait()
            try:
                document = self.probe.read()
            except ProbeError:
                document = None
            if document is not None:
                self._last = document
                try:
                    value = resolve_pointer(document, pointer)
                except (KeyError, TypeError, ValueError):
                    value = _MISSING
                if self._blocked.is_set():
                    return "blocked", self._block_reason, self._last
                if self._killed.is_set() and not self._restarted:
                    return self._restart_and_wait()
                if value is not _MISSING and _matches(value, accepted):
                    outcome = value if isinstance(value, str) else "completed"
                    return outcome, "terminal", self._last
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", f"timed out after {self._timeout_s}s waiting for {pointer}", self._last
            self._early_error_event.wait(min(_POLL_INTERVAL_S, remaining))

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
        with self._ui_lock:
            self._ui_closed = True
        transcript.close()
        try:
            self._capture_startup_log(reason)
            self.evidence.write_json(SERVICE_READBACK, readback if readback is not None else {})
            session_document: dict[str, Any] = {"id": session_id, "file": session_file}
            if self._restarts:
                session_document["restarts"] = list(self._restarts)
            self.evidence.write_json(SESSION_NAME, session_document)
            payload = b""
            hook_raised = False
            session_read_error: str | None = None
            if session_file:
                try:
                    read_bytes = self.session_reader(session_file)
                    if not isinstance(read_bytes, bytes):
                        raise TypeError(f"session_reader must return bytes, got {type(read_bytes).__name__}")
                    payload = read_bytes
                except Exception as exc:
                    hook_raised = True
                    msg = str(exc) or type(exc).__name__
                    if outcome == "harness_error":
                        session_read_error = f"session_reader: {msg}"
                    else:
                        outcome = "harness_error"
                        reason = f"session_reader: {msg}"
                    payload = b""
            self.evidence.add_file(SESSION_LOG, payload)
            if not hook_raised and self.before_seal is not None:
                try:
                    self.before_seal(self.evidence)
                except Exception as exc:
                    outcome = "harness_error"
                    msg = str(exc) or type(exc).__name__
                    reason = f"before_seal: {msg}"
            outcome_document: dict[str, Any] = {
                "outcome": outcome,
                "reason": reason,
                "prompts_sent": prompts_sent,
            }
            if session_read_error is not None:
                outcome_document["session_read_error"] = session_read_error
            self.evidence.write_json(OUTCOME, outcome_document)
            self.evidence.seal()
        finally:
            if client is not None:
                client.stop()
        return outcome

    def _capture_startup_log(self, reason: str) -> None:
        """Copy the worker omp log named in a startup failure into the evidence directory.

        A missing log or a reader error leaves the startup reason unchanged.
        ``session_reader`` is the same hook the trial uses to read worker files.
        """

        path = _startup_log_path(reason)
        if path is None:
            return
        try:
            payload = self.session_reader(path)
        except Exception:  # noqa: BLE001 - a reader failure must not replace the startup reason
            return
        if not isinstance(payload, bytes) or payload == b"":
            return
        self.evidence.add_file(_STARTUP_LOG_NAME, payload)
