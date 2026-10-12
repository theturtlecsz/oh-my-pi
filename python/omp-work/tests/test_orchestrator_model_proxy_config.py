"""Optional model proxy config accepts only bare loopback HTTP origins."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.orchestrator.service import OrchestratorError, load_config


def _write(tmp_path: Path, **extra: object) -> Path:
    body: dict[str, object] = {
        "workspace_id": str(uuid4()),
        "automation_capability_path": str(tmp_path / "automation.json"),
        "qualification_path": str(tmp_path / "qualification.json"),
        "lock_map_path": str(tmp_path / "lock-map.json"),
        "verifier_key_path": str(tmp_path / "verifier.key"),
        "allowed_signers": str(tmp_path / "signers"),
        "control_repo": str(tmp_path / "repo"),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "live_checkout": str(tmp_path / "live"),
    }
    body.update(extra)
    path = tmp_path / "orchestrator.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.mark.parametrize("extra", [{}, {"model_proxy_url": ""}, {"model_proxy_url": None}])
def test_unconfigured_proxy_preserves_legacy_config(tmp_path: Path, extra: dict[str, object]) -> None:
    config = load_config(_write(tmp_path, **extra))
    assert config.model_proxy_url is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8091", "http://127.0.0.1:8091"),
        ("http://localhost:8091", "http://127.0.0.1:8091"),
        ("http://[::1]:8091", "http://[::1]:8091"),
        ("http://127.0.0.1:1", "http://127.0.0.1:1"),
        ("http://127.0.0.1:65535", "http://127.0.0.1:65535"),
    ],
)
def test_loads_loopback_proxy_origin(tmp_path: Path, url: str, expected: str) -> None:
    assert load_config(_write(tmp_path, model_proxy_url=url)).model_proxy_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8091",
        "http://127.0.0.1",
        "http://127.0.0.1:8091/path",
        "ftp://127.0.0.1:8091",
        "http://127.0.0.1:8091/",
        "http://127.0.0.1:8091?token=secret",
        "http://127.0.0.1:8091?",
        "http://127.0.0.1:8091#fragment",
        "http://user:pass@127.0.0.1:8091",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:abc",
        "http://[::1:8091",
        "http://[::2]:8091",
        " http://127.0.0.1:8091",
        "http://127.0.0.1:8091\n",
        False,
        0,
        [],
    ],
)
def test_invalid_proxy_origin_is_refused(tmp_path: Path, url: object) -> None:
    with pytest.raises(OrchestratorError) as raised:
        load_config(_write(tmp_path, model_proxy_url=url))
    assert raised.value.code == "invalid_request"
