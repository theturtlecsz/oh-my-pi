"""OMP-283: external_delivery receipts may omit a candidate binding, every other
evidence kind still requires one, and the owner approval issue set admits OMP-283."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

import omp_work
from omp_work.v1.models import Approval, EvidenceKind, EvidenceReceipt

NOW = datetime(2026, 9, 25, tzinfo=UTC)
WORK = UUID("00000000-0000-7000-8000-000000000001")
REVISION = UUID("00000000-0000-7000-8000-000000000002")
CANDIDATE = UUID("00000000-0000-7000-8000-000000000003")


def _receipt(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "receipt_id": UUID("00000000-0000-7000-8000-000000000004"),
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_id": CANDIDATE,
        "kind": EvidenceKind.VERIFICATION,
        "payload": {"note": "verification"},
        "payload_sha256": "a" * 64,
        "issuer": "owner",
        "issued_at": NOW,
    }
    data.update(updates)
    return data


def test_external_delivery_receipt_allows_null_candidate() -> None:
    receipt = EvidenceReceipt.model_validate(
        _receipt(kind=EvidenceKind.EXTERNAL_DELIVERY, candidate_id=None)
    )
    assert receipt.kind is EvidenceKind.EXTERNAL_DELIVERY
    assert receipt.candidate_id is None


def test_external_delivery_receipt_allows_candidate() -> None:
    receipt = EvidenceReceipt.model_validate(
        _receipt(kind=EvidenceKind.EXTERNAL_DELIVERY, candidate_id=CANDIDATE)
    )
    assert receipt.candidate_id == CANDIDATE


def test_non_external_delivery_receipt_requires_candidate() -> None:
    with pytest.raises(ValidationError):
        EvidenceReceipt.model_validate(
            _receipt(kind=EvidenceKind.VERIFICATION, candidate_id=None)
        )


def test_approval_accepts_omp_283_issue() -> None:
    approval = Approval.model_validate(
        {
            "contract_version": omp_work.CONTRACT_VERSION,
            "contract_sha256": "e" * 64,
            "approved_by": "owner",
            "approved_at": "2026-09-25T00:00:00Z",
            "issue": "OMP-283",
        }
    )
    assert approval.issue == "OMP-283"
