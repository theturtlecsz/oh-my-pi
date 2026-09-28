"""Stdin/stdout stand-in for ``omp --mode rpc``.

Speaks the ready, ``get_state``, prompt-ack, scripted-event, and
``extension_ui_request`` frames from ``docs/rpc.md``. Records every inbound
command. ``--session`` keeps the session id already stored in that file
unless ``--resume-session-id`` replaces it. At stdin EOF (and on SIGTERM,
which follows the client's stdin close) records whether ``manifest.json``
exists in ``--evidence``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

READY = {
    "type": "ready",
    "protocolVersion": 1,
    "supportedProtocolVersions": [1, 2],
    "maxFrameBytes": 1048576,
    "maxReassembledFrameBytes": 67108864,
}

_recorded = False
_args: argparse.Namespace | None = None
_eof_fd: int | None = None


def _emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _respond(request_id: object, command: str, *, success: bool, data: dict[str, Any] | None = None, error: str = "") -> None:
    frame: dict[str, Any] = {"type": "response", "command": command, "success": success}
    if isinstance(request_id, str):
        frame["id"] = request_id
    if success:
        frame["data"] = data if data is not None else {}
    else:
        frame["error"] = error
    _emit(frame)


def _record_command(command: dict[str, Any]) -> None:
    assert _args is not None
    path = Path(_args.record) / "commands.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), "command": command}, separators=(",", ":")) + "\n")
        handle.flush()


def record_shutdown() -> None:
    """Note whether the evidence manifest is already on disk."""

    global _recorded
    if _recorded or _args is None or _eof_fd is None:
        return
    exists = (Path(_args.evidence) / "manifest.json").is_file()
    payload = (json.dumps({"manifest_exists": exists}) + "\n").encode()
    os.lseek(_eof_fd, 0, os.SEEK_SET)
    os.ftruncate(_eof_fd, 0)
    os.write(_eof_fd, payload)
    os.fsync(_eof_fd)
    _recorded = True


def _on_term(_signum: int, _frame: object) -> None:
    record_shutdown()
    os._exit(0)


def _state() -> dict[str, Any]:
    assert _args is not None
    return {
        "sessionId": _args.session_id,
        "sessionFile": str(Path(_args.session_file)),
        "isStreaming": False,
        "isCompacting": False,
        "messageCount": 0,
        "queuedMessageCount": 0,
    }


def _read_session_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and document.get("type") == "session":
            session_id = document.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
    return None


def _apply_resume_identity() -> None:
    """A ``--session`` restart keeps the id in that file unless asked to drift."""

    assert _args is not None
    if not _args.session:
        return
    if _args.resume_session_id:
        _args.session_id = _args.resume_session_id
        return
    found = _read_session_id(Path(_args.session))
    if found:
        _args.session_id = found


def _record_invocation() -> None:
    assert _args is not None
    path = Path(_args.record) / "invocations.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": sys.argv}, separators=(",", ":")) + "\n")
        handle.flush()


def _handle(command: dict[str, Any], events: list[Any]) -> None:
    assert _args is not None
    kind = command.get("type")
    request_id = command.get("id")
    if kind == "negotiate_protocol":
        _respond(request_id, "negotiate_protocol", success=True, data={"protocolVersion": 2})
        return
    if kind == "get_state":
        if _args.bad_state:
            _respond(request_id, "get_state", success=False, error="bad state")
        else:
            _respond(request_id, "get_state", success=True, data=_state())
        return
    if kind == "extension_ui_response":
        return
    if kind == "prompt":
        if _args.reject_prompt:
            _respond(request_id, "prompt", success=False, error="not accepted")
            return
        _respond(request_id, "prompt", success=True, data={"agentInvoked": True})
        if _args.ack_flag:
            Path(_args.ack_flag).write_text(json.dumps({"at": time.time()}) + "\n", encoding="utf-8")
        for request in _args.ui_requests:
            if isinstance(request, dict):
                _emit(request)
        for event in events:
            if isinstance(event, dict):
                _emit(event)
        return
    _respond(request_id, str(kind), success=False, error="unsupported")


def main(argv: list[str] | None = None) -> int:
    global _args
    parser = argparse.ArgumentParser(description="Fake omp RPC server for harbor adapter tests")
    parser.add_argument("--record", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--session-id", default="sess-1")
    parser.add_argument("--events", default="")
    parser.add_argument("--ack-flag", default="")
    parser.add_argument("--no-ready", action="store_true")
    parser.add_argument("--bad-state", action="store_true")
    parser.add_argument("--reject-prompt", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--resume-session-id", default="")
    parser.add_argument("--ui-requests", default="")
    _args = parser.parse_args(argv)
    record = Path(_args.record)
    record.mkdir(parents=True, exist_ok=True)
    _record_invocation()
    _apply_resume_identity()
    session = Path(_args.session_file)
    session.parent.mkdir(parents=True, exist_ok=True)
    if not (_args.session and session.is_file()):
        session.write_bytes(f'{{"type":"session","id":"{_args.session_id}"}}\n{{"type":"note"}}\n'.encode())
    ui_path = _args.ui_requests
    ui_requests: list[Any] = []
    if ui_path:
        loaded_ui = json.loads(Path(ui_path).read_text(encoding="utf-8"))
        if not isinstance(loaded_ui, list):
            raise SystemExit("--ui-requests must be a JSON list")
        ui_requests = loaded_ui
    _args.ui_requests = ui_requests
    events: list[Any] = []
    if _args.events:
        loaded = json.loads(Path(_args.events).read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise SystemExit("--events must be a JSON list")
        events = loaded
    global _eof_fd
    _eof_fd = os.open(record / "eof.json", os.O_CREAT | os.O_RDWR, 0o644)
    signal.signal(signal.SIGTERM, _on_term)
    try:
        if not _args.no_ready:
            _emit(READY)
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                command = json.loads(stripped)
            except json.JSONDecodeError:
                _respond(None, "parse", success=False, error="malformed json")
                continue
            if not isinstance(command, dict):
                _respond(None, "parse", success=False, error="command must be an object")
                continue
            _record_command(command)
            _handle(command, events)
    finally:
        record_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
