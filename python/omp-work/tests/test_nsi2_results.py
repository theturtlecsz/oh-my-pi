from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from omp_work.v1.api_models import (
    AssessIntakeResult,
    CommandResult,
    IntakeFollowOnResult,
    IntakeWriteResult,
    LinkIntakeCriterionResult,
    RecordIntakeClaimResult,
    RecordIntakeDecisionResult,
    RecordIntakeIssueResult,
    RegisterIntakeEvidenceResult,
    StartIntakeResult,
    SupersedeIntakeClaimResult,
)

FOLLOW_ON_CLASSES = [
    ("register_intake_evidence", RegisterIntakeEvidenceResult),
    ("record_intake_claim", RecordIntakeClaimResult),
    ("record_intake_decision", RecordIntakeDecisionResult),
    ("supersede_intake_claim", SupersedeIntakeClaimResult),
    ("record_intake_issue", RecordIntakeIssueResult),
    ("link_intake_criterion", LinkIntakeCriterionResult),
]


def test_start_intake_result_accepts_expected_0_revision_1() -> None:
    res = StartIntakeResult(
        type="start_intake",
        run_id="run-1",
        expected_revision=0,
        revision=1,
    )
    assert res.run_id == "run-1"
    assert res.expected_revision == 0
    assert res.revision == 1
    assert res.type == "start_intake"

    parsed = TypeAdapter(CommandResult).validate_python({
        "type": "start_intake",
        "run_id": "run-1",
        "expected_revision": 0,
        "revision": 1,
    })
    assert isinstance(parsed, StartIntakeResult)


def test_start_intake_result_rejects_expected_1_revision_2() -> None:
    with pytest.raises(ValidationError):
        StartIntakeResult(
            type="start_intake",
            run_id="run-1",
            expected_revision=1,
            revision=2,
        )

    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_python({
            "type": "start_intake",
            "run_id": "run-1",
            "expected_revision": 1,
            "revision": 2,
        })


@pytest.mark.parametrize("type_literal,cls", FOLLOW_ON_CLASSES)
def test_follow_on_result_accepts_expected_1_revision_2(
    type_literal: str, cls: type[IntakeFollowOnResult]
) -> None:
    res = cls(
        type=type_literal,
        run_id="run-1",
        expected_revision=1,
        revision=2,
    )
    assert res.run_id == "run-1"
    assert res.expected_revision == 1
    assert res.revision == 2
    assert res.type == type_literal

    parsed = TypeAdapter(CommandResult).validate_python({
        "type": type_literal,
        "run_id": "run-1",
        "expected_revision": 1,
        "revision": 2,
    })
    assert isinstance(parsed, cls)


@pytest.mark.parametrize("type_literal,cls", FOLLOW_ON_CLASSES)
def test_follow_on_result_rejects_expected_0_revision_1(
    type_literal: str, cls: type[IntakeFollowOnResult]
) -> None:
    with pytest.raises(ValidationError):
        cls(
            type=type_literal,
            run_id="run-1",
            expected_revision=0,
            revision=1,
        )

    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_python({
            "type": type_literal,
            "run_id": "run-1",
            "expected_revision": 0,
            "revision": 1,
        })


def test_assess_intake_result_accepts_revision_2_assessed_revision_2() -> None:
    res = AssessIntakeResult(
        type="assess_intake",
        run_id="run-1",
        revision=2,
        assessed_revision=2,
    )
    assert res.run_id == "run-1"
    assert res.revision == 2
    assert res.assessed_revision == 2
    assert res.type == "assess_intake"

    parsed = TypeAdapter(CommandResult).validate_python({
        "type": "assess_intake",
        "run_id": "run-1",
        "revision": 2,
        "assessed_revision": 2,
    })
    assert isinstance(parsed, AssessIntakeResult)


def test_assess_intake_result_rejects_revision_3_assessed_revision_2() -> None:
    with pytest.raises(ValidationError):
        AssessIntakeResult(
            type="assess_intake",
            run_id="run-1",
            revision=3,
            assessed_revision=2,
        )

    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_python({
            "type": "assess_intake",
            "run_id": "run-1",
            "revision": 3,
            "assessed_revision": 2,
        })


def test_assess_intake_result_has_no_verdict_field() -> None:
    assert "verdict" not in AssessIntakeResult.model_fields
    assert "status" not in AssessIntakeResult.model_fields
    assert "ready" not in AssessIntakeResult.model_fields

    with pytest.raises(ValidationError):
        AssessIntakeResult.model_validate({
            "type": "assess_intake",
            "run_id": "run-1",
            "revision": 2,
            "assessed_revision": 2,
            "verdict": "PASS",
        })

    with pytest.raises(ValidationError):
        AssessIntakeResult.model_validate({
            "type": "assess_intake",
            "run_id": "run-1",
            "revision": 2,
            "assessed_revision": 2,
            "status": "ready",
        })

    with pytest.raises(ValidationError):
        AssessIntakeResult.model_validate({
            "type": "assess_intake",
            "run_id": "run-1",
            "revision": 2,
            "assessed_revision": 2,
            "ready": True,
        })
