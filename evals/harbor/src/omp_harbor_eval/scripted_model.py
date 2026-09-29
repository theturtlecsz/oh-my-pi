"""Scriptable OpenAI-compatible model server for Harbor eval scenarios.

Serves one scripted turn per ``POST /v1/chat/completions`` request on a
loopback host, so an eval can drive the agent with a fixed model transcript.
The script is a JSON array of steps, each ``{"text": str}`` or
``{"tool_calls": [{"name", "arguments", "id"?, "resolved"?}]}``. Every request
appends one JSONL line to the log; a request after the last step gets HTTP 500
``{"error": "script_exhausted"}`` and any other path gets 404.

A call may declare ``"resolved": "confirmation_id"``: its ``arguments`` then
carry the ``$confirmation_id`` placeholder, and the server substitutes the id
from the confirmation preview the host minted in the immediately preceding
turn (the last ``confirmation_id:`` in a ``tool`` message). The workflow host
mints confirmation ids as random single-use values, so a scripted model can
only confirm a write with the id its own preview returned — never a fixed
string.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple

from .adapter import _LOOPBACK_HOSTS

_CHAT_PATH = "/v1/chat/completions"
_NOT_FOUND = json.dumps({"error": "not_found"}).encode("utf-8")
# Value a scripted confirm call carries until the preceding preview's id fills it.
CONFIRMATION_ID_PLACEHOLDER = "$confirmation_id"
_RESOLVED_CONFIRMATION_ID = "confirmation_id"
_PREVIEW_MARKER = "confirmation_id:"
_ID_CHARS = frozenset("0123456789abcdef-")


class ScriptedResponse(NamedTuple):
    status: int
    content_type: str
    payload: bytes
    step: int | None


def _load_script(script_path: str | Path) -> tuple[dict[str, Any], ...]:
    document = json.loads(Path(script_path).read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise ValueError("script must be a JSON array of steps")
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(document):
        if not isinstance(raw, dict):
            raise ValueError(f"script step {index} must be a JSON object")
        if "hold_s" in raw:
            hold_s = raw["hold_s"]
            if (
                isinstance(hold_s, bool)
                or not isinstance(hold_s, (int, float))
                or hold_s < 0
                or not math.isfinite(hold_s)
            ):
                raise ValueError(
                    f"script step {index} hold_s must be a non-negative number, got {hold_s!r}"
                )
        if "tool_calls" in raw:
            calls = raw["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise ValueError(f"script step {index} tool_calls must be a non-empty list")
            for call_index, call in enumerate(calls):
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    raise ValueError(f"script step {index} tool_calls[{call_index}] needs a name")
                if "arguments" not in call:
                    raise ValueError(f"script step {index} tool_calls[{call_index}] needs arguments")
                if "id" in call and not isinstance(call["id"], str):
                    raise ValueError(f"script step {index} tool_calls[{call_index}] id must be a string")
                if "resolved" in call and call["resolved"] != _RESOLVED_CONFIRMATION_ID:
                    raise ValueError(
                        f"script step {index} tool_calls[{call_index}] resolved must be "
                        f'"{_RESOLVED_CONFIRMATION_ID}"'
                    )
        elif not isinstance(raw.get("text"), str):
            raise ValueError(f"script step {index} must have text or tool_calls")
        steps.append(raw)
    return tuple(steps)


def _text_content(content: Any) -> str | None:
    """Plain text of a chat message body: a string, or text blocks joined."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        block["text"]
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts) if parts else None


def _preceding_confirmation_id(body: Any) -> str | None:
    """The confirmation id the host minted in the turn just before this request.

    Walks the request's ``messages`` and returns the last ``confirmation_id:``
    value in a tool result, which is the preview the agent just executed.
    """

    if not isinstance(body, Mapping):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    found: str | None = None
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        content = _text_content(message.get("content"))
        if not content or _PREVIEW_MARKER not in content:
            continue
        for segment in content.split(_PREVIEW_MARKER)[1:]:
            token = segment.lstrip().split(maxsplit=1)[0] if segment.strip() else ""
            candidate = token.rstrip(".,;:\"'")
            if candidate and all(char in _ID_CHARS for char in candidate):
                found = candidate
    return found


def _resolve_arguments(arguments: Any, resolved: Any, confirmation_id: str | None) -> Any:
    """Replace the ``$confirmation_id`` token when a call declares resolution.

    A call that declares ``"resolved": "confirmation_id"`` and reaches this
    point with no preceding preview is an error: the fixture would otherwise
    emit the placeholder verbatim and silently confirm nothing.
    """

    if resolved != _RESOLVED_CONFIRMATION_ID:
        return arguments
    if confirmation_id is None:
        raise ValueError(
            "scripted call declares resolved confirmation_id but the request carries no preceding confirmation preview"
        )
    if isinstance(arguments, str):
        return arguments.replace(CONFIRMATION_ID_PLACEHOLDER, confirmation_id)
    if isinstance(arguments, Mapping):
        return json.loads(json.dumps(arguments).replace(CONFIRMATION_ID_PLACEHOLDER, confirmation_id))
    return arguments


def _arguments_json(arguments: Any) -> str:
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


def _delta(step_index: int, step: Mapping[str, Any], confirmation_id: str | None = None) -> dict[str, Any]:
    if "tool_calls" in step:
        return {
            "tool_calls": [
                {
                    "index": index,
                    "id": call.get("id", f"scripted-{step_index}-{index}"),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": _arguments_json(
                            _resolve_arguments(call["arguments"], call.get("resolved"), confirmation_id)
                        ),
                    },
                }
                for index, call in enumerate(step["tool_calls"])
            ]
        }
    return {"content": step["text"]}


