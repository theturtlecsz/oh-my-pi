from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

import omp_work
import omp_work.__main__
from omp_work.operations import database as database_module


@pytest.fixture
def prospective_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give positive disposable service tests a valid, isolated contract bundle."""
    package_root = tmp_path / "omp-work"
    shutil.copytree(
        Path(__file__).parents[1],
        package_root,
        ignore=shutil.ignore_patterns(
            ".venv", ".pytest_cache", ".coverage", "__pycache__", "*.pyc"
        ),
    )
    contract_dir = package_root / "src/omp_work/contracts/v1"
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: contract_dir)
    monkeypatch.setattr(omp_work.__main__, "_contract_dir", lambda: contract_dir)

    approval = {
        "contract_version": omp_work.CONTRACT_VERSION,
        "contract_sha256": omp_work.contract_sha256(),
        "approved_by": "owner",
        "approved_at": "2026-09-14T00:00:00Z",
        "issue": "HOME-142",
    }
    (contract_dir / "approval.json").write_text(json.dumps(approval) + "\n")

    def validate_bundle(*, require_approval: bool = True) -> None:
        omp_work.validate_bundle(require_approval=require_approval)

    monkeypatch.setattr(database_module, "validate_bundle", validate_bundle)
    return contract_dir
