"""OMP-415: PostgreSQL integration tests for mission event derivation and read API."""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest

from omp_work.v1.models import MISSION_EVENT_TYPES
from test_mission_links_store import (
    _approve,
    _approved,
    _item,
    _link,
    _seed_budget,
    _status,
    _submit,
)
from test_workflow_service import (
    _command,
    _owner_headers,
    _p3_complete,
    _p3_payload,
    _p3_ready,
)

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def test_mission_events_lifecycle_and_read_api(service) -> None:
    # 1. Mission A (usd "10"): approved -> running
    workspace_id, project_id, mission_a_id, approved_a = _approved(
        service,
        budget_policy={
            "usd": "10",
            "tokens": 1000,
            "wall_clock_seconds": 600,
            "max_subagents": 2,
        },
    )
    _status(service, workspace_id, mission_a_id, "running")

    # 2. _p3_ready in A's workspace, seed usd "1", link, complete it
    ready = _p3_ready(service, workspace_id)
    work_id_1 = ready["item"]["work_id"]
    rev_id_1 = (
        ready["item"]["revision"]["revision_id"]
        if "revision" in ready["item"]
        else ready["item"]["revision_id"]
    )
    _seed_budget(
        service,
        workspace_id,
        work_id=work_id_1,
        revision_id=rev_id_1,
        budget={
            "usd": "1",
            "tokens": 100,
            "wall_clock_seconds": 60,
            "max_subagents": 1,
        },
    )
    status, link_body_1 = _link(service, workspace_id, mission_a_id, work_id_1)
    assert status == 200, link_body_1
    payload_1 = _p3_payload(service, workspace_id, ready)
    complete_status, complete_body = _p3_complete(service, workspace_id, payload_1)
    assert complete_status == 200, complete_body

    # 3. link a usd "9" item (drawn moves 1 -> 10, crossing 0.8 fraction of 10)
    item_9 = _item(
        service,
        workspace_id,
        "item 9",
        {
            "usd": "9",
            "tokens": 200,
            "wall_clock_seconds": 60,
            "max_subagents": 1,
        },
    )
    status, link_body_9 = _link(service, workspace_id, mission_a_id, item_9["work_id"])
    assert status == 200, link_body_9

    # 4. create_decision with mission_id str(A)
    decision_id = uuid4()
    decision_payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": str(mission_a_id),
        "question": "Approve next mission phase?",
        "why_it_matters": "Phase transition requires confirmation.",
        "risk_of_delay": "Mission execution pauses until confirmed.",
        "options": ["proceed", "abort"],
        "evidence_refs": ["evidence:alpha"],
        "default_if_any": "proceed",
        "risk_of_each_choice": {
            "proceed": "Low risk.",
            "abort": "Work stopped.",
        },
        "action_class": None,
        "resume_state": "running",
    }
    dec_status, dec_body = _command(
        service,
        workspace_id,
        {"type": "create_decision", "payload": decision_payload},
    )
    assert dec_status == 200, dec_body

    # 5. findings high and low (evidence_refs ["receipt:r1"])
    finding_high_id = uuid4()
    f_high_status, f_high_body = _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_high_id),
                "mission_id": str(mission_a_id),
                "severity": "high",
                "title": "Critical bottleneck detected",
                "evidence_refs": ["receipt:r1"],
            },
        },
    )
    assert f_high_status == 200, f_high_body

    finding_low_id = uuid4()
    f_low_status, f_low_body = _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_low_id),
                "mission_id": str(mission_a_id),
                "severity": "low",
                "title": "Minor cosmetic difference",
                "evidence_refs": ["receipt:r1"],
            },
        },
    )
    assert f_low_status == 200, f_low_body

    # 6. paused; running; blocked; running; completed
    _status(service, workspace_id, mission_a_id, "paused")
    _status(service, workspace_id, mission_a_id, "running")
    _status(service, workspace_id, mission_a_id, "blocked")
    _status(service, workspace_id, mission_a_id, "running")
    _status(service, workspace_id, mission_a_id, "completed")

    # 7. B (A's workspace): running; failed
    mission_b_id, _ = _submit(service, workspace_id, project_id)
    _approve(service, workspace_id, mission_b_id)
    _status(service, workspace_id, mission_b_id, "running")
    _status(service, workspace_id, mission_b_id, "failed")

    # 8. GET mission-events: all 8 types
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/mission-events",
        headers=_owner_headers(workspace_id),
        params={"limit": 500},
    )
    assert resp.status_code == 200
    data = resp.json()
    all_events = data["events"]

    event_types = {e["type"] for e in all_events}
    assert event_types == set(MISSION_EVENT_TYPES)
    assert len(event_types) == 8

    # triggers include "operation_completed" (refs name the work item),
    # "stage_change", "budget_usd>=0.8", "finding_severity>=high", "decision_created"
    triggers = {e["trigger"] for e in all_events}
    assert "operation_completed" in triggers
    assert "stage_change" in triggers
    assert "budget_usd>=0.8" in triggers
    assert "finding_severity>=high" in triggers
    assert "decision_created" in triggers

    op_completed_event = next(
        e for e in all_events if e["trigger"] == "operation_completed"
    )
    work_item_refs = [
        ref["ref"]
        for ref in op_completed_event["evidence_refs"]
        if ref["kind"] == "work_item"
    ]
    assert str(work_id_1) in work_item_refs

    # low finding: none
    for event in all_events:
        for ref in event["evidence_refs"]:
            assert ref["ref"] != str(finding_low_id)

    # evidence_refs start with the domain_event ref
    for event in all_events:
        assert len(event["evidence_refs"]) >= 1
        assert event["evidence_refs"][0]["kind"] == "domain_event"
        assert event["evidence_refs"][0]["ref"] == event["source_event_id"]

    # no finding title/decision question appears
    resp_text = resp.text
    assert "Critical bottleneck detected" not in resp_text
    assert "Minor cosmetic difference" not in resp_text
    assert "Approve next mission phase?" not in resp_text

    # mission_id=B: only B's
    resp_b = service.client.get(
        f"/v1/workspaces/{workspace_id}/mission-events",
        headers=_owner_headers(workspace_id),
        params={"mission_id": str(mission_b_id), "limit": 500},
    )
    assert resp_b.status_code == 200
    b_events = resp_b.json()["events"]
    assert len(b_events) > 0
    assert all(e["mission_id"] == str(mission_b_id) for e in b_events)

    # Resume: limit=2 pages from each next_after_sequence equal one unpaged read:
    # same order, no dup, no gap.
    paged_events = []
    cursor = 0
    while True:
        page_resp = service.client.get(
            f"/v1/workspaces/{workspace_id}/mission-events",
            headers=_owner_headers(workspace_id),
            params={"after_sequence": cursor, "limit": 2},
        )
        assert page_resp.status_code == 200
        page_body = page_resp.json()
        paged_events.extend(page_body["events"])
        if not page_body["has_more"]:
            break
        cursor = page_body["next_after_sequence"]

    assert paged_events == all_events


