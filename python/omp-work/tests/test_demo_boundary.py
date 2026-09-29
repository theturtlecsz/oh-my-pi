"""Demo commands stay off the active economy directory, and service imports stay off demo modules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from omp_work.__main__ import main

_FORBIDDEN = (
    "cockpit_verbs",
    "cockpit_persist",
    "cockpit_pipeline",
    "chaos_suite",
    "durability_a1",
    "full_path",
    "knowledge_b1",
    "cognee_adapter",
    "cognee_store",
    "enola_adapter",
    "enola_store",
    "context_compile",
    "context_compile_bar",
    "research_campaign",
    "campaign_budget_guard",
    "engine_pipeline",
    "ecc_adapter",
)
_TARGETS = (
    "omp_work.__main__",
    "omp_work.jobs",
    "omp_work.v1.server",
    "omp_work.operations.cli",
)
_IMPORT_PROBE = """
import importlib
import json
import sys

target = sys.argv[1]
forbidden = sys.argv[2:]
importlib.import_module(target)
loaded = []
for name in sys.modules:
    parts = name.split(".")
    for item in forbidden:
        if item in parts:
            loaded.append(name)
            break
print(json.dumps(sorted(set(loaded))))
"""


def _run(args: list[str], home: Path, active: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OMP_ECONOMY_ACTIVE_DIR"] = str(active)
    env["XDG_CONFIG_HOME"] = str(home / "xdg-config")
    env["XDG_CACHE_HOME"] = str(home / "xdg-cache")
    env["XDG_DATA_HOME"] = str(home / "xdg-data")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "omp_work", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_pipeline_aliases_write_only_under_scratch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    active = tmp_path / "active"
    for prefix in ([], ["demo"]):
        proc = _run(
            [
                *prefix,
                "pipeline",
                "--job-id",
                "j1",
                "--query",
                "write-first",
                "--objective",
                "demo",
            ],
            home,
            active,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        scratch = Path(data["scratch_dir"])
        try:
            assert data["demo"] is True
            assert data["job_id"] == "j1"
            assert scratch.is_dir()
            assert not _under(scratch, home)
            assert not _under(scratch, active)
            assert (scratch / "enola-store.json").is_file()
            assert (scratch / "ledger.json").is_file()
            assert not home.exists()
            assert not active.exists()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def test_compile_bar_aliases_report_scratch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    active = tmp_path / "active"
    for prefix in ([], ["demo"]):
        proc = _run(
            [*prefix, "compile-bar", "--job-id", "j2", "--objective", "demo"],
            home,
            active,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        scratch = Path(data["scratch_dir"])
        try:
            assert data["demo"] is True
            assert data["job_id"] == "j2"
            assert scratch.is_dir()
            assert not _under(scratch, home)
            assert not _under(scratch, active)
            assert not home.exists()
            assert not active.exists()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def test_pipeline_passes_scratch_store_paths(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)

        class _Result:
            def to_dict(self) -> dict:
                return {"job_id": kwargs["job_id"]}

        return _Result()

    import omp_work.engine_pipeline as engine_pipeline

    monkeypatch.setattr(engine_pipeline, "run_retrieve_compile_campaign", fake)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMP_ECONOMY_ACTIVE_DIR", str(tmp_path / "active"))
    rc = main(
        [
            "demo",
            "pipeline",
            "--job-id",
            "j3",
            "--query",
            "write-first",
            "--objective",
            "demo",
            "--scratch-dir",
            str(scratch),
            "--no-enola",
            "--no-ledger",
        ]
    )
    assert rc == 0
    assert captured["store_path"] == scratch / "cognee-store.json"
    assert captured["enola_store_path"] == scratch / "enola-store.json"
    assert captured["caps_path"] == scratch / "BUDGET-CAPS.json"
    assert captured["ledger_path"] == scratch / "ledger.json"
    assert captured["enola_enabled"] is False
    assert captured["ledger_attach"] is False
    active = tmp_path / "active"
    for key in ("store_path", "enola_store_path", "caps_path", "ledger_path"):
        assert not _under(Path(captured[key]), active)


def test_service_imports_skip_demo_modules() -> None:
    for target in _TARGETS:
        proc = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE, target, *_FORBIDDEN],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert proc.returncode == 0, proc.stderr
        loaded = json.loads(proc.stdout)
        assert loaded == [], (target, loaded)
