"""OMP-426: fold draft_mission_intake decisions into list_decisions. No Postgres."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from omp_work.v1.api_models import DecisionView
from omp_work.v1.decision_records import list_decisions

WORKSPACE = UUID("00000000-0000-7000-8000-000000000426")
PROJECT = UUID("00000000-0000-7000-8000-000000000427")


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, query: str, params: object = None) -> None:
        return None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


def _decision(
    decision_id: UUID,
    mission_id: str,
    *,
    question: str = "Publish the intake externally?",
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(PROJECT),
        "mission_id": mission_id,
        "question": question,
        "why_it_matters": "The mission cannot continue without a ruling.",
        "risk_of_delay": "The window closes and the mission stalls.",
        "options": ["publish", "hold"],
        "evidence_refs": ["receipt:intake"] if evidence_refs is None else evidence_refs,
        "default_if_any": "hold",
        "risk_of_each_choice": {
            "publish": "Irreversible external exposure.",
            "hold": "Missed deadline.",
        },
        "action_class": None,
        "resume_state": "awaiting-owner",
    }


def _row(
    event_type: str,
    payload: dict[str, object],
    occurred_at: datetime | None,
) -> dict[str, object]:
    return {"event_type": event_type, "payload": payload, "occurred_at": occurred_at}


def test_intake_decision_yields_one_pending_view() -> None:
    decision_id = uuid4()
    decision = _decision(
        decision_id,
        "OMP-426",
        evidence_refs=["receipt:intake", "note:scope"],
    )
    views = list_decisions(
        _Cursor([_row("draft_mission_intake", {"decision": decision}, None)]),
        WORKSPACE,
    )
    assert len(views) == 1
    view = views[0]
    assert view["status"] == "pending"
    assert view["mission_id"] == "OMP-426"
    assert view["evidence_refs"] == ["receipt:intake", "note:scope"]
    assert view["answer"] is None
    assert view["answered_at"] is None
    parsed = DecisionView.model_validate(view)
    assert parsed.decision_id == decision_id
    assert parsed.mission_id == "OMP-426"
    assert parsed.evidence_refs == ("receipt:intake", "note:scope")
    assert parsed.status == "pending"


def test_intake_null_decision_yields_no_row() -> None:
    views = list_decisions(
        _Cursor([_row("draft_mission_intake", {"decision": None}, None)]),
        WORKSPACE,
    )
    assert views == []


def test_later_answer_marks_intake_decision_answered() -> None:
    decision_id = uuid4()
    decision = _decision(decision_id, "OMP-426")
    answered_at = datetime(2026, 9, 30, 15, 4, tzinfo=timezone.utc)
    views = list_decisions(
        _Cursor(
            [
                _row(
                    "draft_mission_intake",
                    {"decision": decision},
                    datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc),
                ),
                _row(
                    "answer_decision",
                    {"decision_id": str(decision_id), "answer": "hold"},
                    answered_at,
                ),
            ]
        ),
        WORKSPACE,
    )
    assert len(views) == 1
    view = views[0]
    assert view["status"] == "answered"
    assert view["answer"] == "hold"
    assert view["answered_at"] == answered_at.isoformat()
    assert view["mission_id"] == "OMP-426"
    parsed = DecisionView.model_validate(view)
    assert parsed.status == "answered"
    assert parsed.answer == "hold"
    assert parsed.answered_at == answered_at


def test_create_and_intake_interleave_in_event_order() -> None:
    intake_first = _decision(uuid4(), "M-1")
    created = _decision(uuid4(), "M-2")
    intake_last = _decision(uuid4(), "M-3")
    views = list_decisions(
        _Cursor(
            [
                _row("draft_mission_intake", {"decision": intake_first}, None),
                _row(
                    "create_decision",
                    {"type": "create_decision", "status": "pending", "decision": created},
                    None,
                ),
                _row("draft_mission_intake", {"decision": intake_last}, None),
            ]
        ),
        WORKSPACE,
    )
    assert [view["decision_id"] for view in views] == [
        intake_first["decision_id"],
        created["decision_id"],
        intake_last["decision_id"],
    ]
    assert [view["mission_id"] for view in views] == ["M-1", "M-2", "M-3"]


def test_repeated_intake_same_decision_id_adds_no_second_row() -> None:
    decision_id = uuid4()
    first = _decision(decision_id, "M-1", question="First ruling?")
    second = _decision(
        decision_id,
        "M-2",
        question="Second ruling?",
        evidence_refs=["receipt:later"],
    )
    views = list_decisions(
        _Cursor(
            [
                _row("draft_mission_intake", {"decision": first}, None),
                _row("draft_mission_intake", {"decision": second}, None),
            ]
        ),
        WORKSPACE,
    )
    assert len(views) == 1
    assert views[0]["decision_id"] == str(decision_id)
    assert views[0]["question"] == "First ruling?"
    assert views[0]["mission_id"] == "M-1"
    assert views[0]["evidence_refs"] == ["receipt:intake"]
