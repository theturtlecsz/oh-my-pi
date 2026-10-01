"""OMP-417: tamper-evident verification of the two orchestrator chains.

A consumer of the audit surface must be able to tell a genuine ledger from one
whose history was edited: the appended domain events verify from the workspace
that aggregates them, the mission's step events verify in mission order, and a
single edited or removed row is localised to its sequence or step index.
``omp_work_app`` cannot edit the domain ledger at all, so only an owner-side
edit is detectable.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.audit_chain import verify_domain_chain, verify_step_chain
from omp_work.orchestrator.step_log import append_step
from test_jobs_worker_loop import _connect, _connect_admin, _enqueue, _store
from test_workflow_service import OWNER, _create, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _step(mission_id: str, step_index: int, **extra: object) -> dict[str, object]:
    step: dict[str, object] = {
        "mission_id": mission_id,
        "step_index": step_index,
        "kind": "rule",
        "idempotency_key": f"{mission_id}:{step_index}",
        "rule_id": "d40-advance",
    }
    step.update(extra)
    return step


def _append(
    store: NativeJobStore, workspace_id, job_id: str, step: dict[str, object]
) -> dict[str, object]:
    with store.transaction(workspace_id, OWNER) as cur:
        return append_step(
            store, cur, workspace_id=workspace_id, job_id=job_id, step=step
        )


def _seed_domain_workspace(native_jobs):
    """A fresh workspace with one applied domain event, aggregated under it."""
    service = native_jobs.service
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _create(service, workspace_id, "audit chain target")
    return workspace_id


def _domain_rows(native_jobs, workspace_id):
    with _connect_admin(native_jobs) as conn:
        rows = conn.execute(
            """
            SELECT sequence, payload
            FROM omp_audit.domain_events
            WHERE workspace_id=%s AND aggregate_id=%s AND outcome='applied'
            ORDER BY sequence
            """,
            (workspace_id, workspace_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _verify_domain(native_jobs, workspace_id):
    with _connect_admin(native_jobs) as conn:
        with conn.cursor() as cur:
            return verify_domain_chain(cur, workspace_id, workspace_id)


def _verify_steps(native_jobs, mission_id):
    with _connect_admin(native_jobs) as conn:
        with conn.cursor() as cur:
            return verify_step_chain(cur, native_jobs.workspace_id, mission_id)


def test_domain_chain_verifies(native_jobs) -> None:
    workspace_id = _seed_domain_workspace(native_jobs)

    assert len(_domain_rows(native_jobs, workspace_id)) >= 1
    assert _verify_domain(native_jobs, workspace_id) == (True, None)


def test_domain_events_refuse_update_and_delete_as_app(native_jobs) -> None:
    workspace_id = _seed_domain_workspace(native_jobs)

    with _connect(native_jobs) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE omp_audit.domain_events SET outcome='refused' WHERE workspace_id=%s",
                (workspace_id,),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "DELETE FROM omp_audit.domain_events WHERE workspace_id=%s",
                (workspace_id,),
            )


def test_altered_domain_event_reported_at_sequence(native_jobs) -> None:
    workspace_id = _seed_domain_workspace(native_jobs)
    target = _domain_rows(native_jobs, workspace_id)[0]["sequence"]

    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "ALTER TABLE omp_audit.domain_events DISABLE TRIGGER immutable_events"
        )
        try:
            conn.execute(
                "UPDATE omp_audit.domain_events SET payload = %s WHERE workspace_id=%s AND sequence=%s",
                (Jsonb({"tampered": True}), workspace_id, target),
            )
        finally:
            conn.execute(
                "ALTER TABLE omp_audit.domain_events ENABLE TRIGGER immutable_events"
            )

    assert _verify_domain(native_jobs, workspace_id) == (False, target)


def _mission_steps(native_jobs) -> tuple[str, str]:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())
    _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 1))
    _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 3))
    return job_id, mission_id


def test_step_chain_verifies(native_jobs) -> None:
    _job_id, mission_id = _mission_steps(native_jobs)
    assert _verify_steps(native_jobs, mission_id) == (True, None)


def _step_seq(native_jobs, job_id: str, step_index: int) -> int:
    with _connect_admin(native_jobs) as conn:
        row = conn.execute(
            """
            SELECT seq FROM omp_jobs.job_events
            WHERE job_id=%s AND kind='orch_step' AND payload->>'step_index' = %s
            """,
            (job_id, str(step_index)),
        ).fetchone()
    assert row is not None
    return int(row["seq"])


def test_altered_step_reported_at_step_index(native_jobs) -> None:
    job_id, mission_id = _mission_steps(native_jobs)
    seq = _step_seq(native_jobs, job_id, 2)
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            """
            UPDATE omp_jobs.job_events
            SET payload = payload || '{"rule_id":"tampered"}'::jsonb
            WHERE job_id=%s AND seq=%s
            """,
            (job_id, seq),
        )
    assert _verify_steps(native_jobs, mission_id) == (False, 2)


def test_deleted_step_reported_at_successor_step_index(native_jobs) -> None:
    job_id, mission_id = _mission_steps(native_jobs)
    seq = _step_seq(native_jobs, job_id, 1)
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "DELETE FROM omp_jobs.job_events WHERE job_id=%s AND seq=%s", (job_id, seq)
        )
    assert _verify_steps(native_jobs, mission_id) == (False, 2)
