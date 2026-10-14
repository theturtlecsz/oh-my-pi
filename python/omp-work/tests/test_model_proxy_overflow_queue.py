"""Overflow mode: alternate substitution versus primary-only queueing.

Offline load tests pin the configuration contract: ``overflow`` defaults to
``alternate`` (compatibility) and accepts ``queue``; queue validates and
selects only the primary implement route, while alternate validates the
alternate too and refuses a missing overflow transport; an unknown mode is
refused at construction.

PostgreSQL integration tests drive the real loopback proxy against a scoped
``ModelProxyBudget.call`` latch. In queue mode the proxy keeps the primary
route and waits on provider and total capacity for the call deadline.
Incumbent slots held before the upstream send cannot be freed by a transport
timeout, so a seventh call past its deadline is refused ``provider_saturated``
without a budget or upstream invocation; releasing before the deadline lets
the queued call succeed on the primary provider.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, replace

import httpx
import pytest

from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.model_proxy_budget import ModelProxyBudget
from omp_work.operations.config import OperationsConfig
from test_model_proxy import call, proxy, service, serving, task
from test_model_proxy_upstream import gemini_route, ollama_route

postgres = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)

GEMINI_MODEL = "gemini-3.8-flash-high"
OLLAMA_MODEL = "deepseek-v4.1-flash:cloud"
CONTROL = {"Authorization": "Bearer control-secret"}


def _config(tmp_path, *, overflow="alternate", transports=None, **over):
    operations = OperationsConfig(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    capability = tmp_path / "automation.json"
    capability.write_text(json.dumps({"token": "control-secret"}))
    capability.chmod(0o600)
    return ProxyConfig(
        operations,
        capability,
        transports if transports is not None else {
            "gemini_flash": gemini_route(model=GEMINI_MODEL),
            "ollama_cloud": ollama_route(model=OLLAMA_MODEL),
        },
        overflow=overflow,
        **over,
    )


def test_queue_mode_validates_only_primary_implement_route(tmp_path):
    config = _config(tmp_path, overflow="queue", transports={"gemini_flash": gemini_route(model=GEMINI_MODEL)})
    assert config.overflow == "queue"


def test_alternate_mode_rejects_missing_overflow_transport(tmp_path):
    with pytest.raises(ValueError, match="invalid_transport_map"):
        _config(tmp_path, transports={"gemini_flash": gemini_route(model=GEMINI_MODEL)})


def test_invalid_overflow_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid_overflow"):
        _config(tmp_path, overflow="bogus")


def test_overflow_defaults_to_alternate_and_load_round_trips_queue(tmp_path):
    assert _config(tmp_path).overflow == "alternate"
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for name in ("gemini-key", "ollama-key"):
        key = credentials / name
        key.write_text("provider-secret")
        key.chmod(0o600)
    config_path = tmp_path / "proxy.json"
    config_path.write_text(json.dumps(asdict(_config(tmp_path)), default=str))
    assert ProxyConfig.load(config_path).overflow == "alternate"
    data = json.loads(config_path.read_text())
    data["overflow"] = "queue"
    config_path.write_text(json.dumps(data))
    assert ProxyConfig.load(config_path).overflow == "queue"


def _latched_call(monkeypatch, arrivals, latch, entered, lock):
    original = ModelProxyBudget.call

    def latched(self, *args, **kwargs):
        with lock:
            entered.append(1)
            arrived = len(entered) == 6
        if arrived:
            arrivals.set()
        assert latch.wait(60)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModelProxyBudget, "call", latched)


def _start(ctx, grants, responses, threads):
    for grant in grants:
        worker = threading.Thread(target=lambda g=grant: responses.append(call(ctx, g)))
        worker.start()
        threads.append(worker)


def _register(ctx, client):
    response = client.post("/tasks", json=task(ctx), headers=CONTROL)
    assert response.status_code == 200, response.text
    return response.json()


@postgres
def test_queue_waits_past_deadline_then_refuses_provider_saturated(proxy, monkeypatch):
    ctx = proxy
    config = replace(ctx.config, overflow="queue", timeout_s=.5)
    arrivals, latch, lock, entered = threading.Event(), threading.Event(), threading.Lock(), []
    _latched_call(monkeypatch, arrivals, latch, entered, lock)
    responses, threads = [], []
    original = ctx.client
    with serving(create_app(config, transport=ctx.transport)) as url, httpx.Client(base_url=url, timeout=60) as client:
        ctx.client = client
        try:
            held = [_register(ctx, client) for _ in range(6)]
            _start(ctx, held, responses, threads)
            assert arrivals.wait(15)
            # Queue register ignores in-flight counts: the saturated primary
            # provider still receives a primary grant.
            queued = _register(ctx, client)
            assert queued["descriptor"]["api"] == "google-generative-ai"
            assert queued["descriptor"]["id"] == GEMINI_MODEL
            result = call(ctx, queued)
            assert result.status_code == 403
            assert result.json()["detail"] == "provider_saturated"
            # The six incumbents stay latched before the upstream send, so their
            # slots cannot be freed by a transport timeout and the seventh
            # reached neither budget admission nor the provider.
            assert len(entered) == 6
            assert ctx.calls == []
        finally:
            latch.set()
            for worker in threads:
                worker.join(60)
                assert not worker.is_alive()
            ctx.client = original
    assert len(entered) == 6
    assert len(responses) == 6
    # Incumbents outlived their own deadline while latched and released the
    # provider slot without a transport timeout ever reaching the provider.
    assert ctx.calls == []


@postgres
def test_queue_admits_queued_call_when_slot_frees_before_deadline(proxy, monkeypatch):
    ctx = proxy
    config = replace(ctx.config, overflow="queue")
    arrivals, latch, lock, entered = threading.Event(), threading.Event(), threading.Lock(), []
    _latched_call(monkeypatch, arrivals, latch, entered, lock)
    responses, threads = [], []
    original = ctx.client
    with serving(create_app(config, transport=ctx.transport)) as url, httpx.Client(base_url=url, timeout=60) as client:
        ctx.client = client
        try:
            held = [_register(ctx, client) for _ in range(6)]
            _start(ctx, held, responses, threads)
            assert arrivals.wait(15)
            queued = _register(ctx, client)
            assert queued["descriptor"]["id"] == GEMINI_MODEL
            worker = threading.Thread(target=lambda: responses.append(call(ctx, queued)))
            worker.start()
            threads.append(worker)
            # Six latched slots saturate the primary provider: the queued call
            # waits instead of being served, and no upstream call happened.
            time.sleep(.3)
            assert worker.is_alive()
            assert ctx.calls == []
            latch.set()
            for joined in threads:
                joined.join(60)
                assert not joined.is_alive()
        finally:
            latch.set()
            for joined in threads:
                if joined.is_alive():
                    joined.join(60)
            ctx.client = original
    assert len(responses) == 7 and all(response.status_code == 200 for response in responses)
    assert ctx.calls and all(path != "/api/chat" for path, _ in ctx.calls)
