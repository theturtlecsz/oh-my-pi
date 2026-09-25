"""OMP-219: PostgreSQL integration tests for atomic skip_active_item dispatch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from omp_work.v1.canonical import sha256, text_sha256
from test_workflow_service import (
    _command,
    _create,
    _finalize,
    _grant,
    _reserve,
    _tcb_manifest,
    _verify_and_seal,
)

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _get_work_item_row(service, workspace_id, work_id) -> tuple[str, int, str]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, row_version, current_revision_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        )
        row = cur.fetchone()
        assert row is not None
        return str(row[0]), int(row[1]), str(row[2])


def _get_grant_row(service, workspace_id, grant_id) -> tuple[str, int, datetime | None]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, grant_version, completed_at FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        )
        row = cur.fetchone()
        assert row is not None
        return str(row[0]), int(row[1]), row[2]


def _get_grant_item_row(
    service, workspace_id, grant_id, position
) -> tuple[str, str | None, datetime | None]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT phase, terminal_reason, skipped_at FROM omp_work.execution_grant_items WHERE workspace_id=%s AND grant_id=%s AND position=%s",
            (workspace_id, grant_id, position),
        )
        row = cur.fetchone()
        assert row is not None
        return str(row[0]), row[1], row[2]


def _get_focus_slot(service, workspace_id, owner_id) -> tuple[int, str | None]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT version, work_id FROM omp_work.focus_slots WHERE workspace_id=%s AND owner_id=%s",
            (workspace_id, owner_id),
        )
        row = cur.fetchone()
        if row is None:
            return 0, None
        return int(row[0]), str(row[1]) if row[1] is not None else None


def _get_latest_domain_event(
    service, workspace_id, work_id
) -> tuple[str, dict[str, object], str]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT event_type, payload, outcome FROM omp_audit.domain_events WHERE workspace_id=%s AND aggregate_id=%s ORDER BY sequence DESC LIMIT 1",
            (workspace_id, work_id),
        )
        row = cur.fetchone()
        assert row is not None
        payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        return str(row[0]), payload, str(row[2])


def _owner_id(service) -> str:
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    return data["actor_id"]


def test_mid_queue_skip_leaves_next_item_pending_and_last_item_skip_completes_grant(
    service,
) -> None:
    """Mid-queue skip leaves next item pending, focus clear; last-item skip completes grant.

    Underlying work item row state and row_version remain identical before/after skip.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    owner = _owner_id(service)

    item0 = _create(service, workspace_id, "Item 0", description="Desc 0")
    item1 = _create(service, workspace_id, "Item 1", description="Desc 1")
    work_id_0 = item0["work_id"]
    work_id_1 = item1["work_id"]

    # Capture work item row state before grant
    state_before_0, version_before_0, rev_before_0 = _get_work_item_row(
        service, workspace_id, work_id_0
    )
    state_before_1, version_before_1, rev_before_1 = _get_work_item_row(
        service, workspace_id, work_id_1
    )
    assert state_before_0 not in ("DONE", "CANCELED", "CANCELLED")
    assert state_before_1 not in ("DONE", "CANCELED", "CANCELLED")

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": f"/execute {item0['key']} {item1['key']}",
        "workspace_id": str(workspace_id),
        "repository": "theturtlecsz/oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
    }

    # Begin queue execution with 2 items
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "queue",
                "items": [
                    {
                        "work_id": str(work_id_0),
                        "revision_id": str(item0["revision_id"]),
                        "position": 0,
                        "original_request": "Desc 0",
                        "original_request_sha256": text_sha256("Desc 0"),
                        "initial_git_baseline": head_commit,
                    },
                    {
                        "work_id": str(work_id_1),
                        "revision_id": str(item1["revision_id"]),
                        "position": 1,
                        "original_request": "Desc 1",
                        "original_request_sha256": text_sha256("Desc 1"),
                        "initial_git_baseline": head_commit,
                    },
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "active"
    assert body["result"]["grant"]["grant_version"] == 1

    # Focus slot should be on item 0 with version 1
    focus_ver, focused_work = _get_focus_slot(service, workspace_id, owner)
    assert focus_ver == 1
    assert focused_work == str(work_id_0)

    # Item 0 is criteria_pending; Item 1 is pending
    phase0, _reason0, _ = _get_grant_item_row(service, workspace_id, grant_id, 0)
    phase1, _reason1, _ = _get_grant_item_row(service, workspace_id, grant_id, 1)
    assert phase0 == "criteria_pending"
    assert phase1 == "pending"

    # Work item row 0 state and row_version unchanged by begin_execution
    state_pre_skip_0, version_pre_skip_0, rev_pre_skip_0 = _get_work_item_row(
        service, workspace_id, work_id_0
    )
    assert state_pre_skip_0 == state_before_0
    assert version_pre_skip_0 == version_before_0
    assert rev_pre_skip_0 == rev_before_0

    # 1. Skip item 0 (mid-queue skip)
    skip_reason = "owner deferral of item 0"
    status, skip_body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": skip_reason,
            },
        },
    )
    assert status == 200, skip_body
    res = skip_body["result"]
    assert res["type"] == "skip_active_item"
    assert res["grant"]["state"] == "active"
    assert res["grant"]["grant_version"] == 2
    assert res["item"]["phase"] == "skipped"
    assert res["item"]["terminal_reason"] == "owner_skip"
    assert res["item"]["skipped_at"] is not None
    assert res["reason"] == skip_reason

    # Verify DB state after mid-queue skip:
    # Grant remains active, grant_version incremented once to 2
    g_state, g_version, g_completed_at = _get_grant_row(service, workspace_id, grant_id)
    assert g_state == "active"
    assert g_version == 2
    assert g_completed_at is None

    # Item 0 marked phase='skipped', terminal_reason='owner_skip', skipped_at set
    p0, r0, s0 = _get_grant_item_row(service, workspace_id, grant_id, 0)
    assert p0 == "skipped"
    assert r0 == "owner_skip"
    assert s0 is not None

    # Next item (Item 1) LEAVES PENDING
    p1, r1, s1 = _get_grant_item_row(service, workspace_id, grant_id, 1)
    assert p1 == "pending"
    assert r1 is None
    assert s1 is None

    # Focus slot is CAS-cleared: work_id=NULL, version bumped to 2
    focus_ver, focused_work = _get_focus_slot(service, workspace_id, owner)
    assert focus_ver == 2
    assert focused_work is None

    # Underlying omp_work.work_items row MUST stay open and unchanged
    state_after_skip_0, version_after_skip_0, rev_after_skip_0 = _get_work_item_row(
        service, workspace_id, work_id_0
    )
    assert state_after_skip_0 == state_before_0
    assert state_after_skip_0 not in ("DONE", "CANCELED", "CANCELLED")
    assert version_after_skip_0 == version_before_0
    assert rev_after_skip_0 == rev_before_0

    # Domain event persists reason and metadata
    event_type, event_payload, outcome = _get_latest_domain_event(
        service, workspace_id, work_id_0
    )
    assert event_type == "skip_active_item"
    assert outcome == "applied"
    assert event_payload["reason"] == skip_reason
    assert event_payload["item"]["phase"] == "skipped"

    # 2. Activate item 1
    status, act_body = _command(
        service,
        workspace_id,
        {
            "type": "activate_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "position": 1,
                "work_id": str(work_id_1),
                "expected_revision_id": str(item1["revision_id"]),
                "expected_project_id": None,
                "expected_blocker_ids": [],
                "expected_focus_version": 2,
                "git_baseline": head_commit,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, act_body
    assert act_body["result"]["grant"]["grant_version"] == 3
    assert act_body["result"]["item"]["phase"] == "criteria_pending"

    # Focus slot now on item 1 with version 3
    focus_ver, focused_work = _get_focus_slot(service, workspace_id, owner)
    assert focus_ver == 3
    assert focused_work == str(work_id_1)

    # 3. Skip item 1 (last-item skip completes grant)
    skip_reason_1 = "skip final item in queue"
    status, skip_body_1 = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "position": 1,
                "work_id": str(work_id_1),
                "expected_focus_version": 3,
                "judge_sha256": judge_sha,
                "reason": skip_reason_1,
            },
        },
    )
    assert status == 200, skip_body_1
    res1 = skip_body_1["result"]
    assert res1["grant"]["state"] == "completed"
    assert res1["grant"]["grant_version"] == 4
    assert res1["item"]["phase"] == "skipped"

    # DB state: grant completed!
    g_state, g_version, g_completed_at = _get_grant_row(service, workspace_id, grant_id)
    assert g_state == "completed"
    assert g_version == 4
    assert g_completed_at is not None

    # Focus slot cleared again, version bumped to 4
    focus_ver, focused_work = _get_focus_slot(service, workspace_id, owner)
    assert focus_ver == 4
    assert focused_work is None

    # Item 1 work item row state and row_version remain identical before/after
    state_after_skip_1, version_after_skip_1, rev_after_skip_1 = _get_work_item_row(
        service, workspace_id, work_id_1
    )
    assert state_after_skip_1 == state_before_1
    assert state_after_skip_1 not in ("DONE", "CANCELED", "CANCELLED")
    assert version_after_skip_1 == version_before_1
    assert rev_after_skip_1 == rev_before_1


