"""Real-process proof of R03 acceptance on omp_jobs (OMP-324-s08).

Proves committed-but-unacknowledged recovery and real-process crash safety across
child processes building NativeJobStore on OperationsConfig:
- settle_job, record_usage, cancel_job: replay after child os._exit(137) before printing;
- deliver_outbox: child crash after append leaves committed outbox row, parent closes without re-append;
- stale worker: child A claims, lease expires, reconcile_jobs, child B claims and settles, A's settle fails job_fence_stale;
- cancel: child cancel_job blocks a new process's enqueue under it with job_cancelled, claim returns None.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import reconcile_jobs, settle_job
from omp_work.jobs.outbox import deliver_outbox
from omp_work.jobs.store import JobError, NativeJobStore, register_worker
from omp_work.jobs.usage import record_usage
from omp_work.operations.config import OperationsConfig
from test_research_contract import _register_component

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _config_json(config: OperationsConfig) -> str:
    return json.dumps({
        "config_dir": str(config.config_dir),
        "state_dir": str(config.state_dir),
        "data_dir": str(config.data_dir),
        "database": config.database,
        "host": config.host,
        "port": config.port,
        "aws_profile": config.aws_profile,
        "aws_region": config.aws_region,
        "bucket": config.bucket,
        "prefix": config.prefix,
        "endpoint_url": config.endpoint_url,
    })


def _receipt(native_jobs, role: str = "audit") -> dict[str, object]:
    return {
        "role": role,
        "issuer_component_sha256": native_jobs.components[role],
        "evidence": {
            "id": f"ev-{uuid4()}",
            "kind": "receipt",
            "content_digest": "abcd1234ef",
            "trust": "verified",
        },
    }


def _worker(
    native_jobs,
    store: NativeJobStore,
    capabilities: list[str],
    *,
    capacity: int = 1,
) -> str:
    component = _register_component(
        native_jobs.service,
        native_jobs.workspace_id,
        "worker",
        name=f"proc-{uuid4()}",
        capabilities=tuple(sorted(set(capabilities))),
    )
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=component,
        capabilities=capabilities,
        capacity=capacity,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def _run_child(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_child(proc: subprocess.Popen, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        raise


_CHILD_PRELUDE = """
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from omp_work.jobs.store import NativeJobStore
from omp_work.operations.config import OperationsConfig

d = json.loads(sys.argv[1])
config = OperationsConfig(
    config_dir=Path(d["config_dir"]),
    state_dir=Path(d["state_dir"]),
    data_dir=Path(d["data_dir"]),
    database=d["database"],
    host=d["host"],
    port=int(d["port"]),
    aws_profile=d["aws_profile"],
    aws_region=d["aws_region"],
    bucket=d["bucket"],
    prefix=d["prefix"],
    endpoint_url=d["endpoint_url"],
)
store = NativeJobStore(config)
call_args = json.loads(sys.argv[2])
"""


def test_process_recovery_settle_job(native_jobs) -> None:
    """Child settles job and exits 137 before printing; parent replay recovers stored settlement."""
    store = _store(native_jobs)
    worker_id = _worker(native_jobs, store, ["compute.cpu"])
    job_id = f"job-settle-{uuid4()}"
    work_id = UUID(native_jobs.item["work_id"])

    enqueue_res = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
    assert enqueue_res["status"] == "applied"

    claim_res = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claim_res["status"] == "applied"
    assert claim_res["job"]["job_id"] == job_id
    fence = claim_res["job"]["fence"]

    operation_id = str(uuid4())
    receipts = [_receipt(native_jobs, "audit")]
    settle_args = {
        "operation_id": operation_id,
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "job_id": job_id,
        "worker_id": worker_id,
        "fence": fence,
        "outcome": "succeeded",
        "receipts": receipts,
    }

    script = _CHILD_PRELUDE + """
from omp_work.jobs.lease import settle_job

