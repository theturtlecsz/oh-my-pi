"""Real loopback HTTP exercises grant isolation and durable PostgreSQL spend."""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.model_proxy_upstream import TransportResponse
from test_model_proxy_budget import _admitting_job, _budget_ctx, _engage_stop
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import service

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration required")


@contextmanager
def serving(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            end = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < end
                time.sleep(.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


@pytest.fixture
def proxy(service, tmp_path):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    calls = []
    entered, release = threading.Event(), threading.Event()
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            entered.set()
            assert release.wait(15)
            if self.path.startswith("/v1beta"):
                data = b'data: {"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n'
            else:
                data = b'{"done":true,"prompt_eval_count":10,"eval_count":5}\n'
            data = getattr(ctx, "response_body", data)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass  # A timeout test deliberately disconnects before response.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    class Transport:
        def post(self, url, headers, body, *, max_bytes, timeout):
            parsed = urlsplit(url)
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
            try:
                conn.request("POST", parsed.path + ("?" + parsed.query if parsed.query else ""), body, headers)
                response = conn.getresponse()
                return TransportResponse(response.status, tuple(response.getheaders()), response.read(max_bytes + 1), url)
            finally:
                conn.close()

    capability = tmp_path / "automation.json"
    capability.write_text(json.dumps({"token": "control-secret", "actor_kind": "automation", "scopes": ["work.execute"], "actor_id": str(ctx.actor_id), "workspaces": [str(ctx.workspace_id)]}))
    capability.chmod(0o600)
    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)
    config = ProxyConfig(ctx.service.config, capability, {
        "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
        "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
    })
    ctx.transport = Transport()
    app = create_app(config, transport=ctx.transport)
    ctx.calls, ctx.entered, ctx.release, ctx.app, ctx.config = calls, entered, release, app, config
    try:
        with serving(app) as url, httpx.Client(base_url=url, timeout=30) as client:
            ctx.client = client
            yield ctx
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def task(ctx, **kw):
    data = dict(workspace_id=str(ctx.workspace_id), project_id=str(ctx.project_id), work_id=str(ctx.work_id), job_id=_admitting_job(ctx, lease_seconds=300), worker_id="worker-1", fence=1, stage="implement", expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(), ceiling_usd="1")
    data.update(kw)
    return data


def register(ctx, **kw):
    response = ctx.client.post("/tasks", json=task(ctx, **kw), headers={"Authorization": "Bearer control-secret"})
    assert response.status_code == 200, response.text
    return response.json()


def call(ctx, grant, *, path=None, body=None):
    descriptor = grant["descriptor"]
    gemini = descriptor["api"] == "google-generative-ai"
    return ctx.client.post(path or (f'/v1beta/models/{descriptor["id"]}:streamGenerateContent?alt=sse' if gemini else "/api/chat"), json=body or ({"contents": [{"parts": [{"text": "hi"}]}]} if gemini else {"messages": [{"role": "user", "content": "hi"}], "model": descriptor["id"]}), headers={"Authorization": "Bearer " + grant["model_token"]})


def spend(ctx):
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        return conn.execute("SELECT count(*), COALESCE(sum(price_usd),0) FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,)).fetchone()


def test_auth_separation_usage_once_and_restart(proxy):
    ctx = proxy
    grant = register(ctx)
    headers = {"Authorization": "Bearer " + grant["model_token"]}
    assert ctx.client.post("/tasks", json=task(ctx), headers=headers).status_code == 401
    assert ctx.client.delete("/tasks/" + grant["id"], headers=headers).status_code == 401
    assert ctx.client.post("/api/chat", json={}, headers={"Authorization": "Bearer control-secret"}).status_code == 401
    assert call(ctx, grant).status_code == 200
    n, usd = spend(ctx)
    assert n == 1 and usd > 0 and len(ctx.calls) == 1
    result = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert result.status_code == 200 and Decimal(result.json()["spent_usd"]) == usd
    assert call(ctx, grant).status_code == 403
    assert spend(ctx) == (n, usd)
    with serving(create_app(ctx.config)) as url:
        assert httpx.post(url + "/api/chat", json={}, headers=headers).status_code == 401


@pytest.mark.parametrize("failure", ["route", "expiry", "stop", "ceiling", "options", "lease", "size"])
def test_refusal_poison_prevents_any_upstream(proxy, failure):
    ctx = proxy
    grant = register(ctx, ceiling_usd="0.000001" if failure == "ceiling" else "1", expires_at=(datetime.now(UTC) + timedelta(seconds=.5)).isoformat() if failure == "expiry" else (datetime.now(UTC) + timedelta(seconds=120)).isoformat())
    if failure == "expiry":
        time.sleep(.6)
    if failure == "stop":
        _engage_stop(ctx)
    if failure == "lease":
        with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
            conn.execute("UPDATE omp_jobs.jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE workspace_id=%s", (ctx.workspace_id,))
    response = call(ctx, grant, path="/api/chat" if failure == "route" else None, body={"contents": [{"parts": [{"text": "x" * (ctx.config.max_request_bytes + 1) if failure == "size" else "hi"}]}], **({"generationConfig": {"maxOutputTokens": 999999}} if failure == "options" else {})})
    assert response.status_code in (403, 413)
    assert call(ctx, grant).status_code == 403
    assert ctx.calls == [] and spend(ctx)[0] == 0


def test_registration_authority_and_budget(proxy):
    ctx = proxy
    for override in ({"fence": 2}, {"worker_id": "other"}, {"work_id": str(uuid4())}, {"workspace_id": str(uuid4())}, {"project_id": str(uuid4())}, {"mission_id": str(uuid4())}, {"ceiling_usd": "999"}, {"expires_at": datetime.now(UTC).isoformat()}, {"expires_at": (datetime.now(UTC) + timedelta(hours=48)).isoformat()}, {"provider": "ollama_cloud"}):
        response = ctx.client.post("/tasks", json=task(ctx, **override), headers={"Authorization": "Bearer control-secret"})
        assert response.status_code in (400, 403)
        if "expires_at" in override:
            assert response.status_code == 403
            assert response.json()["detail"] == "expiry_refused"
    assert ctx.calls == []


def test_registration_admits_expiry_beyond_lease_within_deadline(proxy):
    response = proxy.client.post("/tasks", json=task(proxy, expires_at=(datetime.now(UTC) + timedelta(seconds=600)).isoformat()), headers={"Authorization": "Bearer control-secret"})
    assert response.status_code == 200, response.text


def test_mid_call_stop_records_spend_but_refuses(proxy):
    ctx = proxy
    grant = register(ctx)
    ctx.release.clear()
    responses = []
    thread = threading.Thread(target=lambda: responses.append(call(ctx, grant)))
    thread.start()
    try:
        assert ctx.entered.wait(5)
        _engage_stop(ctx)
    finally:
        ctx.release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert responses[0].status_code == 403
    assert spend(ctx)[0] == 1 and spend(ctx)[1] > 0
    assert call(ctx, grant).status_code == 403 and len(ctx.calls) == 1


def test_live_in_flight_routes_overflow_and_refuses_both_saturated(proxy):
    ctx = proxy
    gemini = [register(ctx) for _ in range(6)]
    ctx.release.clear()
    responses, threads = [], []

    def start(grant):
        worker = threading.Thread(target=lambda: responses.append(call(ctx, grant)))
        worker.start()
        threads.append(worker)

    try:
        for grant in gemini:
            start(grant)
        assert ctx.entered.wait(5)
        end = time.monotonic() + 5
        while True:
            overflow = register(ctx)
            if overflow["descriptor"]["api"] == "ollama-chat":
                break
            assert time.monotonic() < end
            time.sleep(.02)
        assert overflow["descriptor"]["id"] == "deepseek-v4.1-flash:cloud"
        tampered = register(ctx)
        before = len(ctx.calls)
        assert call(ctx, tampered, body={"model": "worker-picked-model", "messages": [{"role": "user", "content": "hi"}]}).status_code == 403
        assert call(ctx, tampered).status_code == 403 and len(ctx.calls) == before
        ollama = [overflow] + [register(ctx) for _ in range(3)]
        for grant in ollama:
            start(grant)
        while True:
            result = ctx.client.post("/tasks", json=task(ctx), headers={"Authorization": "Bearer control-secret"})
            if result.status_code == 403:
                break
            assert time.monotonic() < end
            time.sleep(.02)
        assert result.json()["detail"] == "provider_saturated"
    finally:
        ctx.release.set()
        for worker in threads:
            worker.join(15)
            assert not worker.is_alive()
    assert len(responses) == 10 and all(r.status_code == 200 for r in responses)
    ollama_calls = [body for path, body in ctx.calls if path == "/api/chat"]
    assert len(ollama_calls) == 4
    assert all(body["model"] == "deepseek-v4.1-flash:cloud" for body in ollama_calls)
    assert spend(ctx)[0] == 10


def test_non_loopback_bind_refused(proxy):
    with pytest.raises(ValueError, match="loopback_required"):
        replace(proxy.config, host="0.0.0.0")


@pytest.mark.parametrize("response", [b'broken SECRET provider-secret', b'data: {"candidates":[{"finishReason":"STOP","text":"provider-secret"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n'])
def test_secret_response_refused_and_incurred_spend_reported(proxy, response):
    ctx = proxy
    ctx.response_body = response
    grant = register(ctx)
    result = call(ctx, grant)
    assert result.status_code == 403 and "provider-secret" not in result.text
    assert call(ctx, grant).status_code == 403
    n, usd = spend(ctx)
    assert n == 1 and usd > 0 and len(ctx.calls) == 1
    revoked = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert revoked.json()["refused"] is True
    assert Decimal(revoked.json()["spent_usd"]) == usd


def test_task_ceiling_includes_previous_calls(proxy):
    ctx = proxy
    grant = register(ctx, ceiling_usd="0.03376")
    assert call(ctx, grant).status_code == 200
    assert call(ctx, grant).status_code == 403
    assert call(ctx, grant).status_code == 403
    assert len(ctx.calls) == 1 and spend(ctx)[0] == 1


def test_config_load_and_module_refuse_non_loopback(proxy, tmp_path):
    path = tmp_path / "proxy.json"
    data = asdict(proxy.config)
    path.write_text(json.dumps(data, default=str))
    loaded = ProxyConfig.load(path)
    assert loaded.operations.connection_kwargs("omp_work_app") == proxy.config.operations.connection_kwargs("omp_work_app")
    assert loaded.transports["ollama_cloud"].api == "ollama-chat"
    data["host"] = "0.0.0.0"
    path.write_text(json.dumps(data, default=str))
    result = subprocess.run([sys.executable, "-m", "omp_work.model_proxy", "--config", str(path)], capture_output=True, timeout=5)
    assert result.returncode == 2
    assert result.stderr == b"invalid proxy configuration\n" and result.stdout == b""


@pytest.mark.parametrize("limit", ["response_size", "timeout"])
def test_upstream_limits_charge_bound_and_poison_grant(proxy, limit):
    ctx = proxy
    config = replace(ctx.config, **({"max_response_bytes": 10} if limit == "response_size" else {"timeout_s": .1}))
    original = ctx.client
    with serving(create_app(config, transport=ctx.transport)) as url, httpx.Client(base_url=url, timeout=10) as client:
        ctx.client = client
        try:
            grant = register(ctx)
            if limit == "timeout":
                ctx.release.clear()
            result = call(ctx, grant)
            assert result.status_code == 403
            assert call(ctx, grant).status_code == 403
            assert len(ctx.calls) == 1
            n, usd = spend(ctx)
            assert n == 1 and usd == Decimal("0.03375")
            revoked = client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
            assert revoked.json()["refused"] is True
            assert Decimal(revoked.json()["spent_usd"]) == usd
        finally:
            ctx.release.set()
            ctx.client = original


@pytest.mark.parametrize("override", [{"actor_kind": "owner"}, {"scopes": ["work.read"]}, {"expires_at": "2000-01-01T00:00:00+00:00"}])
def test_control_requires_live_automation_execution_capability(proxy, override):
    ctx = proxy
    data = json.loads(ctx.config.control_capability.read_text())
    data.update(override)
    ctx.config.control_capability.write_text(json.dumps(data))
    response = ctx.client.post("/tasks", json=task(ctx), headers={"Authorization": "Bearer control-secret"})
    assert response.status_code == 401 and ctx.calls == []
