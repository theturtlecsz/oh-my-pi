"""Loopback ingress edge gate (OMP-490).

``omp-work ingress serve`` binds 127.0.0.1 and forwards a small allowlist of
client-contract operations to one upstream work service on loopback. The gate
holds the configured workspace id, so a request that names any other workspace,
any other path, any query string, or a body over 64 KiB never reaches the
upstream. It forwards only the Authorization, X-OMP-Contract-SHA256,
Content-Type, and Accept headers, relays the upstream status, body, and content
type, answers 502 when the upstream is unreachable, and writes one stderr line
per request. The line names the caller as a truncated hash of the bearer, never
the bearer itself, and never any header or body content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import load_contract
from .v1.models import ClientOperation

__all__ = [
    "DEFAULT_OPERATIONS",
    "IngressConfig",
    "add_parser",
    "create_ingress_app",
    "load_ingress_config",
    "run",
]

DEFAULT_OPERATIONS: tuple[str, ...] = (
    "project.list",
    "project.context",
    "project.status",
    "project.decisions",
    "mission.status",
    "stop.status",
    "mission.pause",
    "stop.engage",
)

_MAX_BODY = 64 * 1024
_TIMEOUT = 10.0
_FORWARD_HEADERS = frozenset(
    (b"authorization", b"x-omp-contract-sha256", b"content-type", b"accept")
)
_PARAM = "[A-Za-z0-9_-]{1,128}"


@dataclass(frozen=True)
class IngressConfig:
    """The workspace the gate serves and the operations it lets pass."""

    workspace_id: uuid.UUID
    operations: tuple[str, ...]


def _contract_operations() -> dict[str, ClientOperation]:
    return {op.name: op for op in load_contract().client_contract.operations}


def load_ingress_config(path: str | Path) -> IngressConfig:
    """Read and validate an ingress config file.

    The file holds ``{"workspace_id": UUID, "operations": [names]}``. The
    ``operations`` key is optional and defaults to :data:`DEFAULT_OPERATIONS`.
    A missing or malformed workspace id, a name the client contract does not
    declare, and an empty operations list each raise ``ValueError``.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read ingress config: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("ingress config must be a JSON object")
    raw_id = raw.get("workspace_id")
    try:
        workspace_id = uuid.UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"invalid workspace_id: {raw_id!r}") from error
    known = _contract_operations()
    raw_operations = raw.get("operations")
    if raw_operations is None:
        names = DEFAULT_OPERATIONS
    else:
        if not isinstance(raw_operations, list) or not raw_operations:
            raise ValueError("operations must be a non-empty list")
        for name in raw_operations:
            if not isinstance(name, str) or name not in known:
                raise ValueError(f"unknown operation: {name!r}")
        names = tuple(raw_operations)
    return IngressConfig(workspace_id=workspace_id, operations=tuple(names))


def _compile_path(path: str) -> re.Pattern[str]:
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "{workspace_id}":
            segments.append(f"({_PARAM})")
        elif segment.startswith("{") and segment.endswith("}"):
            segments.append(_PARAM)
        else:
            segments.append(re.escape(segment))
    return re.compile("^" + "/".join(segments) + "$")


def _caller(scope: dict[str, object]) -> str:
    for key, value in scope.get("headers", ()):  # type: ignore[union-attr]
        if key == b"authorization":
            return hashlib.sha256(value).hexdigest()[:12]
    return "-"


def _log(method: str, path: str, operation: str, caller: str, status: int) -> None:
    sys.stderr.write(
        f"ingress method={method} path={path} operation={operation or '-'} "
        f"caller={caller} status={status}\n"
    )


