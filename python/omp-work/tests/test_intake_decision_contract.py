from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import omp_work
import pytest
from omp_work.v1.api_models import AnswerIntakeDecisionResult, CommandResult
from omp_work.v1.models import AnswerIntakeDecisionCommand, CommandEnvelope
from omp_work.v1.service import WorkService
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).parents[1]
INTAKE_HOLD = ROOT / "src/omp_work/v1/intake_hold.py"
DECISION = (
    ROOT / "src/omp_work/contracts/v1/decisions/0012-bot-filed-intake-hold.md"
)


def _envelope(payload: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": "answer_intake_decision", "payload": payload},
        }
    )


def test_answer_intake_decision_envelope_parses() -> None:
    work_id = uuid4()
    envelope = _envelope({"work_id": str(work_id), "answer": "approve"})
    assert isinstance(envelope.command, AnswerIntakeDecisionCommand)
    assert envelope.command.type == "answer_intake_decision"
    assert envelope.command.payload.work_id == work_id
    assert envelope.command.payload.answer == "approve"


def test_answer_intake_decision_rejects_other_answer_and_extra_field() -> None:
    work_id = str(uuid4())
    with pytest.raises(ValidationError):
        _envelope({"work_id": work_id, "answer": "reject"})
    with pytest.raises(ValidationError):
        _envelope({"work_id": work_id, "answer": "approve", "note": "extra"})


def test_answer_intake_decision_result_parses_in_command_result() -> None:
    answered_at = datetime(2026, 9, 28, tzinfo=UTC)
    payload = {
        "type": "answer_intake_decision",
        "work_id": str(uuid4()),
        "answer": "approve",
        "answered_at": answered_at,
    }
    result = AnswerIntakeDecisionResult.model_validate(payload)
    assert result.answered_at == answered_at
    parsed = TypeAdapter(CommandResult).validate_python(payload)
    assert isinstance(parsed, AnswerIntakeDecisionResult)


def test_bundle_valid_without_approval() -> None:
    omp_work.validate_bundle(require_approval=False)


def test_answer_intake_decision_scope_is_work_approve() -> None:
    assert WorkService._scopes["answer_intake_decision"] == "work.approve"


def test_decision_rule_matches_intake_hold_docstring() -> None:
    docstring = ast.get_docstring(ast.parse(INTAKE_HOLD.read_text()), clean=False)
    assert docstring is not None
    decision = DECISION.read_text()
    rule = decision.split("## Rule\n\n", 1)[1].split("\n\n", 1)[0]
    assert rule == docstring
