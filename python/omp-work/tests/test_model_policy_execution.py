"""The implement lane's model comes from the routing policy, end to end."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import psycopg
import pytest
from omp_work import model_proxy
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.model_proxy_upstream import TransportResponse
from omp_work.routing.policy import load_policy
from test_model_proxy import call, register, serving, spend
from test_model_proxy_budget import _budget_ctx
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import service

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration required")

GEMINI_USAGE = b'data: {"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n'


class RecordingTransport:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, headers, body, *, max_bytes, timeout):
        self.calls.append((url, json.loads(body)))
        return TransportResponse(200, (), GEMINI_USAGE, url)


def _lane(policy):
    return policy.stages["implement"]


def _proxy(service, tmp_path, implement_model):
    ctx = _budget_ctx(service, ceiling="10", threshold="10")
    capability = tmp_path / "automation.json"
    capability.write_text(json.dumps({"token": "control-secret", "actor_kind": "automation", "scopes": ["work.execute"], "actor_id": str(ctx.actor_id), "workspaces": [str(ctx.workspace_id)]}))
    capability.chmod(0o600)
    for name in ("gemini-key", "ollama-key"):
        path = ctx.service.config.credentials_dir / name
        path.write_text("provider-secret")
        path.chmod(0o600)
    ctx.config = ProxyConfig(ctx.service.config, capability, {
        "gemini_flash": gemini_route(model=implement_model),
        "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
    })
    ctx.transport = RecordingTransport()
    return ctx


def _models(ctx):
    with psycopg.connect(**ctx.service.config.connection_kwargs("postgres")) as conn:
        return [r[0] for r in conn.execute("SELECT model FROM omp_jobs.usage_events WHERE workspace_id=%s", (ctx.workspace_id,)).fetchall()]


def _run(ctx):
    app = create_app(ctx.config, transport=ctx.transport)
    with serving(app) as url, httpx.Client(base_url=url, timeout=30) as client:
        ctx.client = client
        grant = register(ctx)
        assert call(ctx, grant).status_code == 200
        done = client.delete("/tasks/" + grant["id"], headers={"Authorization": "Bearer control-secret"})
        assert done.status_code == 200
    return grant, done.json()


def test_implement_call_uses_policy_model_and_bills_it(service, tmp_path):
    model = _lane(load_policy()).model
    ctx = _proxy(service, tmp_path, model)
    grant, done = _run(ctx)
    assert grant["descriptor"]["id"] == model
    [(url, _)] = ctx.transport.calls
    assert f"models/{model}:streamGenerateContent" in url
    n, usd = spend(ctx)
    assert n == 1 and usd > 0 and _models(ctx) == [model]
    assert Decimal(done["spent_usd"]) == usd


def test_policy_model_change_reaches_upstream_and_usage(service, tmp_path, monkeypatch):
    original = load_policy()
    old = _lane(original).model
    new = old + "-renamed"
    assert _lane(original).provider == "gemini_flash"
    mutated = tmp_path / "routing-policy.v1.json"
    mutated.write_text(json.dumps(original.raw).replace(old, new))
    monkeypatch.setattr(model_proxy, "load_policy", lambda path=None: load_policy(mutated))
    assert _lane(model_proxy.load_policy()).model == new
    ctx = _proxy(service, tmp_path, new)
    grant, _ = _run(ctx)
    assert grant["descriptor"]["id"] == new
    [(url, _)] = ctx.transport.calls
    assert f"models/{new}:streamGenerateContent" in url and f"models/{old}:" not in url
    assert _models(ctx) == [new]
