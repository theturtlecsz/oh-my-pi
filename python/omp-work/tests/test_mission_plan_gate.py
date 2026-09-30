"""OMP-426-s05: mission confirmation gate for plan stamping and public mission view helpers.

Defends the contract:
1. An item linked to an approved mission stamps its execution plan.
2. After a material revise_mission transitions the mission to awaiting_confirmation,
   stamping an execution plan for that work item is refused with approval_required
   diagnostics ("mission_awaiting_confirmation",), writing no plan or candidate.
3. After the owner approves the mission (status returns to approved), stamping stamps.
4. An unlinked item in the same workspace is unaffected and stamps even while a mission
   awaits confirmation.
5. Public view helpers (new_mission_view, revised_mission_view, approved_mission_view,
   status_mission_view, latest_mission, mission_for_work) return expected MissionView state.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.v1.canonical import sha256
from omp_work.v1.missions import (
    approved_mission_view,
    latest_mission,
    mission_for_work,
    new_mission_view,
    revised_mission_view,
    status_mission_view,
)
from omp_work.v1.models import MissionDraft, MissionStatus
from test_workflow_service import (
    OWNER,
    _command,
    _create,
    _grant,
    _owner_headers,
    _tcb_manifest,
    text_sha256,
)

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = {
    "usd": "50",
    "tokens": 5000,
    "wall_clock_seconds": 1800,
    "max_subagents": 4,
}
_ITEM_BUDGET = {
    "usd": "10",
    "tokens": 1000,
    "wall_clock_seconds": 300,
    "max_subagents": 2,
}


def _project(service, provenance: dict | None = None) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Mission project",
                json.dumps(provenance or {}),
            ),
        )
    return workspace_id, project_id


def _draft(project_id: UUID, **overrides) -> dict:
    draft = {
        "project_id": str(project_id),
        "objective": "Mission objective for plan gate testing",
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    draft.update(overrides)
    return draft


def _submit(service, workspace_id: UUID, project_id: UUID, **overrides) -> tuple[UUID, dict]:
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, **overrides),
            },
        },
    )
    assert status == 200, body
    return mission_id, body["result"]["mission"]


def _approve(service, workspace_id: UUID, mission_id: UUID, revision: int = 1) -> dict:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": revision,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def _approved(service, provenance: dict | None = None, **overrides) -> tuple[UUID, UUID, UUID, dict]:
    workspace_id, project_id = _project(service, provenance)
    mission_id, _submitted = _submit(service, workspace_id, project_id, **overrides)
    approved = _approve(service, workspace_id, mission_id)
    assert approved["status"] == "approved"
    return workspace_id, project_id, mission_id, approved


def _revise(service, workspace_id: UUID, mission_id: UUID, base_revision: int, draft: dict):
    return _command(
        service,
        workspace_id,
        {
            "type": "revise_mission",
            "payload": {
                "mission_id": str(mission_id),
                "base_revision": base_revision,
                "draft": draft,
            },
        },
    )


def _seed_budget(service, workspace_id: UUID, *, work_id: str, revision_id: str, budget: dict) -> None:
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(OWNER),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _item(service, workspace_id: UUID, title: str, budget: dict | None) -> dict:
    item = _create(service, workspace_id, title, description="The request description")
    if budget is not None:
        _seed_budget(
            service,
            workspace_id,
            work_id=item["work_id"],
            revision_id=item["revision_id"],
            budget=budget,
        )
    return item


def _link(service, workspace_id: UUID, mission_id: UUID, work_id: str, *, operation_id=None):
    return _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
        operation_id=operation_id,
    )


def _setup_execution_grant(
    service, workspace_id: UUID, item: dict
) -> tuple[str, str, str]:
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
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
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
                        "work_id": str(item["work_id"]),
                        "revision_id": str(item["revision_id"]),
                        "position": 0,
                        "original_request": "The request description",
                        "original_request_sha256": text_sha256("The request description"),
                        "initial_git_baseline": head_commit,
                        "active_blocker_ids": [],
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(item["work_id"]),
                "expected_revision_id": str(item["revision_id"]),
                "criteria": ["AC-1: criteria one"],
                "description_sha256": text_sha256("The request description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]
    return grant_id, new_rev_id, judge_sha


def _stamp(
    service,
    workspace_id: UUID,
    grant_id: str,
    work_id: str,
    revision_id: str,
    judge_sha: str,
    candidate_id: str,
    expected_grant_version: int = 2,
):
    plan_content = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    return _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": expected_grant_version,
                "work_id": str(work_id),
                "revision_id": str(revision_id),
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


def test_item_linked_to_approved_mission_stamps(service) -> None:
    workspace_id, _project_id, mission_id, _approved_m = _approved(service)

    item = _item(service, workspace_id, "item 1", _ITEM_BUDGET)
    status, body = _link(service, workspace_id, mission_id, item["work_id"])
    assert status == 200, body

    grant_id, rev_id, judge_sha = _setup_execution_grant(service, workspace_id, item)
    cand_id = str(uuid4())
    status, body = _stamp(service, workspace_id, grant_id, item["work_id"], rev_id, judge_sha, cand_id)
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
    assert body["result"]["item"]["plan_stamp_sha256"]

    # Verify plan and candidate recorded in DB
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_stamp FROM omp_work.execution_grant_items WHERE grant_id=%s AND work_id=%s",
            (grant_id, item["work_id"]),
        )
        row = cur.fetchone()
        assert row is not None and row[0] is not None

        cur.execute(
            "SELECT count(*) FROM omp_work.candidates WHERE candidate_id=%s AND kind='planned'",
            (cand_id,),
        )
        assert cur.fetchone()[0] == 1


def test_material_revision_blocks_stamping_until_approved(service) -> None:
    workspace_id, project_id, mission_id, _approved_m = _approved(service)

    item = _item(service, workspace_id, "item 2", _ITEM_BUDGET)
    status, body = _link(service, workspace_id, mission_id, item["work_id"])
    assert status == 200, body

    grant_id, rev_id, judge_sha = _setup_execution_grant(service, workspace_id, item)

    # Material revision (adds a new repository) transitions mission to awaiting_confirmation
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, repositories=["repo-1", "repo-2"]),
    )
    assert status == 200, body
    assert body["result"]["mission"]["status"] == "awaiting_confirmation"

    # Stamping item is refused with approval_required and mission_awaiting_confirmation
    cand_id = str(uuid4())
    status, body = _stamp(service, workspace_id, grant_id, item["work_id"], rev_id, judge_sha, cand_id)
    assert status == 409, body
    assert body["error"]["code"] == "approval_required"
    assert body["error"]["diagnostics"] == ["mission_awaiting_confirmation"]

    # Assert no plan or candidate was recorded in DB
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_stamp FROM omp_work.execution_grant_items WHERE grant_id=%s AND work_id=%s",
            (grant_id, item["work_id"]),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None, f"expected plan_stamp to be None, got {row[0]}"

        cur.execute(
            "SELECT count(*) FROM omp_work.candidates WHERE candidate_id=%s",
            (cand_id,),
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT grant_version FROM omp_work.execution_grants WHERE grant_id=%s",
            (grant_id,),
        )
        assert cur.fetchone()[0] == 2

    # Owner approves the mission (status returns to approved)
    approved_rev2 = _approve(service, workspace_id, mission_id, revision=2)
    assert approved_rev2["status"] == "approved"

    # Stamping now succeeds and records the plan
    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        cand_id,
        expected_grant_version=2,
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
    assert body["result"]["item"]["plan_stamp_sha256"]

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_stamp FROM omp_work.execution_grant_items WHERE grant_id=%s AND work_id=%s",
            (grant_id, item["work_id"]),
        )
        row = cur.fetchone()
        assert row is not None and row[0] is not None

        cur.execute(
            "SELECT count(*) FROM omp_work.candidates WHERE candidate_id=%s AND kind='planned'",
            (cand_id,),
        )
        assert cur.fetchone()[0] == 1


def test_unlinked_item_is_unaffected(service) -> None:
    workspace_id, project_id, mission_id, _approved_m = _approved(service)

    # Put the mission in awaiting_confirmation
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, repositories=["repo-unlinked-check"]),
    )
    assert status == 200, body
    assert body["result"]["mission"]["status"] == "awaiting_confirmation"

    # Unlinked item created in the same workspace
    unlinked_item = _create(service, workspace_id, "unlinked item", description="The request description")
    grant_id, rev_id, judge_sha = _setup_execution_grant(service, workspace_id, unlinked_item)
    cand_id = str(uuid4())

    # Stamping unlinked item succeeds despite the mission awaiting confirmation
    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        unlinked_item["work_id"],
        rev_id,
        judge_sha,
        cand_id,
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
    assert body["result"]["item"]["plan_stamp_sha256"]


def test_public_mission_view_helpers(service) -> None:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    draft = MissionDraft.model_validate(_draft(project_id))
    actor_id = uuid4()

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        # new_mission_view helper
        view1 = new_mission_view(cur, workspace_id, mission_id, draft, actor_id, "agent")
        assert view1.mission_id == mission_id
        assert view1.revision == 1
        assert view1.status == MissionStatus.AWAITING_CONFIRMATION

        # approved_mission_view helper
        approved = approved_mission_view(cur, view1, "decision", str(uuid4()), actor_id, "owner")
        assert approved.status == MissionStatus.APPROVED
        assert approved.approved_scope is not None

        # revised_mission_view helper
        revised_draft = MissionDraft.model_validate(_draft(project_id, repositories=["repo-x"]))
        revised = revised_mission_view(cur, workspace_id, approved, revised_draft, actor_id, "agent")
        assert revised.revision == 2
        assert revised.status == MissionStatus.AWAITING_CONFIRMATION

        # status_mission_view helper
        status_view = status_mission_view(
            cur, revised, MissionStatus.PAUSED, "policy_rule", "rule-test", actor_id, "system"
        )
        assert status_view.status == MissionStatus.PAUSED
        assert status_view.transitions[-1].to_status == MissionStatus.PAUSED

        # latest_mission and mission_for_work helpers with no applied events yet
        assert latest_mission(cur, workspace_id, mission_id) is None
        assert mission_for_work(cur, workspace_id, uuid4()) is None
