"""OMP-295: owner approval carries a command-marker attestation.

`approve` writes the attestation; `validate --require-approval` recomputes it
from the raw approval.json fields and refuses a missing or mismatched marker.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

import omp_work
import omp_work.__main__


def _setup_contract_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package_root = tmp_path / "omp-work"
    shutil.copytree(Path(__file__).parents[1], package_root)
    contract_dir = package_root / "src/omp_work/contracts/v1"
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: contract_dir)
    monkeypatch.setattr(omp_work.__main__, "_contract_dir", lambda: contract_dir)
    return contract_dir


def _approve_via_faked_tty(
    monkeypatch: pytest.MonkeyPatch, issue: str = "OMP-295"
) -> None:
    digest = omp_work.contract_sha256()
    fake_stdin = io.StringIO(f"{digest}\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    assert omp_work.__main__.main(["approve", "--issue", issue]) == 0


def test_approve_then_validate_require_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    _approve_via_faked_tty(monkeypatch)
    raw = json.loads((contract_dir / "approval.json").read_text())
    assert raw["attestation"] == omp_work.approval_attestation(
        raw["contract_sha256"], raw["issue"], raw["approved_at"]
    )
    assert omp_work.__main__.main(["validate", "--require-approval"]) == 0


def test_validate_require_approval_rejects_missing_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval = {
        "contract_version": omp_work.CONTRACT_VERSION,
        "contract_sha256": omp_work.contract_sha256(),
        "approved_by": "owner",
        "approved_at": "2026-09-25T00:00:00+00:00",
        "issue": "OMP-295",
    }
    (contract_dir / "approval.json").write_text(json.dumps(approval))
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main(["validate", "--require-approval"])
    assert str(exc.value) == "approval attestation missing or invalid"


def test_validate_require_approval_rejects_foreign_issue_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approved_at = "2026-09-25T00:00:00+00:00"
    approval = {
        "contract_version": omp_work.CONTRACT_VERSION,
        "contract_sha256": omp_work.contract_sha256(),
        "approved_by": "owner",
        "approved_at": approved_at,
        "issue": "OMP-295",
        "attestation": omp_work.approval_attestation(
            omp_work.contract_sha256(), "OMP-266", approved_at
        ),
    }
    (contract_dir / "approval.json").write_text(json.dumps(approval))
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main(["validate", "--require-approval"])
    assert str(exc.value) == "approval attestation missing or invalid"
