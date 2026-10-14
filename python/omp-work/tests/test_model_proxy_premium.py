"""Configured premium authorization precedes grants and provider effects."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from decimal import Decimal

import httpx
import pytest

from omp_work.model_proxy import PremiumReserve, ProxyConfig, create_app
from omp_work.operations.config import OperationsConfig
from test_model_proxy import call, proxy, service, serving, spend, task
from test_model_proxy_upstream import gemini_route, ollama_route

postgres = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)


@postgres
@pytest.mark.parametrize("usd,listed", [("0", True), ("5", False)], ids=["zero-allocation", "unlisted-model"])
def test_premium_registration_refuses_without_provider_effect(proxy, usd, listed):
    ctx = proxy
    primary = ctx.config.transports["gemini_flash"].model
    config = replace(
        ctx.config,
        premium_models=(primary,),
        premium_reserve=PremiumReserve(Decimal(usd), (primary,) if listed else ("another-model",)),
    )
    before = spend(ctx)
    with serving(create_app(config, transport=ctx.transport)) as url:
        response = httpx.post(
            url + "/tasks", json=task(ctx),
            headers={"Authorization": "Bearer control-secret"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "premium_reserve"
    assert ctx.calls == []
    assert spend(ctx) == before


@postgres
@pytest.mark.parametrize("premium", [True, False], ids=["listed-premium", "non-premium"])
def test_authorized_model_registration_and_call_succeed(proxy, premium):
    ctx = proxy
    primary = ctx.config.transports["gemini_flash"].model
    config = replace(
        ctx.config,
        premium_models=(primary,) if premium else ("another-model",),
        premium_reserve=PremiumReserve(Decimal("5"), (primary,)) if premium else PremiumReserve(),
    )
    original = ctx.client
    with serving(create_app(config, transport=ctx.transport)) as url, httpx.Client(base_url=url, timeout=30) as client:
        ctx.client = client
        try:
            response = client.post("/tasks", json=task(ctx), headers={"Authorization": "Bearer control-secret"})
            assert response.status_code == 200, response.text
            grant = response.json()
            assert grant["descriptor"]["id"] == primary
            assert call(ctx, grant).status_code == 200
        finally:
            ctx.client = original
    assert len(ctx.calls) == 1
    count, usd = spend(ctx)
    assert count == 1 and usd > 0


@pytest.fixture
def config_path(tmp_path):
    operations = OperationsConfig(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    operations.credentials_dir.mkdir(parents=True, mode=0o700)
    for name in ("gemini-key", "ollama-key"):
        key = operations.credentials_dir / name
        key.write_text("provider-secret")
        key.chmod(0o600)
    capability = tmp_path / "automation.json"
    capability.write_text(json.dumps({"token": "control-secret"}))
    capability.chmod(0o600)
    config = ProxyConfig(operations, capability, {
        "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
        "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
    })
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps(asdict(config), default=str))
    return path


def test_load_json_lists_decimal_strings_and_asdict_round_trip(config_path):
    data = json.loads(config_path.read_text())
    primary = data["transports"]["gemini_flash"]["model"]
    data["premium_models"] = [primary, "other-premium"]
    data["premium_reserve"] = {"usd": "5.25", "models": [primary]}
    config_path.write_text(json.dumps(data))
    loaded = ProxyConfig.load(config_path)
    assert loaded.premium_models == (primary, "other-premium")
    assert loaded.premium_reserve == PremiumReserve(Decimal("5.25"), (primary,))
    assert loaded.premium_refusal(primary) is None
    assert loaded.premium_refusal("other-premium") == "premium_reserve"
    assert loaded.premium_refusal("unclassified-model") is None
    config_path.write_text(json.dumps(asdict(loaded), default=str))
    assert ProxyConfig.load(config_path) == loaded


def test_load_without_premium_configuration_allows_unclassified_models(config_path):
    data = json.loads(config_path.read_text())
    del data["premium_models"]
    del data["premium_reserve"]
    config_path.write_text(json.dumps(data))
    loaded = ProxyConfig.load(config_path)
    assert loaded.premium_refusal(loaded.transports["gemini_flash"].model) is None


@pytest.mark.parametrize("usd", ["-0.01", "NaN", "sNaN", "Infinity", "-Infinity"])
def test_invalid_allocation_rejected_at_construction_and_load(config_path, usd):
    with pytest.raises(ValueError, match="invalid_premium_reserve"):
        PremiumReserve(Decimal(usd))
    data = json.loads(config_path.read_text())
    data["premium_reserve"] = {"usd": usd, "models": []}
    config_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="invalid_premium_reserve"):
        ProxyConfig.load(config_path)
