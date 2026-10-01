"""OMP-423-s01: PostgreSQL integration tests for mission standing view in WorkStore."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.lease import settle_job
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.v1.canonical import sha256
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from test_workflow_service import OWNER, _command, _create

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _seed_budget(service, workspace_id: UUID, *, work_id: UUID | str, revision_id: UUID | str, budget: dict) -> None:
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


def _create_item(service, workspace_id: UUID, title: str, budget: dict | None) -> dict:
    item = _create(service, workspace_id, title)
    if budget is not None:
        _seed_budget(
            service,
            workspace_id,
            work_id=item["work_id"],
            revision_id=item["revision_id"],
            budget=budget,
        )
    return item


def _submit_and_approve_mission(service, workspace_id: UUID, project_id: UUID, budget_policy: dict) -> UUID:
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": {
                    "project_id": str(project_id),
                    "objective": "Mission standing integration test",
                    "risk_policy": "risk-parent",
                    "approval_policy": "approval-parent",
                    "effort_policy": "effort-parent",
                    "budget_policy": budget_policy,
                },
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return mission_id


def _link(service, workspace_id: UUID, mission_id: UUID, work_id: str | UUID):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
    )
    assert status == 200, body
    return body


def _create_decision(service, workspace_id: UUID, project_id: UUID, mission_id: str | None) -> UUID:
    decision_id = uuid4()
    payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": mission_id,
        "question": f"Ruling for decision {decision_id}?",
        "why_it_matters": "The mission cannot continue without a ruling.",
        "risk_of_delay": "Stalls execution.",
        "options": ["publish", "hold"],
        "evidence_refs": ["receipt:test"],
        "default_if_any": "hold",
        "risk_of_each_choice": {
            "publish": "External impact.",
            "hold": "Delay.",
        },
        "action_class": None,
        "resume_state": json.dumps({"step": 1}),
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "create_decision", "payload": payload},
    )
    assert status == 200, body
    return decision_id


def _answer_decision(service, workspace_id: UUID, decision_id: UUID, answer: str = "publish"):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {"decision_id": str(decision_id), "answer": answer},
        },
    )
    assert status == 200, body
    return body


def _worker(native_jobs, job_store: NativeJobStore, capabilities: list[str]) -> str:
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=capabilities,
        capacity=1,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def test_mission_standing_store(native_jobs) -> None:
    service = native_jobs.service
    workspace_id = native_jobs.workspace_id
    actor_id = native_jobs.actor_id
    store = PostgresWorkStore(service.config)
    job_store = NativeJobStore(service.config)

    project_id = store.ensure_project(
        workspace_id, actor_id, f"p-{uuid4().hex[:8]}", "Mission Project", "surface"
    )
    mission_id = _submit_and_approve_mission(
        service,
        workspace_id,
        project_id,
        {
            "usd": "100",
            "tokens": 10000,
            "wall_clock_seconds": 600,
            "max_subagents": 2,
        },
    )
    other_mission_id = _submit_and_approve_mission(
        service,
        workspace_id,
        project_id,
        {
            "usd": "50",
            "tokens": 5000,
            "wall_clock_seconds": 300,
            "max_subagents": 2,
        },
    )

    linked_item = _create_item(
        service,
        workspace_id,
        "linked item",
        {
            "usd": "10",
            "tokens": 1000,
            "wall_clock_seconds": 60,
            "max_subagents": 1,
        },
    )
    _link(service, workspace_id, mission_id, linked_item["work_id"])
    unlinked_item = _create_item(service, workspace_id, "unlinked item", None)

    # 1. Decisions:
    # A pending decision naming the mission is listed; answered, it is gone; one on another mission never shows.
    dec_m = _create_decision(service, workspace_id, project_id, str(mission_id))
    dec_other = _create_decision(service, workspace_id, project_id, str(other_mission_id))

    standing = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=True)
    pending_ids = [d["decision_id"] for d in standing["pending_decisions"]]
    assert str(dec_m) in pending_ids
    assert str(dec_other) not in pending_ids

    _answer_decision(service, workspace_id, dec_m)
    standing_after_answer = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=True)
    pending_ids_after = [d["decision_id"] for d in standing_after_answer["pending_decisions"]]
    assert str(dec_m) not in pending_ids_after

    # 2. Jobs:
    # A job on a linked item is listed as backlog, then admitted after claim_job, and gone after settle_job; a job on an unlinked item never shows.
    worker_id = _worker(native_jobs, job_store, ["compute.cpu"])
    job_id = f"job-linked-{uuid4()}"
    enqueue_res = enqueue_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=job_id,
        work_id=UUID(linked_item["work_id"]),
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
    assert enqueue_res["status"] == "applied"

    unlinked_job_id = f"job-unlinked-{uuid4()}"
    enqueue_unlinked_res = enqueue_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=unlinked_job_id,
        work_id=UUID(unlinked_item["work_id"]),
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
    assert enqueue_unlinked_res["status"] == "applied"

    # Listed as backlog; unlinked job never shows
    standing = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=True)
    jobs = standing["jobs_in_flight"]
    linked_jobs = [j for j in jobs if j["job_id"] == job_id]
    assert len(linked_jobs) == 1
    assert linked_jobs[0]["status"] == "backlog"
    assert linked_jobs[0]["work_id"] == linked_item["work_id"]
    assert not any(j["job_id"] == unlinked_job_id for j in jobs)

    # Claim job -> admitted
    claim_res = claim_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
        worker_id=worker_id,
    )
    assert claim_res["status"] == "applied"
    assert claim_res["job"]["job_id"] == job_id
    assert claim_res["job"]["status"] == "admitted"
    fence = claim_res["job"]["fence"]

    standing = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=True)
    jobs = standing["jobs_in_flight"]
    linked_jobs = [j for j in jobs if j["job_id"] == job_id]
    assert len(linked_jobs) == 1
    assert linked_jobs[0]["status"] == "admitted"

    # Settle job -> gone
    settle_res = settle_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
        outcome="succeeded",
        receipts=[{
            "role": "audit",
            "issuer_component_sha256": native_jobs.components["audit"],
            "evidence": {
                "id": f"ev-{uuid4()}",
                "kind": "receipt",
                "content_digest": "abcd1234ef",
                "trust": "verified",
            },
        }],
    )
    assert settle_res["status"] == "applied"

    standing = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=True)
    assert not any(j["job_id"] == job_id for j in standing["jobs_in_flight"])

    # 3. standing=False returns the view without either key.
    plain_view = store.read(workspace_id, actor_id, "mission", str(mission_id), standing=False)
    assert "pending_decisions" not in plain_view
    assert "jobs_in_flight" not in plain_view

    default_view = store.read(workspace_id, actor_id, "mission", str(mission_id))
    assert "pending_decisions" not in default_view
    assert "jobs_in_flight" not in default_view
    assert plain_view == default_view

    # 4. Without links -> jobs_in_flight is []
    standing_no_links = store.read(workspace_id, actor_id, "mission", str(other_mission_id), standing=True)
    assert standing_no_links["jobs_in_flight"] == []

    # 5. Invalid mission -> invalid_request
    with pytest.raises(WorkStoreError) as exc_info:
        store.read(workspace_id, actor_id, "mission", str(uuid4()), standing=True)
    assert exc_info.value.code == "invalid_request"
