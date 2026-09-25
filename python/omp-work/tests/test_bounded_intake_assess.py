from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

import omp_work
from omp_work.v1.api_models import AssessBoundedIntakeResult, CommandResponse
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    AssessBoundedIntakeCommand,
    AssessBoundedIntakePayload,
    BoundedIntakeDraft,
    CommandEnvelope,
    IntakeAcceptanceCriterion,
    IntakeBlockingQuestion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    KnownIntakeValue,
)
from omp_work.v1.service import WorkService


def _valid_draft() -> BoundedIntakeDraft:
    text = "Optimize cache eviction policy to avoid unbounded memory growth in worker pool."
    s1_text = "Optimize cache eviction policy"
    s1_start = text.index(s1_text)
    s1_end = s1_start + len(s1_text.encode("utf-8"))
    span1 = IntakeSourceSpan(
        id="span-1",
        start=s1_start,
        end=s1_end,
        exact_text_sha256=text_sha256(s1_text),
    )
    source = IntakeSource(
        text=text,
        sha256=text_sha256(text),
        spans=(span1,),
    )
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=source,
        goal=IntakeGoal(
            id="claim-goal-1",
            statement="Bound memory growth in cache",
            source_span_ids=("span-1",),
        ),
        constraints=(
            IntakeConstraint(
                id="claim-constraint-1",
                statement="Must not regress hit latency",
                source_span_ids=("span-1",),
                key="max_latency_ms",
                value=KnownIntakeValue(value=5),
                polarity="positive",
            ),
        ),
        unknowns=(),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="claim-ac-1",
                statement="Verify latency under load",
                observable_outcome="latency remains < 5ms",
                oracle="automated_test",
            ),
        ),
    )


def test_command_envelope_accepts_assess_command_with_valid_draft() -> None:
    draft = _valid_draft()
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": uuid4(),
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {
                "type": "assess_bounded_intake",
                "payload": {
                    "draft": draft.model_dump(mode="json"),
                },
            },
        }
    )
    assert isinstance(envelope.command, AssessBoundedIntakeCommand)
    assert envelope.command.type == "assess_bounded_intake"
    assert envelope.command.payload.draft.goal.statement == "Bound memory growth in cache"


def test_command_envelope_rejects_unknown_payload_fields() -> None:
    draft = _valid_draft()
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "api_version": "work.omp.dev/v1",
                "workspace_id": uuid4(),
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "correlation_id": uuid4(),
                "command": {
                    "type": "assess_bounded_intake",
                    "payload": {
                        "draft": draft.model_dump(mode="json"),
                        "unknown_field": "not_allowed",
                    },
                },
            }
        )


def test_assess_bounded_intake_payload_rejects_unknown_fields() -> None:
    draft = _valid_draft()
    with pytest.raises(ValidationError):
        AssessBoundedIntakePayload.model_validate(
            {
                "draft": draft.model_dump(mode="json"),
                "unrecognized_field": True,
            }
        )


def test_command_response_accepts_assess_result() -> None:
    response = CommandResponse.model_validate(
        {
            "receipt": {
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "state": "applied",
                "request_sha256": "0" * 64,
                "result_sha256": "1" * 64,
            },
            "result": {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
            },
        }
    )
    assert isinstance(response.result, AssessBoundedIntakeResult)
    assert response.result.type == "assess_bounded_intake"
    assert response.result.semantic_sha256 == "a" * 64
    assert response.result.rule_bundle_sha256 == "b" * 64
    assert response.result.ready_for_ratification is True
    assert response.result.issue_count == 0
    assert response.result.questions == ()


def test_command_response_accepts_assess_result_with_blocking_questions() -> None:
    question = IntakeBlockingQuestion(
        rule_class="missing_verification_oracle",
        deduplication_key="oracle:claim-ac-1",
        statement="missing_verification_oracle:oracle:claim-ac-1",
        priority=1,
        claim_ids=("claim-ac-1",),
    )
    response = CommandResponse.model_validate(
        {
            "receipt": {
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "state": "applied",
                "request_sha256": "0" * 64,
                "result_sha256": "1" * 64,
            },
            "result": {
                "type": "assess_bounded_intake",
                "semantic_sha256": "c" * 64,
                "rule_bundle_sha256": "d" * 64,
                "ready_for_ratification": False,
                "issue_count": 1,
                "questions": [question.model_dump(mode="json")],
            },
        }
    )
    assert isinstance(response.result, AssessBoundedIntakeResult)
    assert response.result.ready_for_ratification is False
    assert response.result.issue_count == 1
    assert len(response.result.questions) == 1
    assert response.result.questions[0].deduplication_key == "oracle:claim-ac-1"


def test_assess_bounded_intake_result_validates_bounds() -> None:
    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": -1,
                "questions": [],
            }
        )

    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "not-a-sha",
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
            }
        )

    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
                "extra_field": "forbidden",
            }
        )


def test_assess_bounded_intake_scope_is_work_approve() -> None:
    assert WorkService._scopes["assess_bounded_intake"] == "work.approve"


def test_command_types_closure_includes_assess_bounded_intake() -> None:
    contract = omp_work.load_contract()
    assert "assess_bounded_intake" in contract.command_types
    assert "assess_bounded_intake" in omp_work._COMMAND_TYPES
