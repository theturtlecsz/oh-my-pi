"""OMP-417: hash-chained orchestrator step log on the native job substrate.

The step log is an audit surface: a consumer reads the mission's steps in
mission order, a retried append of the same identity is a no-op, a retried
append with different fields is refused so one step identity never rewrites
history, and a job from another workspace cannot be written through.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import uuid4

import pytest
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.orchestrator.step_log import (
    STEP_EVENT_KIND,
    append_step,
    list_steps,
    open_intent,
)
from test_jobs_worker_loop import _connect, _connect_admin, _enqueue, _store
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
    store: NativeJobStore, workspace_id, job_id: str, step: Mapping[str, object]
) -> dict[str, object]:
    with store.transaction(workspace_id, OWNER) as cur:
        return append_step(
            store, cur, workspace_id=workspace_id, job_id=job_id, step=step
        )


def _event_count(native_jobs, job_id: str) -> int:
    with _connect_admin(native_jobs) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM omp_jobs.job_events WHERE job_id=%s AND kind=%s",
            (job_id, STEP_EVENT_KIND),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_appends_list_in_mission_order(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())

    first = _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 1))
    second = _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 2))
    third = _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 3))

    assert first["previous_event_sha256"] is None
    assert second["previous_event_sha256"] == first["event_sha256"]
    assert third["previous_event_sha256"] == second["event_sha256"]

    with store.transaction(native_jobs.workspace_id, OWNER) as cur:
        steps = list_steps(cur, native_jobs.workspace_id, mission_id)

    assert [step["step_index"] for step in steps] == [1, 2, 3]
    assert steps[0]["previous_event_sha256"] is None
    assert steps[1]["previous_event_sha256"] == steps[0]["event_sha256"]
    assert steps[2]["previous_event_sha256"] == steps[1]["event_sha256"]


def test_identical_replay_returns_stored_and_writes_nothing(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())
    step = _step(mission_id, 1)

    stored = _append(store, native_jobs.workspace_id, job_id, step)
    assert _event_count(native_jobs, job_id) == 1

    replayed = _append(store, native_jobs.workspace_id, job_id, dict(step))

    assert replayed == stored
    assert _event_count(native_jobs, job_id) == 1


def test_conflicting_replay_refused(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())

    _append(store, native_jobs.workspace_id, job_id, _step(mission_id, 1))
    assert _event_count(native_jobs, job_id) == 1

    with pytest.raises(JobError) as excinfo:
        _append(
            store,
            native_jobs.workspace_id,
            job_id,
            _step(mission_id, 1, rule_id="d41-merge-approval"),
        )

    assert excinfo.value.code == "revision_conflict"
    assert _event_count(native_jobs, job_id) == 1


def test_foreign_workspace_job_refused(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())
    foreign_workspace = uuid4()

    with pytest.raises(JobError) as excinfo:
        _append(store, foreign_workspace, job_id, _step(mission_id, 1))

    assert excinfo.value.code == "invalid_request"
    assert _event_count(native_jobs, job_id) == 0


def test_open_intent_finds_only_unfinished_intents(native_jobs) -> None:
    store = _store(native_jobs)
    job_id = f"job-orch-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    mission_id = str(uuid4())

    _append(
        store,
        native_jobs.workspace_id,
        job_id,
        _step(
            mission_id,
            1,
            kind="external_intent",
            external_ref="pr-42",
        ),
    )
    with store.transaction(native_jobs.workspace_id, OWNER) as cur:
        intent = open_intent(cur, native_jobs.workspace_id, mission_id, "pr-42")
    assert intent is not None
    assert intent["step_index"] == 1

    _append(
        store,
        native_jobs.workspace_id,
        job_id,
        _step(mission_id, 2, kind="external_done", external_ref="pr-42"),
    )
    with store.transaction(native_jobs.workspace_id, OWNER) as cur:
        assert open_intent(cur, native_jobs.workspace_id, mission_id, "pr-42") is None

    _append(
        store,
        native_jobs.workspace_id,
        job_id,
        _step(mission_id, 3, kind="external_intent", external_ref="pr-7"),
    )
    with store.transaction(native_jobs.workspace_id, OWNER) as cur:
        reopened = open_intent(cur, native_jobs.workspace_id, mission_id, "pr-7")
    assert reopened is not None
    assert reopened["step_index"] == 3

    # A re-opened intent is open again: the newest intent with no later done wins.
    _append(
        store,
        native_jobs.workspace_id,
        job_id,
        _step(mission_id, 4, kind="external_intent", external_ref="pr-42"),
    )
    with store.transaction(native_jobs.workspace_id, OWNER) as cur:
        reopened_42 = open_intent(cur, native_jobs.workspace_id, mission_id, "pr-42")
    assert reopened_42 is not None
    assert reopened_42["step_index"] == 4
