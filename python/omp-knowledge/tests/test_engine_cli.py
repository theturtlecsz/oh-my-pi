"""Engine CLI: Cognee/Enola check, lookup of the corrected fact, unavailable exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cli import main
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter
from support.fixtures import make_test_enola_fixture


def _write_enola(directory: Path, snapshot_id: str) -> None:
    facts, receipt, _insights, _raw = make_test_enola_fixture(
        repo_name="repo-lifecycle",
        snapshot_id=snapshot_id,
    )
    directory.mkdir()
    (directory / "facts.jsonl").write_bytes(facts)
    (directory / "receipt.json").write_bytes(receipt)


def _property(properties: dict, key: str):
    if key in properties:
        return properties[key]
    nested = properties.get("fact_properties")
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def test_unavailable_engine_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = KnowledgeConfig(state_dir=tmp_path / "state")
    adapter = RealCogneeAdapter(config, available=False)
    enola = tmp_path / "enola"
    enola.mkdir()
    rc = main(
        ["check", "--enola-dir", str(enola), "--repository-id", str(uuid4())],
        adapter,
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "engine_unavailable" in captured.err


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
def test_check_then_lookup_returns_corrected_value(tmp_path: Path) -> None:
    snapshot_id = "c" * 64
    enola = tmp_path / "enola"
    _write_enola(enola, snapshot_id)
    state = tmp_path / "state"
    home = tmp_path / "home"
    repository_id = str(uuid4())
    workspace_id = str(uuid4())
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OMP_KNOWLEDGE_STATE_DIR"] = str(state)
    env["OMP_KNOWLEDGE_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_knowledge.engine",
            "check",
            "--enola-dir",
            str(enola),
            "--repository-id",
            repository_id,
            "--workspace-id",
            workspace_id,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    report = json.loads(check.stdout)
    assert report["capability"] == "cognee_enola"
    assert report["passed"] is True
    assert report["fact_id"]
    assert report["after"] == "yes"
    assert report["before"] != report["after"]

    lookup = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_knowledge.engine",
            "lookup",
            "--repository-id",
            repository_id,
            "--workspace-id",
            workspace_id,
            "--snapshot-id",
            snapshot_id,
            "--fact-id",
            report["fact_id"],
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lookup.returncode == 0, lookup.stderr
    found = json.loads(lookup.stdout)
    assert found["found"] is True
    assert found["fact_id"] == report["fact_id"]
    assert _property(found["properties"], "corrected") == "yes"
