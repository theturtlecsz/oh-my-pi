"""Service imports stay off demo modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys

_FORBIDDEN = (
    "cockpit_verbs",
    "cockpit_persist",
    "chaos_suite",
    "durability_a1",
    "full_path",
    "enola_adapter",
    "enola_store",
    "research_campaign",
    "campaign_budget_guard",
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
