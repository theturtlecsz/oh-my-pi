from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omp_work.v1.api_models import (
    AttestIntakeAdmissionResult,
    CommandResponse,
    StoredOperationView,
)
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    EvidenceKind,
    EvidenceReceipt,
    OperationReceipt,
    OperationState,
)


def _make_store_result_dict(
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    actor_id: UUID,
) -> dict[str, object]:
    body = {
        "work_id": str(work_id),
        "revision_id": str(revision_id),
        "intake_schema_version": "1.0",
        "semantic_sha256": "a" * 64,
        "rule_bundle_sha256": "b" * 64,
        "advice_id": str(uuid4()),
        "ratified_at": datetime.now(UTC).isoformat(),
    }
    receipt = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        kind=EvidenceKind.INTAKE_ADMISSION,
        payload=body,
        payload_sha256=sha256(body),
        issuer="work-service/intake-admission",
        issued_at=datetime.now(UTC),
        candidate_sha256="c" * 64,
        candidate_commit="d" * 40,
    )
    return {
        "type": "attest_intake_admission",
        "receipt": receipt.model_dump(mode="json"),
        "operator_actor_id": str(actor_id),
    }


def test_attest_intake_admission_result_validates() -> None:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    actor_id = uuid4()

    result_dict = _make_store_result_dict(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        actor_id=actor_id,
    )

    # Exactly the store's keys
    assert set(result_dict.keys()) == {"type", "receipt", "operator_actor_id"}
    receipt_dict = result_dict["receipt"]
    assert isinstance(receipt_dict, dict)
    assert receipt_dict["kind"] == "intake_admission"

    op_receipt = OperationReceipt(
        operation_id=uuid4(),
        request_id=uuid4(),
        state=OperationState.APPLIED,
        request_sha256="1" * 64,
        result_sha256="2" * 64,
    )

    # Validates through CommandResponse
    command_response = CommandResponse.model_validate({
        "receipt": op_receipt.model_dump(mode="json"),
        "result": result_dict,
    })
    assert isinstance(command_response.result, AttestIntakeAdmissionResult)
    assert command_response.result.type == "attest_intake_admission"
    assert command_response.result.receipt.kind == EvidenceKind.INTAKE_ADMISSION
    assert command_response.result.receipt.receipt_id == UUID(receipt_dict["receipt_id"])
    assert command_response.result.operator_actor_id == actor_id

    # Validates through StoredOperationView
    stored_op = StoredOperationView.model_validate({
        "receipt": op_receipt.model_dump(mode="json"),
        "command_type": "attest_intake_admission",
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "result": result_dict,
    })
    assert isinstance(stored_op.result, AttestIntakeAdmissionResult)
    assert stored_op.result.type == "attest_intake_admission"
    assert stored_op.result.operator_actor_id == actor_id


def test_unregistered_command_result_fails_with_union_tag_invalid() -> None:
    bad_result = {
        "type": "nonexistent_result_type",
        "receipt": {},
        "operator_actor_id": str(uuid4()),
    }
    with pytest.raises(ValidationError) as exc_info:
        CommandResponse.model_validate({
            "receipt": {
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "state": "applied",
                "request_sha256": "1" * 64,
                "result_sha256": "2" * 64,
            },
            "result": bad_result,
        })
    errors = exc_info.value.errors()
    assert any(e["type"] == "union_tag_invalid" for e in errors)
