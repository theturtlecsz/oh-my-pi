"""OMP-404-s08: End-to-end proof of item budget lifecycle on PostgreSQL.

Verifies the complete item budget lifecycle across the ledger and jobs substrates:
1. A mission without budget_policy is held with an OMP-414 decision record,
   mission_intake_draft returns None, and the workspace has no new item or job.
2. Publishing a budget-less draft is refused 409 intake_not_ready, creating no item.
3. A mission with budget_policy {usd "1.00", tokens 1000, wall_clock_seconds 3600, max_subagents 4}
   publishes; the receipt carries that budget.
4. Parent job plus one child job (parent_job_id) for the item; record usage (price_usd 0)
   on the child only: 500, 300, 300 tokens. The outbox has tokens alerts at 50, 80, and 100;
   parent and child are cancelled; a new root job enqueue for the item raises budget_exceeded.
5. deliver_budget_alerts sends all pending alerts (the three tokens ones included)
   to a loopback http.server stub (shut down in teardown), once each; a second call sends 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

# Ensure sibling test helpers are importable regardless of test invocation cwd/pythonpath
_TESTS_DIR = str(Path(__file__).parent.resolve())
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from test_bounded_intake_publish import (
    OWNER,
    OWNER_SCOPES,
    _draft,
    _envelope,
    _seed_omp249,
)
from test_budget_alerts_grokbot import _url, alert_server  # noqa: F401 (fixture)

from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.grokbot import deliver_budget_alerts
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.mission_budget import admit_mission_budget, mission_intake_draft
from omp_work.v1.models import ItemBudget
from omp_work.v1.semantics import (
    BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
    bounded_intake_semantic_sha256,
)
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore


def test_item_budget_e2e(native_jobs, alert_server) -> None:
    config = native_jobs.service.config
    workspace_id = uuid4()
    job_store = NativeJobStore(config)
    work_store = PostgresWorkStore(config)
    work_service = WorkService(work_store)

    owner = Principal(
        actor_id=OWNER,
        actor_kind="owner",
        workspaces=frozenset({workspace_id}),
        scopes=OWNER_SCOPES,
    )

    def execute(
        command: dict[str, object],
        *,
        operation_id: UUID | None = None,
    ) -> tuple[object, dict[str, object]]:
        return work_service.execute(
            owner,
            _envelope(workspace_id, command, operation_id=operation_id),
        )

    # 1. A mission without budget_policy: mission_budget.admit_mission_budget holds it with
    # a decision record, mission_intake_draft returns None, and the workspace has no new item or job.
    mission_no_budget = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
        "objective": "Mission without budget policy",
    }
    admission_held = admit_mission_budget(mission_no_budget)
    assert admission_held.state == "held"
    assert admission_held.budget is None
    assert admission_held.decision is not None
    assert "decision_id" in admission_held.decision
    assert admission_held.decision["mission_id"] == mission_no_budget["mission_id"]
    assert "risk_of_delay" in admission_held.decision

    base_draft = _draft()
    held_draft = mission_intake_draft(admission_held, base_draft)
    assert held_draft is None

    with job_store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT count(*) AS count FROM omp_work.work_items WHERE workspace_id=%s",
            (workspace_id,),
        )
        assert cur.fetchone()["count"] == 0
        cur.execute(
            "SELECT count(*) AS count FROM omp_jobs.jobs WHERE workspace_id=%s",
            (workspace_id,),
        )
        assert cur.fetchone()["count"] == 0

    # 2. Publishing a budget-less draft: 409 intake_not_ready, no item.
    draft_no_budget = base_draft.model_copy(update={"budget": None})
    seeded = _seed_omp249(config, workspace_id, draft_no_budget)

    assessment_op_1 = uuid4()
    _, assessment_1 = execute(
        {
            "type": "assess_bounded_intake",
            "payload": {"draft": draft_no_budget.model_dump(mode="json")},
        },
        operation_id=assessment_op_1,
    )
    assert assessment_1["ready_for_ratification"] is True

    _, admission_1 = execute(
        {"type": "attest_intake_admission", "payload": seeded.attest_payload()}
    )
    admission_receipt_1 = admission_1["receipt"]["receipt_id"]

    publish_cmd_no_budget = {
        "type": "publish_bounded_intake",
        "payload": {
            "draft": draft_no_budget.model_dump(mode="json"),
            "assessment_operation_id": str(assessment_op_1),
            "ratified_semantic_sha256": assessment_1["semantic_sha256"],
            "admission_work_id": str(seeded.work_id),
            "admission_revision_id": str(seeded.revision_id),
            "admission_receipt_id": admission_receipt_1,
        },
    }

    with pytest.raises(WorkError) as exc_info:
        execute(publish_cmd_no_budget)

    assert exc_info.value.status == 409
    assert exc_info.value.code == "intake_not_ready"
    assert exc_info.value.diagnostics == ("budget:missing",)

    # Ensure no new work item was created (only OMP-249 seeded exists)
    with job_store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT count(*) AS count FROM omp_work.work_items WHERE workspace_id=%s AND work_id != %s",
            (workspace_id, seeded.work_id),
        )
        assert cur.fetchone()["count"] == 0

    # 3. A mission with budget_policy {usd "1.00", tokens 1000, wall_clock_seconds 3600, max_subagents 4}:
    # its draft publishes; the receipt carries that budget.
    budget_policy = {
        "usd": "1.00",
        "tokens": 1000,
        "wall_clock_seconds": 3600,
        "max_subagents": 4,
    }
    mission_with_budget = {
        "mission_id": f"mission-{uuid4().hex[:8]}",
        "project_id": f"proj-{uuid4().hex[:8]}",
        "budget_policy": budget_policy,
    }
    admission_admitted = admit_mission_budget(mission_with_budget)
    assert admission_admitted.state == "admitted"
    expected_budget = ItemBudget.model_validate(budget_policy)
    assert admission_admitted.budget == expected_budget

    draft_with_budget = mission_intake_draft(admission_admitted, base_draft)
    assert draft_with_budget is not None
    assert draft_with_budget.budget == expected_budget

    # Record fresh Fable advice and native admission for draft_with_budget
    _, advice_res = execute(
        {
            "type": "record_fable_advice",
            "payload": {
                "work_id": str(seeded.work_id),
                "revision_id": str(seeded.revision_id),
                "advice_sha256": "4" * 64,
                "disposition": "considered",
                "intake_semantic_sha256": bounded_intake_semantic_sha256(draft_with_budget),
                "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
            },
        },
        operation_id=uuid4(),
    )
    advice_receipt_2 = advice_res["receipt"]["receipt_id"]

    attest_payload_2 = dict(seeded.attest_payload())
    attest_payload_2["fable_advice_receipt_id"] = str(advice_receipt_2)
    _, admission_2 = execute(
        {"type": "attest_intake_admission", "payload": attest_payload_2}
    )
    admission_receipt_2 = admission_2["receipt"]["receipt_id"]

    assessment_op_2 = uuid4()
    _, assessment_2 = execute(
        {
            "type": "assess_bounded_intake",
            "payload": {"draft": draft_with_budget.model_dump(mode="json")},
        },
        operation_id=assessment_op_2,
    )
    assert assessment_2["ready_for_ratification"] is True

    publish_cmd_with_budget = {
        "type": "publish_bounded_intake",
        "payload": {
            "draft": draft_with_budget.model_dump(mode="json"),
            "assessment_operation_id": str(assessment_op_2),
            "ratified_semantic_sha256": assessment_2["semantic_sha256"],
            "admission_work_id": str(seeded.work_id),
            "admission_revision_id": str(seeded.revision_id),
            "admission_receipt_id": admission_receipt_2,
        },
    }

    receipt, publish_result = execute(publish_cmd_with_budget)
    assert receipt.state.value == "applied"
    assert publish_result["type"] == "publish_bounded_intake"
    published_item = publish_result["item"]
    published_work_id = UUID(published_item["work_id"])
    assert (
        publish_result["receipt"]["payload"]["draft"]["budget"]
        == expected_budget.model_dump(mode="json")
    )

    # 4. Parent job plus one child job (parent_job_id) for the item; record usage (price_usd 0)
    # on the child only: 500, 300, 300 tokens. The outbox has tokens alerts at 50, 80 and 100;
    # parent and child are cancelled; a new root job enqueue for the item raises budget_exceeded.
    parent_job_id = f"job-parent-{uuid4().hex[:8]}"
    enqueue_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        job_id=parent_job_id,
        work_id=published_work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
    )

    child_job_id = f"job-child-{uuid4().hex[:8]}"
    enqueue_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        job_id=child_job_id,
        work_id=published_work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
        parent_job_id=parent_job_id,
    )

    # 500 tokens (reaches 50%)
    record_usage(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=published_work_id,
        job_id=child_job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=500,
        output_tokens=0,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.00",
    )

    # 300 tokens (total 800, reaches 80%)
    record_usage(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=published_work_id,
        job_id=child_job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=300,
        output_tokens=0,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.00",
    )

    # 300 tokens (total 1100, reaches 100% exceeded)
    record_usage(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=published_work_id,
        job_id=child_job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=300,
        output_tokens=0,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.00",
    )

    # Outbox has tokens alerts at 50, 80 and 100
    with job_store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            """
            SELECT payload FROM omp_jobs.outbox
            WHERE workspace_id=%s AND kind='budget_alert'
            ORDER BY event_id
            """,
            (workspace_id,),
        )
        alerts = [row["payload"] for row in cur.fetchall()]

    token_thresholds = {
        a["threshold_percent"] for a in alerts if a.get("dimension") == "tokens"
    }
    assert {50, 80, 100}.issubset(token_thresholds)

    # Parent and child are cancelled
    with job_store.transaction(workspace_id, OWNER) as cur:
        cur.execute(
            "SELECT job_id, status, cancel_reason FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id = ANY(%s)",
            (workspace_id, [parent_job_id, child_job_id]),
        )
        jobs = {r["job_id"]: r for r in cur.fetchall()}
    assert jobs[parent_job_id]["status"] == "cancelled"
    assert jobs[parent_job_id]["cancel_reason"] == "budget_exceeded"
    assert jobs[child_job_id]["status"] == "cancelled"
    assert jobs[child_job_id]["cancel_reason"] == "budget_exceeded"

    # A new root job enqueue for the item raises budget_exceeded
    with pytest.raises(JobError) as exc_info:
        enqueue_job(
            job_store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
            job_id=f"job-new-root-{uuid4().hex[:8]}",
            work_id=published_work_id,
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=30,
        )
    assert exc_info.value.code == "budget_exceeded"

    # 5. jobs.grokbot.deliver_budget_alerts sends all pending alerts (the three tokens ones included)
    # to a loopback http.server stub (shut down in teardown), once each; a second call sends 0.
    sent_count = deliver_budget_alerts(
        job_store,
        workspace_id=workspace_id,
        actor_id=OWNER,
        url=_url(alert_server),
        token="grokbot-test-token",
    )
    assert sent_count >= 3
    received_token_thresholds = {
        req["body"]["threshold_percent"]
        for req in alert_server.requests
        if req["body"].get("dimension") == "tokens"
    }
    assert {50, 80, 100}.issubset(received_token_thresholds)

    second_sent = deliver_budget_alerts(
        job_store,
        workspace_id=workspace_id,
        actor_id=OWNER,
        url=_url(alert_server),
        token="grokbot-test-token",
    )
    assert second_sent == 0
