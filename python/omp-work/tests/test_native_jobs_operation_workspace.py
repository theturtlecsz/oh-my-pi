"""Cross-workspace operation ledger (OMP-553).

The same operation id with the same request replays only inside the workspace
that stored it. Another workspace gets idempotency_conflict, its effect does
not run, and the stored result is not returned.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from omp_work.jobs.store import JobError, NativeJobStore
from native_jobs_support import native_jobs  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def test_same_operation_and_request_do_not_replay_across_workspaces(native_jobs) -> None:
    """Workspace B reuses A's operation id and the identical request.

    B must not receive A's stored result, and B's effect must not run. A
    different request under that foreign id is also a conflict and does not
    carry A's result. A's own retry still replays.
    """
    store = NativeJobStore(native_jobs.service.config)
    operation_id = f"op-cross-{uuid4()}"
    request = {"worker_id": "worker-secret", "capacity": 1}
    secret = {
        "status": "applied",
        "worker": {"worker_id": "worker-secret", "token": "workspace-a-secret"},
    }
    effects: list[str] = []

    def apply_a() -> dict[str, object]:
        effects.append("a")
        return secret

    def apply_b() -> dict[str, object]:
        effects.append("b")
        return {"status": "applied", "worker": {"worker_id": "worker-b"}}

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        first = store.run_operation(
            cur,
            operation_id,
            native_jobs.workspace_id,
            "worker_register",
            request,
            apply_a,
        )
    assert first.state == "applied"
    assert first.result == secret
    assert effects == ["a"]

    other = uuid4()
    with store.transaction(other, native_jobs.actor_id) as cur:
        with pytest.raises(JobError) as same:
            store.run_operation(
                cur, operation_id, other, "worker_register", request, apply_b
            )
        with pytest.raises(JobError) as changed:
            store.run_operation(
                cur,
                operation_id,
                other,
                "worker_register",
                {"worker_id": "worker-other", "capacity": 1},
                apply_b,
            )
    assert same.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in same.value.diagnostics)
    assert "workspace-a-secret" not in " ".join(same.value.diagnostics)
    assert changed.value.code == "idempotency_conflict"
    assert "workspace-a-secret" not in " ".join(changed.value.diagnostics)
    assert effects == ["a"]

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        replay = store.run_operation(
            cur,
            operation_id,
            native_jobs.workspace_id,
            "worker_register",
            request,
            apply_a,
        )
    assert replay.state == "replayed"
    assert replay.result == secret
    assert effects == ["a"]

    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        rows = conn.execute(
            "SELECT workspace_id, result FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == native_jobs.workspace_id
    assert rows[0][1]["worker"]["token"] == "workspace-a-secret"
