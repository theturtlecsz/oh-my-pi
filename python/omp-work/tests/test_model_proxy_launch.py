"""Model proxy launch tests (OMP-417-s08).

Proves loopback launch, CLI configuration validation, and authority boundary:
- loopback launch works with valid configuration and certificates
- non-loopback bind and unsafe key or capability permissions fail CLI launch
- task tokens cannot call control endpoints
- subprocess cleanup terminates only the child PID
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest

from omp_work.model_proxy import ProxyConfig
from omp_work.model_proxy_upstream import TransportResponse
from test_model_proxy_budget import _admitting_job, _budget_ctx
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import service

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"proxy port {port} did not open within {timeout}s")


def _generate_cert(cert_path: Path, key_path: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )


def test_model_proxy_loopback_launch_and_control_auth(service, tmp_path):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    calls = []
    entered, release = threading.Event(), threading.Event()
    release.set()

    class FakeProviderHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            calls.append((self.path, body, dict(self.headers)))
            entered.set()
            assert release.wait(15)
            if self.path.startswith("/v1beta"):
                data = (
                    b'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Done."}]},'
                    b'"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n'
                )
                content_type = "text/event-stream"
            else:
                data = b'{"message":{"role":"assistant","content":"Done."},"done":true,"prompt_eval_count":10,"eval_count":5}\n'
                content_type = "application/x-ndjson"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    _generate_cert(cert_path, key_path)

    fake_server = HTTPServer(("127.0.0.1", 0), FakeProviderHandler)
    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    fake_server.socket = ssl_ctx.wrap_socket(fake_server.socket, server_side=True)
    fake_port = fake_server.server_port
    server_thread = threading.Thread(target=fake_server.serve_forever)
    server_thread.start()

    capability_path = tmp_path / "automation.json"
    control_token = "control-secret-12345"
    capability_path.write_text(
        json.dumps(
            {
                "token": control_token,
                "actor_kind": "automation",
                "scopes": ["work.execute"],
                "actor_id": str(ctx.actor_id),
                "workspaces": [str(ctx.workspace_id)],
            }
        )
    )
    capability_path.chmod(0o600)

    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)

    proxy_port = _find_free_port()
    ops_data = {
        k: str(v) if isinstance(v, Path) else v
        for k, v in asdict(ctx.service.config).items()
    }
    config_data = {
        "operations": ops_data,
        "control_capability": str(capability_path),
        "host": "127.0.0.1",
        "port": proxy_port,
        "transports": {
            "gemini_flash": dict(
                gemini_route(
                    model="gemini-3.8-flash-high",
                    origin=f"https://localhost:{fake_port}",
                ).__dict__
            ),
            "ollama_cloud": dict(
                ollama_route(
                    model="deepseek-v4.1-flash:cloud",
                    origin=f"https://localhost:{fake_port}",
                ).__dict__
            ),
        },
    }
    proxy_config_path = tmp_path / "model-proxy.json"
    proxy_config_path.write_text(json.dumps(config_data, default=str))

    env = dict(os.environ, SSL_CERT_FILE=str(cert_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "omp_work.model_proxy", "--config", str(proxy_config_path)],
        env=env,
    )
    try:
        _wait_for_port(proxy_port, timeout=10.0)
        base_url = f"http://127.0.0.1:{proxy_port}"
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            job_id = _admitting_job(ctx, lease_seconds=300)
            task_payload = {
                "workspace_id": str(ctx.workspace_id),
                "project_id": str(ctx.project_id),
                "work_id": str(ctx.work_id),
                "job_id": job_id,
                "worker_id": "worker-1",
                "fence": 1,
                "stage": "implement",
                "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
                "ceiling_usd": "1",
            }

            # Loopback launch works: register task grant with control bearer
            resp = client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {control_token}"},
                json=task_payload,
            )
            assert resp.status_code == 200, resp.text
            grant = resp.json()
            grant_id = grant["id"]
            model_token = grant["model_token"]

            # Task tokens cannot call control endpoints: POST /tasks
            resp_forbidden_register = client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {model_token}"},
                json=task_payload,
            )
            assert resp_forbidden_register.status_code == 401

            # Task tokens cannot call control endpoints: DELETE /tasks/{id}
            resp_forbidden_revoke = client.delete(
                f"/tasks/{grant_id}",
                headers={"Authorization": f"Bearer {model_token}"},
            )
            assert resp_forbidden_revoke.status_code == 401

            # Native call using model token routes to fake provider
            model_resp = client.post(
                "/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
                headers={"x-goog-api-key": model_token},
                json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            )
            assert model_resp.status_code == 200
            assert b"Done." in model_resp.content
            assert len(calls) == 1

            # Control bearer can revoke the task
            resp_revoke = client.delete(
                f"/tasks/{grant_id}",
                headers={"Authorization": f"Bearer {control_token}"},
            )
            assert resp_revoke.status_code == 200
            assert resp_revoke.json()["refused"] is False
    finally:
        # Kill only child PID
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        release.set()
        fake_server.shutdown()
        fake_server.server_close()
        server_thread.join(timeout=5)


def test_non_loopback_bind_fails(service, tmp_path):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    capability_path = tmp_path / "automation.json"
    capability_path.write_text(
        json.dumps(
            {
                "token": "control-secret",
                "actor_kind": "automation",
                "scopes": ["work.execute"],
                "actor_id": str(ctx.actor_id),
                "workspaces": [str(ctx.workspace_id)],
            }
        )
    )
    capability_path.chmod(0o600)

    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)

    config_data = {
        "operations": {
            "config_dir": str(ctx.service.config.config_dir),
            "state_dir": str(ctx.service.config.state_dir),
            "data_dir": str(ctx.service.config.data_dir),
        },
        "control_capability": str(capability_path),
        "host": "0.0.0.0",
        "port": 8091,
        "transports": {
            "gemini_flash": dict(
                gemini_route(model="gemini-3.8-flash-high").__dict__
            ),
            "ollama_cloud": dict(
                ollama_route(model="deepseek-v4.1-flash:cloud").__dict__
            ),
        },
    }
    proxy_config_path = tmp_path / "model-proxy.json"
    proxy_config_path.write_text(json.dumps(config_data, default=str))

    res = subprocess.run(
        [sys.executable, "-m", "omp_work.model_proxy", "--config", str(proxy_config_path)],
        capture_output=True,
        timeout=5,
    )
    assert res.returncode == 2
    assert res.stderr == b"invalid proxy configuration\n"


def test_unsafe_key_permissions_fail(service, tmp_path):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    capability_path = tmp_path / "automation.json"
    capability_path.write_text(
        json.dumps(
            {
                "token": "control-secret",
                "actor_kind": "automation",
                "scopes": ["work.execute"],
                "actor_id": str(ctx.actor_id),
                "workspaces": [str(ctx.workspace_id)],
            }
        )
    )
    capability_path.chmod(0o600)

    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)

    # Make gemini-key unsafe (0644)
    (ctx.service.config.credentials_dir / "gemini-key").chmod(0o644)

    config_data = {
        "operations": {
            "config_dir": str(ctx.service.config.config_dir),
            "state_dir": str(ctx.service.config.state_dir),
            "data_dir": str(ctx.service.config.data_dir),
        },
        "control_capability": str(capability_path),
        "host": "127.0.0.1",
        "port": 8091,
        "transports": {
            "gemini_flash": dict(
                gemini_route(model="gemini-3.8-flash-high").__dict__
            ),
            "ollama_cloud": dict(
                ollama_route(model="deepseek-v4.1-flash:cloud").__dict__
            ),
        },
    }
    proxy_config_path = tmp_path / "model-proxy.json"
    proxy_config_path.write_text(json.dumps(config_data, default=str))

    res = subprocess.run(
        [sys.executable, "-m", "omp_work.model_proxy", "--config", str(proxy_config_path)],
        capture_output=True,
        timeout=5,
    )
    assert res.returncode == 2
    assert res.stderr == b"invalid proxy configuration\n"


def test_unsafe_capability_permissions_fail(service, tmp_path):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    capability_path = tmp_path / "automation.json"
    capability_path.write_text(
        json.dumps(
            {
                "token": "control-secret",
                "actor_kind": "automation",
                "scopes": ["work.execute"],
                "actor_id": str(ctx.actor_id),
                "workspaces": [str(ctx.workspace_id)],
            }
        )
    )
    # Unsafe capability permission (0644)
    capability_path.chmod(0o644)

    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)

    config_data = {
        "operations": {
            "config_dir": str(ctx.service.config.config_dir),
            "state_dir": str(ctx.service.config.state_dir),
            "data_dir": str(ctx.service.config.data_dir),
        },
        "control_capability": str(capability_path),
        "host": "127.0.0.1",
        "port": 8091,
        "transports": {
            "gemini_flash": dict(
                gemini_route(model="gemini-3.8-flash-high").__dict__
            ),
            "ollama_cloud": dict(
                ollama_route(model="deepseek-v4.1-flash:cloud").__dict__
            ),
        },
    }
    proxy_config_path = tmp_path / "model-proxy.json"
    proxy_config_path.write_text(json.dumps(config_data, default=str))

    res = subprocess.run(
        [sys.executable, "-m", "omp_work.model_proxy", "--config", str(proxy_config_path)],
        capture_output=True,
        timeout=5,
    )
    assert res.returncode == 2
    assert res.stderr == b"invalid proxy configuration\n"
