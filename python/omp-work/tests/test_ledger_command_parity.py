"""Receipts must name the commands CI runs: unit step in ci.yml and package.json integration script."""

import json
from pathlib import Path

from omp_work.skip_accounting_report import catalog, repo_root

UNIT_STEP = "- name: Run Work Ledger unit tests"


def _check(name: str) -> dict:
    return next(c for c in catalog(repo_root()) if c["name"] == name)


def test_unit_command_matches_ci_step() -> None:
    lines = (repo_root() / ".github/workflows/ci.yml").read_text(encoding="utf-8").splitlines()
    step = next(i for i, line in enumerate(lines) if line.strip() == UNIT_STEP)
    run = lines[step + 1].strip()
    assert run.startswith("run: ")
    assert run[len("run: ") :] == _check("work-ledger unit tests")["command"]


def test_integration_command_matches_package_script() -> None:
    package = json.loads(Path(repo_root() / "package.json").read_text(encoding="utf-8"))
    assert _check("work-ledger integration")["command"] == package["scripts"]["test:py:work-ledger:integration"]
