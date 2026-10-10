"""Cross-workspace operation ledger (OMP-553).

The same operation id with the same request replays only inside the workspace
that stored it. Another workspace gets idempotency_conflict, its effect does
not run, and the stored result is not returned. The stored hash is bound to
the caller workspace, including when the body omits workspace_id or copies
another workspace's body.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.v1.canonical import sha256
from native_jobs_support import native_jobs  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _stored_hash(native_jobs, operation_id: str) -> str:
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        row = conn.execute(
            "SELECT request_sha256 FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_same_operation_and_request_do_not_replay_across_workspaces(native_jobs) -> None:
    """Workspace B reuses A's operation id and the identical request body.

    The body has no workspace id. The ledger hash still binds A's workspace,
    so B's copy of that body is a different request. B must not receive A's
    stored result, and B's effect must not run. A different body under that
    foreign id is also a conflict and does not carry A's result. A's own
    retry still replays.
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
    stored = _stored_hash(native_jobs, operation_id)
    assert stored == sha256(
        {"workspace_id": str(native_jobs.workspace_id), "request": request}
    )
    assert stored != sha256(request)

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
    assert sha256({"workspace_id": str(other), "request": request}) != stored

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


def test_copied_request_body_hashes_to_the_caller_workspace(native_jobs) -> None:
    """A body that names workspace A does not keep A's hash when B stores it.

    B also cannot replay A's operation by submitting that copied body.
    """
    store = NativeJobStore(native_jobs.service.config)
    operation_id = f"op-copy-{uuid4()}"
    body = {
        "workspace_id": str(native_jobs.workspace_id),
        "worker_id": "worker-secret",
        "capacity": 1,
    }
    secret = {"status": "applied", "token": "workspace-a-secret"}
    effects: list[str] = []

    def apply_a() -> dict[str, object]:
        effects.append("a")
        return secret

    def apply_b() -> dict[str, object]:
        effects.append("b")
        return {"status": "applied", "token": "workspace-b"}

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        first = store.run_operation(
            cur, operation_id, native_jobs.workspace_id, "worker_register", body, apply_a
        )
    assert first.state == "applied"
    assert first.result == secret
    owner_hash = _stored_hash(native_jobs, operation_id)
    assert owner_hash == sha256(body)

    other = uuid4()
    with store.transaction(other, native_jobs.actor_id) as cur:
        with pytest.raises(JobError) as refused:
            store.run_operation(
                cur, operation_id, other, "worker_register", body, apply_b
            )
    assert refused.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in refused.value.diagnostics)
    assert "workspace-a-secret" not in " ".join(refused.value.diagnostics)
    assert effects == ["a"]

    other_operation = f"op-copy-{uuid4()}"
    with store.transaction(other, native_jobs.actor_id) as cur:
        second = store.run_operation(
            cur, other_operation, other, "worker_register", body, apply_b
        )
    assert second.state == "applied"
    assert second.result == {"status": "applied", "token": "workspace-b"}
    assert "workspace-a-secret" not in str(second.result)
    other_hash = _stored_hash(native_jobs, other_operation)
    assert other_hash == sha256({"workspace_id": str(other), "request": body})
    assert other_hash != owner_hash
    assert effects == ["a", "b"]

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        replay = store.run_operation(
            cur, operation_id, native_jobs.workspace_id, "worker_register", body, apply_a
        )
    assert replay.state == "replayed"
    assert replay.result == secret