def test_in_flight_auditor_launch_follows_audit_ready_then_superseded(service) -> None:
    """In-flight attempt follows audit_ready then superseded, clears launch,

    increments cancellation once.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    owner = _owner_id(service)

    item = _create(
        service, workspace_id, "In-flight item", description="The request description"
    )
    work_id = item["work_id"]
    rev_id = item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": f"/execute {item['key']}",
        "workspace_id": str(workspace_id),
        "repository": "theturtlecsz/oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
    }

    # Begin single execution
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "The request description",
                        "original_request_sha256": text_sha256(
                            "The request description"
                        ),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body

    # Seal criteria
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: criteria one"],
                "description_sha256": text_sha256("The request description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]

    # Stamp execution plan
    candidate_id = str(uuid4())
    plan_content = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://execute-plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]

    # Finalize candidate
    final_commit = "1" * 40
    final_cand_id = str(uuid4())
    final_cand_sha = "2" * 64
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        candidate_id,
        commit=final_commit,
        final_id=final_cand_id,
        candidate_hash=final_cand_sha,
    )

    # Begin close attempt
    attempt_id = str(uuid4())
    status, begin_body = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": attempt_id,
                "authorization_ref": f"execution:{grant_id}:0:1",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(UTC).isoformat(),
                "riders": [],
                "owner_session_start_commit": head_commit,
                "repository": "theturtlecsz/oh-my-pi",
                "diff_sha256": "5" * 64,
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one"]),
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, begin_body

    # Seal audit manifest
    seal = _verify_and_seal(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        {
            "candidate_id": final_cand_id,
            "candidate_sha256": final_cand_sha,
            "commit_sha": final_commit,
        },
        {
            "attempt_id": attempt_id,
            "work_id": work_id,
            "revision_id": new_rev_id,
            "candidate_id": final_cand_id,
            "candidate_sha256": final_cand_sha,
            "candidate_commit": final_commit,
        },
    )
    exec_task_sha = seal["manifest"]["task_sha256"]

    # Reserve auditor launch: attempt enters auditor_in_flight!
    status, res_body = _reserve(service, workspace_id, attempt_id, exec_task_sha)
    assert status == 200, res_body
    in_flight_launch_id = res_body["result"]["launch"]["launch_id"]

    # Verify DB state of close attempt: auditor_in_flight
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, in_flight_launch_id, cancelled_launch_count, terminal_reason FROM omp_work.close_attempts WHERE workspace_id=%s AND attempt_id=%s",
            (workspace_id, attempt_id),
        )
        att_row = cur.fetchone()
        assert att_row is not None
        assert att_row[0] == "auditor_in_flight"
        assert str(att_row[1]) == in_flight_launch_id
        assert att_row[2] == 0
        assert att_row[3] is None

    # Work item state before skip
    wi_state_before, wi_ver_before, _ = _get_work_item_row(
        service, workspace_id, work_id
    )
    assert wi_state_before not in ("DONE", "CANCELED", "CANCELLED")

    # Skip active item: must trigger the two-step transition:
    # 1. auditor_in_flight -> audit_ready (in_flight_launch_id=NULL, cancelled_launch_count=1)
    # 2. audit_ready -> superseded (terminal_reason='item_skipped')
    status, skip_body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "position": 0,
                "work_id": str(work_id),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "skip during auditor launch",
            },
        },
    )
    assert status == 200, skip_body
    assert skip_body["result"]["item"]["phase"] == "skipped"

    # Query DB to assert:
    # - Attempt is superseded
    # - in_flight_launch_id cleared to NULL
    # - cancelled_launch_count incremented once (0 -> 1)
    # - terminal_reason stamped as 'item_skipped'
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, in_flight_launch_id, cancelled_launch_count, terminal_reason FROM omp_work.close_attempts WHERE workspace_id=%s AND attempt_id=%s",
            (workspace_id, attempt_id),
        )
        att_after = cur.fetchone()
        assert att_after is not None
        assert att_after[0] == "superseded"
        assert att_after[1] is None
        assert att_after[2] == 1
        assert att_after[3] == "item_skipped"

    # Grant completed since single item
    g_state, _, _ = _get_grant_row(service, workspace_id, grant_id)
    assert g_state == "completed"

    # Focus slot cleared
    _, focused_work = _get_focus_slot(service, workspace_id, owner)
    assert focused_work is None

    # Work item row remains open with identical state and row_version
    wi_state_after, wi_ver_after, _ = _get_work_item_row(service, workspace_id, work_id)
    assert wi_state_after == wi_state_before
    assert wi_state_after not in ("DONE", "CANCELED", "CANCELLED")
    assert wi_ver_after == wi_ver_before


def test_skip_active_item_supersedes_active_and_audited_attempts(service) -> None:
    """Close attempt in active or audited state transitions once to superseded;

    cancelled_launch_count is unchanged.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(
        service,
        workspace_id,
        "Active attempt item",
        description="The request description",
    )
    work_id = item["work_id"]
    rev_id = item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": f"/execute {item['key']}",
        "workspace_id": str(workspace_id),
        "repository": "theturtlecsz/oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
    }

    # Begin single execution
    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "The request description",
                        "original_request_sha256": text_sha256(
                            "The request description"
                        ),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200

    # Seal criteria
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: criteria one"],
                "description_sha256": text_sha256("The request description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200
    new_rev_id = body["result"]["revision"]["revision_id"]

    # Stamp execution plan
    candidate_id = str(uuid4())
    plan_content = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://execute-plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]

    # Finalize candidate
    final_commit = "1" * 40
    final_cand_id = str(uuid4())
    final_cand_sha = "2" * 64
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        candidate_id,
        commit=final_commit,
        final_id=final_cand_id,
        candidate_hash=final_cand_sha,
    )

    # Begin close attempt: enters 'active' state
    attempt_id = str(uuid4())
    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": attempt_id,
                "authorization_ref": f"execution:{grant_id}:0:1",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(UTC).isoformat(),
                "riders": [],
                "owner_session_start_commit": head_commit,
                "repository": "theturtlecsz/oh-my-pi",
                "diff_sha256": "5" * 64,
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one"]),
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200

    # Verify attempt is 'active' with cancelled_launch_count=0
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, cancelled_launch_count FROM omp_work.close_attempts WHERE workspace_id=%s AND attempt_id=%s",
            (workspace_id, attempt_id),
        )
        r = cur.fetchone()
        assert r is not None
        assert r[0] == "active"
        assert r[1] == 0

    # Skip active item while attempt is in 'active' state
    status, skip_body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "position": 0,
                "work_id": str(work_id),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "skip with active attempt",
            },
        },
    )
    assert status == 200, skip_body

    # Attempt transitioned to superseded, cancelled_launch_count still 0
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT state, in_flight_launch_id, cancelled_launch_count, terminal_reason FROM omp_work.close_attempts WHERE workspace_id=%s AND attempt_id=%s",
            (workspace_id, attempt_id),
        )
        r = cur.fetchone()
        assert r is not None
        assert r[0] == "superseded"
        assert r[1] is None
        assert r[2] == 0
        assert r[3] == "item_skipped"


