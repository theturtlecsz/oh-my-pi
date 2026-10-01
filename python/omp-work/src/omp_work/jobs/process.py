"""Single-process production runner and CLI entry points for native jobs (OMP-400-s03).

Provides run_worker, register_component, and check.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.probe import probe_job_id
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import Handler, JobWorker, Settlement, load_handler
from omp_work.operations.config import OperationsConfig
from omp_work.operations.fingerprints import code_fingerprint
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    RegisterResearchComponentCommand,
    RegisterResearchComponentPayload,
    ResearchComponentDescriptor,
)

__all__ = [
    "CapabilityRouter",
    "check",
    "load_config",
    "register_component",
    "run_worker",
]


class CapabilityRouter:
    """Dispatches job execution to handlers matching required capabilities or kind."""

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = handlers

    def _resolve(self, job: dict[str, Any]) -> Handler:
        for cap in job.get("required_capabilities") or []:
            if cap in self.handlers:
                return self.handlers[cap]
        kind = job.get("kind")
        if kind and kind in self.handlers:
            return self.handlers[kind]
        if "*" in self.handlers:
            return self.handlers["*"]
        if "default" in self.handlers:
            return self.handlers["default"]
        if len(self.handlers) == 1:
            return next(iter(self.handlers.values()))
        raise RuntimeError(
            f"No handler found for job {job.get('job_id')} with kind {job.get('kind')} "
            f"and required_capabilities {job.get('required_capabilities')}"
        )

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        return self._resolve(job).run(ctx, job)

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        return self._resolve(job).observe(ctx, job)


def load_config(path: str | Path) -> dict[str, Any]:
    """Load worker config JSON from a file path."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("config must be a JSON object")

    # Resolve relative bearer_file and ledger_path against config file directory
    if "bearer_file" in data and data["bearer_file"] is not None:
        bf = Path(data["bearer_file"])
        if not bf.is_absolute():
            data["bearer_file"] = str((p.parent / bf).resolve())
    if "ledger_path" in data and data["ledger_path"] is not None:
        lp = Path(data["ledger_path"])
        if not lp.is_absolute():
            data["ledger_path"] = str((p.parent / lp).resolve())

    return data


def _worker_capabilities(config: dict[str, Any]) -> list[str]:
    """Capabilities this process will register, matching ``run_worker``'s default."""
    caps = config.get("capabilities")
    if isinstance(caps, list):
        return [str(item) for item in caps]
    handlers = config.get("handlers")
    if isinstance(handlers, dict):
        return [str(item) for item in handlers]
    return []


def _get_operations_config(config: dict[str, Any]) -> OperationsConfig:
    ops = config.get("operations")
    if ops is None:
        return OperationsConfig.defaults()
    if isinstance(ops, OperationsConfig):
        return ops
    if isinstance(ops, dict):
        return OperationsConfig(
            config_dir=Path(ops["config_dir"]),
            state_dir=Path(ops["state_dir"]),
            data_dir=Path(ops["data_dir"]),
            database=ops.get("database", "omp_work"),
            host=ops.get("host", "127.0.0.1"),
            port=int(ops.get("port", 54321)),
            aws_profile=ops.get("aws_profile", "default"),
            aws_region=ops.get("aws_region", "us-east-1"),
            bucket=ops.get("bucket", "omp-work-ledger-037842804132-us-east-1"),
            prefix=ops.get("prefix", "work-ledger/v1"),
            endpoint_url=ops.get("endpoint_url"),
        )
    return OperationsConfig.defaults()


def run_worker(path: str | Path, once: bool = False) -> int:
    """Run JobWorker as one production process with an advisory lock and SIGTERM handling."""
    config = load_config(path)
    ops_config = _get_operations_config(config)
    worker_id = str(config["worker_id"])

    # Hold a pg advisory lock keyed on worker_id for the process lifetime; a second instance exits 3.
    # A worker whose capabilities include omp.orchestrator also holds the workspace
    # controller lock. A second such process, whatever its worker_id, exits 3.
    conn_kwargs = ops_config.connection_kwargs("omp_work_app")
    lock_conn = psycopg.connect(**conn_kwargs, autocommit=True)
    with lock_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (worker_id,)
        )
        row = cur.fetchone()
        if not row or not row[0]:
            lock_conn.close()
            sys.exit(3)
        if "omp.orchestrator" in _worker_capabilities(config):
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('omp_jobs.controller:' || %s, 0))",
                (str(config["workspace_id"]),),
            )
            held = cur.fetchone()
            if not held or not held[0]:
                lock_conn.close()
                sys.exit(3)

    # SIGTERM finishes the current job, exits 0, no drain.
    stop_event = threading.Event()

    def _sigterm_handler(_signum: int, _frame: Any) -> None:
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        store = NativeJobStore(ops_config)
        handlers_spec = config.get("handlers", {})
        loaded_handlers = {
            k: load_handler(v) if isinstance(v, dict) and "factory" in v else v
            for k, v in handlers_spec.items()
        }
        # Add capability router fallback under "*" so capability and kind routing both work
        if "*" not in loaded_handlers:
            loaded_handlers["*"] = CapabilityRouter(loaded_handlers)

        worker_config = dict(config)
        if "capabilities" not in worker_config:
            worker_config["capabilities"] = list(handlers_spec.keys())

        worker = JobWorker(store, worker_config, loaded_handlers)
        worker.start()

        if once:
            worker.tick()
        else:
            idle_sleep = float(config.get("idle_sleep", 0.1))
            worker.run_forever(stop_event=stop_event, idle_sleep=idle_sleep)
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        try:
            lock_conn.close()
        except (OSError, psycopg.Error):
            pass

    return 0


