"""Tests for step chain ordering across multiple jobs and tampering detection (OMP-417-s07-s01)."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.audit_chain import verify_step_chain
from omp_work.orchestrator.step_log import append_step
from omp_work.v1.canonical import sha256
from psycopg.types.json import Jsonb
from test_jobs_worker_loop import _connect_admin, _enqueue, _store
from test_workflow_service import OWNER

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
    store: NativeJobStore, workspace_id: object, job_id: str, step: dict[str, object]
) -> dict[str, object]:
    with store.transaction(workspace_id, OWNER) as cur:
        return append_step(
            store, cur, workspace_id=workspace_id, job_id=job_id, step=step
        )


def _verify_steps(native_jobs: object, mission_id: str) -> tuple[bool, int | None]:
    with _connect_admin(native_jobs) as conn, conn.cursor() as cur:
        return verify_step_chain(cur, native_jobs.workspace_id, mission_id)


def test_steps_across_reverse_lexical_jobs_verify(native_jobs: object) -> None:
    store = _store(native_jobs)
    mission_id = str(uuid4())

    job_z = f"z-{uuid4()}"
    job_a = f"a-{uuid4()}"
    assert job_z > job_a

    _enqueue(native_jobs, store, job_id=job_z)
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 1))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 3))

    _enqueue(native_jobs, store, job_id=job_a)
    _append(store, native_jobs.workspace_id, job_a, _step(mission_id, 4))
    _append(store, native_jobs.workspace_id, job_a, _step(mission_id, 5))

    ok, bad_index = _verify_steps(native_jobs, mission_id)
    assert (ok, bad_index) == (True, None)


def test_altered_step_fails_verification(native_jobs: object) -> None:
    store = _store(native_jobs)
    mission_id = str(uuid4())
    job_z = f"z-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_z)
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 1))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 3))

    with _connect_admin(native_jobs) as conn:
        conn.execute(
            """
            UPDATE omp_jobs.job_events
            SET payload = payload || '{"rule_id":"tampered"}'::jsonb
            WHERE job_id=%s AND payload->>'step_index' = '2'
            """,
            (job_z,),
        )

    ok, bad_index = _verify_steps(native_jobs, mission_id)
    assert not ok
    assert bad_index is not None and bad_index >= 2


def test_rehashed_step_fails_verification(native_jobs: object) -> None:
    store = _store(native_jobs)
    mission_id = str(uuid4())
    job_z = f"z-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_z)
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 1))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 3))

    with _connect_admin(native_jobs) as conn:
        row = conn.execute(
            """
            SELECT seq, payload
            FROM omp_jobs.job_events
            WHERE job_id=%s AND payload->>'step_index' = '2'
            """,
            (job_z,),
        ).fetchone()
        assert row is not None
        seq = row["seq"]
        payload = dict(row["payload"])
        payload["rule_id"] = "tampered-and-rehashed"
        body = {k: v for k, v in payload.items() if k != "event_sha256"}
        payload["event_sha256"] = sha256(body)
        conn.execute(
            "UPDATE omp_jobs.job_events SET payload=%s WHERE job_id=%s AND seq=%s",
            (Jsonb(payload), job_z, seq),
        )

    ok, bad_index = _verify_steps(native_jobs, mission_id)
    assert not ok
    assert bad_index is not None and bad_index >= 2


def test_deleted_step_fails_verification(native_jobs: object) -> None:
    store = _store(native_jobs)
    mission_id = str(uuid4())
    job_z = f"z-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_z)
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 1))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 3))

    with _connect_admin(native_jobs) as conn:
        conn.execute(
            """
            DELETE FROM omp_jobs.job_events
            WHERE job_id=%s AND payload->>'step_index' = '2'
            """,
            (job_z,),
        )

    ok, bad_index = _verify_steps(native_jobs, mission_id)
    assert not ok
    assert bad_index is not None and bad_index >= 2


def test_forked_step_fails_verification(native_jobs: object) -> None:
    store = _store(native_jobs)
    mission_id = str(uuid4())
    job_z = f"z-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_z)
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 1))
    s2 = _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 2))
    _append(store, native_jobs.workspace_id, job_z, _step(mission_id, 3))

    fork_payload = {
        "mission_id": mission_id,
        "step_index": 4,
        "kind": "rule",
        "idempotency_key": f"{mission_id}:4-fork",
        "rule_id": "forked-rule",
        "previous_event_sha256": s2["event_sha256"],
    }
    fork_payload["event_sha256"] = sha256(fork_payload)

    with _connect_admin(native_jobs) as conn:
        row = conn.execute(
            "SELECT max(seq) AS max_seq, max(at) AS max_at FROM omp_jobs.job_events WHERE job_id=%s",
            (job_z,),
        ).fetchone()
        assert row is not None
        next_seq = int(row["max_seq"]) + 1
        at = row["max_at"]
        conn.execute(
            """
            INSERT INTO omp_jobs.job_events (job_id, seq, actor, reason, kind, at, payload)
            VALUES (%s, %s, 'owner', 'fork-test', 'orch_step', %s, %s)
            """,
            (job_z, next_seq, at, Jsonb(fork_payload)),
        )

    ok, bad_index = _verify_steps(native_jobs, mission_id)
    assert not ok
    assert bad_index is not None and bad_index >= 3
