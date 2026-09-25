"""OMP-283: external_delivery receipts may omit a candidate binding, every other
evidence kind still requires one, the owner approval issue set admits OMP-283,
and the record_external_delivery command validates and dispatches under
work.close."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import omp_work
from omp_work.v1.models import (
    Approval,
    CommandEnvelope,
    EvidenceKind,
    EvidenceReceipt,
    RecordExternalDeliveryCommand,
    RecordExternalDeliveryPayload,
)
from omp_work.v1.service import Principal, WorkError, WorkService

NOW = datetime(2026, 9, 25, tzinfo=UTC)
WORK = UUID("00000000-0000-7000-8000-000000000001")
REVISION = UUID("00000000-0000-7000-8000-000000000002")
CANDIDATE = UUID("00000000-0000-7000-8000-000000000003")
WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")


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


def _delivery_payload(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "work_id": WORK,
        "revision_id": REVISION,
        "evidence": "delivered to the customer on 2026-09-25",
    }
    data.update(updates)
    return data


def _delivery_envelope(**updates: object) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": WORKSPACE,
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {
                "type": "record_external_delivery",
                "payload": _delivery_payload(**updates),
            },
        }
    )


def test_record_external_delivery_command_round_trips() -> None:
    envelope = _delivery_envelope()
    assert isinstance(envelope.command, RecordExternalDeliveryCommand)
    assert envelope.command.type == "record_external_delivery"
    assert envelope.command.payload.work_id == WORK
    assert envelope.command.payload.revision_id == REVISION
    assert (
        envelope.command.payload.evidence
        == "delivered to the customer on 2026-09-25"
    )
    assert CommandEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


def test_record_external_delivery_evidence_kept_verbatim() -> None:
    payload = RecordExternalDeliveryPayload.model_validate(
        _delivery_payload(evidence="  spaced evidence  ")
    )
    assert payload.evidence == "  spaced evidence  "


def test_record_external_delivery_accepts_4096_ascii_bytes() -> None:
    payload = RecordExternalDeliveryPayload.model_validate(
        _delivery_payload(evidence="a" * 4096)
    )
    assert len(payload.evidence.encode()) == 4096


def test_record_external_delivery_refuses_4097_ascii_bytes() -> None:
    with pytest.raises(ValidationError):
        RecordExternalDeliveryPayload.model_validate(
            _delivery_payload(evidence="a" * 4097)
        )


def test_record_external_delivery_refuses_multibyte_over_4096_bytes() -> None:
    evidence = "\u00e9" * 3000
    assert len(evidence) <= 4096
    assert len(evidence.encode()) > 4096
    with pytest.raises(ValidationError):
        RecordExternalDeliveryPayload.model_validate(
            _delivery_payload(evidence=evidence)
        )


@pytest.mark.parametrize(
    "evidence",
    ["", "   ", "line\nbreak", "carriage\rreturn", "nul\x00byte"],
)
def test_record_external_delivery_refuses_blank_and_control(evidence: str) -> None:
    with pytest.raises(ValidationError):
        RecordExternalDeliveryPayload.model_validate(
            _delivery_payload(evidence=evidence)
        )


def test_record_external_delivery_refuses_unknown_payload_field() -> None:
    with pytest.raises(ValidationError):
        RecordExternalDeliveryPayload.model_validate(
            _delivery_payload(extra="nope")
        )


def test_command_types_closure_includes_record_external_delivery() -> None:
    contract = omp_work.load_contract()
    assert "record_external_delivery" in contract.command_types


def test_record_external_delivery_scope_is_work_close() -> None:
    assert WorkService._scopes["record_external_delivery"] == "work.close"


def test_record_external_delivery_requires_work_close_scope() -> None:
    service = WorkService(store=None)
    principal = Principal(
        actor_id=uuid4(),
        actor_kind="owner",
        workspaces=frozenset({WORKSPACE}),
        scopes=frozenset({"work.mutate"}),
    )
    with pytest.raises(WorkError) as exc:
        service.execute(principal, _delivery_envelope())
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403
