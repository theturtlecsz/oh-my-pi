"""OMP-595-s06: a lost or frozen lease stops the real sandboxed stage child.

PostgreSQL integration with a real sandbox child and a database ``JobWorker``.
The implement stage runs a jailed process tree (a child plus a descendant). The
test lets it pass the initial one-second lease interval on renewals, then loses
the lease (cancel or reassignment) or engages the agent stop. The termination
bound is declared before the run and the death of every process carrying the
launch marker is measured on the host, not the signal. Namespace skips do not
count as a pass: without ``unshare`` the module fails.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.action_classify import ResolvedTarget
from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.store import JobError
from omp_work.orchestrator import operations
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import LeaseLost, OrchestratorHandler, submit
from orchestrator_e2e_support import (
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_stop_pause_resume import _engage, _release
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

# Declared before any run: from the loss or stop to the last jailed process dead.
TERMINATION_BOUND_S = 5.0
LEASE_SECONDS = 1

_CHILD_SCRIPT = r"""
import subprocess, sys, time
from pathlib import Path
marker = sys.argv[1]
descendant = subprocess.Popen(
    ["/usr/bin/python3", "-c", "import time\nwhile True: time.sleep(0.1)", marker]
)
start = time.monotonic()
while time.monotonic() - start < 120:
    Path("/work/beat").write_text(f"{time.monotonic() - start:.3f}")
    time.sleep(0.05)
