"""GitHub pull-request settings on OrchestratorConfig (OMP-518-s04)."""

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


def test_github_settings_load(tmp_path: Path) -> None:
    token = tmp_path / "github.token"
    config = load_config(
        _write(
            tmp_path,
            github_api_url="https://github.example/api/v3",
            github_repositories={"ledger": "acme/ledger"},
            github_token_path=str(token),
            required_checks=["ci", "lint"],
        )
    )
    assert config.github_api_url == "https://github.example/api/v3"
    assert isinstance(config.github_token_path, Path)
    assert config.github_token_path == token
    assert isinstance(config.required_checks, tuple)
    assert config.required_checks == ("ci", "lint")
    assert config.github_repositories["ledger"] == "acme/ledger"


def test_github_settings_default_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path))
    assert config.github_api_url == "https://api.github.com"
    assert config.github_repositories == {}
    assert config.github_token_path is None
    assert config.required_checks == ()


def test_empty_github_token_path_is_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, github_token_path=""))
    assert config.github_token_path is None


@pytest.mark.parametrize(
    "extra",
    [
        {"required_checks": "ci"},
        {"required_checks": [""]},
        {"required_checks": [1]},
        {"github_repositories": ["a/b"]},
        {"github_repositories": {"repo": "a/b/c"}},
        {"github_repositories": {"repo": "a"}},
    ],
)
def test_github_settings_reject_invalid(tmp_path: Path, extra: dict[str, object]) -> None:
    with pytest.raises(OrchestratorError) as raised:
        load_config(_write(tmp_path, **extra))
    assert raised.value.code == "invalid_request"