settle_job(
    store,
    operation_id=call_args["operation_id"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    job_id=call_args["job_id"],
    worker_id=call_args["worker_id"],
    fence=int(call_args["fence"]),
    outcome=call_args["outcome"],
    receipts=call_args["receipts"],
)
os._exit(137)
print("committed, response lost")
"""

    proc = _run_child(
        script,
        _config_json(native_jobs.service.config),
        json.dumps(settle_args),
    )
    rc, stdout, stderr = _wait_child(proc)
    assert rc == 137
    assert stdout == ""

    # Parent resubmits same operation_id and arguments
    replayed = settle_job(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
        outcome="succeeded",
        receipts=receipts,
    )
    assert replayed["status"] == "replayed"
    assert replayed["job"]["status"] == "sealed"
    assert replayed["settlement"]["outcome"] == "succeeded"
    assert replayed["settlement"]["worker_id"] == worker_id
    assert replayed["settlement"]["fence"] == fence
    assert replayed["settlement"]["receipts"] == receipts

    with _connect(native_jobs) as conn:
        ops = conn.execute(
            "SELECT * FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchall()
        assert len(ops) == 1

        events = conn.execute(
            "SELECT * FROM omp_jobs.job_events WHERE job_id=%s AND kind='settled'",
            (job_id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["operation_id"] == operation_id

        reservations = conn.execute(
            "SELECT * FROM omp_jobs.reservations WHERE job_id=%s",
            (job_id,),
        ).fetchall()
        assert len(reservations) == 1
        assert reservations[0]["release_reason"] == "settled"
        assert reservations[0]["released_at"] is not None


def test_process_recovery_record_usage(native_jobs) -> None:
    """Child records usage and exits 137 before printing; parent replay recovers usage row and outbox row."""
    store = _store(native_jobs)
    work_id = UUID(native_jobs.item["work_id"])
    operation_id = str(uuid4())
    request_id = f"req-{uuid4()}"

    usage_args = {
        "operation_id": operation_id,
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "work_id": str(work_id),
        "job_id": None,
        "request_id": request_id,
        "role": "coder",
        "model": "gemini-3.8-flash",
        "input_tokens": 120,
        "output_tokens": 60,
        "cache_tokens": 30,
        "measurement": "measured",
        "price_usd": "0.0050000000",
        "price_version": "v1",
    }

    script = _CHILD_PRELUDE + """
from omp_work.jobs.usage import record_usage

record_usage(
    store,
    operation_id=call_args["operation_id"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    work_id=UUID(call_args["work_id"]),
    job_id=call_args.get("job_id"),
    request_id=call_args["request_id"],
    role=call_args["role"],
    model=call_args["model"],
    input_tokens=call_args["input_tokens"],
    output_tokens=call_args["output_tokens"],
    cache_tokens=call_args["cache_tokens"],
    measurement=call_args["measurement"],
    price_usd=call_args["price_usd"],
    price_version=call_args.get("price_version"),
)
os._exit(137)
print("committed, response lost")
"""

    proc = _run_child(
        script,
        _config_json(native_jobs.service.config),
        json.dumps(usage_args),
    )
    rc, stdout, stderr = _wait_child(proc)
    assert rc == 137
    assert stdout == ""

    # Parent resubmits same operation_id and arguments
    replayed = record_usage(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=None,
        request_id=request_id,
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=120,
        output_tokens=60,
        cache_tokens=30,
        measurement="measured",
        price_usd="0.0050000000",
        price_version="v1",
    )
    assert replayed["status"] == "replayed"
    usage_id = replayed["usage_id"]
    assert isinstance(usage_id, str) and len(usage_id) == 64

    with _connect(native_jobs) as conn:
        ops = conn.execute(
            "SELECT * FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchall()
        assert len(ops) == 1

        usages = conn.execute(
            "SELECT * FROM omp_jobs.usage_events WHERE usage_id=%s",
            (usage_id,),
        ).fetchall()
        assert len(usages) == 1
        assert usages[0]["tokens"] == 210

        outbox = conn.execute(
            "SELECT * FROM omp_jobs.outbox WHERE event_id=%s",
            (f"usage:{usage_id}",),
        ).fetchall()
        assert len(outbox) == 1
        assert outbox[0]["state"] == "committed"


def test_process_recovery_cancel_job(native_jobs) -> None:
    """Child cancels job and exits 137 before printing; parent replay recovers cancellation."""
    store = _store(native_jobs)
    worker_id = _worker(native_jobs, store, ["compute.cpu"])
    job_id = f"job-cancel-{uuid4()}"
    work_id = UUID(native_jobs.item["work_id"])

    enqueue_res = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
    assert enqueue_res["status"] == "applied"

    claim_res = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claim_res["status"] == "applied"
    assert claim_res["job"]["job_id"] == job_id

    operation_id = str(uuid4())
    reason = "test-cancel-process"
    cancel_args = {
        "operation_id": operation_id,
        "workspace_id": str(native_jobs.workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "job_id": job_id,
        "reason": reason,
    }

    script = _CHILD_PRELUDE + """
from omp_work.jobs.cancel import cancel_job

cancel_job(
    store,
    operation_id=call_args["operation_id"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    job_id=call_args["job_id"],
    reason=call_args["reason"],
)
os._exit(137)
print("committed, response lost")
"""

    proc = _run_child(
        script,
        _config_json(native_jobs.service.config),
        json.dumps(cancel_args),
    )
    rc, stdout, stderr = _wait_child(proc)
    assert rc == 137
    assert stdout == ""

    # Parent resubmits same operation_id and arguments
    replayed = cancel_job(
        store,
        operation_id=operation_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason=reason,
    )
    assert replayed["status"] == "replayed"
    assert job_id in replayed["job_ids"]

    with _connect(native_jobs) as conn:
        ops = conn.execute(
            "SELECT * FROM omp_jobs.operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchall()
        assert len(ops) == 1

        events = conn.execute(
            "SELECT * FROM omp_jobs.job_events WHERE job_id=%s AND kind='cancelled'",
            (job_id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["operation_id"] == operation_id

        reservations = conn.execute(
            "SELECT * FROM omp_jobs.reservations WHERE job_id=%s",
            (job_id,),
        ).fetchall()
        assert len(reservations) == 1
        assert reservations[0]["release_reason"] == "cancelled"
        assert reservations[0]["released_at"] is not None

        job_row = conn.execute(
            "SELECT status, cancel_reason FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "cancelled"
        assert job_row["cancel_reason"] == reason


def test_process_recovery_deliver_outbox(native_jobs, tmp_path) -> None:
    """Child delivers outbox and patches recovery.close to os._exit(137); parent delivers 0 and ledger has one event."""
    store = _store(native_jobs)
    workspace_id = uuid4()
    work_id = UUID(native_jobs.item["work_id"])
    ledger_path = tmp_path / "deliver_outbox_ledger.json"

    usage_res = record_usage(
        store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=work_id,
        job_id=None,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=10,
        output_tokens=5,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.001",
    )
    usage_id = usage_res["usage_id"]

    script = _CHILD_PRELUDE + """
from omp_work.contracts.v1 import recovery
from omp_work.jobs.outbox import deliver_outbox

def patched_close(record):
    os._exit(137)

recovery.close = patched_close

deliver_outbox(
    store,
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    ledger_path=Path(call_args["ledger_path"]),
)
os._exit(137)
print("committed, response lost")
"""

    deliver_args = {
        "workspace_id": str(workspace_id),
        "actor_id": str(native_jobs.actor_id),
        "ledger_path": str(ledger_path),
    }

    proc = _run_child(
        script,
        _config_json(native_jobs.service.config),
        json.dumps(deliver_args),
    )
    rc, stdout, stderr = _wait_child(proc)
    assert rc == 137

    # Child appended the event before recovery.close exited
    events = json.loads(ledger_path.read_text())["events"]
    assert len(events) == 1
    assert events[0]["usage_id"] == usage_id

    # The DB row is still committed because transaction was never committed by child
    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT state FROM omp_jobs.outbox WHERE event_id=%s",
            (f"usage:{usage_id}",),
        ).fetchone()
        assert row["state"] == "committed"

    # Parent's deliver_outbox appends nothing
    appended = deliver_outbox(
        store,
        workspace_id=workspace_id,
        actor_id=native_jobs.actor_id,
        ledger_path=ledger_path,
    )
    assert appended == 0

    # Ledger has one event per usage_id
    events_after = json.loads(ledger_path.read_text())["events"]
    assert len(events_after) == 1
    assert events_after[0]["usage_id"] == usage_id

    # Outbox row is closed
    with _connect(native_jobs) as conn:
        row = conn.execute(
            "SELECT state FROM omp_jobs.outbox WHERE event_id=%s",
            (f"usage:{usage_id}",),
        ).fetchone()
        assert row["state"] == "closed"


def test_process_recovery_stale_worker(native_jobs) -> None:
    """Child A claims (lease_seconds 1), lease expires, reconcile_jobs, child B claims and settles; A's settle fails job_fence_stale."""
    store = _store(native_jobs)
    worker_a = _worker(native_jobs, store, ["compute.cpu"])
    worker_b = _worker(native_jobs, store, ["compute.cpu"])
    job_id = f"job-stale-{uuid4()}"
    work_id = UUID(native_jobs.item["work_id"])

    enqueue_res = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=1,
    )
    assert enqueue_res["status"] == "applied"

    # Child A claims with lease_seconds 1 and exits 137 before printing
    script_claim_a = _CHILD_PRELUDE + """
from omp_work.jobs.admission import claim_job

claim_job(
    store,
    operation_id=call_args["operation_id"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    worker_id=call_args["worker_id"],
)
os._exit(137)
print("committed, response lost")
"""
    proc_a = _run_child(
        script_claim_a,
        _config_json(native_jobs.service.config),
        json.dumps({
            "operation_id": str(uuid4()),
            "workspace_id": str(native_jobs.workspace_id),
            "actor_id": str(native_jobs.actor_id),
            "worker_id": worker_a,
        }),
    )
    rc_a, stdout_a, stderr_a = _wait_child(proc_a)
    assert rc_a == 137

    with _connect(native_jobs) as conn:
        claimed_row = conn.execute(
            "SELECT status, worker_id, fence FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert claimed_row["status"] == "admitted"
        assert claimed_row["worker_id"] == worker_a
        assert claimed_row["fence"] == 1

    # Lease expires
    time.sleep(1.1)

    # reconcile_jobs returns the job to backlog
    reconciled = reconcile_jobs(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    assert reconciled["status"] == "applied"
    assert job_id in reconciled["job_ids"]

    # Child B claims and settles
    receipts = [_receipt(native_jobs, "audit")]
    script_b = _CHILD_PRELUDE + """
from omp_work.jobs.admission import claim_job
from omp_work.jobs.lease import settle_job

claimed = claim_job(
    store,
    operation_id=call_args["claim_op"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    worker_id=call_args["worker_id"],
)
fence = claimed["job"]["fence"]
settle_job(
    store,
    operation_id=call_args["settle_op"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    job_id=call_args["job_id"],
    worker_id=call_args["worker_id"],
    fence=fence,
    outcome="succeeded",
    receipts=call_args["receipts"],
)
os._exit(137)
print("committed, response lost")
"""
    proc_b = _run_child(
        script_b,
        _config_json(native_jobs.service.config),
        json.dumps({
            "claim_op": str(uuid4()),
            "settle_op": str(uuid4()),
            "workspace_id": str(native_jobs.workspace_id),
            "actor_id": str(native_jobs.actor_id),
            "worker_id": worker_b,
            "job_id": job_id,
            "receipts": receipts,
        }),
    )
    rc_b, stdout_b, stderr_b = _wait_child(proc_b)
    assert rc_b == 137

    # A's settle with fence 1 -> job_fence_stale
    with pytest.raises(JobError) as stale_err:
        settle_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            worker_id=worker_a,
            fence=1,
            outcome="succeeded",
            receipts=receipts,
        )
    assert stale_err.value.code == "job_fence_stale"

    # B's settlement unchanged
    with _connect(native_jobs) as conn:
        job_row = conn.execute(
            "SELECT status, settlement FROM omp_jobs.jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "sealed"
        assert job_row["settlement"]["worker_id"] == worker_b
        assert job_row["settlement"]["outcome"] == "succeeded"
        assert job_row["settlement"]["receipts"] == receipts


def test_process_recovery_cancel_and_blocked_admission(native_jobs) -> None:
    """After a child's cancel_job, a new process's enqueue under it fails with job_cancelled; claim returns None."""
    store = _store(native_jobs)
    worker_id = _worker(native_jobs, store, ["compute.cpu"])
    root_id = f"job-root-{uuid4()}"
    work_id = UUID(native_jobs.item["work_id"])

    enqueue_res = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=root_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
    assert enqueue_res["status"] == "applied"

    # Child cancels the root job and exits 137 before printing
    script_cancel = _CHILD_PRELUDE + """
from omp_work.jobs.cancel import cancel_job

cancel_job(
    store,
    operation_id=call_args["operation_id"],
    workspace_id=UUID(call_args["workspace_id"]),
    actor_id=UUID(call_args["actor_id"]),
    job_id=call_args["job_id"],
    reason=call_args["reason"],
)
os._exit(137)
print("committed, response lost")
"""
    proc_cancel = _run_child(
        script_cancel,
        _config_json(native_jobs.service.config),
        json.dumps({
            "operation_id": str(uuid4()),
            "workspace_id": str(native_jobs.workspace_id),
            "actor_id": str(native_jobs.actor_id),
            "job_id": root_id,
            "reason": "stop",
        }),
    )
    rc_c, stdout_c, stderr_c = _wait_child(proc_cancel)
    assert rc_c == 137

    # A new process's enqueue under it -> job_cancelled
    child_id = f"job-child-{uuid4()}"
    script_enqueue_under = _CHILD_PRELUDE + """
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import JobError

try:
    enqueue_job(
        store,
        operation_id=call_args["operation_id"],
        workspace_id=UUID(call_args["workspace_id"]),
        actor_id=UUID(call_args["actor_id"]),
        job_id=call_args["job_id"],
        parent_job_id=call_args["parent_job_id"],
        work_id=UUID(call_args["work_id"]),
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=60,
    )
except JobError as err:
    if err.code == "job_cancelled":
        sys.exit(42)
    sys.exit(1)
sys.exit(0)
"""
    proc_enq = _run_child(
        script_enqueue_under,
        _config_json(native_jobs.service.config),
        json.dumps({
            "operation_id": str(uuid4()),
            "workspace_id": str(native_jobs.workspace_id),
            "actor_id": str(native_jobs.actor_id),
            "job_id": child_id,
            "parent_job_id": root_id,
            "work_id": str(work_id),
        }),
    )
    rc_enq, stdout_enq, stderr_enq = _wait_child(proc_enq)
    assert rc_enq == 42

    with _connect(native_jobs) as conn:
        child_row = conn.execute(
            "SELECT * FROM omp_jobs.jobs WHERE job_id=%s",
            (child_id,),
        ).fetchone()
        assert child_row is None

    # claim returns None
    claim_res = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claim_res["status"] == "applied"
    assert claim_res["job"] is None
