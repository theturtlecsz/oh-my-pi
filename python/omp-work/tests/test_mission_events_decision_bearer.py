"""OMP-514: decision 0018 records bearer tokens on mapped pushes, and Approval admits the issue."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omp_work.v1.models import Approval

ROOT = Path(__file__).parents[1]
DECISION = ROOT / "src/omp_work/contracts/v1/decisions/0018-mission-events.md"


def test_decision_0018_records_mapped_push_bearer() -> None:
    text = DECISION.read_text()
    assert "<config_dir>/push-bearer.json" in text
    assert "Authorization: Bearer <token>" in text
    assert "token_files" in text
    assert "OMP-514" in text
    assert "no bearer on pushes" not in text


def test_approval_accepts_omp_514_and_rejects_unknown_issue() -> None:
    payload = {
        "contract_version": "work.omp.dev/v1",
        "contract_sha256": "a" * 64,
        "approved_by": "owner",
        "approved_at": "2026-10-02T00:00:00Z",
    }
    approval = Approval.model_validate({**payload, "issue": "OMP-514"})
    assert approval.issue == "OMP-514"
    with pytest.raises(ValidationError):
        Approval.model_validate({**payload, "issue": "OMP-9999"})