def test_record_finding_refusals(service) -> None:
    workspace_id, project_id, mission_id, _ = _approved(service)

    # Unknown mission refusal: invalid_request "mission_not_found"
    unknown_mission_id = uuid4()
    finding_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_id),
                "mission_id": str(unknown_mission_id),
                "severity": "high",
                "title": "Some finding",
                "evidence_refs": ["receipt:r1"],
            },
        },
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "mission_not_found" in body["error"]["diagnostics"]

    # Record valid finding
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_id),
                "mission_id": str(mission_id),
                "severity": "high",
                "title": "First recording",
                "evidence_refs": ["receipt:r1"],
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "record_finding"
    assert body["result"]["finding"]["finding_id"] == str(finding_id)

    # Reused finding_id refusal: revision_conflict "finding_exists"
    status_reused, body_reused = _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_id),
                "mission_id": str(mission_id),
                "severity": "critical",
                "title": "Second recording with same id",
                "evidence_refs": ["receipt:r2"],
            },
        },
    )
    assert status_reused == 409
    assert body_reused["error"]["code"] == "revision_conflict"
    assert "finding_exists" in body_reused["error"]["diagnostics"]


def test_mission_events_config_thresholds(service) -> None:
    workspace_id, project_id, mission_id, _ = _approved(
        service,
        budget_policy={
            "usd": "10",
            "tokens": 1000,
            "wall_clock_seconds": 600,
            "max_subagents": 2,
        },
    )
    _status(service, workspace_id, mission_id, "running")

    # Record medium finding
    finding_id = uuid4()
    _command(
        service,
        workspace_id,
        {
            "type": "record_finding",
            "payload": {
                "finding_id": str(finding_id),
                "mission_id": str(mission_id),
                "severity": "medium",
                "title": "Medium severity finding",
                "evidence_refs": ["receipt:med"],
            },
        },
    )

    # Default threshold is high -> medium finding is NOT included
    resp_def = service.client.get(
        f"/v1/workspaces/{workspace_id}/mission-events",
        headers=_owner_headers(workspace_id),
    )
    assert resp_def.status_code == 200
    events_def = resp_def.json()["events"]
    assert not any(e["type"] == "important_finding" for e in events_def)

    # Write mission-events.json to override thresholds
    config_file = service.config.config_dir / "mission-events.json"
    config_file.write_text(
        json.dumps(
            {
                "finding_severity_threshold": "medium",
                "budget_usd_fraction": "0.4",
            }
        )
    )

    resp_custom = service.client.get(
        f"/v1/workspaces/{workspace_id}/mission-events",
        headers=_owner_headers(workspace_id),
    )
    assert resp_custom.status_code == 200
    events_custom = resp_custom.json()["events"]
    assert any(
        e["type"] == "important_finding"
        and e["trigger"] == "finding_severity>=medium"
        for e in events_custom
    )
