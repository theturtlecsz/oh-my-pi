"""OMP-403: decision target binding contract.

The decision contract accepts an optional target digest on the record and an
optional expiry on the answer. A malformed digest or a naive expiry is refused
at the boundary, a view carrying both fields round-trips, and payloads that set
neither keep validating unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from omp_work.v1.api_models import AnswerDecisionResult, DecisionView
from omp_work.v1.models import (
    AnswerDecisionPayload,
    CreateDecisionPayload,
)
from pydantic import ValidationError

TARGET = "a" * 64


def _create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "decision_id": str(uuid4()),
        "project_id": str(uuid4()),
        "mission_id": "OMP-403",
        "question": "Apply this target?",
        "why_it_matters": "The approval must name the bytes it covers.",
        "risk_of_delay": "The action waits until the owner answers.",
        "options": ("approve", "decline"),
        "evidence_refs": ("receipt:target-binding",),
        "default_if_any": "decline",
        "risk_of_each_choice": {
            "approve": "The named target may proceed once.",
            "decline": "The action does not run.",
        },
        "action_class": "contract_hash",
        "resume_state": "target-approved",
    }
    body.update(overrides)
    return body


def test_target_must_be_a_lowercase_hex64_digest() -> None:
    accepted = CreateDecisionPayload.model_validate(_create_body(target_sha256=TARGET))
    assert accepted.target_sha256 == TARGET
    for bad in ("abc", "A" * 64, "g" * 64, "a" * 63):
        with pytest.raises(ValidationError):
            CreateDecisionPayload.model_validate(_create_body(target_sha256=bad))


def test_expiry_must_be_timezone_aware() -> None:
    decision_id = uuid4()
    aware = AnswerDecisionPayload.model_validate(
        {
            "decision_id": str(decision_id),
            "answer": "approve",
            "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
    )
    assert aware.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        AnswerDecisionPayload.model_validate(
            {
                "decision_id": str(decision_id),
                "answer": "approve",
                "expires_at": "2099-01-01T00:00:00",
            }
        )


def test_decision_view_round_trips_both_fields() -> None:
    body = _create_body(target_sha256=TARGET)
    body.pop("resume_state")
    view = DecisionView.model_validate(
        {
            **body,
            "status": "answered",
            "answer": "approve",
            "answered_at": datetime(2026, 9, 30, tzinfo=timezone.utc).isoformat(),
            "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
    )
    assert view.target_sha256 == TARGET
    assert view.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert view.model_dump(mode="json")["target_sha256"] == TARGET


def test_payloads_without_the_new_fields_still_validate() -> None:
    assert CreateDecisionPayload.model_validate(_create_body()).target_sha256 is None
    answer = AnswerDecisionPayload.model_validate(
        {"decision_id": str(uuid4()), "answer": "approve"}
    )
    assert answer.expires_at is None


def test_answer_result_omits_unset_expiry_and_keeps_a_set_one() -> None:
    decision_id = uuid4()
    unset = AnswerDecisionResult.model_validate(
        {
            "type": "answer_decision",
            "decision_id": str(decision_id),
            "mission_id": "OMP-414",
            "answer": "approve",
            "resume_state": "contract-approved",
        }
    )
    assert unset.model_dump(mode="json") == {
        "type": "answer_decision",
        "decision_id": str(decision_id),
        "mission_id": "OMP-414",
        "answer": "approve",
        "resume_state": "contract-approved",
    }
    expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    set_result = AnswerDecisionResult.model_validate(
        {
            "type": "answer_decision",
            "decision_id": str(decision_id),
            "answer": "approve",
            "expires_at": expires_at.isoformat(),
        }
    )
    assert (
        datetime.fromisoformat(set_result.model_dump(mode="json")["expires_at"])
        == expires_at
    )
