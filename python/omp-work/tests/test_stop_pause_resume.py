"""OMP-430-s06: a stop pauses native work without changing it; release resumes it.

PostgreSQL integration (``OMP_WORK_POSTGRES_INTEGRATION=1``). Uses the
``native_jobs`` fixture and the native-jobs helpers of
``test_jobs_worker_loop.py``, and the ``derive_mission_events`` projection
covered by ``test_mission_events_derive.py``.

The contract: a client principal engages the stop while a job holds a one
second lease. Renewal is refused with ``agent_stop_engaged``; after the lease
expires ``reconcile_jobs`` returns the job to the backlog (one fence, reason
``lease_expired``, never failed or cancelled); ``claim_job`` leases nothing
while stopped; the mission's status and revision are unchanged and the stop
derives no mission event. The owner's ``release_stop`` lets ``JobWorker.tick``
claim it once more and resume from saved state through ``observe`` — not
``run`` — then settle.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.jobs.admission import claim_job
from omp_work.jobs.lease import reconcile_jobs, renew_lease
from omp_work.jobs.store import JobError
from omp_work.jobs.worker import JobWorker, Settlement, WorkerConfig
from omp_work.mission_events import derive_mission_events
from omp_work.v1.canonical import sha256
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.store import PostgresWorkStore
from test_jobs_worker_loop import (
    _connect,
    _connect_admin,
    _enqueue,
    _receipt,
    _resources,
    _store,
    _work_id,
)
from test_workflow_service import OWNER, _command, _owner_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")

_BUDGET = {
    "usd": "10",
    "tokens": 1000,
    "wall_clock_seconds": 600,
    "max_subagents": 2,
}
_ITEM_BUDGET = {
    "usd": "6",
    "tokens": 600,
    "wall_clock_seconds": 120,
    "max_subagents": 2,
}


class _RecordingHandler:
    """Settles on observe; run is the failure the saved-state resume must avoid."""

    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt
        self.observe_count = 0
        self.run_count = 0

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        self.observe_count += 1
        return Settlement(outcome="succeeded", receipts=[self.receipt])

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        self.run_count += 1
        return Settlement(outcome="succeeded", receipts=[self.receipt])


def _project(service, workspace_id: UUID, project_id: UUID) -> None:
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
                "Stop pause project",
                json.dumps({}),
            ),
        )


def _seed_budget(native_jobs, workspace_id: UUID, *, work_id: str, revision_id: str) -> None:
    """Seed the intake_publication receipt link_mission_work draws the item budget from."""
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": _ITEM_BUDGET},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(OWNER),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (candidate_id, workspace_id, work_id, revision_id, "e" * 64, datetime.now(UTC)),
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


def _stop_envelope(workspace_id: UUID, command_type: str, reason: str) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": command_type, "payload": {"reason": reason}},
        }
    )


def _engage(service, workspace_id: UUID, client_actor_id: UUID) -> None:
    receipt, _ = PostgresWorkStore(service.config).execute(
        _stop_envelope(workspace_id, "engage_stop", "runaway loop"),
        actor_id=client_actor_id,
        actor_kind="grokbot",
        required_scope="work.stop",
    )
    assert receipt.state == OperationState.APPLIED


def _release(service, workspace_id: UUID) -> None:
    receipt, _ = PostgresWorkStore(service.config).execute(
        _stop_envelope(workspace_id, "release_stop", "operator resumed work"),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert receipt.state == OperationState.APPLIED


def _stopped(service, workspace_id: UUID) -> bool:
    return bool(PostgresWorkStore(service.config).stop_status(workspace_id, OWNER)["stopped"])


def _job_row(native_jobs, job_id: str) -> dict[str, Any]:
    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT status, fence, worker_id, lease_expires_at, settlement"
            " FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


def _job_event_kinds(native_jobs, job_id: str) -> list[str]:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq",
            (job_id,),
        ).fetchall()
    return [str(row["kind"]) for row in rows]


def _reservation_reason(native_jobs, job_id: str) -> str | None:
    with _connect(native_jobs) as conn:
        rows = conn.execute(
            "SELECT release_reason FROM omp_jobs.reservations WHERE job_id=%s"
            " ORDER BY fence DESC",
            (job_id,),
        ).fetchall()
    assert rows
    return rows[0]["release_reason"]


def _derive(native_jobs, workspace_id: UUID) -> list[dict[str, Any]]:
    """The mission events derived from every applied domain event of the workspace."""
    with _connect_admin(native_jobs) as conn:
        events = conn.execute(
            "SELECT event_id, sequence, outcome, event_type, aggregate_id,"
            " occurred_at, payload FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND outcome='applied' ORDER BY sequence",
            (workspace_id,),
        ).fetchall()
    return derive_mission_events(list(events), mission_for_work=lambda *_: None)


def _mission(service, workspace_id: UUID, mission_id: UUID) -> dict[str, Any]:
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/missions/{mission_id}",
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_stop_pauses_job_and_release_resumes_from_saved_state(native_jobs) -> None:
    service = native_jobs.service
    store = _store(native_jobs)
    workspace_id = native_jobs.workspace_id
    work_id = _work_id(native_jobs)

    # A mission over the job's work item — the ledger state a stop must not perturb.
    project_id = uuid4()
    _project(service, workspace_id, project_id)
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
                    "objective": "Pause the linked work without changing the mission",
                    "risk_policy": "risk-parent",
                    "approval_policy": "approval-parent",
                    "effort_policy": "effort-parent",
                    "budget_policy": _BUDGET,
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
    _seed_budget(
        native_jobs,
        workspace_id,
        work_id=str(work_id),
        revision_id=str(native_jobs.item["revision_id"]),
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
    )
    assert status == 200, body
    before_mission = body["result"]["mission"]
    assert before_mission["status"] == "approved"
    before_derived = _derive(native_jobs, workspace_id)

    handler = _RecordingHandler(_receipt(native_jobs, "audit"))
    worker_id = f"worker-pause-{uuid4()}"
    config = WorkerConfig(
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=2,
    )
    worker = JobWorker(store, config, handler)
    worker.start()

    job_id = f"job-pause-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=1, resources=_resources())
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["job"]["job_id"] == job_id
    assert claimed["job"]["lease_seconds"] == 1
    fence = int(claimed["job"]["fence"])

    # A client principal engages the stop.
    _engage(service, workspace_id, uuid4())
    assert _stopped(service, workspace_id) is True

    # Renewal is refused while stopped.
    with pytest.raises(JobError) as excinfo:
        renew_lease(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
        )
    assert excinfo.value.code == "agent_stop_engaged"

    # Expire the lease; reconcile returns the job to the backlog (OMP-324 path).
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
            " WHERE job_id=%s",
            (job_id,),
        )
    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert reconciled["job_ids"] == [job_id]

    # A stopped tick does not settle — it reconciles and then leases nothing.
    assert worker.tick() is None
    assert handler.run_count == 0
    assert handler.observe_count == 0

    row = _job_row(native_jobs, job_id)
    assert row["status"] == "backlog"
    assert int(row["fence"]) == fence + 1
    assert row["worker_id"] is None
    assert row["lease_expires_at"] is None
    kinds = _job_event_kinds(native_jobs, job_id)
    assert kinds == ["enqueued", "claimed", "lease_expired"]
    assert not any(kind in {"failed", "cancelled", "settled"} for kind in kinds)
    assert _reservation_reason(native_jobs, job_id) == "lease_expired"

    # Claiming while stopped gives no job.
    claimed_while_stopped = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed_while_stopped["status"] == "applied"
    assert claimed_while_stopped["job"] is None

    # The stop changed no mission state and derived no mission event.
    stopped_mission = _mission(service, workspace_id, mission_id)
    assert stopped_mission["status"] == before_mission["status"]
    assert stopped_mission["revision"] == before_mission["revision"]
    assert stopped_mission["transitions"] == before_mission["transitions"]
    assert _derive(native_jobs, workspace_id) == before_derived

    # The owner releases; the next tick resumes from saved state.
    _release(service, workspace_id)
    assert _stopped(service, workspace_id) is False

    resumed = worker.tick()
    assert resumed == job_id
    assert handler.observe_count == 1
    assert handler.run_count == 0
    settled = _job_row(native_jobs, job_id)
    assert settled["status"] == "sealed"
    assert settled["settlement"]["outcome"] == "succeeded"
