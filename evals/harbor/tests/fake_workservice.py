"""Loopback WorkService stand-in with scripted readiness and read views.

Serves ``GET /v1/health/ready``, ``GET /v1/workspaces/{id}/execution``, and
``GET /v1/work-items/{key}`` on 127.0.0.1. ``ready``, ``execution``, and
``work_item`` are JSON objects or zero-arg callables returning one.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

JsonSource = Mapping[str, Any] | Callable[[], Mapping[str, Any]]
ReadySource = bool | Callable[[], bool]


def _call(source: JsonSource) -> Mapping[str, Any]:
    document = source() if callable(source) else source
    if not isinstance(document, Mapping):
        raise TypeError("scripted workservice view must be a JSON object")
    return document


class FakeWorkService:
    def __init__(
        self,
        *,
        bearer: str,
        workspace_id: str,
        ready: ReadySource,
        execution: JsonSource,
        work_item: JsonSource,
    ) -> None:
        self.bearer = bearer
        self.workspace_id = workspace_id
        self._ready = ready
        self._execution = execution
        self._work_item = work_item
        self.calls: list[dict[str, Any]] = []
        self.base_url = ""
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeWorkService:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def start(self) -> str:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                service._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._httpd = Server(("127.0.0.1", 0), Handler)
        port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-workservice", daemon=True)
        self._thread.start()
        return self.base_url

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        authorization = handler.headers.get("Authorization", "")
        workspace = handler.headers.get("X-OMP-Workspace-ID", "")
        authorized = authorization == f"Bearer {self.bearer}"
        with self._lock:
            status, document = self._dispatch(path, authorized, workspace)
            self.calls.append(
                {
                    "method": "GET",
                    "path": path,
                    "status": status,
                    "authorized": authorized,
                    "workspace": workspace,
                    "at": time.time(),
                    "ready": document.get("ready") if isinstance(document, Mapping) else None,
                    "state": document.get("state") if isinstance(document, Mapping) else None,
                }
            )
        body = json.dumps(document).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _dispatch(self, path: str, authorized: bool, workspace: str) -> tuple[int, dict[str, Any]]:
        if path == "/v1/health/ready":
            ready = self._ready() if callable(self._ready) else self._ready
            return 200, {"live": True, "ready": bool(ready), "alerts": [] if ready else ["not_ready"]}
        execution_path = f"/v1/workspaces/{urllib.parse.quote(self.workspace_id, safe='')}/execution"
        if path in {execution_path, f"{execution_path}/"}:
            return self._authed(self._execution, authorized, workspace)
        prefix = "/v1/work-items/"
        if path.startswith(prefix):
            return self._authed(self._work_item, authorized, workspace)
        return 404, {"error": {"code": "not_found"}}

    def _authed(self, source: JsonSource, authorized: bool, workspace: str) -> tuple[int, dict[str, Any]]:
        if not authorized or workspace != self.workspace_id:
            return 401, {"error": {"code": "unauthenticated"}}
        return 200, dict(_call(source))
