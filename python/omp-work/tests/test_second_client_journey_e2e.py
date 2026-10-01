"""OMP-424-s02: the stdlib second client walks one mission journey over HTTP.

One test drives the whole journey through the second client: projects and
context read the seeded project, submit leaves exactly one pending decision,
confirm applies it, the platform starts the mission and raises a fresh
decision, the client answers it, the platform records an important finding and
completes the mission, and the client reads running, then completed, then the
mission events. Every assertion below reads what the client printed, except that
the ``important_finding`` event must cite the evidence ref the platform
recorded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from second_client_world import world

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _bodies(journey, step: str) -> list[dict[str, object]]:
    return [body for name, body, _state in journey if name == step]


def test_second_client_journey(tmp_path: Path) -> None:
    with world(tmp_path) as w:
        capability = w.mint("journey")
        w.designate(capability.actor_id)
        journey = w.run_journey(capability)

        # 1. projects lists the seeded project, and context returns it.
        projects = _bodies(journey, "projects")[-1]
        assert projects["outcome"] == "read"
        listed = [
            project
            for project in projects["result"]["projects"]
            if str(project["project_id"]) == str(w.project_id)
        ]
        assert len(listed) == 1, projects
        context = _bodies(journey, "context")[-1]
        assert context["outcome"] == "read"
        # A missing project would be invalid_request; the context read proves it.
        assert set(context["result"]) == {"refs", "missions", "history", "research"}

        # 2. submit leaves exactly one pending decision.
        submitted = _bodies(journey, "submit")[-1]
        assert submitted["outcome"] == "applied"
        drafted = submitted["result"]
        assert drafted["outcome"] == "awaiting_owner"
        submit_decision_id = str(drafted["decision_id"])
        assert drafted["mission"]["status"] == "awaiting_confirmation"

        # 3. confirm is applied and the mission reads approved; the answered
        # decision no longer sits pending while the raised one does.
        confirm = _bodies(journey, "confirm")[-1]
        assert confirm["outcome"] == "applied"
        assert confirm["result"]["intent"] == "confirm_scope"
        assert confirm["result"]["mission"]["status"] == "approved"
        decisions = _bodies(journey, "decisions")[-1]
        by_id = {
            str(decision["decision_id"]): decision
            for decision in decisions["result"]["decisions"]
        }
        assert by_id[submit_decision_id]["status"] == "answered"
        pending = [
            decision_id
            for decision_id, decision in by_id.items()
            if decision["status"] == "pending"
        ]
        assert len(pending) == 1, decisions

        # 4. the raised decision is answered, and status then reads running.
        answer = _bodies(journey, "answer")[-1]
        assert answer["outcome"] == "applied"
        assert answer["result"]["intent"] == "answer_decision"
        assert answer["result"]["answer"] == "proceed"
        first_status = _bodies(journey, "status")[0]
        assert first_status["state"] == "running"

        # 5. completion follows the answered decision; status then reads completed.
        completion = _bodies(journey, "completion")[-1]
        assert completion["mission"]["status"] == "completed"
        second_status = _bodies(journey, "status")[1]
        assert second_status["state"] == "completed"

        # 6. events carry the journey's four transitions.
        events = _bodies(journey, "events")[-1]
        rows = events["events"]
        assert sorted(row["type"] for row in rows) == [
            "decision.required",
            "important_finding",
            "mission.completed",
            "mission.started",
        ]
        assert all(row["trigger"] for row in rows)

        # 7. the important finding cites the evidence ref the platform recorded.
        recorded = _bodies(journey, "record_finding")[-1]
        finding_ref = recorded["finding"]["evidence_refs"][0]
        finding_event = next(row for row in rows if row["type"] == "important_finding")
        assert finding_ref in [ref["ref"] for ref in finding_event["evidence_refs"]]
