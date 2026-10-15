"""OMP-595 r2: fixed task authority and cleanup bound model worker lifetimes."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.jobs import renew_lease
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator import operations, worker_sandbox
from test_orchestrator_worker_wiring import run, wiring

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_WALL_CLOCK_SECONDS = 7200
_ENTRY_TIMEOUT_S = 90.0
_JOIN_TIMEOUT_S = 30.0


def test_controls_only_renewal_keeps_fixed_task_deadline(wiring, monkeypatch):
    """A healthy renewal outlives the launch lease and cannot move the 7200s deadline."""
    state = wiring
    seen: dict[str, object] = {}
    manifests: list[dict[str, object]] = []
    failures: list[BaseException] = []
    entered = threading.Event()
    release = threading.Event()
    inner = worker_sandbox.run_sandboxed

    def blocking(argv, identity, recorder, policy, env, timeout, **kwargs):
        manifest_path = env.get("OMP_WORKER_MANIFEST")
        if not manifest_path:
            raise AssertionError("worker manifest was not bound")
        manifests.append(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
        entered.set()
        if not release.wait(_JOIN_TIMEOUT_S):
            raise AssertionError("sandbox release timed out")
        return inner(argv, identity, recorder, policy, env, timeout, **kwargs)

    def before_run(ctx):
        oldest = _persist_controls(ctx, {"wall_clock_seconds": _WALL_CLOCK_SECONDS})
        seen["oldest"] = oldest
        seen["deadline"] = operations._task_deadline(ctx)
        assert seen["deadline"] == oldest + timedelta(seconds=_WALL_CLOCK_SECONDS)

    def after_run(ctx, outcome):
        del outcome
        again = operations._task_deadline(ctx)
        assert again == seen["deadline"]
        seen["after"] = again

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", blocking)
    state.before_run = before_run
    state.after_run = after_run

    def drive() -> None:
        try:
            seen["outcome"] = run(state)
        except BaseException as exc:  # noqa: BLE001 - the tick thread must surface every failure
            failures.append(exc)

    thread = threading.Thread(target=drive, name="orchestrator-renewal-tick")
    thread.start()
    try:
        _wait_until(entered, thread, failures, _ENTRY_TIMEOUT_S, "sandbox entry")
        assert len(manifests) == 1
        assert len(state.posts) == 1
        ctx = state.contexts[0]
        renewed = renew_lease(
            NativeJobStore(ctx.config.ops),
            operation_id=str(uuid4()),
            workspace_id=ctx.workspace_id,
            actor_id=ctx.principal.actor_id,
            job_id=str(ctx.lease.job_id),
            worker_id=ctx.lease.worker_id,
            fence=ctx.lease.fence,
        )
        assert renewed["status"] == "applied"
        assert len(state.posts) == 1
        seen["renewed_lease"] = datetime.fromisoformat(str(renewed["job"]["lease_expires_at"]))
    finally:
        release.set()
        thread.join(_JOIN_TIMEOUT_S)
    if thread.is_alive():
        raise AssertionError("tick thread did not finish")
    if failures:
        raise failures[0]

    manifest = manifests[0]
    ctx = state.contexts[0]
    task_doc = _task_capability(ctx)
    expires_at = state.posts[0]["expires_at"]
    deadline = seen["oldest"] + timedelta(seconds=_WALL_CLOCK_SECONDS)
    assert expires_at == manifest["task_deadline"] == task_doc["expires_at"]
    assert datetime.fromisoformat(expires_at) == deadline
    initial_lease = datetime.fromisoformat(str(manifest["lease_expires_at"]))
    assert initial_lease < seen["renewed_lease"] < deadline
    assert seen["after"] == seen["deadline"] == deadline
    assert len(state.posts) == 1
    assert state.revoked == [state.grant["id"]]
    assert seen["outcome"].outcome == "succeeded"


def test_exhausted_published_budget_never_launches_worker(wiring):
    """An elapsed 60s item budget crashes before registering a grant or running."""
    state = wiring
    state.request_patch = {"controls": {}}

    def before_run(ctx):
        # wiring captures state.expiry before this hook; aging sooner would
        # prevent the fixture from reaching the implementation under test.
        assert datetime.fromisoformat(state.expiry) > datetime.now(UTC)
        with psycopg.connect(**ctx.config.ops.connection_kwargs("postgres")) as conn:
            aged = conn.execute(
                """
                WITH RECURSIVE tree(job_id, created_at) AS (
                    SELECT job_id, created_at FROM omp_jobs.jobs
                    WHERE workspace_id=%s AND work_id=%s
                    UNION
                    SELECT child.job_id, child.created_at
                    FROM omp_jobs.jobs AS child
                    JOIN tree ON child.parent_job_id = tree.job_id
                    WHERE child.workspace_id=%s
                )
                UPDATE omp_jobs.jobs
                SET created_at = clock_timestamp() - interval '120 seconds'
                WHERE workspace_id=%s AND job_id=(
                    SELECT job_id FROM tree ORDER BY created_at, job_id LIMIT 1
                )
                """,
                (ctx.workspace_id, ctx.work_id, ctx.workspace_id, ctx.workspace_id),
            )
        assert aged.rowcount == 1
        assert ctx.lease_ok() is True
        assert operations._task_deadline(ctx) is None

    state.before_run = before_run
    outcome = run(state)
    assert outcome.outcome == "crashed"
    assert outcome.data["code"] == "lease_dead"
    assert state.posts == []
    assert state.runs == []
    assert state.revoked == []


def test_live_renewal_preserves_nonzero_worker_exit(wiring):
    """A renewed live lease leaves exit 1 as worker failure, not lease death."""
    state = wiring
    state.request_patch = {"controls": {}}
    state.exit_code = 1
    lease_seconds = 2

    def before_run(ctx):
        initial_expiry = ctx.lease_expires_at()
        assert initial_expiry is not None
        with psycopg.connect(**ctx.config.ops.connection_kwargs("postgres")) as conn:
            updated = conn.execute(
                """
                UPDATE omp_jobs.jobs SET lease_seconds=%s
                WHERE workspace_id=%s AND job_id=%s AND worker_id=%s AND fence=%s
                  AND status='admitted'
                """,
                (lease_seconds, ctx.workspace_id, ctx.lease.job_id,
                 ctx.lease.worker_id, ctx.lease.fence),
            )
        assert updated.rowcount == 1
        renewed = renew_lease(
            NativeJobStore(ctx.config.ops),
            operation_id=str(uuid4()),
            workspace_id=ctx.workspace_id,
            actor_id=ctx.principal.actor_id,
            job_id=str(ctx.lease.job_id),
            worker_id=ctx.lease.worker_id,
            fence=ctx.lease.fence,
        )
        assert renewed["status"] == "applied"
        assert datetime.fromisoformat(renewed["job"]["lease_expires_at"]) > initial_expiry
        assert ctx.lease_expires_at() > initial_expiry
        assert ctx.lease_ok() is True

    def after_run(ctx, outcome):
        assert ctx.lease_ok() is True
        assert ctx.lease_expires_at() > datetime.now(UTC)
        deadline = operations._task_deadline(ctx)
        assert deadline == datetime.fromisoformat(state.expiry)
        assert (deadline - datetime.now(UTC)).total_seconds() > lease_seconds
        assert outcome.outcome == "crashed"
        assert outcome.data["exit_code"] == 1
        assert outcome.data.get("code") != "lease_dead"

    state.before_run = before_run
    state.after_run = after_run
    assert run(state).outcome == "crashed"
    assert len(state.posts) == 1
    assert len(state.runs) == 1
    assert state.revoked == [state.grant["id"]]


@pytest.mark.parametrize("failure_at", ["manifest_write", "sandbox"])
def test_post_grant_exception_revokes_and_removes_manifest(wiring, monkeypatch, failure_at):
    """Local write/launch failures must not leak a live grant or its manifest."""
    state = wiring
    paths: list[Path] = []
    sandbox_entries: list[Path] = []
    manifest_fd: int | None = None
    failure = RuntimeError(f"post-grant {failure_at} failure")

    with monkeypatch.context() as patches:
        def before_run(ctx):
            del ctx
            mkstemp = operations.tempfile.mkstemp
            dump = operations.json.dump

            def capture_manifest(*args, **kwargs):
                nonlocal manifest_fd
                fd, name = mkstemp(*args, **kwargs)
                if kwargs.get("prefix") == "omp-worker-":
                    manifest_fd = fd
                    paths.append(Path(name))
                return fd, name

            def write_manifest(doc, stream, *args, **kwargs):
                if (
                    failure_at == "manifest_write"
                    and manifest_fd is not None
                    and getattr(stream, "name", None) == manifest_fd
                ):
                    raise failure
                return dump(doc, stream, *args, **kwargs)

            def fail_sandbox(*args, **kwargs):
                path = kwargs["manifest_path"]
                sandbox_entries.append(path)
                assert failure_at == "sandbox", "manifest write failure entered sandbox"
                assert paths == [path]
                manifest = json.loads(path.read_text(encoding="utf-8"))
                assert manifest["model"]["proxy"]["baseUrl"] == state.url
                raise failure

            patches.setattr(operations.tempfile, "mkstemp", capture_manifest)
            patches.setattr(operations.json, "dump", write_manifest)
            patches.setattr(operations, "_sandbox", fail_sandbox)

        state.before_run = before_run
        with pytest.raises(RuntimeError, match=f"^post-grant {failure_at} failure$") as raised:
            run(state)

    assert raised.value is failure
    assert len(state.posts) == 1
    assert state.revoked == [state.grant["id"]]
    assert len(paths) == 1
    assert not paths[0].exists()
    assert sandbox_entries == (paths if failure_at == "sandbox" else [])
    assert state.runs == []


def _persist_controls(ctx, controls: dict[str, int]):
    """Drop the item budget and store controls on the submitted step and the live request.

    Receipt rows reject DELETE until their user triggers are disabled. The same
    transaction enables those triggers again before it commits.
    """
    ctx.request = {**ctx.request, "controls": dict(controls)}
    mission_id = str(ctx.mission_id)
    with psycopg.connect(**ctx.config.ops.connection_kwargs("postgres"), row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("ALTER TABLE omp_evidence.receipts DISABLE TRIGGER USER")
            try:
                deleted = conn.execute(
                    """
                    DELETE FROM omp_evidence.receipts
                    WHERE workspace_id=%s AND work_id=%s AND kind='intake_publication'
                      AND EXISTS (
                        SELECT 1
                        FROM omp_jobs.jobs AS job
                        JOIN omp_jobs.job_events AS event ON event.job_id = job.job_id
                        WHERE job.workspace_id=%s AND job.work_id=%s
                          AND event.kind='orch_step'
                          AND event.payload->>'mission_id'=%s
                      )
                    """,
                    (ctx.workspace_id, ctx.work_id, ctx.workspace_id, ctx.work_id, mission_id),
                )
                updated = conn.execute(
                    """
                    UPDATE omp_jobs.job_events AS event
                    SET payload = jsonb_set(event.payload, '{request,controls}', %s::jsonb, true)
                    FROM omp_jobs.jobs AS job
                    WHERE event.job_id = job.job_id
                      AND job.workspace_id=%s
                      AND event.kind='orch_step'
                      AND event.payload->>'mission_id'=%s
                      AND event.payload->>'kind'='submitted'
                    """,
                    (json.dumps(controls), ctx.workspace_id, mission_id),
                )
                oldest = conn.execute(
                    """
                    WITH RECURSIVE tree(job_id, created_at) AS (
                        SELECT job_id, created_at FROM omp_jobs.jobs
                        WHERE workspace_id=%s AND work_id=%s
                        UNION
                        SELECT child.job_id, child.created_at
                        FROM omp_jobs.jobs AS child
                        JOIN tree ON child.parent_job_id = tree.job_id
                        WHERE child.workspace_id=%s
                    )
                    SELECT MIN(tree.created_at) AS oldest
                    FROM tree
                    WHERE EXISTS (
                        SELECT 1
                        FROM omp_jobs.jobs AS job
                        JOIN omp_jobs.job_events AS event ON event.job_id = job.job_id
                        WHERE job.workspace_id=%s AND job.work_id=%s
                          AND event.kind='orch_step'
                          AND event.payload->>'mission_id'=%s
                    )
                    """,
                    (
                        ctx.workspace_id,
                        ctx.work_id,
                        ctx.workspace_id,
                        ctx.workspace_id,
                        ctx.work_id,
                        mission_id,
                    ),
                ).fetchone()
                stored = conn.execute(
                    """
                    SELECT event.payload#>'{request,controls}' AS controls
                    FROM omp_jobs.job_events AS event
                    JOIN omp_jobs.jobs AS job ON job.job_id = event.job_id
                    WHERE job.workspace_id=%s
                      AND event.kind='orch_step'
                      AND event.payload->>'mission_id'=%s
                      AND event.payload->>'kind'='submitted'
                    """,
                    (ctx.workspace_id, mission_id),
                ).fetchone()
            finally:
                conn.execute("ALTER TABLE omp_evidence.receipts ENABLE TRIGGER USER")
    assert deleted.rowcount >= 1
    assert updated.rowcount == 1
    assert stored is not None and stored["controls"] == controls
    assert oldest is not None and oldest["oldest"] is not None
    return oldest["oldest"]


def _task_capability(ctx) -> dict[str, object]:
    directory = ctx.config.ops.config_dir / "capabilities"
    path = directory / f"task-{ctx.mission_id}-{ctx.step_index}-implement.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_until(event, thread, failures, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set():
        if failures:
            raise failures[0]
        if not thread.is_alive():
            if failures:
                raise failures[0]
            raise AssertionError(f"tick thread exited before {label}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {label}")
        event.wait(min(0.2, remaining))