sys.exit(0)
"""


def test_namespaces_are_available() -> None:
    """Fail, not skip, when the jail cannot be built: the cases below need a real child."""
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
        capture_output=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr


def _admin(service: Any) -> psycopg.Connection:
    return psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    )


def _live(marker: str) -> list[int]:
    """Host pids of every live, non-zombie process whose argv carries ``marker``."""
    pids: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, "cmdline").read_bytes()
            state = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if marker.encode() in raw and state != "Z":
            pids.append(int(entry.name))
    return pids


def _wait(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _death_seconds(marker: str) -> float:
    """Seconds until no process of the launch is alive (measured from the call)."""
    started = time.monotonic()
    while _live(marker):
        if time.monotonic() - started > TERMINATION_BOUND_S + 5:
            break
        time.sleep(0.01)
    return time.monotonic() - started


class _Ticker(threading.Thread):
    def __init__(self, worker: Any) -> None:
        super().__init__(name="lease-cancel-tick")
        self.worker = worker
        self.result: str | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.worker.tick()
        except BaseException as exc:  # surfaced by the test
            self.error = exc


def _job(service: Any, job_id: str) -> dict[str, Any]:
    with _admin(service) as conn:
        row = conn.execute(
            "SELECT status, fence, worker_id, settlement, lease_expires_at,"
            " lease_expires_at < clock_timestamp() AS expired"
            " FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


def _kinds(service: Any, job_id: str) -> list[str]:
    with _admin(service) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_jobs.job_events WHERE job_id=%s ORDER BY seq", (job_id,)
        ).fetchall()
    return [str(row["kind"]) for row in rows]


def _mission_jobs(service: Any, world: Any) -> int:
    with _admin(service) as conn:
        return int(
            conn.execute(
                "SELECT count(*) AS n FROM omp_jobs.jobs WHERE job_id LIKE %s",
                (f"orch:{world.mission_id}:%",),
            ).fetchone()["n"]
        )


def _steps(world: Any) -> list[dict[str, Any]]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _implement_outcomes(world: Any) -> list[dict[str, Any]]:
    return [
        step
        for step in _steps(world)
        if step.get("stage") == "implement" and step.get("kind") == "outcome"
    ]


def _ready(service: Any, tmp_path: Path, monkeypatch: Any) -> SimpleNamespace:
    """A mission whose next claimable job is the implement stage, run by the real jail."""
    marker = f"omp595-{uuid4().hex}"
    captured: list[Any] = []
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    world = open_e2e_world(
        service,
        tmp_path,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    world.request = replace(world.request, worker_argv=("/usr/bin/python3", "-c", _CHILD_SCRIPT, marker))
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"])))
    worker = open_worker(world)
    real_init = orchestrator.StageContext.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        if kwargs.get("stage") == "implement":
            captured.append(self)

    monkeypatch.setattr(orchestrator.StageContext, "__init__", spy)
    tick_until(worker, world, lambda v: any(s.get("stage") == "grant" and s.get("kind") == "advance" for s in v["steps"]))
    with _admin(service) as conn:
        job = conn.execute(
            "UPDATE omp_jobs.jobs SET lease_seconds=%s WHERE job_id LIKE %s AND status='backlog'"
            " RETURNING job_id",
            (LEASE_SECONDS, f"orch:{world.mission_id}:%"),
        ).fetchall()
    assert len(job) == 1
    return SimpleNamespace(
        world=world,
        worker=worker,
        marker=marker,
        captured=captured,
        job_id=str(job[0]["job_id"]),
        service=service,
    )


def _run_past_initial_interval(ready: SimpleNamespace, ticker: _Ticker) -> None:
    """The jailed tree is up (child plus descendant) and renewals carried it past the first lease."""
    service, job_id = ready.service, ready.job_id
    ticker.start()
    _wait(lambda: len(_live(ready.marker)) >= 2, 30, "jailed child and descendant")
    first = _job(service, job_id)
    assert first["status"] == "admitted"
    _wait(
        lambda: (row := _job(service, job_id))["lease_expires_at"] > first["lease_expires_at"]
        and _wait_past(first["lease_expires_at"], service, job_id),
        15,
        "renewal beyond the initial lease interval",
    )
    assert _live(ready.marker), "child must outlive the initial lease interval"
    assert ticker.is_alive()


def _wait_past(initial: datetime, service: Any, job_id: str) -> bool:
    with _admin(service) as conn:
        return bool(
            conn.execute("SELECT clock_timestamp() > %s AS past", (initial,)).fetchone()["past"]
        )


def _assert_late_writes_refused(ready: SimpleNamespace) -> None:
    """A stage context that outlived the lease refuses commands, actions, and step writes."""
    ctx = ready.captured[-1]
    before = _steps(ready.world)
    ran: list[str] = []
    with pytest.raises((LeaseLost, orchestrator.Frozen)):
        ctx.command("set_mission_status", {"mission_id": str(ready.world.mission_id)})
    with pytest.raises((LeaseLost, orchestrator.Frozen)):
        ctx.act(
            {"kind": "mission_abandon", "resource_id": str(ready.world.mission_id)},
            ResolvedTarget(),
            lambda _op, _resolved: ran.append("effect"),
            rule_id="terminal-with-basis",
        )
    with pytest.raises((LeaseLost, orchestrator.Frozen)):
        ctx.intents.record_intent("late-key", "push", "target")
    assert ran == []
    assert _steps(ready.world) == before


def _finish(ready: SimpleNamespace, *tickers: _Ticker) -> None:
    """End own threads and children: cancel the job, join, kill any leftover marker process."""
    try:
        cancel_job(
            ready.worker.store,
            operation_id=str(uuid4()),
            workspace_id=ready.world.workspace_id,
            actor_id=OWNER,
            job_id=ready.job_id,
            reason="test_cleanup",
        )
    except JobError:
        pass
    for ticker in tickers:
        if ticker.ident is not None:
            ticker.join(timeout=20)
    for pid in _live(ready.marker):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("recovered", [False, True], ids=["fresh", "recovered"])
@pytest.mark.parametrize("loss", ["cancel", "reassign"])
def test_lost_lease_kills_child_and_writes_nothing(
    service: Any, tmp_path: Path, monkeypatch: Any, recovered: bool, loss: str
) -> None:
    ready = _ready(service, tmp_path / "world", monkeypatch)
    world = ready.world
    runner = ready.worker
    if recovered:
        # A first worker claimed the stage and died; its lease lapses and a second worker recovers it.
        first = open_worker(world)
        claimed = claim_job(
            first.store,
            operation_id=str(uuid4()),
            workspace_id=world.workspace_id,
            actor_id=OWNER,
            worker_id=first.worker_id,
        )
        assert claimed["job"]["job_id"] == ready.job_id
        with _admin(service) as conn:
            conn.execute(
                "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
                " WHERE job_id=%s",
                (ready.job_id,),
            )
        runner = open_worker(world)
    ticker = _Ticker(runner)
    jobs_before = _mission_jobs(service, world)
    try:
        _run_past_initial_interval(ready, ticker)
        row = _job(service, ready.job_id)
        assert row["worker_id"] == runner.worker_id
        if recovered:
            kinds = _kinds(service, ready.job_id)
            assert "lease_expired" in kinds
            assert [s for s in _steps(world) if s.get("kind") == "recovery"]
        assert len(_live(ready.marker)) >= 2

        if loss == "cancel":
            cancel_job(
                runner.store,
                operation_id=str(uuid4()),
                workspace_id=world.workspace_id,
                actor_id=OWNER,
                job_id=ready.job_id,
                reason="owner_cancel",
            )
        else:
            with _admin(service) as conn:
                conn.execute("UPDATE omp_jobs.jobs SET fence = fence + 1 WHERE job_id=%s", (ready.job_id,))
        took = _death_seconds(ready.marker)
        assert _live(ready.marker) == [], "child or descendant survived the lease loss"
        assert took <= TERMINATION_BOUND_S

        ticker.join(timeout=20)
        assert not ticker.is_alive()
        assert ticker.error is None
        assert ticker.result == ready.job_id
        assert runner.cancel_requested.is_set()
        assert runner.cancel_reason in {"owner_cancel", "job_fence_stale"}

        # The killed child's result is stale: no outcome, no advance, no new job, no settlement.
        assert _implement_outcomes(world) == []
        assert not [s for s in _steps(world) if s.get("stage") == "implement" and s.get("kind") != "recovery"
                    and s.get("kind") in {"advance", "retry", "repair", "reroute", "reschedule", "end"}]
        assert _mission_jobs(service, world) == jobs_before
        assert _job(service, ready.job_id)["settlement"] is None
        _assert_late_writes_refused(ready)
    finally:
        _finish(ready, ticker)


def test_stop_kills_child_but_holds_the_lease_until_release(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    ready = _ready(service, tmp_path / "world", monkeypatch)
    world, worker, workspace_id = ready.world, ready.worker, ready.world.workspace_id
    ticker = _Ticker(worker)
    resumed: _Ticker | None = None
    other = open_worker(world)
    try:
        _run_past_initial_interval(ready, ticker)
        before = _job(service, ready.job_id)
        fence, jobs_before = int(before["fence"]), _mission_jobs(service, world)
        _engage(service, workspace_id, uuid4())
        took = _death_seconds(ready.marker)
        assert _live(ready.marker) == [], "child or descendant survived the stop"
        assert took <= TERMINATION_BOUND_S
        ticker.join(timeout=20)
        assert ticker.error is None
        assert ticker.result == ready.job_id
        assert worker.frozen_requested.is_set() and not worker.cancel_requested.is_set()

        # Frozen: no outcome, no settlement, nothing enqueued; late writes refuse.
        assert _implement_outcomes(world) == []
        assert _mission_jobs(service, world) == jobs_before
        _assert_late_writes_refused(ready)

        # Held past expiry with the same worker, fence, and reservation.
        _wait(lambda: _job(service, ready.job_id)["expired"], 10, "lease expiry during the stop")
        held = _job(service, ready.job_id)
        assert held["status"] == "admitted" and held["worker_id"] == worker.worker_id
        assert int(held["fence"]) == fence and held["settlement"] is None
        events = _kinds(service, ready.job_id)
        assert reconcile_jobs(
            worker.store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=OWNER,
        )["job_ids"] == []
        assert _kinds(service, ready.job_id) == events
        with _admin(service) as conn:
            released = conn.execute(
                "SELECT release_reason FROM omp_jobs.reservations WHERE job_id=%s", (ready.job_id,)
            ).fetchall()
        assert released and all(row["release_reason"] is None for row in released)
        try:
            claimed = claim_job(
                other.store,
                operation_id=str(uuid4()),
                workspace_id=workspace_id,
                actor_id=OWNER,
                worker_id=other.worker_id,
            )
        except JobError:
            claimed = {"job": None}
        assert claimed["job"] is None
        assert _job(service, ready.job_id)["worker_id"] == worker.worker_id

        # Release resumes the saved work once for the same worker.
        _release(service, workspace_id)
        resumed = _Ticker(worker)
        resumed.start()
        _wait(lambda: len(_live(ready.marker)) >= 2, 30, "resumed child and descendant")
        kinds = _kinds(service, ready.job_id)
        assert kinds.count("lease_resumed") == 1 and kinds.count("resume_claimed") == 1
        assert "lease_recovered" not in kinds
        assert len([s for s in _steps(world) if s.get("kind") == "recovery"]) == 1
        assert int(_job(service, ready.job_id)["fence"]) == fence
    finally:
        try:
            _release(service, workspace_id)
        except AssertionError:
            pass
        _finish(ready, ticker, *([resumed] if resumed else []))


def test_task_deadline_is_the_remaining_ceiling_and_never_widens(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """The sandbox timeout is the durable ceiling less item spend, not the renewable lease."""
    ready = _ready(service, tmp_path / "world", monkeypatch)
    world = ready.world
    handler = OrchestratorHandler(world.config)
    claim = orchestrator.LeaseClaim(
        job_id=ready.job_id, worker_id=ready.worker.worker_id, fence=1
    )
    with _admin(service) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status='admitted', worker_id=%s, fence=1,"
            " lease_expires_at = clock_timestamp() + interval '1 hour' WHERE job_id=%s",
            (claim.worker_id, claim.job_id),
        )
    handler.mission_id = world.mission_id

    def deadline(controls: dict[str, Any]) -> float | None:
        ctx = SimpleNamespace(
            config=world.config,
            workspace_id=world.workspace_id,
            principal=handler.principal,
            lease=claim,
            request={"controls": controls},
            lease_expires_at=lambda: datetime.now(UTC),
        )
        result = operations._task_deadline(ctx)
        return None if result is None else (result - datetime.now(UTC)).total_seconds()

    try:
        published = deadline({})
        assert published is not None and 0 < published <= 60  # the seeded item budget ceiling
        narrower = deadline({"wall_clock_seconds": 40})
        assert narrower is not None and narrower <= 40
        assert narrower < published
        wider = deadline({"wall_clock_seconds": 3000})
        assert wider is not None and wider <= 60  # a looser request ceiling never widens the budget
        assert deadline({"wall_clock_seconds": 1}) is None  # the tree is older than one second
    finally:
        _finish(ready)