def register_component(
    path: str | Path,
    *,
    client: WorkClient | None = None,
) -> str:
    """Register the worker component via WorkClient and print its sha."""
    config = load_config(path)
    work_url = str(config["work_url"])
    workspace_id = UUID(str(config["workspace_id"]))
    bearer_file = Path(config["bearer_file"])

    handlers = config.get("handlers", {})
    capabilities = sorted(handlers.keys())
    artifact_sha = code_fingerprint()
    name = str(config.get("name") or config.get("component_name") or "jobs-worker")
    version = str(config.get("version") or config.get("component_version") or "1")
    roles = sorted(config.get("roles") or [])

    descriptor = {
        "contract_version": "research-component.v1",
        "kind": "worker",
        "name": name,
        "version": version,
        "artifact_sha256": artifact_sha,
        "roles": roles,
        "capabilities": capabilities,
    }
    comp_sha = sha256(descriptor)

    if client is None:
        client = WorkClient(
            base_url=work_url,
            workspace_id=workspace_id,
            bearer_file=bearer_file,
        )

    envelope = CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=RegisterResearchComponentCommand(
            type="register_research_component",
            payload=RegisterResearchComponentPayload(
                component_sha256=comp_sha,
                descriptor=ResearchComponentDescriptor.model_validate(descriptor),
            ),
        ),
    )
    client.execute(envelope)
    print(comp_sha)
    return comp_sha


def probe_lease_seconds(timeout: float, lease: int | None = None) -> int:
    """Return the probe job lease in seconds.

    ``None`` derives a short lease from ``timeout`` (a quarter of it, clamped to
    1..30). An explicit ``lease`` must be an int (not bool) in 1..3600 and below
    ``timeout``, otherwise ValueError.
    """
    if lease is None:
        return max(1, min(30, int(timeout // 4)))
    if isinstance(lease, bool) or not isinstance(lease, int):
        raise ValueError("lease must be an int in 1..3600 and less than timeout")
    if lease < 1 or lease > 3600 or lease >= timeout:
        raise ValueError("lease must be an int in 1..3600 and less than timeout")
    return lease


def check(
    path: str | Path,
    work_id: str | UUID | None = None,
    count: int = 1,
    timeout: float = 30.0,
    lease: int | None = None,
    sleep: float = 0.0,
) -> dict[str, Any]:
    """Enqueue probe jobs and poll until sealed with exactly one settled event."""
    config = load_config(path)
    ops_config = _get_operations_config(config)
    workspace_id = UUID(str(config["workspace_id"]))
    actor_id = UUID(str(config["actor_id"]))

    target_work_id = work_id or config.get("work_id")
    if target_work_id is None:
        raise ValueError("work_id is required for check")
    work_uuid = UUID(str(target_work_id))

    store = NativeJobStore(ops_config)
    lease_sec = probe_lease_seconds(timeout, lease)
    job_ids = [probe_job_id(sleep) for _ in range(count)]
    for jid in job_ids:
        enqueue_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=actor_id,
            job_id=jid,
            work_id=work_uuid,
            kind="compute",
            required_capabilities=["omp.probe"],
            resources={"cpu": 0, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=lease_sec,
        )

    deadline = time.monotonic() + timeout
    passed = False
    jobs_info: list[dict[str, Any]] = []

    conn_kwargs = ops_config.connection_kwargs("omp_work_app")
    with psycopg.connect(**conn_kwargs, row_factory=dict_row) as conn:
        while True:
            jobs_info.clear()
            all_sealed = True
            with conn.cursor() as cur:
                for jid in job_ids:
                    cur.execute(
                        "SELECT job_id, status FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                        (workspace_id, jid),
                    )
                    job_row = cur.fetchone()
                    status = job_row["status"] if job_row else None

                    cur.execute(
                        "SELECT count(*) AS cnt FROM omp_jobs.job_events WHERE job_id=%s AND kind='settled'",
                        (jid,),
                    )
                    event_row = cur.fetchone()
                    settled_count = int(event_row["cnt"]) if event_row else 0

                    if not (status == "sealed" and settled_count == 1):
                        all_sealed = False

                    jobs_info.append(
                        {
                            "job_id": jid,
                            "status": status,
                            "settled_events": settled_count,
                        }
                    )

            if all_sealed:
                passed = True
                break

            if time.monotonic() >= deadline:
                break

            time.sleep(0.05)

    return {
        "capability": "jobs",
        "passed": passed,
        "jobs": jobs_info,
    }
