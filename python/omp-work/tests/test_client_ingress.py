"""OMP-490: loopback ingress edge gate tests.

The gate is exercised through the FastAPI test client with an in-process ASGI
upstream recorder, so the assertions cover the observable edge contract: which
requests reach the upstream, which headers they carry, which refusals never
reach it, the relayed status/body/type, and the one stderr line per request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from omp_work import load_contract
from omp_work.__main__ import main
from omp_work.client_ingress import (
    DEFAULT_OPERATIONS,
    IngressConfig,
    create_ingress_app,
    load_ingress_config,
)

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORKSPACE = UUID("22222222-2222-4222-8222-222222222222")
_TOKEN = "client-bearer-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_ALLOWED = frozenset(
    ("authorization", "x-omp-contract-sha256", "content-type", "accept")
)


class _Upstream:
    """An ASGI upstream that records every request it receives."""

    def __init__(self, status: int = 200, body: bytes = b'{"outcome":"read"}') -> None:
        self.requests: list[dict[str, object]] = []
        self._status = status
        self._body = body

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "headers": {key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]},
                "body": body,
            }
        )
        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": [(b"content-type", b"application/problem+json")],
            }
        )
        await send({"type": "http.response.body", "body": self._body})


def _gate(upstream: _Upstream) -> TestClient:
    config = IngressConfig(workspace_id=WORKSPACE, operations=DEFAULT_OPERATIONS)
    app = create_ingress_app(config, "http://upstream.test", httpx.ASGITransport(upstream))
    return TestClient(app)


def test_allowed_read_and_write_reach_upstream_with_only_gate_headers() -> None:
    upstream = _Upstream()
    sent = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-OMP-Contract-SHA256": "a" * 64,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Trace": "drop-me",
        "Cookie": "session=drop-me",
    }
    with _gate(upstream) as client:
        read = client.get(
            f"/v1/workspaces/{WORKSPACE}/client/projects", headers=sent
        )
        assert read.status_code == 200
        assert read.json() == {"outcome": "read"}

        write = client.post(
            f"/v1/workspaces/{WORKSPACE}/client/missions/{uuid4()}/pause",
            headers=sent,
            content=b'{"reason":"owner"}',
        )
        assert write.status_code == 200

    assert [request["method"] for request in upstream.requests] == ["GET", "POST"]
    first, second = upstream.requests
    assert first["path"] == f"/v1/workspaces/{WORKSPACE}/client/projects"
    assert second["path"].startswith(
        f"/v1/workspaces/{WORKSPACE}/client/missions/"
    )
    assert second["path"].endswith("/pause")
    assert second["body"] == b'{"reason":"owner"}'
    for request in upstream.requests:
        forwarded = request["headers"]
        assert set(forwarded) <= _ALLOWED | {"host", "content-length"}
        assert forwarded["authorization"] == f"Bearer {_TOKEN}"
        assert forwarded["x-omp-contract-sha256"] == "a" * 64
        assert "x-trace" not in forwarded
        assert "cookie" not in forwarded


def test_relays_upstream_status_body_and_content_type() -> None:
    upstream = _Upstream(status=409, body=b'{"error":{"code":"contract_mismatch"}}')
    with _gate(upstream) as client:
        response = client.get(
            f"/v1/workspaces/{WORKSPACE}/client/projects", headers=_AUTH
        )
    assert response.status_code == 409
    assert response.content == b'{"error":{"code":"contract_mismatch"}}'
    assert response.headers["content-type"] == "application/problem+json"
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("method,path", [
    ("POST", f"/v1/workspaces/{WORKSPACE}/client/missions/{uuid4()}/resume"),
    ("POST", "/v1/commands"),
    ("GET", "/v1/health/ready"),
    ("DELETE", f"/v1/workspaces/{WORKSPACE}/client/projects"),
    ("GET", f"/v1/workspaces/{OTHER_WORKSPACE}/client/projects"),
])
def test_refusals_never_reach_upstream(method: str, path: str) -> None:
    upstream = _Upstream()
    with _gate(upstream) as client:
        response = client.request(method, path, headers=_AUTH)
    assert response.status_code == 404
    assert response.headers["x-omp-ingress"] == "refused"
    assert response.json()["error"]["code"] == "not_found"
    assert upstream.requests == []


def test_query_string_is_refused_before_upstream() -> None:
    upstream = _Upstream()
    with _gate(upstream) as client:
        response = client.get(
            f"/v1/workspaces/{WORKSPACE}/client/projects?detail=true", headers=_AUTH
        )
    assert response.status_code == 404
    assert response.headers["x-omp-ingress"] == "refused"
    assert upstream.requests == []


def test_percent_encoded_dot_dot_segment_is_refused() -> None:
    upstream = _Upstream()
    with _gate(upstream) as client:
        response = client.get(
            f"/v1/workspaces/{WORKSPACE}/client/%2e%2e/stop", headers=_AUTH
        )
    assert response.status_code == 404
    assert response.headers["x-omp-ingress"] == "refused"
    assert upstream.requests == []


def test_body_over_64_kib_is_refused_with_413() -> None:
    upstream = _Upstream()
    oversized = b"x" * (70 * 1024)
    with _gate(upstream) as client:
        response = client.post(
            f"/v1/workspaces/{WORKSPACE}/client/missions/{uuid4()}/pause",
            headers={**_AUTH, "Content-Type": "application/json"},
            content=oversized,
        )
    assert response.status_code == 413
    assert response.headers["x-omp-ingress"] == "refused"
    assert response.json()["error"]["code"] == "payload_too_large"
    assert upstream.requests == []


def test_dead_upstream_is_502() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream is down", request=request)

    config = IngressConfig(workspace_id=WORKSPACE, operations=DEFAULT_OPERATIONS)
    app = create_ingress_app(config, "http://upstream.test", httpx.MockTransport(refuse))
    with TestClient(app) as client:
        response = client.get(
            f"/v1/workspaces/{WORKSPACE}/client/projects", headers=_AUTH
        )
    assert response.status_code == 502
    assert response.headers["x-omp-ingress"] == "refused"
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_load_config_defaults_and_rejections(tmp_path: Path) -> None:
    path = tmp_path / "ingress.json"
    path.write_text(json.dumps({"workspace_id": str(WORKSPACE)}), encoding="utf-8")
    config = load_ingress_config(path)
    assert config.workspace_id == WORKSPACE
    assert config.operations == DEFAULT_OPERATIONS
    known = {operation.name for operation in load_contract().client_contract.operations}
    assert all(name in known for name in config.operations)

    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"workspace_id": str(WORKSPACE), "operations": ["project.list"]}),
        encoding="utf-8",
    )
    assert load_ingress_config(explicit).operations == ("project.list",)

    for payload in (
        {"workspace_id": "not-a-uuid"},
        {},
        {"workspace_id": str(WORKSPACE), "operations": []},
        {"workspace_id": str(WORKSPACE), "operations": ["/v1/commands"]},
        {"workspace_id": str(WORKSPACE), "operations": ["bogus.operation"]},
    ):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_ingress_config(bad)


def test_log_line_names_operation_and_caller_hash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    upstream = _Upstream()
    with _gate(upstream) as client:
        client.get(f"/v1/workspaces/{WORKSPACE}/client/projects", headers=_AUTH)
        client.post("/v1/commands", headers=_AUTH)

    err = capsys.readouterr().err
    expected = hashlib.sha256(f"Bearer {_TOKEN}".encode()).hexdigest()[:12]
    assert (
        f"ingress method=GET path=/v1/workspaces/{WORKSPACE}/client/projects "
        f"operation=project.list caller={expected} status=200" in err
    )
    assert (
        "ingress method=POST path=/v1/commands operation=- "
        f"caller={expected} status=404" in err
    )
    assert _TOKEN not in err
    assert "Authorization" not in err


def test_missing_bearer_logs_dash_caller(
    capsys: pytest.CaptureFixture[str],
) -> None:
    upstream = _Upstream()
    with _gate(upstream) as client:
        client.get(f"/v1/workspaces/{WORKSPACE}/client/projects")
    err = capsys.readouterr().err
    assert "operation=project.list caller=- status=200" in err


def test_ingress_serve_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["ingress", "serve", "--help"])
    assert exit_info.value.code == 0