async def _read_body(receive: object) -> tuple[bytes, bool]:
    """Read the request body. Return ``(body, too_large)``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()  # type: ignore[operator]
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > _MAX_BODY:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks), False


class _Ingress:
    def __init__(
        self,
        config: IngressConfig,
        upstream: str,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self._config = config
        self._upstream = upstream
        self._transport = transport
        patterns: list[tuple[str, str, re.Pattern[str]]] = []
        known = _contract_operations()
        for name in config.operations:
            operation = known[name]
            patterns.append((name, operation.method, _compile_path(operation.path)))
        self._patterns = tuple(patterns)
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _match(self, method: str, path: str) -> str | None:
        if ".." in path.split("/"):
            return None
        for name, pattern_method, pattern in self._patterns:
            if pattern_method != method:
                continue
            matched = pattern.match(path)
            if matched is None:
                continue
            try:
                if uuid.UUID(matched.group(1)) != self._config.workspace_id:
                    continue
            except (ValueError, IndexError):
                continue
            return name
        return None

    async def _client_for_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            client = httpx.AsyncClient(
                base_url=self._upstream,
                transport=self._transport,
                timeout=_TIMEOUT,
            )
            client.headers.clear()
            self._client = client
            self._loop = loop
        return self._client

    async def _forward(
        self, method: str, path: str, scope: dict[str, object], body: bytes
    ) -> httpx.Response:
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", ())  # type: ignore[union-attr]
            if key.lower() in _FORWARD_HEADERS
        }
        client = await self._client_for_loop()
        content = body if method == "POST" else None
        return await client.request(method, path, headers=headers, content=content)

    async def _respond(
        self, send: object, status: int, headers: list[tuple[bytes, bytes]], body: bytes
    ) -> None:
        await send(  # type: ignore[operator]
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})  # type: ignore[operator]

    async def _refuse(self, send: object, status: int, code: str) -> None:
        body = json.dumps({"error": {"code": code}}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"x-omp-ingress", b"refused"),
        ]
        await self._respond(send, status, headers, body)

    async def _handle(self, scope: dict[str, object], receive: object, send: object) -> None:
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        caller = _caller(scope)
        if scope.get("query_string"):
            _log(method, path, "", caller, 404)
            await self._refuse(send, 404, "not_found")
            return
        operation = self._match(method, path)
        if operation is None:
            _log(method, path, "", caller, 404)
            await self._refuse(send, 404, "not_found")
            return
        body, too_large = await _read_body(receive)
        if too_large:
            _log(method, path, operation, caller, 413)
            await self._refuse(send, 413, "payload_too_large")
            return
        try:
            response = await self._forward(method, path, scope, body)
        except httpx.HTTPError:
            _log(method, path, operation, caller, 502)
            await self._refuse(send, 502, "upstream_unavailable")
            return
        content_type = response.headers.get("content-type")
        headers = (
            [(b"content-type", content_type.encode("latin-1"))]
            if content_type is not None
            else []
        )
        _log(method, path, operation, caller, response.status_code)
        await self._respond(send, response.status_code, headers, response.content)

    async def _lifespan(self, receive: object, send: object) -> None:
        while True:
            message = await receive()  # type: ignore[operator]
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})  # type: ignore[operator]
            elif message["type"] == "lifespan.shutdown":
                if self._client is not None:
                    await self._client.aclose()
                    self._client = None
                await send({"type": "lifespan.shutdown.complete"})  # type: ignore[operator]
                return

    async def __call__(
        self, scope: dict[str, object], receive: object, send: object
    ) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._lifespan(receive, send)
        elif kind == "http":
            await self._handle(scope, receive, send)


def create_ingress_app(
    config: IngressConfig,
    upstream: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> _Ingress:
    """Build the ASGI ingress app for ``config`` in front of ``upstream``."""
    return _Ingress(config, upstream, transport)


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser("ingress")
    commands = parser.add_subparsers(dest="ingress_command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--config", required=True, type=Path)
    serve_parser.add_argument("--port", type=int, default=54323)
    serve_parser.add_argument("--upstream", default="http://127.0.0.1:54322")


def run(args: argparse.Namespace) -> int:
    """Run ``ingress serve``. The gate always binds loopback."""
    if getattr(args, "ingress_command", None) != "serve":
        return 2
    try:
        config = load_ingress_config(args.config)
    except ValueError as error:
        print(f"ingress: {error}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(
        create_ingress_app(config, args.upstream),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )
    return 0
