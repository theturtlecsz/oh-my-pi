"""Tests for loopback model proxy client (OMP-417)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpcore
import httpx
import pytest

from omp_work.model_proxy import TaskRequest
from omp_work.orchestrator import model_proxy_client


def _valid_capability(
    tmp_path: Path,
    *,
    token: str = "test-control-secret",
    actor_kind: str = "automation",
    scopes: list[str] | None = None,
    workspaces: list[str] | None = None,
) -> Path:
    cap_path = tmp_path / f"capability-{uuid4()}.json"
    data = {
        "token": token,
        "actor_kind": actor_kind,
        "scopes": ["work.execute"] if scopes is None else scopes,
        "workspaces": [str(uuid4())] if workspaces is None else workspaces,
    }
    cap_path.write_text(json.dumps(data), encoding="utf-8")
    return cap_path


def _valid_payload(workspace_id: str | None = None) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id or str(uuid4()),
        "project_id": str(uuid4()),
        "mission_id": None,
        "work_id": str(uuid4()),
        "job_id": "job-alpha",
        "worker_id": "worker-1",
        "fence": 1,
        "stage": "implement",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "ceiling_usd": "1.50",
    }


def test_register_sends_control_bearer_and_every_payload_field_and_returns_descriptor(
    tmp_path: Path,
) -> None:
    token = "secret-token-xyz"
    cap_path = _valid_capability(tmp_path, token=token)
    payload = _valid_payload()

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "task-001",
                "model_token": "mt-12345",
                "descriptor": {
                    "api": "google-generative-ai",
                    "id": "gemini-2.5-flash",
                    "limits": {"context_tokens": 1000000, "max_output_tokens": 8192},
                    "baseUrl": "http://127.0.0.1:8091",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    res = model_proxy_client.register(
        cap_path,
        "http://127.0.0.1:8091",
        payload,
        transport=transport,
    )

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert req.url.path == "/tasks"
    assert req.headers["authorization"] == f"Bearer {token}"

    body = json.loads(req.content.decode("utf-8"))
    expected_fields = {
        "workspace_id",
        "project_id",
        "mission_id",
        "work_id",
        "job_id",
        "worker_id",
        "fence",
        "stage",
        "expires_at",
        "ceiling_usd",
    }
    assert set(body.keys()) == expected_fields
    assert body["workspace_id"] == payload["workspace_id"]
    assert body["project_id"] == payload["project_id"]
    assert body["mission_id"] is None
    assert body["work_id"] == payload["work_id"]
    assert body["job_id"] == "job-alpha"
    assert body["worker_id"] == "worker-1"
    assert body["fence"] == 1
    assert body["stage"] == "implement"
    assert body["ceiling_usd"] == "1.50"

    assert res == {
        "id": "task-001",
        "model_token": "mt-12345",
        "descriptor": {
            "api": "google-generative-ai",
            "id": "gemini-2.5-flash",
            "limits": {"context_tokens": 1000000, "max_output_tokens": 8192},
            "baseUrl": "http://127.0.0.1:8091",
        },
    }
    assert res["descriptor"]["id"] == "gemini-2.5-flash"


def test_register_accepts_task_request_instance(tmp_path: Path) -> None:
    cap_path = _valid_capability(tmp_path, token="cap-token")
    task_req = TaskRequest(
        workspace_id=uuid4(),
        project_id=uuid4(),
        mission_id=uuid4(),
        work_id=uuid4(),
        job_id="job-beta",
        worker_id="worker-9",
        fence=0,
        stage="implement",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        ceiling_usd=Decimal("5.00"),
    )

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "task-002",
                "model_token": "mt-99999",
                "descriptor": {
                    "api": "ollama",
                    "id": "deepseek-r1",
                    "limits": {"context_tokens": 32768, "max_output_tokens": 4096},
                    "baseUrl": "http://127.0.0.1:8091",
                },
            },
        )

    res = model_proxy_client.register(
        cap_path,
        "http://localhost:8091",
        task_req,
        transport=httpx.MockTransport(handler),
    )

    assert len(captured_requests) == 1
    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert body["job_id"] == "job-beta"
    assert body["worker_id"] == "worker-9"
    assert body["fence"] == 0
    assert body["mission_id"] == str(task_req.mission_id)
    assert res["id"] == "task-002"
    assert res["model_token"] == "mt-99999"


def test_revoke_task_sends_delete_and_surfaces_refused_and_spent_usd(
    tmp_path: Path,
) -> None:
    token = "revoke-secret-token"
    cap_path = _valid_capability(tmp_path, token=token)
    task_id = "task-to-delete-456"

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"refused": False, "spent_usd": "0.0825"})

    transport = httpx.MockTransport(handler)
    res = model_proxy_client.revoke_task(
        cap_path,
        "http://127.0.0.1:8091",
        task_id,
        transport=transport,
    )

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "DELETE"
    assert req.url.path == f"/tasks/{task_id}"
    assert req.headers["authorization"] == f"Bearer {token}"
    assert res == {"refused": False, "spent_usd": "0.0825"}
    assert res["refused"] is False
    assert res["spent_usd"] == "0.0825"


def test_revoke_task_surfaces_refused_true(tmp_path: Path) -> None:
    cap_path = _valid_capability(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refused": True, "spent_usd": "1.0000"})

    res = model_proxy_client.revoke_task(
        cap_path,
        "http://127.0.0.1:8091",
        "task-123",
        transport=httpx.MockTransport(handler),
    )
    assert res["refused"] is True
    assert res["spent_usd"] == "1.0000"


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://example.com:8091",
        "http://192.168.1.1:8091",
        "http://10.0.0.1:8091",
        "http://0.0.0.0:8091",
        "http://[::]:8091",
        "https://evil.com",
        "ftp://127.0.0.1:8091",
        "http://127.0.0.1.nip.io:8091",
        "http://user:pass@127.0.0.1:8091",
        "http://127.0.0.1:8091/extra/path",
        "http://127.0.0.1:8091?query=1",
        "",
        "   ",
        "not_a_valid_url",
    ],
)
def test_non_loopback_base_url_raises_before_any_socket(
    tmp_path: Path, bad_url: str
) -> None:
    cap_path = _valid_capability(tmp_path)
    payload = _valid_payload()

    def unreachable_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("socket attempted on invalid base_url")

    transport = httpx.MockTransport(unreachable_transport)

    with pytest.raises(ValueError):
        model_proxy_client.register(cap_path, bad_url, payload, transport=transport)

    with pytest.raises(ValueError):
        model_proxy_client.revoke_task(
            cap_path, bad_url, "task-1", transport=transport
        )


@pytest.mark.parametrize(
    "loopback_url",
    [
        "http://127.0.0.1:8091",
        "http://127.0.0.1:8091/",
        "http://localhost:8091",
        "http://[::1]:8091",
        "http://127.0.0.2:8091",
    ],
)
def test_loopback_variants_accepted(tmp_path: Path, loopback_url: str) -> None:
    cap_path = _valid_capability(tmp_path)
    payload = _valid_payload()

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "t1",
                    "model_token": "mt1",
                    "descriptor": {"id": "m1"},
                },
            )
        return httpx.Response(200, json={"refused": False, "spent_usd": "0"})

    transport = httpx.MockTransport(handler)
    res_reg = model_proxy_client.register(
        cap_path, loopback_url, payload, transport=transport
    )
    assert res_reg["id"] == "t1"

    res_rev = model_proxy_client.revoke_task(
        cap_path, loopback_url, "t1", transport=transport
    )
    assert res_rev["refused"] is False
    assert calls == ["POST", "DELETE"]


@pytest.mark.parametrize(
    ("scheme", "proxy_env"),
    [("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY"), ("http", "ALL_PROXY")],
)
def test_control_bearer_never_connects_to_environment_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str, proxy_env: str
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv(proxy_env, "http://203.0.113.10:8080")

    destinations: list[tuple[str, int]] = []

    def connect_tcp(
        self: httpcore.SyncBackend, host: str, port: int, *args: Any, **kwargs: Any
    ) -> httpcore.NetworkStream:
        destinations.append((host, port))
        raise httpcore.ConnectError("socket-free destination probe")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    cap_path = _valid_capability(tmp_path)
    base_url = f"{scheme}://127.0.0.1:8091"

    # Use real HTTPX transport selection; MockTransport disables env proxies.
    with pytest.raises(httpx.ConnectError):
        model_proxy_client.register(cap_path, base_url, _valid_payload())
    with pytest.raises(httpx.ConnectError):
        model_proxy_client.revoke_task(cap_path, base_url, "task-123")

    assert destinations == [("127.0.0.1", 8091), ("127.0.0.1", 8091)]


def test_transport_error_raises_on_register_and_revoke(tmp_path: Path) -> None:
    cap_path = _valid_capability(tmp_path)
    payload = _valid_payload()

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(failing_handler)

    with pytest.raises(httpx.TransportError):
        model_proxy_client.register(
            cap_path,
            "http://127.0.0.1:8091",
            payload,
            transport=transport,
        )

    with pytest.raises(httpx.TransportError):
        model_proxy_client.revoke_task(
            cap_path,
            "http://127.0.0.1:8091",
            "task-123",
            transport=transport,
        )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500])
def test_non_2xx_raises_http_status_error(tmp_path: Path, status_code: int) -> None:
    cap_path = _valid_capability(tmp_path)
    payload = _valid_payload()

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, json={"detail": f"error_{status_code}"}
        )

    transport = httpx.MockTransport(error_handler)

    with pytest.raises(httpx.HTTPStatusError) as exc_reg:
        model_proxy_client.register(
            cap_path,
            "http://127.0.0.1:8091",
            payload,
            transport=transport,
        )
    assert exc_reg.value.response.status_code == status_code

    with pytest.raises(httpx.HTTPStatusError) as exc_rev:
        model_proxy_client.revoke_task(
            cap_path,
            "http://127.0.0.1:8091",
            "task-123",
            transport=transport,
        )
    assert exc_rev.value.response.status_code == status_code


@pytest.mark.parametrize(
    "override",
    [
        {"token": ""},
        {"token": 12345},
        {"actor_kind": "owner"},
        {"actor_kind": "task-agent"},
        {"scopes": ["work.read"]},
        {"scopes": []},
        {"workspaces": "not-a-list"},
        {"workspaces": None},
    ],
)
def test_invalid_capability_raises(tmp_path: Path, override: dict[str, Any]) -> None:
    cap_path = tmp_path / f"cap-{uuid4()}.json"
    data = {
        "token": "valid-token",
        "actor_kind": "automation",
        "scopes": ["work.execute"],
        "workspaces": [str(uuid4())],
    }
    data.update(override)
    cap_path.write_text(json.dumps(data), encoding="utf-8")

    payload = _valid_payload()
    with pytest.raises(ValueError):
        model_proxy_client.register(cap_path, "http://127.0.0.1:8091", payload)

    with pytest.raises(ValueError):
        model_proxy_client.revoke_task(
            cap_path, "http://127.0.0.1:8091", "task-1"
        )


def test_missing_capability_file_raises(tmp_path: Path) -> None:
    non_existent = tmp_path / "does-not-exist.json"
    payload = _valid_payload()

    with pytest.raises(OSError):
        model_proxy_client.register(
            non_existent, "http://127.0.0.1:8091", payload
        )

    with pytest.raises(OSError):
        model_proxy_client.revoke_task(
            non_existent, "http://127.0.0.1:8091", "task-1"
        )


def test_invalid_payload_raises(tmp_path: Path) -> None:
    cap_path = _valid_capability(tmp_path)

    # Missing required field 'work_id'
    bad_payload_missing = _valid_payload()
    del bad_payload_missing["work_id"]
    with pytest.raises(Exception):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", bad_payload_missing
        )

    # Extra forbidden field
    bad_payload_extra = _valid_payload()
    bad_payload_extra["extra_field"] = "illegal"
    with pytest.raises(Exception):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", bad_payload_extra
        )

    # Invalid stage (must be 'implement')
    bad_payload_stage = _valid_payload()
    bad_payload_stage["stage"] = "verify"
    with pytest.raises(Exception):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", bad_payload_stage
        )

    # Negative fence
    bad_payload_fence = _valid_payload()
    bad_payload_fence["fence"] = -1
    with pytest.raises(Exception):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", bad_payload_fence
        )

    # Non-positive ceiling_usd
    bad_payload_ceiling = _valid_payload()
    bad_payload_ceiling["ceiling_usd"] = "0"
    with pytest.raises(Exception):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", bad_payload_ceiling
        )

    # Non-mapping, non-TaskRequest payload
    with pytest.raises(TypeError):
        model_proxy_client.register(
            cap_path, "http://127.0.0.1:8091", "not-a-payload"  # type: ignore[arg-type]
        )


def test_empty_task_id_raises_on_revoke(tmp_path: Path) -> None:
    cap_path = _valid_capability(tmp_path)
    with pytest.raises(ValueError):
        model_proxy_client.revoke_task(
            cap_path, "http://127.0.0.1:8091", ""
        )
    with pytest.raises(ValueError):
        model_proxy_client.revoke_task(
            cap_path, "http://127.0.0.1:8091", "   "
        )


def test_callers_can_wrap_attribute_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap_path = _valid_capability(tmp_path)
    payload = _valid_payload()

    calls: list[str] = []

    def mock_register(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("register")
        return {"id": "wrapped-id", "model_token": "wrapped-token", "descriptor": {}}

    def mock_revoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("revoke")
        return {"refused": False, "spent_usd": "0"}

    monkeypatch.setattr(model_proxy_client, "register", mock_register)
    monkeypatch.setattr(model_proxy_client, "revoke_task", mock_revoke)

    reg_out = model_proxy_client.register(cap_path, "http://127.0.0.1:8091", payload)
    rev_out = model_proxy_client.revoke_task(cap_path, "http://127.0.0.1:8091", "task-1")

    assert calls == ["register", "revoke"]
    assert reg_out["id"] == "wrapped-id"
    assert rev_out["spent_usd"] == "0"
