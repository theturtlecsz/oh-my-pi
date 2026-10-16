"""OMP-595 r2: one durable lease lifecycle across submission, renewal, loss, and freeze.

The ledger's r2 acceptance boundary is a single run, not four isolated slices:
a submission whose response is lost is replayed to the same work/job identity,
the published job is claimed by a real database ``JobWorker`` whose handler runs
real ``run_implement`` in the real sandbox jail, the run continues past the
initial lease on renewals, and is then stopped physically, refused a stale
completion, and (for the stop case) held with ownership and resumed exactly
once. The model-authority case repeats the published job through the real model
proxy with a fake provider, so the hard task deadline is proven independent of
the renewable lease.

A process that carries the launch marker must die within the declared bound,
measured on the host; a namespace probe fails, never skips, because a skip here
would hide the missing child rather than prove its termination.
"""

from __future__ import annotations

# ruff: noqa: F811  (the re-exported `binary` fixture is also a test parameter name)
import asyncio
import json
import os
import subprocess
import sys
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
from omp_work.action_classify import ResolvedTarget
from omp_work.jobs import worker as jobs_worker
from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs
from omp_work.jobs.store import JobError
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.service import LeaseLost, OrchestratorHandler
from omp_work.v1.server import create_app as work_app
from orchestrator_e2e_support import (
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from psycopg.rows import dict_row
from test_item_budget_alerts import _seed_budget
from test_jobs_worker_process import _config_dict, _env_for_child
from test_model_proxy import serving
from test_model_proxy_upstream import gemini_route
from test_orchestrator_production_worker import (
    CONTENT,
    CONTROL_TOKEN,
    PROVIDER_KEY,
    binary,  # noqa: F401 (fixture)
    upstream,
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
# The model case needs an interval longer than worker startup so the held
# provider response provably spans a committed renewal.
_MODEL_LEASE_SECONDS = 8
_GATE_TIMEOUT_S = 30.0

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

# The response-losing submitter: one load of config/request per process, one
# crash injected either before or after the publication commit.
_SUBMIT = """
import json, os, sys
from contextlib import contextmanager
from pathlib import Path
from omp_work.orchestrator import service as orch
ops = orch._ops_from(json.loads(sys.argv[3]))
config = orch.load_config(Path(sys.argv[1]), ops)
request = orch.load_request(Path(sys.argv[2]))
phase = sys.argv[4]
if phase == 'work_commit':
    execute = orch._execute
    def crash(*args, **kwargs):
        result = execute(*args, **kwargs)
        if args[2] == 'create_work_batch':
            os._exit(71)
        return result
    orch._execute = crash
if phase == 'publication_commit':
    transaction = orch.NativeJobStore.transaction
    append = orch.append_step
    published = False
    def record(*args, **kwargs):
        global published
        result = append(*args, **kwargs)
        published = True
        return result
    @contextmanager
    def lose_response(self, *args, **kwargs):
        with transaction(self, *args, **kwargs) as cur:
            yield cur
        if published:
            os._exit(72)
    orch.append_step = record
    orch.NativeJobStore.transaction = lose_response
print(json.dumps(orch.submit(config, request)))
"""


def test_namespaces_are_available() -> None:
    """Fail, not skip, when the jail cannot be built: the cases below need a real child."""
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
        capture_output=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr


def _tested(record_property: Any) -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record_property("tested_git_revision", revision)
    print(f"durable lease e2e tested_git_revision={revision}")
    return revision


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
        super().__init__(name="durable-lease-tick")
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


def _mission_jobs(service: Any, world: Any) -> list[str]:
    with _admin(service) as conn:
        rows = conn.execute(
            "SELECT job_id FROM omp_jobs.jobs WHERE job_id LIKE %s ORDER BY job_id",
            (f"orch:{world.mission_id}:%",),
        ).fetchall()
    return [str(row["job_id"]) for row in rows]


def _published_rows(service: Any, workspace_id: UUID) -> tuple[list[dict[str, Any]], int]:
    with _admin(service) as conn:
        jobs = conn.execute(
            "SELECT job_id, work_id, status FROM omp_jobs.jobs WHERE workspace_id=%s ORDER BY job_id",
            (workspace_id,),
        ).fetchall()
        items = int(
            conn.execute(
                "SELECT count(*) AS n FROM omp_work.work_items WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()["n"]
        )
    return jobs, items


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


def _fresh(service: Any, world: Any, phase: str = "normal") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _SUBMIT,
            str(world.config_path),
            str(world.request_path),
            json.dumps(_config_dict(service.config)),
            phase,
        ],
        env=_env_for_child(),
        capture_output=True,
        text=True,
        timeout=25,
    )


def _open_world(
    service: Any, tmp_path: Path, monkeypatch: Any, *, phase: str
) -> SimpleNamespace:
    """A disposable world whose submission identity is lost and replayed, then run by the jail."""
    marker = f"omp595-{uuid4().hex}"
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
    world.request = replace(
        world.request, worker_argv=("/usr/bin/python3", "-c", _CHILD_SCRIPT, marker)
    )
    _persist_request(world)
    return SimpleNamespace(
        world=world,
        marker=marker,
        captured=[],
        job_id=None,
        worker=None,
        service=service,
        phase=phase,
        monkeypatch=monkeypatch,
    )


def _persist_request(world: Any) -> None:
    """Keep the on-disk request the submitter loads identical to the in-memory one."""
    world.request_path.write_text(
        json.dumps(world.request.document(), indent=2), encoding="utf-8"
    )


def _replay_publication(ready: SimpleNamespace) -> dict[str, Any]:
    """Crash after work (or after publication), then replay the same identity exactly once."""
    world, service = ready.world, ready.service
    crashed = _fresh(service, world, ready.phase)
    assert crashed.returncode == (71 if ready.phase == "work_commit" else 72), crashed.stderr
    jobs, items = _published_rows(service, world.workspace_id)
    assert items == 1
    if ready.phase == "work_commit":
        assert jobs == [] and _steps(world) == []
    else:
        assert len(jobs) == 1

    replay = _fresh(service, world)
    assert replay.returncode == 0, replay.stderr
    result = json.loads(replay.stdout)
    assert result["status"] == "enqueued"

    jobs, items = _published_rows(service, world.workspace_id)
    assert items == 1 and len(jobs) == 1
    assert str(jobs[0]["work_id"]) == result["work_id"]
    assert jobs[0]["job_id"] == result["job_id"] == orchestrator._stage_job(world.mission_id, 0)
    steps = _steps(world)
    assert len(steps) == 1
    submitted = steps[0]
    assert submitted["kind"] == "submitted"
    assert submitted["request"] == world.request.document()
    assert submitted["work_id"] == result["work_id"]
    assert submitted["previous_event_sha256"] is None
    return result


def _prepare_published_job(ready: SimpleNamespace) -> None:
    """Seed the item budget, open the real worker, and stop at the implement stage."""
    service, world = ready.service, ready.world
    work_id = UUID(str(_published_rows(service, world.workspace_id)[0][0]["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )
    real_init = orchestrator.StageContext.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        if kwargs.get("stage") == "implement":
            ready.captured.append(self)

    ready.monkeypatch.setattr(orchestrator.StageContext, "__init__", spy)
    worker = open_worker(world)
    tick_until(
        worker,
        world,
        lambda v: any(s.get("stage") == "grant" and s.get("kind") == "advance" for s in v["steps"]),
    )
    with _admin(service) as conn:
        job = conn.execute(
            "UPDATE omp_jobs.jobs SET lease_seconds=%s WHERE job_id LIKE %s AND status='backlog'"
            " RETURNING job_id",
            (LEASE_SECONDS, f"orch:{world.mission_id}:%"),
        ).fetchall()
    assert len(job) == 1
    ready.worker = worker
    ready.job_id = str(job[0]["job_id"])


def _run_past_initial_interval(ready: SimpleNamespace, ticker: _Ticker) -> None:
    """The jailed tree is up (child plus descendant) and renewals carried it past the first lease."""
    service, job_id = ready.service, ready.job_id
    ticker.start()
    _wait(lambda: len(_live(ready.marker)) >= 2, 30, "jailed child and descendant")
    first = _job(service, job_id)
    assert first["status"] == "admitted"
    _wait(
        lambda: (row := _job(service, job_id))["lease_expires_at"] > first["lease_expires_at"]
        and _past(first["lease_expires_at"], service, job_id),
        15,
        "renewal beyond the initial lease interval",
    )
    assert _live(ready.marker), "child must outlive the initial lease interval"
    assert ticker.is_alive()


def _past(initial: datetime, service: Any, job_id: str) -> bool:
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


@pytest.mark.parametrize("phase", ["work_commit", "publication_commit"])
def test_lost_response_replays_one_job_and_runs_past_the_initial_lease(
    service: Any, tmp_path: Path, monkeypatch: Any, record_property: Any, phase: str
) -> None:
    """A lost submission response replays to the same identity and one runnable job."""
    _tested(record_property)
    ready = _open_world(service, tmp_path / phase, monkeypatch, phase=phase)
    result = _replay_publication(ready)
    _prepare_published_job(ready)
    ticker = _Ticker(ready.worker)
    try:
        _run_past_initial_interval(ready, ticker)
        assert _job(service, ready.job_id)["status"] == "admitted"
        assert result["job_id"] in _mission_jobs(service, ready.world)
        assert ready.job_id in _mission_jobs(service, ready.world)
    finally:
        _finish(ready, ticker)


@pytest.mark.parametrize("loss", ["cancel", "reassign"])
def test_lost_lease_stops_the_run_within_bound_and_refuses_stale_completion(
    service: Any, tmp_path: Path, monkeypatch: Any, record_property: Any, loss: str
) -> None:
    """The replayed run is killed physically in the bound and its stale result is not accepted."""
    _tested(record_property)
    ready = _open_world(service, tmp_path / "lost", monkeypatch, phase="work_commit")
    _replay_publication(ready)
    _prepare_published_job(ready)
    ticker = _Ticker(ready.worker)
    jobs_before = _mission_jobs(service, ready.world)
    try:
        _run_past_initial_interval(ready, ticker)
        assert len(_live(ready.marker)) >= 2
        if loss == "cancel":
            cancel_job(
                ready.worker.store,
                operation_id=str(uuid4()),
                workspace_id=ready.world.workspace_id,
                actor_id=OWNER,
                job_id=ready.job_id,
                reason="owner_cancel",
            )
        else:
            with _admin(service) as conn:
                conn.execute(
                    "UPDATE omp_jobs.jobs SET fence = fence + 1 WHERE job_id=%s", (ready.job_id,)
                )
        took = _death_seconds(ready.marker)
        print(f"durable lease e2e lost_lease[{loss}] termination_seconds={took:.3f}")
        assert _live(ready.marker) == [], "child or descendant survived the lease loss"
        assert took <= TERMINATION_BOUND_S

        ticker.join(timeout=20)
        assert not ticker.is_alive()
        assert ticker.error is None
        assert ticker.result == ready.job_id
        assert ready.worker.cancel_requested.is_set()
        assert ready.worker.cancel_reason in {"owner_cancel", "job_fence_stale"}

        # The killed child's result is stale: no outcome, no advance, no new job, no settlement.
        assert _implement_outcomes(ready.world) == []
        assert not [
            s
            for s in _steps(ready.world)
            if s.get("stage") == "implement"
            and s.get("kind") in {"advance", "retry", "repair", "reroute", "reschedule", "end"}
        ]
        assert _mission_jobs(service, ready.world) == jobs_before
        assert _job(service, ready.job_id)["settlement"] is None
        _assert_late_writes_refused(ready)
    finally:
        _finish(ready, ticker)


def test_frozen_stop_holds_ownership_then_release_resumes_once(
    service: Any, tmp_path: Path, monkeypatch: Any, record_property: Any
) -> None:
    """An engaged stop kills the run, keeps the lease, and one release resumes it once."""
    _tested(record_property)
    ready = _open_world(service, tmp_path / "frozen", monkeypatch, phase="publication_commit")
    _replay_publication(ready)
    _prepare_published_job(ready)
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
        print(f"durable lease e2e frozen_stop termination_seconds={took:.3f}")
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
        _wait(lambda: bool(_job(service, ready.job_id)["expired"]), 10, "lease expiry during stop")
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
            reserved = conn.execute(
                "SELECT release_reason FROM omp_jobs.reservations WHERE job_id=%s",
                (ready.job_id,),
            ).fetchall()
        assert reserved and all(row["release_reason"] is None for row in reserved)
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

        # Release resumes the saved work once, for the same worker, with no second effect.
        _release(service, workspace_id)
        resumed = _Ticker(worker)
        resumed.start()
        _wait(lambda: len(_live(ready.marker)) >= 2, 30, "resumed child and descendant")
        kinds = _kinds(service, ready.job_id)
        assert kinds.count("lease_resumed") == 1 and kinds.count("resume_claimed") == 1
        assert "lease_recovered" not in kinds
        assert len([s for s in _steps(world) if s.get("kind") == "recovery"]) == 1
        assert int(_job(service, ready.job_id)["fence"]) == fence
        assert _mission_jobs(service, world) == jobs_before
        assert _implement_outcomes(world) == []
    finally:
        try:
            _release(service, workspace_id)
        except AssertionError:
            pass
        _finish(ready, ticker, *([resumed] if resumed else []))


class _RenewalWatch:
    """Poll the claimed implement job for a committed renewal past the initial lease."""

    def __init__(self, db_kwargs: dict[str, Any], workspace_id: UUID) -> None:
        self.db_kwargs = db_kwargs
        self.workspace_id = workspace_id
        self.job_id: str | None = None
        self.worker_id: str | None = None
        self.fence: int | None = None
        self.claimed_expiry: datetime | None = None
        self.renewed: dict[str, Any] | None = None

    def target(self, job_id: str) -> None:
        self.job_id = job_id

    def claim(self, job: dict[str, Any]) -> None:
        self.worker_id = str(job["worker_id"])
        self.fence = int(job["fence"])
        # The public claim view serializes the expiry; the direct queries below
        # return a real timestamp, so normalize once here.
        expiry = job["lease_expires_at"]
        self.claimed_expiry = expiry if isinstance(expiry, datetime) else datetime.fromisoformat(expiry)

    def _row(self) -> dict[str, Any] | None:
        with psycopg.connect(**self.db_kwargs, row_factory=dict_row, autocommit=True) as conn:
            return conn.execute(
                "SELECT status, worker_id, fence, lease_expires_at,"
                " clock_timestamp() > %s AS past_claim,"
                " lease_expires_at > %s AS moved"
                " FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (self.claimed_expiry, self.claimed_expiry, self.workspace_id, self.job_id),
            ).fetchone()

    async def wait_for_renewal(self, timeout: float) -> dict[str, Any]:
        assert self.claimed_expiry is not None, "the implement job was never claimed"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self._row()
            if (
                row is not None
                and row["status"] == "admitted"
                and str(row["worker_id"]) == self.worker_id
                and int(row["fence"]) == self.fence
                and bool(row["moved"])
                and bool(row["past_claim"])
            ):
                self.renewed = row
                return row
            await asyncio.sleep(0.05)
        raise TimeoutError("timed out waiting for a committed renewal past the initial lease")


class _HoldFirstModelResponse:
    """Hold the first model response until a renewal past the initial lease is committed."""

    def __init__(self, app: Any, watch: _RenewalWatch, calls: list[Any]) -> None:
        self.app = app
        self.watch = watch
        self.calls = calls
        self.errors: list[Exception] = []
        self.model_calls = 0
        self.calls_at_renewal: int | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        is_model = scope["method"] == "POST" and path != "/tasks" and not path.startswith("/tasks/")
        if is_model:
            self.model_calls += 1
        hold = is_model and self.model_calls == 1

        async def send_recorded(message: Any) -> None:
            if message["type"] == "http.response.start" and hold:
                # The provider call is in flight; renewal must carry the run past
                # the initial lease before any provider bytes are forwarded.
                await self.watch.wait_for_renewal(_GATE_TIMEOUT_S)
                self.calls_at_renewal = len(self.calls)
            await send(message)

        try:
            await self.app(scope, receive, send_recorded)
        except Exception as exc:
            self.errors.append(exc)
            raise


def test_renewed_model_authority_bounds_the_replayed_job_by_the_hard_deadline(
    service: Any, tmp_path: Path, monkeypatch: Any, binary: Any, record_property: Any
) -> None:
    """A replayed job's real model worker crosses the lease under renewals, bounded by the deadline."""
    _tested(record_property)
    outfile, shim, native = binary
    monkeypatch.setenv("GEMINI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OMP_CONTROL_TOKEN", CONTROL_TOKEN)
    ready = _open_world(service, tmp_path / "model", monkeypatch, phase="work_commit")
    world = ready.world
    world.request = replace(
        world.request,
        worker_argv=(str(outfile), "orchestrator-worker"),
        scope=world.request.scope.model_copy(
            update={
                "budget_policy": world.request.scope.budget_policy.model_copy(
                    update={"tokens": 200_000}
                )
            }
        ),
    )
    capability = world.config.automation_capability_path
    data = json.loads(capability.read_text(encoding="utf-8"))
    data["token"] = CONTROL_TOKEN
    capability.write_text(json.dumps(data), encoding="utf-8")
    capability.chmod(0o600)
    key = service.config.credentials_dir / "gemini-key"
    key.write_text(PROVIDER_KEY, encoding="utf-8")
    key.chmod(0o600)

    _persist_request(world)
    result = _replay_publication(ready)
    work_id = UUID(result["work_id"])
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    _seed_budget(
        SimpleNamespace(service=service, workspace_id=world.workspace_id, actor_id=OWNER),
        work_id=work_id,
        revision_id=UUID(str(item["revision"]["revision_id"])),
        budget={"usd": "2.00", "tokens": 200_000, "wall_clock_seconds": 60, "max_subagents": 0},
    )

    watch = _RenewalWatch(service.config.connection_kwargs("postgres"), world.workspace_id)
    real_claim = jobs_worker.claim_job

    def observe_claim(*args: Any, **kwargs: Any) -> Any:
        observed = real_claim(*args, **kwargs)
        job = observed.get("job")
        if job is not None and str(job["job_id"]) == watch.job_id:
            watch.claim(dict(job))
        return observed

    monkeypatch.setattr(jobs_worker, "claim_job", observe_claim)
    manifests: list[dict[str, Any]] = []
    real_sandbox = worker_sandbox.run_sandboxed

    def observe_sandbox(
        argv: Any, identity: Any, recorder: Any, workdir: Any, env: Any, timeout: Any, **kwargs: Any
    ) -> int:
        manifest_path = env.get("OMP_WORKER_MANIFEST") if env else None
        if manifest_path:
            manifests.append(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
        kwargs["ro_binds"] = (*kwargs.get("ro_binds", ()), str(shim), str(native))
        return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", observe_sandbox)

    with upstream(False) as (transport, calls, _entered, release):
        config = ProxyConfig(
            service.config,
            capability,
            {"gemini_flash": gemini_route(model="gemini-3.8-flash-high")},
            overflow="queue",
        )
        app = _HoldFirstModelResponse(create_app(config, transport=transport), watch, calls)
        with (
            serving(app) as proxy_url,
            serving(work_app(service.config, capabilities_dir=service.capabilities)) as work_url,
        ):
            world.config = replace(
                world.config,
                model_proxy_url=proxy_url,
                workservice_url=work_url,
                lease_seconds=_MODEL_LEASE_SECONDS,
            )
            worker = open_worker(world)
            tick_until(
                worker,
                world,
                lambda v: any(
                    s.get("stage") == "grant" and s.get("kind") == "advance" for s in v["steps"]
                ),
            )
            with _admin(service) as conn:
                job = conn.execute(
                    "UPDATE omp_jobs.jobs SET lease_seconds=%s WHERE job_id LIKE %s"
                    " AND status='backlog' RETURNING job_id",
                    (_MODEL_LEASE_SECONDS, f"orch:{world.mission_id}:%"),
                ).fetchall()
            assert len(job) == 1
            implement_job = str(job[0]["job_id"])
            watch.target(implement_job)
            try:
                tick_until(
                    worker,
                    world,
                    lambda v: any(
                        s.get("stage") == "implement" and s.get("kind") == "outcome"
                        for s in v["steps"]
                    ),
                    limit=60,
                )
            finally:
                release.set()

    assert not app.errors
    assert watch.renewed is not None, "the provider response never spanned a renewal"
    assert app.calls_at_renewal == 1, "the first provider call was not in flight at the renewal"
    assert len(calls) >= 1
    outcome = _implement_outcomes(world)
    assert len(outcome) == 1
    assert outcome[0]["outcome"] == "succeeded"
    assert Path(outcome[0]["data"]["worktree"], "feature.txt").read_text(encoding="utf-8") == CONTENT

    # The replayed submission published one job and one submitted step, ran once.
    steps = _steps(world)
    assert sum(s.get("kind") == "submitted" for s in steps) == 1
    assert result["job_id"] in _mission_jobs(service, world)
    assert implement_job in _mission_jobs(service, world)

    # The fixed hard deadline outlives the initial lease and is the same for the
    # manifest and its task capability, so renewal cannot move it.
    assert len(manifests) == 1
    deadline = datetime.fromisoformat(manifests[0]["task_deadline"])
    assert deadline > watch.claimed_expiry.astimezone(UTC)
    cap_dir = world.config.ops.config_dir / "capabilities"
    cap_files = list(cap_dir.glob(f"task-{world.mission_id}-*-implement.json"))
    assert len(cap_files) == 1
    task_cap = json.loads(cap_files[0].read_text(encoding="utf-8"))
    assert task_cap["expires_at"] == manifests[0]["task_deadline"]
    assert task_cap["worker_id"] == watch.worker_id
    assert task_cap["fence"] == watch.fence