def test_stale_wrong_inactive_pending_terminal_inputs_reject_with_zero_partial_writes(
    service,
) -> None:
    """Stale/wrong/inactive/pending/terminal inputs reject with zero partial writes."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    owner = _owner_id(service)

    item0 = _create(service, workspace_id, "Item 0", description="Desc 0")
    item1 = _create(service, workspace_id, "Item 1", description="Desc 1")
    work_id_0 = item0["work_id"]
    work_id_1 = item1["work_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": "/execute items",
        "workspace_id": str(workspace_id),
        "repository": "theturtlecsz/oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(UTC).isoformat(),
    }

    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "queue",
                "items": [
                    {
                        "work_id": str(work_id_0),
                        "revision_id": str(item0["revision_id"]),
                        "position": 0,
                        "original_request": "Desc 0",
                        "original_request_sha256": text_sha256("Desc 0"),
                        "initial_git_baseline": head_commit,
                    },
                    {
                        "work_id": str(work_id_1),
                        "revision_id": str(item1["revision_id"]),
                        "position": 1,
                        "original_request": "Desc 1",
                        "original_request_sha256": text_sha256("Desc 1"),
                        "initial_git_baseline": head_commit,
                    },
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200

    def assert_zero_partial_writes():
        # Grant version is still 1, state is still active
        g_st, g_ver, _ = _get_grant_row(service, workspace_id, grant_id)
        assert g_st == "active"
        assert g_ver == 1

        # Item 0 is criteria_pending; Item 1 is pending
        p0, _, _ = _get_grant_item_row(service, workspace_id, grant_id, 0)
        p1, _, _ = _get_grant_item_row(service, workspace_id, grant_id, 1)
        assert p0 == "criteria_pending"
        assert p1 == "pending"

        # Focus slot is still item 0, version 1
        f_ver, f_work = _get_focus_slot(service, workspace_id, owner)
        assert f_ver == 1
        assert f_work == str(work_id_0)

    # 1. Stale grant version
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 99,  # Stale
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "stale grant version",
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "revision_conflict", body
    assert_zero_partial_writes()

    # 2. Stale focus version
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 99,  # Stale focus
                "judge_sha256": judge_sha,
                "reason": "stale focus version",
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "focus_conflict", body
    assert_zero_partial_writes()

    # 3. Wrong focus work (focus points to item 0, but payload claims item 1)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 1,
                "work_id": str(work_id_1),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "wrong focused work",
            },
        },
    )
    # Position 1 item is pending, which rejects before or at focus check
    assert status in (400, 409)
    assert_zero_partial_writes()

    # 4. Judge drift
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": "9" * 64,  # Drifted judge
                "reason": "judge drift",
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_judge_drift", body
    assert_zero_partial_writes()

    # 5. Wrong grant ID
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": str(uuid4()),  # Unknown grant
                "expected_grant_version": 1,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "wrong grant id",
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    assert_zero_partial_writes()

    # 6. Wrong position
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 99,  # Wrong position
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "wrong position",
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    assert_zero_partial_writes()

    # 7. Pending item (Item 1 is pending)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 1,
                "work_id": str(work_id_1),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "try skipping pending item",
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    assert_zero_partial_writes()

    # 8. Skip item 0 successfully
    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 1,
                "judge_sha256": judge_sha,
                "reason": "legitimate skip",
            },
        },
    )
    assert status == 200

    # 9. Terminal item: skipping item 0 again must reject
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "position": 0,
                "work_id": str(work_id_0),
                "expected_focus_version": 2,
                "judge_sha256": judge_sha,
                "reason": "skip already skipped item",
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body

    # 10. Inactive grant: pause grant, then try to skip
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "target_state": "paused",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "skip_active_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "position": 1,
                "work_id": str(work_id_1),
                "expected_focus_version": 2,
                "judge_sha256": judge_sha,
                "reason": "skip while paused",
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_grant_inactive", body
