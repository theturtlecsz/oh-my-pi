"""Worker-native Gemini route and credential contracts for the model proxy.

``google.ts`` posts to ``{baseUrl}/models/{id}:streamGenerateContent?alt=sse``
with ``x-goog-api-key`` and no ``Authorization``; ``run_worker`` publishes a
``http://127.0.0.1:{port}`` base URL with no path. These tests drive that exact
wire shape through the real loopback proxy and a fake provider, and pin the
charge-then-refuse accounting that runs when the provider rejects the call.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from omp_work.jobs.store import NativeJobStore
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.model_proxy_upstream import TransportResponse
from test_model_proxy import serving
from test_model_proxy_budget import _admitting_job, _budget_ctx
from test_model_proxy_upstream import gemini_route, ollama_route

pytest_plugins = ["test_model_proxy"]

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration required")


def _gemini_body() -> dict:
    return {"contents": [{"parts": [{"text": "hi"}]}]}


def _task(ctx, **kw):
    data = dict(workspace_id=str(ctx.workspace_id), project_id=str(ctx.project_id), work_id=str(ctx.work_id), job_id=_admitting_job(ctx, lease_seconds=300), worker_id="worker-1", fence=1, stage="implement", expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(), ceiling_usd="1")
    data.update(kw)
    return data


def _register(ctx, **kw):
    response = ctx.client.post("/tasks", json=_task(ctx, **kw), headers={"Authorization": "Bearer control-secret"})
    assert response.status_code == 200, response.text
    return response.json()


def _spend(ctx):
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        return conn.execute("SELECT count(*), COALESCE(sum(price_usd),0) FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,)).fetchone()


def _durable_charge(ctx):
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        row = conn.execute("SELECT COALESCE(sum((result->'settlement'->>'actual_usd')::numeric),0) AS usd FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt'", (ctx.workspace_id,)).fetchone()
        return row[0]


@pytest.fixture
def proxy(service, tmp_path):
    """Loopback proxy whose fake provider status is test-controlled."""
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    calls: list[tuple[str, dict]] = []
    state = SimpleNamespace(status=200, calls=calls)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            data = (b'data: {"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n'
                    if self.path.startswith("/v1beta")
                    else b'{"done":true,"prompt_eval_count":10,"eval_count":5}\n')
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

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
    ctx.state, ctx.config = state, config
    ctx.app = create_app(config, transport=Transport())
    try:
        with serving(ctx.app) as url, httpx.Client(base_url=url, timeout=30) as client:
            ctx.client = client
            yield ctx
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_worker_gemini_route_and_x_goog_api_key(proxy):
    ctx = proxy
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    response = ctx.client.post(
        f"/models/{model_id}:streamGenerateContent?alt=sse",
        json=_gemini_body(),
        headers={"x-goog-api-key": grant["model_token"]},
    )
    assert response.status_code == 200, response.text
    count, usd = _spend(ctx)
    assert count == 1 and usd > 0 and len(ctx.state.calls) == 1
    revoked = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert revoked.status_code == 200 and revoked.json()["refused"] is False
    assert Decimal(revoked.json()["spent_usd"]) == usd


def test_missing_or_unknown_credential_is_unauthenticated(proxy):
    ctx = proxy
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    path = f"/models/{model_id}:streamGenerateContent?alt=sse"
    assert ctx.client.post(path, json=_gemini_body()).status_code == 401
    assert ctx.client.post(path, json=_gemini_body(), headers={"x-goog-api-key": "not-the-token"}).status_code == 401
    assert ctx.client.post(path, json=_gemini_body(), headers={"Authorization": "Bearer not-the-token"}).status_code == 401
    assert ctx.state.calls == []
    assert ctx.client.post(path, json=_gemini_body(), headers={"x-goog-api-key": grant["model_token"]}).status_code == 200


def test_control_stays_bearer_only(proxy):
    ctx = proxy
    grant = _register(ctx)
    # The control plane never accepts the model token as its bearer. The Gemini
    # worker sends its token as x-goog-api-key, which the control plane ignores.
    headers = {"Authorization": "Bearer " + grant["model_token"]}
    assert ctx.client.post("/tasks", json=_task(ctx), headers=headers).status_code == 401
    assert ctx.client.delete("/tasks/" + grant["id"], headers=headers).status_code == 401
    assert ctx.client.post("/tasks", json=_task(ctx), headers={"x-goog-api-key": "control-secret"}).status_code == 401


def test_bearer_and_v1beta_path_still_accepted(proxy):
    ctx = proxy
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    response = ctx.client.post(
        f"/v1beta/models/{model_id}:streamGenerateContent?alt=sse",
        json=_gemini_body(),
        headers={"Authorization": "Bearer " + grant["model_token"]},
    )
    assert response.status_code == 200, response.text
    assert len(ctx.state.calls) == 1 and _spend(ctx)[0] == 1


def test_gemini_route_accepts_empty_query(proxy):
    ctx = proxy
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    response = ctx.client.post(
        f"/models/{model_id}:streamGenerateContent",
        json=_gemini_body(),
        headers={"x-goog-api-key": grant["model_token"]},
    )
    assert response.status_code == 200, response.text
    assert len(ctx.state.calls) == 1


@pytest.mark.parametrize("path", [
    "/models/{id}:streamGenerateContent?alt=json",
    "/v1/models/{id}:streamGenerateContent",
    "/v1beta/models/{id}:streamGenerateContent?alt=json",
    "/v1beta/models/{id}:generateContent?alt=sse",
    "/api/chat",
])
def test_gemini_other_routes_refused_before_upstream(proxy, path):
    ctx = proxy
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    response = ctx.client.post(
        path.format(id=model_id),
        json=_gemini_body(),
        headers={"x-goog-api-key": grant["model_token"]},
    )
    assert response.status_code == 403 and response.json()["detail"] == "route_mismatch"
    assert ctx.state.calls == [] and _spend(ctx)[0] == 0


def test_provider_error_keeps_durable_decimal_charge(proxy):
    ctx = proxy
    ctx.state.status = 500
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    response = ctx.client.post(
        f"/models/{model_id}:streamGenerateContent?alt=sse",
        json=_gemini_body(),
        headers={"x-goog-api-key": grant["model_token"]},
    )
    assert response.status_code == 403
    assert response.json()["detail"] != "proxy_refused"
    assert len(ctx.state.calls) == 1
    durable = _durable_charge(ctx)
    assert isinstance(durable, Decimal) and durable > 0
    revoked = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert revoked.json()["refused"] is True
    assert Decimal(revoked.json()["spent_usd"]) == durable


def test_no_charge_refusal_reports_original_code(proxy):
    ctx = proxy
    grant = _register(ctx, ceiling_usd="0.03376")
    model_id = grant["descriptor"]["id"]
    headers = {"x-goog-api-key": grant["model_token"]}
    path = f"/models/{model_id}:streamGenerateContent?alt=sse"
    assert ctx.client.post(path, json=_gemini_body(), headers=headers).status_code == 200
    refusal = ctx.client.post(path, json=_gemini_body(), headers=headers)
    # The ceiling refusal writes no receipt, so the durable-charge read is zero
    # and must surface the admission code rather than fall through to proxy_refused.
    assert refusal.status_code == 403 and refusal.json()["detail"] == "task_ceiling_exceeded"
    revoked = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert Decimal(revoked.json()["spent_usd"]) > 0


@pytest.mark.parametrize("raw", [7, 7.0])
def test_non_decimal_durable_charge_stays_decimal_not_proxy_refused(proxy, monkeypatch, raw):
    ctx = proxy
    ctx.state.status = 500
    grant = _register(ctx)
    model_id = grant["descriptor"]["id"]
    real_transaction = NativeJobStore.transaction

    @contextmanager
    def raw_charge_transaction(self, *args, **kwargs):
        # The durable-charge read is a raw numeric. Model a non-Decimal driver
        # value here for that one query: an unguarded `Decimal += float` raises
        # TypeError, which the outer handler maps to proxy_refused instead of
        # reporting the durable charge.
        with real_transaction(self, *args, **kwargs) as cur:
            class Cursor:
                def __getattr__(self, name):
                    return getattr(cur, name)

                def execute(self, query, params=None):
                    self.durable_charge = "request_id" in str(query) and "settlement" in str(query)
                    return cur.execute(query, params)

                def fetchone(self):
                    row = cur.fetchone()
                    return {**row, "usd": raw} if self.durable_charge and row is not None and "usd" in row else row

            yield Cursor()

    monkeypatch.setattr(NativeJobStore, "transaction", raw_charge_transaction)
    response = ctx.client.post(
        f"/models/{model_id}:streamGenerateContent?alt=sse",
        json=_gemini_body(),
        headers={"x-goog-api-key": grant["model_token"]},
    )
    assert response.status_code == 403 and response.json()["detail"] != "proxy_refused"
    revoked = ctx.client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
    assert Decimal(revoked.json()["spent_usd"]) == Decimal(7)



