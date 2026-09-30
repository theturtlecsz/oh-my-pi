"""OMP-415-s03: derive mission events from applied domain events."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omp_work.mission_events import derive_mission_events

WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
LOG = "LOG-TEXT-must-not-leak"
QUESTION = "QUESTION-TEXT-must-not-leak"
OBJECTIVE = "OBJECTIVE-TEXT-must-not-leak"
FINDING_BODY = "FINDING-BODY-must-not-leak"

MISSION = UUID("11111111-1111-4111-8111-111111111111")
OTHER_MISSION = UUID("22222222-2222-4222-8222-222222222222")
WORK = UUID("33333333-3333-4333-8333-333333333333")
OTHER_WORK = UUID("44444444-4444-4444-8444-444444444444")
DECISION = UUID("55555555-5555-4555-8555-555555555555")
FINDING = UUID("66666666-6666-4666-8666-666666666666")
CRITICAL_FINDING = UUID("77777777-7777-4777-8777-777777777777")

_OUTPUT_KEYS = {
    "mission_event_id",
    "sequence",
    "mission_id",
    "type",
    "trigger",
    "occurred_at",
    "source_event_id",
    "evidence_refs",
}


def _id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"omp-415-s03/{name}"))


def _event(
    name: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    sequence: int,
    outcome: str = "applied",
    aggregate_id: Any = None,
) -> dict[str, Any]:
    body = dict(payload)
    body.setdefault("log", LOG)
    return {
        "event_id": _id(name),
        "sequence": sequence,
        "outcome": outcome,
        "event_type": event_type,
        "aggregate_id": aggregate_id,
        "occurred_at": WHEN,
        "payload": body,
    }


def _mission(
    mission_id: UUID,
    *,
    transitions: list[dict[str, Any]] | None = None,
    budget: dict[str, str] | None = None,
    drawn: str | None = None,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mission: dict[str, Any] = {
        "mission_id": str(mission_id),
        "objective": OBJECTIVE,
        "log": LOG,
    }
    if transitions is not None:
        mission["transitions"] = transitions
    if budget is not None:
        mission["budget"] = budget
    elif budget is None and drawn is not None:
        mission["budget"] = None
    if drawn is not None:
        mission["drawn"] = {"usd": drawn}
    if links is not None:
        mission["links"] = links
    return mission


def _link(work_id: UUID, usd: str) -> dict[str, Any]:
    return {"work_id": str(work_id), "budget": {"usd": usd}}


def _status(name: str, sequence: int, transitions: list[dict[str, str]]) -> dict[str, Any]:
    return _event(
        name,
        "set_mission_status",
        {"mission": _mission(MISSION, transitions=transitions)},
        sequence=sequence,
        aggregate_id=MISSION,
    )


def _expected_mission_event_id(event_id: str, type_: str) -> str:
    namespace = uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events")
    return str(uuid5(namespace, f"{event_id}:{type_}"))


def _leaks(value: Any) -> bool:
    if isinstance(value, str):
        return LOG in value or QUESTION in value or OBJECTIVE in value or FINDING_BODY in value
    if isinstance(value, dict):
        return any(_leaks(key) or _leaks(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_leaks(item) for item in value)
    return False


def _missions(work_id: Any, sequence: Any) -> UUID | None:
    assert sequence is not None
    if work_id == WORK:
        return MISSION
    return None


def test_all_types_and_both_progressed_triggers() -> None:
    started = _status(
        "started",
        1,
        [
            {"from_status": "running", "to_status": "paused"},
            {"from_status": "approved", "to_status": "running"},
        ],
    )
    blocked = _status("blocked", 2, [{"from_status": "running", "to_status": "blocked"}])
    completed = _status("completed", 3, [{"from_status": "running", "to_status": "completed"}])
    failed = _status("failed", 4, [{"from_status": "running", "to_status": "failed"}])
    stage = _status("stage", 5, [{"from_status": "paused", "to_status": "running"}])
    completed_work = _event(
        "complete",
        "complete_work",
        {"work_id": str(WORK), "state": "done", "log": LOG},
        sequence=6,
        aggregate_id=WORK,
    )
    decision = _event(
        "decision",
        "create_decision",
        {
            "decision": {
                "decision_id": str(DECISION),
                "mission_id": str(MISSION),
                "question": QUESTION,
                "evidence_refs": ["receipt:abc", "diff:1"],
                "log": LOG,
            }
        },
        sequence=7,
        aggregate_id=DECISION,
    )
    threshold = _event(
        "threshold",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="90",
                links=[_link(WORK, "90")],
            )
        },
        sequence=8,
        aggregate_id=MISSION,
    )
    finding = _event(
        "finding",
        "record_finding",
        {
            "finding": {
                "finding_id": str(FINDING),
                "mission_id": str(MISSION),
                "severity": "high",
                "summary": FINDING_BODY,
                "evidence_refs": ["receipt:abc"],
                "log": LOG,
            }
        },
        sequence=9,
        aggregate_id=FINDING,
    )
    critical = _event(
        "critical",
        "record_finding",
        {
            "severity": "critical",
            "finding_id": str(CRITICAL_FINDING),
            "mission_id": str(MISSION),
            "summary": FINDING_BODY,
        },
        sequence=10,
        aggregate_id=CRITICAL_FINDING,
    )
    events = [
        started,
        blocked,
        completed,
        failed,
        stage,
        completed_work,
        decision,
        threshold,
        finding,
        critical,
    ]
    snapshot = deepcopy(events)
    rows = derive_mission_events(events, mission_for_work=_missions)
    assert events == snapshot
    assert [row["type"] for row in rows] == [
        "mission.started",
        "mission.blocked",
        "mission.completed",
        "mission.failed",
        "mission.progressed",
        "mission.progressed",
        "decision.required",
        "budget.threshold_reached",
        "important_finding",
        "important_finding",
    ]
    assert {row["type"] for row in rows} == {
        "mission.started",
        "mission.blocked",
        "mission.completed",
        "mission.failed",
        "mission.progressed",
        "decision.required",
        "budget.threshold_reached",
        "important_finding",
    }
    progressed = [row for row in rows if row["type"] == "mission.progressed"]
    assert [row["trigger"] for row in progressed] == ["stage_change", "operation_completed"]
    assert [row["trigger"] for row in rows] == [
        "status:approved->running",
        "status:blocked",
        "status:completed",
        "status:failed",
        "stage_change",
        "operation_completed",
        "decision_created",
        "budget_usd>=0.8",
        "finding_severity>=high",
        "finding_severity>=high",
    ]
    assert all(set(row) == _OUTPUT_KEYS for row in rows)
    assert all(row["mission_id"] == str(MISSION) for row in rows)
    assert all(row["occurred_at"] == WHEN for row in rows)
    assert [row["sequence"] for row in rows] == list(range(1, 11))
    assert [row["source_event_id"] for row in rows] == [event["event_id"] for event in events]
    for row in rows:
        assert row["mission_event_id"] == _expected_mission_event_id(row["source_event_id"], row["type"])
        assert not _leaks(row)

    assert rows[0]["evidence_refs"] == [{"kind": "domain_event", "ref": started["event_id"]}]
    assert rows[5]["evidence_refs"] == [
        {"kind": "domain_event", "ref": completed_work["event_id"]},
        {"kind": "work_item", "ref": str(WORK)},
    ]
    assert rows[5]["mission_id"] == str(MISSION)
    assert rows[6]["evidence_refs"] == [
        {"kind": "domain_event", "ref": decision["event_id"]},
        {"kind": "decision", "ref": str(DECISION)},
        {"kind": "evidence", "ref": "receipt:abc"},
        {"kind": "evidence", "ref": "diff:1"},
    ]
    assert rows[7]["evidence_refs"] == [
        {"kind": "domain_event", "ref": threshold["event_id"]},
        {"kind": "work_item", "ref": str(WORK)},
    ]
    assert rows[8]["evidence_refs"] == [
        {"kind": "domain_event", "ref": finding["event_id"]},
        {"kind": "finding", "ref": str(FINDING)},
        {"kind": "evidence", "ref": "receipt:abc"},
    ]
    assert rows[9]["evidence_refs"] == [
        {"kind": "domain_event", "ref": critical["event_id"]},
        {"kind": "finding", "ref": str(CRITICAL_FINDING)},
    ]


def test_ids_are_stable() -> None:
    event = _status("started", 1, [{"from_status": "approved", "to_status": "running"}])
    first = derive_mission_events([event], mission_for_work=_missions)
    second = derive_mission_events([event], mission_for_work=_missions)
    assert first == second
    assert first[0]["mission_event_id"] == _expected_mission_event_id(event["event_id"], "mission.started")


def test_silent_rows_emit_nothing() -> None:
    calls: list[tuple[Any, Any]] = []

    def mission_for_work(work_id: Any, sequence: Any) -> UUID | None:
        calls.append((work_id, sequence))
        return None

    events = [
        _event(
            "low",
            "record_finding",
            {
                "finding": {
                    "finding_id": str(FINDING),
                    "mission_id": str(MISSION),
                    "severity": "low",
                    "summary": FINDING_BODY,
                    "evidence_refs": ["receipt:abc"],
                }
            },
            sequence=1,
            aggregate_id=FINDING,
        ),
        _event(
            "medium",
            "record_finding",
            {"severity": "medium", "finding_id": str(FINDING), "mission_id": str(MISSION)},
            sequence=2,
            aggregate_id=FINDING,
        ),
        _status(
            "abandoned",
            3,
            [
                {"from_status": "approved", "to_status": "running"},
                {"from_status": "running", "to_status": "abandoned"},
            ],
        ),
        _event(
            "refused",
            "set_mission_status",
            {"mission": _mission(MISSION, transitions=[{"from_status": "approved", "to_status": "running"}])},
            sequence=4,
            outcome="refused",
            aggregate_id=MISSION,
        ),
        _event(
            "unlinked",
            "complete_work",
            {"work_id": str(OTHER_WORK), "log": LOG},
            sequence=5,
            aggregate_id=OTHER_WORK,
        ),
        _event(
            "not-uuid",
            "create_decision",
            {
                "decision": {
                    "decision_id": str(DECISION),
                    "mission_id": "OMP-415",
                    "question": QUESTION,
                    "evidence_refs": ["receipt:abc"],
                }
            },
            sequence=6,
            aggregate_id=DECISION,
        ),
        _event(
            "approve",
            "approve_mission",
            {"mission": _mission(MISSION, transitions=[{"from_status": "approved", "to_status": "running"}])},
            sequence=7,
            aggregate_id=MISSION,
        ),
    ]
    assert derive_mission_events(events, mission_for_work=mission_for_work) == []
    assert calls == [(OTHER_WORK, 5)]


def test_budget_threshold_fires_once_over_two_links() -> None:
    crossing = _event(
        "cross",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="90",
                links=[_link(WORK, "90")],
            )
        },
        sequence=1,
        aggregate_id=MISSION,
    )
    still_over = _event(
        "over",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="95",
                links=[_link(WORK, "90"), _link(OTHER_WORK, "5")],
            )
        },
        sequence=2,
        aggregate_id=MISSION,
    )
    rows = derive_mission_events([crossing, still_over], mission_for_work=_missions)
    assert len(rows) == 1
    assert rows[0]["source_event_id"] == crossing["event_id"]
    assert rows[0]["type"] == "budget.threshold_reached"
    assert rows[0]["trigger"] == "budget_usd>=0.8"
    assert rows[0]["evidence_refs"][-1] == {"kind": "work_item", "ref": str(WORK)}

    under = _event(
        "under",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="50",
                links=[_link(WORK, "50")],
            )
        },
        sequence=3,
        aggregate_id=MISSION,
    )
    exact = _event(
        "exact",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="80",
                links=[_link(WORK, "80")],
            )
        },
        sequence=4,
        aggregate_id=MISSION,
    )
    already = _event(
        "already",
        "link_mission_work",
        {
            "mission": _mission(
                MISSION,
                budget={"usd": "100"},
                drawn="90",
                links=[_link(WORK, "80"), _link(OTHER_WORK, "10")],
            )
        },
        sequence=5,
        aggregate_id=MISSION,
    )
    missing_budget = _event(
        "no-budget",
        "link_mission_work",
        {"mission": _mission(MISSION, drawn="90", links=[_link(WORK, "90")])},
        sequence=6,
        aggregate_id=MISSION,
    )
    assert missing_budget["payload"]["mission"]["budget"] is None
    boundary = derive_mission_events(
        [under, exact, already, missing_budget],
        mission_for_work=_missions,
    )
    assert [row["source_event_id"] for row in boundary] == [exact["event_id"]]

    halved = derive_mission_events([under], mission_for_work=_missions, budget_fraction=Decimal("0.5"))
    assert len(halved) == 1
    assert halved[0]["trigger"] == "budget_usd>=0.5"
    assert derive_mission_events([under], mission_for_work=_missions) == []


def test_finding_threshold_rank() -> None:
    def finding(name: str, severity: str) -> dict[str, Any]:
        return _event(
            name,
            "record_finding",
            {
                "finding": {
                    "finding_id": str(FINDING),
                    "mission_id": str(MISSION),
                    "severity": severity,
                    "summary": FINDING_BODY,
                }
            },
            sequence=1,
            aggregate_id=FINDING,
        )

    low = finding("low", "low")
    medium = finding("medium", "medium")
    assert derive_mission_events([low], mission_for_work=_missions, finding_threshold="medium") == []
    medium_rows = derive_mission_events([medium], mission_for_work=_missions, finding_threshold="medium")
    assert len(medium_rows) == 1
    assert medium_rows[0]["trigger"] == "finding_severity>=medium"
    assert derive_mission_events([medium], mission_for_work=_missions, finding_threshold="critical") == []
    low_rows = derive_mission_events([low], mission_for_work=_missions, finding_threshold="low")
    assert low_rows[0]["trigger"] == "finding_severity>=low"


def test_attribute_event_uses_last_transition() -> None:
    event_id = _id("attr")
    event = SimpleNamespace(
        event_id=event_id,
        sequence=4,
        outcome="applied",
        event_type="set_mission_status",
        aggregate_id=MISSION,
        occurred_at=WHEN,
        payload={
            "log": LOG,
            "mission": {
                "mission_id": MISSION,
                "objective": OBJECTIVE,
                "log": LOG,
                "transitions": (
                    {"from_status": "running", "to_status": "paused"},
                    SimpleNamespace(from_status="approved", to_status="running"),
                ),
            },
        },
    )
    rows = derive_mission_events([event], mission_for_work=_missions)
    assert len(rows) == 1
    assert rows[0]["type"] == "mission.started"
    assert rows[0]["trigger"] == "status:approved->running"
    assert rows[0]["mission_id"] == str(MISSION)
    assert rows[0]["source_event_id"] == event_id
    assert rows[0]["mission_event_id"] == _expected_mission_event_id(event_id, "mission.started")
    assert not _leaks(rows[0])


def test_receipt_evidence_ref_and_log_absent() -> None:
    decision = _event(
        "decision",
        "create_decision",
        {
            "decision": {
                "decision_id": str(DECISION),
                "mission_id": str(OTHER_MISSION),
                "question": QUESTION,
                "why_it_matters": OBJECTIVE,
                "evidence_refs": ["receipt:abc"],
                "log": LOG,
            }
        },
        sequence=1,
        aggregate_id=DECISION,
    )
    rows = derive_mission_events([decision], mission_for_work=_missions)
    assert rows[0]["evidence_refs"] == [
        {"kind": "domain_event", "ref": decision["event_id"]},
        {"kind": "decision", "ref": str(DECISION)},
        {"kind": "evidence", "ref": "receipt:abc"},
    ]
    assert not _leaks(rows[0])
    assert rows[0]["mission_id"] == str(OTHER_MISSION)
