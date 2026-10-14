"""The arch-dev deployment loads with primary routing and closed premium access."""
from __future__ import annotations

import json
from pathlib import Path

from omp_work.model_proxy import ProxyConfig
from omp_work.routing.policy import load_policy
from omp_work.routing.router import route_stage


def test_shipped_config_loads_primary_route_and_refuses_unfunded_premium(tmp_path):
    shipped = Path(__file__).parents[1] / "deploy" / "model-proxy.json"
    data = json.loads(shipped.read_text())
    for name in ("config_dir", "state_dir", "data_dir"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        data["operations"][name] = str(directory)
    credentials = Path(data["operations"]["config_dir"]) / "credentials"
    credentials.mkdir(mode=0o700)
    for route in data["transports"].values():
        key = credentials / route["key_file"]
        key.write_text("test-provider-secret")
        key.chmod(0o600)
    capability = tmp_path / "automation.json"
    capability.write_text(json.dumps({"token": "test-control-secret"}))
    capability.chmod(0o600)
    data["control_capability"] = str(capability)
    config_path = tmp_path / "model-proxy.json"
    config_path.write_text(json.dumps(data))
    config_path.chmod(0o600)

    config = ProxyConfig.load(config_path)
    primary = route_stage(load_policy(), "implement")
    assert config.transports[primary.provider].model == primary.model
    assert config.overflow == "queue"
    assert config.premium_refusal("claude-opus-5-5") == "premium_reserve"
    assert config.premium_refusal("gpt-6-sol") == "premium_reserve"
    assert config.premium_refusal(primary.model) is None