def _frames(step_index: int, step: Mapping[str, Any], confirmation_id: str | None = None) -> bytes:
    delta = _delta(step_index, step, confirmation_id)
    packet = {
        "id": "scripted-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "scripted",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    finish = {
        **packet,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls" if "tool_calls" in delta else "stop",
            }
        ],
    }
    return f"data: {json.dumps(packet)}\n\ndata: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode("utf-8")


def _decode_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


class _ScriptedHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        _exc_type, exc_value, _ = sys.exc_info()
        if isinstance(exc_value, (BrokenPipeError, ConnectionResetError, OSError)):
            return
        super().handle_error(request, client_address)


def models_yml(base_url: str) -> str:
    """Provider ``scripted`` pointed at one keyless, zero-cost model."""

    return (
        "providers:\n"
        "  scripted:\n"
        f"    baseUrl: {base_url}\n"
        "    api: openai-completions\n"
        "    auth: none\n"
        "    models:\n"
        "      - id: scripted\n"
        "        name: Scripted\n"
        "        cost:\n"
        "          input: 0\n"
        "          output: 0\n"
        "          cacheRead: 0\n"
        "          cacheWrite: 0\n"
    )


class ScriptedModelServer:
    """Replay ``script_path`` one step per chat completion, logging each request."""

    def __init__(
        self,
        script_path: str | Path,
        log_path: str | Path,
        host: str = "127.0.0.1",
        *,
        port: int = 0,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(f"host must be a loopback origin {sorted(_LOOPBACK_HOSTS)}, got {host!r}")
        if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
            raise ValueError(f"port must be an integer in 0..65535, got {port!r}")
        self.script_path = Path(script_path)
        self.log_path = Path(log_path)
        self.host = host
        self.port = port
        self.steps = _load_script(self.script_path)
        self.base_url = ""
        self._index = 0
        self._ordinal = 0
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ScriptedModelServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def start(self) -> str:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    service._handle(self)
                except (BrokenPipeError, ConnectionResetError, socket.error, OSError):
                    pass

            def do_GET(self) -> None:  # noqa: N802
                try:
                    service._handle(self)
                except (BrokenPipeError, ConnectionResetError, socket.error, OSError):
                    pass

            def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
                return

        server_class: type[ThreadingHTTPServer] = _ScriptedHTTPServer
        if self.host == "::1":

            class _IPv6Server(_ScriptedHTTPServer):
                address_family = socket.AF_INET6

            server_class = _IPv6Server

        self._httpd = server_class((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        host = f"[{self.host}]" if ":" in self.host else self.host
        self.base_url = f"http://{host}:{self.port}/v1"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="scripted-model", daemon=True
        )
        self._thread.start()
        return self.base_url

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def close(self) -> None:
        httpd = self._httpd
        thread = self._thread
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def respond(self, body: Any) -> ScriptedResponse:
        """Next step, or 500 ``script_exhausted`` once the script runs out."""

        with self._lock:
            index = self._index
            if index >= len(self.steps):
                return ScriptedResponse(
                    500,
                    "application/json",
                    json.dumps({"error": "script_exhausted"}).encode("utf-8"),
                    None,
                )
            step = self.steps[index]
            self._index = index + 1
        hold_s = step.get("hold_s", 0)
        if hold_s > 0:
            time.sleep(hold_s)
        try:
            frames = _frames(index, step, _preceding_confirmation_id(body))
        except ValueError as exc:
            return ScriptedResponse(500, "application/json", json.dumps({"error": str(exc)}).encode("utf-8"), index)
        return ScriptedResponse(200, "text/event-stream", frames, index)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
            raw = handler.rfile.read(length) if length > 0 else b""
        except (BrokenPipeError, ConnectionResetError, socket.error, OSError):
            return
        body = _decode_body(raw)
        path = urllib.parse.urlsplit(handler.path).path
        with self._lock:
            self._ordinal += 1
            ordinal = self._ordinal
        if handler.command == "POST" and path == _CHAT_PATH:
            result = self.respond(body)
        else:
            result = ScriptedResponse(404, "application/json", _NOT_FOUND, None)
        self._log(ordinal, path, body, result)
        try:
            handler.send_response(result.status)
            handler.send_header("Content-Type", result.content_type)
            handler.send_header("Content-Length", str(len(result.payload)))
            handler.end_headers()
            handler.wfile.write(result.payload)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.error, OSError):
            pass

    def _log(self, ordinal: int, path: str, body: Any, result: ScriptedResponse) -> None:
        record = {
            "ordinal": ordinal,
            "path": path,
            "body": body,
            "step": result.step,
            "status": result.status,
        }
        line = json.dumps(record) + "\n"
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_harbor_eval.scripted_model",
        description="Serve a scripted OpenAI-compatible model on a loopback host.",
    )
    parser.add_argument("--script", required=True, help="JSON array of scripted steps")
    parser.add_argument("--log", required=True, help="JSONL request log path")
    parser.add_argument("--host", default="127.0.0.1", help="loopback host to bind")
    parser.add_argument("--port", type=int, default=0, help="port to bind (0 picks a free port)")
    args = parser.parse_args(argv)
    with ScriptedModelServer(args.script, args.log, args.host, port=args.port) as server:
        print(server.base_url, flush=True)
        server.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
